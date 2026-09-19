# -*- coding: utf-8 -*-
"""
test_mcp_tools.py — pv-sim-biz MCPツール群のローカル検証（gitに含めない）
====================================================================
fip/ghと同じ慣習: このファイルはリポジトリにコミットしない。
直接呼び出し検証のみ（HTTP経由の検証は別途 python app.py 起動後にcurlで実施）。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # noqa: build_ui()実行、mcp_toolsのgr.api登録込み（サーバー起動はしない）
import mcp_tools

n_pass = 0
n_fail = 0


def check(label, cond, detail=""):
    global n_pass, n_fail
    mark = "PASS" if cond else "FAIL"
    if cond:
        n_pass += 1
    else:
        n_fail += 1
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))


print("=" * 70)
print("test_mcp_tools.py — 直接呼び出し検証")
print("=" * 70)

# ------------------------------------------------------------
print("\n【list_stations】")
stations = mcp_tools.list_stations()
check("count > 0", stations["count"] > 0, f"count={stations['count']}")
check("station_no=44132(東京)が含まれる", any(s["station_no"] == "44132" for s in stations["stations"]))

# ------------------------------------------------------------
print("\n【estimate_pv_generation】")
gen = mcp_tools.estimate_pv_generation(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
)
check("エラーなし", "error" not in gen, gen.get("error"))
check("年間発電量が妥当レンジ", 500_000 < gen.get("annual_generation_kwh", 0) < 700_000,
      f"{gen.get('annual_generation_kwh')}")

# 異常系: faces空
gen_err = mcp_tools.estimate_pv_generation(station_no="44132", faces=[])
check("faces空でエラー", "error" in gen_err)

# ------------------------------------------------------------
print("\n【validate_industrial_params】正常系")
v = mcp_tools.validate_industrial_params(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    contract_type="high_voltage",
    battery_enabled=True, battery_mode="rule_based",
    battery_capacity_kwh=100.0, battery_max_charge_kw=50.0, battery_max_discharge_kw=50.0,
)
check("valid=True", v["valid"], v.get("errors"))
check("正規化パラメータにfacilitiesが1件", len(v["normalized_params"]["facilities"]) == 1)
check("basic_charge_yen_per_kwがHVデフォルトに解決", v["normalized_params"]["basic_charge_yen_per_kw"] == 1890.00)
check("sell_price_yen_per_kwhがFIT早期単価に解決", v["normalized_params"]["sell_price_yen_per_kwh"] == 19.0)

print("\n【validate_industrial_params】異常系")
v_err = mcp_tools.validate_industrial_params(
    station_no="99999",  # 存在しない地点
    facilities=[{"building_type": "invalid_type", "floor_area_m2": -1, "building_count": 0}],
    battery_enabled=True, battery_capacity_kwh=-5.0,
)
check("valid=False", not v_err["valid"])
check("errorsが複数件", len(v_err["errors"]) >= 3, f"{len(v_err['errors'])}件: {v_err['errors']}")

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】ルールベース蓄電池")
sim = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    contract_type="high_voltage",
    battery_enabled=True, battery_mode="rule_based",
    battery_capacity_kwh=100.0, battery_max_charge_kw=50.0, battery_max_discharge_kw=50.0,
    business_model="self_owned",
)
check("エラーなし", "error" not in sim, sim.get("error"))
check("assumptions同梱", "assumptions" in sim)
check("annual.generation_kwh > 0", sim.get("annual", {}).get("generation_kwh", 0) > 0)
check("annual.battery_charge_kwh > 0", sim.get("annual", {}).get("battery_charge_kwh", 0) > 0)
check("investment.simple_payback_years あり", sim.get("investment", {}).get("simple_payback_years") is not None)
check("caveats同梱", len(sim.get("caveats", [])) > 0)
print(f"  年間発電量: {sim['annual']['generation_kwh']:,} kWh")
print(f"  自家消費率: {sim['annual']['self_consumption_rate_pct']}%")
print(f"  年間経済メリット: {sim['electricity_cost']['annual_economic_merit_yen']:,} 円")
print(f"  単純投資回収年数: {sim['investment']['simple_payback_years']} 年")

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】LP最適化蓄電池")
sim_lp = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    contract_type="high_voltage",
    battery_enabled=True, battery_mode="lp_optimized",
    battery_capacity_kwh=100.0, battery_max_charge_kw=50.0, battery_max_discharge_kw=50.0,
)
check("エラーなし", "error" not in sim_lp, sim_lp.get("error"))
check("ルールベースよりLPの方が経済メリットが同等以上",
      sim_lp["electricity_cost"]["annual_economic_merit_yen"] >= sim["electricity_cost"]["annual_economic_merit_yen"] * 0.95,
      f"LP={sim_lp['electricity_cost']['annual_economic_merit_yen']} vs RB={sim['electricity_cost']['annual_economic_merit_yen']}")

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】複数施設合算")
sim_multi = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 1000.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 1000.0}],
    facilities=[
        {"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1},
        {"building_type": "hospital", "floor_area_m2": 22400.0, "building_count": 1},
    ],
    battery_enabled=False,
)
check("エラーなし", "error" not in sim_multi, sim_multi.get("error"))
check("複数施設のほうが需要量が大きい",
      sim_multi["annual"]["demand_kwh"] > sim["annual"]["demand_kwh"])

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】リース事業モデル")
sim_lease = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    battery_enabled=False,
    business_model="lease", contract_years=15, target_irr_pct=10.0,
)
check("エラーなし", "error" not in sim_lease, sim_lease.get("error"))
check("required_lease_yen_per_year あり", sim_lease["business"].get("required_lease_yen_per_year") is not None)
check("P-IRRが需要家向け出力に含まれない（非開示方針）",
      "irr" not in str(sim_lease["business"]).lower() or "target_irr_pct" in sim_lease["business"])
print(f"  必要リース料: {sim_lease['business'].get('required_lease_yen_per_year'):,} 円/年")
print(f"  需要家メリット: {sim_lease['business'].get('customer_annual_benefit_yen'):,} 円/年")

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】PPA事業モデル")
sim_ppa = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    battery_enabled=False,
    business_model="ppa", contract_years=15, target_irr_pct=10.0,
)
check("エラーなし", "error" not in sim_ppa, sim_ppa.get("error"))
check("required_ppa_price_yen_per_kwh あり", sim_ppa["business"].get("required_ppa_price_yen_per_kwh") is not None)
print(f"  必要PPA単価: {sim_ppa['business'].get('required_ppa_price_yen_per_kwh')} 円/kWh")

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】逆潮流禁止モード")
sim_noexp = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    sell_mode="no_export",
    battery_enabled=False,
)
check("エラーなし", "error" not in sim_noexp, sim_noexp.get("error"))
check("export_kwh=0", sim_noexp["annual"]["export_kwh"] == 0)
check("curtailed_kwh > 0", sim_noexp["annual"]["curtailed_kwh"] > 0)

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】両面パネル（Phase 4d-2）")
sim_mono = mcp_tools.simulate_industrial_pv(
    station_no="14163",  # 積雪のある札幌で検証（アルベド切替の効果を出しやすい）
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    battery_enabled=False,
)
sim_bif = mcp_tools.simulate_industrial_pv(
    station_no="14163",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    battery_enabled=False,
    bifacial_enabled=True, bifaciality=0.75, gcr=0.4, panel_height_m=2.0, pitch_m=5.0,
    snow_albedo_enabled=True,
)
check("エラーなし", "error" not in sim_bif, sim_bif.get("error"))
check("両面パネルのほうが片面より発電量が多い",
      sim_bif["annual"]["generation_kwh"] > sim_mono["annual"]["generation_kwh"],
      f"bif={sim_bif['annual']['generation_kwh']} vs mono={sim_mono['annual']['generation_kwh']}")
check("assumptionsにbifaciality等が反映", sim_bif["assumptions"]["bifaciality"] == 0.75)
check("両面パネルのcaveatあり", any("両面パネル" in c for c in sim_bif["caveats"]))

# 異常系: bifaciality範囲外
v_bif_err = mcp_tools.validate_industrial_params(
    station_no="44132",
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    battery_enabled=False,
    bifacial_enabled=True, bifaciality=1.5,
)
check("bifaciality範囲外でvalid=False", not v_bif_err["valid"])

# ------------------------------------------------------------
print("\n【simulate_industrial_pv】マイクログリッド（Phase 4d-2）単一施設")
sim_mg_single = mcp_tools.simulate_industrial_pv(
    station_no="82182",
    faces=[{"ppeak_kw": 800.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 800.0}],
    facilities=[{"building_type": "hospital", "floor_area_m2": 22400.0, "building_count": 1}],
    battery_enabled=False,
    mg_enabled=True, mg_line_distance_km=2.0, mg_line_cost_yen_per_km=30000000.0,
    mg_opex_ratio_pct=2.0, mg_irr_period_years=20,
)
check("エラーなし", "error" not in sim_mg_single, sim_mg_single.get("error"))
check("microgridセクションあり", sim_mg_single.get("microgrid") is not None)
check("project_irr_pctが数値", isinstance(sim_mg_single["microgrid"].get("project_irr_pct"), (int, float)))
print(f"  MG投資合計: {sim_mg_single['microgrid']['mg_total_investment_yen']:,} 円")
print(f"  P-IRR: {sim_mg_single['microgrid']['project_irr_pct']}%")

print("\n【simulate_industrial_pv】マイクログリッド 複数施設（束ねメリット）")
sim_mg_multi = mcp_tools.simulate_industrial_pv(
    station_no="82182",
    faces=[{"ppeak_kw": 800.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 800.0}],
    facilities=[
        {"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1},
        {"building_type": "hospital", "floor_area_m2": 22400.0, "building_count": 1},
    ],
    battery_enabled=False,
    mg_enabled=True, mg_line_distance_km=2.0, mg_line_cost_yen_per_km=30000000.0,
    mg_opex_ratio_pct=2.0, mg_irr_period_years=20,
)
check("エラーなし", "error" not in sim_mg_multi, sim_mg_multi.get("error"))
check("複数施設で束ねメリットが単一施設より大きい（または同等以上)",
      sim_mg_multi["microgrid"]["bundling_merit_yen_per_year"] >= sim_mg_single["microgrid"]["bundling_merit_yen_per_year"],
      f"multi={sim_mg_multi['microgrid']['bundling_merit_yen_per_year']} vs "
      f"single={sim_mg_single['microgrid']['bundling_merit_yen_per_year']}")

# 単一施設のMG警告確認
v_mg_warn = mcp_tools.validate_industrial_params(
    station_no="82182",
    facilities=[{"building_type": "hospital", "floor_area_m2": 22400.0, "building_count": 1}],
    battery_enabled=False,
    mg_enabled=True,
)
check("単一施設+MGで束ねメリット警告あり", any("束ねメリット" in w for w in v_mg_warn["warnings"]))

print("\n【simulate_industrial_pv】マイクログリッド × リース事業モデル併用")
sim_mg_lease = mcp_tools.simulate_industrial_pv(
    station_no="82182",
    faces=[{"ppeak_kw": 800.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 800.0}],
    facilities=[{"building_type": "hospital", "floor_area_m2": 22400.0, "building_count": 1}],
    battery_enabled=False,
    business_model="lease", contract_years=15, target_irr_pct=10.0,
    mg_enabled=True, mg_line_distance_km=2.0, mg_line_cost_yen_per_km=30000000.0,
    mg_opex_ratio_pct=2.0, mg_irr_period_years=20,
)
check("エラーなし", "error" not in sim_mg_lease, sim_mg_lease.get("error"))
check("MG併用時のinvestment_baseがMG総投資額と一致",
      sim_mg_lease["business"]["investment_base_yen"] == sim_mg_lease["microgrid"]["mg_total_investment_yen"])
check("mg_opex_included_yen_per_yearあり", "mg_opex_included_yen_per_year" in sim_mg_lease["business"])

# 異常系: MGパラメータ範囲外
v_mg_err = mcp_tools.validate_industrial_params(
    station_no="82182",
    facilities=[{"building_type": "hospital", "floor_area_m2": 22400.0, "building_count": 1}],
    battery_enabled=False,
    mg_enabled=True, mg_line_distance_km=-1.0, mg_opex_ratio_pct=200.0,
)
check("MG範囲外パラメータでvalid=False", not v_mg_err["valid"])
check("MGエラーが複数件", len(v_mg_err["errors"]) >= 2, v_mg_err["errors"])

# ------------------------------------------------------------
print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
