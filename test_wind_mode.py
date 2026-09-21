# -*- coding: utf-8 -*-
"""
test_wind_mode.py - 風力発電（オフサイトPPA）の run_simulation 統合（W2）の検証
==============================================================================
設計: docs/wind_design_spec.md §5-3〜5-5
対象:
  1. 風力OFFで従来と変わらないこと（wind_args 省略／enabled=False／pv_enabled 明示）
  2. 発電の合成（太陽光＋風力）が下流に渡ること（別経路で再計算して照合）
  3. 経済性（風力PPA支払・投資回収・リース料の不変性）
  4. 24/7指標（量ベース達成率・時間一致率）
  5. 太陽光OFF（風力のみ）、入力検証、蓄電池LP・受電上限・最適容量探索との組み合わせ

実行: python test_wind_mode.py
※ 風力OFFの厳密な回帰（10シナリオ×7項目=70項目のバイト一致）は、変更前後で run_simulation の出力を
  保存・照合して別途確認済み（産業用・MG・DC・両面・最適容量探索を含む）。
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


SENDAI = "34392 (SENDAI)"
BASE = dict(
    station_choice=SENDAI, csv_file=None, demand_custom_csv=None,
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
    face_args=face_args([(150.0, "南", 180.0, 30, 0)]),
    num_facilities=1, num_faces=1, display_month=7, display_day=1,
)
RATE = dict(basic_charge_per_kw=1890.0, energy_charge_summer=19.93, energy_charge_other=18.77,
            power_factor_pct=85, fuel_adjustment=0.0, renewable_surcharge=4.18)
DC = dict(workload_preset="手動設定", capacity_mode="IT容量を直接入力", it_capacity_kw=1000.0,
          profile_mode=app.PROFILE_FLAT, noise_level="なし", it_load_factor_pct=80.0, pue_const=1.40)


def run(**over):
    kw = dict(BASE)
    kw.update(over)
    return app.run_simulation(**kw)


def wind(mode="coverage", **kw):
    """wind_args を作る。mode='coverage'（需要カバー率100%）／'capacity'（容量指定）。"""
    d = dict(enabled=True, cf_pct=None, ppa_price=None)
    if mode == "coverage":
        d.update(sizing_mode=app.WIND_SIZING_COVERAGE, coverage_pct=100.0)
    else:
        d.update(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=100.0)
    d.update(kw)
    return d


def errdetail(out):
    """失敗時だけ、エラー文の先頭を1行で見せる。"""
    return out[4][:80].replace("\n", " ") if out[4].startswith("エラー") else ""


def num(text, label, after=None):
    """テキストから label の直後の数値を取る（after があればその文字列より後ろで探す）。"""
    start = text.find(after) if after else 0
    m = re.search(re.escape(label) + r"\s*(-?[\d,]+(?:\.\d+)?)", text[start:])
    return float(m.group(1).replace(",", "")) if m else None


def row(text, label):
    """24/7の比較表の行（量ベース達成率, 時間一致率）を取る。"""
    m = re.search(r"^\s+" + re.escape(label) + r"\s+([\d.]+)%\s+([\d.]+)%", text, re.M)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


# ============================================================
print("【1. 風力OFFで従来と変わらない】")
base_out = run()
for label, over in (("wind_args=None", dict(wind_args=None)),
                    ("wind_args=enabled False", dict(wind_args=dict(enabled=False, capacity_kw=999.0))),
                    ("pv_enabled=True を明示", dict(pv_enabled=True))):
    o = run(**over)
    check(f"{label}: 結果テキスト・デバッグが同一", o[4] == base_out[4] and o[5] == base_out[5])
    check(f"{label}: グラフが同一", all(a.to_json() == b.to_json() for a, b in zip(o[:2], base_out[:2])))
check("風力OFFの result_state に風力のキーがない（従来と同じ中身）",
      not any(k in base_out[6] for k in ("gen_pv", "gen_wind", "wind_info")))
check("風力OFFの結果に風力の節がない", "風力" not in base_out[4] and "24/7" not in base_out[4])

# ============================================================
print("\n【2. 発電の合成が下流に渡る（別経路で再計算）】")
pv_only = run()
w_out = run(wind_args=wind("coverage"))
st = w_out[6]
demand = st["demand_30min"]
info = st["wind_info"]
check("エラーなく完了", not w_out[4].startswith("エラー"), errdetail(w_out))
check("太陽光の発電量は風力の有無で変わらない（ビット同一）",
      np.array_equal(st["gen_pv"], pv_only[6]["total_gen_clipped"]))
check("風力の発電量 = build_wind_30min（東北）", np.array_equal(
    st["gen_wind"], app.build_wind_30min("02", info["capacity_kw"], None)["gen_30min"]))
check("下流に渡る発電量 = 太陽光 + 風力", np.array_equal(st["total_gen_clipped"], st["gen_pv"] + st["gen_wind"]))
gen = st["total_gen_clipped"]
sc = st["sc_result"]
check("系統購入量 = Σmax(0, 需要 − 発電)", abs(sc["annual_import"] - float(np.maximum(0, demand - gen).sum())) < 1e-6)
check("自家消費量 = Σmin(発電, 需要)", abs(sc["annual_self"] - float(np.minimum(gen, demand).sum())) < 1e-6)
check("需要カバー率100% → 風力の年間発電量 = 年間需要",
      abs(info["annual_kwh"] - float(demand.sum())) < 1e-3, f"{info['annual_kwh']:.1f} vs {float(demand.sum()):.1f}")
check("月別グラフ用の monthly は合計（太陽光＋風力）",
      abs(sum(st["monthly"].values()) - float(gen.sum())) < 1e-3)
fig = app.make_daily_chart(st, 1, 15, sc, demand)
idx = [i for i, md in enumerate(st["month_day"]) if md == (1, 15)][0]
gen_trace = [t for t in fig.data if t.name == "発電量"][0]
check("日別グラフの発電量は合計（風力を含む）", np.allclose(gen_trace.y, gen[idx]))

# ============================================================
print("\n【3. 経済性（受電点の基準・託送・風力PPA支払・投資回収）】")
md = st["month_day"]
pv_g0, w_g0 = st["gen_pv"], st["gen_wind"]
sur = RATE["renewable_surcharge"]
X, Y = 2.15, 3.0   # 仙台・高圧の既定（託送の電力量料金・小売手数料）
# 受電点の基準で、アプリとは別の式で再計算する（蓄電池なし・余剰売電）
R = np.maximum(0, demand - pv_g0)                 # 受電点で系統から受ける量（風力の配達分を含む）
retail = np.maximum(0, demand - pv_g0 - w_g0)     # 小売から買う量（風力で賄えなかった残り）
deliv = R - retail                                # 風力の配達量
before = app.calc_electricity_cost(demand, md, **RATE)
c_R = app.calc_electricity_cost(R, md, **RATE)
c_ret = app.calc_electricity_cost(retail, md, **RATE)
after_total = c_R["annual_basic"] + c_ret["annual_energy_charge"] + float(deliv.sum()) * (X + sur + Y)
pv_surplus = np.maximum(0, pv_g0 - demand)        # 売電できるのは敷地内の太陽光の余剰だけ
merit_pre = (before["annual_total"] - after_total) + float(pv_surplus.sum()) * 19.0
payment = info["annual_kwh"] * app.WIND_PPA_PRICE_DEFAULT
check("既定: PPA単価11.96・託送(仙台・高圧)2.15・小売手数料3.0円/kWh",
      (info["ppa_price"], info["wheeling_yen"], info["retail_fee_yen"]) == (11.96, 2.15, 3.0))
check("風力PPA支払 = 年間発電量 × 単価（pay-as-produced）", abs(info["payment_yen"] - payment) < 1e-6)
check("風力の配達量 = R − 小売購入（再計算と一致）", abs(info["delivered_kwh"] - float(deliv.sum())) < 1e-6,
      f"{info['delivered_kwh']:.1f} vs {float(deliv.sum()):.1f}")
check("無駄になった風力 = 発電量 − 配達量", abs(info["wasted_kwh"] - (float(w_g0.sum()) - float(deliv.sum()))) < 1e-6)
txt = w_out[4]
got = num(txt, "年間経済メリット:", after="【風力込みの年間経済メリット】")
check("風力込みの年間経済メリット = 受電点基準で再計算した電気代削減＋売電 − 風力PPA支払",
      got is not None and abs(got - (merit_pre - payment)) < 1.5, f"{got} vs {merit_pre - payment:.0f}")
check("支払前のメリットも再計算と一致", abs(num(txt, "（風力PPA支払の前）:") - merit_pre) < 1.5)
pb = num(txt, "単純投資回収年数:")
net_inv = 150.0 * 158000
check("投資回収年数 = 実質投資額（PVのみ。風力は含めない）÷ 風力込み年間メリット",
      pb is not None and abs(pb - net_inv / (merit_pre - payment)) < 0.06, f"{pb} vs {net_inv / (merit_pre - payment):.2f}")
check("初期投資に風力を含めない（設備投資合計 = PV 23,700,000円）", "設備投資合計: 23,700,000 円" in txt)


def after_block(t):
    """【導入後】の契約電力と基本料金を取る。"""
    i = t.find("【導入後】")
    return num(t[i:], "契約電力:"), num(t[i:], "基本料金:")


cp_pv, bs_pv = after_block(pv_only[4])
cp_w, bs_w = after_block(txt)
check("契約電力は風力で下がらない（太陽光のみの場合と同じ）", cp_pv is not None and cp_pv == cp_w, f"{cp_pv} vs {cp_w}")
check("基本料金も風力で下がらない（太陽光のみの場合と同じ）", bs_pv is not None and bs_pv == bs_w, f"{bs_pv} vs {bs_w}")
check("結果に『風力では下がりません』と明記", "風力では下がりません" in txt)

# 単価の感度: 託送・手数料を1円/kWh上げると、メリットは配達量×1円だけ減る（賦課金は届いた分に必ずかかる）
d1 = float(deliv.sum())
for key, label in (("wheeling_yen", "託送の電力量料金"), ("retail_fee_yen", "小売手数料")):
    o = run(wind_args=wind("coverage", **{key: (2.15 if key == "wheeling_yen" else 3.0) + 1.0}))
    g = num(o[4], "年間経済メリット:", after="【風力込みの年間経済メリット】")
    check(f"{label}を+1円/kWh → 年間メリットが 配達量×1円 だけ減る", abs((got - g) - d1) < 1.5, f"{got - g:.1f} vs {d1:.1f}")
o0 = run(wind_args=wind("coverage", wheeling_yen=0.0, retail_fee_yen=0.0))
g0 = num(o0[4], "年間経済メリット:", after="【風力込みの年間経済メリット】")
check("託送と手数料を0円にすると、年間メリットは 配達量×(託送+手数料) だけ増える（賦課金は残る）",
      abs((g0 - got) - d1 * (X + Y)) < 1.5, f"{g0 - got:.1f} vs {d1 * (X + Y):.1f}")
check("届いた風力1kWhあたりの負担の行がある（PPA支払＋届いた分の費用 ÷ 届いた量）",
      abs(num(txt, "届いた風力1kWhあたりの負担:") - (payment + d1 * (X + sur + Y)) / d1) < 0.01,
      str(num(txt, "届いた風力1kWhあたりの負担:")))

# 投資回収: 風力が大きすぎて年間メリットが負になる場合は回収不可
neg = run(wind_args=wind("capacity", capacity_kw=3000.0, ppa_price=40.0))
check("風力PPA支払が大きく年間メリットが負なら回収年数は計算不可",
      "回収年数は計算不可" in neg[4], neg[4][neg[4].find("【投資回収】"):][:80].replace("\n", " "))

# 売電は敷地内の太陽光の余剰だけ（風力の余剰は売電できない）
big = run(wind_args=wind("coverage", coverage_pct=300.0))
sb = big[6]["sc_result"]
pv_sur_big = float(np.maximum(0, big[6]["gen_pv"] - big[6]["demand_30min"]).sum())
check("売電量は太陽光の余剰だけ（風力が3倍あっても増えない）", abs(sb["annual_export"] - pv_sur_big) < 1e-6,
      f"{sb['annual_export']:.1f} vs {pv_sur_big:.1f}")
check("運転の結果（プール）の余剰は別キーに残る（太陽光＋風力の余剰）", sb["annual_export_pooled"] > sb["annual_export"])
check("FITの注記は出さない（風力の余剰は売電しない扱いに変えたため）", "FITは適用できない" not in big[4])
check("『売電はできない』『売電できず、無駄になる』の記載", "売電はできない" in big[4] and "売電できず、無駄になる" in big[4])
nx = run(wind_args=wind("coverage", coverage_pct=300.0), sell_mode="逆潮流禁止（売電なし）")
sn = nx[6]["sc_result"]
check("逆潮流禁止: 売電0・出力抑制は太陽光の余剰だけ",
      sn["annual_export"] == 0.0 and abs(sn["annual_curtailment"] - pv_sur_big) < 1e-6,
      f"{sn['annual_export']} / {sn['annual_curtailment']:.1f} vs {pv_sur_big:.1f}")

# 蓄電池あり（ルールベース・LP）: 受電点基準の再構成がエネルギー収支と矛盾しない
for label, over in (("ルールベース", dict(bat_enabled=True, bat_capacity=200.0, bat_max_charge=100.0, bat_max_discharge=100.0)),
                    ("LP", dict(bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=200.0,
                                bat_max_charge=100.0, bat_max_discharge=100.0))):
    ob = run(wind_args=wind("coverage", coverage_pct=150.0), **over)
    stb = ob[6]
    scb, Db, pvb, wb = stb["sc_result"], stb["demand_30min"], stb["gen_pv"], stb["gen_wind"]
    off = app.offsite_receiving(pvb, [stb["wind_info"]], Db, scb)
    chb, disb = scb["battery_charge"], scb["battery_discharge"]
    resid = scb["import_"] + pvb + wb + disb - (Db + chb + scb["export"] + scb["curtailment"])
    check(f"{label}: 運転のエネルギー収支が成り立つ（残差 1e-4 kWh 以内）", float(np.abs(resid).max()) < 1e-4,
          f"{float(np.abs(resid).max()):.1e}")
    check(f"{label}: 受電量 R ≧ 小売購入、配達量は 0〜風力発電量", bool((off["receive"] >= scb["import_"] - 1e-9).all()
          and (off["delivered"] >= 0).all() and (off["delivered"] <= wb + 1e-9).all()))
    check(f"{label}: 太陽光の余剰 ＋ 無駄になった風力 = 運転結果（プール）の余剰（売電＋抑制）",
          abs(float(off["pv_surplus"].sum() + off["wasted_by_source"][0].sum())
              - float((scb["export"] + scb["curtailment"]).sum())) < 0.5)
    check(f"{label}: 完了して受電点基準の料金が出る", "風力では下がりません" in ob[4] and not ob[4].startswith("エラー"))
lpw = run(wind_args=wind("coverage"), bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=100.0,
          bat_max_charge=50.0, bat_max_discharge=50.0)
check("LP+風力: LPは風力を敷地内と同じに扱う旨の注記を出し、最適化年間コストは出さない",
      "敷地内の発電と同じに扱って最適化" in lpw[4]
      and "最適化年間コスト:" not in lpw[4] and "最適化ピークデマンド:" not in lpw[4])

# ============================================================
print("\n【4. 24/7指標】")
imp = np.maximum(0, demand - gen)   # 蓄電池なしの系統購入量（風力の配達分は含めない＝小売から買う分）
pv_g = st["gen_pv"]
w_g = st["gen_wind"]


def rates(g):
    d = float(demand.sum())
    vol = min(1.0, float(g.sum()) / d) * 100
    hourly = (1 - float(np.maximum(0, demand - g).sum()) / d) * 100
    return vol, hourly


for label, g in (("太陽光のみ", pv_g), ("風力のみ", w_g), ("太陽光＋風力", pv_g + w_g)):
    vol, hr = rates(g)
    gv, gh = row(txt, label)
    check(f"{label}: 量ベース達成率・時間一致率が再計算と一致（蓄電池なし）",
          gv is not None and abs(gv - vol) < 0.06 and abs(gh - hr) < 0.06, f"{gv}/{gh} vs {vol:.1f}/{hr:.1f}")
vs, hs = rates(pv_g)
vw, hw = rates(w_g)
vc, hc = rates(pv_g + w_g)
check("補完の効果: 合計の時間一致率 > 太陽光のみ、> 風力のみ", hc > hs and hc > hw, f"{hc:.1f} > {hs:.1f}, {hw:.1f}")
check("時間一致率 ≤ 量ベース達成率（年間で足りても、その時間には足りない）", hc <= vc + 1e-9)
set_line = re.search(r"時間一致率: ([\d.]+)%（系統購入 ([\d,.]+) kWh/年）", txt)
check("設定どおりの時間一致率 = 1 − 系統購入/需要（蓄電池なしなので参考値と同じ）",
      abs(float(set_line.group(1)) - hc) < 0.06)
# 月別の表: 月別の系統購入の合計 = 年間の系統購入
tbl = re.findall(r"^\s+(\d+)月\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)%", txt, re.M)
check("月別の表は12行", len(tbl) == 12, str(len(tbl)))
check("月別の系統購入の合計 ≒ 年間の系統購入（丸めの範囲）",
      abs(sum(float(r[4].replace(",", "")) for r in tbl) - float(imp.sum())) < 12)
check("同時性の限界を明記している", "同時性は反映されません" in txt)

# 蓄電池LPありのときは、LPが電気代最小化である注記を出す
lp = run(wind_args=wind("coverage"), bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=100.0,
         bat_max_charge=50.0, bat_max_discharge=50.0)
check("蓄電池LP: 完了し、24/7の節に『時間一致率の最大化を目的にしていません』の注記",
      not lp[4].startswith("エラー") and "最大化を目的にしていません" in lp[4])
hr_lp = float(re.search(r"時間一致率: ([\d.]+)%（系統購入", lp[4]).group(1))
check("蓄電池を足すと時間一致率は下がらない（蓄電池なし以上）", hr_lp >= hc - 0.06, f"{hr_lp:.1f} vs {hc:.1f}")

# ============================================================
print("\n【5. 太陽光OFF（風力のみ）】")
wo = run(pv_enabled=False, wind_args=wind("coverage"))
check("完了する", not wo[4].startswith("エラー"), errdetail(wo))
check("太陽光は使用しないと表示し、面別発電量は出さない",
      "太陽光発電: 使用しない（風力のみ）" in wo[4] and "面別年間発電量" not in wo[4])
check("太陽光の発電量はゼロ", float(np.abs(wo[6]["gen_pv"]).sum()) == 0.0)
check("発電量 = 風力のみ", np.array_equal(wo[6]["total_gen_clipped"], wo[6]["gen_wind"]))
check("比較表は『風力のみ』の1行", row(wo[4], "風力のみ")[0] is not None and row(wo[4], "太陽光のみ")[0] is None)
check("初期投資がないので回収年数は該当しない", "投資回収年数は該当しません" in wo[4])
check("設備投資は0円", "設備投資合計: 0 円" in wo[4] or "PV: 0.0 kW" in wo[4])
bad_faces = face_args([(150.0, "南", "abc", 30, 0)])
o = run(pv_enabled=False, wind_args=wind("coverage"), face_args=bad_faces)
check("太陽光OFFのときは面設定を読まない（不正な方位角でもエラーにならない）", not o[4].startswith("エラー"))
o = run(pv_enabled=True, wind_args=wind("coverage"), face_args=bad_faces)
check("太陽光ONなら従来どおり不正な方位角はエラー", o[4].startswith("エラー") and "方位角" in o[4])
o = run(pv_enabled=False)
check("太陽光も風力も使わない設定はエラー", o[4].startswith("エラー") and "どちらか" in o[4])
o = run(wind_args=wind("coverage"), face_args=face_args([(0.0, "南", 180.0, 30, 0)]))
check("太陽光ONでも面のPpeakが0なら風力のみとして動く", not o[4].startswith("エラー"))
# 太陽光の容量を0にして風力だけを見る使い方（UIで最も自然な操作）。『太陽光を使わない』と同じ表示になること
check("Ppeak=0: 『太陽光発電: 使用しない』と出し、空の面別見出し・K'・太陽光のみの行は出さない",
      "太陽光発電: 使用しない（風力のみ）" in o[4] and "面別年間発電量" not in o[4] and "K' =" not in o[4]
      and row(o[4], "太陽光のみ")[0] is None and "太陽光=使用しない" in o[5])
o_small = run(face_args=face_args([(0.0, "南", 180.0, 30, 0)]), wind_args=wind("capacity", capacity_kw=5.0))
check("24/7の『差』は負のゼロ（-0.0）にならない（風力が小さく、発電を全量使い切るとき）",
      "差: -" not in o_small[4] and "差: 0.0 ポイント" in o_small[4], re.search(r"差: [^ ]+", o_small[4]).group(0))
o = run(face_args=face_args([(0.0, "南", 180.0, 30, 0)]))
check("風力なしで有効な面がなければ従来のエラー", "有効な面設定がありません" in o[4])

# ============================================================
print("\n【6. 入力検証】")
o = run(station_choice="44132 (TOKYO)", wind_args=wind("coverage"))
check("需要地が東京（対象外エリア）ならエラー", o[4].startswith("エラー") and "北海道・東北" in o[4] and "TOKYO" in o[4], o[4][:60])
o = run(station_choice="14163 (SAPPORO)", wind_args=wind("coverage"))
check("札幌は北海道エリアの風力で動く", not o[4].startswith("エラー") and "北海道エリア" in o[4])
o = run(station_choice="54232 (NIIGATA)", wind_args=wind("coverage"))
check("新潟は東北エリアの風力で動く", not o[4].startswith("エラー") and "東北エリア" in o[4])
o = run(wind_args=wind("capacity", capacity_kw=None))
check("契約容量が空ならエラー", o[4].startswith("エラー") and "契約容量" in o[4])
o = run(wind_args=wind("coverage", coverage_pct=0))
check("カバー率0ならエラー", o[4].startswith("エラー") and "カバー率" in o[4])
o = run(wind_args=wind("capacity", cf_pct=0))
check("設備利用率0ならエラー", o[4].startswith("エラー") and "設備利用率" in o[4])
o = run(wind_args=wind("capacity", ppa_price=-1.0))
check("PPA単価が負ならエラー", o[4].startswith("エラー") and "PPA単価" in o[4])
o = run(wind_args=wind("capacity", ppa_price=0.0))
check("PPA単価0円は許容（支払0）", not o[4].startswith("エラー") and "PPA支払: 0 円/年" in o[4])
o = run(wind_args=wind("coverage"), mg_enabled=True)
check("風力とMGの併用が動く（W2b。詳細は節9）",
      not o[4].startswith("エラー") and "マイクログリッド事業" in o[4] and "風力の調達費用" in o[4], errdetail(o))
try:
    app.resolve_wind(wind("coverage"), None, 1e6)
    check("CSVアップロード（地点なし）はエラー", False)
except ValueError as e:
    check("CSVアップロード（地点なし）はエラー", "CSV" in str(e))
check("resolve_wind: 無効なら None", app.resolve_wind(None, "34392", 1e6) is None
      and app.resolve_wind(dict(enabled=False), "34392", 1e6) is None)
o = run(wind_args=wind("capacity", capacity_kw=1000.0, cf_pct=35.8))
check("設備利用率35.8%（東北）では契約容量で頭打ちの注記", "契約容量で頭打ち" in o[4])

# 託送・手数料の既定値（エリア×契約種別）と検証
for sta, ct, wh in (("14163 (SAPPORO)", "高圧", 2.28), ("14163 (SAPPORO)", "特別高圧", 1.02),
                    (SENDAI, "高圧", 2.15), (SENDAI, "特別高圧", 0.97)):
    o = run(station_choice=sta, contract_type=ct, wind_args=wind("coverage"))
    wi = o[6]["wind_info"]
    check(f"託送の既定: {sta.split(' ')[1]}・{ct} = {wh}円/kWh、手数料 {app.WIND_RETAIL_FEE_YEN[ct]}円/kWh",
          wi["wheeling_yen"] == wh and wi["retail_fee_yen"] == app.WIND_RETAIL_FEE_YEN[ct],
          f"{wi['wheeling_yen']}/{wi['retail_fee_yen']}")
o = run(wind_args=wind("capacity", wheeling_yen=-1.0))
check("託送の単価が負ならエラー", o[4].startswith("エラー") and "託送" in o[4])
o = run(wind_args=wind("capacity", retail_fee_yen=float("nan")))
check("小売手数料がNaNならエラー", o[4].startswith("エラー") and "小売手数料" in o[4])
o = run(wind_args=wind("capacity", wheeling_yen=0.0, retail_fee_yen=0.0))
check("託送0円・手数料0円は許容", not o[4].startswith("エラー"))
check("WIND_INPUT_KEYS に託送・手数料がある", "wheeling_yen" in app.WIND_INPUT_KEYS and "retail_fee_yen" in app.WIND_INPUT_KEYS)

# ============================================================
print("\n【7. リース／PPA（モードA）との組み合わせ】")
lease_no = run(business_model="リース")
lease_w = run(business_model="リース", wind_args=wind("coverage"))
l0 = num(lease_no[4], "必要リース料:")
l1 = num(lease_w[4], "必要リース料:")
check("リース料は風力の有無で変わらない（対象はPV設備のみ）", l0 == l1 and l0 > 0, f"{l0} vs {l1}")
net_w = num(lease_w[4], "年間経済メリット:", after="【風力込みの年間経済メリット】")
cust = num(lease_w[4], "需要家年間メリット:")
check("需要家年間メリット = 風力込み年間経済メリット − リース料", abs(cust - (net_w - l1)) < 1.5, f"{cust} vs {net_w - l1:.0f}")
check("需要家メリットの見出しは『電気代削減−風力の費用』（PPA支払と、届いた分の託送等を引いた値）",
      "電気代削減−風力の費用:" in lease_w[4])
check("風力なしのリースの見出しは従来どおり『電気代削減』", "  電気代削減: " in lease_no[4])
ppa_w = run(business_model="PPA", wind_args=wind("coverage"))
ppa_price = num(ppa_w[4], "必要PPA単価:")
self_pv = num(ppa_w[4], "年間自家消費量:")
check("PPA単価 × 敷地内の太陽光・蓄電池分の自家消費量 = 必要リース料", abs(ppa_price * self_pv - l1) < 0.02 * l1 / 100 + 50,
      f"{ppa_price * self_pv:.0f} vs {l1:.0f}")
# 自家消費 ＝ 敷地内の太陽光・蓄電池分 ＋ 風力の配達量（蓄電池なしなら厳密）。W2 の発電量比の按分は、この実測に置き換えた
pv_only_self = float(np.minimum(st["gen_pv"], demand).sum())
check("太陽光分の自家消費量 = 全自家消費量 − 風力の配達量（= Σmin(太陽光, 需要)。蓄電池なし）",
      abs(self_pv - (sc["annual_self"] - info["delivered_kwh"])) < 0.1 and abs(self_pv - pv_only_self) < 0.1,
      f"{self_pv} vs {sc['annual_self'] - info['delivered_kwh']:.1f} / {pv_only_self:.1f}")
check("風力の配達分を除いた旨の注記", "風力の配達分を除いた" in ppa_w[4])

# ============================================================
print("\n【8. データセンター・蓄電池LPとの組み合わせ／併用できない機能の明示エラー】")
dc_kw = dict(demand_source=app.DEMAND_SOURCE_DATACENTER, station_choice="14163 (SAPPORO)",
             face_args=face_args([(3000.0, "南", 180.0, 30, 0)]))
dc_no = run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_NONE), **dc_kw)
dc_w = run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_NONE), wind_args=wind("coverage"), **dc_kw)
check("DC + 風力が動く（北海道エリア）", not dc_w[4].startswith("エラー") and "北海道エリア" in dc_w[4])
check("DCの需要の節は従来どおり出る", "データセンター" in dc_w[4])
check("DCでも風力ONで系統購入が減る", dc_w[6]["sc_result"]["annual_import"] < dc_no[6]["sc_result"]["annual_import"])
check("DC: 契約種別が特別高圧なら託送は北海道・特高の1.02円/kWh",
      run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_NONE), wind_args=wind("coverage"), contract_type="特別高圧",
          **dc_kw)[6]["wind_info"]["wheeling_yen"] == 1.02)

# 併用できないもの（受電点基準でLPが最適化されていない）は、黙って誤った数字を出さず明示エラー
gc = run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_EHV33), wind_args=wind("coverage", coverage_pct=60.0),
         bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=3000.0, bat_max_charge=1500.0,
         bat_max_discharge=1500.0, **dc_kw)
check("風力と系統受電上限（DC）の併用は明示エラー", gc[4].startswith("エラー") and "受電上限" in gc[4], errdetail(gc))
gc2 = run(dc_args=dict(DC, grid_cap_mode=app.GRID_CAP_NONE), wind_args=wind("coverage", coverage_pct=60.0),
          bat_enabled=True, bat_mode="最適充放電（LP）", bat_capacity=3000.0, bat_max_charge=1500.0,
          bat_max_discharge=1500.0, **dc_kw)
check("受電上限を『制限なし』にすればDC + 風力 + LPは動く", not gc2[4].startswith("エラー"), errdetail(gc2))
cs = run(bat_enabled=True, bat_mode="最適容量探索", bat_max_charge=100.0, bat_max_discharge=100.0,
         wind_args=wind("coverage"))
check("風力と最適容量探索の併用は明示エラー", cs[4].startswith("エラー") and "最適容量探索" in cs[4], errdetail(cs))
cs0 = run(bat_enabled=False, bat_mode="最適容量探索", wind_args=wind("coverage"))
check("蓄電池OFFなら（モードが残っていても）エラーにしない", not cs0[4].startswith("エラー"), errdetail(cs0))

# ============================================================
print("\n【9. マイクログリッド（モードB）との組み合わせ（W2b）】")
# MGの既定: 自営線 2km × 3,000万円/km、運営コストは投資額の2%、P-IRR期間20年。施設は1つ（束ねメリットの独立計算を単純にするため）
MG_TOTAL = 150.0 * 158000 + 2.0 * 30000000     # PV（風力は設備を持たない）＋自営線
MG_OPEX = MG_TOTAL * 0.02
mg_w = run(wind_args=wind("coverage"), mg_enabled=True)
mg_pv = run(mg_enabled=True)
mt = mg_w[4]
stm = mg_w[6]
dm, pvm, wm, scm, infm = stm["demand_30min"], stm["gen_pv"], stm["gen_wind"], stm["sc_result"], stm["wind_info"]
check("MG + 風力が動く", not mt.startswith("エラー"), errdetail(mg_w))
# 受電点の基準で独立に再計算（蓄電池なし）
Rm = np.maximum(0, dm - pvm)
dlm = Rm - np.maximum(0, dm - pvm - wm)
basic_saving = app.calc_electricity_cost(dm, md, **RATE)["annual_basic"] - app.calc_electricity_cost(Rm, md, **RATE)["annual_basic"]
avg_price = 19.93 * 0.25 + 18.77 * 0.75               # 網内単価（PPA以外は電力量単価の加重平均）
wind_cost = infm["payment_yen"] + float(dlm.sum()) * (2.15 + sur + 3.0)
cf_expected = scm["annual_self"] * avg_price + basic_saving - MG_OPEX - wind_cost
cf_got = num(mt, "年間キャッシュフロー:")
check("年間キャッシュフロー = 網内売電 ＋ 束ね − 運営 − 風力の調達費用（独立に再計算）",
      cf_got is not None and abs(cf_got - cf_expected) < 1.5, f"{cf_got} vs {cf_expected:.0f}")
check("風力の調達費用の表示 = PPA支払 ＋ 届いた分の託送・賦課金・手数料", abs(num(mt, "風力の調達費用:") - wind_cost) < 1.5,
      f"{num(mt, '風力の調達費用:')} vs {wind_cost:.0f}")
irr_raw = app._calc_irr([-MG_TOTAL] + [cf_expected] * 20)
if irr_raw is None:
    # 既定のMG（自営線6,000万円）に、需要の100%相当の風力（PPA支払・託送等が売電収入を上回る）を足すと赤字になる
    check("キャッシュフローが負なら P-IRR は『算出不可』と表示する", "算出不可" in mt and cf_expected < 0, f"cf={cf_expected:.0f}")
else:
    irr_got = num(mt, "P-IRR:", after="【P-IRR（")
    check("P-IRR = 独立に再計算したキャッシュフローのIRR", irr_got is not None and abs(irr_got - irr_raw * 100) < 0.01,
          f"{irr_got} vs {irr_raw * 100:.2f}")
check("束ねメリット（基本料金差額）は風力の有無で変わらない（風力では契約電力が下がらない）",
      num(mt, "基本料金差額:") == num(mg_pv[4], "基本料金差額:"), f"{num(mt, '基本料金差額:')} vs {num(mg_pv[4], '基本料金差額:')}")
check("束ねメリット = 個別契約の基本料金 − 受電点基準のMG基本料金（再計算）", abs(num(mt, "基本料金差額:") - basic_saving) < 1.5)
check("MGの初期投資に風力を含めない（MG投資合計 = PV＋自営線）", f"MG投資合計: {MG_TOTAL:,.0f} 円" in mt)
mg_x = run(wind_args=wind("coverage", wheeling_yen=3.15), mg_enabled=True)
check("託送を+1円/kWh → MGの年間キャッシュフローが 配達量×1円 だけ減る",
      abs((cf_got - num(mg_x[4], "年間キャッシュフロー:")) - float(dlm.sum())) < 1.5)
check("風力なしのMGには風力の行がない（従来の出力）", "風力" not in mg_pv[4])

# PPA（MG）: 単価は（投資の回収＋風力の調達費用）÷ 網内に供給した全量。P-IRRは目標（10%）を下回らない
crf = 0.10 * 1.10 ** 15 / (1.10 ** 15 - 1)
lease_mg = MG_TOTAL * crf + MG_OPEX
mg_ppa = run(business_model="PPA", wind_args=wind("coverage"), mg_enabled=True)
pt = mg_ppa[4]
ppa_paid = num(pt, "PPA支払:", after="【需要家メリット（")   # 風力の節の「PPA支払」ではなく、需要家メリットの節の値
check("MG+PPA: PPA支払 = 投資の回収 ＋ 風力の調達費用（単価×全量）", abs(ppa_paid - (lease_mg + wind_cost)) < 1.5,
      f"{ppa_paid} vs {lease_mg + wind_cost:.0f}")
check("MG+PPA: 年間キャッシュフロー = 投資の回収 ＋ 束ね − 運営（風力の費用は単価で回収される）",
      abs(num(pt, "年間キャッシュフロー:") - (MG_TOTAL * crf + basic_saving)) < 2.0,
      f"{num(pt, '年間キャッシュフロー:')} vs {MG_TOTAL * crf + basic_saving:.0f}")
check("MG+PPA: P-IRRは目標10%を下回らない（束ねメリット分だけ上回る）", num(pt, "P-IRR:", after="【P-IRR（") >= 10.0 - 0.01,
      str(num(pt, "P-IRR:", after="【P-IRR（")))
check("MG+PPA: 需要家年間メリット = 風力込みの年間メリット − 投資の回収分",
      abs(num(pt, "需要家年間メリット:") - ((merit_pre - payment) - lease_mg)) < 2.0,
      f"{num(pt, '需要家年間メリット:')} vs {(merit_pre - payment) - lease_mg:.0f}")
mg_ls = run(business_model="リース", wind_args=wind("coverage"), mg_enabled=True)
check("MG+リース: リース料は風力の有無で変わらない ＋ 需要家メリットは風力込み − リース料",
      abs(num(mg_ls[4], "必要リース料:") - lease_mg) < 1.5
      and abs(num(mg_ls[4], "需要家年間メリット:") - ((merit_pre - payment) - lease_mg)) < 2.0)

# IRR: 全期間の収支が負だと符号が変わらずIRRは存在しない。以前はニュートン法が発散して OverflowError が画面のエラーになった
check("_calc_irr: 全期間が負のキャッシュフローは例外を出さず None", app._calc_irr([-1e8] + [-3e6] * 20) is None
      and app._calc_irr([-1e8] + [-1e7] * 20) is None and app._calc_irr([0.0] * 5) is None)
check("_calc_irr: 通常のキャッシュフローは従来どおり（10年で元本回収なら0%）",
      abs(app._calc_irr([-1000.0] + [100.0] * 10)) < 1e-6 and abs(app._calc_irr([-1000.0] + [200.0] * 10) - 0.1514) < 1e-3)

# 複数施設: 束ねメリットの行が出て、完了する
mg2 = run(wind_args=wind("coverage"), mg_enabled=True, num_facilities=2,
          facility_args=fac_args([("役所・自治体庁舎", 4500, 1), ("公立小学校", 6900, 2)]))
check("MG + 風力 + 複数施設が動く（束ねメリットの内訳が出る）",
      not mg2[4].startswith("エラー") and "個別契約時基本料金合計" in mg2[4], errdetail(mg2))

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
