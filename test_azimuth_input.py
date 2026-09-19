# -*- coding: utf-8 -*-
"""
test_azimuth_input.py - 面設定「方位角」欄の解釈の検証
=====================================================
背景: 方位角欄が gr.Number(value=None) だと Gradio 6.26 は未操作でも 0 を送り、
      既定の面1が北向き（方位角0°）で計算されていた。Textbox（空欄=未指定）に変更し、
      run_simulation が空欄→方位（選択）、数値→直接入力、不正値→エラーとして扱う。

実行: python test_azimuth_input.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


def fac_args():
    a = ["役所・自治体庁舎", 4500, 1]
    while len(a) < app.MAX_FACILITIES * 3:
        a += ["なし", 0, 0]
    return tuple(a)


def face_args(azi, ori="南", ppeak=1000.0):
    a = [ppeak, ori, azi, 30, 0]
    while len(a) < app.MAX_FACES * 5:
        a += [0, "南", "", 30, 0]
    return tuple(a)


BASE = dict(
    station_choice="14163 (SAPPORO)", csv_file=None, demand_custom_csv=None,
    KHD=0.97, KPD=0.95, KPM=0.94, KPA=0.97, eta_ino=0.90, alpha_pct=-0.35, delta_t=21.5,
    bat_enabled=False, bat_mode="ルールベース", bat_capacity=5.0, bat_efficiency=95,
    bat_max_charge=2.5, bat_max_discharge=2.5, bat_soc_min=20, bat_soc_max=95,
    elec_basic=1890.0, elec_summer=19.93, elec_other=18.77, elec_pf=85, elec_fuel=0.0, elec_renewable=4.18,
    contract_type="高圧", sell_mode="余剰売電", sell_price=19.0,
    pv_cost_per_kw=158000, bat_cost_per_kwh=200000, substation_cost_per_kva=27500,
    subsidy_enabled=False, subsidy_pv_pct=0, subsidy_bat_pct=0, co2_factor=0.000431,
    business_model="自己所有", contract_years=15, target_irr=10.0,
    mg_enabled=False, mg_line_distance=2.0, mg_line_cost_per_km=30000000, mg_opex_ratio=2.0, mg_irr_period=20,
    bifacial_enabled=False, bifaciality_val=0.75, gcr_val=0.4, height_val=2.0, pitch_val=5.0,
    snow_albedo_enabled=True,
    facility_args=fac_args(), num_facilities=1, num_faces=1, display_month=7, display_day=1,
)


def run(azi, ori="南"):
    kw = dict(BASE)
    kw["face_args"] = face_args(azi, ori)
    return app.run_simulation(**kw)


def annual_and_azimuth(res):
    text, dbg = res[4], res[5]
    gen = float(re.search(r"年間発電量: ([\d.]+) kWh", text).group(1))
    azi = float(re.search(r"\(([\d.]+)°\)", dbg).group(1))
    return gen, azi


print("=" * 70)
print("test_azimuth_input.py — 方位角欄の解釈")
print("=" * 70)

g_south, a_south = annual_and_azimuth(run(180.0))
check("基準: 数値180を直接入力 → 方位角180°", a_south == 180.0, f"{a_south}")

g_empty, a_empty = annual_and_azimuth(run(""))
check("空欄('') + 方位「南」 → 南=180°（UIの既定。以前は0°になっていた）", a_empty == 180.0, f"{a_empty}")
check("空欄の発電量 = 180°直接入力の発電量", abs(g_empty - g_south) < 1e-6, f"{g_empty:.1f} vs {g_south:.1f}")

g_none, a_none = annual_and_azimuth(run(None))
check("None + 方位「南」 → 180°（従来どおり）", a_none == 180.0 and abs(g_none - g_south) < 1e-6)

_, a_ws = annual_and_azimuth(run("   "))
check("空白のみ → 未指定として扱う（180°）", a_ws == 180.0, f"{a_ws}")

_, a_east = annual_and_azimuth(run("", ori="東"))
check("空欄 + 方位「東」 → 東=90°（ドロップダウンが効く）", a_east == 90.0, f"{a_east}")

_, a_str = annual_and_azimuth(run(" 225.5 "))
check("文字列の数値(前後空白あり)を直接入力 → 225.5°（直接入力が方位選択より優先）", a_str == 225.5, f"{a_str}")

g_north, a_north = annual_and_azimuth(run("0"))
check("明示的な0 → 0°=真北（0は正当な入力として尊重される）", a_north == 0.0, f"{a_north}")
check("北向きは南向きより発電量が少ない", g_north < g_south * 0.8, f"北{g_north:.0f} / 南{g_south:.0f}")

_, a_wrap = annual_and_azimuth(run("540"))
check("360超は360で割った余り（540 → 180°）", a_wrap == 180.0, f"{a_wrap}")
_, a_neg = annual_and_azimuth(run("-90"))
check("負値も正規化される（-90 → 270°）", a_neg == 270.0, f"{a_neg}")

print("\n【不正入力はエラーメッセージで返る（例外で落ちない）】")
for bad in ("abc", "180度", "nan", "inf", "1,80"):
    r = run(bad)
    ok = len(r) == 7 and r[4].startswith("エラー: 面1の方位角") and "数値で入力してください" in r[4] and r[0] is None
    check(f"「{bad}」 → 面1の方位角エラー", ok, r[4][:50])

print("\n【UI定義】")
azi_comps = [c for c in app.demo.blocks.values()
             if getattr(c, "label", None) == "方位角 [°]（直接入力優先）"]
check("方位角欄は MAX_FACES 個", len(azi_comps) == app.MAX_FACES, f"{len(azi_comps)}")
check("すべて Textbox（Number だと未操作で0が送られる）",
      all(type(c).__name__ == "Textbox" for c in azi_comps), str({type(c).__name__ for c in azi_comps}))
check("すべて初期値が空文字", all(c.value == "" for c in azi_comps))

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
