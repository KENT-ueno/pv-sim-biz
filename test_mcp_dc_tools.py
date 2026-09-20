# -*- coding: utf-8 -*-
"""
test_mcp_dc_tools.py - データセンターMCPツール（Phase 7 段階4）の検証
====================================================================
対象: estimate_dc_demand / validate_dc_params / simulate_dc（直接呼び出し）
  1. 簡易キー→日本語ラベルの対応が app.py の定義と一致していること
  2. estimate_dc_demand: 需要の要約が app.resolve_dc_demand と一致、入力検証
  3. validate_dc_params: 正常系・異常系・警告
  4. simulate_dc: UI（run_simulation）と数値が一致、受電上限の各状態、リース、MG非対応
  5. 産業用ツールの出力に受電上限・DCの節が混ざらないこと

実行: python test_mcp_dc_tools.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np

import app  # noqa: build_ui() と gr.api 登録込み（サーバー起動はしない）
import mcp_tools

n_pass = 0
n_fail = 0


def check(label, cond, detail=""):
    global n_pass, n_fail
    if cond:
        n_pass += 1
    else:
        n_fail += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


print("=" * 70)
print("test_mcp_dc_tools.py — データセンターMCPツール")
print("=" * 70)

# ------------------------------------------------------------
print("\n【1. 簡易キー→ラベルの対応】")
m = mcp_tools._dc_maps(app)
check("用途ラベルが DC_WORKLOAD_PRESETS のキーと一致", set(m["workload"].values()) == set(app.DC_WORKLOAD_PRESETS.keys()))
check("規模ラベルが DC_SIZE_PRESETS のキーと一致", set(m["size_preset"].values()) == set(app.DC_SIZE_PRESETS.keys()))
check("容量指定ラベルが CAPACITY_MODES と一致", set(m["capacity_mode"].values()) == set(app.CAPACITY_MODES))
check("プロファイルラベルが IT_LOAD_PROFILE_MODES と一致", set(m["profile"].values()) == set(app.IT_LOAD_PROFILE_MODES))
check("ノイズラベルが NOISE_LEVELS と一致", set(m["noise_level"].values()) == set(app.NOISE_LEVELS))
check("受電上限ラベルが GRID_CAP_MODES と一致", set(m["grid_cap"].values()) == set(app.GRID_CAP_MODES))
for name in ("estimate_dc_demand", "validate_dc_params", "simulate_dc"):
    check(f"{name} が gr.api で登録されている（app.py の登録行）",
          f'gr.api(mcp_tools.{name}, api_name="{name}")' in open(
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py"), encoding="utf-8").read())

# ------------------------------------------------------------
print("\n【2. estimate_dc_demand】")
e = mcp_tools.estimate_dc_demand()
check("既定（中規模・ハウジング・PUE1.4）でエラーなし", "error" not in e, e.get("error"))
dcs = e["datacenter"]
check("中規模=IT 5,000kW", dcs["it_capacity_kw"] == 5000.0)
check("年間IT電力量 = 5,000kW×80%×8,760h = 35,040,000kWh", dcs["annual_it_kwh"] == 35_040_000, str(dcs["annual_it_kwh"]))
check("年間施設電力量 = IT×PUE1.4 = 49,056,000kWh（CEC形状は年平均1.0に正規化）",
      dcs["annual_facility_kwh"] == 49_056_000, str(dcs["annual_facility_kwh"]))
check("実効平均PUE = 1.4", dcs["effective_avg_pue"] == 1.4)
check("月別電力量の合計 = 年間施設電力量（±12kWh: 丸め）",
      abs(sum(e["monthly_facility_kwh"].values()) - dcs["annual_facility_kwh"]) <= 12)
check("1日の平均需要カーブは48点", len(e["average_daily_profile_kw"]) == 48)
check("48点の平均 ≈ 施設平均電力（年間kWh÷8,760h）",
      abs(np.mean(e["average_daily_profile_kw"]) - dcs["annual_facility_kwh"] / 8760.0) < 0.5)
dem_ref, info_ref = app.resolve_dc_demand(mcp_tools._dc_args_from_params(
    mcp_tools._normalize_dc_params("housing", "size_preset", "medium", 1000.0, 100, 10.0, "cec", "low",
                                   80.0, 90.0, 50.0, 14, 1.40, "none", None)[0]))
check("app.resolve_dc_demand と導入前ピークが一致", dcs["peak_demand_kw"] == round(info_ref["peak_demand_kw"], 1))
check("受電電圧区分の目安（ピーク6,418kW → 特別高圧30,000V）", "30,000V" in dcs["voltage_class_hint"], dcs["voltage_class_hint"])
check("申請受電容量の目安 = ピーク÷0.67", dcs["suggested_application_capacity_kw"] == round(info_ref["peak_demand_kw"] / 0.67))
check("規模プリセットの暫定値が警告に出る", any("暫定値" in w for w in e["warnings"]))
check("JSONシリアライズ可能", bool(json.dumps(e, ensure_ascii=False)))
check("同じ入力なら同じ結果（ノイズ固定シード）", mcp_tools.estimate_dc_demand() == e)

e_rack = mcp_tools.estimate_dc_demand(capacity_mode="rack_density", n_racks=100, kw_per_rack=10.0)
e_direct = mcp_tools.estimate_dc_demand(capacity_mode="it_capacity", it_capacity_kw=1000.0)
check("ラック数×密度（100×10kW）= IT容量直接（1,000kW）と同じ需要",
      e_rack["datacenter"] == e_direct["datacenter"] and e_direct["datacenter"]["it_capacity_kw"] == 1000.0)
check("ラック指定時は規模プリセットの暫定値警告が出ない", not any("暫定値" in w for w in e_rack["warnings"]))
e_ai = mcp_tools.estimate_dc_demand(workload="ai_training", capacity_mode="it_capacity", it_capacity_kw=1000.0, it_load_factor_pct=95.0)
check("AI学習=定常プロファイル・年間IT電力量が水準どおり",
      e_ai["datacenter"]["load_profile"] == "flat" and e_ai["datacenter"]["annual_it_kwh"] == round(1000 * 0.95 * 8760))
e_srv = mcp_tools.estimate_dc_demand(workload="in_house_server_room", capacity_mode="it_capacity", it_capacity_kw=1000.0)
check("自社サーバー室=日変動プロファイル", e_srv["datacenter"]["load_profile"] == "diurnal")
e_man = mcp_tools.estimate_dc_demand(workload="manual", profile="flat", noise_level="none",
                                     capacity_mode="it_capacity", it_capacity_kw=1000.0)
prof = np.array(e_man["average_daily_profile_kw"])
check("manual+flat+ノイズなし = 完全に平坦な需要", prof.max() - prof.min() < 0.2 and e_man["datacenter"]["it_load_kw"]["min"] == e_man["datacenter"]["it_load_kw"]["max"])

for label, kw, key in [
    ("不明な用途", dict(workload="warehouse"), "workload"),
    ("不明な容量指定", dict(capacity_mode="floor_area"), "capacity_mode"),
    ("不明な規模", dict(size_preset="giga"), "size_preset"),
    ("IT容量0", dict(capacity_mode="it_capacity", it_capacity_kw=0), "it_capacity_kw"),
    ("IT容量が上限超", dict(capacity_mode="it_capacity", it_capacity_kw=1e9), "it_capacity_kw"),
    ("ラック数0", dict(capacity_mode="rack_density", n_racks=0), "n_racks"),
    ("ラック密度が負", dict(capacity_mode="rack_density", kw_per_rack=-1), "kw_per_rack"),
    ("PUE 0.9（1.0未満）", dict(pue=0.9), "pue"),
    ("IT負荷率0%", dict(it_load_factor_pct=0), "it_load_factor_pct"),
    ("IT負荷率101%", dict(it_load_factor_pct=101), "it_load_factor_pct"),
    ("manualで不明なプロファイル", dict(workload="manual", profile="sine"), "profile"),
    ("manualで不明なノイズ", dict(workload="manual", noise_level="extreme"), "noise_level"),
    ("日変動でボトム>ピーク", dict(workload="in_house_server_room", it_peak_pct=60, it_bottom_pct=80), "it_bottom_pct"),
    ("日変動でピーク時刻24", dict(workload="in_house_server_room", it_peak_hour=24), "it_peak_hour"),
    ("数値でない容量", dict(capacity_mode="it_capacity", it_capacity_kw="abc"), "it_capacity_kw"),
]:
    r = mcp_tools.estimate_dc_demand(**kw)
    check(f"異常系: {label} → errors に {key}", "errors" in r and any(key in x for x in r["errors"]), str(r.get("errors")))
_v_err = mcp_tools.validate_dc_params(workload="warehouse", pue=0.5)
check("エラーがあるときは規模プリセットの暫定値の警告を出さない（直す点だけを伝える）",
      not _v_err["valid"] and not any("暫定値" in w for w in _v_err["warnings"]), str(_v_err["warnings"]))
check("エラーがなければ暫定値の警告は出る（規模プリセット指定時）",
      any("暫定値" in w for w in mcp_tools.validate_dc_params()["warnings"]))
check("複数の不備は全て報告される", len(mcp_tools.estimate_dc_demand(pue=0.5, it_load_factor_pct=0, workload="x").get("errors", [])) >= 3)
check("PUE 2.5 は警告（エラーではない）", "error" not in mcp_tools.estimate_dc_demand(pue=2.5)
      and any("PUE" in w for w in mcp_tools.estimate_dc_demand(pue=2.5)["warnings"]))

# ------------------------------------------------------------
print("\n【3. validate_dc_params】")
v = mcp_tools.validate_dc_params()
check("既定で valid", v["valid"], str(v.get("errors")))
p = v["normalized_params"]
check("需要ソースが datacenter", p["demand_source"] == "datacenter")
check("facilities は不要（空）で検証が通る", p["facilities"] == [])
check("MGは無効", p["mg_enabled"] is False)
check("正規化されたDC入力: IT容量が解決済み（5,000kW）", p["dc"]["it_capacity_kw"] == 5000.0)
check("用途プリセット指定時は profile/noise を使わない（None）", p["dc"]["profile"] is None and p["dc"]["noise_level"] is None)
check("解決後のプロファイルが入る", p["dc"]["resolved_profile"] == app.PROFILE_CEC)
check("受電上限なし: grid_cap_kw=None", p["dc"]["grid_cap"] == "none" and p["dc"]["grid_cap_kw"] is None)
check("契約種別の食い違い警告（高圧でピーク6,418kW）", any("extra_high_voltage が目安" in w for w in v["warnings"]))
check("next_step が simulate_dc を案内", "simulate_dc" in v["next_step"])
check("JSONシリアライズ可能", bool(json.dumps(v, ensure_ascii=False)))

v_ehv = mcp_tools.validate_dc_params(capacity_mode="it_capacity", it_capacity_kw=1000.0, contract_type="extra_high_voltage")
check("特別高圧でピーク2,000kW未満 → 高圧が目安の警告", any("high_voltage が目安" in w for w in v_ehv["warnings"]))
v_ok = mcp_tools.validate_dc_params(capacity_mode="it_capacity", it_capacity_kw=1000.0, contract_type="high_voltage")
check("目安と一致すれば契約種別の警告なし", not any("が目安" in w for w in v_ok["warnings"]))

v_hv = mcp_tools.validate_dc_params(grid_cap="hv_under_2000kw")
check("プリセット: 高圧に収める = 1,999kW", v_hv["normalized_params"]["dc"]["grid_cap_kw"] == 1999.0)
v_ehv33 = mcp_tools.validate_dc_params(grid_cap="ehv_under_10000kw")
check("プリセット: 22・33kVに収める = 9,999kW", v_ehv33["normalized_params"]["dc"]["grid_cap_kw"] == 9999.0)
check("蓄電池なしで上限が拘束 → 強制の条件（LPのみ）の警告", any("lp_optimized のときだけ強制" in w for w in v_hv["warnings"]))
v_lp = mcp_tools.validate_dc_params(grid_cap="hv_under_2000kw", battery_enabled=True, battery_mode="lp_optimized",
                                    battery_capacity_kwh=2000.0, battery_max_charge_kw=1000.0, battery_max_discharge_kw=1000.0)
check("LP+上限: 実行時間の目安に受電上限が出る", "受電上限" in v_lp["estimated_runtime_seconds"], v_lp["estimated_runtime_seconds"])
check("LP+上限: 時間がかかりうる旨の警告", any("数十秒〜2分" in w for w in v_lp["warnings"]))
check("LP+上限: 強制の条件（LPのみ）の警告は出ない", not any("lp_optimized のときだけ強制" in w for w in v_lp["warnings"]))
v_loose = mcp_tools.validate_dc_params(capacity_mode="it_capacity", it_capacity_kw=1000.0, grid_cap="ehv_under_10000kw")
check("上限がピーク以上 → 拘束しない旨の警告", any("拘束しません" in w for w in v_loose["warnings"]))
check("拘束しないときは強制条件の警告を重ねない", not any("lp_optimized のときだけ強制" in w for w in v_loose["warnings"]))
v_man = mcp_tools.validate_dc_params(grid_cap="manual", grid_cap_kw=3500.0)
check("手入力: grid_cap_kw がそのまま入る", v_man["normalized_params"]["dc"]["grid_cap_kw"] == 3500.0)
check("手入力で grid_cap_kw 未指定 → エラー", not mcp_tools.validate_dc_params(grid_cap="manual", grid_cap_kw=None)["valid"])
check("手入力で grid_cap_kw が負 → エラー", not mcp_tools.validate_dc_params(grid_cap="manual", grid_cap_kw=-5)["valid"])
check("不明な grid_cap → エラー", any("grid_cap" in x for x in mcp_tools.validate_dc_params(grid_cap="66kv")["errors"]))
check("プリセット指定時は grid_cap_kw を無視する", mcp_tools.validate_dc_params(grid_cap="hv_under_2000kw", grid_cap_kw=5.0)["normalized_params"]["dc"]["grid_cap_kw"] == 1999.0)

v_bad = mcp_tools.validate_dc_params(faces=[], pue=0.5, grid_cap="xx", battery_enabled=True, battery_capacity_kwh=-1)
check("DC・PV・蓄電池・上限の不備を全て報告", not v_bad["valid"] and len(v_bad["errors"]) >= 4, str(v_bad["errors"]))
check("不明な station_no → エラー", not mcp_tools.validate_dc_params(station_no="00000")["valid"])
check("business_model=lease の目標IRRが範囲外 → エラー", not mcp_tools.validate_dc_params(business_model="lease", target_irr_pct=90)["valid"])

# ------------------------------------------------------------
print("\n【4. simulate_dc】")
COMMON_TARIFF = dict(basic_charge_yen_per_kw=1890.0, energy_charge_summer_yen_per_kwh=19.93,
                     energy_charge_other_yen_per_kwh=18.77, power_factor_pct=85.0,
                     fuel_adjustment_yen_per_kwh=0.0, renewable_surcharge_yen_per_kwh=4.18)
S = dict(station_no="14163", faces=[{"ppeak_kw": 1000.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 0}],
         workload="manual", capacity_mode="it_capacity", it_capacity_kw=1000.0, it_load_factor_pct=80.0, pue=1.40,
         profile="cec", noise_level="none", **COMMON_TARIFF)
LPB = dict(battery_enabled=True, battery_mode="lp_optimized", battery_capacity_kwh=2000.0,
           battery_max_charge_kw=1000.0, battery_max_discharge_kw=1000.0, battery_soc_min_pct=10.0, battery_soc_max_pct=90.0)

# --- UI（run_simulation）との数値一致 ---
FAC = tuple(["役所・自治体庁舎", 4500, 1] + ["なし", 0, 0] * (app.MAX_FACILITIES - 1))
FACE = tuple([1000.0, "南", 180.0, 30, 0] + [0, "南", "", 30, 0] * (app.MAX_FACES - 1))
UI_BASE = dict(
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
UI_DC = dict(workload_preset="手動設定", capacity_mode="IT容量を直接入力", it_capacity_kw=1000.0,
             it_load_factor_pct=80.0, pue_const=1.40, noise_level="なし", profile_mode=app.PROFILE_CEC,
             grid_cap_mode="手入力", grid_cap_kw=1190.0)


def ui(dc, **over):
    kw = dict(UI_BASE)
    kw.update(over)
    return app.run_simulation(demand_source="datacenter", dc_args=dc, **kw)


s0 = mcp_tools.simulate_dc(**S)
check("蓄電池なし・上限なし: エラーなし", "error" not in s0, s0.get("error"))
check("datacenter節が付く", "datacenter" in s0 and s0["datacenter"]["it_capacity_kw"] == 1000.0)
check("上限なしでは grid_cap 節が付かない", "grid_cap" not in s0)
check("MG節は None（DCツールはMG非対応）", s0["microgrid"] is None)
check("結果に免責（仮想需要・PUE一定・MG非対応）が入る",
      any("仮想需要" in c for c in s0["caveats"]) and any("PUEは一定値" in c for c in s0["caveats"])
      and any("マイクログリッド" in c for c in s0["caveats"]))
u0 = ui(dict(UI_DC, grid_cap_mode="制限なし"))
sc_ui = u0[6]["sc_result"]
check("UIと年間系統購入量が一致（±1kWh）", abs(s0["annual"]["import_kwh"] - round(sc_ui["annual_import"])) <= 1,
      f"mcp={s0['annual']['import_kwh']} ui={sc_ui['annual_import']:.1f}")
check("UIと年間需要量が一致", s0["annual"]["demand_kwh"] == round(sc_ui["annual_demand"]))
check("UIと導入前ピークが一致（契約電力）", s0["electricity_cost"]["contract_power_before_kw"] == round(float(np.max(u0[6]["demand_30min"])) * 2.0, 1))

# --- 受電上限（LPで強制）---
s1 = mcp_tools.simulate_dc(**S, **LPB, grid_cap="manual", grid_cap_kw=1190.0)
check("LP+上限1,190kW: エラーなし・実行不可能でもない", "error" not in s1 and not s1.get("grid_cap_infeasible"), str(s1.get("error")))
g = s1["grid_cap"]
check("status=enforced・上限を強制", g["status"] == "enforced" and g["enforced"] is True)
check("導入後ピーク ≤ 上限", g["peak_after_kw"] <= 1190.0 + 1e-6 and g["within_cap_after"] is True, str(g["peak_after_kw"]))
check("導入前ピークが上限超過だったことを示す（1,194kW）", g["within_cap_before"] is False and g["peak_before_kw"] > 1190.0)
u1 = ui(UI_DC, **dict(bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=2000.0, bat_max_charge=1000.0, bat_max_discharge=1000.0))
ui_peak = float(np.max(u1[6]["sc_result"]["import_"])) * 2.0
check("UIと導入後ピークが一致（LPは決定論的）", abs(g["peak_after_kw"] - ui_peak) < 0.06, f"mcp={g['peak_after_kw']} ui={ui_peak:.3f}")
check("UIと年間系統購入量が一致（±1kWh）", abs(s1["annual"]["import_kwh"] - round(u1[6]["sc_result"]["annual_import"])) <= 1)
check("契約電力（導入後）が上限以下", s1["electricity_cost"]["contract_power_after_kw"] <= 1190.0 + 1e-6)
check("JSONシリアライズ可能", bool(json.dumps(s1, ensure_ascii=False)))

# --- 守れない（診断で確定）---
s2 = mcp_tools.simulate_dc(**dict(S, profile="flat"), **LPB, grid_cap="manual", grid_cap_kw=1000.0)
check("基底負荷が上限超: grid_cap_infeasible=True（エラーではない）", s2.get("grid_cap_infeasible") is True and "error" not in s2)
check("status=infeasible・LPは実行していない", s2["grid_cap"]["status"] == "infeasible" and "実行していない" in s2["reason"])
check("違反に energy が含まれる", "energy" in s2["grid_cap"]["violations"])
check("蓄電池が無限に大きくても必要な上限の下限（約1,012kW）", 1011 <= s2["grid_cap"]["min_cap_kw_if_battery_unlimited"] <= 1014,
      str(s2["grid_cap"]["min_cap_kw_if_battery_unlimited"]))
check("経済性の節を含まない", "annual" not in s2 and "investment" not in s2)
check("UIと同じ説明文（message）を返す", "受電上限を守れません" in s2["message"] and "蓄電池を大きくしても解決しません" in s2["message"])
check("需要シフトを案内しない", "シフト" not in s2["message"])
check("次の一手（next_step）を案内", "上限を上げる" in s2["next_step"])
check("datacenter節と assumptions は付く", "datacenter" in s2 and "assumptions" in s2)

s3 = mcp_tools.simulate_dc(**S, **dict(LPB, battery_capacity_kwh=1.0, battery_max_charge_kw=0.5, battery_max_discharge_kw=0.5),
                           grid_cap="manual", grid_cap_kw=1190.0)
check("蓄電池が小さすぎる: 出力・容量不足を診断", s3.get("grid_cap_infeasible") is True
      and {"power", "capacity"} <= set(s3["grid_cap"]["violations"]), str(s3["grid_cap"]["violations"]))
rb = s3["grid_cap"]["required_battery_lower_bound"]
check("必要な蓄電池の下限の目安（放電出力・使用可能容量・公称容量）が出る",
      rb is not None and rb["discharge_power_kw"] > 0 and rb["usable_capacity_kwh"] > 0
      and rb["nominal_capacity_kwh"] >= rb["usable_capacity_kwh"], str(rb))
check("下限の目安である旨の注意書きが付く", "下限の目安" in rb["note"])

# --- LPが実行不可能（診断の必要条件は満たす）: LPを差し替えて経路を確認 ---
orig_opt = app.optimize_battery


def _raise(*a, **k):
    raise app.GridCapInfeasibleError("test")


app.optimize_battery = _raise
try:
    s4 = mcp_tools.simulate_dc(**S, **LPB, grid_cap="manual", grid_cap_kw=1190.0)
finally:
    app.optimize_battery = orig_opt
check("LP実行不可能: infeasible_lp として診断を返す", s4.get("grid_cap_infeasible") is True and s4["grid_cap"]["status"] == "infeasible_lp")
check("充電レート不足を案内", "充電レート不足" in s4["message"] and "LPが実行不可能" in s4["reason"])
check("診断の必要条件は満たしている（違反なし）", s4["grid_cap"]["violations"] == [])

# --- 強制しない（蓄電池なし／ルールベース）---
s5 = mcp_tools.simulate_dc(**S, grid_cap="manual", grid_cap_kw=1190.0)
check("蓄電池なし: status=not_enforced・計算は実行される", s5["grid_cap"]["status"] == "not_enforced" and "annual" in s5)
check("導入後ピークが上限を超過と判定（約4kW）", s5["grid_cap"]["within_cap_after"] is False
      and 3.0 < s5["grid_cap"]["peak_after_kw"] - 1190.0 < 5.0, str(s5["grid_cap"]["peak_after_kw"]))
u5 = ui(UI_DC)
check("UIと導入後ピークが一致", abs(s5["grid_cap"]["peak_after_kw"] - float(np.max(u5[6]["sc_result"]["import_"])) * 2.0) < 0.06)
s6 = mcp_tools.simulate_dc(**S, **dict(LPB, battery_mode="rule_based"), grid_cap="manual", grid_cap_kw=1190.0)
check("ルールベース: status=not_enforced", s6["grid_cap"]["status"] == "not_enforced" and s6["grid_cap"]["enforced"] is False)

s7 = mcp_tools.simulate_dc(**dict(S, capacity_mode="size_preset", size_preset="small"), grid_cap="ehv_under_10000kw")
check("上限がピーク以上: 蓄電池なしでも守れる（needs_battery=False）", s7["grid_cap"]["needs_battery"] is False
      and s7["grid_cap"]["within_cap_after"] is True)

# --- 平坦需要では蓄電池の価値ゼロ（設計書 §11）---
s8 = mcp_tools.simulate_dc(**dict(S, profile="flat"), **dict(LPB, battery_capacity_kwh=500.0, battery_max_charge_kw=250.0, battery_max_discharge_kw=250.0))
check("平坦需要+LP: 充放電ゼロ・その理由の免責が付く",
      s8["annual"]["battery_charge_kwh"] == 0 and any("価値は構造的にゼロ" in c for c in s8["caveats"]))

# --- 事業モデル・入力不備 ---
s9 = mcp_tools.simulate_dc(**S, business_model="lease", contract_years=15, target_irr_pct=10.0)
check("リース: 逆算リース料が出る", s9["business"].get("required_lease_yen_per_year", 0) > 0)
check("不正な入力は error/errors を返す（実行しない）", "errors" in mcp_tools.simulate_dc(pue=0.5) and "annual" not in mcp_tools.simulate_dc(pue=0.5))

# ------------------------------------------------------------
print("\n【5. 産業用ツールへの影響なし】")
ind = mcp_tools.simulate_industrial_pv(
    station_no="44132",
    faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities=[{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}])
check("産業用: エラーなし", "error" not in ind, ind.get("error"))
check("産業用の出力に grid_cap / datacenter の節が混ざらない", "grid_cap" not in ind and "datacenter" not in ind)
check("産業用の出力のキー構成が従来どおり",
      list(ind.keys()) == ["assumptions", "annual", "electricity_cost", "investment", "business",
                           "microgrid", "caveats", "validation_warnings"], str(list(ind.keys())))
def _non_json(o, path=""):
    """出力にJSON標準以外の型（numpy・辞書キーを含む）が混ざっていないか。
    混ざると、MCP経由で数値が文字列になる（例: numpy.float64 → "11.5"）。"""
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if type(k) not in (str, int, float, bool):   # json.dumps が受け付ける辞書キー（numpyのキーは不可）
                out.append(f"{path}/<key {k!r}: {type(k).__name__}>")
            out += _non_json(v, f"{path}/{k}")
    elif type(o) is list:
        for i, v in enumerate(o):
            out += _non_json(v, f"{path}[{i}]")
    elif o is None or type(o) in (str, int, float, bool):
        pass
    else:
        out.append(f"{path}: {type(o).__name__}")
    return out


# _jsonable 自体の単体検査（値は変えず、型だけをJSON標準にする）
_src = {"f": np.float64(11.5), "i": np.int64(3), "b": np.bool_(True), "a": np.array([1.5, 2.5]),
        "t": (np.float32(1.0), 2), "nest": {np.int64(7): [np.float64(0.25)]}, "n": None, "s": "x"}
_out = mcp_tools._jsonable(_src)
check("_jsonable: numpy のスカラー・配列・辞書キー・タプルをJSON標準の型に変換", _non_json(_out) == [], str(_non_json(_out)))
check("_jsonable: 値は変わらない", _out["f"] == 11.5 and _out["i"] == 3 and _out["b"] is True and _out["a"] == [1.5, 2.5]
      and _out["t"] == [1.0, 2] and _out["nest"] == {7: [0.25]} and _out["n"] is None and _out["s"] == "x")
check("_jsonable: NaN/inf はそのまま通す（値は変えない）", np.isnan(mcp_tools._jsonable(np.float64("nan"))) and mcp_tools._jsonable(float("inf")) == float("inf"))
check("MCP出力がJSON標準の型だけ（産業用）", _non_json(ind) == [], str(_non_json(ind)))
check("MCP出力がJSON標準の型だけ（DC: 蓄電池なし・LP+上限・診断・需要見積り）",
      _non_json(s0) == [] and _non_json(s1) == [] and _non_json(s2) == [] and _non_json(e) == [],
      str([_non_json(s0), _non_json(s1), _non_json(s2), _non_json(e)]))
check("estimate_pv_generation / list_stations / validate もJSON標準の型だけ",
      _non_json(mcp_tools.estimate_pv_generation(station_no="44132", faces=[{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}])) == []
      and _non_json(mcp_tools.list_stations()) == [] and _non_json(v) == [])
check("公開ツールの成功系の戻り値を _jsonable で返している（app.pyに登録された7ツールの return を機械的に確認）",
      open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_tools.py"), encoding="utf-8").read().count("return _jsonable(") == 8)
check("産業用の validate は facilities 必須のまま",
      not mcp_tools.validate_industrial_params(facilities=[])["valid"])

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(1 if n_fail else 0)
