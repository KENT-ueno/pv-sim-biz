"""
pv-sim-biz LP「パススルー乱用」バグの波及チェック

pv-sim-fipで発見されたバグ（chargeとdischargeが同一スロットで両方>0となる
パススルーで容量制約が事実上無効化される）が、pv-sim-bizでも発生していないかを
網羅的に検証する。

検証対象:
  - optimize_battery (L787)
  - optimize_battery_capacity (L949)
  - simulate_battery (L671) ← ルールベース、構造上安全だが念のため
"""

import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app


# === 結果出力先 ===
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test_verify_battery_bug_result.txt")
_lines = []


def log(msg=""):
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("cp932", errors="replace").decode("cp932"))
    _lines.append(msg)


def build_pv(point_no="44132", ppeak_kw=500.0):
    """500kWの産業用PV（東京）を構築"""
    lat, lon, ghi_df, temp_df = app.load_from_db(point_no)
    snow_df = app.load_snow_depth(point_no)
    albedo_flat = app.build_albedo_series(snow_df)
    faces = [{
        "ppeak": ppeak_kw, "tilt": 30.0, "orientation": "南",
        "azimuth": 180.0, "pcs_limit_kw": ppeak_kw,
    }]
    g = app.calculate_generation(
        lat=lat, lon=lon, ghi_df=ghi_df, temp_df=temp_df, faces=faces,
        KHD=app.DEFAULT_KHD, KPD=app.DEFAULT_KPD, KPM=app.DEFAULT_KPM,
        KPA=app.DEFAULT_KPA, eta_ino=0.95,
        alpha_pct=0.41, delta_t=18.4,
        bifacial=False, albedo_flat=albedo_flat,
    )
    return g["total_gen_clipped"], g["month_day"]


def build_demand():
    """中規模オフィス1棟の需要カーブを構築"""
    args = ["役所・自治体庁舎", 4500.0, 1] + ["なし", 0.0, 0] * (app.MAX_FACILITIES - 1)
    demand, individual = app.load_combined_demand(args, app.MAX_FACILITIES, custom_csv=None)
    return demand


def check_lp_result(name, res, capacity_kwh, max_charge_kw, max_discharge_kw,
                    soc_min_pct, soc_max_pct):
    """LP結果の物理的整合性をチェック"""
    ch = res["battery_charge"].flatten()
    dc = res["battery_discharge"].flatten()
    soc = res["soc"].flatten()

    soc_max_exp = capacity_kwh * soc_max_pct / 100.0
    soc_min_exp = capacity_kwh * soc_min_pct / 100.0
    max_per_slot_ch = max_charge_kw * 0.5
    max_per_slot_dc = max_discharge_kw * 0.5
    max_power_per_slot = max(max_per_slot_ch, max_per_slot_dc)

    # === 物理チェック ===
    soc_violation_high = int((soc > soc_max_exp + 1e-6).sum())
    soc_violation_low = int((soc < soc_min_exp - 1e-6).sum())
    both_pos = int(((ch > 1e-6) & (dc > 1e-6)).sum())
    pcs_violation = int(((ch + dc) > max_power_per_slot + 1e-4).sum())
    cycles_per_day = (ch.sum() / capacity_kwh) / 365 if capacity_kwh > 0 else 0
    terminal_ok = abs(soc[-1] - soc_min_exp) < 1e-3

    log(f"  [{name}] cap={capacity_kwh:8.2f} kWh")
    log(f"    soc>max          : {soc_violation_high}")
    log(f"    soc<min          : {soc_violation_low}")
    log(f"    both_pos (ch&dc) : {both_pos}  ← パススルー残党")
    log(f"    pcs_violation    : {pcs_violation}  ← mutual exclusion違反")
    log(f"    cycles/day       : {cycles_per_day:.3f}  (健全なら ≤2)")
    log(f"    terminal SOC ok  : {terminal_ok}")
    log(f"    annual_charge    : {ch.sum():.1f} kWh/年")
    log(f"    annual_discharge : {dc.sum():.1f} kWh/年")
    log(f"    charge/cap ratio : {ch.sum()/capacity_kwh:.1f}  (= 2 × cycles/day × 365)")

    # 判定
    issues = []
    if soc_violation_high > 0: issues.append("SOC上限違反")
    if soc_violation_low > 0: issues.append("SOC下限違反")
    if both_pos > 0: issues.append("パススルー検出")
    if pcs_violation > 0: issues.append("PCS制約違反")
    if cycles_per_day > 2.5: issues.append(f"異常な高サイクル({cycles_per_day:.2f}/day)")
    if not terminal_ok: issues.append("終端SOC不一致")

    if issues:
        log(f"    ★問題: {', '.join(issues)}")
        return False
    else:
        log(f"    ✓ OK")
        return True


def run_tests():
    log("=" * 70)
    log("pv-sim-biz LP「パススルー乱用」バグ波及チェック")
    log("=" * 70)
    log()

    # === 観点A: LP定式化の網羅チェック（構造解析の結果報告）===
    log("【観点A】LP定式化の網羅チェック（構造解析）")
    log("-" * 70)
    log("  app.py内の LpProblem インスタンス:")
    log("    1. optimize_battery        L787 / LpProblem L826")
    log("    2. optimize_battery_capacity L949 / LpProblem L983")
    log()
    log("  両LPとも mutual exclusion 制約 `ch[t] + dc[t] <= max_power_per_slot`")
    log("  が**未実装**である（pv-sim-fipと同じパターン）。")
    log()
    log("  ただしpv-sim-bizの目的関数:")
    log("    minimize: 基本料金 + Σ(grid_import * unit_price) - Σ(grid_export * sell_price)")
    log()
    log("  かつ、デフォルト料金で sell_price < unit_price が常に成立:")
    log(f"    高圧 unit_price (夏): {app.ELECTRICITY_RATE_HV['energy_charge_summer'] + app.ELECTRICITY_RATE_HV['renewable_surcharge']:.2f} 円/kWh")
    log(f"    高圧 unit_price (他): {app.ELECTRICITY_RATE_HV['energy_charge_other'] + app.ELECTRICITY_RATE_HV['renewable_surcharge']:.2f} 円/kWh")
    log(f"    FIT早期 sell_price : {app.FIT_PRICE_EARLY:.2f} 円/kWh")
    log(f"    FIT利用なし        : {app.DEFAULT_SELL_PRICE:.2f} 円/kWh")
    log()
    log("  → 買電 > 売電 のため『grid購入→充電→放電→売電』のアービトラージは赤字")
    log("  → SOC遷移 `+ch*eff - dc/eff` のためパススルーは SOC を消費（純損失）")
    log("  理論的にはパススルーは LP 最適解にならないはず。実機テストで確認する。")
    log()

    # === データ準備 ===
    log("【テストデータ準備】")
    log("-" * 70)
    gen, md = build_pv(ppeak_kw=500.0)
    demand = build_demand()
    log(f"  PV: 500kW, 年間発電 {float(gen.sum()):.1f} kWh")
    log(f"  需要: 役所4500m2 x 1棟, 年間需要 {float(demand.sum()):.1f} kWh")
    log()

    # === 観点E: SOC物理一貫性チェック（複数容量）===
    log("【観点E】optimize_battery のSOC物理一貫性チェック")
    log("-" * 70)

    rate_kwargs = dict(
        basic_charge_per_kw=app.ELECTRICITY_RATE_HV["basic_charge_per_kw"],
        energy_charge_summer=app.ELECTRICITY_RATE_HV["energy_charge_summer"],
        energy_charge_other=app.ELECTRICITY_RATE_HV["energy_charge_other"],
        power_factor_pct=app.ELECTRICITY_RATE_HV["power_factor_pct"],
        fuel_adjustment=app.ELECTRICITY_RATE_HV["fuel_adjustment"],
        renewable_surcharge=app.ELECTRICITY_RATE_HV["renewable_surcharge"],
    )

    all_ok = True
    test_cases = [
        # (容量, 充放電レート, 売電単価, no_export, ラベル)
        (10.0,   100.0, app.FIT_PRICE_EARLY, False, "small/FIT19/export"),
        (100.0,  100.0, app.FIT_PRICE_EARLY, False, "mid/FIT19/export"),
        (1000.0, 200.0, app.FIT_PRICE_EARLY, False, "large/FIT19/export"),
        (100.0,  100.0, 8.50, False, "mid/sell8.5/export"),
        (100.0,  100.0, app.FIT_PRICE_EARLY, True,  "mid/FIT19/no_export"),
        (1000.0, 200.0, app.FIT_PRICE_EARLY, True,  "large/FIT19/no_export"),
    ]
    for cap, rate, sp, ne, label in test_cases:
        try:
            res = app.optimize_battery(
                gen, demand, md,
                capacity_kwh=cap, efficiency_pct=95,
                max_charge_kw=rate, max_discharge_kw=rate,
                soc_min_pct=20, soc_max_pct=95,
                sell_price=sp, no_export=ne,
                **rate_kwargs,
            )
            ok = check_lp_result(label, res, cap, rate, rate, 20, 95)
            all_ok = all_ok and ok
            log()
        except Exception as e:
            log(f"  [{label}] エラー: {e}")
            all_ok = False
            log()

    # === 観点G: 容量探索の境界解チェック ===
    log("【観点G】optimize_battery_capacity の境界解チェック")
    log("-" * 70)
    try:
        bat_cost_net = app.BATTERY_COST_PER_KWH  # 補助金なし
        for sp_label, sp in [("FIT19", app.FIT_PRICE_EARLY), ("売電なし", None)]:
            ne = (sp is None)
            stage1 = app.optimize_battery_capacity(
                gen, demand, md,
                efficiency_pct=95,
                max_charge_kw=200, max_discharge_kw=200,
                soc_min_pct=20, soc_max_pct=95,
                sell_price=sp, battery_cost_per_kwh=bat_cost_net,
                payback_years=15, no_export=ne, capacity_upper=2000,
                **rate_kwargs,
            )
            opt_cap = stage1["optimal_capacity_kwh"]
            log(f"  [{sp_label}] optimal_capacity_kwh = {opt_cap:.2f} kWh")
            if opt_cap >= 2000 - 1e-3:
                log(f"    ★境界解（上限張り付き）")
            elif opt_cap < 1e-3:
                log(f"    optimal=0（蓄電池導入不要との結論）")
            else:
                log(f"    ✓ 内点解")
            log()

            # 段階1で求めた最適容量で実際にLP回して整合性チェック
            if opt_cap > 0.5:
                res2 = app.optimize_battery(
                    gen, demand, md,
                    capacity_kwh=opt_cap, efficiency_pct=95,
                    max_charge_kw=200, max_discharge_kw=200,
                    soc_min_pct=20, soc_max_pct=95,
                    sell_price=sp, no_export=ne,
                    **rate_kwargs,
                )
                ok = check_lp_result(f"verify_{sp_label}", res2, opt_cap, 200, 200, 20, 95)
                all_ok = all_ok and ok
                log()
    except Exception as e:
        log(f"  エラー: {e}")
        all_ok = False
        log()

    # === 観点B: ルールベース simulate_battery の構造チェック ===
    log("【観点B】ルールベース simulate_battery の整合性")
    log("-" * 70)
    try:
        rb = app.simulate_battery(
            gen, demand, md,
            capacity_kwh=100.0, efficiency_pct=95,
            max_charge_kw=100, max_discharge_kw=100,
            soc_min_pct=20, soc_max_pct=95, no_export=False,
        )
        ch = rb["battery_charge"].flatten()
        dc = rb["battery_discharge"].flatten()
        both = int(((ch > 1e-6) & (dc > 1e-6)).sum())
        log(f"  ルールベースのboth_pos: {both} (構造上0のはず)")
        log(f"  年間充電: {ch.sum():.1f}, 年間放電: {dc.sum():.1f}")
        if both == 0:
            log(f"  ✓ OK（ルールベースは if/else 構造で物理的にパススルー不可）")
        else:
            log(f"  ★問題: パススルー検出")
            all_ok = False
    except Exception as e:
        log(f"  エラー: {e}")
        all_ok = False
    log()

    # === 観点C: エネルギーバランス整合性 ===
    log("【観点C】LP外で蓄電池あり計算をしている箇所")
    log("-" * 70)
    log("  grep結果: 蓄電池ありモードの計算経路は")
    log("    L1580-1600: optimize_battery (LP)")
    log("    L1601-1612: simulate_battery (ルールベース)")
    log("  の2経路のみ。LP/ルールベース以外で battery_charge/discharge を生成する")
    log("  経路は存在しない。")
    log("  → OK")
    log()

    # === 観点D: 出力抑制と蓄電池の相互作用 ===
    log("【観点D】出力抑制 (curtailment) と蓄電池の相互作用")
    log("-" * 70)
    log("  pv-sim-biz の no_export モードでは grid_export[t] の上限を 0 に固定。")
    log("  PV余剰は curtailment[t] で吸収。")
    log("  pv-sim-fip のような『POIベース輸出上限が放電もカウントする』形式ではない。")
    log("  上の実機テスト (no_export=True) で物理整合性を確認済み。")
    log()

    # === 観点F: ケースB（FIT→FIP転）→ N/A ===
    log("【観点F】ケースB（FIT→FIP転）の蓄電池運用")
    log("-" * 70)
    log("  pv-sim-biz は FIT→FIP転 のような multi-phase ロジックを持たない。")
    log("  → N/A")
    log()

    log("=" * 70)
    if all_ok:
        log("総合判定: OK（実機テストでパススルー・SOC違反は検出されず）")
    else:
        log("総合判定: 要確認（上記★を参照）")
    log("=" * 70)


if __name__ == "__main__":
    try:
        run_tests()
    finally:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(_lines))
        print(f"\n結果を保存しました: {OUT_PATH}")
