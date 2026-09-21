# -*- coding: utf-8 -*-
"""
test_wind_lp.py - 風力（オフサイト電源）を受電点の基準で解く蓄電池LP（W2d）の検証
==============================================================================
設計: docs/wind_design_spec.md §5-4（W2d）
対象:
  1. optimize_battery（offsite）: 独立実装（scipy.linprog）との目的関数の一致、物理的な不変条件
     （収支・受電量と売電量の同時に正にならないこと・売電は太陽光の余剰だけ・受電上限）
  2. optimize_battery_capacity（offsite）: 独立実装との一致
  3. 売電単価が風力の配達単価を上回る（FIT）ときに「風力を受電して太陽光を売る」裁定が生じないこと
  4. run_simulation: 受電上限（DC）・最適容量探索と風力の併用、蓄電池ルールベースを上回ること
  5. 風力OFFの LP が変わらないこと（offsite=None）

実行: python test_wind_lp.py（scipy が無ければ独立実装との照合は飛ばす）
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import app

try:
    from scipy.optimize import linprog
    from scipy.sparse import lil_matrix
    HAS_SCIPY = True
except ImportError:  # pragma: no cover
    HAS_SCIPY = False

n_pass = 0
n_fail = 0


def check(label, cond, detail=""):
    global n_pass, n_fail
    if cond:
        n_pass += 1
    else:
        n_fail += 1
    detail = str(detail).replace("\n", " ")
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


RATE = dict(basic_charge_per_kw=1890.0, energy_charge_summer=19.93, energy_charge_other=18.77,
            power_factor_pct=85, fuel_adjustment=0.0, renewable_surcharge=4.18)
BAT = dict(efficiency_pct=95.0, max_charge_kw=60.0, max_discharge_kw=60.0, soc_min_pct=20.0, soc_max_pct=95.0)

# ------------------------------------------------------------------
# 小さな合成データ（4日×48コマ。夏の日を含めて季節単価の分岐も通す）
# ------------------------------------------------------------------
N_DAYS = 4
MONTH_DAY = [(1, 1), (1, 2), (7, 1), (7, 2)]
rng = np.random.default_rng(20260921)
slot = np.arange(48)
sun = np.clip(np.sin((slot - 12) / 24.0 * np.pi), 0, None)          # 日中だけ正
PV = np.stack([sun * a for a in (90.0, 30.0, 110.0, 60.0)]) * 0.5    # kWh/30分
WIND = np.stack([np.clip(15.0 + 12.0 * np.sin(slot / 7.0 + k) + rng.normal(0, 3, 48), 0, None)
                 for k in range(N_DAYS)]) * 0.5
DEM = (40.0 + 25.0 * np.exp(-((slot - 34) / 6.0) ** 2) + rng.normal(0, 2, 48)).clip(5) * 0.5
DEM = np.stack([DEM * f for f in (1.0, 1.15, 0.9, 1.3)])


def source(w, wheeling=2.15, fee=3.0):
    return dict(gen_30min=w, wheeling_yen=wheeling, retail_fee_yen=fee, payment_yen=float(w.sum()) * 11.96)


SRC = source(WIND)
SPEC = app.offsite_lp_spec([SRC])
W_UNIT = 2.15 + 3.0 + RATE["renewable_surcharge"]


def unit_prices():
    m = np.array([MONTH_DAY[d][0] for d in range(N_DAYS) for _ in range(48)])
    return np.where((m >= 7) & (m <= 9), RATE["energy_charge_summer"], RATE["energy_charge_other"]) \
        + RATE["fuel_adjustment"] + RATE["renewable_surcharge"]


def reference_lp(pv, wind, dem, capacity, sell, no_export, cap_kw=None, cap_var=None, batt_cost=0.0, payback=1.0, rate=RATE):
    """独立実装（scipy.linprog / HiGHS）。受電点を「買い（N+）と売り（N−）」の正味の流れで表す別の定式化。

    optimize_battery は受電量Rと売電Eを別変数にして収支で結ぶが、こちらは需要+充電−放電−PV = 正味受電 として
    N+（正味受電）・E（売電）・C（出力抑制）に分け、風力の配達分 w ≤ N+ ・ w ≤ 風力発電量 とする。
    cap_var=True なら容量も変数（optimize_battery_capacity に対応）。
    cap_kw を渡したときは、optimize_battery と同じく初期SOC＝年末SOC（変数）の周期条件にする。
    返り値: 最適値（電気代−売電収入。cap_var のときは＋蓄電池の年額換算）
    """
    T = pv.size
    dt = 0.5
    eff = BAT["efficiency_pct"] / 100.0
    up = unit_prices()
    pvf, wf, df = pv.ravel(), wind.ravel(), dem.ravel()
    # 変数の並び: ch, dis, soc, N(=受電), w, E, C の各T本 + peak + (cap)
    idx = {k: i * T for i, k in enumerate(("ch", "dis", "soc", "N", "w", "E", "C"))}
    cyc = cap_kw is not None and not cap_var
    n_var = 7 * T + 1 + (1 if cap_var else 0) + (1 if cyc else 0)
    ipeak = 7 * T
    icap = 7 * T + 1
    icyc = 7 * T + 1
    c = np.zeros(n_var)
    pf = (185 - rate["power_factor_pct"]) / 100.0
    c[ipeak] = rate["basic_charge_per_kw"] * 12 * pf
    # 抑えた売電単価（offsite_lp_sell_price）は使わない。sell がそのまま効く条件だけを比較する
    c[idx["N"]:idx["N"] + T] = up
    c[idx["w"]:idx["w"] + T] = -(up - W_UNIT)
    c[idx["E"]:idx["E"] + T] = -sell
    if cap_var:
        c[icap] = batt_cost / payback
    A_eq = lil_matrix((3 * T + (1 if cap_var else 0) + 1, n_var))
    b_eq = np.zeros(A_eq.shape[0])
    for t in range(T):
        # 収支: N + pv + dis = dem + ch + E + C
        r = t
        A_eq[r, idx["N"] + t] = 1
        A_eq[r, idx["dis"] + t] = 1
        A_eq[r, idx["ch"] + t] = -1
        A_eq[r, idx["E"] + t] = -1
        A_eq[r, idx["C"] + t] = -1
        b_eq[r] = df[t] - pvf[t]
        # SOC遷移
        r = T + t
        A_eq[r, idx["soc"] + t] = 1
        A_eq[r, idx["ch"] + t] = -eff
        A_eq[r, idx["dis"] + t] = 1 / eff
        if t > 0:
            A_eq[r, idx["soc"] + t - 1] = -1
        elif cap_var:
            A_eq[r, icap] = -BAT["soc_min_pct"] / 100.0        # 初期SOC = 容量×下限
        elif cyc:
            A_eq[r, icyc] = -1                                 # 初期SOC = 変数（周期条件）
        else:
            b_eq[r] = capacity * BAT["soc_min_pct"] / 100.0
    # 終端SOC = 初期SOC（cap ありでは初期SOCも変数にはせず、ここでは cap なしの比較だけに使う）
    r = 2 * T
    A_eq[r, idx["soc"] + T - 1] = 1
    if cap_var:
        A_eq[r, icap] = -BAT["soc_min_pct"] / 100.0
    elif cyc:
        A_eq[r, icyc] = -1
    else:
        b_eq[r] = capacity * BAT["soc_min_pct"] / 100.0
    A_eq = A_eq[:2 * T + 1]
    b_eq = b_eq[:2 * T + 1]

    rows, rhs = [], []
    A_ub = lil_matrix((4 * T + (2 * T if cap_var else 0), n_var))
    b_ub = np.zeros(A_ub.shape[0])
    for t in range(T):
        # w ≤ N
        A_ub[t, idx["w"] + t] = 1
        A_ub[t, idx["N"] + t] = -1
        # 売電・抑制 ≤ そのコマの太陽光
        A_ub[T + t, idx["E"] + t] = 1
        A_ub[T + t, idx["C"] + t] = 1
        b_ub[T + t] = pvf[t]
        # 充電+放電 ≤ PCS
        A_ub[2 * T + t, idx["ch"] + t] = 1
        A_ub[2 * T + t, idx["dis"] + t] = 1
        b_ub[2 * T + t] = max(BAT["max_charge_kw"], BAT["max_discharge_kw"]) * dt
        # peak ≥ N/dt
        A_ub[3 * T + t, idx["N"] + t] = 1 / dt
        A_ub[3 * T + t, ipeak] = -1
        if cap_var:
            A_ub[4 * T + t, idx["soc"] + t] = 1
            A_ub[4 * T + t, icap] = -BAT["soc_max_pct"] / 100.0
            A_ub[5 * T + t, idx["soc"] + t] = -1
            A_ub[5 * T + t, icap] = BAT["soc_min_pct"] / 100.0
    bounds = [(0, None)] * n_var
    for t in range(T):
        bounds[idx["ch"] + t] = (0, BAT["max_charge_kw"] * dt)
        bounds[idx["dis"] + t] = (0, BAT["max_discharge_kw"] * dt)
        if not cap_var:
            bounds[idx["soc"] + t] = (capacity * BAT["soc_min_pct"] / 100.0, capacity * BAT["soc_max_pct"] / 100.0)
        bounds[idx["w"] + t] = (0, float(wf[t]))
        if no_export:
            bounds[idx["E"] + t] = (0, 0)
        else:
            bounds[idx["C"] + t] = (0, 0)
        if cap_kw is not None:
            bounds[idx["N"] + t] = (0, cap_kw * dt)
    if cap_var:
        bounds[icap] = (0, 2000)
    if cyc:
        bounds[icyc] = (capacity * BAT["soc_min_pct"] / 100.0, capacity * BAT["soc_max_pct"] / 100.0)
    res = linprog(c, A_ub=A_ub.tocsr(), b_ub=b_ub, A_eq=A_eq.tocsr(), b_eq=b_eq, bounds=bounds, method="highs")
    assert res.status == 0, res.message
    return res.fun


def true_cost(sc, pv, sell, no_export=False, spec=SPEC, rate=RATE):
    """LPが返した運転を、実際の売電単価・受電点の料金（offsite_cost_after）で評価した電気代−売電収入。"""
    off = app.offsite_receiving(pv, spec["sources"], DEM, sc)
    ca = app.offsite_cost_after(off, spec["sources"], MONTH_DAY, rate)
    exp_kwh = 0.0 if no_export else float(np.sum(off["pv_surplus"]))   # 売電は太陽光の余剰だけ
    return ca["annual_total"] - sell * exp_kwh, ca, off


def solve(capacity, sell, no_export=False, cap_kw=None, offsite=SPEC, pv=PV, rate=RATE):
    return app.optimize_battery(pv, DEM, MONTH_DAY, capacity_kwh=capacity, sell_price=sell, no_export=no_export,
                                grid_import_cap_kw=cap_kw, offsite=offsite, **BAT, **rate)


def invariants(tag, sc, pv, wind, cap_kw=None):
    R = sc["offsite_receive"]
    w = sc["offsite_delivered"]
    E = sc["export"]
    C = sc["curtailment"]
    ch, dis = sc["battery_charge"], sc["battery_discharge"]
    tol = 1e-6
    check(f"{tag}: 収支 R + PV + 放電 = 需要 + 充電 + 売電 + 抑制", np.allclose(R + pv + dis, DEM + ch + E + C, atol=1e-5),
          f"最大差 {np.abs(R + pv + dis - DEM - ch - E - C).max():.2e}")
    check(f"{tag}: 風力の配達分 0 ≤ w ≤ 風力の発電量、w ≤ R",
          (w >= -tol).all() and (w <= wind + tol).all() and (w <= R + tol).all())
    check(f"{tag}: 小売購入 = R − w（≥0）", np.allclose(sc["import_"], np.maximum(R - w, 0.0), atol=1e-9) and (sc["import_"] >= 0).all())
    both = np.minimum(R, E + C)
    check(f"{tag}: 受電量と売電（抑制）が同じコマで同時に正にならない（買電と売電は受電点で相殺）",
          both.max() < 1e-6, f"最大 {both.max():.3e} kWh")
    check(f"{tag}: 売電・抑制はそのコマの太陽光の余剰以下（風力は売電できない）", ((E + C) <= pv + 1e-6).all())
    check(f"{tag}: SOCが範囲内", (sc["soc"] >= 0.2 * sc["battery_capacity"] - 1e-6).all()
          and (sc["soc"] <= 0.95 * sc["battery_capacity"] + 1e-6).all())
    check(f"{tag}: 最適化ピーク = 受電量の最大（kW）", abs(sc["opt_peak_kw"] - R.max() / 0.5) < 1e-4,
          f"{sc['opt_peak_kw']:.3f} vs {R.max() / 0.5:.3f}")
    if cap_kw is not None:
        check(f"{tag}: 受電量が上限以下（風力の配達分を含む）", R.max() / 0.5 <= cap_kw + 1e-6, f"{R.max() / 0.5:.3f} ≤ {cap_kw}")


print("test_wind_lp.py — 風力を受電点の基準で解く蓄電池LP（W2d）")

# ============================================================
print("\n【1. optimize_battery(offsite): 独立実装との一致と不変条件】")
for label, kw in (
    ("売電8.5円・上限なし", dict(sell=8.5, no_export=False)),
    ("逆潮流禁止・上限なし", dict(sell=8.5, no_export=True)),
):
    sc = solve(200.0, **kw)
    tc, ca, off = true_cost(sc, PV, kw["sell"], kw["no_export"])
    check(f"{label}: 返り値 opt_annual_cost = 運転を受電点の料金で評価した値（電気代−売電収入）",
          abs(sc["opt_annual_cost"] - tc) < 0.5, f"{sc['opt_annual_cost']:.2f} vs {tc:.2f}")
    if HAS_SCIPY:
        ref = reference_lp(PV, WIND, DEM, 200.0, kw["sell"], kw["no_export"])
        check(f"{label}: 独立実装（scipy）の最適値と一致", abs(ref - sc["opt_annual_cost"]) < 1e-3 * abs(ref) + 0.5,
              f"{sc['opt_annual_cost']:.3f} vs {ref:.3f}")
    invariants(label, sc, PV, WIND)
    check(f"{label}: offsite_receiving はLPの受電量をそのまま使う", np.allclose(off["receive"], sc["offsite_receive"], atol=1e-9))
    check(f"{label}: 配達分（offsite_receiving）= LPの w", np.allclose(off["delivered"], sc["offsite_delivered"], atol=1e-6))

# 上限あり: 基本料金を0にする（基本料金があるとLPが自らピークを最小化し、上限が拘束しない）。上限が拘束する条件で比べる
RATE0 = dict(RATE, basic_charge_per_kw=0.0)
sc0 = solve(200.0, 8.5, rate=RATE0)
cap_kw = float(sc0["offsite_receive"].max() / 0.5) * 0.7
sc_c = solve(200.0, 8.5, cap_kw=cap_kw, rate=RATE0)
invariants("受電上限あり", sc_c, PV, WIND, cap_kw=cap_kw)
check("受電上限あり: 上限が拘束している（上限なしの最適は上限を超える）", sc0["offsite_receive"].max() / 0.5 > cap_kw + 1.0,
      f"{sc0['offsite_receive'].max() / 0.5:.1f} > {cap_kw:.1f}")
check("受電上限あり: 結果に上限が残る", abs(sc_c["grid_import_cap_kw"] - cap_kw) < 1e-9)
tc, _, _ = true_cost(sc_c, PV, 8.5, rate=RATE0)
check("受電上限あり: opt_annual_cost = 評価値", abs(sc_c["opt_annual_cost"] - tc) < 0.5, f"{sc_c['opt_annual_cost']:.2f} vs {tc:.2f}")
check("受電上限あり: 上限なしより電気代は下がらない（拘束したぶん高い）", sc_c["opt_annual_cost"] > sc0["opt_annual_cost"] + 1.0,
      f"{sc_c['opt_annual_cost']:.2f} > {sc0['opt_annual_cost']:.2f}")
if HAS_SCIPY:
    ref = reference_lp(PV, WIND, DEM, 200.0, 8.5, False, cap_kw=cap_kw, rate=RATE0)
    check("受電上限あり: 独立実装（scipy。周期SOC）の最適値と一致", abs(ref - sc_c["opt_annual_cost"]) < 1e-3 * abs(ref) + 0.5,
          f"{sc_c['opt_annual_cost']:.3f} vs {ref:.3f}")

# 実行不可能な上限は例外
try:
    solve(200.0, 8.5, cap_kw=1.0)
    check("守れない受電上限は GridCapInfeasibleError", False)
except app.GridCapInfeasibleError:
    check("守れない受電上限は GridCapInfeasibleError", True)

# ============================================================
print("\n【2. optimize_battery_capacity(offsite): 独立実装との一致】")
if HAS_SCIPY:
    for label, kw in (("売電8.5円", dict(no_export=False)), ("逆潮流禁止", dict(no_export=True))):
        r = app.optimize_battery_capacity(PV, DEM, MONTH_DAY, sell_price=8.5, battery_cost_per_kwh=20000.0,
                                          payback_years=1.0, offsite=SPEC, capacity_upper=2000, **kw, **BAT, **RATE)
        ref = reference_lp(PV, WIND, DEM, None, 8.5, kw["no_export"], cap_var=True, batt_cost=20000.0, payback=1.0)
        check(f"{label}: 最適値（電気代−売電＋蓄電池の年額換算）が独立実装と一致",
              abs(ref - r["opt_annual_cost"]) < 1e-3 * abs(ref) + 0.5, f"{r['opt_annual_cost']:.3f} vs {ref:.3f}")
        check(f"{label}: 最適容量が有限で0以上", 0 <= r["optimal_capacity_kwh"] <= 2000, f"{r['optimal_capacity_kwh']:.2f} kWh")
else:
    print("  （scipy が無いため飛ばす）")

print("\n【2b. grid_search_battery_capacity(offsite): 各容量の指標を、LPの運転を受電点の料金で評価し直した値と照合】")
before_total = app.calc_electricity_cost(DEM, MONTH_DAY, **RATE)["annual_total"]
gs = app.grid_search_battery_capacity(
    PV, DEM, MONTH_DAY, efficiency_pct=95.0, max_charge_kw=60.0, max_discharge_kw=60.0, soc_min_pct=20.0, soc_max_pct=95.0,
    sell_price=8.5, pv_cost=158000.0, battery_cost_per_kwh=200000.0, total_ppeak=150.0, co2_factor=0.000431,
    cost_before_total=before_total, payback_years=15, no_export=False, optimal_capacity=20.0, n_steps=3, offsite=SPEC, **RATE)
check("グリッドサーチは4点（容量0.1〜120kWh）を返す", len(gs) == 4, str([round(r["capacity"], 1) for r in gs]))
for r in gs:
    sc_g = solve(r["capacity"], 8.5)
    tc_g, ca_g, _ = true_cost(sc_g, PV, 8.5)
    merit_exp = before_total - tc_g - SPEC["payment_yen"]          # 電気代削減＋売電収入−風力PPA支払
    check(f"容量{r['capacity']:.1f}kWh: 年間メリット = 電気代削減＋売電収入−風力PPA支払", abs(r["annual_merit"] - merit_exp) < 0.5,
          f"{r['annual_merit']:.2f} vs {merit_exp:.2f}")
    check(f"容量{r['capacity']:.1f}kWh: 契約電力は受電量の最大で決まる", abs(r["contract_power_kw"] - ca_g["contract_power_kw"]) < 1e-6,
          f"{r['contract_power_kw']:.2f}")
    check(f"容量{r['capacity']:.1f}kWh: CO2削減量 = (需要−小売購入)×係数", abs(r["co2_reduction"] - (sc_g["annual_demand"] - sc_g["annual_import"]) * 0.000431) < 1e-6)

# ============================================================
print("\n【3. 売電単価が風力の配達単価を上回るとき（FIT 19円）に、風力を受電して太陽光を売る裁定が生じない】")
sell_fit = 19.0
check("前提: 売電単価 > 風力の配達単価（託送+賦課金+手数料）", sell_fit > W_UNIT, f"{sell_fit} > {W_UNIT:.2f}")
sc_fit = solve(200.0, sell_fit)
invariants("FIT 19円", sc_fit, PV, WIND)
tc_fit, _, off_fit = true_cost(sc_fit, PV, sell_fit)
check("FIT 19円: opt_annual_cost（実際の売電単価で評価し直した値）= 運転の評価値", abs(sc_fit["opt_annual_cost"] - tc_fit) < 0.5,
      f"{sc_fit['opt_annual_cost']:.2f} vs {tc_fit:.2f}")
# 蓄電池なし（容量ほぼ0）でも、受電と売電が同時に正にならない＝太陽光を売って風力を使う裁定がない
sc_fit0 = solve(0.1, sell_fit)
check("FIT 19円・蓄電池ほぼなし: 受電と売電は同時に正にならない", np.minimum(sc_fit0["offsite_receive"], sc_fit0["export"]).max() < 1e-6)
# 蓄電池なしの物理的な基準: 太陽光を先に使い、足りない分を（風力→小売で）受電する
n_phys = np.maximum(0.0, DEM - PV)
check("FIT 19円・蓄電池ほぼなし: 受電量は 需要−太陽光 の正の部分とほぼ一致（風力を買って太陽光を売る運転がない）",
      np.abs(sc_fit0["offsite_receive"] - n_phys).max() < 0.1, f"最大差 {np.abs(sc_fit0['offsite_receive'] - n_phys).max():.4f} kWh")
sell_lp = app.offsite_lp_sell_price(sell_fit, unit_prices(), W_UNIT)
check("LP内の売電単価は配達単価より厳密に低い（同時に正にする運転を損にする）", sell_lp < W_UNIT and sell_lp >= 0, f"{sell_lp:.2f} < {W_UNIT:.2f}")
check("売電単価が低いとき（8.5円）はそのまま使う", app.offsite_lp_sell_price(8.5, unit_prices(), W_UNIT) == 8.5)

# ============================================================
print("\n【4. ルールベース（W2e: 風力を貯めず、風力→蓄電池→小売の順）を、LPが上回る（受電点の基準の電気代で比較）】")
rule = app.simulate_battery(PV, DEM, MONTH_DAY, capacity_kwh=200.0, no_export=False, offsite_gen=WIND, **BAT)
tc_rule, _, _ = true_cost(rule, PV, 8.5)
sc8 = solve(200.0, 8.5)
tc8, _, _ = true_cost(sc8, PV, 8.5)
check("LP ≤ ルールベース（受電点の電気代−売電収入。LPが最適）", tc8 <= tc_rule + 0.5, f"{tc8:,.0f} ≤ {tc_rule:,.0f}")
nob = app.calculate_self_consumption(PV + WIND, DEM, MONTH_DAY)
tc_nob, _, _ = true_cost(nob, PV, 8.5)
check("LP ≤ 蓄電池なし", tc8 <= tc_nob + 0.5, f"{tc8:,.0f} ≤ {tc_nob:,.0f}")
check("ルールベース（W2e）≤ 蓄電池なし（風力を貯めないので、蓄電池を足しても悪くならない）", tc_rule <= tc_nob + 0.5,
      f"{tc_rule:,.0f} ≤ {tc_nob:,.0f}")

# ============================================================
print("\n【5. offsite=None（風力なし）は従来のLPのまま】")
plain = app.optimize_battery(PV, DEM, MONTH_DAY, capacity_kwh=200.0, sell_price=8.5, no_export=False, **BAT, **RATE)
check("offsite=None: 結果に offsite_* のキーがない", "offsite_receive" not in plain and "offsite_delivered" not in plain)
check("offsite=None: 従来のキー構成（上限なしなら grid_import_cap_kw も無い）", "grid_import_cap_kw" not in plain)
zero_w = source(np.zeros_like(WIND))
sc_z = app.optimize_battery(PV, DEM, MONTH_DAY, capacity_kwh=200.0, sell_price=8.5, no_export=False,
                            offsite=app.offsite_lp_spec([zero_w]), **BAT, **RATE)
check("風力が常に0のオフサイトLPは、風力なしのLPと同じ最適値（同じ問題になる）",
      abs(sc_z["opt_annual_cost"] - plain["opt_annual_cost"]) < 0.5, f"{sc_z['opt_annual_cost']:.2f} vs {plain['opt_annual_cost']:.2f}")

print("\n【5b. offsite_receiving: LPの結果（offsite_receive を持つ）は、受電量・売電・抑制をそのまま使う（合成データ）】")
z = np.zeros((1, 48))
sc_syn = dict(import_=z + 2.0, battery_charge=z, battery_discharge=z, offsite_receive=z + 5.0, export=z + 3.0, curtailment=z + 0.5)
src_syn = dict(gen_30min=z + 4.0, wheeling_yen=2.15, retail_fee_yen=3.0)
off_syn = app.offsite_receiving(z, [src_syn], z + 10.0, sc_syn)
check("受電量 = LPの offsite_receive（収支からの逆算ではない）", np.allclose(off_syn["receive"], 5.0))
check("太陽光の余剰 = LPの売電 + 抑制（同じコマで受電と売電が正でも、そのまま）", np.allclose(off_syn["pv_surplus"], 3.5), str(off_syn["pv_surplus"][0, 0]))
check("配達量 = 受電量 − 小売購入（風力の発電量以下）", np.allclose(off_syn["delivered"], 3.0))
sc_rule = dict(import_=z + 6.0, battery_charge=z, battery_discharge=z)
off_rule = app.offsite_receiving(z, [src_syn], z + 10.0, sc_rule)
check("ルールベース（offsite_receive なし）は従来どおり収支から逆算する", np.allclose(off_rule["receive"], 10.0) and np.allclose(off_rule["pv_surplus"], 0.0))

print("\n【5c. simulate_battery(offsite_gen)（W2e）: ルールベースは風力を貯めない。参照実装・不変条件・従来との同一性】")


def reference_rule(pv, wind, dem, cap_kwh, ch_kw, dis_kw, eff_pct=95.0, smin=20.0, smax=95.0, no_export=False):
    """ルールベース（風力あり）の参照実装。simulate_battery とは別に、1コマずつ素直に書く。
    順序: 太陽光を直接使う → 太陽光の余剰で充電 → 不足に風力（届いた分）→ 風力で賄えない残りに放電 → 残りが小売購入。"""
    eff = eff_pct / 100.0
    lo, hi = cap_kwh * smin / 100.0, cap_kwh * smax / 100.0
    soc = lo
    out = {k: np.zeros(pv.shape) for k in ("ch", "dis", "w", "retail", "recv", "exp", "cur")}
    for d in range(pv.shape[0]):
        for s in range(pv.shape[1]):
            g, dm, wd = pv[d, s], dem[d, s], wind[d, s]
            direct = min(g, dm)
            surplus, deficit = g - direct, dm - direct
            c = 0.0
            if surplus > 0:
                c = min(surplus, ch_kw * 0.5, max(0.0, (hi - soc) / eff))
                soc += c * eff
                surplus -= c
            w = min(deficit, wd)
            deficit -= w
            x = 0.0
            if deficit > 0:
                x = min(deficit, dis_kw * 0.5, max(0.0, (soc - lo) * eff))
                soc -= x / eff
                deficit -= x
            soc = max(lo, min(hi, soc))
            if no_export:
                out["cur"][d, s], surplus = surplus, 0.0
            out["ch"][d, s], out["dis"][d, s], out["w"][d, s] = c, x, w
            out["retail"][d, s], out["exp"][d, s] = deficit, surplus
            out["recv"][d, s] = w + deficit
    return out


for label, kw in (("余剰売電", dict(no_export=False)), ("逆潮流禁止", dict(no_export=True))):
    rb = app.simulate_battery(PV, DEM, MONTH_DAY, capacity_kwh=80.0, efficiency_pct=95.0, max_charge_kw=60.0, max_discharge_kw=60.0,
                              soc_min_pct=20.0, soc_max_pct=95.0, offsite_gen=WIND, **kw)
    ref = reference_rule(PV, WIND, DEM, 80.0, 60.0, 60.0, **kw)
    check(f"{label}: 充電・放電・配達・小売購入・受電量・売電・抑制が参照実装と一致",
          all(np.allclose(a, b, atol=1e-9) for a, b in (
              (rb["battery_charge"], ref["ch"]), (rb["battery_discharge"], ref["dis"]), (rb["offsite_delivered"], ref["w"]),
              (rb["import_"], ref["retail"]), (rb["offsite_receive"], ref["recv"]), (rb["export"], ref["exp"]),
              (rb["curtailment"], ref["cur"]))))
    direct = np.minimum(PV, DEM)
    check(f"{label}: 収支 需要 = 太陽光の直接使用 + 風力の配達 + 放電 + 小売購入",
          np.allclose(DEM, direct + rb["offsite_delivered"] + rb["battery_discharge"] + rb["import_"], atol=1e-9))
    check(f"{label}: 蓄電池は太陽光の余剰でだけ充電する（風力では充電しない。受電量を押し上げない）",
          ((rb["battery_charge"] > 1e-12) <= (PV > DEM)).all() and (rb["battery_charge"] <= np.maximum(0.0, PV - DEM) + 1e-9).all())
    check(f"{label}: 受電量 = 風力の配達分 + 小売購入", np.allclose(rb["offsite_receive"], rb["offsite_delivered"] + rb["import_"], atol=1e-9))
    check(f"{label}: 自家消費量 = 需要 − 小売購入（風力の配達分を含む）、自家消費率の分母は太陽光＋風力の発電量",
          np.allclose(rb["self_consumption"], DEM - rb["import_"], atol=1e-9)
          and abs(rb["annual_self"] - (DEM.sum() - rb["annual_import"])) < 1e-6
          and abs(rb["self_consumption_rate"] - rb["annual_self"] / (PV + WIND).sum() * 100) < 1e-9
          and abs(sum(rb["monthly_gen"].values()) - (PV + WIND).sum()) < 1e-6)
    check(f"{label}: 受電量は蓄電池なし（Σ max(0, 需要−太陽光)）を超えない（従来の合算運転は超えうる）",
          (rb["offsite_receive"] <= np.maximum(0.0, DEM - PV) + 1e-9).all())
    check(f"{label}: 放電は風力で賄えない残りの分だけ（風力の配達分を蓄電池で置き換えない）",
          (rb["battery_discharge"] <= np.maximum(0.0, DEM - PV - WIND) + 1e-9).all())
    check(f"{label}: 配達量 = min(太陽光で賄えない不足, 風力)（蓄電池に依存しない）",
          np.allclose(rb["offsite_delivered"], np.minimum(np.maximum(0.0, DEM - PV), WIND), atol=1e-9))
    off_rb = app.offsite_receiving(PV, SPEC["sources"], DEM, rb)
    check(f"{label}: offsite_receiving は運転の受電量・配達量・太陽光の余剰をそのまま使う",
          np.allclose(off_rb["receive"], rb["offsite_receive"]) and np.allclose(off_rb["delivered"], rb["offsite_delivered"])
          and np.allclose(off_rb["pv_surplus"], rb["export"] + rb["curtailment"]))
    nb = app.calculate_self_consumption(PV + WIND, DEM, MONTH_DAY)
    check(f"{label}: 蓄電池なしの小売購入（従来の合算）以下（蓄電池は購入を減らす）",
          rb["annual_import"] <= nb["annual_import"] + 1e-9, f"{rb['annual_import']:.2f} <= {nb['annual_import']:.2f}")

# 風力が常に0なら、風力なしのルールベースと同じ運転になる
rb0 = app.simulate_battery(PV, DEM, MONTH_DAY, capacity_kwh=80.0, efficiency_pct=95.0, max_charge_kw=60.0, max_discharge_kw=60.0,
                           soc_min_pct=20.0, soc_max_pct=95.0, offsite_gen=np.zeros_like(WIND))
pl = app.simulate_battery(PV, DEM, MONTH_DAY, capacity_kwh=80.0, efficiency_pct=95.0, max_charge_kw=60.0, max_discharge_kw=60.0,
                          soc_min_pct=20.0, soc_max_pct=95.0)
check("風力が常に0: 風力なしのルールベースと同じ運転（充電・放電・購入・売電）",
      all(np.allclose(rb0[k], pl[k]) for k in ("battery_charge", "battery_discharge", "import_", "export")))
check("offsite_gen を省略: 従来のキー構成（offsite_* を持たない）", "offsite_receive" not in pl and "offsite_delivered" not in pl)

# 従来の合算運転が受電量のピークを押し上げうること（W2eの動機）。凪と風のコマだけの小さなデータで作る:
# 需要10kWh/コマ・太陽光なし、コマ5だけ風力50kWh。合算運転は風力の余剰40kWhを蓄電池に貯める（充電30kWh）が、
# 貯めるには送配電網から受ける量を 10 → 40kWh に増やすことになり、受電量（契約電力）のピークが上がる
z1 = np.zeros((1, 48))
d1 = z1 + 10.0
w1 = z1.copy()
w1[0, 5] = 50.0
kw1 = dict(capacity_kwh=500.0, efficiency_pct=95.0, max_charge_kw=60.0, max_discharge_kw=60.0, soc_min_pct=20.0, soc_max_pct=95.0)
pool1 = app.simulate_battery(z1 + w1, d1, [(1, 1)], **kw1)
r_pool1 = np.maximum(np.maximum(0.0, d1 + pool1["battery_charge"] - pool1["battery_discharge"] - z1), pool1["import_"])
rb_1 = app.simulate_battery(z1, d1, [(1, 1)], offsite_gen=w1, **kw1)
check("従来の合算運転は風力を貯めて受電量のピークを押し上げる（10kWh → 40kWh/コマ）、W2eの運転は押し上げない",
      abs(r_pool1.max() - 40.0) < 1e-9 and abs(rb_1["offsite_receive"].max() - 10.0) < 1e-9,
      f"合算 {r_pool1.max()} / W2e {rb_1['offsite_receive'].max()}")
check("  W2eの運転: 風力は貯めず（充電0）、コマ5の風力の配達は需要10kWhまで（残り40kWhは無駄）",
      rb_1["battery_charge"].sum() == 0.0 and abs(rb_1["offsite_delivered"][0, 5] - 10.0) < 1e-9)

# WIND_LP_FAST=1: run_simulation を通す節（約1.5分）を飛ばす（変異テストでLPの定式化だけを見るとき用）
if os.environ.get("WIND_LP_FAST"):
    print(f"\n結果（節6・7を飛ばした高速版）: PASS {n_pass} / FAIL {n_fail}")
    sys.exit(0 if n_fail == 0 else 1)

# ============================================================
print("\n【6. run_simulation: 受電上限（DC）・最適容量探索との併用】")
src_t = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_wind_mode.py"), encoding="utf-8").read()
ns = {"__name__": "prelude", "__file__": os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_wind_mode.py")}
exec(compile(src_t[:src_t.index('print("【1.')], "prelude", "exec"), ns)     # run / wind / DC / face_args などの共通準備
run, wind, DC, face_args, num = ns["run"], ns["wind"], ns["DC"], ns["face_args"], ns["num"]
dc_kw = dict(demand_source=app.DEMAND_SOURCE_DATACENTER, station_choice="14163 (SAPPORO)",
             face_args=face_args([(3000.0, "南", 180.0, 30, 0)]))
LPB = dict(bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=3000.0, bat_max_charge=1500.0, bat_max_discharge=1500.0)
w60 = wind("coverage", coverage_pct=60.0)
for cap_mode, cap_val in ((app.GRID_CAP_HV, 1999.0), (app.GRID_CAP_EHV33, 9999.0)):
    o = run(dc_args=dict(DC, grid_cap_mode=cap_mode), wind_args=w60, **LPB, **dc_kw)
    ok = not o[4].startswith("エラー") and o[6] is not None
    check(f"DC + 風力 + 受電上限（{cap_val:,.0f}kW）+ LP: 計算できる（従来は明示エラー）", ok, o[4][:60].replace("\n", " "))
    if ok:
        scr = o[6]["sc_result"]
        R = scr["offsite_receive"]
        check(f"  受電量（風力の配達分を含む）の最大が上限以下", R.max() / 0.5 <= cap_val + 1e-6, f"{R.max() / 0.5:.1f} kW")
        check("  結果に受電上限の節と『強制しています』が出る", "受電上限" in o[4] and "強制しています" in o[4])
        m = re.search(r"導入後ピーク: ([\d,\.]+) kW", o[4])
        check("  導入後ピークは受電量の最大（風力の配達分を含む）", m is not None and abs(float(m.group(1).replace(",", "")) - R.max() / 0.5) < 0.06,
              f"{m.group(1) if m else None} vs {R.max() / 0.5:.1f}")
        check("  『受電点の基準で行います』の説明が出る", "受電点の基準で行います" in o[4])
        check("  LPの最適化ピーク・コストが出る（風力あり）", "最適化ピークデマンド（受電点）" in o[4] and "最適化年間コスト" in o[4])

# 上限を守れない条件（診断）は風力があっても診断を返す（風力は上限を守る助けにならない）
tight = run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_MANUAL, grid_cap_kw=300.0), wind_args=w60, **LPB, **dc_kw)
tight_nw = run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_MANUAL, grid_cap_kw=300.0), **LPB, **dc_kw)
check("上限が低すぎる条件: 風力があっても診断（守れない）を返す", tight[6] is None and "受電上限" in tight[4] and "守れません" in tight[4], tight[4][:60])
check("  診断は風力なしと同じ（風力は上限を守る助けにならない）", tight[4].split("══ 受電上限 ══")[-1] == tight_nw[4].split("══ 受電上限 ══")[-1])

# ルールベース + 受電上限 + 風力: 上限は強制せず、導入後ピークは受電量（風力の配達分＋小売購入）の最大で判定する。
# 受電量のピークと小売購入のピークが分かれる条件にする: 太陽光なし（風力のみ）・CEC実測形状（ピークが1コマに立つ）。
# 需要が定常だと、風力が止まる夜のコマで両者が一致してしまい、測り方の違いを検出できない
rb = run(dc_args=dict(DC, profile_mode=app.PROFILE_CEC, grid_cap_mode=app.GRID_CAP_HV), wind_args=wind("coverage", coverage_pct=300.0),
         bat_enabled=True, bat_mode="ルールベース", bat_capacity=3000.0, bat_max_charge=1500.0, bat_max_discharge=1500.0,
         pv_enabled=False, **dc_kw)
check("ルールベース + 風力 + 受電上限: 計算でき、上限は強制せず判定だけ出る（上限内なら『蓄電池なしでも上限を守れます』）",
      not rb[4].startswith("エラー") and rb[6] is not None and "受電上限" in rb[4] and "蓄電池なしでも上限を守れます" in rb[4], rb[4][:60])
if rb[6] is not None:
    m = re.search(r"導入後ピーク: ([\d,\.]+) kW", rb[4])
    st = rb[6]
    scr_rb = st["sc_result"]
    check("  ルールベースの運転結果も受電点の基準（offsite_receive を持つ）", "offsite_receive" in scr_rb)
    r_peak = scr_rb["offsite_receive"].max() / 0.5
    retail_peak = scr_rb["import_"].max() / 0.5
    check("  導入後ピークは受電量（風力の配達分＋小売購入）の最大", m is not None and abs(float(m.group(1).replace(",", "")) - r_peak) < 0.06,
          f"{m.group(1) if m else None} vs {r_peak:.1f}")
    check("  この条件では受電量のピークが小売購入のピークより大きい（確認の条件が有効）", r_peak > retail_peak + 1.0, f"{r_peak:.1f} > {retail_peak:.1f}")

# LP（風力あり・上限なし）: 結果テキストの整合（LPの最適化コスト = 導入後の電気代 − 売電収入）
o = run(wind_args=wind("coverage"), bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=200.0,
        bat_max_charge=100.0, bat_max_discharge=100.0)
st = o[6]
scr = st["sc_result"]
off = app.offsite_receiving(st["gen_pv"], [st["wind_info"]], st["demand_30min"], scr)
ca = app.offsite_cost_after(off, [st["wind_info"]], st["month_day"], RATE)
sell = 19.0 * scr["annual_export"]
check("run_simulation LP + 風力（売電19円）: 最適化年間コスト = 導入後の電気代 − 売電収入", abs(scr["opt_annual_cost"] - (ca["annual_total"] - sell)) < 1.0,
      f"{scr['opt_annual_cost']:,.1f} vs {ca['annual_total'] - sell:,.1f}")
check("run_simulation LP + 風力: 受電量と売電が同時に正にならない", np.minimum(scr["offsite_receive"], scr["export"] + scr["curtailment"]).max() < 1e-6)

# 最適容量探索 + 風力
cs_o = run(bat_enabled=True, bat_mode="最適容量探索", bat_max_charge=100.0, bat_max_discharge=100.0, wind_args=wind("coverage"),
             bat_cost_per_kwh=60000.0)   # 単価を下げて、最適容量が数十kWhになる条件にする（風力の扱いで最適容量が変わる）
cs_ok = not cs_o[4].startswith("エラー") and "最適蓄電池容量" in cs_o[4] and "最適容量探索エラー" not in cs_o[4]
check("最適容量探索 + 風力: 計算できる（従来は明示エラー）", cs_ok, cs_o[4][:80].replace("\n", " "))
if cs_ok:
    opt_cap = num(cs_o[4], "最適蓄電池容量:")
    check("  最適容量が数値で出る（有限・0以上）", opt_cap is not None and opt_cap >= 0, str(opt_cap))
    check("  段階2のグリッドサーチが完了する", "段階2: グリッドサーチ】探索完了" in cs_o[4] and cs_o[3] is not None)
    # 最適容量探索モードでは、上部の設備投資に入力欄の容量（別モードの値が残りうる）の蓄電池を数えない。
    # 上部の需給・料金は蓄電池なしで計算しているので、費用だけ乗ると回収年数が数十〜百年に見える（W2d のUI確認で発見）
    top = cs_o[4][cs_o[4].find("初期投資・投資回収"):cs_o[4].find("CO2削減量")]
    check("  最適容量探索: 上部の設備投資に蓄電池の費用を入れない（太陽光のみ = 150kW × 158,000円）",
          "設備投資合計: 23,700,000 円" in top and "kWh × " not in top and "蓄電池: 含めません" in top, top[:160])
    merit1 = num(cs_o[4], "年間コスト削減:", after="最適蓄電池容量探索")
    # 結果テキストの最適容量は小数1桁なので、同じ条件で段階1のLPを直接解いて正確な最適容量を得る
    st_c = cs_o[6]
    cap_exact = app.optimize_battery_capacity(
        st_c["gen_pv"], st_c["demand_30min"], st_c["month_day"], efficiency_pct=95, max_charge_kw=100.0,
        max_discharge_kw=100.0, soc_min_pct=20, soc_max_pct=95, sell_price=19.0, battery_cost_per_kwh=60000.0,
        payback_years=15, no_export=False, offsite=app.offsite_lp_spec([st_c["wind_info"]]), **RATE)["optimal_capacity_kwh"]
    check("  結果テキストの最適容量 = 直接解いた段階1の最適容量（小数1桁）", abs(cap_exact - opt_cap) < 0.051, f"{cap_exact:.4f} vs {opt_cap}")
    # 同じ容量で通常のLP（風力あり）を実行し、年間経済メリット（PPA支払後）と段階1の値が一致することを確認する
    lp_same = run(wind_args=wind("coverage"), bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=float(cap_exact),
                  bat_max_charge=100.0, bat_max_discharge=100.0)
    top2 = lp_same[4][lp_same[4].find("初期投資・投資回収"):lp_same[4].find("CO2削減量")]
    check("  最適充放電（LP）モードでは従来どおり蓄電池の費用を数える（最適容量探索の修正の副作用がない）",
          "蓄電池: " in top2 and "kWh × " in top2 and "蓄電池: 含めません" not in top2, top2[:160])
    merit2 = num(lp_same[4], "年間経済メリット:", after="風力込みの年間経済メリット")
    check("  段階1の年間コスト削減（風力PPA支払後）= 最適容量で通常LPを解いた年間経済メリット",
          merit1 is not None and merit2 is not None and abs(merit1 - merit2) <= 1.0 + 1e-6 * abs(merit2),
          f"{merit1} vs {merit2}")

# ============================================================
print("\n【7. 風力を使わないとき（従来経路）の LP は変わらない】")
plain_run = run(bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=200.0, bat_max_charge=100.0, bat_max_discharge=100.0)
check("風力なし: 従来のLP（受電点の節が出ない・offsite_* が無い）",
      "受電点の基準で行います" not in plain_run[4] and "offsite_receive" not in plain_run[6]["sc_result"])

print(f"\n結果: PASS {n_pass} / FAIL {n_fail}")
sys.exit(0 if n_fail == 0 else 1)
