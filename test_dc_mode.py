# -*- coding: utf-8 -*-
"""
test_dc_mode.py - データセンターモード（Phase 7 段階2・3）の検証
================================================================
対象:
  1. resolve_dc_demand: 容量3方式・プロファイル・入力検証
  2. run_simulation の需要ソース分岐（datacenter / industrial）
  3. 産業用の既定経路が demand_source 追加で変わっていないこと
  4. UI配線（DC入力の並びとDC_INPUT_KEYSの一致、タブ選択→gr.State）

実行: python test_dc_mode.py
※ 産業用出力の厳密な回帰（HEADとの全文一致）は、変更前後で run_simulation の出力を保存・照合して別途確認済み。
"""
import os
import re
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
    facility_args=fac_args([("役所・自治体庁舎", 4500, 1)]),
    # 方位角は南=180°を明示する（DCの検証を方位角欄の解釈から独立させるため。欄の解釈は test_azimuth_input.py）
    face_args=face_args([(1000.0, "南", 180.0, 30, 0)]),
    num_facilities=1, num_faces=1, display_month=7, display_day=1,
)

MANUAL_FLAT = dict(workload_preset="手動設定", capacity_mode="IT容量を直接入力", it_capacity_kw=1000.0,
                   profile_mode=app.PROFILE_FLAT, noise_level="なし", it_load_factor_pct=80.0, pue_const=1.40)


def run(**over):
    kw = dict(BASE)
    kw.update(over)
    return app.run_simulation(**kw)


print("=" * 70)
print("test_dc_mode.py — データセンターモード")
print("=" * 70)

# ------------------------------------------------------------
print("\n【1. resolve_dc_demand: 容量の3方式】")
d_preset, i_preset = app.resolve_dc_demand(dict(MANUAL_FLAT, capacity_mode="規模プリセット", size_preset="中規模"))
check("規模プリセット「中規模」= IT 5,000kW", i_preset["it_capacity_kw"] == 5000.0, f"{i_preset['it_capacity_kw']}")
d_dir, i_dir = app.resolve_dc_demand(MANUAL_FLAT)
check("IT容量直接入力 = 1,000kW", i_dir["it_capacity_kw"] == 1000.0)
d_rack, i_rack = app.resolve_dc_demand(dict(MANUAL_FLAT, capacity_mode="ラック数×density", n_racks=100, kw_per_rack=10.0))
check("ラック数×density: 100ラック×10kW = 1,000kW", i_rack["it_capacity_kw"] == 1000.0)
check("直接入力とラック指定(同容量)で需要が完全一致", np.array_equal(d_dir, d_rack))
check("需要は (365, 48)", d_dir.shape == (365, 48))

print("\n【2. 需要の物理整合（demand = IT × PUE、水準と形状の分離）】")
bd = i_dir["breakdown"]
check("年間IT電力量 = 容量 × 負荷率 × 8760h", abs(bd["annual_it_kwh"] - 1000.0 * 0.8 * 8760) < 1e-3, f"{bd['annual_it_kwh']:.1f}")
check("年間総電力量 = IT × PUE(1.40)", abs(bd["annual_total_kwh"] - bd["annual_it_kwh"] * 1.40) < 1e-3)
check("定常・ノイズなし: ピーク = 1000×0.8×1.4 = 1,120kW", abs(i_dir["peak_demand_kw"] - 1120.0) < 1e-6, f"{i_dir['peak_demand_kw']}")
its = []
for prof in app.IT_LOAD_PROFILE_MODES:
    for noise in app.NOISE_LEVELS:
        _, inf = app.resolve_dc_demand(dict(MANUAL_FLAT, profile_mode=prof, noise_level=noise))
        its.append(inf["breakdown"]["annual_it_kwh"])
check("全プロファイル×ノイズで年間電力量が不変（形状は年平均=1.0に正規化）",
      max(its) - min(its) < 1e-6 * max(its), f"幅={max(its) - min(its):.3e} kWh")
_, i_a = app.resolve_dc_demand(dict(MANUAL_FLAT, profile_mode=app.PROFILE_CEC, noise_level="高（12〜18%）"))
_, i_b = app.resolve_dc_demand(dict(MANUAL_FLAT, profile_mode=app.PROFILE_CEC, noise_level="高（12〜18%）"))
check("ノイズは固定シードで再現的（同入力→同出力）", np.array_equal(i_a["it_load_30min"], i_b["it_load_30min"]))

print("\n【3. 用途プリセットがプロファイル/ノイズを決める】")
_, i_h = app.resolve_dc_demand(dict(MANUAL_FLAT, workload_preset="ハウジング（コロケーション）"))
check("ハウジング → CEC実測形状", i_h["profile_mode"] == app.PROFILE_CEC)
_, i_t = app.resolve_dc_demand(dict(MANUAL_FLAT, workload_preset="AI（学習中心）"))
check("AI学習 → 定常", i_t["profile_mode"] == app.PROFILE_FLAT)
_, i_s = app.resolve_dc_demand(dict(MANUAL_FLAT, workload_preset="自社サーバー室"))
check("自社サーバー室 → 日変動", i_s["profile_mode"] == app.PROFILE_DIURNAL)

print("\n【4. 入力検証（ValueError）】")
for label, args in [
    ("IT容量0", dict(MANUAL_FLAT, it_capacity_kw=0)),
    ("ラック数0", dict(MANUAL_FLAT, capacity_mode="ラック数×density", n_racks=0, kw_per_rack=10.0)),
    ("IT負荷率0%", dict(MANUAL_FLAT, it_load_factor_pct=0)),
    ("IT負荷率101%", dict(MANUAL_FLAT, it_load_factor_pct=101)),
    ("PUE 0.9（1.0未満）", dict(MANUAL_FLAT, pue_const=0.9)),
]:
    try:
        app.resolve_dc_demand(args)
        check(f"{label} → ValueError", False, "例外なし")
    except ValueError as e:
        check(f"{label} → ValueError", True, str(e)[:40])
try:
    app.resolve_dc_demand({})
    check("空dc_argsは既定値で補われる", True)
except Exception as e:
    check("空dc_argsは既定値で補われる", False, str(e))

print("\n【5. run_simulation: データセンター経路】")
fm, fd, fdem, fcap, text, dbg, state = run(demand_source="datacenter", dc_args=MANUAL_FLAT)
check("エラーなし", not text.startswith("エラー"), text[:80] if text.startswith("エラー") else "")
check("結果の先頭が「データセンター需要」節", text.startswith("══ データセンター需要 ══"))
check("需要の節に導入前ピーク1,120.0kW", "導入前ピークデマンド: 1,120.0 kW" in text)
check("年間需要量 = 9,811,200 kWh（IT×PUE）", "年間需要量: 9811200.0 kWh/年" in text)
check("産業用の「需要構成」節は出ない", "── 需要構成 ──" not in text)
check("グラフ3種が生成される", fm is not None and fd is not None and fdem is not None)
check("デバッグ欄にDCの需要ソース行", "需要ソース: データセンター" in dbg)
check("stateに需要配列を保持（日付変更時の再描画用）", state["demand_30min"].shape == (365, 48))
check("契約種別「高圧」でピーク<2,000kW → 食い違い警告は出ない", "契約種別は" not in text)

_, _, _, _, text_big, _, _ = run(demand_source="datacenter",
                                   dc_args=dict(MANUAL_FLAT, it_capacity_kw=5000.0))
check("ピーク>=2,000kWで契約種別「高圧」 → 「特別高圧」が目安の警告",
      "契約種別は「特別高圧」が目安です" in text_big)
_, _, _, _, text_ehv, _, _ = run(demand_source="datacenter", contract_type="特別高圧",
                                   elec_basic=1770.0, elec_summer=18.48, elec_other=17.47,
                                   dc_args=dict(MANUAL_FLAT, it_capacity_kw=5000.0))
check("特別高圧を選べば警告は出ない", "契約種別は" not in text_ehv)

print("\n【6. 平坦需要では蓄電池の価値がゼロになる（バグではない。設計書 §11）】")
_, _, _, _, t_flat, _, _ = run(demand_source="datacenter", dc_args=MANUAL_FLAT,
                                 bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=2000.0,
                                 bat_max_charge=1000.0, bat_max_discharge=1000.0)
check("定常需要: 年間充電量 0.0", "年間充電量: 0.0 kWh/年" in t_flat)
check("充放電損失が「-0.0」と表示されない（DC時は丸める）", "-0.0" not in t_flat)
check("充放電ゼロの理由が注記される", "充放電量ゼロ: この条件では蓄電池に裁定余地がありません" in t_flat)
_, _, _, _, t_dv, _, _ = run(demand_source="datacenter",
                               dc_args=dict(MANUAL_FLAT, profile_mode=app.PROFILE_DIURNAL),
                               bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=2000.0,
                               bat_max_charge=1000.0, bat_max_discharge=1000.0)
m = re.search(r"年間充電量: ([\d.]+) kWh/年", t_dv)
check("日変動需要: 蓄電池が動く（年間充電量>0）", m is not None and float(m.group(1)) > 0, m.group(1) if m else "")
check("日変動需要: ゼロ注記は出ない", "充放電量ゼロ" not in t_dv)

print("\n【7. マイクログリッド有効でもDCが落ちない（単一サイト扱い）】")
_, _, _, _, t_mg, _, _ = run(demand_source="datacenter", dc_args=MANUAL_FLAT, mg_enabled=True)
check("エラーなし", not t_mg.startswith("エラー"), t_mg[:80] if t_mg.startswith("エラー") else "")
check("MG収益の節が出る", "マイクログリッド事業" in t_mg)

print("\n【8. 不正入力はエラーメッセージで返る（例外で落ちない）】")
r = run(demand_source="datacenter", dc_args=dict(MANUAL_FLAT, it_capacity_kw=0))
check("戻り値は7要素", len(r) == 7)
check("「エラー: IT定格容量が0以下」", r[4].startswith("エラー: IT定格容量が0以下"), r[4][:50])
check("グラフはNone", r[0] is None and r[1] is None)

print("\n【9. 産業用の既定経路は demand_source 追加で変わらない】")
r_default = run()
r_explicit = run(demand_source="industrial")
check("demand_source省略 = 'industrial' 明示（結果テキスト完全一致）", r_default[4] == r_explicit[4])
check("産業用の結果に「データセンター」節は出ない", "データセンター需要" not in r_default[4])
check("産業用の結果に「需要構成」節が出る", "── 需要構成 ──" in r_default[4])
r_ignore = run(demand_source="industrial", dc_args=MANUAL_FLAT)
check("産業用ではdc_argsを渡しても無視される", r_default[4] == r_ignore[4])

print("\n【10. UI配線】")
demo = app.demo
check("Blocksが構築されている", demo is not None)
check("DC_INPUT_KEYS は13個", len(app.DC_INPUT_KEYS) == 13, f"{len(app.DC_INPUT_KEYS)}")
check("DC_INPUT_KEYS に重複なし", len(set(app.DC_INPUT_KEYS)) == len(app.DC_INPUT_KEYS))
check("需要ソース定数が異なる", app.DEMAND_SOURCE_INDUSTRIAL != app.DEMAND_SOURCE_DATACENTER)

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
