# -*- coding: utf-8 -*-
"""
test_grid_cap.py - 系統受電上限（Phase 7 段階5）の検証
=====================================================
対象:
  1. 上限の選択肢・プリセット（受電電圧の階層境界の1kW下）と入力検証
  2. diagnose_grid_cap: 解析的に答えが分かる合成データでの数値検証
  3. 診断の健全性: 乱数の小さな問題で「診断が守れないと言う ⇔ LPが実行不可能」を突き合わせ
  4. optimize_battery: 上限あり/なし、周期SOC、上限なしがビット同一であること
  5. run_simulation（DCモード）: 守れる/守れない/強制しない の各状態と文言
  6. 産業用では上限が適用されないこと、UI定義

実行: python test_grid_cap.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import app

n_pass = 0
n_fail = 0


def check(label, cond, detail=""):
    global n_pass, n_fail
    if cond:
        n_pass += 1
    else:
        n_fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


RATES = dict(basic_charge_per_kw=1890.0, energy_charge_summer=19.93, energy_charge_other=18.77,
             power_factor_pct=85, fuel_adjustment=0.0, renewable_surcharge=4.18)


def lp(gen, dem, cap_kw, capacity_kwh, ch_kw, dc_kw, eff=95.0, smin=10.0, smax=90.0):
    """小さな (n_days,48) 配列でLPを解く。守れなければ GridCapInfeasibleError。"""
    n = gen.shape[0]
    md = [(1, d + 1) for d in range(n)]
    return app.optimize_battery(
        gen, dem, md, capacity_kwh=capacity_kwh, efficiency_pct=eff,
        max_charge_kw=ch_kw, max_discharge_kw=dc_kw, soc_min_pct=smin, soc_max_pct=smax,
        sell_price=8.5, grid_import_cap_kw=cap_kw, **RATES)


def diag(gen, dem, cap_kw, capacity_kwh, ch_kw, dc_kw, eff=95.0, smin=10.0, smax=90.0):
    return app.diagnose_grid_cap(gen, dem, cap_kw, capacity_kwh, eff, ch_kw, dc_kw, smin, smax)


print("=" * 70)
print("test_grid_cap.py — 系統受電上限")
print("=" * 70)

# ------------------------------------------------------------
print("\n【1. 上限の選択肢・プリセット・入力検証】")
th = app.VOLTAGE_CLASS_THRESHOLDS
check("プリセット「高圧に収める」= 2,000kWの1kW下 = 1,999kW", app.GRID_CAP_PRESETS_KW[app.GRID_CAP_HV] == 1999.0)
check("プリセット「22・33kVに収める」= 10,000kWの1kW下 = 9,999kW", app.GRID_CAP_PRESETS_KW[app.GRID_CAP_EHV33] == 9999.0)
check("プリセットは resolve_voltage_class の境界から導出（定義が食い違わない）",
      app.GRID_CAP_PRESETS_KW[app.GRID_CAP_HV] == th[0][1] - 1.0 and app.GRID_CAP_PRESETS_KW[app.GRID_CAP_EHV33] == th[1][1] - 1.0)
check("1,999kWは高圧(hv_6000)のまま", app.resolve_voltage_class(1999.0) == "hv_6000")
check("9,999kWは22・33kV(ehv_30000)のまま", app.resolve_voltage_class(9999.0) == "ehv_30000")
check("（参考）2,000kWちょうどは特別高圧に分類される＝境界そのものを上限にしない理由",
      app.resolve_voltage_class(2000.0) == "ehv_30000")
check("選択肢は4つ（制限なし/高圧/22・33kV/手入力）", len(app.GRID_CAP_MODES) == 4)
check("制限なし → None", app.resolve_grid_cap("制限なし")[0] is None)
check("None・空文字 → 制限なし", app.resolve_grid_cap(None)[0] is None and app.resolve_grid_cap("")[0] is None)
check("高圧プリセット → 1,999kW", app.resolve_grid_cap(app.GRID_CAP_HV)[0] == 1999.0)
check("手入力 2500 → 2,500kW", app.resolve_grid_cap(app.GRID_CAP_MANUAL, 2500)[0] == 2500.0)
for bad in (0, -5, None, float("nan"), float("inf"), "abc"):
    try:
        app.resolve_grid_cap(app.GRID_CAP_MANUAL, bad)
        check(f"手入力 {bad!r} → ValueError", False, "例外なし")
    except ValueError:
        check(f"手入力 {bad!r} → ValueError", True)
try:
    app.resolve_grid_cap("存在しない選択肢")
    check("不明な選択肢 → ValueError", False)
except ValueError:
    check("不明な選択肢 → ValueError", True)

# ------------------------------------------------------------
print("\n【2. diagnose_grid_cap: 解析的に答えが分かる合成データ】")
# 1日=昼12h(コマ0-23)は150kW、夜12h(コマ24-47)は50kWの需要、PVなし
day = np.concatenate([np.full(24, 150.0 * 0.5), np.full(24, 50.0 * 0.5)])
dem = np.tile(day, (3, 1))
gen0 = np.zeros_like(dem)

d = diag(gen0, dem, 100.0, 1000.0, 200.0, 200.0, eff=100.0, smin=0.0, smax=100.0)
check("η=100%・上限100kW: 超過600kWh/日 = 余力600kWh/日 → エネルギー収支は成立（ぎりぎり）", d["energy_feasible"])
check("必要放電出力 = 50kW", abs(d["required_power_kw"] - 50.0) < 1e-9, f"{d['required_power_kw']}")
check("必要使用可能容量 = 600kWh（12h×50kW）", abs(d["required_usable_kwh"] - 600.0) < 1e-6, f"{d['required_usable_kwh']}")
check("蓄電池が十分なら違反なし", d["violations"] == [], str(d["violations"]))

d = diag(gen0, dem, 100.0, 1000.0, 200.0, 200.0, eff=95.0, smin=0.0, smax=100.0)
check("η=95%・上限100kW: 超過600 > η²×余力(541.5) → energy違反", "energy" in d["violations"])
check("energy違反のとき必要量は出さない（無限大でも解決しない）", d["min_cap_kw_energy"] is not None)
check("蓄電池無限大でも必要な上限の下限 > 100kW（約103kW）", 100.0 < d["min_cap_kw_energy"] < 110.0, f"{d['min_cap_kw_energy']:.2f}")

d = diag(gen0, dem, 105.0, 1000.0, 200.0, 200.0, eff=95.0, smin=0.0, smax=100.0)
check("上限105kW: 超過540kWh ≤ η²×余力(595.65) → energy成立", d["energy_feasible"])
check("必要使用可能容量 = 540/0.95 = 568.42kWh", abs(d["required_usable_kwh"] - 540.0 / 0.95) < 1e-6, f"{d['required_usable_kwh']:.3f}")
check("必要放電出力 = 45kW", abs(d["required_power_kw"] - 45.0) < 1e-9)

d = diag(gen0, dem, 105.0, 100.0, 200.0, 30.0, eff=95.0, smin=0.0, smax=100.0)
check("放電出力30kW < 必要45kW → power違反", "power" in d["violations"])
check("容量100kWh < 必要568kWh → capacity違反", "capacity" in d["violations"])

d = diag(gen0, dem, 155.0, 0.0, 0.0, 0.0)
check("上限が最大負荷以上 → 蓄電池なしで守れる（needs_battery=False, 違反なし）",
      (not d["needs_battery"]) and d["violations"] == [])

d = diag(np.full_like(dem, 100.0 * 0.5), dem, 60.0, 1000.0, 200.0, 200.0, eff=100.0, smin=0.0, smax=100.0)
check("PV 100kW を差し引くと超過が消える（net最大50kW<上限60kW）", not d["needs_battery"], f"peak_net={d['peak_net_kw']}")

# ------------------------------------------------------------
print("\n【3. 診断の健全性: 乱数の小問題で 診断⇔LP を突き合わせ】")
print("  （充電電力を十分大きくしたとき、診断の3条件は必要十分になるはず。不一致は診断ロジックのバグ）")
rng = np.random.default_rng(7)
n_case = 60
mismatch = []
n_inf = n_feas = 0
for k in range(n_case):
    nd = 2
    base = rng.uniform(40, 160)
    wave = rng.uniform(0.0, 0.6) * base * np.sin(np.linspace(0, 2 * np.pi, 48, endpoint=False) + rng.uniform(0, 6.28))
    noise = rng.uniform(-0.1, 0.1, size=(nd, 48)) * base
    dem_k = np.maximum((base + wave)[None, :] + noise, 1.0) * 0.5   # [kWh/30分]
    pv_k = (np.maximum(np.sin(np.linspace(-0.5, 2.6, 48)), 0) * rng.uniform(0, 80))[None, :] * np.ones((nd, 1)) * 0.5
    net_kw = (dem_k - pv_k) / 0.5
    cap_k = float(rng.uniform(np.percentile(net_kw, 20), net_kw.max() * 1.02))
    capacity = float(rng.choice([2.0, 10.0, 40.0, 150.0, 600.0]))
    dis_kw = float(rng.choice([2.0, 10.0, 40.0, 150.0]))
    eff = float(rng.choice([90.0, 95.0, 100.0]))
    dg = diag(pv_k, dem_k, cap_k, capacity, 1e4, dis_kw, eff=eff)
    try:
        lp(pv_k, dem_k, cap_k, capacity, 1e4, dis_kw, eff=eff)
        lp_feasible = True
    except app.GridCapInfeasibleError:
        lp_feasible = False
    diag_feasible = (dg["violations"] == [])
    n_feas += lp_feasible
    n_inf += (not lp_feasible)
    if lp_feasible != diag_feasible:
        mismatch.append((k, cap_k, capacity, dis_kw, eff, dg["violations"], lp_feasible))
check(f"{n_case}ケース: 診断と LP の実行可否が完全一致", not mismatch, f"不一致 {len(mismatch)} 件 {mismatch[:2]}")
check("実行可能・不可能の両方のケースを含む（検証として意味がある）", n_inf >= 10 and n_feas >= 10, f"不可能{n_inf}件 / 可能{n_feas}件")

# 充電レート不足: 診断（理想=充電無制限）は通るがLPは実行不可能 → infeasible_lp の経路
day2 = np.concatenate([np.full(8, 150.0 * 0.5), np.full(40, 30.0 * 0.5)])  # 4h超過 / 20h余力
dem2 = np.tile(day2, (3, 1))
g2 = np.zeros_like(dem2)
dg2 = diag(g2, dem2, 100.0, 400.0, 5.0, 60.0, eff=95.0)
check("充電レート不足の例: 診断の必要条件は満たす（違反なし）", dg2["violations"] == [], str(dg2["violations"]))
try:
    lp(g2, dem2, 100.0, 400.0, 5.0, 60.0)
    check("同条件でLPは実行不可能（充電5kWでは回復できない）", False, "解けてしまった")
except app.GridCapInfeasibleError as e:
    check("同条件でLPは実行不可能 → GridCapInfeasibleError", True, str(e))
try:
    lp(g2, dem2, 100.0, 400.0, 100.0, 60.0)
    check("充電電力を100kWにすれば解ける", True)
except app.GridCapInfeasibleError:
    check("充電電力を100kWにすれば解ける", False)

# ------------------------------------------------------------
print("\n【4. optimize_battery: 上限あり/なし】")
dem4 = np.tile(np.concatenate([np.full(24, 120.0 * 0.5), np.full(24, 60.0 * 0.5)]), (3, 1))
g4 = np.zeros_like(dem4)
r_none = lp(g4, dem4, None, 400.0, 100.0, 100.0)
r_zero = lp(g4, dem4, 0, 400.0, 100.0, 100.0)
check("grid_import_cap_kw=None と 0 は同じ結果（上限なし）",
      np.array_equal(r_none["import_"], r_zero["import_"]) and "grid_import_cap_kw" not in r_none)
# 上限100kW: 超過240kWh/日 → 必要な使用可能容量 240/0.95≒253kWh。容量400kWh（使用可能320kWh）なら足りる
r_cap = lp(g4, dem4, 100.0, 400.0, 100.0, 100.0)
check("上限あり: 全コマの系統購入電力 ≤ 100kW", float(r_cap["import_"].max()) / 0.5 <= 100.0 + 1e-6,
      f"{float(r_cap['import_'].max()) / 0.5:.4f}")
check("上限ありの結果に grid_import_cap_kw が入る", r_cap.get("grid_import_cap_kw") == 100.0)
check("上限なし（従来）の戻り値に新キーは増えない", set(r_none.keys()) == set(r_zero.keys()))
soc = r_cap["soc"].ravel()
step0 = r_cap["battery_charge"].ravel()[0] * 0.95 - r_cap["battery_discharge"].ravel()[0] / 0.95
soc_init = soc[0] - step0
check("周期SOC: 初期SOC = 年末SOC（LPソルバーの許容内）", abs(soc_init - soc[-1]) < 1e-3, f"init={soc_init:.6f} end={soc[-1]:.6f} 差={abs(soc_init - soc[-1]):.2e}")
check("SOCは範囲内（10〜90%×400kWh）", soc.min() >= 40.0 - 1e-4 and soc.max() <= 360.0 + 1e-4)
check("上限なしでは従来どおり 終端SOC = SOC下限（40kWh）", abs(r_none["soc"].ravel()[-1] - 40.0) < 1e-6,
      f"{r_none['soc'].ravel()[-1]:.4f}")

# ------------------------------------------------------------
print("\n【5. run_simulation（DCモード）】")
FAC = tuple(["役所・自治体庁舎", 4500, 1] + ["なし", 0, 0] * (app.MAX_FACILITIES - 1))
FACE = tuple([1000.0, "南", 180.0, 30, 0] + [0, "南", "", 30, 0] * (app.MAX_FACES - 1))
BASE = dict(
    station_choice="14163 (SAPPORO)", csv_file=None, demand_custom_csv=None,
    KHD=0.97, KPD=0.95, KPM=0.94, KPA=0.97, eta_ino=0.90, alpha_pct=-0.35, delta_t=21.5,
    bat_enabled=False, bat_mode="ルールベース", bat_capacity=5.0, bat_efficiency=95,
    bat_max_charge=2.5, bat_max_discharge=2.5, bat_soc_min=10, bat_soc_max=90,
    elec_basic=1890.0, elec_summer=19.93, elec_other=18.77, elec_pf=85, elec_fuel=0.0, elec_renewable=4.18,
    contract_type="高圧", sell_mode="余剰売電", sell_price=19.0,
    pv_cost_per_kw=158000, bat_cost_per_kwh=200000, substation_cost_per_kva=27500,
    subsidy_enabled=False, subsidy_pv_pct=0, subsidy_bat_pct=0, co2_factor=0.000431,
    business_model="自己所有", contract_years=15, target_irr=10.0,
    mg_enabled=False, mg_line_distance=2.0, mg_line_cost_per_km=30000000, mg_opex_ratio=2.0, mg_irr_period=20,
    bifacial_enabled=False, bifaciality_val=0.75, gcr_val=0.4, height_val=2.0, pitch_val=5.0,
    snow_albedo_enabled=True, facility_args=FAC, face_args=FACE,
    num_facilities=1, num_faces=1, display_month=7, display_day=1,
)
DC = dict(workload_preset="手動設定", capacity_mode="IT容量を直接入力", it_capacity_kw=1000.0,
          it_load_factor_pct=80.0, pue_const=1.40, noise_level="なし", profile_mode=app.PROFILE_CEC,
          grid_cap_mode="手入力", grid_cap_kw=1190.0)


def run(dc, **over):
    kw = dict(BASE)
    kw.update(over)
    return app.run_simulation(demand_source="datacenter", dc_args=dc, **kw)


LP_BAT = dict(bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=2000.0,
              bat_max_charge=1000.0, bat_max_discharge=1000.0)

r = run(DC, **LP_BAT)
t = r[4]
check("LP+上限: 図が生成される（解けた）", r[0] is not None)
check("結果に「受電上限」の節", "══ 受電上限 ══" in t)
check("導入後ピークが上限内と表示", "（上限内）" in t.split("導入後ピーク:")[1].split("\n")[0])
peak_after = float(np.max(r[6]["sc_result"]["import_"])) * 2.0
check("実際の導入後ピーク ≤ 上限1,190kW", peak_after <= 1190.0 + 1e-6, f"{peak_after:.2f}")
check("LPで強制した旨と周期SOCの説明", "上限を制約として強制" in t and "周期条件" in t)
sc = r[6]["sc_result"]
soc = sc["soc"].ravel()
st0 = sc["battery_charge"].ravel()[0] * 0.95 - sc["battery_discharge"].ravel()[0] / 0.95
check("周期SOC（実データ）: 初期SOC = 年末SOC（LPソルバーの許容内）", abs((soc[0] - st0) - soc[-1]) < 1e-2, f"差={abs((soc[0] - st0) - soc[-1]):.2e}")

r = run(dict(DC, profile_mode=app.PROFILE_FLAT, grid_cap_kw=1000.0), **LP_BAT)
t = r[4]
check("基底負荷が上限超: LPを実行せず診断を返す（図なし・7要素）", r[0] is None and len(r) == 7 and r[6] is None)
check("「受電上限を守れません」と「蓄電池を大きくしても解決しません」", "受電上限を守れません" in t and "蓄電池を大きくしても解決しません" in t)
check("上限の下限の目安（約1,012kW）が出る", "約 1,012 kW 以上" in t or "約 1,013 kW 以上" in t, t[t.find("→ 上限を約"):][:30])
check("実装していない需要シフトを案内しない", "シフト" not in t and "今後実装予定" not in t)
check("診断メッセージにもDC需要の節が先頭に付く", t.startswith("══ データセンター需要 ══"))

r = run(DC, bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=1.0,
        bat_max_charge=0.5, bat_max_discharge=0.5)
t = r[4]
check("蓄電池が小さすぎる: 出力・容量不足を診断（図なし）", r[0] is None and "放電出力が不足" in t and "容量が不足" in t)
check("必要な蓄電池の下限の目安が出る", "必要な蓄電池の下限の目安: 放電出力 ≥" in t)
check("目安は下限であるとの注意書き", "下限の目安です" in t)

r = run(DC)
t = r[4]
check("蓄電池なし: 計算は実行される（図あり）", r[0] is not None)
check("導入後ピークが上限を超過と判定", "上限を 4.0 kW 超過" in t)
check("「蓄電池が「なし」のため、上限は強制していません」", "蓄電池が「なし」のため、上限は強制していません" in t)
check("必要な蓄電池の目安を案内", "上限を守るために必要な蓄電池の下限の目安" in t)

r = run(DC, bat_enabled=True, bat_mode="ルールベース", bat_capacity=2000.0,
        bat_max_charge=1000.0, bat_max_discharge=1000.0)
check("ルールベース: 上限は強制しない（「ルールベース」と明示）", "蓄電池が「ルールベース」のため、上限は強制していません" in r[4])
check("ルールベース: ユーザーの蓄電池モードは変えられない（最適化を実行していない）",
      not r[6]["sc_result"].get("optimized"))

r = run(dict(DC, grid_cap_mode="制限なし"), **LP_BAT)
check("上限なし: 「受電上限」の節は出ない", "══ 受電上限 ══" not in r[4])
check("上限なし: LPは従来どおり（初期SOC=SOC下限の終端）", abs(r[6]["sc_result"]["soc"].ravel()[-1] - 200.0) < 1e-4,
      f"{r[6]['sc_result']['soc'].ravel()[-1]:.3f}")

r = run(dict(DC, grid_cap_mode=app.GRID_CAP_MANUAL, grid_cap_kw=0))
check("手入力の上限が0以下 → 日本語エラー（例外で落ちない）",
      r[0] is None and r[4].startswith("エラー: 受電上限（手入力）は0より大きい数値"), r[4][:40])

r = run(dict(DC, grid_cap_mode=app.GRID_CAP_MANUAL, grid_cap_kw=5000.0))
check("上限5,000kW（負荷より高い）: 蓄電池なしで守れる旨", "蓄電池なしでも上限を守れます" in r[4] or "常に上限内" in r[4])

# infeasible_lp の文言（実シナリオでは再現しにくいので、組み立て関数を直接確認）
dcinfo = dict(peak_demand_kw=1207.4, grid_cap_kw=1190.0, grid_cap_label="手入力")
dg_ok = dict(cap_kw=1190.0, peak_net_kw=1207.0, exceed_slots=10, total_slots=17520, exceed_energy_kwh=50.0,
             spare_energy_kwh=9e6, needs_battery=True, energy_feasible=True, min_cap_kw_energy=None,
             required_power_kw=4.0, required_usable_kwh=7.0, required_capacity_kwh=9.0, usable_kwh=100.0,
             max_discharge_kw=50.0, violations=[])
txt = app.format_grid_cap(dcinfo, dg_ok, "infeasible_lp",
                          battery=dict(enabled=True, mode_label="最適充放電（LP）", capacity_kwh=100.0,
                                       max_charge_kw=0.1, max_discharge_kw=50.0))
check("infeasible_lp: 充電レート不足を主因として案内", "充電レート不足" in txt and "最大充電電力" in txt)
check("infeasible_lp: 下限の目安も併記", "必要な蓄電池の下限の目安" in txt)

# ------------------------------------------------------------
print("\n【6. 産業用では上限を適用しない／UI定義】")
kw = dict(BASE)
kw.update(LP_BAT)
kw.update(bat_capacity=100.0, bat_max_charge=50.0, bat_max_discharge=50.0)
r_ind = app.run_simulation(**kw)
r_ind_cap = app.run_simulation(demand_source="industrial", dc_args=DC, **kw)
check("産業用 + dc_args(上限あり) → 結果は上限なしと完全一致（上限は無視）", r_ind[4] == r_ind_cap[4])
check("産業用の結果に「受電上限」の節は出ない", "受電上限" not in r_ind_cap[4])

comps = [c for c in app.demo.blocks.values() if getattr(c, "label", None) == "系統受電上限"]
check("受電上限のドロップダウンが1つ定義されている", len(comps) == 1)
check("選択肢が GRID_CAP_MODES と一致し、既定は「制限なし」",
      bool(comps) and [ch[1] if isinstance(ch, (tuple, list)) else ch for ch in comps[0].choices] == app.GRID_CAP_MODES
      and comps[0].value == app.GRID_CAP_NONE)
check("DC_INPUT_KEYS の末尾が grid_cap_mode, grid_cap_kw", app.DC_INPUT_KEYS[-2:] == ("grid_cap_mode", "grid_cap_kw"))

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
