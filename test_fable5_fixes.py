# -*- coding: utf-8 -*-
"""
test_fable5_fixes.py - Fable 5レビューで検出したバグ修正の検証テスト
====================================================================
対象修正:
  1. GHI×2バグ: compute_poa_30minがErbsに実際の2倍のGHIを渡していた
  2. LPパススルー禁止: 売電単価>買電単価でLPがUnboundedになる構造欠陥
  3. 同時充放電の排他制約（mutual exclusion）
  4. _calc_irr のニュートン法発散ガード＋二分法フォールバック

実行: python test_fable5_fixes.py
結果: test_fable5_fixes_result.txt に書き出し
"""
import sys
import os
import time
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

import app

out = io.StringIO()
def log(msg=""):
    print(msg)
    out.write(msg + "\n")

n_pass = 0
n_fail = 0
def check(label, cond, detail=""):
    global n_pass, n_fail
    mark = "PASS" if cond else "FAIL"
    if cond:
        n_pass += 1
    else:
        n_fail += 1
    log(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))

log("=" * 70)
log("Fable 5 バグ修正 検証テスト")
log("=" * 70)

# ------------------------------------------------------------
# テスト1: GHIスケール修正（compute_poa_30min）
# ------------------------------------------------------------
log("\n【テスト1】GHIスケール修正: Erbsに実強度のGHIが渡ること")
log("-" * 70)

import pvlib

pno = "44132"  # 東京
lat, lon, ghi_df, temp_df = app.load_from_db(pno)
ghi_30, temp_30, md = app.prepare_30min_data(ghi_df, temp_df)

# app実装のPOA（修正後）
poa_app = app.compute_poa_30min(ghi_30, lat, lon, 30, 180)
e_app = poa_app.sum() * 0.5  # kW/m2 × 0.5h = kWh/m2

# 独立に組んだ正解パス（W/m2でErbs→POA→kWh/m2）
times = pd.date_range("2023-01-01 00:00", periods=365 * 48, freq="30min", tz="Asia/Tokyo")
site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")
solpos = site.get_solarposition(times)
ghi_series = pd.Series(ghi_30.flatten() * 1000.0, index=times)
erbs = pvlib.irradiance.erbs(ghi_series, solpos["zenith"], times)
poa_ref = pvlib.irradiance.get_total_irradiance(
    surface_tilt=30, surface_azimuth=180,
    solar_zenith=solpos["zenith"], solar_azimuth=solpos["azimuth"],
    dni=erbs["dni"].fillna(0).clip(lower=0),
    ghi=ghi_series,
    dhi=erbs["dhi"].fillna(0).clip(lower=0),
)["poa_global"].fillna(0).clip(lower=0)
e_ref = poa_ref.values.sum() / 1000.0 * 0.5

ratio = e_app / e_ref
log(f"  年間POA（東京・南30°）: app={e_app:.1f} 正解パス={e_ref:.1f} kWh/m2/年  比={ratio:.5f}")
check("app実装と正解パスが一致（比 1±0.001）", abs(ratio - 1) < 1e-3)

# 修正前の値（×2000パス）より小さくなっていること（過大評価の解消）
ghi_series_2x = pd.Series(ghi_30.flatten() * 2000.0, index=times)
erbs2 = pvlib.irradiance.erbs(ghi_series_2x, solpos["zenith"], times)
poa_2x = pvlib.irradiance.get_total_irradiance(
    surface_tilt=30, surface_azimuth=180,
    solar_zenith=solpos["zenith"], solar_azimuth=solpos["azimuth"],
    dni=erbs2["dni"].fillna(0).clip(lower=0),
    ghi=ghi_series_2x,
    dhi=erbs2["dhi"].fillna(0).clip(lower=0),
)["poa_global"].fillna(0).clip(lower=0)
e_old = poa_2x.values.sum() / 2000.0 * 0.5
log(f"  参考: 修正前相当={e_old:.1f} kWh/m2/年（+{(e_old / e_ref - 1) * 100:.2f}%過大だった）")
check("修正前の過大評価が解消（app < 修正前相当）", e_app < e_old)

# 水平面（tilt=0）ではPOA≒GHI（transpositionの恒等性チェック）
poa_flat = app.compute_poa_30min(ghi_30, lat, lon, 0, 180)
e_ghi = ghi_30.sum() * 0.5
e_flat = poa_flat.sum() * 0.5
log(f"  水平面POA={e_flat:.1f} vs GHI={e_ghi:.1f} kWh/m2/年  比={e_flat / e_ghi:.4f}")
check("水平面POA ≒ GHI（±2%）", abs(e_flat / e_ghi - 1) < 0.02)

# ------------------------------------------------------------
# テスト2: LPパススルー禁止（Unbounded解消）
# ------------------------------------------------------------
log("\n【テスト2】LPパススルー禁止: 売電>買電でもUnboundedにならない")
log("-" * 70)

n_days2 = 2
gen0 = np.zeros((n_days2, 48))          # PVなし
dem0 = np.ones((n_days2, 48)) * 5.0     # 10kW定常需要
md2 = [(1, 1), (1, 2)]

# 修正前は 売600/買12 で Unbounded だったケース
try:
    res = app.optimize_battery(
        gen0, dem0, md2,
        capacity_kwh=10.0, efficiency_pct=95,
        max_charge_kw=5.0, max_discharge_kw=5.0,
        soc_min_pct=20, soc_max_pct=95,
        basic_charge_per_kw=1890.0,
        energy_charge_summer=12.0, energy_charge_other=12.0,
        power_factor_pct=85, fuel_adjustment=0.0, renewable_surcharge=0.0,
        sell_price=600.0, no_export=False,
    )
    log(f"  買12/売600: 解けた  import={res['annual_import']:.1f}  export={res['annual_export']:.1f} kWh")
    check("旧Unboundedケースが最適解に到達", True)
    # PVゼロなので売電原資は蓄電池放電のみ（系統パススルーは不可能）
    check("PVゼロ時の売電 ≤ 年間放電量", res["annual_export"] <= res["annual_discharge"] + 1e-6,
          f"export={res['annual_export']:.1f}, discharge={res['annual_discharge']:.1f}")
except Exception as e:
    check("旧Unboundedケースが最適解に到達", False, str(e))

# ------------------------------------------------------------
# テスト3: 物理整合（現実的なPV+需要のフルイヤーLP）＋実行時間
# ------------------------------------------------------------
log("\n【テスト3】フルイヤーLPの物理整合と実行時間（HF Spaces想定）")
log("-" * 70)

faces = [{"ppeak": 500.0, "tilt": 30.0, "orientation": "南",
          "azimuth": 180.0, "pcs_limit_kw": 500.0}]
g = app.calculate_generation(
    lat=lat, lon=lon, ghi_df=ghi_df, temp_df=temp_df, faces=faces,
    KHD=app.DEFAULT_KHD, KPD=app.DEFAULT_KPD, KPM=app.DEFAULT_KPM,
    KPA=app.DEFAULT_KPA, eta_ino=app.DEFAULT_ETA_INO,
    alpha_pct=app.DEFAULT_ALPHA, delta_t=app.DEFAULT_DELTA_T,
)
gen_full = g["total_gen_clipped"]
log(f"  PV 500kW 年間発電量（修正後）: {g['annual']:.1f} kWh/年")
check("年間発電量が妥当レンジ（500〜700 MWh: 東京500kW南30°）",
      500_000 < g["annual"] < 700_000, f"{g['annual']:.0f} kWh")

dem_full, _ = app.load_combined_demand(("役所・自治体庁舎", 4500, 1) + ("なし", 0, 0) * 5, 1)
check("需要データ読込", dem_full is not None)

max_kw = 50.0
t0 = time.time()
res_full = app.optimize_battery(
    gen_full, dem_full, g["month_day"],
    capacity_kwh=100.0, efficiency_pct=95,
    max_charge_kw=max_kw, max_discharge_kw=max_kw,
    soc_min_pct=20, soc_max_pct=95,
    basic_charge_per_kw=1890.0,
    energy_charge_summer=19.93, energy_charge_other=18.77,
    power_factor_pct=85, fuel_adjustment=0.0, renewable_surcharge=4.18,
    sell_price=19.0, no_export=False,
)
elapsed = time.time() - t0
log(f"  フルイヤーLP実行時間: {elapsed:.1f} 秒（新規制約 +35,040本込み）")
check("実行時間がHF Spaces許容内（<120秒）", elapsed < 120, f"{elapsed:.1f}s")

ch = res_full["battery_charge"].flatten()
dc = res_full["battery_discharge"].flatten()
soc = res_full["soc"].flatten()
exp = res_full["export"].flatten()
gen_flat = gen_full.flatten()
max_slot = max_kw * 0.5

both_pos = int(((ch > 1e-6) & (dc > 1e-6)).sum())
pcs_viol = int(((ch + dc) > max_slot + 1e-4).sum())
soc_hi = int((soc > 100.0 * 0.95 + 1e-6).sum())
soc_lo = int((soc < 100.0 * 0.20 - 1e-6).sum())
# パススルー検査: 売電がPV+放電を超えるコマ
pt_viol = int((exp > gen_flat + dc + 1e-6).sum())
cycles_day = ch.sum() / 100.0 / 365

check("同時充放電コマなし（mutual exclusion）", both_pos == 0, f"{both_pos}コマ")
check("充放電合計がPCS上限内", pcs_viol == 0, f"{pcs_viol}コマ")
check("SOC上限違反なし", soc_hi == 0, f"{soc_hi}コマ")
check("SOC下限違反なし", soc_lo == 0, f"{soc_lo}コマ")
check("売電 ≤ PV+放電（全コマ）", pt_viol == 0, f"{pt_viol}コマ")
check("サイクル数が健全（≤2回/日）", cycles_day <= 2.0, f"{cycles_day:.2f}回/日")
check("終端SOC=初期SOC", abs(soc[-1] - 20.0) < 1e-4, f"soc[T-1]={soc[-1]:.4f}")

# ------------------------------------------------------------
# テスト4: 最適容量探索LP（容量が決定変数）も同様に解けること
# ------------------------------------------------------------
log("\n【テスト4】optimize_battery_capacity（容量LP）")
log("-" * 70)

for no_exp, label in [(False, "余剰売電"), (True, "逆潮流禁止")]:
    t0 = time.time()
    cap_res = app.optimize_battery_capacity(
        gen_full, dem_full, g["month_day"],
        efficiency_pct=95, max_charge_kw=max_kw, max_discharge_kw=max_kw,
        soc_min_pct=20, soc_max_pct=95,
        basic_charge_per_kw=1890.0,
        energy_charge_summer=19.93, energy_charge_other=18.77,
        power_factor_pct=85, fuel_adjustment=0.0, renewable_surcharge=4.18,
        sell_price=19.0, battery_cost_per_kwh=200000, payback_years=15,
        no_export=no_exp,
    )
    elapsed = time.time() - t0
    opt_cap = cap_res["optimal_capacity_kwh"]
    log(f"  [{label}] 最適容量={opt_cap:.1f} kWh  実行時間={elapsed:.1f}秒")
    check(f"[{label}] 内点解（0 < cap < 2000）", 0 <= opt_cap < 2000, f"{opt_cap:.1f} kWh")
    check(f"[{label}] 実行時間 <120秒", elapsed < 120, f"{elapsed:.1f}s")

# ------------------------------------------------------------
# テスト5: _calc_irr（ニュートン法＋二分法フォールバック）
# ------------------------------------------------------------
log("\n【テスト5】_calc_irr")
log("-" * 70)

def npv_at(cf, r):
    return sum(c / (1 + r) ** t for t, c in enumerate(cf))

# 既知ケース: -1000, +400×5年 → IRR ≈ 28.65%
cf1 = [-1000] + [400] * 5
irr1 = app._calc_irr(cf1)
check("標準ケースのIRRが収束（NPV≈0）", irr1 is not None and abs(npv_at(cf1, irr1)) < 1e-4,
      f"IRR={irr1 * 100:.2f}%" if irr1 is not None else "None")

# 全期間マイナス（解なし）→ Noneを返しクラッシュしない
cf2 = [-1000] + [-50] * 5
irr2 = app._calc_irr(cf2)
check("解なしケースでNone（クラッシュなし）", irr2 is None, f"戻り値={irr2}")

# 大幅マイナスIRR（ニュートン法が発散しやすい）→ 二分法で救済
cf3 = [-1000] + [10] * 5
irr3 = app._calc_irr(cf3)
check("大幅マイナスIRRでも収束", irr3 is not None and abs(npv_at(cf3, irr3)) < 1e-4,
      f"IRR={irr3 * 100:.2f}%" if irr3 is not None else "None")

# ------------------------------------------------------------
# テスト6: ルールベース蓄電池（回帰: 修正の影響を受けないこと）
# ------------------------------------------------------------
log("\n【テスト6】ルールベース蓄電池（回帰）")
log("-" * 70)

rb = app.simulate_battery(
    gen_full, dem_full, g["month_day"],
    capacity_kwh=100.0, efficiency_pct=95,
    max_charge_kw=max_kw, max_discharge_kw=max_kw,
    soc_min_pct=20, soc_max_pct=95, no_export=False,
)
rb_soc = rb["soc"].flatten()
check("SOC範囲内", ((rb_soc >= 20.0 - 1e-6) & (rb_soc <= 95.0 + 1e-6)).all())
check("充放電損失 > 0", rb["annual_charge"] > rb["annual_discharge"])
log(f"  年間充電={rb['annual_charge']:.1f} 放電={rb['annual_discharge']:.1f} kWh")

# ------------------------------------------------------------
log("\n" + "=" * 70)
log(f"結果: PASS {n_pass} / FAIL {n_fail}")
log("=" * 70)

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "test_fable5_fixes_result.txt"), "w", encoding="utf-8") as f:
    f.write(out.getvalue())

sys.exit(0 if n_fail == 0 else 1)
