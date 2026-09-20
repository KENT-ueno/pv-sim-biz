"""風力発電の形状データ（wind_shape.csv）と、エリアのメタ情報を作る開発用スクリプト。

実行時には不要（app.py は生成済みの wind_shape.csv を読むだけ）。
一般送配電事業者「エリア需給実績」の30分値（暦年2025）を取得して、
    風力発電(t) = 風力発電実績 + 風力出力制御量      （＝出力制御前の資源量）
を年平均1.0に正規化して書き出す。設計は docs/wind_design_spec.md §3・§5-1。

使い方:
    python tools/build_wind_shape.py            # ネットから取得して wind_shape.csv を生成
    python tools/build_wind_shape.py --cache DIR # 取得したCSVをDIRに保存（再実行時は再取得しない）

出力（標準出力）に WIND_AREA_META の値を出すので、app.py の定数へ転記する。
"""
import argparse
import io
import os
import sys
import urllib.request

import numpy as np
import pandas as pd

YEAR = 2025  # 暦年。非うるう年なので METPV の365日軸（1/1〜12/31）とそのまま合う

# 出典: 各一般送配電事業者「エリア需給実績」。ファイル名は eria_jukyu_YYYYMM_NN.csv（NNはエリアコード）
AREAS = {
    "01": {
        "name": "北海道",
        "base": "https://www.hepco.co.jp/network/con_service/public_document/supply_demand_results/csv/",
    },
    "02": {
        "name": "東北",
        "base": "https://setsuden.nw.tohoku-epco.co.jp/common/demand/",
    },
}

WIND_COL = "風力発電実績"
CURTAIL_COL = "風力出力制御量"

# 検証値（docs/wind_design_spec.md §3-3。2026-09-20 に実測）
TOHOKU_EXPECTED = {"mean_mw": 605.3, "max_mw": 2019.0, "shape_max": 3.336, "curtail_pct": 1.37}


def fetch(url, cache_dir=None):
    """CSVのバイト列を返す。User-Agent が無いと403になる事業者がある。"""
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, url.rsplit("/", 1)[-1])
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if path:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    return data


def decode(data):
    """文字コードは事業者でまちまち（cp932 / utf-8）。cp932 → utf-8 の順に試す。"""
    for enc in ("cp932", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("cp932 / utf-8 のどちらでもデコードできません")


def parse_month(data):
    """1か月分のCSVを DataFrame にする。ヘッダ行は先頭行ではなく、DATE を含む行。"""
    lines = decode(data).splitlines()
    head = next((i for i, l in enumerate(lines) if "DATE" in l), None)
    if head is None:
        raise ValueError("ヘッダ行（DATE）が見つかりません")
    df = pd.read_csv(io.StringIO("\n".join(lines[head:])))
    # 列は必ず列名で拾う（事業者によって列数・並びが違うため）
    for col in ("DATE", "TIME", WIND_COL, CURTAIL_COL):
        if col not in df.columns:
            raise ValueError(f"列 {col!r} がありません: {list(df.columns)}")
    df = df[["DATE", "TIME", WIND_COL, CURTAIL_COL]]
    # 北海道は月の日数が少ない月を、カンマだけの空行で31日分（1488行）に埋めている。
    # 日付が空の行は詰め物なので捨てる（日付があるのに値が空の行は欠測なので build_area で止める）
    return df.dropna(subset=["DATE"]).reset_index(drop=True)


def build_area(code, cache_dir=None):
    """1エリア分（暦年）の (実績, 制御量, 日付, コマ) を返す。"""
    base = AREAS[code]["base"]
    frames = []
    for m in range(1, 13):
        url = f"{base}eria_jukyu_{YEAR}{m:02d}_{code}.csv"
        frames.append(parse_month(fetch(url, cache_dir)))
    df = pd.concat(frames, ignore_index=True)

    if len(df) != 365 * 48:
        raise ValueError(f"エリア{code}: {len(df)} 行（期待 {365 * 48}）")
    date = pd.to_datetime(df["DATE"])
    if date.dt.year.nunique() != 1 or int(date.dt.year.iloc[0]) != YEAR:
        raise ValueError(f"エリア{code}: 暦年{YEAR}以外の行があります")

    # 欠測は黙って0にしない。数と位置を出して止める
    wind = pd.to_numeric(df[WIND_COL], errors="coerce")
    curtail = pd.to_numeric(df[CURTAIL_COL], errors="coerce")
    bad = int(wind.isna().sum())
    if bad:
        raise ValueError(f"エリア{code}: {WIND_COL} に欠測が {bad} 件あります")
    # 出力制御量が空欄の事業者は0扱い（制御が無いことを空欄で表す）。ただし件数は記録する
    curtail_blank = int(curtail.isna().sum())
    curtail = curtail.fillna(0.0)

    # 日付が 1/1 から連続する365日×48コマであること
    dates = date.dt.strftime("%m-%d").to_numpy().reshape(365, 48)
    if not (dates == dates[:, :1]).all():
        raise ValueError(f"エリア{code}: 1日48コマの並びが崩れています")
    days = dates[:, 0]
    if days[0] != "01-01" or days[-1] != "12-31" or len(set(days)) != 365:
        raise ValueError(f"エリア{code}: 日付が 1/1〜12/31 の365日になっていません")

    return {
        "wind_mw": wind.to_numpy(float),
        "curtail_mw": curtail.to_numpy(float),
        "days": days,
        "curtail_blank": curtail_blank,
    }


def summarize(code, d):
    """正規化形状とメタ情報。単位 [MW平均] は30分の平均電力（電力量は×0.5h）。"""
    total = d["wind_mw"] + d["curtail_mw"]  # 出力制御前（資源量ベース）
    mean_mw = float(total.mean())
    if mean_mw <= 0:
        raise ValueError(f"エリア{code}: 風力の平均が0以下です")
    shape = total / mean_mw
    curtail_pct = float(d["curtail_mw"].sum() / total.sum() * 100)
    meta = {
        "name": AREAS[code]["name"],
        "mean_mw": round(mean_mw, 1),
        "max_mw": round(float(total.max()), 1),
        "curtail_pct": round(curtail_pct, 2),
        "shape_max": round(float(shape.max()), 3),
    }
    return shape, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=None, help="取得したCSVの保存先（再実行時に再取得しない）")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "wind_shape.csv"))
    args = ap.parse_args()

    shapes, metas, days = {}, {}, None
    for code in AREAS:
        d = build_area(code, args.cache)
        shapes[code], metas[code] = summarize(code, d)
        if d["curtail_blank"]:
            print(f"[注意] エリア{code}: {CURTAIL_COL} の空欄 {d['curtail_blank']} 件を0として扱った")
        if days is None:
            days = d["days"]
        elif not (days == d["days"]).all():
            raise ValueError("エリア間で日付軸が一致しません")

    # 出力: month, day, slot, area_01, area_02 （17,520行）
    md = [(int(s[:2]), int(s[3:])) for s in days]
    out = pd.DataFrame({
        "month": np.repeat([m for m, _ in md], 48),
        "day": np.repeat([dd for _, dd in md], 48),
        "slot": np.tile(np.arange(48), 365),
    })
    for code in AREAS:
        out[f"area_{code}"] = np.round(shapes[code], 6)
    out.to_csv(args.out, index=False, float_format="%.6f")

    print(f"wrote {os.path.abspath(args.out)}  rows={len(out)}")
    print("\n# app.py に転記する定数（出典: エリア需給実績 暦年%d、30分値）" % YEAR)
    print("WIND_AREA_META = {")
    for code, m in metas.items():
        print(f'    "{code}": {m!r},')
    print("}")

    # 東北は設計時の実測値と一致すること（取得・解釈の回帰確認）
    t = metas["02"]
    for k, v in TOHOKU_EXPECTED.items():
        if abs(t[k] - v) > max(0.06, abs(v) * 0.001):
            print(f"[不一致] 東北 {k}: {t[k]} （期待 {v}）", file=sys.stderr)
            sys.exit(1)
    print("\n東北の検証値: 一致")


if __name__ == "__main__":
    main()
