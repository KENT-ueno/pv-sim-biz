# -*- coding: utf-8 -*-
"""
test_wind_ui.py - 風力発電（オフサイトPPA）のUI（W3）の検証
=========================================================
設計: docs/wind_design_spec.md §5-6
対象:
  1. 入力の変換（build_wind_args / _opt_float）とエリア表示（wind_area_note）
  2. UI配線: 「計算」ボタンの関数を、UIの入力コンポーネントの並びどおりに呼ぶ（位置ずれの検出が目的）
     各欄に別々の値を入れ、結果のデバッグ欄に正しく届くことを見る
  3. 表示切替（有効／容量⇔カバー率）と、グラフの内訳（風力あり）／従来のグラフ（風力なし）

実行: python test_wind_ui.py
※ 風力OFFのグラフ・結果が従来とバイト単位で同じことは、run_simulation の出力を保存→照合する回帰
  （10シナリオ×7項目。グラフ2種を含む）で確認済み。
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


# ============================================================
print("【1. 入力の変換とエリア表示】")
# 末尾5つは W2f-3 で追加（payment_basis, gen_charge_mode, gen_charge_discount_yen, balancing_yen, loss_rate_pct）
check("OFFなら入力を検証しない（不正な値でも通る）",
      app.build_wind_args(False, "x", "abc", "abc", "abc", "abc", "abc", "abc", "x", "x", "abc", "abc", "abc")
      == {"enabled": False})
w = app.build_wind_args(True, app.WIND_SIZING_CAPACITY, "", "", 29.1, 11.96, "", "",
                        app.WIND_PAYMENT_BASIS_GENERATED, app.WIND_GEN_CHARGE_AUTO, "", "", "")
check("空欄は None（既定値を使う）", w["capacity_kw"] is None and w["coverage_pct"] is None
      and w["wheeling_yen"] is None and w["retail_fee_yen"] is None
      and w["gen_charge_discount_yen"] is None and w["balancing_yen"] is None and w["loss_rate_pct"] is None)
check("数値欄はそのまま", w["cf_pct"] == 29.1 and w["ppa_price"] == 11.96 and w["enabled"] is True)
check("ラジオ欄はそのまま", w["payment_basis"] == app.WIND_PAYMENT_BASIS_GENERATED
      and w["gen_charge_mode"] == app.WIND_GEN_CHARGE_AUTO)
w = app.build_wind_args(True, app.WIND_SIZING_COVERAGE, " 1,000 ", "80", None, None, "2.5", " 0 ",
                        app.WIND_PAYMENT_BASIS_USED, app.WIND_GEN_CHARGE_ADD, " 0 ", "1.5", "3.0")
check("前後の空白・桁区切りを許す", w["capacity_kw"] == 1000.0 and w["coverage_pct"] == 80.0)
check("Number欄の空（None）は None", w["cf_pct"] is None and w["ppa_price"] is None)
check("0 は 0（空欄と区別する）", w["retail_fee_yen"] == 0.0 and w["wheeling_yen"] == 2.5
      and w["gen_charge_discount_yen"] == 0.0)
check("新設5欄もそのまま届く（使用量払い・加算・0円・1.5円・3.0%）",
      w["payment_basis"] == app.WIND_PAYMENT_BASIS_USED and w["gen_charge_mode"] == app.WIND_GEN_CHARGE_ADD
      and w["balancing_yen"] == 1.5 and w["loss_rate_pct"] == 3.0)
for bad in ("abc", "1e999", "nan", "inf", "１２x"):
    try:
        app.build_wind_args(True, app.WIND_SIZING_CAPACITY, bad, "", 29.1, 11.96, "", "",
                            app.WIND_PAYMENT_BASIS_GENERATED, app.WIND_GEN_CHARGE_AUTO, "", "", "")
        check(f"不正な値 {bad!r} はエラー", False)
    except ValueError as e:
        check(f"不正な値 {bad!r} はエラー", "風力発電（オフサイトPPA）の契約容量" in str(e))

n1 = app.wind_area_note("34392 (SENDAI)", False, "高圧")
check("仙台・高圧: 東北・託送2.15・グロスマージン4.1（暫定）・損失率5.2%",
      "東北" in n1 and "2.15" in n1 and "4.1" in n1 and "暫定" in n1 and "5.2" in n1, n1)
n2 = app.wind_area_note("14163 (SAPPORO)", False, "特別高圧")
check("札幌・特高: 北海道・託送1.02・グロスマージン4.1・損失率2.0%",
      "北海道" in n2 and "1.02" in n2 and "4.1" in n2 and "2.0" in n2, n2)
check("東京は北海道・東北のみと案内（越境しない）", "北海道・東北の地点でのみ" in app.wind_area_note("44132 (TOKYO)", False, "高圧"))
check("CSVアップロードは使えない旨", "CSV" in app.wind_area_note("34392 (SENDAI)", True, "高圧"))
check("地点未選択は案内", "地点を選ぶ" in app.wind_area_note(None, False, "高圧"))

# ============================================================
print("\n【2. UI配線（「計算」ボタンの関数を、入力コンポーネントの並びどおりに呼ぶ）】")
click = [f for f in app.demo.fns.values() if getattr(f.fn, "__name__", "") == "on_click"]
check("「計算」ボタンの関数が1つ登録されている", len(click) == 1, str(len(click)))
click = click[0]
inputs = list(click.inputs)
labels = [getattr(c, "label", None) for c in inputs]
n_wind = len(app.WIND_INPUT_KEYS)
tail = inputs[-(n_wind + 1):]
check("入力の末尾は『太陽光を使う』＋風力13欄（WIND_INPUT_KEYS と同じ並び。W2f-3）",
      [type(c).__name__ for c in tail] == ["Checkbox", "Checkbox", "Radio", "Textbox", "Textbox", "Number", "Number",
                                           "Textbox", "Textbox", "Radio", "Radio", "Textbox", "Textbox", "Textbox"],
      str([type(c).__name__ for c in tail]))
tl = [getattr(c, "label", "") or "" for c in tail]
check("末尾の欄のラベルが 太陽光・風力・容量の指定方法・契約容量・カバー率・設備利用率・PPA発電単価・託送・"
      "小売グロスマージン・支払の対象・発電側課金・割引・バランシング・損失率 の順",
      "太陽光発電を使う" in tl[0] and "風力発電（オフサイトPPA）を併用する" in tl[1] and "容量の指定方法" in tl[2] and "契約容量" in tl[3]
      and "需要カバー率" in tl[4] and "設備利用率" in tl[5] and "PPA発電単価" in tl[6] and "託送" in tl[7]
      and "小売グロスマージン" in tl[8] and "支払の対象" in tl[9] and "発電側課金" in tl[10]
      and "系統設備効率化割引" in tl[11] and "発電バランシング単価" in tl[12] and "損失率" in tl[13],
      str(tl))
check("既定: 太陽光ON・風力OFF・容量の指定・設備利用率29.1・PPA発電単価11.96・全量払い・発電側課金自動",
      tail[0].value is True and tail[1].value is False and tail[2].value == app.WIND_SIZING_CAPACITY
      and tail[5].value == 29.1 and tail[6].value == 11.96
      and tail[9].value == app.WIND_PAYMENT_BASIS_GENERATED and tail[10].value == app.WIND_GEN_CHARGE_AUTO,
      str([c.value for c in tail]))
check("任意入力（容量・カバー率・託送・グロスマージン・割引・バランシング・損失率）は空欄で始まる（Numberにしない）",
      all(t.value in (None, "") for t in (tail[3], tail[4], tail[7], tail[8], tail[11], tail[12], tail[13])))

# 既定値の並びで呼ぶための引数（State等は既定値）。地点は仙台（東北エリア）にする
# UIの既定は施設タイプ「なし」（需要なし）なので、施設1に役所（4,500m² × 1棟）を設定する
defaults = [c.value for c in inputs]
_fac = next(i for i, c in enumerate(inputs) if getattr(c, "label", None) == "施設タイプ")
defaults[_fac], defaults[_fac + 1], defaults[_fac + 2] = "役所・自治体庁舎", 4500, 1
station_idx = next(i for i, c in enumerate(inputs) if getattr(c, "label", "") and "観測地点" in str(c.label)
                   or type(c).__name__ == "Dropdown" and any("SENDAI" in str(ch) for ch in getattr(c, "choices", [])))
sendai = next(str(ch[1] if isinstance(ch, (tuple, list)) else ch) for ch in inputs[station_idx].choices
              if "SENDAI" in str(ch))
tokyo = next(str(ch[1] if isinstance(ch, (tuple, list)) else ch) for ch in inputs[station_idx].choices
             if "TOKYO" in str(ch))


def click_with(station=sendai, **over):
    """over: pv / enabled / sizing / capacity / coverage / cf / ppa / wheeling / fee /
    payment_basis / gen_charge_mode / gen_charge_discount / balancing / loss_rate。"""
    a = list(defaults)
    a[station_idx] = station
    m = dict(pv=-14, enabled=-13, sizing=-12, capacity=-11, coverage=-10, cf=-9, ppa=-8, wheeling=-7, fee=-6,
             payment_basis=-5, gen_charge_mode=-4, gen_charge_discount=-3, balancing=-2, loss_rate=-1)
    for k, v in over.items():
        a[m[k]] = v
    return click.fn(*a)


base = click_with()
check("風力OFF（既定）で計算できる", not base[4].startswith("エラー") and "風力" not in base[4], base[4][:60])
o = click_with(enabled=True, capacity="500", **{})
_a = list(defaults); _a[station_idx] = sendai; _a[_fac] = "なし"; _a[-13] = True; _a[-11] = "500"
o_nodemand = click.fn(*_a)
check("需要が未設定（施設なし）で風力ON → 需要の設定を促すエラー",
      o_nodemand[4].startswith("エラー") and "需要の設定" in o_nodemand[4], o_nodemand[4][:60])

o = click_with(enabled=True, capacity="500", cf=25.0, ppa=10.0, wheeling="3.0", fee="1.0")
dbg = o[5]
check("風力ON: 完了し風力の節が出る", not o[4].startswith("エラー") and "風力発電（オフサイトPPA）の電力量と費用" in o[4], o[4][:60])
m = re.search(r"風力発電（オフサイトPPA）: (\S+)エリア ([\d,.]+)kW, 設備利用率 ([\d.]+)%, PPA ([\d.]+)円/kWh, 託送\(従量\) ([\d.]+)円/kWh, "
              r"小売グロスマージン ([\d.]+)円/kWh, 損失率 ([\d.]+)%, 発電側課金\((\w+)\) ([\d,]+)円/年, "
              r"バランシング ([\d.]+)円/kWh, 支払の対象=(\S+), 太陽光=(\S+)", dbg)
check("各欄の値が正しい欄に届く（容量500・利用率25・PPA10・託送3・グロスマージン1・損失率5.2・バランシング1.1既定・"
      "支払全量払い既定・発電側課金includedはPPA上書きの自動判定）",
      bool(m) and m.group(1) == "東北" and m.group(2) == "500.0" and m.group(3) == "25.0" and m.group(4) == "10.00"
      and m.group(5) == "3.00" and m.group(6) == "1.00" and m.group(7) == "5.2" and m.group(8) == "included"
      and m.group(10) == "1.10" and m.group(11) == "全量払い" and m.group(12) == "使用",
      str(m.groups() if m else dbg[-300:]))
# 2026-09-22 発見の回帰テスト: wind_ppa_input は gr.Number（未操作でも既定値11.96を送る。gr.Textbox の
# 空欄=None とは違う）。ppa_overridden の判定を "is not None" にすると、PPA欄を触らなくても常に真になり、
# 「自動」が常に「含む」に固定されてしまう（実際のUIのdefaults値をそのまま渡すこのテストで検出できる）
o = click_with(enabled=True, capacity="500")
check("PPA欄を触らない（実際のUI既定値=11.96のまま）なら『自動』は加算になる",
      "発電側課金(add)" in o[5], o[5][o[5].find("風力:"):][:250])
check("PPA発電単価の行に出典タグ[公的データの代理値]が出る（未上書き）", "[公的データの代理値]" in o[4])
check("賦課金の出典タグが空にならない（renewable_surchargeがpsに無いフォールバックの不具合）",
      "賦課金 [出典あり]" in o[4], o[4][max(0, o[4].find("使用電力量にかかる託送") - 5):][:200])
check("支払の対象を触らない（既定=全量払い）なら出典タグは既定区分（『入力値』にならない）",
      "全量払い（発電量の全量に払う） [—]" in o[4], o[4][o[4].find("支払の対象"):][:60])

o = click_with(enabled=True, capacity="500", pv=False)
check("『太陽光を使う』OFF → 太陽光=使用しない（風力のみ）", "太陽光=使用しない" in o[5] and "太陽光発電: 使用しない" in o[4])
o = click_with(enabled=True, sizing=app.WIND_SIZING_COVERAGE, coverage="100", capacity="99999")
w_kwh = float(re.search(r"年間発電量（発電端）: ([\d,.]+) kWh/年", o[4]).group(1).replace(",", ""))
demand_kwh = float(re.search(r"年間需要量: ([\d,.]+) kWh/年", o[4]).group(1).replace(",", ""))
check("需要カバー率で指定 → 契約容量の欄（99999）は使わず、風力の年間発電量 = 年間需要 × 100%",
      abs(w_kwh - demand_kwh) < 1.0, f"{w_kwh} vs {demand_kwh}")
o = click_with(enabled=True, capacity="abc")
check("契約容量が数値でない → 日本語のエラー（計算しない）", o[4].startswith("エラー") and "風力発電（オフサイトPPA）の契約容量" in o[4] and o[0] is None, o[4][:60])
o = click_with(enabled=True, capacity="")
check("契約容量が空欄 → エラー", o[4].startswith("エラー") and "契約容量" in o[4])
o = click_with(station=tokyo, enabled=True, capacity="500")
check("需要地が東京 → 風力は使えない旨のエラー", o[4].startswith("エラー") and "北海道・東北" in o[4])
o = click_with(station=tokyo, capacity="abc", coverage="zzz")
check("風力OFFなら風力欄の不正な値は無視（東京でも計算できる）", not o[4].startswith("エラー"))
off_dirty = click_with(capacity="123", cf=99.0, ppa=1.0, wheeling="9", fee="9")
check("風力OFFなら風力欄の値は結果に影響しない（従来と同一）", off_dirty[4] == base[4] and off_dirty[5] == base[5])

# ============================================================
print("\n【3. 表示切替】")
def find_fn(component):
    return [f for f in app.demo.fns.values() if len(f.inputs) >= 1 and f.inputs[0] is component]


en_fn = find_fn(tail[1])
check("『風力を併用する』のチェックで設定欄の表示が切り替わる（変更ハンドラが登録されている）", len(en_fn) >= 1)
if en_fn:
    check("ON → 表示、OFF → 非表示", en_fn[0].fn(True)["visible"] is True and en_fn[0].fn(False)["visible"] is False)
sz_fn = find_fn(tail[2])
check("容量の指定方法の変更ハンドラが登録されている", len(sz_fn) >= 1)
if sz_fn:
    cap_u, cov_u = sz_fn[0].fn(app.WIND_SIZING_CAPACITY)
    cap_u2, cov_u2 = sz_fn[0].fn(app.WIND_SIZING_COVERAGE)
    check("容量を指定 → 契約容量の欄だけ表示、カバー率で指定 → カバー率の欄だけ表示",
          cap_u["visible"] is True and cov_u["visible"] is False and cap_u2["visible"] is False and cov_u2["visible"] is True)
area_fns = [f for f in app.demo.fns.values() if getattr(f.fn, "__name__", "") == "<lambda>" and len(f.inputs) == 3]
check("地点・CSV・契約種別の変更でエリア表示が更新される（3つの変更ハンドラ）", len(area_fns) >= 3, str(len(area_fns)))
if area_fns:
    check("ハンドラの結果はエリア表示（仙台→東北）", "東北" in area_fns[0].fn(sendai, None, "高圧"))

# ============================================================
print("\n【4. グラフ（風力あり: 内訳／風力なし: 従来）】")
o_w = click_with(enabled=True, capacity="300")
mon = o_w[0]
names = [t.name for t in mon.data]
check("月別: 太陽光・風力の積み上げ＋需要・自家消費の4系列", names == ["発電量（太陽光）", "風力発電（オフサイトPPA）（需要地に届く電力量）", "需要量", "自家消費（風力発電（オフサイトPPA）の使用電力量を含む）"], str(names))
pv_y, w_y = np.array(mon.data[0].y), np.array(mon.data[1].y)
st = o_w[6]
check("月別: 風力は太陽光の上に積む（base = 太陽光）", np.allclose(np.array(mon.data[1].base), pv_y))
check("月別: 太陽光＋風力 = 発電量の合計", abs(float(pv_y.sum() + w_y.sum()) - float(st["total_gen_clipped"].sum())) < 1e-6)
day = o_w[1]
dn = [t.name for t in day.data]
check("日別: 合計の『発電量』に、うち太陽光・うち風力の線が加わる", "発電量" in dn and "うち太陽光" in dn and "うち風力発電（オフサイトPPA）（需要地に届く電力量）" in dn, str(dn))
idx = [i for i, md_ in enumerate(st["month_day"]) if md_ == (7, 1)][0]
wt = [t for t in day.data if t.name == "うち風力発電（オフサイトPPA）（需要地に届く電力量）"][0]
check("日別: 風力の線 = その日の風力発電量", np.allclose(wt.y, st["gen_wind"][idx]))
mon0 = base[0]
check("風力なしの月別は従来の系列（発電量・需要量・自家消費）", [t.name for t in mon0.data] == ["発電量", "需要量", "自家消費"],
      str([t.name for t in mon0.data]))
check("風力なしの日別に風力の線はない", not any("風力" in (t.name or "") for t in base[1].data))
# 日付変更の再描画（保存した結果から）も風力の内訳を持つ
redraw = app.make_daily_chart(st, 1, 15, st["sc_result"], st["demand_30min"])
check("日付を変えての再描画（保存した結果から）でも内訳が出る", any(t.name == "うち風力発電（オフサイトPPA）（需要地に届く電力量）" for t in redraw.data))

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
