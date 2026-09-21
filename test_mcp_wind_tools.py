# -*- coding: utf-8 -*-
"""
test_mcp_wind_tools.py - 風力発電（オフサイトPPA）のMCPツール（W4）の検証
=======================================================================
設計: docs/wind_design_spec.md §5-7
対象:
  1. simulate_industrial_pv / simulate_dc の wind・pv_enabled が、UI（run_simulation）と数値で一致する
     （メリット・配達量・費用・契約電力・24/7・MGのP-IRR・PPA単価）
  2. 入力検証（未知のキー・容量の指定・対象外の地点・受電上限との併用・数値の範囲）
  3. list_wind_areas / estimate_wind_generation
  4. 出力がJSON標準の型だけ（numpy型はMCP経由で文字列になる既知の罠）
  5. gr.api に登録されている

実行: python test_mcp_wind_tools.py
※ wind を省略したときの出力が従来とバイト同一であることは、MCPツールの出力を保存→照合する回帰
  （12ケース。産業用・LP・MG・PPA・リース・両面・DC・受電上限）で確認済み。
  プロトコル層（tools/call）での型の確認は、サーバー起動後に別途行う（CLAUDE.md）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import app
import mcp_tools as m

n_pass = 0
n_fail = 0


def check(label, cond, detail=""):
    global n_pass, n_fail
    if cond:
        n_pass += 1
    else:
        n_fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def non_json(o, path="$"):
    """JSON標準の型（dict/list/str/int/float/bool/None）以外が混ざっていないか。"""
    bad = []
    if isinstance(o, dict):
        for k, v in o.items():
            if not isinstance(k, str):
                bad.append(f"{path}.{k!r}(key:{type(k).__name__})")
            bad += non_json(v, f"{path}.{k}")
    elif isinstance(o, (list, tuple)):
        if isinstance(o, tuple):
            bad.append(f"{path}(tuple)")
        for i, v in enumerate(o):
            bad += non_json(v, f"{path}[{i}]")
    elif not (o is None or type(o) in (str, int, float, bool)):
        bad.append(f"{path}({type(o).__name__})")
    return bad


def fac_args(specs):
    a = []
    for t, area, n in specs:
        a += [t, area, n]
    while len(a) < app.MAX_FACILITIES * 3:
        a += ["なし", 0, 0]
    return tuple(a)


def face_args(specs):
    a = []
    for pp, ori, azi, tilt, pcs in specs:
        a += [pp, ori, azi, tilt, pcs]
    while len(a) < app.MAX_FACES * 5:
        a += [0, "南", None, 30, 0]
    return tuple(a)


UI_BASE = dict(
    station_choice="34392 (SENDAI)", csv_file=None, demand_custom_csv=None,
    KHD=app.DEFAULT_KHD, KPD=app.DEFAULT_KPD, KPM=app.DEFAULT_KPM, KPA=app.DEFAULT_KPA, eta_ino=app.DEFAULT_ETA_INO,
    alpha_pct=app.DEFAULT_ALPHA, delta_t=app.DEFAULT_DELTA_T,
    bat_enabled=False, bat_mode="ルールベース", bat_capacity=5.0, bat_efficiency=95,
    bat_max_charge=2.5, bat_max_discharge=2.5, bat_soc_min=20, bat_soc_max=95,
    elec_basic=None, elec_summer=None, elec_other=None, elec_pf=None, elec_fuel=None, elec_renewable=None,
    contract_type="高圧", sell_mode="余剰売電", sell_price=19.0,
    pv_cost_per_kw=158000, bat_cost_per_kwh=200000, substation_cost_per_kva=27500,
    subsidy_enabled=False, subsidy_pv_pct=0, subsidy_bat_pct=0, co2_factor=0.000431,
    business_model="自己所有", contract_years=15, target_irr=10.0,
    mg_enabled=False, mg_line_distance=2.0, mg_line_cost_per_km=30000000, mg_opex_ratio=2.0, mg_irr_period=20,
    bifacial_enabled=False, bifaciality_val=0.75, gcr_val=0.4, height_val=2.0, pitch_val=5.0,
    snow_albedo_enabled=True,
    facility_args=fac_args([("役所・自治体庁舎", 4500, 1)]),
    face_args=face_args([(150.0, "南", 180.0, 30, 0)]),
    num_facilities=1, num_faces=1, display_month=7, display_day=1,
)
MCP_FACES = [{"ppeak_kw": 150.0, "tilt_deg": 30.0, "azimuth_deg": 180.0}]
MCP_FAC = [{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}]


def mcp_sim(**kw):
    a = dict(station_no="34392", faces=MCP_FACES, facilities=MCP_FAC, sell_scheme="fit", fit_elapsed_years=1)
    a.update(kw)
    return m.simulate_industrial_pv(**a)


def ui_sim(wind, **over):
    kw = dict(UI_BASE)
    kw.update(over)
    return app.run_simulation(wind_args=wind, **kw)


def uw(cap=None, cov=None, **kw):
    """UI側の wind_args。"""
    d = dict(enabled=True, sizing_mode=app.WIND_SIZING_CAPACITY if cap is not None else app.WIND_SIZING_COVERAGE,
             capacity_kw=cap, coverage_pct=cov, cf_pct=None, ppa_price=None, wheeling_yen=None, retail_fee_yen=None)
    d.update(kw)
    return d


def num(text, label, after=None):
    start = text.find(after) if after else 0
    mm = re.search(re.escape(label) + r"\s*(-?[\d,]+(?:\.\d+)?)", text[start:])
    return float(mm.group(1).replace(",", "")) if mm else None


# ============================================================
print("【1. UI（run_simulation）との数値一致】")
# A. 産業用・蓄電池なし・需要カバー率100%
ma = mcp_sim(wind={"coverage_pct": 100.0})
ua = ui_sim(uw(cov=100.0))
ut = ua[4]
w = ma.get("wind", {})
check("A: MCPが完了し wind 節が返る", "error" not in ma and bool(w), str(ma.get("errors") or ma.get("error"))[:80])
check("A: 風力の年間発電量・配達量・無駄になった分がUIと一致",
      w["generation_kwh"] == round(ua[6]["wind_info"]["annual_kwh"]) and w["delivered_kwh"] == round(ua[6]["wind_info"]["delivered_kwh"])
      and w["wasted_kwh"] == round(ua[6]["wind_info"]["wasted_kwh"]))
check("A: 年間経済メリット（風力込み）がUIと一致（±1.5円）",
      abs(ma["electricity_cost"]["annual_economic_merit_yen"] - num(ut, "年間経済メリット:", after="【風力込みの年間経済メリット】")) < 1.5,
      f"{ma['electricity_cost']['annual_economic_merit_yen']} vs {num(ut, '年間経済メリット:', after='【風力込みの年間経済メリット】')}")
check("A: 風力PPA支払・届いた分の費用がUIと一致",
      abs(w["cost"]["ppa_payment_yen_per_year"] - num(ut, "PPA支払:")) < 1.5
      and abs(w["cost"]["delivered_extra_cost_yen_per_year"] - num(ut, "届いた風力にかかる費用:")) < 1.5)
cp_ui = num(ut[ut.find("【導入後】"):], "契約電力:")
check("A: 導入後の契約電力がUIと一致（風力では下がらない）",
      abs(ma["electricity_cost"]["contract_power_after_kw"] - cp_ui) < 0.06
      and ma["electricity_cost"]["contract_power_after_kw"] == mcp_sim()["electricity_cost"]["contract_power_after_kw"],
      f"{ma['electricity_cost']['contract_power_after_kw']} vs {cp_ui}")
check("A: 投資回収年数がUIと一致", abs(ma["investment"]["simple_payback_years"] - num(ut, "単純投資回収年数:")) < 0.06)
check("A: 24/7（設定どおり）の時間一致率・量ベース達成率がUIと一致",
      abs(w["matching_24_7"]["hourly_match_pct"] - float(re.search(r"時間一致率: ([\d.]+)%（系統購入", ut).group(1))) < 0.06
      and abs(w["matching_24_7"]["volume_pct"] - float(re.search(r"量ベース達成率: ([\d.]+)%（年間", ut).group(1))) < 0.06)
ref = w["matching_24_7"]["reference_without_battery"]
mt = re.search(r"太陽光のみ\s+([\d.]+)%\s+([\d.]+)%", ut)
check("A: 蓄電池なしの参考値（太陽光のみ）がUIと一致",
      abs(ref["pv_only"]["volume_pct"] - float(mt.group(1))) < 0.06 and abs(ref["pv_only"]["hourly_match_pct"] - float(mt.group(2))) < 0.06)
check("A: 月別は12行で、月別の系統購入の合計 ≒ 年間の系統購入",
      len(w["monthly"]) == 12 and abs(sum(r["import_kwh"] for r in w["monthly"]) - ma["annual"]["import_kwh"]) <= 12)
check("A: 太陽光の発電量は風力の有無で変わらない", ma["annual"]["pv_generation_kwh"] == mcp_sim()["annual"]["generation_kwh"])
check("A: 売電量は太陽光の余剰だけ（風力があっても従来と同じ）", ma["annual"]["export_kwh"] == mcp_sim()["annual"]["export_kwh"] or
      ma["annual"]["export_kwh"] <= mcp_sim()["annual"]["export_kwh"] + 1)

# B. 蓄電池LP（受電点基準への組み替えがルールベース以外でも一致）
mb = mcp_sim(wind={"capacity_kw": 200.0}, battery_enabled=True, battery_mode="lp_optimized", battery_capacity_kwh=200.0,
             battery_max_charge_kw=100.0, battery_max_discharge_kw=100.0)
ub = ui_sim(uw(cap=200.0), bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=200.0, bat_max_charge=100.0, bat_max_discharge=100.0)
check("B: LP+風力: メリット・配達量・契約電力がUIと一致",
      "error" not in mb and abs(mb["electricity_cost"]["annual_economic_merit_yen"]
                                - num(ub[4], "年間経済メリット:", after="【風力込みの年間経済メリット】")) < 1.5
      and mb["wind"]["delivered_kwh"] == round(ub[6]["wind_info"]["delivered_kwh"]),
      str(mb.get("error"))[:80])

# C. 風力のみ（pv_enabled=false）
mc = mcp_sim(wind={"capacity_kw": 300.0}, pv_enabled=False, faces=[])
uc = ui_sim(uw(cap=300.0), pv_enabled=False)
check("C: 風力のみ: 完了し、太陽光の発電量は0・初期投資は0・回収年数はなし",
      "error" not in mc and mc["annual"]["pv_generation_kwh"] == 0 and mc["investment"]["total_investment_yen"] == 0
      and mc["investment"]["simple_payback_years"] is None, str(mc.get("errors") or mc.get("error"))[:80])
check("C: 風力のみ: メリットがUIと一致・24/7の参考値に太陽光のみの行はない",
      abs(mc["electricity_cost"]["annual_economic_merit_yen"] - num(uc[4], "年間経済メリット:", after="【風力込みの年間経済メリット】")) < 1.5
      and "pv_only" not in mc["wind"]["matching_24_7"]["reference_without_battery"])
# 年間メリットが正になる小さな風力のみ: 初期投資がないので、回収年数は 0 年ではなく『なし』（None）
mc3 = mcp_sim(wind={"capacity_kw": 20.0}, pv_enabled=False, faces=[])
check("C: 風力のみ（小規模・メリットが正）: 初期投資0のため回収年数は None（0年と出さない）",
      mc3["electricity_cost"]["annual_economic_merit_yen"] > 0 and mc3["investment"]["net_investment_yen"] == 0
      and mc3["investment"]["simple_payback_years"] is None,
      f"merit={mc3['electricity_cost']['annual_economic_merit_yen']} payback={mc3['investment']['simple_payback_years']}")
mc2 = mcp_sim(wind={"capacity_kw": 300.0}, faces=[{"ppeak_kw": 0.0}])
check("C: 面のPpeakが0はMCPでは入力エラー（風力のみにしたいときは pv_enabled=false）", "error" in mc2 and "ppeak_kw" in str(mc2))

# D. MG + PPA + 風力（W2b）
FAC2 = [{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}]
md = mcp_sim(wind={"coverage_pct": 60.0}, mg_enabled=True, business_model="ppa")
ud = ui_sim(uw(cov=60.0), mg_enabled=True, business_model="PPA")
mg = md.get("microgrid") or {}
check("D: MG+PPA+風力: 年間キャッシュフローがUIと一致",
      "error" not in md and abs(mg["mg_annual_cashflow_yen"] - num(ud[4], "年間キャッシュフロー:")) < 1.5,
      f"{mg.get('mg_annual_cashflow_yen')} vs {num(ud[4], '年間キャッシュフロー:')}")
check("D: MG+PPA+風力: 必要PPA単価がUIと一致", abs(md["business"]["required_ppa_price_yen_per_kwh"] - num(ud[4], "必要PPA単価:")) < 0.006)
irr_ui = re.search(r"P-IRR: ([\d.]+)%", ud[4][ud[4].find("【P-IRR（"):])
check("D: MG+PPA+風力: P-IRRがUIと一致（算出不可なら双方None）",
      (irr_ui is None and mg["project_irr_pct"] is None) or (irr_ui is not None and abs(mg["project_irr_pct"] - float(irr_ui.group(1))) < 0.006))
check("D: MG: 風力の調達費用が返る", mg.get("wind_procurement_cost_yen_per_year") is not None
      and abs(mg["wind_procurement_cost_yen_per_year"] - num(ud[4], "風力の調達費用:")) < 1.5)
me = mcp_sim(wind={"coverage_pct": 60.0}, mg_enabled=True, business_model="lease")
ue = ui_sim(uw(cov=60.0), mg_enabled=True, business_model="リース")
check("D2: MG+リース+風力: 需要家年間メリットがUIと一致",
      abs(me["business"]["customer_annual_benefit_yen"] - num(ue[4], "需要家年間メリット:")) < 1.5)
mf = mcp_sim(wind={"coverage_pct": 100.0}, business_model="ppa")
uf = ui_sim(uw(cov=100.0), business_model="PPA")
check("D3: 自家消費型PPA+風力: PPA単価・自家消費量（敷地内分）・需要家メリットがUIと一致",
      abs(mf["business"]["required_ppa_price_yen_per_kwh"] - num(uf[4], "必要PPA単価:")) < 0.006
      and abs(mf["business"]["annual_self_consumption_kwh"] - num(uf[4], "年間自家消費量:")) < 1.5
      and abs(mf["business"]["customer_annual_benefit_yen"] - num(uf[4], "需要家年間メリット:")) < 1.5)

# E. データセンター + 風力（北海道）
me_dc = m.simulate_dc(station_no="14163", faces=[{"ppeak_kw": 3000.0, "tilt_deg": 30.0, "azimuth_deg": 180.0}],
                      wind={"coverage_pct": 50.0})
p = me_dc.get("assumptions", {})
dem, _info = app.resolve_dc_demand(m._dc_args_from_params(p["dc"])) if p else (None, None)
if p:
    ue_dc = app.run_simulation(**{**UI_BASE, "station_choice": "14163 (SAPPORO)",
                                  "face_args": face_args([(3000.0, "南", 180.0, 30, 0)]),
                                  "demand_source": app.DEMAND_SOURCE_DATACENTER,
                                  "dc_args": m._dc_args_from_params(p["dc"]), "sell_price": 19.0},
                          wind_args=uw(cov=50.0))
    check("E: DC+風力: 完了し、メリット・配達量がUIと一致（北海道エリア）",
          "error" not in me_dc and me_dc["wind"]["area"] == "北海道"
          and abs(me_dc["electricity_cost"]["annual_economic_merit_yen"]
                  - num(ue_dc[4], "年間経済メリット:", after="【風力込みの年間経済メリット】")) < 1.5
          and me_dc["wind"]["delivered_kwh"] == round(ue_dc[6]["wind_info"]["delivered_kwh"]),
          str(me_dc.get("error") or me_dc.get("errors"))[:80])
else:
    check("E: DC+風力: 完了する", False, str(me_dc)[:100])
check("E: DC+風力: 契約種別が特別高圧なら託送は北海道・特高の1.02円/kWh",
      m.simulate_dc(station_no="14163", contract_type="extra_high_voltage", wind={"coverage_pct": 50.0})
      ["wind"]["cost"]["wheeling_yen_per_kwh"] == 1.02)

# ============================================================
print("\n【2. 入力検証】")
v = m.validate_industrial_params(station_no="34392", wind={"capacity_kw": 500.0})
check("正常: valid で、既定値が解決されて normalized_params に入る（仙台・高圧）",
      v["valid"] and v["normalized_params"]["wind"] == {
          "capacity_kw": 500.0, "cf_pct": 29.1, "ppa_price_yen_per_kwh": 11.96, "wheeling_yen_per_kwh": 2.15,
          "retail_fee_yen_per_kwh": 3.0, "area": "東北"}, str(v.get("normalized_params", {}).get("wind")))
vs = m.validate_industrial_params(station_no="14163", contract_type="extra_high_voltage", wind={"coverage_pct": 80.0})
check("札幌・特高: 託送1.02・手数料1.5・エリア北海道",
      vs["valid"] and vs["normalized_params"]["wind"]["wheeling_yen_per_kwh"] == 1.02
      and vs["normalized_params"]["wind"]["retail_fee_yen_per_kwh"] == 1.5 and vs["normalized_params"]["wind"]["area"] == "北海道")
check("wind を省略すると normalized_params に wind / pv_enabled のキーがない（従来と同じ）",
      "wind" not in m.validate_industrial_params()["normalized_params"] and "pv_enabled" not in m.validate_industrial_params()["normalized_params"])
bad_cases = [
    ("未知のキー", dict(wind={"capacity_kw": 500.0, "capacty": 1})),
    ("容量とカバー率の両方", dict(wind={"capacity_kw": 500.0, "coverage_pct": 50.0})),
    ("容量もカバー率もない", dict(wind={"cf_pct": 25.0})),
    ("容量が0", dict(wind={"capacity_kw": 0})),
    ("容量が文字列", dict(wind={"capacity_kw": "abc"})),
    ("設備利用率が101", dict(wind={"capacity_kw": 500.0, "cf_pct": 101})),
    ("PPA単価が負", dict(wind={"capacity_kw": 500.0, "ppa_price_yen_per_kwh": -1})),
    ("託送がNaN", dict(wind={"capacity_kw": 500.0, "wheeling_yen_per_kwh": float("nan")})),
    ("wind が辞書でない", dict(wind=[1000])),
    ("対象外の地点（東京）", dict(station_no="44132", wind={"capacity_kw": 500.0})),
    ("太陽光も風力もなし", dict(pv_enabled=False)),
]
for label, kw in bad_cases:
    r = m.validate_industrial_params(**kw)
    check(f"不正: {label} → valid=false", r["valid"] is False and len(r["errors"]) >= 1, str(r.get("errors"))[:70])
r = m.simulate_industrial_pv(station_no="44132", wind={"capacity_kw": 500.0})
check("simulate: 対象外の地点は error（計算しない）", "error" in r and "北海道・東北" in str(r.get("errors")))
r = m.validate_industrial_params(pv_enabled=False, faces=[], wind={"capacity_kw": 500.0}, station_no="34392")
check("pv_enabled=false なら faces が空でも valid（風力のみ）", r["valid"] and r["normalized_params"]["faces"] == [])
r = m.validate_dc_params(station_no="14163", wind={"coverage_pct": 50.0}, grid_cap="hv_under_2000kw")
check("DC: 風力と系統受電上限の併用は valid=false", r["valid"] is False and any("受電上限" in e for e in r["errors"]), str(r.get("errors"))[:80])
r = m.validate_dc_params(station_no="14163", wind={"coverage_pct": 50.0}, grid_cap="none")
check("DC: 受電上限なしなら valid", r["valid"], str(r.get("errors")))
r = m.simulate_dc(station_no="14163", wind={"coverage_pct": 50.0}, grid_cap="hv_under_2000kw")
check("simulate_dc: 併用は error", "error" in r and "受電上限" in str(r))

# ============================================================
print("\n【3. list_wind_areas / estimate_wind_generation】")
L = m.list_wind_areas()
check("list_wind_areas: 北海道・東北の2エリア", [a["code"] for a in L["areas"]] == ["01", "02"] and [a["name"] for a in L["areas"]] == ["北海道", "東北"])
st_all = {s["station_no"] for a in L["areas"] for s in a["stations"]}
check("対象地点は8つ（札幌＋東北6県＋新潟）", len(st_all) == 8 and "54232" in st_all and "14163" in st_all, str(sorted(st_all)))
check("各地点に名前がある", all(s["name"] for a in L["areas"] for s in a["stations"]))
days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
for a in L["areas"]:
    wm = sum(d * s for d, s in zip(days, a["monthly_shape"])) / 365
    check(f"{a['name']}: 月別の形状の日数加重平均 = 1.0、時間帯別の平均 = 1.0",
          abs(wm - 1.0) < 0.002 and abs(np.mean(a["diurnal_shape"]) - 1.0) < 0.002, f"{wm:.4f}/{np.mean(a['diurnal_shape']):.4f}")
tohoku = L["areas"][1]
check("東北: 冬に強く夏に弱い（2月 > 4×7月）・託送は高圧2.15／特高0.97",
      tohoku["monthly_shape"][1] > 4 * tohoku["monthly_shape"][6]
      and tohoku["wheeling_yen_per_kwh"] == {"high_voltage": 2.15, "extra_high_voltage": 0.97})
check("北海道の託送は高圧2.28／特高1.02", L["areas"][0]["wheeling_yen_per_kwh"] == {"high_voltage": 2.28, "extra_high_voltage": 1.02})
check("既定値: 設備利用率29.1・PPA単価11.96・手数料 高圧3.0／特高1.5",
      L["defaults"]["cf_pct"] == 29.1 and L["defaults"]["ppa_price_yen_per_kwh"] == 11.96
      and L["defaults"]["retail_fee_yen_per_kwh"] == {"high_voltage": 3.0, "extra_high_voltage": 1.5})
check("出典が併記されている", all(k in L["defaults"] for k in ("cf_source", "ppa_price_source", "retail_fee_source", "wheeling_source")))

E = m.estimate_wind_generation(station_no="34392", capacity_kw=10000.0, cf_pct=24.6)
check("estimate: 東北 10,000kW・CF24.6% → 年間 21,549,600 kWh（= 容量×利用率×8760h）",
      E["annual_kwh"] == round(10000 * 0.246 * 8760) and E["area"] == "東北", str(E.get("annual_kwh")))
check("estimate: 月別は12個で合計 ≒ 年間・2月 > 4×7月", len(E["monthly_kwh"]) == 12 and abs(sum(E["monthly_kwh"]) - E["annual_kwh"]) <= 12
      and E["monthly_kwh"][1] > 4 * E["monthly_kwh"][6])
check("estimate: クリップなし（CF24.6%）", E["clipped_kwh"] == 0 and E["effective_cf_pct"] == 24.6)
E2 = m.estimate_wind_generation(station_no="34392", capacity_kw=10000.0, cf_pct=35.8)
check("estimate: 東北 CF35.8% は定格で頭打ち（クリップあり・実効利用率が下がる）", E2["clipped_kwh"] > 0 and E2["effective_cf_pct"] < 35.8)
check("estimate: 既定の設備利用率は29.1", m.estimate_wind_generation("14163", 1000.0)["cf_pct"] == 29.1)
for label, kw in (("対象外の地点（東京）", dict(station_no="44132")), ("容量が0", dict(capacity_kw=0)), ("容量が文字列", dict(capacity_kw="x")),
                  ("設備利用率が0", dict(cf_pct=0)), ("設備利用率が文字列", dict(cf_pct="x")), ("容量が大きすぎる", dict(capacity_kw=1e7))):
    r = m.estimate_wind_generation(**kw)
    check(f"estimate: {label} → error", "error" in r, str(r)[:60])
check("estimate の値は simulate の風力の発電量と一致（同じ容量・利用率）",
      m.estimate_wind_generation("34392", 500.0)["annual_kwh"] == mcp_sim(wind={"capacity_kw": 500.0})["wind"]["generation_kwh"])

# ============================================================
print("\n【4. 出力がJSON標準の型だけ】")
for label, o in (("A(産業用+風力)", ma), ("B(LP)", mb), ("C(風力のみ)", mc), ("D(MG+PPA)", md), ("D2(MGリース)", me), ("E(DC)", me_dc),
                 ("list_wind_areas", L), ("estimate_wind_generation", E), ("validate(wind)", v)):
    bad = non_json(o)
    check(f"{label}: 標準の型だけ", not bad, str(bad[:3]))

# ============================================================
print("\n【5. gr.api への登録】")
names = {getattr(f, "api_name", None) for f in app.demo.fns.values()}
check("list_wind_areas と estimate_wind_generation が公開されている", {"list_wind_areas", "estimate_wind_generation"} <= names, str(sorted(n for n in names if n)[:12]))

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
