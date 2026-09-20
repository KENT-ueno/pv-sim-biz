# -*- coding: utf-8 -*-
"""
test_wind_shape.py - 風力発電（オフサイトPPA）の形状データと計算層の検証
====================================================================
設計: docs/wind_design_spec.md
  W0: wind_shape.csv（tools/build_wind_shape.py の出力）と WIND_AREA_META の整合
  W1: 計算層（load_wind_shape / resolve_wind_capacity / build_wind_30min / station_to_wind_area）

実行: python test_wind_shape.py
"""
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
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
# W0: wind_shape.csv
# ============================================================
print("【W0: wind_shape.csv】")
df = pd.read_csv(os.path.join(HERE, "wind_shape.csv"))
check("列は month, day, slot, area_01, area_02",
      list(df.columns) == ["month", "day", "slot", "area_01", "area_02"], str(list(df.columns)))
check("17,520行（365日×48コマ）", len(df) == 365 * 48, f"{len(df)}")
check("欠測なし", int(df.isna().sum().sum()) == 0)

# METPV の月日軸（1/1〜12/31の365日、2/29なし）と1対1で対応する
con = sqlite3.connect(os.path.join(HERE, "radiation.db"))
md_db = list(con.execute(
    "select month,day from radiation where point_no='14163' and element_no=1 order by month,day"))
con.close()
md_csv = list(df.iloc[::48][["month", "day"]].itertuples(index=False, name=None))
check("月日軸が radiation.db（METPV）と一致", md_csv == md_db)
check("slot は各日 0〜47", bool((df["slot"].to_numpy().reshape(365, 48) == np.arange(48)).all()))

for code, meta in app.WIND_AREA_META.items():
    s = df[f"area_{code}"].to_numpy()
    name = meta["name"]
    # CSVは小数6桁なので厳密な1.0にはならない（読み込み側で再正規化する）。丸め誤差の範囲であること
    check(f"{name}: 年平均が1.0（丸め誤差 1e-6 以内）", abs(s.mean() - 1.0) < 1e-6, f"{s.mean() - 1.0:.2e}")
    check(f"{name}: 負値なし", bool((s >= 0).all()))
    check(f"{name}: 形状の最大値が WIND_AREA_META と一致",
          abs(s.max() - meta["shape_max"]) < 0.001, f"csv {s.max():.4f} / meta {meta['shape_max']}")

print("\n【W0: WIND_AREA_META】")
check("エリアは北海道(01)・東北(02)のみ", sorted(app.WIND_AREA_META) == ["01", "02"])
t = app.WIND_AREA_META["02"]
# 設計時（2026-09-20）に実測した東北の値。取得・解釈が変わっていないことの回帰確認
check("東北: 平均 605.3 MW", t["mean_mw"] == 605.3)
check("東北: 最大 2,019.0 MW", t["max_mw"] == 2019.0)
check("東北: 形状の最大値 3.336", t["shape_max"] == 3.336)
check("東北: 出力制御 1.37%", t["curtail_pct"] == 1.37)
for code, meta in app.WIND_AREA_META.items():
    check(f"{meta['name']}: 最大/平均 = 形状の最大値",
          abs(meta["max_mw"] / meta["mean_mw"] - meta["shape_max"]) < 0.005,
          f"{meta['max_mw'] / meta['mean_mw']:.4f} vs {meta['shape_max']}")

print("\n【W0: 設備利用率・PPA単価の既定値（出典: 調達価格等算定委員会 第112回）】")
check("設備利用率の既定 29.1%", app.WIND_CF_DEFAULT_PCT == 29.1)
check("PPA単価の既定 11.96 円/kWh", app.WIND_PPA_PRICE_DEFAULT == 11.96)

# 設計書 §3-3 の実測値（東北の日内・月別・CFごとのクリップ）
print("\n【W0: 東北の形状の性質（設計書 §3-3）】")
s = df["area_02"].to_numpy()
hour = df["slot"].to_numpy() // 2
month = df["month"].to_numpy()
diurnal = [s[hour == h].mean() for h in range(24)]
check("日内はほぼ平坦（0.90〜1.10）", 0.90 <= min(diurnal) and max(diurnal) <= 1.10,
      f"{min(diurnal):.2f}〜{max(diurnal):.2f}")
mon = [s[month == m].mean() for m in range(1, 13)]
check("冬に強く夏に弱い: 2月 > 1.8、7月 < 0.5",
      mon[1] > 1.8 and mon[6] < 0.5, f"2月{mon[1]:.2f} / 7月{mon[6]:.2f}")
check("CF29.1% ならピークが定格以内（クリップなし）", 0.291 * s.max() <= 1.0, f"{0.291 * s.max():.3f}")
check("CF35.8% は定格を超える（クリップが効く）", 0.358 * s.max() > 1.0, f"{0.358 * s.max():.3f}")

# ============================================================
# W1: 計算層
# ============================================================
print("\n" + "=" * 70)
print("【W1: 地点 → エリア対応】")
con = sqlite3.connect(os.path.join(HERE, "radiation.db"))
stations = {r[0]: r[1] for r in con.execute("select point_no, point_name from points")}
con.close()
mapped = {pn for pn in stations if app.station_to_wind_area(pn) is not None}
check("対応表の地点はすべて radiation.db に存在する", set(app.WIND_STATION_AREA) <= set(stations))
check("対象は8地点（札幌＋東北6県＋新潟）", len(mapped) == 8, str(sorted(stations[p] for p in mapped)))
check("札幌のみ北海道(01)",
      [stations[p] for p in mapped if app.station_to_wind_area(p) == "01"] == ["SAPPORO"])
check("東北(02)は青森・秋田・盛岡・仙台・山形・福島・新潟",
      sorted(stations[p] for p in mapped if app.station_to_wind_area(p) == "02")
      == sorted(["AOMORI", "AKITA", "MORIOKA", "SENDAI", "YAMAGATA", "FUKUSHIMA", "NIIGATA"]))
check("新潟は東北電力NWの供給区域（東北6県＋新潟）なので東北(02)", app.station_to_wind_area("54232") == "02")
check("東京・大阪・那覇・富山などは対象外（None）",
      all(app.station_to_wind_area(p) is None for p in ("44132", "62078", "91197", "55102")))
check("数値で渡しても引ける", app.station_to_wind_area(14163) == "01")
check("None・未知の地点は None", app.station_to_wind_area(None) is None and app.station_to_wind_area("00000") is None)

print("\n【W1: load_wind_shape】")
for code, meta in app.WIND_AREA_META.items():
    sh = app.load_wind_shape(code)
    check(f"{meta['name']}: 形状は (365, 48)", sh.shape == (365, 48), str(sh.shape))
    check(f"{meta['name']}: 年平均が厳密に 1.0（1e-12 以内）", abs(sh.mean() - 1.0) < 1e-12, f"{sh.mean() - 1.0:.1e}")
    check(f"{meta['name']}: 最大値が WIND_AREA_META と一致（0.001 以内）", abs(sh.max() - meta["shape_max"]) < 0.001)
a = app.load_wind_shape("02")
a[:] = 0.0
check("戻り値を書き換えても次回の読み込みに影響しない（コピー）", app.load_wind_shape("02").mean() > 0.99)
check("エリアコードの前後の空白は許容", app.load_wind_shape(" 02 ").shape == (365, 48))
for bad in ("03", "", None, 2):
    try:
        app.load_wind_shape(bad)
        check(f"対象外エリア {bad!r} はエラー", False)
    except ValueError as e:
        check(f"対象外エリア {bad!r} はエラー", "対象外" in str(e))

print("\n【W1: resolve_wind_capacity】")
check("容量指定: そのまま", app.resolve_wind_capacity(app.WIND_SIZING_CAPACITY, capacity_kw=10000) == 10000.0)
check("指定方法が None でも容量指定として扱う", app.resolve_wind_capacity(None, capacity_kw=500) == 500.0)
# カバー率100%・需要 87,600 MWh・CF 25% → 87,600,000 / (8760×0.25) = 40,000 kW
cap = app.resolve_wind_capacity(app.WIND_SIZING_COVERAGE, coverage_pct=100, annual_demand_kwh=87_600_000, cf_pct=25)
check("カバー率: 需要 87,600MWh・CF25%・100% → 40,000 kW", abs(cap - 40000.0) < 1e-6, f"{cap}")
cap = app.resolve_wind_capacity(app.WIND_SIZING_COVERAGE, coverage_pct=50, annual_demand_kwh=87_600_000)
check("カバー率: CF省略は既定29.1%を使う", abs(cap - 43_800_000 / (8760 * 0.291)) < 1e-6, f"{cap:.1f}")
check("カバー率は100%超も可", app.resolve_wind_capacity(app.WIND_SIZING_COVERAGE, coverage_pct=150,
                                                      annual_demand_kwh=1e6) > 0)
bad_cases = [
    ("容量が None", dict(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=None)),
    ("容量が 0", dict(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=0)),
    ("容量が負", dict(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=-1)),
    ("容量が NaN", dict(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=float("nan"))),
    ("カバー率が None", dict(sizing_mode=app.WIND_SIZING_COVERAGE, coverage_pct=None, annual_demand_kwh=1e6)),
    ("カバー率が 0", dict(sizing_mode=app.WIND_SIZING_COVERAGE, coverage_pct=0, annual_demand_kwh=1e6)),
    ("年間需要が None", dict(sizing_mode=app.WIND_SIZING_COVERAGE, coverage_pct=100, annual_demand_kwh=None)),
    ("年間需要が 0", dict(sizing_mode=app.WIND_SIZING_COVERAGE, coverage_pct=100, annual_demand_kwh=0)),
    ("設備利用率が 0", dict(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=1000, cf_pct=0)),
    ("設備利用率が 100 超", dict(sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=1000, cf_pct=101)),
    ("指定方法が不明", dict(sizing_mode="???", capacity_kw=1000)),
]
for label, kw in bad_cases:
    try:
        app.resolve_wind_capacity(**kw)
        check(f"不正入力はエラー: {label}", False)
    except ValueError:
        check(f"不正入力はエラー: {label}", True)

print("\n【W1: build_wind_30min】")
# 東北 10,000kW・CF24.6% → 10,000 × 0.246 × 8760 = 21,549,600 kWh（設計時の比較: 約21,550 MWh）
w = app.build_wind_30min("02", 10000, cf_pct=24.6)
check("戻り値の発電量は (365, 48)", w["gen_30min"].shape == (365, 48))
check("年間発電量 = 容量 × 利用率 × 8760h（クリップなし）",
      abs(w["annual_kwh"] - 10000 * 0.246 * 8760) < 1.0, f"{w['annual_kwh']:.1f}")
check("配列の合計 = annual_kwh", abs(float(w["gen_30min"].sum()) - w["annual_kwh"]) < 1e-6)
check("クリップなし（CF24.6%）", w["clipped_kwh"] == 0.0 and w["clipped_pct"] == 0.0)
check("実効設備利用率 = 指定の24.6%", abs(w["effective_cf_pct"] - 24.6) < 1e-9, f"{w['effective_cf_pct']}")
check("発電量は非負", bool((w["gen_30min"] >= 0).all()))
check("1コマは定格 × 0.5h 以下", bool((w["gen_30min"] <= 10000 * 0.5 + 1e-9).all()))
check("既定の設備利用率は29.1%", abs(app.build_wind_30min("02", 1000)["cf_pct"] - 29.1) < 1e-12)
check("東北CF29.1%でもクリップなし（ピーク0.971）", app.build_wind_30min("02", 1000, 29.1)["clipped_kwh"] == 0.0)

# CF35.8%（リプレース実績の平均値）: 東北はピークが定格を超えて頭打ちになる。北海道は超えない
wt = app.build_wind_30min("02", 10000, cf_pct=35.8)
check("東北CF35.8%: クリップが効く", wt["clipped_kwh"] > 0, f"{wt['clipped_kwh']:.0f} kWh")
check("東北CF35.8%: クリップ率は約0.70%（設計時の実測）", abs(wt["clipped_pct"] - 0.70) < 0.05, f"{wt['clipped_pct']:.3f}%")
check("東北CF35.8%: クリップ後も定格を超えない", bool((wt["gen_30min"] <= 10000 * 0.5 + 1e-9).all()))
check("東北CF35.8%: 実効利用率は指定より低い", wt["effective_cf_pct"] < 35.8, f"{wt['effective_cf_pct']:.2f}%")
check("東北CF35.8%: 発電量 + クリップ量 = クリップ前",
      abs(wt["annual_kwh"] + wt["clipped_kwh"] - wt["unclipped_kwh"]) < 1e-6)
wh = app.build_wind_30min("01", 10000, cf_pct=35.8)
check("北海道CF35.8%: クリップなし（ピーク0.979）", wh["clipped_kwh"] == 0.0)

# 容量に比例する（線形）
w2 = app.build_wind_30min("02", 20000, cf_pct=24.6)
check("容量2倍で発電量も2倍", np.allclose(w2["gen_30min"], 2 * w["gen_30min"]))

# 季節性: 東北は冬に強く夏に弱い（月別にばらして確認）
days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
edges = np.cumsum([0] + days)
mon_kwh = [float(w["gen_30min"][edges[i]:edges[i + 1]].sum()) for i in range(12)]
check("東北: 2月の発電量 > 7月の4倍（2月は日数が少ないのに）", mon_kwh[1] > 4 * mon_kwh[6],
      f"2月{mon_kwh[1]:.0f} / 7月{mon_kwh[6]:.0f}")
check("月別の合計 = 年間", abs(sum(mon_kwh) - w["annual_kwh"]) < 1e-3)

# カバー率で決めた容量で、年間発電量が 需要 × カバー率 に一致する（往復）
D = 50_000_000.0
cap = app.resolve_wind_capacity(app.WIND_SIZING_COVERAGE, coverage_pct=80, annual_demand_kwh=D, cf_pct=29.1)
wc = app.build_wind_30min("01", cap, cf_pct=29.1)
check("カバー率80%: 年間発電量が 需要×80% に一致", abs(wc["annual_kwh"] - 0.8 * D) < 1.0,
      f"{wc['annual_kwh']:.0f} vs {0.8 * D:.0f}")

# 型: MCPに載せる値は Python の標準型（numpy型は文字列化される既知の罠）
check("スカラーの戻り値は Python の float / str",
      all(type(w[k]) in (float, str) for k in w if k != "gen_30min"),
      str({k: type(w[k]).__name__ for k in w if k != "gen_30min"}))

for args, label in (((None, 1000), "エリアが None"), (("02", 0), "容量が 0"), (("02", -5), "容量が負"),
                    (("03", 1000), "対象外エリア"), (("02", None), "容量が None")):
    try:
        app.build_wind_30min(*args)
        check(f"不正入力はエラー: {label}", False)
    except ValueError:
        check(f"不正入力はエラー: {label}", True)
try:
    app.build_wind_30min("02", 1000, cf_pct=0)
    check("不正入力はエラー: 設備利用率が 0", False)
except ValueError:
    check("不正入力はエラー: 設備利用率が 0", True)

print("\n" + "=" * 70)
print(f"結果: PASS {n_pass} / FAIL {n_fail}")
print("=" * 70)
sys.exit(0 if n_fail == 0 else 1)
