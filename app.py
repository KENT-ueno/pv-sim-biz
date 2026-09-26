"""
app.py - 産業用太陽光需給シミュレーター（JIS C 8907準拠）
====================================================
産業用（高圧・特別高圧）太陽光発電＋蓄電池の需給シミュレーション

機能:
  - SQLite DBからの地点データ読み込み（プリセット）
  - CSVアップロードによるカスタム地点対応
  - 最大8面のアレイ設定
  - 30分単位（48コマ/日）の発電量計算
  - pvlibによる傾斜面日射量（POA）変換
  - JIS C 8907準拠の温度補正
  - PCS出力制限（面ごとに個別設定、各面でclip後に合算）
  - 複数施設合算の需要プリセット（マイクログリッド対応）
  - 月別棒グラフ・日別48コマ折れ線グラフ
"""

import os
import sys
import csv
import io
import sqlite3
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import gradio as gr

try:
    import pvlib
    HAS_PVLIB = True
except ImportError:
    HAS_PVLIB = False

try:
    import pulp
    HAS_PULP = True
except ImportError:
    HAS_PULP = False


def _force_utf8_stdio():
    """標準出力をUTF-8に切り替える（Windowsローカル開発用）。

    Windowsの既定コンソールコードページ（日本語環境ではCP932）だと、
    Gradioが `mcp_server=True` の起動時に出力するバナーの絵文字（🔨）で
    UnicodeEncodeError を起こし、`python app.py` がクラッシュする。
    HF Spaces（Linux・UTF-8）では元々問題にならないため、この関数は実質no-opになる。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        enc = (getattr(stream, "encoding", "") or "").lower()
        if stream is None or enc in ("utf-8", "utf8"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # Python 3.7+
        except Exception:
            pass


_force_utf8_stdio()


# === 定数・設定 ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "radiation.db")

G_STC = 1.0  # 標準日射強度 [kW/m2]
NO_DATA = 8888

# === 産業用施設タイプ定義 ===
BUILDING_TYPES = {
    "役所・自治体庁舎": {"file": "comstock_medium_office.csv", "default_area_m2": 4500},
    "公立小学校": {"file": "comstock_primary_school.csv", "default_area_m2": 6900},
    "公立中学校・高校": {"file": "comstock_secondary_school.csv", "default_area_m2": 19600},
    "公立病院": {"file": "comstock_hospital.csv", "default_area_m2": 22400},
    "ホテル": {"file": "comstock_large_hotel.csv", "default_area_m2": 11300},
    "コンビニ・小売店": {"file": "comstock_retail.csv", "default_area_m2": 2300},
}
BUILDING_TYPE_CHOICES = ["なし"] + list(BUILDING_TYPES.keys())
MAX_FACILITIES = 6

# 蓄電池デフォルト値
BATTERY_DEFAULTS = {
    "capacity_kwh": 5.0,        # 蓄電池容量 [kWh]
    "efficiency_pct": 95,        # 充放電効率 [%]（片道、往復≒90%）
    "max_charge_kw": 2.5,       # 最大充電電力 [kW]
    "max_discharge_kw": 2.5,    # 最大放電電力 [kW]
    "soc_min_pct": 20,          # SOC下限 [%]
    "soc_max_pct": 95,          # SOC上限 [%]
}
BATTERY_MODES = ["ルールベース", "最適充放電（LP）", "最適容量探索"]

# 方位 → pvlib用アジマス角（北=0, 時計回り）
ORIENTATION_TO_AZIMUTH = {
    "南": 180, "南西": 225, "西": 270, "北西": 315,
    "北": 0, "北東": 45, "東": 90, "南東": 135,
}

# JIS C 8907 表5 デフォルト補正係数
DEFAULT_KHD = 0.97
DEFAULT_KPD = 0.95
DEFAULT_KPM = 0.94
DEFAULT_KPA = 0.97
DEFAULT_ETA_INO = 0.90
DEFAULT_ALPHA = -0.35  # %/℃
DEFAULT_DELTA_T = 21.5  # ℃（架台設置形）

MAX_FACES = 8

# === 両面パネルデフォルト値（Phase 6） ===
BIFACIAL_DEFAULTS = {
    "bifaciality": 0.75,    # 背面/前面効率比
    "gcr": 0.4,             # 地面被覆率（パネル高÷列間隔）
    "height": 2.0,          # パネル中心地上高 [m]
    "pitch": 5.0,           # 列間隔 [m]
}
ALBEDO_NORMAL = 0.2   # 通常（草・土）
ALBEDO_SNOW = 0.7     # 積雪時（NEDO METPV-20定義値）

# === 高圧電気料金デフォルト値（東京電力EP 高圧電力A） ===
# 参考: https://www.tepco.co.jp/ep/corporate/plan_h/plan06.html
ELECTRICITY_RATE_HV = {  # 高圧（東京電力EP 高圧電力A）
    "basic_charge_per_kw": 1890.00,   # 基本料金単価 [円/kW・月]
    "energy_charge_summer": 19.93,    # 電力量料金 夏季7-9月 [円/kWh]
    "energy_charge_other": 18.77,     # 電力量料金 その他季 [円/kWh]
    "power_factor_pct": 85,           # 力率 [%]（85%=割引なし）
    "fuel_adjustment": 0.00,          # 燃料費調整単価 [円/kWh]
    "renewable_surcharge": 4.18,      # 再エネ賦課金 [円/kWh]（R8年度）
}
ELECTRICITY_RATE_EHV = {  # 特別高圧（東京電力EP 特別高圧電力）
    "basic_charge_per_kw": 1770.00,   # 基本料金単価 [円/kW・月]
    "energy_charge_summer": 18.48,    # 電力量料金 夏季7-9月 [円/kWh]
    "energy_charge_other": 17.47,     # 電力量料金 その他季 [円/kWh]
    "power_factor_pct": 85,           # 力率 [%]（85%=割引なし）
    "fuel_adjustment": 0.00,          # 燃料費調整単価 [円/kWh]
    "renewable_surcharge": 4.18,      # 再エネ賦課金 [円/kWh]（R8年度）
}
SUBSTATION_COST_PER_KVA = 27500  # 受電設備工事費 [円/kVA]

# ============================================================
# 電気料金タリフ（パイロット版: 北海道電力エリア）
# ============================================================
# 出典・調査記録: docs/hokkaido_power_voltage_tariff.md（2026-09-06確認）
#   受電電圧の区分:  北海道電力ネットワーク 託送供給等約款 第13条
#     https://www.hepco.co.jp/network/con_service/stipulation/pdf/r080401_con_supply.pdf
#   料金単価:  北海道電力 標準電圧6,000V向け
#     https://www.hepco.co.jp/business/price/unitprice/unitprice04.html
#              北海道電力 標準電圧30,000V/60,000V向け
#     https://www.hepco.co.jp/business/price/unitprice/unitprice03.html
#
# 【将来の拡張方針】
#   パイロット版は北海道電力のみ。他エリア（東北・東京・…）を追加するときは
#   TARIFFS に電力会社を1階層足すだけで済む構造にしてある。
#   計算関数 calc_electricity_cost() はスカラー単価を受け取るだけで
#   タリフの出自を問わないため、テーブル追加以外のコード変更は不要。
#
# 【注意1: 季時別区分】
#   北海道電力の「一般料金」は電力量料金が年間単一単価であり、
#   東京電力EPのような夏季(7-9月)/その他季の区別を持たない。
#   このため energy_charge_summer と energy_charge_other に同一値を入れている。
#   （季時別が必要な「時間帯別料金」メニューはパイロット版では扱わない）
#
# 【注意2: 低圧を持たない理由】
#   データセンターは最小規模でも数百kW級であり、低圧（50kW未満）で受電することは
#   考えられないため、低圧メニューは意図的に実装しない（設計書 §4-2）。

# 受電電圧区分の境界（託送供給等約款 第13条の標準電圧。契約電力[kW]で判定）
VOLTAGE_CLASS_THRESHOLDS = [
    # (下限kW, 上限kW（未満）, 区分キー)
    (0,      2000,        "hv_6000"),    # 50kW以上2,000kW未満  → 標準電圧 6,000V（実系統 6.6kV）
    (2000,   10000,       "ehv_30000"),  # 2,000kW以上10,000kW未満 → 標準電圧 30,000V（実系統 22/33kV）
    (10000,  float("inf"), "ehv_60000"), # 10,000kW以上          → 標準電圧 60,000V（実系統 66kV）
]

VOLTAGE_CLASS_LABELS = {
    "hv_6000":   "高圧 6,000V（実系統 6.6kV／50kW以上2,000kW未満）",
    "ehv_30000": "特別高圧 30,000V（実系統 22・33kV／2,000kW以上10,000kW未満）",
    "ehv_60000": "特別高圧 60,000V（実系統 66kV／10,000kW以上）",
}

# 共通の従量費目（全国一律・メニュー非依存）
DEFAULT_POWER_FACTOR_PCT = 85     # 力率 [%]（85%=割引なし）
DEFAULT_FUEL_ADJUSTMENT = 0.00    # 燃料費調整単価 [円/kWh]（時期により変動、既定0）
DEFAULT_RENEWABLE_SURCHARGE = 4.18  # 再エネ賦課金 [円/kWh]（R8年度、全国一律）

# 電力会社 → 受電電圧区分 → メニュー
# メニュー種別:  "業務用電力" = オフィス・商業施設等向け
#                "高圧電力"/"特別高圧電力" = 工場等の産業用向け（負荷率の高い需要家に有利）
TARIFFS = {
    "北海道電力": {
        "hv_6000": {
            "高圧電力": {
                "basic_charge_per_kw": 2880.20,
                "energy_charge_summer": 21.62,
                "energy_charge_other": 21.62,
            },
            "業務用電力": {
                "basic_charge_per_kw": 2693.20,
                "energy_charge_summer": 23.40,
                "energy_charge_other": 23.40,
            },
        },
        "ehv_30000": {
            "特別高圧電力": {
                "basic_charge_per_kw": 2696.50,
                "energy_charge_summer": 20.07,
                "energy_charge_other": 20.07,
            },
            "業務用電力": {
                "basic_charge_per_kw": 2630.50,
                "energy_charge_summer": 21.02,
                "energy_charge_other": 21.02,
            },
        },
        "ehv_60000": {
            "特別高圧電力": {
                "basic_charge_per_kw": 2685.50,
                "energy_charge_summer": 20.03,
                "energy_charge_other": 20.03,
            },
            "業務用電力": {
                "basic_charge_per_kw": 2619.50,
                "energy_charge_summer": 20.98,
                "energy_charge_other": 20.98,
            },
        },
    },
}

DEFAULT_UTILITY = "北海道電力"
# DCは24時間稼働で負荷率が非常に高いため、電力量料金が安い産業用メニュー
# （高圧電力／特別高圧電力）を既定とする。業務用電力も選択可（比較用）。
INDUSTRIAL_MENU_BY_CLASS = {
    "hv_6000": "高圧電力",
    "ehv_30000": "特別高圧電力",
    "ehv_60000": "特別高圧電力",
}


# ============================================================
# データセンター需要モデル（設計書 §5）
# ============================================================
# --- 負荷プロファイル方式 ---
# 「IT負荷率（年平均）」を水準、プロファイルを形状として分離する。
# 各プロファイルは年平均=1.0に正規化された形状を返し、水準を掛けて load_factor(t) になる。
PROFILE_CEC = "CEC実測形状（米国商用DC）"
PROFILE_FLAT = "定常（AI学習・ハイパースケール）"
PROFILE_DIURNAL = "日変動（自社サーバー室・業務連動）"
IT_LOAD_PROFILE_MODES = [PROFILE_CEC, PROFILE_FLAT, PROFILE_DIURNAL]

NOISE_LEVELS = ["なし", "低（3〜7%）", "高（12〜18%）"]

PUE_MODES = ["気温連動", "一定"]

# --- 規模プリセット（⚠ 暫定値。公開された定義に基づく数値ではない） ---
# LBNL Shape Maker のREADMEは large/medium/small を用いるが、
# その MW 区分はリポジトリに公開されていない（2026-09-19確認）。
# 下記は業界で一般的に使われる区分をユーザー合意のうえ採用したもの。
CAPACITY_MODES = ["規模プリセット", "IT容量を直接入力", "ラック数×density"]
DC_SIZE_PRESETS = {
    "エッジ": {"it_capacity_kw": 300.0, "note": "0.1〜0.5 MW／基地局併設・地域拠点"},
    "小規模": {"it_capacity_kw": 1000.0, "note": "0.5〜2 MW／企業自社DC・小規模ハウジング"},
    "中規模": {"it_capacity_kw": 5000.0, "note": "2〜20 MW／商用ハウジング・コロケーション"},
    "ハイパースケール": {"it_capacity_kw": 40000.0, "note": "20〜100+ MW／クラウド・AI学習"},
}

# --- 用途プリセット（プロファイルとノイズを一括設定） ---
# ハウジング → CEC実測形状（商用DCのinterval meter由来）
# AI（学習）  → 定常。LBNL "Flat — characteristic of large AI training clusters
#               running continuous batch jobs" ／ PNNL-38601 "Active states … maintain
#               elevated levels for tens of minutes to hours" に対応
DC_WORKLOAD_PRESETS = {
    "ハウジング（コロケーション）": {"profile": PROFILE_CEC, "noise": "低（3〜7%）"},
    "AI（学習中心）": {"profile": PROFILE_FLAT, "noise": "低（3〜7%）"},
    "AI（推論中心）": {"profile": PROFILE_CEC, "noise": "高（12〜18%）"},
    "自社サーバー室": {"profile": PROFILE_DIURNAL, "noise": "高（12〜18%）"},
    "手動設定": None,
}

# ノイズ強度（LBNL Shape Maker: low 3-7% / high 12-18% of baseline）
NOISE_BANDS = {"なし": (0.0, 0.0), "低（3〜7%）": (0.03, 0.07), "高（12〜18%）": (0.12, 0.18)}
NOISE_SEED = 20260919  # 再現性のため固定（同じ入力なら常に同じ結果を返す）

# ラック電力密度の目安 [kW/ラック]（⚠ 参考値。UI表示用で計算のデフォルトではない）
RACK_DENSITY_HINT = "従来型 4〜8 ／ 高密度 10〜20 ／ AI・GPU 40〜130"

# CEC形状で平日/休日を割り当てるための基準年。
# NEDO METPV-20は代表年データで曜日を持たないため、暦を1つ固定する必要がある。
CEC_REFERENCE_YEAR = 2025

DC_DEFAULTS = {
    "it_capacity_kw": 1000.0,   # IT定格容量 [kW]
    "it_load_factor_pct": 80.0,  # IT負荷率（年平均）[%]
    "it_peak_pct": 90.0,        # ピーク負荷率（日変動時）[%]
    "it_bottom_pct": 50.0,      # ボトム負荷率（日変動時）[%]
    "it_peak_hour": 14,         # ピーク時刻（日変動時）[時]
    "pue_const": 1.40,          # PUE一定値
    "n_racks": 100,             # ラック数（ラック数×density方式のとき）
    "kw_per_rack": 10.0,        # ラック電力密度 [kW/ラック]（同上）
}

# --- 需要ソース（run_simulation の分岐キー。UIでは gr.Tab.select → gr.State で保持する） ---
DEMAND_SOURCE_INDUSTRIAL = "industrial"   # 産業用・MG（ComStock需要プリセット・複数施設合算）
DEMAND_SOURCE_DATACENTER = "datacenter"   # データセンター（IT負荷 × PUE）

# UIのDC入力コンポーネントを dc_args 辞書に戻すためのキー。
# build_ui の dc_components と同じ並びにすること（on_click が zip で対応づける）。
DC_INPUT_KEYS = (
    "workload_preset", "capacity_mode", "size_preset", "it_capacity_kw", "n_racks", "kw_per_rack",
    "profile_mode", "noise_level", "it_load_factor_pct", "it_peak_pct", "it_bottom_pct", "it_peak_hour",
    "pue_const",
    "grid_cap_mode", "grid_cap_kw",
)

# --- 風力発電（オフサイトPPA。設計は docs/wind_design_spec.md） ---
# 形状は wind_shape.csv（tools/build_wind_shape.py で生成）。エリアは北海道・東北のみ。
# 出典: 各一般送配電事業者「エリア需給実績」暦年2025（30分値）。風力は「風力発電実績＋風力出力制御量」
# （出力制御前）。mean_mw/max_mw は [MW平均]（30分の平均電力）、curtail_pct は出力制御量の割合 [%]、
# shape_max は年平均1.0に正規化した形状の最大値。値は tools/build_wind_shape.py の出力を転記した
WIND_AREA_META = {
    "01": {'name': '北海道', 'mean_mw': 356.1, 'max_mw': 974.0, 'curtail_pct': 0.07, 'shape_max': 2.735},
    "02": {'name': '東北', 'mean_mw': 605.3, 'max_mw': 2019.0, 'curtail_pct': 1.37, 'shape_max': 3.336},
}
# 設備利用率・PPA単価の既定値。出典: 資源エネルギー庁 調達価格等算定委員会 第112回（2026年1月）
# 「風力発電について」。陸上風力（新設）の設備利用率の想定値（2025年度）／今年度入札の平均落札価格
WIND_CF_DEFAULT_PCT = 29.1
WIND_PPA_PRICE_DEFAULT = 11.96  # 円/kWh

# オフサイト電源（送配電網で届く電源）が届いた電気にも今もかかる費用（設計は wind_design_spec §5-4）。
# 敷地内の太陽光と違い、風力は託送を使うので次の費用は削減されない:
#   託送の電力量料金（従量分）・再エネ賦課金・小売事業者の手数料。契約電力（基本料金）も下がらない。
# 託送の電力量料金単価 [円/kWh]（標準接続送電サービス。2025年10月〜。**消費税等相当額を含む表示のまま**）。
#   出典: 北海道電力ネットワーク「託送供給の料金単価」／東北電力ネットワーク「託送供給等約款の変更届出」
#   （2025-07-29、届出単価）。既存の電気料金欄の税区分は未確認のため補正していない（差は約0.2円/kWh）
WIND_WHEELING_ENERGY_YEN = {
    ("01", "高圧"): 2.28, ("01", "特別高圧"): 1.02,   # 北海道
    ("02", "高圧"): 2.15, ("02", "特別高圧"): 0.97,   # 東北
}
# 小売グロスマージン（旧名 小売手数料）[円/kWh]。旧版は自然エネルギー財団の推定（2023年度・全国平均）
#   だったが、W2f（2026-09-22）で JPEA 報告（2024-06-03）p.5「グロスマージン4.1円/kWh」に置き換えた
#   （R4年度・高圧・太陽光の平均。定義は p.20・p.21・p.25 の注「小売事業者の販管費や需要側インバランスコストを含む」）。
#   **特高は高圧の値を暫定流用**（原資料に特高の値がない）。**風力には暫定**（太陽光・高圧の値を流用）
WIND_RETAIL_FEE_YEN = {"高圧": 4.1, "特別高圧": 4.1}
RETAIL_GROSS_MARGIN_YEN = WIND_RETAIL_FEE_YEN  # 別名（W2f。名前は互換のため WIND_RETAIL_FEE_YEN を残す）

# 発電側課金（系統連系受電課金）[円/kW・月, 円/kWh]。課金対象kWは契約容量（風力の capacity_kw）。
#   出典: 北海道電力NW 託送供給等約款（2026-04-01実施）p.75「110円00銭・0円35銭」
#         東北電力NW 同 p.61「93円04銭・0円29銭」。高圧・特別高圧で同じ単価
GEN_SIDE_CHARGE_YEN = {
    "01": (110.00, 0.35),   # 北海道
    "02": (93.04, 0.29),    # 東北
}
# 損失率 [%]（需要側の契約種別で選ぶ）。到達可能量 = 発電量 × (1 − 損失率/100)。
#   出典: 北海道 約款 p.108「高圧4.7% 特別高圧2.0%」／東北 約款 p.88「高圧5.2% 特別高圧1.9%」
#   式は北海道 約款 p.88「接続供給電力量 × 1/(1−損失率)」
LOSS_RATE_PCT = {
    ("01", "高圧"): 4.7, ("01", "特別高圧"): 2.0,
    ("02", "高圧"): 5.2, ("02", "特別高圧"): 1.9,
}
# 発電バランシング単価 [円/kWh]。出典: JPEA報告（2024-06-03）p.5「発電インバランス1.1円/kWh」
#   （R4年度・高圧・太陽光の平均。風力には暫定。実務では小売・外部事業者が負担する例が多いが合計は変わらない）
OFFSITE_BALANCING_YEN = 1.1

# 出典の区分（誠実さの台帳。設計は wind_design_spec.md §9-3。結果テキスト・MCP・list_wind_areas が共有する）
#   A: 公的データからの代理値／B: 一次資料の公定値／C: 実績調査値・推定値／D: 出典なし・未算入／U: ユーザー入力
PRICE_SOURCE_LABELS = {
    "A": "公的データの代理値", "B": "出典あり", "C": "実績調査値・暫定", "D": "未算入", "U": "入力値",
}
# 項目名 → (既定の出典区分, 説明)。resolve_wind が上書きされた項目を "U" に切り替えて返す
PRICE_SOURCES = {
    "ppa_price": ("A", "調達価格等算定委員会 第112回（2026年1月）陸上風力の入札の平均落札価格。"
                       "相対契約の単価ではない代理値。発電側課金を含まない（同委員会の意見 p.117）"),
    "gen_charge": ("B", "系統連系受電課金（発電側課金）。北海道電力NW／東北電力NW 託送供給等約款"
                        "（2026-04-01実施）p.75・p.61"),
    "gen_charge_discount": ("D", "系統設備効率化割引は接続変電所で決まるため未算入（0円＝割引なしの最大）"),
    "loss_rate": ("B", "北海道 約款 p.108／東北 約款 p.88 の損失率"),
    "balancing": ("C", "JPEA報告（2024-06-03）p.5「発電インバランス1.1円/kWh」（R4年度・高圧・太陽光の平均。"
                       "風力発電（オフサイトPPA）には暫定）"),
    "wheeling": ("B", "北海道 約款 p.53・p.57／東北 約款 p.44・p.47 の託送電力量料金（従量分）"),
    "renewable_surcharge": ("B", "経済産業省 2026年度の再エネ賦課金単価（電気料金設定の値）"),
    "retail_fee": ("C", "JPEA報告（2024-06-03）p.5「グロスマージン4.1円/kWh」（R4年度・高圧・太陽光の平均。"
                        "特高は高圧の値を暫定流用。風力発電（オフサイトPPA）には暫定）"),
    "payment_basis": ("—", "JPEA報告 p.29（完全ミラー47%＝全量払い／部分ミラー53%＝使用量払い）。既定は全量払い"),
    "surplus_settlement": ("—", "余剰精算は入れない（余剰の収入は需要家に入らない。JPEA p.6・p.29）"),
}

# --- 系統受電上限（設計書 §7）。DCタブの入力。上限を守る手段は蓄電池（最適充放電LP） ---
# 日本の受電電圧は契約電力で階層化されている。上限の実質的な候補はその境界そのもの
#   高圧 6.6kV: 2,000kW未満 ／ 22・33kV: 10,000kW未満 ／ それ以上は66kV（154kVは対象外）
# 「〜kW未満」なので、上限は境界の1kW下にする。resolve_voltage_class は2,000kWちょうどを
# 特別高圧に分類するため、LPが上限に張り付いたとき（通常起きる）に意図した区分から外れてしまう。
GRID_CAP_NONE = "制限なし"
GRID_CAP_HV = "高圧6.6kVに収める（2,000kW未満）"
GRID_CAP_EHV33 = "22・33kVに収める（10,000kW未満）"
GRID_CAP_MANUAL = "手入力"
GRID_CAP_MODES = [GRID_CAP_NONE, GRID_CAP_HV, GRID_CAP_EHV33, GRID_CAP_MANUAL]
GRID_CAP_PRESETS_KW = {
    GRID_CAP_HV: VOLTAGE_CLASS_THRESHOLDS[0][1] - 1.0,      # 2,000 → 1,999 kW
    GRID_CAP_EHV33: VOLTAGE_CLASS_THRESHOLDS[1][1] - 1.0,   # 10,000 → 9,999 kW
}
GRID_CAP_MANUAL_DEFAULT_KW = 2000.0  # 「手入力」を選んだときの入力欄の初期値（編集前提の目安。出典のある値ではない）

# CEC 2025 IEPR の受電容量→最大需要の換算係数（§5-6(2)）。
# ⚠ これは「観測された上限」であり典型値ではない。予測用途では安全側だが、
#    受電容量の逆算に使うと必要容量を小さく見積もる方向に働く（保守性の向きが反転する）。
CEC_UTILIZATION_FACTOR = 0.67

# CEC 2025 IEPR 調和モデル由来の時間別ロードファクタ（年間最大=1000 の千分率）
# [月(1-12)][時刻(1-24)]。出典と抽出方法は docs/design_spec.md §5-7 を参照。
CEC_LF_WEEKDAY = (  # 平日: 12ヶ月 × 24時刻
    ( 900,  897,  896,  896,  897,  900,  903,  907,  912,  916,  920,  923,  925,  926,  926,  925,  922,  919,  914,  910,  904,  899,  895,  891),  #  1月
    ( 888,  886,  885,  885,  886,  888,  892,  895,  899,  903,  907,  910,  912,  913,  914,  913,  911,  908,  905,  901,  896,  892,  888,  885),  #  2月
    ( 882,  881,  880,  880,  882,  885,  888,  892,  896,  900,  904,  907,  910,  912,  913,  912,  911,  908,  906,  902,  898,  894,  890,  887),  #  3月
    ( 885,  884,  883,  884,  886,  889,  893,  898,  902,  907,  912,  916,  920,  922,  923,  923,  922,  920,  917,  913,  909,  905,  901,  898),  #  4月
    ( 895,  894,  894,  895,  897,  901,  906,  911,  917,  923,  929,  934,  938,  941,  943,  943,  942,  939,  936,  931,  926,  921,  917,  913),  #  5月
    ( 910,  908,  908,  910,  913,  917,  923,  929,  936,  943,  950,  956,  961,  964,  966,  966,  964,  961,  957,  952,  946,  940,  934,  929),  #  6月
    ( 926,  924,  923,  925,  928,  933,  939,  946,  954,  962,  970,  976,  981,  985,  986,  986,  984,  980,  975,  968,  961,  954,  948,  942),  #  7月
    ( 938,  935,  935,  936,  939,  944,  951,  958,  966,  975,  982,  989,  994,  998,  999,  998,  995,  991,  985,  977,  970,  962,  954,  948),  #  8月
    ( 943,  940,  939,  940,  943,  948,  955,  962,  970,  978,  985,  992,  996,  999, 1000,  999,  995,  990,  984,  976,  968,  960,  952,  946),  #  9月
    ( 940,  937,  936,  937,  939,  944,  949,  956,  963,  970,  977,  983,  987,  989,  989,  988,  984,  979,  972,  965,  957,  949,  941,  935),  # 10月
    ( 930,  927,  926,  926,  928,  932,  937,  942,  949,  955,  960,  965,  968,  970,  970,  968,  964,  960,  953,  946,  939,  932,  926,  920),  # 11月
    ( 915,  912,  911,  911,  913,  916,  920,  924,  930,  935,  939,  943,  946,  947,  946,  945,  942,  938,  932,  926,  920,  914,  908,  904),  # 12月
)

CEC_LF_WEEKEND = (  # 休日: 12ヶ月 × 24時刻
    ( 896,  893,  891,  890,  888,  888,  889,  890,  891,  892,  894,  896,  897,  898,  899,  899,  898,  897,  896,  894,  892,  890,  887,  884),  #  1月
    ( 882,  880,  879,  877,  877,  877,  877,  878,  879,  880,  882,  883,  884,  886,  886,  886,  886,  886,  885,  884,  882,  880,  879,  877),  #  2月
    ( 875,  874,  873,  872,  872,  872,  872,  873,  875,  876,  878,  879,  881,  882,  884,  884,  884,  884,  884,  883,  882,  881,  879,  878),  #  3月
    ( 877,  875,  875,  874,  874,  875,  876,  877,  879,  881,  883,  886,  888,  890,  891,  893,  893,  894,  894,  893,  892,  890,  889,  888),  #  4月
    ( 886,  885,  884,  884,  884,  885,  886,  888,  891,  894,  897,  900,  903,  905,  908,  909,  910,  911,  911,  910,  909,  907,  905,  903),  #  5月
    ( 901,  900,  899,  898,  898,  899,  901,  904,  907,  910,  914,  918,  922,  925,  928,  930,  932,  932,  932,  930,  928,  926,  923,  920),  #  6月
    ( 918,  916,  914,  913,  914,  915,  917,  919,  923,  927,  932,  936,  940,  944,  947,  949,  950,  950,  950,  948,  945,  942,  939,  935),  #  7月
    ( 932,  929,  927,  926,  925,  926,  928,  931,  935,  939,  944,  948,  953,  957,  960,  962,  962,  962,  961,  958,  955,  951,  947,  943),  #  8月
    ( 939,  935,  933,  931,  930,  931,  933,  936,  939,  943,  948,  952,  956,  960,  962,  964,  964,  963,  962,  959,  955,  951,  946,  942),  #  9月
    ( 937,  934,  930,  929,  928,  928,  930,  932,  935,  938,  942,  946,  950,  952,  954,  955,  955,  954,  952,  949,  945,  941,  936,  932),  # 10月
    ( 928,  924,  921,  919,  918,  918,  919,  921,  923,  926,  929,  932,  935,  937,  938,  939,  938,  937,  935,  932,  928,  924,  920,  916),  # 11月
    ( 913,  909,  906,  905,  904,  904,  904,  905,  907,  909,  911,  913,  915,  917,  918,  918,  917,  916,  914,  912,  909,  906,  902,  899),  # 12月
)


# --- 気温連動PUEモデルのパラメータ（設計書 §5-4） ---
# ⚠⚠ 重要: 以下のデフォルトは物理的に妥当なオーダーではあるが、
#     **特定の公表資料に基づく数値ではない（暫定値）**。
#     JDCC・環境省/経産省のDC実態調査でPUE実績と突合し、
#     docs/pue_model.md に出典付きで記録すること（設計書 §5-4・§13）。
PUE_MODEL_DEFAULTS = {
    "alpha": 0.10,      # 電源設備損失率（UPS/PDU/変圧器/照明。IT負荷比）
    "cop_free": 25.0,   # 外気冷房時COP（ファン動力のみ）
    "t_free": 15.0,     # 外気冷房上限温度 [℃]
    "t_full": 20.0,     # 機械式冷凍機フル稼働温度 [℃]
    "cop_ref": 4.5,     # 冷凍機COP（基準外気温時）
    "t_ref": 25.0,      # COP基準外気温 [℃]
    "beta": 0.08,       # COP温度勾配 [/℃]
    "cop_min": 2.0,     # 冷凍機COP下限
    "pue_max": 1.80,    # PUE上限
}

# === 売電制度 ===
SELL_MODES = ["余剰売電", "逆潮流禁止（売電なし）"]
SELL_SCHEMES = ["FIT利用あり", "FIT利用なし"]
FIT_PRICE_EARLY = 19.0    # FIT単価 ～5年目 [円/kWh]
FIT_PRICE_LATE = 8.3      # FIT単価 6～20年目 [円/kWh]
FIT_PRICE_POST = 8.50     # 卒FIT 21年目以降 [円/kWh]
DEFAULT_SELL_PRICE = 8.50  # FIT利用なし [円/kWh]

# === 産業用PV・蓄電池単価 ===
PV_COST_PER_KW = 158000      # PVシステム単価 [円/kW]（産業用）
BATTERY_COST_PER_KWH = 200000  # 蓄電池単価 [円/kWh]（産業用）

# === CO2排出係数 ===
CO2_EMISSION_FACTOR = 0.000431  # t-CO2/kWh（全国平均）

# === 事業モデル（モードA: 自家消費型） ===
BUSINESS_MODELS = ["自己所有", "リース", "PPA"]
DEFAULT_CONTRACT_YEARS = 15   # リース/PPA契約年数
DEFAULT_TARGET_IRR = 10.0     # 事業者目標P-IRR [%]

# === マイクログリッド（モードB） ===
MG_LINE_COST_PER_KM = 30_000_000  # 自営線単価 [円/km]
MG_LINE_DISTANCE_KM = 2.0         # 自営線距離 [km]（デフォルト）
MG_OPEX_RATIO = 2.0               # 年間運営コスト [%]（イニシャル比）
MG_IRR_PERIOD = 20                # P-IRR計算期間 [年]


# ============================================================
# データ読み込み
# ============================================================

def get_station_options():
    """DBから地点一覧を取得してドロップダウン用リストを返す"""
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT point_no, point_name FROM points ORDER BY point_no"
    ).fetchall()
    conn.close()
    return [f"{no} ({name})" for no, name in rows]


def load_from_db(point_no):
    """
    DBから指定地点のGHI(要素1)と気温(要素5)を読み込む。
    Returns: lat, lon, ghi_df, temp_df
    """
    conn = sqlite3.connect(DB_PATH)
    h_cols = ", ".join([f"h{i:02d}" for i in range(1, 25)])

    pt = conn.execute(
        "SELECT lat, lon FROM points WHERE point_no = ?", (point_no,)
    ).fetchone()
    if pt is None:
        conn.close()
        raise ValueError(f"地点 {point_no} がDBに見つかりません")
    lat, lon = pt

    ghi_rows = conn.execute(
        f"SELECT month, day, {h_cols} FROM radiation "
        f"WHERE point_no = ? AND element_no = 1 ORDER BY day_of_year",
        (point_no,)
    ).fetchall()

    temp_rows = conn.execute(
        f"SELECT month, day, {h_cols} FROM radiation "
        f"WHERE point_no = ? AND element_no = 5 ORDER BY day_of_year",
        (point_no,)
    ).fetchall()
    conn.close()

    cols = ["month", "day"] + [f"h{i:02d}" for i in range(1, 25)]
    ghi_df = pd.DataFrame(ghi_rows, columns=cols)
    temp_df = pd.DataFrame(temp_rows, columns=cols)

    # DB内の欠測値(8888)をNaNに変換（CSV読込と同等の処理）
    h_cols = [f"h{i:02d}" for i in range(1, 25)]
    ghi_df[h_cols] = ghi_df[h_cols].replace(8888, np.nan)
    temp_df[h_cols] = temp_df[h_cols].replace(8888, np.nan)

    return lat, lon, ghi_df, temp_df


def load_snow_depth(point_no):
    """
    DBから指定地点の積雪深（要素9）を読み込む。
    Returns:
        snow_df: DataFrame (365行 × 24列: h01-h24) 単位: 1cm、またはデータなしならNone
    """
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    h_cols = ", ".join([f"h{i:02d}" for i in range(1, 25)])
    rows = conn.execute(
        f"SELECT month, day, {h_cols} FROM radiation "
        f"WHERE point_no = ? AND element_no = 9 ORDER BY day_of_year",
        (point_no,)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    cols = ["month", "day"] + [f"h{i:02d}" for i in range(1, 25)]
    snow_df = pd.DataFrame(rows, columns=cols)
    # DB内の欠測値(8888)をNaNに変換
    h_data_cols = [f"h{i:02d}" for i in range(1, 25)]
    snow_df[h_data_cols] = snow_df[h_data_cols].replace(8888, np.nan)
    return snow_df


def build_albedo_series(snow_df):
    """
    積雪深DataFrameからalbedo時系列（30分×365日）を生成する。
    積雪深 > 0 → 0.7（積雪）、それ以外 → 0.2（通常）。
    Returns:
        albedo_flat: np.array (365*48,) — 30分単位のalbedo値
    """
    if snow_df is None:
        return np.full(365 * 48, ALBEDO_NORMAL)

    n_days = len(snow_df)
    albedo_30min = np.full((n_days, 48), ALBEDO_NORMAL)
    h_cols = [f"h{i:02d}" for i in range(1, 25)]

    for i in range(n_days):
        for j, col in enumerate(h_cols):
            val = snow_df.iloc[i][col]
            # 積雪深 > 0 なら積雪albedo（NaN/Noneは通常扱い）
            is_snow = (val is not None and not np.isnan(val) and val > 0)
            albedo_30min[i, j * 2] = ALBEDO_SNOW if is_snow else ALBEDO_NORMAL
            albedo_30min[i, j * 2 + 1] = ALBEDO_SNOW if is_snow else ALBEDO_NORMAL

    return albedo_30min.flatten()


def load_from_csv(file_obj):
    """アップロードされたNEDO CSVファイルからGHIと気温を読み込む。"""
    if hasattr(file_obj, 'name'):
        filepath = file_obj.name
    else:
        filepath = file_obj

    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    hdr = rows[0]
    point_name = hdr[1].strip()
    lat = float(hdr[2]) + float(hdr[3]) / 60.0
    lon = float(hdr[4]) + float(hdr[5]) / 60.0

    ghi_records = []
    temp_records = []
    for row in rows[1:]:
        if not row or len(row) < 33:
            continue
        elem = int(row[0])
        month = int(row[1])
        day = int(row[2])
        hourly = []
        for i in range(24):
            v = float(row[4 + i])
            hourly.append(None if v == NO_DATA else v)
        if elem == 1:
            ghi_records.append([month, day] + hourly)
        elif elem == 5:
            temp_records.append([month, day] + hourly)

    cols = ["month", "day"] + [f"h{i:02d}" for i in range(1, 25)]
    ghi_df = pd.DataFrame(ghi_records, columns=cols)
    temp_df = pd.DataFrame(temp_records, columns=cols)

    return lat, lon, ghi_df, temp_df


# ============================================================
# 需要データ読み込み（産業用・複数施設合算）
# ============================================================

def load_building_demand(building_key, floor_area_m2, count):
    """
    1施設タイプの需要データを読み込み、延床面積×棟数でスケーリング。

    Args:
        building_key: BUILDING_TYPESのキー
        floor_area_m2: 延床面積 [m²]
        count: 棟数
    Returns:
        np.array (365, 48) [kWh/30分] or None
    """
    info = BUILDING_TYPES.get(building_key)
    if info is None:
        return None

    filepath = os.path.join(BASE_DIR, info["file"])
    if not os.path.exists(filepath):
        return None

    df = pd.read_csv(filepath, parse_dates=["timestamp"])
    demand_per_m2 = df["demand_kWh_per_m2"].values

    n_slots = 365 * 48  # 17,520
    if len(demand_per_m2) >= n_slots:
        demand_flat = demand_per_m2[:n_slots]
    else:
        demand_flat = np.zeros(n_slots)
        demand_flat[:len(demand_per_m2)] = demand_per_m2

    # m²あたり原単位 × 延床面積 × 棟数
    demand_flat = demand_flat * floor_area_m2 * count

    return demand_flat.reshape(365, 48)


def load_custom_demand_csv(custom_csv):
    """
    カスタム需要CSVを読み込む（timestamp, demand_kWh の2列）。

    Returns:
        np.array (365, 48) [kWh/30分] or None
    """
    if custom_csv is None:
        return None

    filepath = custom_csv.name if hasattr(custom_csv, 'name') else custom_csv
    df = pd.read_csv(filepath, parse_dates=["timestamp"])

    # demand_kWh列を優先、なければdemand_kWh_per_m2列をそのまま使用
    if "demand_kWh" in df.columns:
        demand_values = df["demand_kWh"].values
    elif "demand_kWh_per_m2" in df.columns:
        demand_values = df["demand_kWh_per_m2"].values
    else:
        return None

    n_slots = 365 * 48
    if len(demand_values) >= n_slots:
        demand_flat = demand_values[:n_slots]
    else:
        demand_flat = np.zeros(n_slots)
        demand_flat[:len(demand_values)] = demand_values

    return demand_flat.reshape(365, 48)


def load_combined_demand(facility_args, num_facilities, custom_csv=None):
    """
    複数施設の需要データを合算する。

    Args:
        facility_args: tuple/list (施設タイプ, 延床面積, 棟数) × MAX_FACILITIES
        num_facilities: 表示中の施設数
        custom_csv: カスタムCSVファイル（追加合算用）
    Returns:
        tuple: (combined, individual_demands)
            combined: np.array (365, 48) or None（需要データなし時）
            individual_demands: list of np.array (365, 48)（個別施設の需要データ）
    """
    combined = None
    individual_demands = []  # 個別施設の需要データを保持（MG基本料金計算用）

    for i in range(int(num_facilities)):
        idx = i * 3
        btype = facility_args[idx]
        area = facility_args[idx + 1]
        count = facility_args[idx + 2]

        if btype is None or btype == "なし":
            continue
        if not area or float(area) <= 0:
            continue
        if not count or int(count) <= 0:
            continue

        demand = load_building_demand(btype, float(area), int(count))
        if demand is not None:
            individual_demands.append(demand)
            if combined is None:
                combined = demand.copy()
            else:
                combined += demand

    # カスタムCSV（追加合算）
    if custom_csv is not None:
        custom_demand = load_custom_demand_csv(custom_csv)
        if custom_demand is not None:
            individual_demands.append(custom_demand)
            if combined is None:
                combined = custom_demand
            else:
                combined += custom_demand

    return combined, individual_demands


# ============================================================
# 30分補間
# ============================================================

def interpolate_to_30min(hourly_values):
    """24コマの毎時値を48コマの30分値に線形補間する。"""
    h = np.array(hourly_values, dtype=float)
    x_hourly = np.arange(0.5, 24.5, 1.0)
    x_30min = np.arange(0.25, 24.25, 0.5)
    result = np.interp(x_30min, x_hourly, h)
    return result


def prepare_30min_data(ghi_df, temp_df):
    """365日分のGHIと気温を30分48コマに補間する。"""
    n_days = len(ghi_df)
    ghi_30min = np.zeros((n_days, 48))
    temp_30min = np.zeros((n_days, 48))
    month_day = []

    h_cols = [f"h{i:02d}" for i in range(1, 25)]

    for i in range(n_days):
        ghi_hourly = ghi_df.iloc[i][h_cols].values.astype(float)
        ghi_hourly = np.nan_to_num(ghi_hourly, nan=0.0)
        ghi_kwh = ghi_hourly * 0.01 / 3.6

        temp_hourly = temp_df.iloc[i][h_cols].values.astype(float)
        temp_hourly = np.nan_to_num(temp_hourly, nan=0.0)
        temp_c = temp_hourly * 0.1

        ghi_30min[i] = interpolate_to_30min(ghi_kwh)
        temp_30min[i] = interpolate_to_30min(temp_c)
        month_day.append((int(ghi_df.iloc[i]["month"]), int(ghi_df.iloc[i]["day"])))

    return ghi_30min, temp_30min, month_day


# ============================================================
# pvlibによるPOA変換
# ============================================================

def compute_poa_30min(ghi_30min, lat, lon, surface_tilt, surface_azimuth,
                      bifacial=False, bifaciality=0.75, gcr=0.4,
                      height=2.0, pitch=5.0, albedo_flat=None):
    """
    30分GHI配列からpvlibを使ってPOA（傾斜面日射量）を計算する。

    Args:
        ghi_30min: np.array (365, 48) [kW/m2] 30分平均日射強度
                   （毎時kWh値を線形補間した値。大きさは平均kWに等しい）
        lat, lon: 緯度経度
        surface_tilt: 傾斜角 [度]
        surface_azimuth: 方位角 [度] (北=0, 時計回り)
        bifacial: 両面パネルモード
        bifaciality: 背面/前面効率比（両面時のみ）
        gcr: 地面被覆率（両面時のみ）
        height: パネル中心地上高 [m]（両面時のみ）
        pitch: 列間隔 [m]（両面時のみ）
        albedo_flat: np.array (365*48,) albedo時系列（両面時のみ）
    Returns:
        poa_30min: np.array (365, 48) [kW/m2] 傾斜面30分平均日射強度
    """
    if not HAS_PVLIB:
        return ghi_30min.copy()

    n_days = ghi_30min.shape[0]
    total_slots = n_days * 48

    times = pd.date_range(
        start="2023-01-01 00:00", periods=total_slots, freq="30min", tz="Asia/Tokyo"
    )

    # kW/m2 → W/m2（実際の日射強度をそのままpvlibに渡す。
    # ×2000にすると晴天指数ktが2倍で評価されErbsの直達/散乱分離が歪む）
    ghi_flat = ghi_30min.flatten() * 1000.0
    ghi_series = pd.Series(ghi_flat, index=times)

    site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")
    solpos = site.get_solarposition(times)

    erbs = pvlib.irradiance.erbs(ghi_series, solpos["zenith"], times)
    dni = erbs["dni"].fillna(0).clip(lower=0)
    dhi = erbs["dhi"].fillna(0).clip(lower=0)

    if bifacial:
        # 両面パネル: infinite_sheds モデル
        from pvlib.bifacial.infinite_sheds import get_irradiance as get_bifacial_irradiance

        albedo_series = pd.Series(
            albedo_flat if albedo_flat is not None else np.full(total_slots, ALBEDO_NORMAL),
            index=times,
        )
        result = get_bifacial_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            solar_zenith=solpos["zenith"],
            solar_azimuth=solpos["azimuth"],
            gcr=gcr,
            height=height,
            pitch=pitch,
            ghi=ghi_series,
            dhi=dhi,
            dni=dni,
            albedo=albedo_series,
            bifaciality=bifaciality,
        )
        poa_global = result["poa_global"].fillna(0).clip(lower=0)
    else:
        # 片面パネル: 従来の get_total_irradiance
        poa_components = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            solar_zenith=solpos["zenith"],
            solar_azimuth=solpos["azimuth"],
            dni=dni,
            ghi=ghi_series,
            dhi=dhi,
        )
        poa_global = poa_components["poa_global"].fillna(0).clip(lower=0)

    poa_kw = poa_global.values / 1000.0  # W/m2 → kW/m2（30分平均）
    poa_30min = poa_kw.reshape(n_days, 48)

    return poa_30min


# ============================================================
# 発電量計算（JIS C 8907準拠・30分48コマ）
# ============================================================

def calculate_generation(
    lat, lon, ghi_df, temp_df,
    faces, KHD, KPD, KPM, KPA, eta_ino,
    alpha_pct, delta_t,
    bifacial=False, bifaciality=0.75, gcr=0.4,
    height=2.0, pitch=5.0, albedo_flat=None,
):
    """メイン発電量計算関数。"""
    K_prime = KHD * KPD * KPM * KPA * eta_ino

    ghi_30min, temp_30min, month_day = prepare_30min_data(ghi_df, temp_df)
    n_days = len(month_day)

    face_poa_list = []
    for face in faces:
        azimuth = face.get("azimuth", ORIENTATION_TO_AZIMUTH.get(face["orientation"], 180))
        tilt = face["tilt"]
        poa = compute_poa_30min(
            ghi_30min, lat, lon, tilt, azimuth,
            bifacial=bifacial, bifaciality=bifaciality,
            gcr=gcr, height=height, pitch=pitch,
            albedo_flat=albedo_flat,
        )
        face_poa_list.append(poa)

    total_gen = np.zeros((n_days, 48))
    face_annual = []

    for f_idx, face in enumerate(faces):
        ppeak = face["ppeak"]
        poa = face_poa_list[f_idx]
        pcs_kw = face.get("pcs_limit_kw")

        tcr = temp_30min + delta_t
        kpt = 1.0 + alpha_pct * (tcr - 25.0) / 100.0
        k_total = K_prime * kpt

        # poaは30分平均kW/m2 → 瞬時出力kW × 0.5h = kWh/30分
        ep_face = k_total * ppeak * poa / G_STC * 0.5
        ep_face = np.clip(ep_face, 0, None)

        if pcs_kw and pcs_kw > 0:
            pcs_limit_30min = pcs_kw * 0.5
            ep_face = np.clip(ep_face, 0, pcs_limit_30min)

        total_gen += ep_face
        face_annual.append(float(np.sum(ep_face)))

    total_gen_clipped = total_gen

    monthly = {}
    for i in range(n_days):
        m = month_day[i][0]
        day_total = np.sum(total_gen_clipped[i])
        monthly[m] = monthly.get(m, 0) + day_total

    annual = sum(monthly.values())

    return {
        "annual": annual,
        "monthly": monthly,
        "face_annual": face_annual,
        "K_prime": K_prime,
        "total_gen_clipped": total_gen_clipped,
        "month_day": month_day,
        "n_faces": len(faces),
    }


# ============================================================
# 風力発電（オフサイトPPA。設計は docs/wind_design_spec.md §3・§5-2）
# ============================================================
#   風力発電(t) [kW] = min( 契約容量 × 設備利用率 × 形状(t) , 契約容量 )
# 水準（契約容量・設備利用率）はユーザー入力、形状（年平均=1.0）はエリア需給実績。
# calculate_generation（太陽光）には触れず、独立に作って足す。

WIND_SHAPE_CSV = os.path.join(BASE_DIR, "wind_shape.csv")

# 風力の調達エリアと需要地は同じエリアに限る（越境託送はモデル化しない）。
# 地点→エリアは緯度経度から推定せず、供給区域で明示する。
# 東北エリアは「東北6県＋新潟県」（東北電力ネットワーク）なので NIIGATA を含む
WIND_STATION_AREA = {
    "14163": "01",  # SAPPORO
    "31312": "02",  # AOMORI
    "32402": "02",  # AKITA
    "33431": "02",  # MORIOKA
    "34392": "02",  # SENDAI
    "35426": "02",  # YAMAGATA
    "36126": "02",  # FUKUSHIMA
    "54232": "02",  # NIIGATA
}

WIND_SIZING_CAPACITY = "容量を指定"
WIND_SIZING_COVERAGE = "需要カバー率で指定"
WIND_SIZING_MODES = [WIND_SIZING_CAPACITY, WIND_SIZING_COVERAGE]

# 支払の対象（W2f。設計は wind_design_spec.md §9-4）。全量払い=完全ミラー（既定）／使用量払い=部分ミラー
WIND_PAYMENT_BASIS_GENERATED = "全量払い"
WIND_PAYMENT_BASIS_USED = "使用量払い"
WIND_PAYMENT_BASES = [WIND_PAYMENT_BASIS_GENERATED, WIND_PAYMENT_BASIS_USED]
_PAYMENT_BASIS_INTERNAL = {WIND_PAYMENT_BASIS_GENERATED: "generated", WIND_PAYMENT_BASIS_USED: "used"}

# 発電側課金の扱い（W2f。設計は wind_design_spec.md §9-3）。自動＝PPA単価が既定値のままなら加算・入力すれば含む
WIND_GEN_CHARGE_AUTO = "自動"
WIND_GEN_CHARGE_ADD = "加算"
WIND_GEN_CHARGE_INCLUDED = "含む"
WIND_GEN_CHARGE_MODES = [WIND_GEN_CHARGE_AUTO, WIND_GEN_CHARGE_ADD, WIND_GEN_CHARGE_INCLUDED]
_GEN_CHARGE_MODE_INTERNAL = {WIND_GEN_CHARGE_ADD: "add", WIND_GEN_CHARGE_INCLUDED: "included"}

_wind_shape_cache = {}


def station_to_wind_area(point_no):
    """観測地点番号 → 風力のエリアコード（'01' 北海道 / '02' 東北）。対象外の地点は None。"""
    if point_no is None:
        return None
    return WIND_STATION_AREA.get(str(point_no).strip())


def load_wind_shape(area_code):
    """wind_shape.csv から (365, 48) の形状を返す。年平均は厳密に 1.0。

    CSVは小数6桁で丸めているため、年平均が 1.0 から 1e-8 ほどずれる。読み込み時に再正規化する。
    呼び出し側が書き換えても次回に影響しないよう、コピーを返す。
    """
    code = str(area_code).strip() if area_code is not None else ""
    if code not in WIND_AREA_META:
        raise ValueError(
            f"風力発電（オフサイトPPA）のエリア「{area_code}」は対象外です（対象: "
            + "・".join(f"{m['name']}({c})" for c, m in WIND_AREA_META.items()) + "）")
    if code not in _wind_shape_cache:
        df = pd.read_csv(WIND_SHAPE_CSV)
        if len(df) != 365 * 48:
            raise ValueError(f"wind_shape.csv の行数が想定と違います: {len(df)}（期待 {365 * 48}）")
        col = f"area_{code}"
        if col not in df.columns:
            raise ValueError(f"wind_shape.csv に列 {col} がありません")
        s = df[col].to_numpy(dtype=float).reshape(365, 48)
        s = s / s.mean()
        s.setflags(write=False)
        _wind_shape_cache[code] = s
    return _wind_shape_cache[code].copy()


def resolve_wind_capacity(sizing_mode, capacity_kw=None, coverage_pct=None,
                          annual_demand_kwh=None, cf_pct=None):
    """風力の契約容量 [kW] を決める。

    容量を指定: capacity_kw をそのまま使う。
    需要カバー率で指定: 年間の風力発電量が 年間需要 × カバー率 になる容量。
        capacity = 年間需要 × カバー率 / (8760h × 設備利用率)
    カバー率は100%を超えてよい（24/7の検討では需要以上を調達することがある）。
    """
    cf = WIND_CF_DEFAULT_PCT if cf_pct is None else cf_pct
    if not (0 < cf <= 100):
        raise ValueError(f"風力発電（オフサイトPPA）の設備利用率は0より大きく100以下で指定してください（{cf}）")
    if sizing_mode == WIND_SIZING_COVERAGE:
        if coverage_pct is None or not coverage_pct > 0:
            raise ValueError("風力発電（オフサイトPPA）の需要カバー率は0より大きい値で指定してください")
        if annual_demand_kwh is None or not annual_demand_kwh > 0:
            raise ValueError("風力発電（オフサイトPPA）の需要カバー率で容量を決めるには、年間需要が必要です")
        return float(annual_demand_kwh) * (coverage_pct / 100.0) / (8760.0 * cf / 100.0)
    if sizing_mode not in (None, WIND_SIZING_CAPACITY):
        raise ValueError(f"風力発電（オフサイトPPA）の容量の指定方法「{sizing_mode}」は不明です（{' / '.join(WIND_SIZING_MODES)}）")
    if capacity_kw is None or not capacity_kw > 0:
        raise ValueError("風力発電（オフサイトPPA）の契約容量は0より大きい値で指定してください")
    return float(capacity_kw)


def build_wind_30min(area_code, capacity_kw, cf_pct=None):
    """風力の30分発電量（365, 48）[kWh/30分] と内訳を返す。契約容量でクリップし、クリップ量も返す。

    設備利用率が高いと、形状のピーク時に容量×利用率×形状が定格を超える（東北で約30.0%超、北海道で約36.6%超）。
    そのときは定格で頭打ちにし、頭打ち分を clipped_kwh として報告する（黙って捨てない）。
    """
    cf = WIND_CF_DEFAULT_PCT if cf_pct is None else cf_pct
    if not (0 < cf <= 100):
        raise ValueError(f"風力発電（オフサイトPPA）の設備利用率は0より大きく100以下で指定してください（{cf}）")
    if capacity_kw is None or not capacity_kw > 0:
        raise ValueError("風力発電（オフサイトPPA）の契約容量は0より大きい値で指定してください")
    capacity_kw = float(capacity_kw)
    shape = load_wind_shape(area_code)
    power_kw = capacity_kw * (cf / 100.0) * shape          # クリップ前の瞬時出力 [kW]
    clipped_power = np.minimum(power_kw, capacity_kw)
    gen = clipped_power * 0.5                               # kW × 0.5h = kWh/30分
    unclipped_kwh = float(power_kw.sum() * 0.5)
    annual_kwh = float(gen.sum())
    code = str(area_code).strip()
    return {
        "gen_30min": gen,
        "annual_kwh": annual_kwh,
        "unclipped_kwh": unclipped_kwh,
        "clipped_kwh": unclipped_kwh - annual_kwh,
        "clipped_pct": (unclipped_kwh - annual_kwh) / unclipped_kwh * 100.0,
        "effective_cf_pct": annual_kwh / (capacity_kw * 8760.0) * 100.0,
        "area_code": code,
        "area_name": WIND_AREA_META[code]["name"],
        "capacity_kw": capacity_kw,
        "cf_pct": float(cf),
    }


# UIの風力入力を wind_args 辞書に戻すためのキー（DC_INPUT_KEYS と同じ手口）。
# 末尾5つは W2f-3 で追加（既存の並びは変えない。配線テストで守る）
WIND_INPUT_KEYS = ("enabled", "sizing_mode", "capacity_kw", "coverage_pct", "cf_pct", "ppa_price",
                   "wheeling_yen", "retail_fee_yen",
                   "payment_basis", "gen_charge_mode", "gen_charge_discount_yen", "balancing_yen",
                   "loss_rate_pct")


def resolve_wind(wind_args, point_no, annual_demand_kwh, station_label=None, contract_type="高圧"):
    """風力の入力を検証し、発電量と支払額まで解決する。無効なら None。

    風力の調達エリアは需要地（観測地点）のエリアと同じでなければならない（越境託送はモデル化しない）。
    エリアは地点から決まるので、入力には持たない。

    wind_args: WIND_INPUT_KEYS をキーとする辞書
        enabled       : 風力を使うか
        sizing_mode   : WIND_SIZING_CAPACITY / WIND_SIZING_COVERAGE
        capacity_kw   : 契約容量 [kW]（容量を指定のとき）
        coverage_pct  : 需要カバー率 [%]（需要カバー率で指定のとき）
        cf_pct        : 設備利用率 [%]（省略で WIND_CF_DEFAULT_PCT）
        ppa_price     : PPA単価 [円/kWh]（省略で WIND_PPA_PRICE_DEFAULT）
        wheeling_yen  : 託送の電力量料金単価 [円/kWh]（省略でエリア・契約種別ごとの WIND_WHEELING_ENERGY_YEN）
        retail_fee_yen: 小売グロスマージン [円/kWh]（省略で契約種別ごとの WIND_RETAIL_FEE_YEN。旧名 小売手数料）
        payment_basis           : 支払の対象（WIND_PAYMENT_BASIS_GENERATED/_USED。省略で全量払い）
        gen_charge_mode         : 発電側課金の扱い（WIND_GEN_CHARGE_AUTO/_ADD/_INCLUDED。省略で自動）
        gen_charge_discount_yen : 系統設備効率化割引 [円/年]（省略で0＝未算入）
        balancing_yen           : 発電バランシング単価 [円/kWh]（省略で OFFSITE_BALANCING_YEN）
        loss_rate_pct           : 損失率 [%]（省略でエリア・契約種別ごとの LOSS_RATE_PCT）
    returns: build_wind_30min の結果に ppa_price / wheeling_yen / retail_fee_yen / deliverable_30min /
        loss_rate / gen_charge_yen / gen_charge_mode / balancing_yen / payment_basis / price_sources /
        payment_yen（全量払いのときだけ確定値。使用量払いは None＝offsite_payment で届いた量から計算）
        を足した辞書。オフサイト電源として経済性に渡す（source_name / delivered は run_simulation が足す）
    """
    if not wind_args or not wind_args.get("enabled"):
        return None
    label = station_label or (str(point_no) if point_no is not None else "")
    if point_no is None:
        raise ValueError("風力発電（オフサイトPPA）は観測地点（DB）を選んだときだけ使えます。"
                         "CSVアップロードでは需要地が北海道・東北かを確認できないためです")
    area = station_to_wind_area(point_no)
    if area is None:
        raise ValueError(f"風力発電（オフサイトPPA）は北海道・東北の地点でのみ使えます（選択中: {label}）。"
                         "他エリアの需要地に対する越境託送はモデル化していません")
    cf = wind_args.get("cf_pct")
    capacity_kw = resolve_wind_capacity(
        wind_args.get("sizing_mode"), capacity_kw=wind_args.get("capacity_kw"),
        coverage_pct=wind_args.get("coverage_pct"), annual_demand_kwh=annual_demand_kwh, cf_pct=cf)
    ppa = wind_args.get("ppa_price")
    ppa = WIND_PPA_PRICE_DEFAULT if ppa is None else ppa
    if not (ppa >= 0 and np.isfinite(ppa)):
        raise ValueError(f"風力発電（オフサイトPPA）のPPA単価は0以上の数値で指定してください（{ppa}）")
    ct = "特別高圧" if contract_type == "特別高圧" else "高圧"
    wheeling = wind_args.get("wheeling_yen")
    wheeling = WIND_WHEELING_ENERGY_YEN[(area, ct)] if wheeling is None else wheeling
    if not (wheeling >= 0 and np.isfinite(wheeling)):
        raise ValueError(f"風力発電（オフサイトPPA）の託送料金（電力量料金）は0以上の数値で指定してください（{wheeling}）")
    fee = wind_args.get("retail_fee_yen")
    fee = WIND_RETAIL_FEE_YEN[ct] if fee is None else fee
    if not (fee >= 0 and np.isfinite(fee)):
        raise ValueError(f"風力発電（オフサイトPPA）の小売グロスマージンは0以上の数値で指定してください（{fee}）")

    # --- W2f: 損失率・発電側課金・発電バランシング・支払の対象（設計は wind_design_spec.md §9） ---
    loss_pct = wind_args.get("loss_rate_pct")
    loss_pct = LOSS_RATE_PCT[(area, ct)] if loss_pct is None else loss_pct
    if not (np.isfinite(loss_pct) and 0 <= loss_pct < 100):
        raise ValueError(f"風力発電（オフサイトPPA）の損失率は0以上100未満の数値で指定してください（{loss_pct}）")
    loss_rate = loss_pct / 100.0

    discount = wind_args.get("gen_charge_discount_yen")
    discount = 0.0 if discount is None else discount
    if not (np.isfinite(discount) and discount >= 0):
        raise ValueError(f"風力発電（オフサイトPPA）の系統設備効率化割引は0以上の数値で指定してください（{discount}）")

    balancing_rate = wind_args.get("balancing_yen")
    balancing_rate = OFFSITE_BALANCING_YEN if balancing_rate is None else balancing_rate
    if not (np.isfinite(balancing_rate) and balancing_rate >= 0):
        raise ValueError(f"風力発電（オフサイトPPA）の発電バランシング単価は0以上の数値で指定してください（{balancing_rate}）")

    pb_label = wind_args.get("payment_basis")
    pb_label = WIND_PAYMENT_BASIS_GENERATED if pb_label is None else pb_label
    if pb_label not in WIND_PAYMENT_BASES:
        raise ValueError(f"風力発電（オフサイトPPA）の支払の対象は {WIND_PAYMENT_BASES} のいずれかで指定してください（{pb_label}）")

    gc_label = wind_args.get("gen_charge_mode")
    gc_label = WIND_GEN_CHARGE_AUTO if gc_label is None else gc_label
    if gc_label not in WIND_GEN_CHARGE_MODES:
        raise ValueError(f"風力発電（オフサイトPPA）の発電側課金の扱いは {WIND_GEN_CHARGE_MODES} のいずれかで指定してください（{gc_label}）")
    # 「PPA単価が既定値のまま」かどうかは値そのもので判定する（§9-3）。UIの入力欄が gr.Number
    # （未操作でも既定値11.96を送る。gr.Textbox の空欄=None とは違う）なので、"is not None" では
    # 常に真になってしまい「自動」が機能しない（未操作でも常に「含む」判定になる不具合。2026-09-22 発見）
    ppa_overridden = float(ppa) != WIND_PPA_PRICE_DEFAULT
    if gc_label == WIND_GEN_CHARGE_AUTO:
        gc_internal = "included" if ppa_overridden else "add"
    else:
        gc_internal = _GEN_CHARGE_MODE_INTERNAL[gc_label]

    w = build_wind_30min(area, capacity_kw, cf)
    w["source_name"] = "風力発電（オフサイトPPA）"
    w["ppa_price"] = float(ppa)
    w["wheeling_yen"] = float(wheeling)
    w["retail_fee_yen"] = float(fee)
    w["contract_type"] = ct
    w["loss_rate"] = loss_rate
    w["deliverable_30min"] = w["gen_30min"] * (1.0 - loss_rate)
    base_rate, energy_rate = GEN_SIDE_CHARGE_YEN[area]
    gen_charge_raw = capacity_kw * base_rate * 12.0 + w["annual_kwh"] * energy_rate - float(discount)
    w["gen_charge_yen"] = max(0.0, gen_charge_raw)
    w["gen_charge_discount_yen"] = float(discount)
    w["gen_charge_mode"] = gc_internal
    w["gen_charge_mode_label"] = gc_label
    # 「自動」が PPA単価の入力によって「含む」になったか（結果に注記を出すため。§9-3）
    w["gen_charge_auto_included"] = gc_label == WIND_GEN_CHARGE_AUTO and ppa_overridden
    w["balancing_yen"] = float(balancing_rate)
    w["payment_basis"] = _PAYMENT_BASIS_INTERNAL[pb_label]
    w["payment_basis_label"] = pb_label
    w["price_sources"] = {
        "ppa_price": "U" if ppa_overridden else PRICE_SOURCES["ppa_price"][0],
        "gen_charge": PRICE_SOURCES["gen_charge"][0],
        "gen_charge_discount": "U" if wind_args.get("gen_charge_discount_yen") is not None else PRICE_SOURCES["gen_charge_discount"][0],
        "loss_rate": "U" if wind_args.get("loss_rate_pct") is not None else PRICE_SOURCES["loss_rate"][0],
        "balancing": "U" if wind_args.get("balancing_yen") is not None else PRICE_SOURCES["balancing"][0],
        "wheeling": "U" if wind_args.get("wheeling_yen") is not None else PRICE_SOURCES["wheeling"][0],
        "retail_fee": "U" if wind_args.get("retail_fee_yen") is not None else PRICE_SOURCES["retail_fee"][0],
        # payment_basis も gr.Radio（常に具体的な値を送る）なので、既定値と異なるときだけ "U" にする
        "payment_basis": "U" if pb_label != WIND_PAYMENT_BASIS_GENERATED else PRICE_SOURCES["payment_basis"][0],
    }
    # 全量払いは発電量だけで決まる（delivered_kwh 不要）。使用量払いは届いた量が要るので None にし、
    # run_simulation 側で offsite_payment(w, delivered_kwh) を呼んで確定させる（§9-6）
    w["payment_yen"] = offsite_payment(w, 0.0)["total"] if w["payment_basis"] == "generated" else None
    return w


def offsite_payment(source, delivered_kwh):
    """オフサイト電源の発電側の支払（風力PPA）。設計は wind_design_spec.md §9-2・§9-4。

    ここで計算するのは発電側の支払（PPA単価・発電側課金・発電バランシング）だけで、届いた分にかかる
    託送・再エネ賦課金・小売グロスマージン（unit_extra_yen。offsite_lp_spec/offsite_cost_after が計算）は含まない。
    全量払い: 発電量Gだけで決まる（delivered_kwh は使わない）。使用量払い: 発電側単価を (1−損失率) で
    割り戻し、PPA使用量（delivered_kwh）に掛ける（JPEA の「売電価格＋送電ロス＋発電インバランス」と同じ構造）。
    source: resolve_wind の戻り値（annual_kwh・ppa_price・gen_charge_yen・gen_charge_mode・balancing_yen・
             loss_rate・payment_basis を持つ）
    delivered_kwh: 期間内の PPA使用量の合計 [kWh]（全量払いでは使わない）
    returns: {"basis", "ppa", "gen_charge", "balancing", "loss_part", "total"}（円）。
             loss_part は ppa・gen_charge に含まれる損失の割り戻し分の内訳表示用（total には二重計上しない）。
             **全量払いのとき `gen_charge` は常に生の計算値**（発電側課金の扱いが「含む」で合計に加算しない場合も、
             参考額としてこの値を返す。合計にいくら入っているかは `total` の方だけを見ればよい。2026-09-22、
             Codexの実機検証で「含む」のとき0が返って参考額が分からないと指摘があり修正）。使用量払いのときの
             `gen_charge` は届いた量・損失で調整した**配分後**の額（生の年額とは異なる。§9-6の設計どおり）。
             使用量払いでも「含む」のときは合計に加算しない**配分後の参考額**を返す（2026-09-26 のコードレビュー #2。
             以前は0を返していた）。どちらの支払でも「含む」なら ppa + gen_charge + balancing ≠ total になる

    後方互換: `payment_basis` を持たない簡易な source（W2f 以前の資産・テスト用の最小フィクスチャなど）は、
    既に持っている `payment_yen` をそのまま総額として使う（全量払い扱い。内訳は出さない）
    """
    if "payment_basis" not in source:
        total = float(source.get("payment_yen", 0.0))
        return {"basis": "generated", "ppa": total, "gen_charge": 0.0, "balancing": 0.0,
                "loss_part": 0.0, "total": total}
    G = float(source["annual_kwh"])
    basis = source["payment_basis"]
    gen_charge_active = source["gen_charge_mode"] == "add"
    gen_charge_raw = float(source["gen_charge_yen"])
    gen_charge_total = gen_charge_raw if gen_charge_active else 0.0
    if basis == "used":
        loss = float(source["loss_rate"])
        keep = 1.0 - loss
        ppa_unit = float(source["ppa_price"])
        gc_unit = (gen_charge_total / G) if G > 0 else 0.0
        ppa = delivered_kwh * ppa_unit / keep
        added = delivered_kwh * gc_unit / keep  # 合計に加算する額（「含む」なら0）
        # 表示は常に届いた量で配分した額（「含む」でも合計には入らない参考額。全量払いの生の値と同じ扱い）
        gen_charge = delivered_kwh * ((gen_charge_raw / G) if G > 0 else 0.0) / keep
        balancing = delivered_kwh * float(source["balancing_yen"])
        loss_part = (ppa + added) - delivered_kwh * (ppa_unit + gc_unit)
        total = ppa + added + balancing
    else:
        ppa = G * float(source["ppa_price"])
        added = gen_charge_total  # 合計に加算する額（「含む」なら0）
        gen_charge = gen_charge_raw  # 表示は常に生の計算値（「含む」でも合計には入らない参考額）
        balancing = G * float(source["balancing_yen"])
        loss_part = 0.0
        total = ppa + added + balancing
    return {"basis": basis, "ppa": ppa, "gen_charge": gen_charge, "balancing": balancing,
            "loss_part": loss_part, "total": total}


def offsite_payment_total(sources, delivered_by_source):
    """電源ごとの発電側の支払（offsite_payment の total）の合計 [円/年]。

    delivered_by_source: 電源ごとの届いた量 [kWh/30分]（offsite_receiving の delivered_by_source）。
    全量払いの電源は届いた量によらず一定、使用量払いの電源は届いた量で決まる。グリッドサーチ・最適容量探索の
    段階1で容量ごとに呼ぶ（同じ式を2か所に書かないための共通関数。2026-09-26 のコードレビュー）
    """
    return sum(offsite_payment(s, float(np.sum(d)))["total"] for s, d in zip(sources, delivered_by_source))


GEN_CHARGE_AUTO_INCLUDED_NOTE = (
    "※ 発電側課金の扱いが「自動」で、PPA発電単価に既定値以外が入っているため、「PPA単価に含む」として"
    "計算しています。見積のPPA単価に発電側課金が含まれていない場合は「加算」を選んでください")


def used_basis_surplus_note(surplus_kwh, generation_kwh, retail_margin_yen):
    """使用量払いの注記（§9-4。UI・MCPで同じ文面を使う）。余剰の割合は発電端の余剰 ÷ 発電量。"""
    pct = surplus_kwh / generation_kwh * 100.0 if generation_kwh > 0 else 0.0
    return (f"※ 使用量払いでは、余剰電力量（発電量の{pct:.1f}%）のリスクを小売が負う前提です。"
            f"小売グロスマージン {retail_margin_yen:.1f}円（JPEA R4 の平均。需要に合わせた規模の太陽光が中心）は、"
            "この規模の余剰のリスクを織り込んだ値ではありません。実際の契約では単価が上がる可能性があります")


def offsite_used_unit_yen(source):
    """使用量払いの発電側単価 [円/届いた kWh]（§9-2: (PPA単価＋発電側課金/G)/(1−損失率)＋発電バランシング）。

    offsite_payment の使用量払いの total を届いた量で割った値と同じ。全量払い（発電量だけで決まり、
    届いた量を増やしても支払は増えない）と、payment_basis を持たない簡易な電源では 0。
    """
    if source.get("payment_basis") != "used":
        return 0.0
    G = float(source["annual_kwh"])
    gc = float(source["gen_charge_yen"]) if source["gen_charge_mode"] == "add" else 0.0
    keep = 1.0 - float(source["loss_rate"])
    return (float(source["ppa_price"]) + (gc / G if G > 0 else 0.0)) / keep + float(source["balancing_yen"])


def offsite_lp_surplus_penalty(offsite, dem_flat, gen_flat):
    """使用量払いのLPで、風力を蓄電池に貯める運転に実際の単価を見せるための値（#4、2026-09-26）。

    LPは風力を「来たら使う」（must-take）として、届いた分の単価（託送＋賦課金＋小売GM）だけを課している（§9-4）。
    使用量払いでは、蓄電池なしでも受ける量 base(t) = min(到達可能量, max(0, 需要−太陽光)) を超えて受電すると、
    オフサイトの電気は消費に先に配分される（出なり）ので、超えた分は風力になり、発電側単価も払うことになる。
    そこで風力に余剰があるコマ（到達可能量 > base）では、受電量のうち base を超える分に発電側単価を上乗せする。
    風力の余剰を超えて受電する分（そこからは小売の電気）にも上乗せするので、その部分は割高に見る（保守側の近似）。
    returns: (used_unit [円/kWh], base [kWh/30分], 余剰があるコマの添字のリスト)。全量払いなら (0.0, None, [])
    """
    used_unit = float(offsite.get("used_unit_yen", 0.0))
    if used_unit <= 0:
        return 0.0, None, []
    w_flat = offsite["gen_30min"].flatten()
    base = np.minimum(w_flat, np.maximum(0.0, dem_flat - gen_flat))
    idx = [t for t in range(len(w_flat)) if w_flat[t] > base[t] + 1e-9]
    return used_unit, base, idx


def annual_balance(cost_before, cost_after, off, month_day, rate_params, offsite_payment_yen, sell_revenue):
    """【年間の損得】の数値。A（導入しない場合）から B（導入した場合）への増減を項目ごとに返す（2026-09-26）。

    増減は「支払の変化」（正＝支払が増える、負＝減る）。A + 各項目の和 = B、年間経済メリット = A − B。
    - pv_energy: 太陽光・蓄電池で、小売から買う電力量料金が変わる分（小売の電力量料金の変化全体から、
      オフサイト電源の使用電力量の分を除いた残り）
    - basic: 基本料金の変化（契約電力。オフサイト電源では下がらない）
    - offsite_retail: オフサイト電源の使用電力量の分、小売の電力量料金が減る分（30分ごとの使用電力量 × その月の単価）
    - offsite_extra: 使用電力量にかかる託送・再エネ賦課金・小売グロスマージン
    - offsite_payment: オフサイト電源の支払（PPA発電単価＋発電側課金＋発電バランシング）
    - sell: 売電収入（支払を減らす）
    """
    A = float(cost_before["annual_total"])
    offsite_retail = calc_electricity_cost(off["delivered"], month_day, **rate_params)["annual_energy_charge"]
    extra = float(cost_after.get("offsite_energy_yen", 0.0))
    retail_energy_after = float(cost_after["annual_energy_charge"]) - extra
    items = {
        "pv_energy": retail_energy_after + offsite_retail - float(cost_before["annual_energy_charge"]),
        "basic": float(cost_after["annual_basic"]) - float(cost_before["annual_basic"]),
        "offsite_retail": -offsite_retail,
        "offsite_extra": extra,
        "offsite_payment": float(offsite_payment_yen),
        "sell": -float(sell_revenue),
    }
    B = A + sum(items.values())
    return {
        "A": A, "B": B, "merit": A - B, "items": items,
        "contract_before_kw": float(cost_before["contract_power_kw"]),
        "contract_after_kw": float(cost_after["contract_power_kw"]),
        "merit_pv": -(items["pv_energy"] + items["basic"] + items["sell"]),
        "merit_offsite": -(items["offsite_retail"] + items["offsite_extra"] + items["offsite_payment"]),
    }


def format_annual_balance(bal, pv_used, pv_kw, bat_kwh, wind_kw, used_kwh, no_export):
    """【年間の損得】の結果テキスト（UI）。A から B への増減を1行ずつ並べ、縦に足せば検算できる形にする。"""
    W = "風力発電（オフサイトPPA）"
    it = bal["items"]

    def sg(v):
        # 符号つきの金額。四捨五入して0円なら符号を付けない（-0.0 が「-0 円」と出ないように）
        r = int(round(v))
        return "0" if r == 0 else f"{r:+,}"

    local = "・".join([n for n, on in (("太陽光", pv_used), ("蓄電池", bat_kwh > 0)) if on])
    parts = ([f"太陽光 {pv_kw:,.1f}kW"] if pv_used else []) + [f"{W} {wind_kw:,.1f}kW"] + \
        ([f"蓄電池 {bat_kwh:,.1f}kWh"] if bat_kwh > 0 else [])
    t = "【年間の損得】（導入しない場合 A と、導入した場合 B の年間の支払を比べる）\n"
    t += f"  A. 導入しない場合（需要をすべて小売から買う）: {bal['A']:,.0f} 円/年\n"
    if local:
        verb = "減る" if it["pv_energy"] <= 0 else "増える"
        t += f"     {local}の自家消費で、小売から買う電力量料金が{verb}: {sg(it['pv_energy'])} 円\n"
    cb, ca = bal["contract_before_kw"], bal["contract_after_kw"]
    if ca < cb:
        t += f"     契約電力の低下（{cb:,.1f} → {ca:,.1f} kW）で基本料金が減る: {sg(it['basic'])} 円\n"
    elif ca > cb:
        t += f"     契約電力の上昇（{cb:,.1f} → {ca:,.1f} kW）で基本料金が増える: {sg(it['basic'])} 円\n"
    else:
        t += f"     契約電力は変わらない（{cb:,.1f} kW）ので基本料金も変わらない: {sg(it['basic'])} 円\n"
    t += (f"     {W}の使用電力量（{used_kwh:,.1f} kWh）の分、小売から買う電力量料金が減る: "
          f"{sg(it['offsite_retail'])} 円\n")
    t += f"     {W}の使用電力量にかかる託送・再エネ賦課金・小売グロスマージン: {sg(it['offsite_extra'])} 円\n"
    t += f"     {W}の支払（PPA発電単価＋発電側課金＋発電バランシング）: {sg(it['offsite_payment'])} 円\n"
    if not no_export:
        t += f"     売電収入: {sg(it['sell'])} 円\n"
    t += f"  B. 導入した場合（{' ＋ '.join(parts)}）: {bal['B']:,.0f} 円/年\n"
    sign_note = "マイナス＝導入すると支払が増える" if bal["merit"] < 0 else "プラス＝導入すると支払が減る"
    t += f"  年間経済メリット（A − B）: {bal['merit']:,.0f} 円/年（{sign_note}）\n"
    if local:
        t += f"    うち {local}: {sg(bal['merit_pv'])} 円/年 ／ {W}: {sg(bal['merit_offsite'])} 円/年\n"
    return t


def offsite_source_cost(source, delivered_kwh, renewable_surcharge):
    """オフサイト電源1つの費用の内訳。UI（run_simulation）とMCP（mcp_tools）で共有する（二重実装しない）。

    費用は2階建て（§9-2）: ①発電側の支払（offsite_payment）＋ ②届いた分の費用（託送＋再エネ賦課金＋小売グロスマージン）。
    ②は導入後の電気代（offsite_cost_after）に含まれるので、年間経済メリットから別に引くのは①だけ。
    returns: {"payment": offsite_payment の戻り値, "unit_extra": ②の単価 [円/kWh],
              "delivered_extra": ② [円/年], "total": ①＋② [円/年]}
    """
    pay = offsite_payment(source, delivered_kwh)
    unit_extra = source["wheeling_yen"] + renewable_surcharge + source["retail_fee_yen"]
    delivered_extra = delivered_kwh * unit_extra
    return {"payment": pay, "unit_extra": unit_extra, "delivered_extra": delivered_extra,
            "total": pay["total"] + delivered_extra}


def _opt_float(v, label):
    """UIの任意入力を数値にする。空欄・None は None（既定値を使う）。数値以外・非有限はエラー。

    gr.Number は空欄を表せず（未操作でも0を送る。Gradio 6.26）、方位角欄と同じ理由で、任意入力はテキスト欄にしている。
    """
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", "")
        if v == "":
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{label}「{v}」は数値で入力してください（空欄なら既定値を使います）")
    if not np.isfinite(f):
        raise ValueError(f"{label}「{v}」は有限の数値で入力してください")
    return f


def build_wind_args(enabled, sizing_mode, capacity_txt, coverage_txt, cf_pct, ppa_price, wheeling_txt, fee_txt,
                    payment_basis, gen_charge_mode, gen_charge_discount_txt, balancing_txt, loss_rate_txt):
    """UIの風力入力（WIND_INPUT_KEYS と同じ並び）を wind_args 辞書にする。OFFなら入力を検証しない。"""
    if not enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "sizing_mode": sizing_mode,
        "capacity_kw": _opt_float(capacity_txt, "風力発電（オフサイトPPA）の契約容量"),
        "coverage_pct": _opt_float(coverage_txt, "風力発電（オフサイトPPA）の需要カバー率"),
        "cf_pct": _opt_float(cf_pct, "風力発電（オフサイトPPA）の設備利用率"),
        "ppa_price": _opt_float(ppa_price, "風力発電（オフサイトPPA）のPPA単価"),
        "wheeling_yen": _opt_float(wheeling_txt, "風力発電（オフサイトPPA）の託送料金（電力量料金）"),
        "retail_fee_yen": _opt_float(fee_txt, "風力発電（オフサイトPPA）の小売グロスマージン"),
        "payment_basis": payment_basis,
        "gen_charge_mode": gen_charge_mode,
        "gen_charge_discount_yen": _opt_float(gen_charge_discount_txt, "風力発電（オフサイトPPA）の系統設備効率化割引"),
        "balancing_yen": _opt_float(balancing_txt, "風力発電（オフサイトPPA）の発電バランシング単価"),
        "loss_rate_pct": _opt_float(loss_rate_txt, "風力発電（オフサイトPPA）の損失率"),
    }


def wind_area_note(station_choice, has_csv, contract_type):
    """風力の入力欄の上に出す、調達エリアと既定値（表示のみ。エリアは地点から決まる）。"""
    if has_csv:
        return "⚠ CSVアップロードでは風力発電（オフサイトPPA）を使えません（需要地が北海道・東北かを確認できないため）。観測地点を選んでください"
    if not station_choice:
        return "地点を選ぶと、風力発電（オフサイトPPA）の調達エリアと既定値が決まります"
    area = station_to_wind_area(str(station_choice).split(" ")[0])
    if area is None:
        return (f"⚠ 風力発電（オフサイトPPA）は北海道・東北の地点でのみ使えます（選択中: {station_choice}）。"
                "他エリアの需要地に対する越境託送はモデル化していません")
    ct = "特別高圧" if contract_type == "特別高圧" else "高圧"
    name = WIND_AREA_META[area]["name"]
    base_rate, energy_rate = GEN_SIDE_CHARGE_YEN[area]
    return (f"**調達エリア: {name}**（需要地と同じエリア）　既定: 託送の電力量料金 "
            f"{WIND_WHEELING_ENERGY_YEN[(area, ct)]:.2f} 円/kWh（{name}・{ct}・標準接続送電・税込表示）／"
            f"小売グロスマージン {WIND_RETAIL_FEE_YEN[ct]:.1f} 円/kWh（JPEA・暫定）／"
            f"損失率 {LOSS_RATE_PCT[(area, ct)]:.1f}%／発電側課金 {base_rate:.2f}円/kW月＋{energy_rate:.2f}円/kWh／"
            f"発電バランシング {OFFSITE_BALANCING_YEN:.1f}円/kWh（暫定）")


def monthly_sums(arr_365x48, month_day):
    """(365, 48) の配列を月ごとに合計する（{月: 合計}）。"""
    out = {}
    for i, (m, _d) in enumerate(month_day):
        out[m] = out.get(m, 0.0) + float(np.sum(arr_365x48[i]))
    return out


def matching_rates(gen_30min, demand_30min):
    """蓄電池なしの、量ベース達成率と時間一致率（いずれも%）。

    量ベース達成率 = min(1, 年間発電量 / 年間需要量)    従来の「再エネ100%」の見方
    時間一致率     = 1 − 系統購入量 / 年間需要量       30分ごとに需要を発電で埋められた割合（24/7の実力）
    """
    demand = float(np.sum(demand_30min))
    if demand <= 0:
        return None, None
    volume = min(1.0, float(np.sum(gen_30min)) / demand) * 100.0
    buy = float(np.sum(np.maximum(0.0, demand_30min - gen_30min)))
    return volume, (1.0 - buy / demand) * 100.0


def offsite_receiving(pv_gen, sources, demand_30min, sc_result):
    """オフサイト電源を含む運転結果を、受電点（需要地と系統の接続点）の基準に組み替える。

    蓄電池の運転（収支）は、ルールベース・蓄電池なしでは太陽光とオフサイト電源を合わせた発電で計算済み（sc_result）。
    ここでは料金の計算に要る量を再構成する。蓄電池LPを受電点の基準で解いた結果（offsite_receive を持つ）は、
    LPが決めた受電量・売電・出力抑制をそのまま使う。
    オフサイト電源は送配電網で届くので、需要家は届いた電気も受電点で系統から受ける:
        受電量 R(t) = max(0, 需要 + 充電 − 放電 − 敷地内の太陽光)          ← 契約電力・受電上限の基準
        小売から買う量 = sc_result["import_"]（オフサイトで賄えなかった残り）
        オフサイトの配達量 = R − 小売から買う量（各電源の発電量以下に丸める）
    敷地内の太陽光だけが売電・出力抑制の対象になる（オフサイトの余剰は売電できず、無駄になる）:
        太陽光の余剰 = max(0, 太陽光 + 放電 − 需要 − 充電）
    sources: オフサイト電源のリスト（各要素は gen_30min を持つ辞書）。複数あれば各コマで到達可能量の比に按分する。
        deliverable_30min（到達可能量 = 発電量 × (1−損失率)）があればそれを上限にする（W2f）。
        無ければ（損失0のテスト用の電源など）gen_30min をそのまま使う＝従来どおり
    """
    zeros = np.zeros_like(demand_30min)
    ch = sc_result.get("battery_charge", zeros)
    dis = sc_result.get("battery_discharge", zeros)
    retail = sc_result["import_"]
    if "offsite_receive" in sc_result:
        # 蓄電池LPを受電点の基準で解いた結果（optimize_battery の offsite）。受電量・売電・出力抑制はLPの値をそのまま使う
        # （太陽光を売電しながら風力を受電するような運転がありえるため、収支から逆算しない）
        receive = np.maximum(sc_result["offsite_receive"], retail)
        pv_surplus = sc_result["export"] + sc_result["curtailment"]
    else:
        receive = np.maximum(np.maximum(0.0, demand_30min + ch - dis - pv_gen), retail)  # LPの数値誤差でRが下回らないように
        pv_surplus = np.maximum(0.0, pv_gen + dis - demand_30min - ch)
    deliverable = [s.get("deliverable_30min", s["gen_30min"]) for s in sources]
    total = sum(deliverable)
    delivered = np.clip(receive - retail, 0.0, total)
    share = np.divide(1.0, total, out=np.zeros_like(total), where=total > 0)
    delivered_by_source = [delivered * d * share for d in deliverable]
    # 発電端に換算した余剰・送電ロス（W2f）。損失率が無い電源（loss_rate 省略）は keep=1.0＝従来どおり
    gen_equiv_by_source = []
    for s, dl in zip(sources, delivered_by_source):
        keep = 1.0 - s.get("loss_rate", 0.0)
        gen_equiv_by_source.append(dl / keep if keep > 0 else dl)
    return {
        "receive": receive,
        "retail": retail,
        "delivered": delivered,
        "delivered_by_source": delivered_by_source,
        "wasted_by_source": [s["gen_30min"] - ge for s, ge in zip(sources, gen_equiv_by_source)],
        "loss_by_source": [ge - dl for ge, dl in zip(gen_equiv_by_source, delivered_by_source)],
        "pv_surplus": pv_surplus,
    }


def offsite_lp_sell_price(sell_price, unit_price, w_unit):
    """オフサイト電源ありのLPで、目的関数に使う売電単価 [円/kWh]（実際の売電単価とは別）。

    受電点は買電と売電を相殺して1つの向きの流れになる（買いながら売ることはできない）。LPの変数では
    受電量Rと売電Eを同じコマで同時に正にできてしまい、売電単価が風力の配達分の単価（託送＋賦課金＋手数料）
    や小売の電力量単価を上回ると「風力を受電して太陽光を売る」裁定が生じる（相殺すれば成立しない運転）。
    売電単価をそれらより低く抑えれば、同時に正にする運転は損になり、LPは相殺された運転を選ぶ
    （売電単価≦受電の限界単価のとき、受電量と売電量の関数は凸で、LPが厳密に解ける）。
    等号だと同値のコマで同時に正になりうるので、0.01円/kWh だけ下げて厳密に損にする。
    実際の売電収入・電気代は、返ってきた運転を実際の単価で評価し直す（optimize_battery の opt_annual_cost）。
    """
    return max(0.0, min(float(sell_price), float(w_unit) - 0.01, float(np.min(unit_price)) - 0.01))


def offsite_lp_spec(sources):
    """オフサイト電源のリストから、蓄電池LP（optimize_battery / optimize_battery_capacity）に渡す仕様を作る。

    gen_30min      : オフサイト電源の**到達可能量**の合計 [kWh/30分]（LPの配達分の上限。キー名は変えない。W2f）。
                     deliverable_30min が無い電源（損失0のテスト用など）は gen_30min をそのまま使う
    unit_extra_yen : 配達分に課す託送の電力量料金＋小売グロスマージン [円/kWh]（再エネ賦課金はLP側で電気料金の設定から足す）。
                     電源が複数のときは年間発電量で重み付けした平均（現状の電源は風力だけ）。
                     使用量払いでも must-take のためこの値（§9-4）
    used_unit_yen  : 使用量払いの発電側単価 [円/届いた kWh]（全量払いは0）。LPはこれを、風力に余剰があるコマで
                     蓄電池なしでも受ける量を超えて受電する分にだけ課す（offsite_lp_surplus_penalty。#4）
    sources        : offsite_receiving / offsite_cost_after に渡す元のリスト
    発電側の支払（PPA・発電側課金・発電バランシング）は持たない。使用量払いでは届いた量が決まるまで額が定まらないため、
    運転の結果から offsite_payment_total で計算する（以前は payment_yen を持っていたが、本番のコードは読んでおらず、
    使用量払いで None になる罠だったので削除した。2026-09-26 のコードレビュー）
    """
    deliverable = [s.get("deliverable_30min", s["gen_30min"]) for s in sources]
    total = sum(deliverable)
    annual = [float(np.sum(s["gen_30min"])) for s in sources]
    denom = sum(annual)
    unit = (sum(a * (s["wheeling_yen"] + s["retail_fee_yen"]) for a, s in zip(annual, sources)) / denom
            if denom > 0 else 0.0)
    # 使用量払いの発電側単価（全量払いの電源は0）。LPが風力を貯める運転の判断に使う（offsite_lp_surplus_penalty）
    used_unit = (sum(a * offsite_used_unit_yen(s) for a, s in zip(annual, sources)) / denom if denom > 0 else 0.0)
    return {"gen_30min": total, "unit_extra_yen": unit, "used_unit_yen": used_unit, "sources": sources}


def offsite_cost_after(off, sources, month_day, rate_params):
    """オフサイト電源を含む導入後の電気料金（calc_electricity_cost と同じ形の辞書）。

    - 基本料金・契約電力・月別最大デマンド: 受電量 R の最大で決まる（オフサイトでは下がらない）
    - 電力量料金: 小売から買う分は従来どおり ＋ オフサイトの配達分には
        託送の電力量料金 ＋ 再エネ賦課金 ＋ 小売手数料 がかかる（PPA単価は別に、発電した全量に支払う）
    """
    c = calc_electricity_cost(off["receive"], month_day, **rate_params)
    e = calc_electricity_cost(off["retail"], month_day, **rate_params)
    sur = rate_params["renewable_surcharge"]
    monthly = dict(e["monthly_energy_cost"])
    extra = 0.0
    for s, dl in zip(sources, off["delivered_by_source"]):
        unit = s["wheeling_yen"] + sur + s["retail_fee_yen"]
        for m, kwh in monthly_sums(dl, month_day).items():
            monthly[m] = monthly.get(m, 0.0) + kwh * unit
            extra += kwh * unit
    out = dict(c)
    out["annual_energy_charge"] = e["annual_energy_charge"] + extra
    out["annual_total"] = c["annual_basic"] + out["annual_energy_charge"]
    out["monthly_energy_cost"] = monthly
    out["offsite_energy_yen"] = extra
    return out


def format_247(gen_pv, wind_info, demand_30min, sc_result, month_day, pv_enabled):
    """24/7（時間単位の再エネ一致）の節。風力を使うときの主指標（設計書 §1-2・§5-5）。

    蓄電池なしの参考値（太陽光のみ／風力のみ／合計）と、設定どおり（蓄電池・受電上限を含む）の値を並べる。
    量ベース達成率と時間一致率の差が「年間では足りていても、時間では足りない分」。
    風力は**需要地点に届いた分（到達可能量）**で比べる（発電端の全量ではない。W2f）。
    """
    gen_wind = wind_info.get("deliverable_30min", wind_info["gen_30min"])
    gen_all = gen_pv + gen_wind
    annual_demand = float(sc_result["annual_demand"])
    if annual_demand <= 0:
        return ""
    t = "\n══ 24/7（時間単位の再エネ一致） ══\n"
    t += "【蓄電池なしで比べる（参考）】\n"
    if pv_enabled:
        rows = [("太陽光のみ", gen_pv), ("風力発電（オフサイトPPA）のみ", gen_wind), ("太陽光＋風力発電（オフサイトPPA）", gen_all)]
    else:
        rows = [("風力発電（オフサイトPPA）のみ", gen_wind)]
    for label, g in rows:
        vol, hourly = matching_rates(g, demand_30min)
        t += f"  {label}: 量ベース達成率 {vol:.1f}% ／ 時間一致率 {hourly:.1f}%\n"
    vol_all = min(1.0, float(np.sum(gen_all)) / annual_demand) * 100.0
    hourly_set = (1.0 - float(sc_result["annual_import"]) / annual_demand) * 100.0
    t += "【設定どおり（蓄電池・受電上限を含む）】\n"
    t += f"  量ベース達成率: {vol_all:.1f}%（年間の発電量 ÷ 年間の需要量）\n"
    t += f"  時間一致率: {hourly_set:.1f}%（系統購入 {float(sc_result['annual_import']):,.1f} kWh/年）\n"
    t += (f"  差: {max(0.0, vol_all - hourly_set):.1f} ポイント"
          "（量ベース達成率と時間一致率の差。大きいほど、年間では足りていてもその時間には足りていない）\n")
    if sc_result.get("optimized"):
        t += ("  ※ 蓄電池（最適充放電LP）は電気代を最小にする運転で、時間一致率の最大化を目的にしていません。\n"
              "    一致率は結果として上がった値で、上限を示すものではありません\n")
    t += "【月別（設定どおり）】\n"
    pv_col = f"{'太陽光[kWh]':>13s}" if pv_enabled else ""
    t += f"  {'月':>3s}{pv_col}{'風力発電（オフサイトPPA）[kWh]':>26s}{'需要[kWh]':>12s}{'系統購入[kWh]':>15s}{'時間一致率':>11s}\n"
    m_pv, m_w = monthly_sums(gen_pv, month_day), monthly_sums(gen_wind, month_day)
    m_dem = sc_result["monthly_demand"]
    m_imp = monthly_sums(sc_result["import_"], month_day)
    for m in range(1, 13):
        dem = float(m_dem.get(m, 0.0))
        rate = (1.0 - m_imp.get(m, 0.0) / dem) * 100.0 if dem > 0 else 0.0
        pv_val = f"{m_pv.get(m, 0.0):>13,.0f}" if pv_enabled else ""
        t += (f"  {m:>2d}月{pv_val}{m_w.get(m, 0.0):>26,.0f}"
              f"{dem:>12,.0f}{m_imp.get(m, 0.0):>15,.0f}{rate:>10.1f}%\n")
    if pv_enabled:
        t += ("※ 風力発電（オフサイトPPA）は需要地に届く電力量です。太陽光は平年値（METPV-20）、風力発電（オフサイトPPA）は2025年の実績で、\n"
              "   別のデータです。同じ日に凪と曇天が重なるような日単位・30分単位の同時性は反映されません。\n"
              "   月別・時間帯別の平均的な補完関係を見る目安です\n")
    else:
        t += "※ 風力発電（オフサイトPPA）は需要地に届く電力量です（2025年の実績）\n"
    return t


# ============================================================
# 自家消費計算
# ============================================================

def calculate_self_consumption(generation_30min, demand_30min, month_day, no_export=False):
    """30分コマごとの自家消費・余剰売電・買電を計算する。"""
    self_consumption = np.minimum(generation_30min, demand_30min)
    surplus = np.maximum(0, generation_30min - demand_30min)
    if no_export:
        export_grid = np.zeros_like(surplus)
        curtailment = surplus  # 出力抑制量
    else:
        export_grid = surplus
        curtailment = np.zeros_like(surplus)
    import_grid = np.maximum(0, demand_30min - generation_30min)

    annual_self = float(np.sum(self_consumption))
    annual_export = float(np.sum(export_grid))
    annual_import = float(np.sum(import_grid))
    annual_gen = float(np.sum(generation_30min))
    annual_demand = float(np.sum(demand_30min))

    self_consumption_rate = (annual_self / annual_gen * 100) if annual_gen > 0 else 0
    self_sufficiency_rate = (annual_self / annual_demand * 100) if annual_demand > 0 else 0

    monthly_gen = {}
    monthly_demand = {}
    monthly_self = {}
    monthly_export = {}
    monthly_import = {}
    n_days = len(month_day)
    for i in range(n_days):
        m = month_day[i][0]
        monthly_gen[m] = monthly_gen.get(m, 0) + np.sum(generation_30min[i])
        monthly_demand[m] = monthly_demand.get(m, 0) + np.sum(demand_30min[i])
        monthly_self[m] = monthly_self.get(m, 0) + np.sum(self_consumption[i])
        monthly_export[m] = monthly_export.get(m, 0) + np.sum(export_grid[i])
        monthly_import[m] = monthly_import.get(m, 0) + np.sum(import_grid[i])

    return {
        "self_consumption": self_consumption,
        "export": export_grid,
        "import_": import_grid,
        "curtailment": curtailment,
        "annual_self": annual_self,
        "annual_export": annual_export,
        "annual_import": annual_import,
        "annual_demand": annual_demand,
        "annual_curtailment": float(np.sum(curtailment)),
        "self_consumption_rate": self_consumption_rate,
        "self_sufficiency_rate": self_sufficiency_rate,
        "monthly_gen": monthly_gen,
        "monthly_demand": monthly_demand,
        "monthly_self": monthly_self,
        "monthly_export": monthly_export,
        "monthly_import": monthly_import,
    }


# ============================================================
# 蓄電池シミュレーション
# ============================================================

def simulate_battery(generation_30min, demand_30min, month_day,
                     capacity_kwh, efficiency_pct,
                     max_charge_kw, max_discharge_kw,
                     soc_min_pct, soc_max_pct,
                     no_export=False, offsite_gen=None):
    """蓄電池の充放電シミュレーション（30分×365日）。

    offsite_gen: オフサイト電源（風力）の発電量 (365, 48)。None なら従来どおり（ビット同一）。
        指定すると、generation_30min には敷地内の太陽光だけを渡す（風力は含めない）。蓄電池は太陽光の余剰だけを貯める。
        不足分は 風力（送配電網で届く）→蓄電池→小売 の順に充てる:
          太陽光で賄えない不足 D のうち、風力の配達量 = min(D, 風力の発電量)、蓄電池は風力で賄えなかった残りにだけ放電、
          小売購入 = 残り。受電量 R（契約電力・受電上限の基準）= 風力の配達量 + 小売購入（蓄電池の放電で減る）
        風力を貯めると受電量のピークを押し上げて契約電力が上がるため貯めない。また風力（約9円/kWh）を蓄電池で置き換えると、
        貯めずに売れた太陽光の余剰（FIT等）を捨てるだけで損になるため、放電は風力の後にする（設計書 wind §5-4・W2e）。
        結果に offsite_receive（R）・offsite_delivered を加え、import_ は小売購入、self_consumption は配達分を含める。
    """
    n_days, n_slots = generation_30min.shape
    dt = 0.5

    eff = efficiency_pct / 100.0
    soc_min = capacity_kwh * soc_min_pct / 100.0
    soc_max = capacity_kwh * soc_max_pct / 100.0
    max_charge_per_slot = max_charge_kw * dt
    max_discharge_per_slot = max_discharge_kw * dt

    soc = np.zeros((n_days, n_slots))
    battery_charge = np.zeros((n_days, n_slots))
    battery_discharge = np.zeros((n_days, n_slots))
    self_consumption = np.zeros((n_days, n_slots))
    export_grid = np.zeros((n_days, n_slots))
    import_grid = np.zeros((n_days, n_slots))
    curtailment_arr = np.zeros((n_days, n_slots))

    current_soc = soc_min
    if offsite_gen is not None:
        offsite_receive = np.zeros((n_days, n_slots))
        offsite_delivered = np.zeros((n_days, n_slots))

    for d in range(n_days):
        for s in range(n_slots):
            gen = generation_30min[d, s]
            dem = demand_30min[d, s]

            pv_direct = min(gen, dem)
            surplus = gen - pv_direct
            deficit = dem - pv_direct

            charge = 0.0
            if surplus > 0:
                room = (soc_max - current_soc) / eff
                charge = min(surplus, max_charge_per_slot, max(0, room))
                current_soc += charge * eff
                surplus -= charge

            delivered = 0.0
            if offsite_gen is not None:
                # 風力が先: 太陽光で賄えない不足 deficit に、届いた風力を充てる。
                # 蓄電池は風力で賄えなかった残り（小売から買うはずの分）にだけ放電する。
                # 風力（約9円/kWh）を蓄電池で置き換えると、貯めずに売れた太陽光の余剰（FIT等）を捨てるだけで損になるため
                delivered = min(deficit, offsite_gen[d, s])
                offsite_delivered[d, s] = delivered
                deficit -= delivered

            discharge = 0.0
            if deficit > 0:
                available = (current_soc - soc_min) * eff
                discharge = min(deficit, max_discharge_per_slot, max(0, available))
                current_soc -= discharge / eff
                deficit -= discharge

            current_soc = max(soc_min, min(soc_max, current_soc))

            # 逆潮流禁止時は余剰を出力抑制
            if no_export:
                curtailment_arr[d, s] = surplus
                surplus = 0.0

            soc[d, s] = current_soc
            battery_charge[d, s] = charge
            battery_discharge[d, s] = discharge
            self_consumption[d, s] = pv_direct + discharge + delivered
            export_grid[d, s] = surplus
            import_grid[d, s] = deficit
            if offsite_gen is not None:
                # 送配電網から受ける量 R ＝ 風力の配達分 ＋ 小売購入（蓄電池の放電後）。契約電力・受電上限の基準
                offsite_receive[d, s] = delivered + deficit

    annual_self = float(np.sum(self_consumption))
    annual_export = float(np.sum(export_grid))
    annual_import = float(np.sum(import_grid))
    if offsite_gen is not None:
        # 発電量は太陽光＋風力の合計で示す（オフサイトなしの並びと同じ見せ方）
        generation_30min = generation_30min + offsite_gen
    annual_gen = float(np.sum(generation_30min))
    annual_demand = float(np.sum(demand_30min))
    annual_charge = float(np.sum(battery_charge))
    annual_discharge = float(np.sum(battery_discharge))

    self_consumption_rate = (annual_self / annual_gen * 100) if annual_gen > 0 else 0
    self_sufficiency_rate = (annual_self / annual_demand * 100) if annual_demand > 0 else 0

    monthly_gen = {}
    monthly_demand = {}
    monthly_self = {}
    monthly_export = {}
    monthly_import = {}
    for i in range(n_days):
        m = month_day[i][0]
        monthly_gen[m] = monthly_gen.get(m, 0) + np.sum(generation_30min[i])
        monthly_demand[m] = monthly_demand.get(m, 0) + np.sum(demand_30min[i])
        monthly_self[m] = monthly_self.get(m, 0) + np.sum(self_consumption[i])
        monthly_export[m] = monthly_export.get(m, 0) + np.sum(export_grid[i])
        monthly_import[m] = monthly_import.get(m, 0) + np.sum(import_grid[i])

    result = {
        "self_consumption": self_consumption,
        "export": export_grid,
        "import_": import_grid,
        "soc": soc,
        "battery_charge": battery_charge,
        "battery_discharge": battery_discharge,
        "annual_self": annual_self,
        "annual_export": annual_export,
        "annual_import": annual_import,
        "annual_demand": annual_demand,
        "annual_charge": annual_charge,
        "annual_discharge": annual_discharge,
        "self_consumption_rate": self_consumption_rate,
        "self_sufficiency_rate": self_sufficiency_rate,
        "monthly_gen": monthly_gen,
        "monthly_demand": monthly_demand,
        "monthly_self": monthly_self,
        "monthly_export": monthly_export,
        "monthly_import": monthly_import,
        "battery_capacity": capacity_kwh,
        "curtailment": curtailment_arr,
        "annual_curtailment": float(np.sum(curtailment_arr)),
    }
    if offsite_gen is not None:
        # 受電点の基準の運転（LPの offsite と同じキー。offsite_receiving がそのまま使う）
        result["offsite_receive"] = offsite_receive
        result["offsite_delivered"] = offsite_delivered
    return result


# ============================================================
# 蓄電池最適充放電（PuLP/CBC線形計画法）
# ============================================================

class GridCapInfeasibleError(RuntimeError):
    """系統受電上限を守れず、LPが実行不可能（Infeasible）だったことを表す。

    汎用の最適化失敗（RuntimeError）と区別し、呼び出し側が診断メッセージを返せるようにする。
    """


def optimize_battery(generation_30min, demand_30min, month_day,
                     capacity_kwh, efficiency_pct,
                     max_charge_kw, max_discharge_kw,
                     soc_min_pct, soc_max_pct,
                     basic_charge_per_kw, energy_charge_summer,
                     energy_charge_other, power_factor_pct,
                     fuel_adjustment, renewable_surcharge,
                     sell_price, no_export=False, grid_import_cap_kw=None, offsite=None):
    """蓄電池の最適充放電スケジュールをLP（線形計画法）で求める。

    目的関数: 年間電気代（基本料金＋電力量料金−売電収入）の最小化
    ソルバー: CBC（PuLP同梱）

    grid_import_cap_kw: 系統受電上限 [kW]（None/0=制限なし）。指定すると全コマで
        系統購入電力 ≤ 上限 を制約に加える（設計書 §7）。この場合のみ、年初に蓄電池が空から
        始まる人工的な実行不可能を避けるため「初期SOC＝年末SOC（変数）」の周期条件にする。
        上限を守れないときは GridCapInfeasibleError を送出する。
        None のときは従来のLP（初期SOC=SOC下限固定）とビット単位で同一。
    offsite: オフサイト電源（風力）のLP用の仕様（offsite_lp_spec の戻り値）。None なら従来のLPとビット単位で同一。
        指定すると受電点の基準で最適化する（設計書 wind §5-4・W2d）:
          ・generation_30min には敷地内の太陽光だけを渡す（風力は含めない）
          ・grid_import 変数は「受電量 R」（契約電力・受電上限の基準）になる。そのうち風力の配達分 w
            （0 ≤ w ≤ 風力の発電量、w ≤ R）には託送・賦課金・手数料の単価、残り R−w には小売の電力量単価を課す
          ・売電・出力抑制は敷地内の太陽光の余剰だけ（風力は売電できない）
        戻り値の import_ は小売から買う量（R−w）。受電量 R は offsite_receive、配達分は offsite_delivered
    """
    if not HAS_PULP:
        raise RuntimeError("PuLPがインストールされていません。pip install PuLP を実行してください。")

    n_days, n_slots = generation_30min.shape
    T = n_days * n_slots  # 17,520コマ
    dt = 0.5  # 30分 = 0.5時間
    # 受電上限 [kWh/30分]（制限なしは None → 変数の上限なし）
    cap_kwh_slot = float(grid_import_cap_kw) * dt if grid_import_cap_kw and grid_import_cap_kw > 0 else None

    eff = efficiency_pct / 100.0
    soc_min = capacity_kwh * soc_min_pct / 100.0
    soc_max = capacity_kwh * soc_max_pct / 100.0
    max_charge_per_slot = max_charge_kw * dt
    max_discharge_per_slot = max_discharge_kw * dt

    # 1次元に展開
    gen_flat = generation_30min.flatten()
    dem_flat = demand_30min.flatten()
    month_flat = np.array([month_day[d][0] for d in range(n_days) for _ in range(n_slots)])

    # 時間帯別電力量単価（燃調＋再エネ込み）
    unit_price = np.where(
        (month_flat >= 7) & (month_flat <= 9),
        energy_charge_summer + fuel_adjustment + renewable_surcharge,
        energy_charge_other + fuel_adjustment + renewable_surcharge,
    )

    # === LP定式化 ===
    prob = pulp.LpProblem("BatteryOptimization", pulp.LpMinimize)

    # 決定変数
    charge = [pulp.LpVariable(f"ch_{t}", lowBound=0, upBound=max_charge_per_slot) for t in range(T)]
    discharge = [pulp.LpVariable(f"dc_{t}", lowBound=0, upBound=max_discharge_per_slot) for t in range(T)]
    # 系統購入。受電上限があれば変数の上限にする（peak_demand ≥ 購入/dt なので契約電力も上限以下になる）
    grid_import = [pulp.LpVariable(f"gi_{t}", lowBound=0, upBound=cap_kwh_slot) for t in range(T)]
    if offsite is not None:
        # オフサイト電源: grid_import は受電量 R（受電上限・ピークの基準）。風力の配達分 w は R に含まれ、発電量以下
        w_flat = offsite["gen_30min"].flatten()
        w_unit = offsite["unit_extra_yen"] + renewable_surcharge  # 配達分の託送＋手数料＋再エネ賦課金 [円/kWh]
        delivered = [pulp.LpVariable(f"wd_{t}", lowBound=0, upBound=float(w_flat[t])) for t in range(T)]
        # 使用量払い: 風力に余剰があるコマで、蓄電池なしでも受ける量を超える受電 ex ≥ R − base に発電側単価を課す（#4）
        used_unit, used_base, used_idx = offsite_lp_surplus_penalty(offsite, dem_flat, gen_flat)
        used_ex = {t: pulp.LpVariable(f"wx_{t}", lowBound=0) for t in used_idx}
    if no_export:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0, upBound=0) for t in range(T)]
    else:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0) for t in range(T)]
    # 出力抑制変数（逆潮流禁止時のみPV余剰を捨てる。余剰売電時は0に固定し縮退を防ぐ）
    curtail_ub = None if no_export else 0
    curtailment = [pulp.LpVariable(f"ct_{t}", lowBound=0, upBound=curtail_ub) for t in range(T)]
    soc_var = [pulp.LpVariable(f"soc_{t}", lowBound=soc_min, upBound=soc_max) for t in range(T)]
    peak_demand = pulp.LpVariable("peak_kw", lowBound=0)  # ピークデマンド（kW）
    # 受電上限あり: 初期SOCを変数にして年末SOCと一致させる（周期条件）。上限なしは従来どおり soc_min 固定
    soc_init = pulp.LpVariable("soc_init", lowBound=soc_min, upBound=soc_max) if cap_kwh_slot is not None else soc_min
    # 1台のPCSを充電または放電のどちらかに使う（同時充放電の排他制約用）
    max_power_per_slot = max(max_charge_per_slot, max_discharge_per_slot)

    # 目的関数: 基本料金 + 電力量料金 - 売電収入
    pf_factor = (185 - power_factor_pct) / 100.0
    annual_basic = basic_charge_per_kw * peak_demand * 12 * pf_factor  # 年間基本料金
    if offsite is None:
        annual_energy = pulp.lpSum([grid_import[t] * unit_price[t] for t in range(T)])
    else:
        # 受電量 R のうち、小売から買う分 R−w は電力量単価、風力の配達分 w は託送等の単価
        annual_energy = pulp.lpSum([grid_import[t] * unit_price[t] - delivered[t] * (unit_price[t] - w_unit)
                                    for t in range(T)])
        if used_ex:
            annual_energy += pulp.lpSum([used_ex[t] * used_unit for t in used_idx])
    sell_price_val = sell_price if sell_price is not None else 0
    # オフサイトあり: 買電と売電の相殺を保つため、LP内の売電単価を抑える（offsite_lp_sell_price）。実際の値は後で評価し直す
    sell_lp = sell_price_val if offsite is None else offsite_lp_sell_price(sell_price_val, unit_price, w_unit)
    annual_sell = pulp.lpSum([grid_export[t] * sell_lp for t in range(T)])
    prob += annual_basic + annual_energy - annual_sell

    # 制約条件
    for t in range(T):
        # エネルギーバランス: 系統購入（オフサイトあり: 受電量）+ PV発電 + 放電 = 需要 + 充電 + 系統売電 + 出力抑制
        prob += grid_import[t] + gen_flat[t] + discharge[t] == dem_flat[t] + charge[t] + grid_export[t] + curtailment[t]

        if offsite is None:
            # 売電・出力抑制はPV発電＋放電からのみ（系統買電の即売り＝パススルー禁止。
            # この制約がないと売電単価>買電単価のときLPがUnboundedになる）
            prob += grid_export[t] + curtailment[t] <= gen_flat[t] + discharge[t]
        else:
            # オフサイトあり: 売電・出力抑制の対象はそのコマの太陽光の余剰だけ（風力は売電できない。
            # 放電を含めると、風力を蓄電池に貯めて売電する裁定が生じてしまうため）
            prob += grid_export[t] + curtailment[t] <= gen_flat[t]
            # 風力の配達分は受電量の一部
            prob += delivered[t] <= grid_import[t]
            if t in used_ex:
                prob += used_ex[t] >= grid_import[t] - float(used_base[t])

        # 同時充放電の排他制約（ソフト版）: PCS 1台は充電か放電のどちらか
        prob += charge[t] + discharge[t] <= max_power_per_slot

        # ピークデマンド制約: peak_demand ≥ 系統購入の瞬時電力(kW)
        # grid_import[t]はkWh/30分なので、kWに変換するには÷0.5
        prob += peak_demand >= grid_import[t] / dt

        # SOC遷移（受電上限ありのときの初期SOCは変数 soc_init）
        if t == 0:
            prob += soc_var[t] == soc_init + charge[t] * eff - discharge[t] / eff
        else:
            prob += soc_var[t] == soc_var[t - 1] + charge[t] * eff - discharge[t] / eff

    # 終端SOC制約: 年末SOCを初期SOCに戻す（年次比較の公平性）
    # 上限なし: 初期SOC = soc_min（固定）／ 受電上限あり: 初期SOC = 年末SOC（変数、周期条件）
    prob += soc_var[T - 1] == soc_init

    # === ソルバー実行 ===
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=120)
    prob.solve(solver)

    if prob.status != pulp.constants.LpStatusOptimal:
        if cap_kwh_slot is not None and prob.status == pulp.constants.LpStatusInfeasible:
            raise GridCapInfeasibleError(
                f"受電上限 {float(grid_import_cap_kw):,.0f} kW を守れません（LPが実行不可能）")
        raise RuntimeError(f"最適化に失敗しました（ステータス: {pulp.LpStatus[prob.status]}）")

    # === 結果取得 ===
    charge_vals = np.array([ch.varValue for ch in charge]).reshape(n_days, n_slots)
    discharge_vals = np.array([dc.varValue for dc in discharge]).reshape(n_days, n_slots)
    gi_vals = np.array([gi.varValue for gi in grid_import]).reshape(n_days, n_slots)
    ge_vals = np.array([ge.varValue for ge in grid_export]).reshape(n_days, n_slots)
    soc_vals = np.array([s.varValue for s in soc_var]).reshape(n_days, n_slots)
    if offsite is not None:
        # gi_vals は受電量 R。小売から買う量は R − 風力の配達分（LPの数値誤差で負にならないよう0で下げる）
        recv_vals = gi_vals
        deliv_vals = np.array([w.varValue for w in delivered]).reshape(n_days, n_slots)
        gi_vals = np.maximum(recv_vals - deliv_vals, 0.0)
        # 発電量・月別発電量は太陽光＋風力の合計で示す（オフサイトなしの並びと同じ見せ方）
        generation_30min = generation_30min + offsite["gen_30min"]

    # 自家消費 = 需要 - 系統購入
    self_consumption = demand_30min - gi_vals
    # 出力抑制はLP変数から直接取得
    curtailment_arr = np.array([ct.varValue for ct in curtailment]).reshape(n_days, n_slots)

    # 年間集計
    annual_self = float(np.sum(self_consumption))
    annual_export = float(np.sum(ge_vals))
    annual_import = float(np.sum(gi_vals))
    annual_gen = float(np.sum(generation_30min))
    annual_demand = float(np.sum(demand_30min))
    annual_charge = float(np.sum(charge_vals))
    annual_discharge = float(np.sum(discharge_vals))

    self_consumption_rate = (annual_self / annual_gen * 100) if annual_gen > 0 else 0
    self_sufficiency_rate = (annual_self / annual_demand * 100) if annual_demand > 0 else 0

    # 月別集計
    monthly_gen = {}
    monthly_demand = {}
    monthly_self = {}
    monthly_export = {}
    monthly_import = {}
    for i in range(n_days):
        m = month_day[i][0]
        monthly_gen[m] = monthly_gen.get(m, 0) + np.sum(generation_30min[i])
        monthly_demand[m] = monthly_demand.get(m, 0) + np.sum(demand_30min[i])
        monthly_self[m] = monthly_self.get(m, 0) + np.sum(self_consumption[i])
        monthly_export[m] = monthly_export.get(m, 0) + np.sum(ge_vals[i])
        monthly_import[m] = monthly_import.get(m, 0) + np.sum(gi_vals[i])

    opt_peak_kw = peak_demand.varValue
    opt_cost = pulp.value(prob.objective)
    if offsite is not None:
        # 目的関数の売電は抑えた単価だったので、実際の売電単価で評価し直す（電気代−売電収入）
        opt_cost -= (sell_price_val - sell_lp) * float(np.sum(ge_vals))
        # 使用量払いの上乗せ（#4）は判断用で電気代ではない（実際の支払は offsite_payment で届いた量から計算する）
        opt_cost -= used_unit * float(sum(used_ex[t].varValue for t in used_idx))

    result = {
        "self_consumption": self_consumption,
        "export": ge_vals,
        "import_": gi_vals,
        "soc": soc_vals,
        "battery_charge": charge_vals,
        "battery_discharge": discharge_vals,
        "annual_self": annual_self,
        "annual_export": annual_export,
        "annual_import": annual_import,
        "annual_demand": annual_demand,
        "annual_charge": annual_charge,
        "annual_discharge": annual_discharge,
        "self_consumption_rate": self_consumption_rate,
        "self_sufficiency_rate": self_sufficiency_rate,
        "monthly_gen": monthly_gen,
        "monthly_demand": monthly_demand,
        "monthly_self": monthly_self,
        "monthly_export": monthly_export,
        "monthly_import": monthly_import,
        "battery_capacity": capacity_kwh,
        "curtailment": curtailment_arr,
        "annual_curtailment": float(np.sum(curtailment_arr)),
        "optimized": True,
        "opt_peak_kw": opt_peak_kw,
        "opt_annual_cost": opt_cost,
    }
    if cap_kwh_slot is not None:
        # 受電上限を制約として強制したことを結果に残す（従来の戻り値のキー構成は上限なしでは変えない）
        result["grid_import_cap_kw"] = float(grid_import_cap_kw)
    if offsite is not None:
        # 受電点の基準の運転（オフサイト電源を含む）。契約電力・受電上限・託送等の課金は受電量で見る
        result["offsite_receive"] = recv_vals
        result["offsite_delivered"] = deliv_vals
    return result


# ============================================================
# 系統受電上限の診断（実行不可能の理由と、必要な蓄電池の下限の目安）
# ============================================================

def diagnose_grid_cap(generation_30min, demand_30min, cap_kw, capacity_kwh=0.0,
                      efficiency_pct=95.0, max_charge_kw=0.0, max_discharge_kw=0.0,
                      soc_min_pct=20.0, soc_max_pct=95.0):
    """系統受電上限 cap_kw を守れるかを、LPを解かずに必要条件で診断する（設計書 §7・§13）。

    各コマで PV差引後の負荷 net = 需要 − PV発電 が上限を超える分（need>0）は、その時刻の蓄電池放電で
    賄うしかない。上限を下回る余力（need<0）は、蓄電池を充電する機会になる。この構造から
    次の3つを**必要条件**として判定する（1つでも破れていれば、LPを解くまでもなく確実に守れない）。

      energy   : 年間で見て、超過分 Σneed⁺ が、余力を効率損失込みで貯めた量 η²·Σneed⁻ 以下であること
                 （破れると蓄電池をいくら大きくしても解決しない＝基底負荷が上限に近すぎる）
      power    : 最大の超過 max(need⁺)/0.5h が蓄電池の最大放電電力以下であること
      capacity : 理想的な（充電レート無制限の）SOCの最大ドローダウンが、使用可能容量以下であること

    必要量（required_power_kw / required_usable_kwh）は充放電レートや効率のばらつきを考えない**下限の目安**で、
    実際にはこれ以上必要になりうる。LPが実行不可能でも3条件を満たす場合は、主に充電レート不足など。

    Returns:
        dict（needs_battery=False なら上限は蓄電池なしで守れる）
    """
    dt = 0.5
    eta = efficiency_pct / 100.0
    net = (np.asarray(demand_30min, dtype=float) - np.asarray(generation_30min, dtype=float)).ravel()
    need = net - float(cap_kw) * dt
    npos = np.maximum(need, 0.0)     # 放電が必要な量 [kWh/30分]
    spare = np.maximum(-need, 0.0)   # 上限までの余力 [kWh/30分]
    n_exc = int((npos > 1e-9).sum())

    d = {
        "cap_kw": float(cap_kw),
        "peak_net_kw": float(net.max() / dt),
        "exceed_slots": n_exc,
        "total_slots": int(net.size),
        "exceed_energy_kwh": float(npos.sum()),
        "spare_energy_kwh": float(spare.sum()),
        "needs_battery": n_exc > 0,
        "energy_feasible": True,
        "min_cap_kw_energy": None,
        "required_power_kw": 0.0,
        "required_usable_kwh": 0.0,
        "required_capacity_kwh": 0.0,
        "usable_kwh": float(capacity_kwh) * (soc_max_pct - soc_min_pct) / 100.0,
        "max_discharge_kw": float(max_discharge_kw),
        "violations": [],
    }
    if n_exc == 0:
        return d

    # --- power: 最大の超過を賄う放電電力 ---
    d["required_power_kw"] = float(npos.max() / dt)
    if d["required_power_kw"] > max_discharge_kw * (1.0 + 1e-9):
        d["violations"].append("power")

    # --- energy: 年間の収支 ---
    d["energy_feasible"] = bool(npos.sum() <= eta ** 2 * spare.sum() * (1.0 + 1e-9))
    if not d["energy_feasible"]:
        d["violations"].append("energy")
        # 蓄電池が無限に大きくても必要な上限の下限（energy条件が成り立つ最小の上限）を二分法で求める
        lo, hi = 0.0, float(net.max() / dt)
        for _ in range(80):
            mid = (lo + hi) / 2.0
            lack = np.maximum(net - mid * dt, 0.0).sum() - eta ** 2 * np.maximum(mid * dt - net, 0.0).sum()
            if lack > 0:
                lo = mid
            else:
                hi = mid
        d["min_cap_kw_energy"] = hi
        return d

    # --- capacity: 理想SOCの最大ドローダウン（年をまたぐため2周分で評価） ---
    # 放電 need⁺ でSOCは need⁺/η 減り、余力 need⁻ を全部充電すればSOCは η·need⁻ 増える
    y = np.where(need > 0, -need / eta, spare * eta)
    s = np.concatenate([[0.0], np.cumsum(np.concatenate([y, y]))])
    d["required_usable_kwh"] = float(np.max(np.maximum.accumulate(s) - s))
    soc_width = (soc_max_pct - soc_min_pct) / 100.0
    d["required_capacity_kwh"] = d["required_usable_kwh"] / soc_width if soc_width > 0 else float("inf")
    if d["required_usable_kwh"] > d["usable_kwh"] * (1.0 + 1e-9):
        d["violations"].append("capacity")
    return d


# ============================================================
# 蓄電池最適容量探索（LP一体化 + グリッドサーチ）
# ============================================================

def optimize_battery_capacity(generation_30min, demand_30min, month_day,
                              efficiency_pct, max_charge_kw, max_discharge_kw,
                              soc_min_pct, soc_max_pct,
                              basic_charge_per_kw, energy_charge_summer,
                              energy_charge_other, power_factor_pct,
                              fuel_adjustment, renewable_surcharge,
                              sell_price, battery_cost_per_kwh, payback_years,
                              no_export=False, capacity_upper=2000, offsite=None):
    """段階1: LP一体化で蓄電池の最適容量を求める。

    蓄電池容量を決定変数に含め、年間運用コスト＋蓄電池投資年額換算の
    合計を最小化する。P-IRR最大化の近似探索として機能する。

    offsite: オフサイト電源（風力）のLP用の仕様。指定時は optimize_battery と同じ受電点の基準で解く
    （generation_30min は太陽光のみ。全量払いの風力PPAの支払は容量に依存しない定数なので目的関数に含めない。
    使用量払いは optimize_battery と同じく、蓄電池なしでも受ける量を超える受電に発電側単価を課す。#4）。
    """
    if not HAS_PULP:
        raise RuntimeError("PuLPがインストールされていません。")

    n_days, n_slots = generation_30min.shape
    T = n_days * n_slots
    dt = 0.5

    eff = efficiency_pct / 100.0
    max_charge_per_slot = max_charge_kw * dt
    max_discharge_per_slot = max_discharge_kw * dt

    gen_flat = generation_30min.flatten()
    dem_flat = demand_30min.flatten()
    month_flat = np.array([month_day[d][0] for d in range(n_days) for _ in range(n_slots)])

    unit_price = np.where(
        (month_flat >= 7) & (month_flat <= 9),
        energy_charge_summer + fuel_adjustment + renewable_surcharge,
        energy_charge_other + fuel_adjustment + renewable_surcharge,
    )

    prob = pulp.LpProblem("OptimalCapacity", pulp.LpMinimize)

    # 決定変数（容量も変数）
    capacity_var = pulp.LpVariable("cap", lowBound=0, upBound=capacity_upper)
    charge = [pulp.LpVariable(f"ch_{t}", lowBound=0, upBound=max_charge_per_slot) for t in range(T)]
    discharge = [pulp.LpVariable(f"dc_{t}", lowBound=0, upBound=max_discharge_per_slot) for t in range(T)]
    grid_import = [pulp.LpVariable(f"gi_{t}", lowBound=0) for t in range(T)]
    if offsite is not None:
        # grid_import は受電量 R。風力の配達分 w は R に含まれ、発電量以下（optimize_battery と同じ定式化）
        w_flat = offsite["gen_30min"].flatten()
        w_unit = offsite["unit_extra_yen"] + renewable_surcharge
        delivered = [pulp.LpVariable(f"wd_{t}", lowBound=0, upBound=float(w_flat[t])) for t in range(T)]
        # 使用量払い: 風力に余剰があるコマで、蓄電池なしでも受ける量を超える受電 ex ≥ R − base に発電側単価を課す（#4）
        used_unit, used_base, used_idx = offsite_lp_surplus_penalty(offsite, dem_flat, gen_flat)
        used_ex = {t: pulp.LpVariable(f"wx_{t}", lowBound=0) for t in used_idx}
    if no_export:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0, upBound=0) for t in range(T)]
    else:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0) for t in range(T)]
    # 出力抑制変数（逆潮流禁止時のみPV余剰を捨てる。余剰売電時は0に固定し縮退を防ぐ）
    curtail_ub = None if no_export else 0
    curtailment = [pulp.LpVariable(f"ct_{t}", lowBound=0, upBound=curtail_ub) for t in range(T)]
    soc_var = [pulp.LpVariable(f"soc_{t}", lowBound=0) for t in range(T)]
    peak_demand = pulp.LpVariable("peak_kw", lowBound=0)
    # 1台のPCSを充電または放電のどちらかに使う（同時充放電の排他制約用）
    max_power_per_slot = max(max_charge_per_slot, max_discharge_per_slot)

    # SOC上下限 = capacity_var × 比率（capacity_varとの積は線形なので制約として表現可能）
    soc_max_ratio = soc_max_pct / 100.0
    soc_min_ratio = soc_min_pct / 100.0

    # 目的関数: 年間運用コスト + 蓄電池投資年額換算
    pf_factor = (185 - power_factor_pct) / 100.0
    annual_basic = basic_charge_per_kw * peak_demand * 12 * pf_factor
    if offsite is None:
        annual_energy = pulp.lpSum([grid_import[t] * unit_price[t] for t in range(T)])
    else:
        annual_energy = pulp.lpSum([grid_import[t] * unit_price[t] - delivered[t] * (unit_price[t] - w_unit)
                                    for t in range(T)])
        if used_ex:
            annual_energy += pulp.lpSum([used_ex[t] * used_unit for t in used_idx])
    sell_price_val = sell_price if sell_price is not None else 0
    sell_lp = sell_price_val if offsite is None else offsite_lp_sell_price(sell_price_val, unit_price, w_unit)
    annual_sell = pulp.lpSum([grid_export[t] * sell_lp for t in range(T)])
    # 蓄電池投資の年額換算（容量が変数）
    annual_battery_cost = capacity_var * battery_cost_per_kwh / payback_years
    prob += annual_basic + annual_energy - annual_sell + annual_battery_cost

    # 制約条件
    for t in range(T):
        # エネルギーバランス: 系統購入 + PV発電 + 放電 = 需要 + 充電 + 系統売電 + 出力抑制
        prob += grid_import[t] + gen_flat[t] + discharge[t] == dem_flat[t] + charge[t] + grid_export[t] + curtailment[t]
        if offsite is None:
            # 売電・出力抑制はPV発電＋放電からのみ（パススルー禁止、Unbounded対策）
            prob += grid_export[t] + curtailment[t] <= gen_flat[t] + discharge[t]
        else:
            # オフサイトあり: 売電・出力抑制はそのコマの太陽光の余剰だけ（風力は売電できない）
            prob += grid_export[t] + curtailment[t] <= gen_flat[t]
            prob += delivered[t] <= grid_import[t]
            if t in used_ex:
                prob += used_ex[t] >= grid_import[t] - float(used_base[t])
        # 同時充放電の排他制約（ソフト版）
        prob += charge[t] + discharge[t] <= max_power_per_slot
        prob += peak_demand >= grid_import[t] / dt
        # SOC上下限（容量に連動）
        prob += soc_var[t] <= capacity_var * soc_max_ratio
        prob += soc_var[t] >= capacity_var * soc_min_ratio
        # SOC遷移
        if t == 0:
            prob += soc_var[t] == capacity_var * soc_min_ratio + charge[t] * eff - discharge[t] / eff
        else:
            prob += soc_var[t] == soc_var[t - 1] + charge[t] * eff - discharge[t] / eff

    # 終端SOC制約: 年末SOCを初期SOCに戻す（年次比較の公平性）
    # ※現在の初期SOC = capacity * soc_min_ratio（固定）。初期SOCを可変にする場合は要見直し
    prob += soc_var[T - 1] == capacity_var * soc_min_ratio

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=120)
    prob.solve(solver)

    if prob.status != pulp.constants.LpStatusOptimal:
        raise RuntimeError(f"最適容量探索に失敗（ステータス: {pulp.LpStatus[prob.status]}）")

    optimal_capacity = capacity_var.varValue
    opt_peak_kw = peak_demand.varValue
    opt_cost = pulp.value(prob.objective)
    if offsite is not None:
        opt_cost -= (sell_price_val - sell_lp) * float(sum(ge.varValue for ge in grid_export))
        opt_cost -= used_unit * float(sum(used_ex[t].varValue for t in used_idx))

    return {
        "optimal_capacity_kwh": optimal_capacity,
        "opt_peak_kw": opt_peak_kw,
        "opt_annual_cost": opt_cost,
    }


def grid_search_battery_capacity(generation_30min, demand_30min, month_day,
                                 efficiency_pct, max_charge_kw, max_discharge_kw,
                                 soc_min_pct, soc_max_pct,
                                 sell_price, pv_cost, battery_cost_per_kwh,
                                 total_ppeak, co2_factor,
                                 cost_before_total, payback_years,
                                 no_export=False,
                                 optimal_capacity=None, n_steps=10,
                                 offsite=None,
                                 **rate_kwargs):
    """段階2: グリッドサーチで容量ごとの指標を計算。

    段階1の最適容量を基準に探索範囲を決め、各容量でLP最適化を実行。
    rate_kwargs: basic_charge_per_kw, energy_charge_summer, etc.
    offsite: オフサイト電源（風力）の仕様（offsite_lp_spec の戻り値）。指定時は generation_30min を太陽光のみとし、
        料金を受電点の基準（offsite_cost_after）で計算し、年間メリットから風力PPAの支払を引く
    """
    # 探索範囲を決定
    # 最低でも充放電レートの2時間分を上限とし、意味のある範囲を探索
    min_meaningful = max(max_charge_kw * 2, 50)  # 最低50kWh or 充電2時間分
    if optimal_capacity and optimal_capacity > 0:
        cap_max = max(optimal_capacity * 3, min_meaningful)
    else:
        cap_max = min_meaningful
    cap_min = 0
    capacities = np.linspace(cap_min, cap_max, n_steps + 1)
    # 0は蓄電池なし（LP不要）なので最初のステップだけ特別扱い
    if capacities[0] == 0:
        capacities[0] = 0.1  # ゼロ割り回避

    results = []
    pv_investment = total_ppeak * pv_cost

    for cap in capacities:
        try:
            sc = optimize_battery(
                generation_30min, demand_30min, month_day,
                capacity_kwh=cap,
                efficiency_pct=efficiency_pct,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
                soc_min_pct=soc_min_pct,
                soc_max_pct=soc_max_pct,
                sell_price=sell_price,
                no_export=no_export,
                offsite=offsite,
                **rate_kwargs,
            )
            # 導入後の電気料金
            if offsite is None:
                cost_after = calc_electricity_cost(
                    sc["import_"], month_day,
                    **rate_kwargs,
                )
            else:
                # 受電点の基準（契約電力は受電量の最大。風力の配達分に託送等がかかる）
                off = offsite_receiving(generation_30min, offsite["sources"], demand_30min, sc)
                cost_after = offsite_cost_after(off, offsite["sources"], month_day, rate_kwargs)
            # 年間コスト削減
            saving = cost_before_total - cost_after["annual_total"]
            sell_rev = sc["annual_export"] * (sell_price if sell_price is not None else 0)
            annual_merit = saving + sell_rev if not no_export else saving
            if offsite is not None:
                # 風力の発電側の支払（W2f）。全量払いは容量によらない定数、使用量払いは容量ごとの配達量で決まる
                annual_merit -= offsite_payment_total(offsite["sources"], off["delivered_by_source"])

            # 投資額
            bat_inv = cap * battery_cost_per_kwh
            total_inv = pv_investment + bat_inv

            # 回収年数
            payback = total_inv / annual_merit if annual_merit > 0 else 999

            # P-IRR
            cf = [-total_inv] + [annual_merit] * int(payback_years)
            irr = _calc_irr(cf)

            # CO2
            grid_reduction = sc["annual_demand"] - sc["annual_import"]
            co2 = grid_reduction * co2_factor

            results.append({
                "capacity": cap,
                "annual_merit": annual_merit,
                "total_investment": total_inv,
                "payback_years": payback,
                "irr": irr if irr is not None else 0,
                "contract_power_kw": cost_after["contract_power_kw"],
                "co2_reduction": co2,
            })
        except Exception:
            continue

    return results


def make_capacity_search_chart(search_results, optimal_capacity=None):
    """最適容量探索の結果をPlotlyグラフで可視化。"""
    if not search_results:
        fig = go.Figure()
        fig.update_layout(title="最適容量探索: データなし")
        return fig

    caps = [r["capacity"] for r in search_results]
    merits = [r["annual_merit"] / 10000 for r in search_results]  # 万円
    investments = [r["total_investment"] / 10000 for r in search_results]  # 万円
    irrs = [r["irr"] * 100 for r in search_results]  # %
    paybacks = [min(r["payback_years"], 50) for r in search_results]  # 上限50年

    fig = make_subplots(
        rows=2, cols=2,
        subplot_titles=(
            "年間コスト削減額（万円/年）",
            "投資額（万円）",
            "P-IRR（%）",
            "投資回収年数（年）",
        ),
        vertical_spacing=0.15,
        horizontal_spacing=0.12,
    )

    # 年間コスト削減額
    fig.add_trace(go.Scatter(
        x=caps, y=merits, mode="lines+markers", name="コスト削減",
        line=dict(color="green"),
    ), row=1, col=1)

    # 投資額
    fig.add_trace(go.Scatter(
        x=caps, y=investments, mode="lines+markers", name="投資額",
        line=dict(color="blue"),
    ), row=1, col=2)

    # P-IRR
    fig.add_trace(go.Scatter(
        x=caps, y=irrs, mode="lines+markers", name="P-IRR",
        line=dict(color="red"),
    ), row=2, col=1)

    # 投資回収年数
    fig.add_trace(go.Scatter(
        x=caps, y=paybacks, mode="lines+markers", name="回収年数",
        line=dict(color="orange"),
    ), row=2, col=2)

    # 最適容量の縦線
    if optimal_capacity is not None and optimal_capacity > 0:
        # 小数点以下が意味ある場合は小数表示、そうでなければ整数表示
        cap_label = f"{optimal_capacity:.1f}" if optimal_capacity < 10 else f"{optimal_capacity:.0f}"
        for row, col in [(1, 1), (1, 2), (2, 1), (2, 2)]:
            fig.add_vline(
                x=optimal_capacity, line_dash="dash", line_color="red",
                annotation_text=f"最適 {cap_label}kWh",
                row=row, col=col,
            )

    fig.update_layout(
        height=700, template="plotly_white",
        showlegend=False,
        title_text="蓄電池容量 最適化探索",
    )
    for row, col in [(1, 1), (1, 2), (2, 1), (2, 2)]:
        fig.update_xaxes(title_text="蓄電池容量 [kWh]", row=row, col=col)

    return fig


# ============================================================
# 高圧電気料金計算
# ============================================================

def calc_electricity_cost(demand_or_import_30min, month_day,
                          basic_charge_per_kw, energy_charge_summer,
                          energy_charge_other, power_factor_pct,
                          fuel_adjustment, renewable_surcharge):
    """
    高圧電気料金を計算する。

    Args:
        demand_or_import_30min: (365, 48) 30分ごとの電力消費量 [kWh/30分]
                                導入前=需要データ、導入後=系統購入量
        month_day: [(month, day), ...] 365日分
        basic_charge_per_kw: 基本料金単価 [円/kW・月]
        energy_charge_summer: 電力量料金 夏季 [円/kWh]
        energy_charge_other: 電力量料金 その他季 [円/kWh]
        power_factor_pct: 力率 [%]
        fuel_adjustment: 燃料費調整単価 [円/kWh]
        renewable_surcharge: 再エネ賦課金 [円/kWh]

    Returns:
        dict: 契約電力、月別最大デマンド、基本料金、電力量料金、年間合計など
    """
    n_days = len(month_day)

    # --- 月別最大デマンド（30分デマンド値 → kW換算） ---
    # demand_or_import_30min[d, s] は kWh/30分 → 平均電力 kW = kWh / 0.5h = × 2
    monthly_max_demand_kw = {}  # {month: max_demand_kw}
    monthly_energy_kwh = {}     # {month: total_kwh}

    for d in range(n_days):
        m = month_day[d][0]
        # 30分デマンド → kW（平均電力）
        day_demand_kw = demand_or_import_30min[d] * 2.0  # kWh/30min → kW
        day_max_kw = float(np.max(day_demand_kw))
        day_energy = float(np.sum(demand_or_import_30min[d]))

        if m not in monthly_max_demand_kw:
            monthly_max_demand_kw[m] = day_max_kw
            monthly_energy_kwh[m] = day_energy
        else:
            monthly_max_demand_kw[m] = max(monthly_max_demand_kw[m], day_max_kw)
            monthly_energy_kwh[m] += day_energy

    # --- 契約電力（実量制: 年間最大デマンド） ---
    contract_power_kw = max(monthly_max_demand_kw.values()) if monthly_max_demand_kw else 0

    # --- 力率割引係数 ---
    # (185 - 力率) / 100  例: 85% → 1.00, 95% → 0.90, 75% → 1.10
    pf_factor = (185 - power_factor_pct) / 100.0

    # --- 基本料金（年間） ---
    monthly_basic = basic_charge_per_kw * contract_power_kw * pf_factor
    annual_basic = monthly_basic * 12

    # --- 電力量料金（年間） ---
    annual_energy_charge = 0.0
    monthly_energy_cost = {}
    for m in range(1, 13):
        kwh = monthly_energy_kwh.get(m, 0)
        # 夏季: 7-9月、その他季: それ以外
        if m in (7, 8, 9):
            unit_price = energy_charge_summer
        else:
            unit_price = energy_charge_other
        # 電力量料金 + 燃料費調整 + 再エネ賦課金
        total_unit = unit_price + fuel_adjustment + renewable_surcharge
        cost = kwh * total_unit
        monthly_energy_cost[m] = cost
        annual_energy_charge += cost

    # --- 年間合計 ---
    annual_total = annual_basic + annual_energy_charge

    return {
        "contract_power_kw": contract_power_kw,
        "monthly_max_demand_kw": monthly_max_demand_kw,
        "pf_factor": pf_factor,
        "monthly_basic": monthly_basic,
        "annual_basic": annual_basic,
        "annual_energy_charge": annual_energy_charge,
        "monthly_energy_cost": monthly_energy_cost,
        "monthly_energy_kwh": monthly_energy_kwh,
        "annual_total": annual_total,
    }


def make_demand_chart(cost_before, cost_after=None, contract_label="高圧"):
    """月別最大デマンド比較グラフ（導入前後）"""
    months = list(range(1, 13))
    month_labels = [f"{m}月" for m in months]
    before_vals = [cost_before["monthly_max_demand_kw"].get(m, 0) for m in months]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=month_labels, y=before_vals,
        name="導入前", marker_color="steelblue",
        text=[f"{v:.0f}" for v in before_vals], textposition="outside",
    ))

    if cost_after is not None:
        after_vals = [cost_after["monthly_max_demand_kw"].get(m, 0) for m in months]
        fig.add_trace(go.Bar(
            x=month_labels, y=after_vals,
            name="導入後", marker_color="seagreen",
            text=[f"{v:.0f}" for v in after_vals], textposition="outside",
        ))

    # 契約電力ライン（年間最大デマンド）
    fig.add_hline(
        y=cost_before["contract_power_kw"],
        line_dash="dash", line_color="red", line_width=1.5,
        annotation_text=f"契約電力(前) {cost_before['contract_power_kw']:.0f}kW",
        annotation_position="top right",
    )
    if cost_after is not None:
        fig.add_hline(
            y=cost_after["contract_power_kw"],
            line_dash="dash", line_color="green", line_width=1.5,
            annotation_text=f"契約電力(後) {cost_after['contract_power_kw']:.0f}kW",
            annotation_position="bottom right",
        )

    fig.update_layout(
        title=f"月別最大デマンド比較（{contract_label}）",
        xaxis_title="月",
        yaxis_title="最大デマンド [kW]",
        barmode="group",
        template="plotly_dark",
        height=525,
        margin=dict(t=50, b=40, l=50, r=50),
    )
    return fig


# ============================================================
# グラフ生成
# ============================================================

def _make_monthly_chart_wind(result, sc_result=None):
    """月別グラフ（風力あり）: 発電量を 太陽光＋風力 の積み上げで、需要・自家消費と並べる。"""
    months = list(range(1, 13))
    labels = [f"{m}月" for m in months]
    pv_m = monthly_sums(result["gen_pv"], result["month_day"])
    w_m = monthly_sums(result["gen_wind"], result["month_day"])
    pv_v = [pv_m.get(m, 0.0) for m in months]
    w_v = [w_m.get(m, 0.0) for m in months]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=labels, y=pv_v, name="発電量（太陽光）", marker_color="orange", offsetgroup="gen"))
    # 風力は需要地点に届く分（到達可能量。W2f）。発電端の全量は result["gen_wind_sent"]
    fig.add_trace(go.Bar(x=labels, y=w_v, base=pv_v, name="風力発電（オフサイトPPA）（需要地に届く電力量）", marker_color="teal", offsetgroup="gen"))
    if sc_result is not None:
        fig.add_trace(go.Bar(x=labels, y=[sc_result["monthly_demand"].get(m, 0) for m in months],
                             name="需要量", marker_color="steelblue", offsetgroup="dem"))
        fig.add_trace(go.Bar(x=labels, y=[sc_result["monthly_self"].get(m, 0) for m in months],
                             name="自家消費（風力発電（オフサイトPPA）の使用電力量を含む）", marker_color="green", offsetgroup="self"))
    fig.update_layout(
        title="月別 発電量（太陽光＋風力発電（オフサイトPPA）の積み上げ） vs 需要量 vs 自家消費", barmode="group",
        xaxis_title="月", yaxis_title="電力量 [kWh]", template="plotly_white", height=600,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def make_monthly_chart(result, sc_result=None):
    """月別発電量棒グラフ（需要データがあれば並列表示）"""
    if "gen_wind" in result:
        return _make_monthly_chart_wind(result, sc_result)
    months = list(range(1, 13))
    month_labels = [f"{m}月" for m in months]
    gen_values = [result["monthly"].get(m, 0) for m in months]

    fig = go.Figure()

    if sc_result is not None:
        demand_values = [sc_result["monthly_demand"].get(m, 0) for m in months]
        self_values = [sc_result["monthly_self"].get(m, 0) for m in months]

        fig.add_trace(go.Bar(
            x=month_labels, y=gen_values,
            name="発電量", marker_color="orange",
            text=[f"{v:.0f}" for v in gen_values], textposition="outside",
        ))
        fig.add_trace(go.Bar(
            x=month_labels, y=demand_values,
            name="需要量", marker_color="steelblue",
            text=[f"{v:.0f}" for v in demand_values], textposition="outside",
        ))
        fig.add_trace(go.Bar(
            x=month_labels, y=self_values,
            name="自家消費", marker_color="green",
            text=[f"{v:.0f}" for v in self_values], textposition="outside",
        ))
        fig.update_layout(title="月別 発電量 vs 需要量 vs 自家消費", barmode="group")
    else:
        fig.add_trace(go.Bar(
            x=month_labels, y=gen_values,
            marker_color="orange",
            text=[f"{v:.0f}" for v in gen_values], textposition="outside",
        ))
        fig.update_layout(title="月別発電量")

    fig.update_layout(
        xaxis_title="月", yaxis_title="電力量 [kWh]",
        template="plotly_white", height=600,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def make_daily_chart(result, month, day, sc_result=None, demand_30min=None):
    """指定日の48コマ発電量折れ線グラフ（需要データあれば需給バランス表示）"""
    month_day = result["month_day"]
    gen = result["total_gen_clipped"]

    target_idx = None
    for i, (m, d) in enumerate(month_day):
        if m == month and d == day:
            target_idx = i
            break

    if target_idx is None:
        fig = go.Figure()
        fig.update_layout(title=f"{month}月{day}日のデータがありません")
        return fig

    gen_values = gen[target_idx]
    times = [f"{h}:{m:02d}" for h in range(24) for m in (0, 30)]

    fig = go.Figure()

    if sc_result is not None and demand_30min is not None:
        demand_values = demand_30min[target_idx]
        self_values = sc_result["self_consumption"][target_idx]

        fig.add_trace(go.Scatter(
            x=times, y=self_values,
            fill="tozeroy", fillcolor="rgba(0, 180, 0, 0.25)",
            line=dict(color="rgba(0, 180, 0, 0)"), name="自家消費",
        ))
        fig.add_trace(go.Scatter(
            x=times, y=gen_values,
            mode="lines", line=dict(color="orange", width=2), name="発電量",
        ))
        fig.add_trace(go.Scatter(
            x=times, y=demand_values,
            mode="lines", line=dict(color="steelblue", width=2), name="需要量",
        ))
        if "soc" in sc_result:
            soc_values = sc_result["soc"][target_idx]
            capacity = sc_result.get("battery_capacity", 1)
            soc_pct = soc_values / capacity * 100
            fig.add_trace(go.Scatter(
                x=times, y=soc_pct,
                mode="lines", line=dict(color="purple", width=2, dash="dot"),
                name="SOC [%]", yaxis="y2",
            ))

        title_text = f"{month}月{day}日の需給バランス（30分単位）"
    else:
        fig.add_trace(go.Scatter(
            x=times, y=gen_values,
            mode="lines+markers", marker=dict(size=4),
            line=dict(color="royalblue"), name="発電量",
        ))
        title_text = f"{month}月{day}日の発電量（30分単位）"

    if "gen_wind" in result:
        # 「発電量」は太陽光＋風力の合計。内訳を細い線で足す（風力は需要地点に届く分＝到達可能量。W2f）
        fig.add_trace(go.Scatter(x=times, y=result["gen_pv"][target_idx], mode="lines",
                                 line=dict(color="gold", width=1, dash="dot"), name="うち太陽光"))
        fig.add_trace(go.Scatter(x=times, y=result["gen_wind"][target_idx], mode="lines",
                                 line=dict(color="teal", width=1, dash="dot"), name="うち風力発電（オフサイトPPA）（需要地に届く電力量）"))
    layout_kwargs = dict(
        title=title_text, xaxis_title="時刻", yaxis_title="電力量 [kWh/30分]",
        template="plotly_white", height=600,
        xaxis=dict(dtick=4, tickangle=45),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    if sc_result is not None and "soc" in sc_result:
        layout_kwargs["yaxis2"] = dict(
            title="SOC [%]", overlaying="y", side="right",
            showgrid=False, range=[0, 100],
        )
    fig.update_layout(**layout_kwargs)
    return fig


# ============================================================
# 受電電圧区分・タリフ解決（新規実装。設計書 §6-3・§6-4）
# ============================================================

def resolve_voltage_class(contract_power_kw):
    """契約電力[kW]から受電電圧区分を判定する。

    北海道電力ネットワーク 託送供給等約款 第13条の標準電圧に基づく原則判定。
    実際の受電電圧は需要場所の設備条件・周辺系統の状況により上位/下位になる場合が
    あるため、UIでは手動指定も選択できるようにしている。

    Args:
        contract_power_kw: 契約電力 [kW]

    Returns:
        str: 区分キー（"hv_6000" / "ehv_30000" / "ehv_60000"）
    """
    kw = float(contract_power_kw) if contract_power_kw else 0.0
    for lo, hi, key in VOLTAGE_CLASS_THRESHOLDS:
        if lo <= kw < hi:
            return key
    return "ehv_60000"


def get_menu_choices(voltage_class, utility=DEFAULT_UTILITY):
    """指定の電力会社・電圧区分で選択可能な料金メニュー名のリストを返す。"""
    return list(TARIFFS.get(utility, {}).get(voltage_class, {}).keys())


def get_tariff(voltage_class, menu=None, utility=DEFAULT_UTILITY):
    """タリフ表から料金単価を取得する。

    Args:
        voltage_class: 区分キー（"hv_6000" 等）
        menu: 料金メニュー名。Noneなら産業用メニュー（負荷率の高いDCに有利）を既定採用
        utility: 電力会社名

    Returns:
        dict: basic_charge_per_kw / energy_charge_summer / energy_charge_other
              ＋ 共通の従量費目（力率・燃調・再エネ賦課金）
        ※ 返す値は「タリフ表の既定値」であり、UIでユーザーが上書きした値はここには入らない
    """
    by_class = TARIFFS.get(utility, {}).get(voltage_class, {})
    if not by_class:
        raise ValueError(f"タリフが未定義です: {utility} / {voltage_class}")
    if menu is None or menu not in by_class:
        menu = INDUSTRIAL_MENU_BY_CLASS.get(voltage_class)
        if menu not in by_class:
            menu = list(by_class.keys())[0]
    rates = dict(by_class[menu])
    rates["power_factor_pct"] = DEFAULT_POWER_FACTOR_PCT
    rates["fuel_adjustment"] = DEFAULT_FUEL_ADJUSTMENT
    rates["renewable_surcharge"] = DEFAULT_RENEWABLE_SURCHARGE
    rates["menu"] = menu
    rates["voltage_class"] = voltage_class
    rates["utility"] = utility
    return rates


# ============================================================
# データセンター需要モデル（新規実装。設計書 §5）
# ============================================================
# demand(t) = IT_load(t) × PUE(T_out(t))
#
# 【Phase 0の実装範囲】
#   IT負荷: 定常 / 日変動（正弦波）に対応。CSVアップロードは未実装（設計書 §13）。
#   PUE:    「一定」のみ実装。気温連動PUE（§5-4）はPhase 1で実装する。
# ============================================================

def resolve_it_capacity_kw(capacity_mode, size_preset=None, it_capacity_kw=None,
                           n_racks=None, kw_per_rack=None):
    """容量の指定方法を解決して IT定格容量 [kW] を返す。

    DCの容量指定は実務上2軸ある（設計書 §6-2）。内部では常に IT容量[kW] に正規化する。
      - 規模プリセット: エッジ/小規模/中規模/ハイパースケール
      - IT容量を直接入力: 「20MWのDC」という業界標準の言い方
      - ラック数×density: 設備設計者向け。ラック数 × ラック電力密度[kW/ラック]

    延床面積[m2]は採らない。DCはラック密度で電力密度が1桁変わるため
    （pv-sim-bizのComStock方式がDCに転用できない理由そのもの。設計書 §5-1）。
    """
    if capacity_mode == "ラック数×density":
        n = float(n_racks) if n_racks else 0.0
        d = float(kw_per_rack) if kw_per_rack else 0.0
        return n * d
    if capacity_mode == "規模プリセット":
        preset = DC_SIZE_PRESETS.get(size_preset)
        if preset:
            return float(preset["it_capacity_kw"])
    return float(it_capacity_kw) if it_capacity_kw else 0.0


def _cec_shape_30min():
    """CEC調和モデル由来の形状を (365, 48) で返す（年平均=1.0に正規化）。

    CEC_LF_WEEKDAY/WEEKEND は [月][時刻] の千分率テーブル（年間最大=1000）。
    毎時24点を interpolate_to_30min() で48コマへ補間する。

    平日/休日の判定には暦が要るが、NEDO METPV-20は代表年データで曜日を持たない。
    このため CEC_REFERENCE_YEAR で暦を固定する（設計書 §5-7）。
    """
    import datetime as _dt
    shape = np.zeros((365, 48))
    d0 = _dt.date(CEC_REFERENCE_YEAR, 1, 1)
    for i in range(365):
        day = d0 + _dt.timedelta(days=i)
        tbl = CEC_LF_WEEKDAY if day.weekday() < 5 else CEC_LF_WEEKEND
        hourly = np.array(tbl[day.month - 1], dtype=float) / 1000.0
        shape[i] = interpolate_to_30min(hourly)
    return shape / shape.mean()


def _diurnal_shape_30min(peak_pct, bottom_pct, peak_hour):
    """日変動（正弦波）の形状を (365, 48) で返す（年平均=1.0に正規化）。

    ピーク時刻を頂点とする正弦波。ピーク/ボトム負荷率の比だけが形状を決め、
    絶対水準は呼び出し側の「IT負荷率（年平均）」が担う。
    """
    pk = float(peak_pct) / 100.0
    bt = float(bottom_pct) / 100.0
    ph = float(peak_hour) if peak_hour is not None else 14.0
    mid = (pk + bt) / 2.0
    amp = (pk - bt) / 2.0
    hours = np.arange(0.25, 24.25, 0.5)  # 各30分コマの中央時刻
    lf_day = mid + amp * np.cos(2.0 * np.pi * (hours - ph) / 24.0)
    shape = np.tile(lf_day, (365, 1))
    return shape / shape.mean()


def _apply_noise(shape, noise_level):
    """短周期変動を重畳する（形状の年平均=1.0は保つ）。

    LBNL Shape Maker の定義に合わせ、日ごとにノイズ強度を帯の中で変え、
    各コマに一様乱数を乗せる（low 3-7% / high 12-18% of baseline）。
    再現性のため NOISE_SEED で固定する。
    """
    lo, hi = NOISE_BANDS.get(noise_level, (0.0, 0.0))
    if hi <= 0:
        return shape
    rng = np.random.default_rng(NOISE_SEED)
    daily_amp = rng.uniform(lo, hi, size=(365, 1))          # 日ごとのノイズ強度
    noise = rng.uniform(-1.0, 1.0, size=shape.shape) * daily_amp
    out = shape * (1.0 + noise)
    out = np.clip(out, 0.0, None)
    return out / out.mean()


def build_it_load_30min(it_capacity_kw, profile_mode=PROFILE_FLAT,
                        load_factor_pct=80.0,
                        peak_pct=90.0, bottom_pct=50.0, peak_hour=14,
                        noise_level="なし"):
    """IT機器の消費電力量を (365, 48) [kWh/30分] で返す。

    IT_load(t) = it_capacity_kw × load_factor(t) × 0.5
    load_factor(t) = IT負荷率（年平均） × 形状(t)      ※形状は年平均=1.0に正規化

    **水準と形状を分離する**のが要点（設計書 §5-7）。
    水準（IT負荷率）はサイト固有の値でユーザーが決める。
    形状は出典のあるものを使う:

      - PROFILE_CEC   : CEC 2025 IEPR の調和モデル由来（商用DCのinterval meter、
                        PG&E約100施設2020-2024で校正）。平日/休日×月×時刻の576点テーブル
      - PROFILE_FLAT  : 時刻変動なし。LBNL "Flat — characteristic of large AI training
                        clusters running continuous batch jobs"
      - PROFILE_DIURNAL: ピーク時刻を頂点とする正弦波。振れ幅は根拠がないため
                        自社サーバー室など振れの大きいサイトを手で作る用途に限る

    ノイズは LBNL Shape Maker の定義（low 3-7% / high 12-18% of baseline）に準拠。

    Args:
        it_capacity_kw: IT定格容量 [kW]
        profile_mode: PROFILE_CEC / PROFILE_FLAT / PROFILE_DIURNAL
        load_factor_pct: IT負荷率（年平均）[%]
        peak_pct / bottom_pct / peak_hour: 日変動時の形状パラメータ
        noise_level: NOISE_LEVELS のいずれか

    Returns:
        np.ndarray: (365, 48) [kWh/30分]
    """
    cap = float(it_capacity_kw) if it_capacity_kw else 0.0
    dt = 0.5  # 30分 = 0.5時間
    level = float(load_factor_pct) / 100.0

    if profile_mode == PROFILE_CEC:
        shape = _cec_shape_30min()
    elif profile_mode == PROFILE_DIURNAL:
        shape = _diurnal_shape_30min(peak_pct, bottom_pct, peak_hour)
    else:  # PROFILE_FLAT
        shape = np.ones((365, 48))

    shape = _apply_noise(shape, noise_level)
    return cap * level * shape * dt


def build_pue_30min(pue_mode="一定", temp_30min=None, pue_const=1.40, pue_params=None):
    """各30分コマのPUEを (365, 48) で返す。

    Args:
        pue_mode: "一定" / "気温連動"
        temp_30min: (365, 48) 外気温 [℃]（気温連動時に必要）
        pue_const: PUE一定値
        pue_params: 気温連動モデルのパラメータ（PUE_MODEL_DEFAULTS 参照）

    Returns:
        np.ndarray: (365, 48) PUE
    """
    if pue_mode == "気温連動":
        # TODO(Phase 1): 設計書 §5-4 の気温連動PUEモデルを実装する
        #   PUE(T) = 1 + α + 1/COP_eff(T)
        #   x = clip((T − T_free)/(T_full − T_free), 0, 1)
        #   COP_mech(T) = max(COP_min, COP_ref − β×(T − T_ref))
        #   1/COP_eff(T) = (1 − x)/COP_free + x/COP_mech(T)
        #   → PUE_max でクリップ
        raise NotImplementedError(
            "気温連動PUEはPhase 1で実装予定です。現在は「一定」モードを使用してください。"
        )
    return np.full((365, 48), float(pue_const))


def build_dc_demand_30min(it_load_30min, pue_30min):
    """DC施設全体の電力需要 (365, 48) [kWh/30分] を返す。

    demand(t) = IT_load(t) × PUE(t)

    Returns:
        (demand_30min, breakdown) breakdownは内訳の年間集計 dict
    """
    demand = it_load_30min * pue_30min
    annual_it = float(np.sum(it_load_30min))
    annual_total = float(np.sum(demand))
    # 冷却＋電源設備損失（＝施設総電力 − IT電力）。
    # Phase 0のPUE一定モードでは冷却とその他損失を分離できないため合算で扱う。
    annual_overhead = annual_total - annual_it
    breakdown = {
        "annual_it_kwh": annual_it,
        "annual_total_kwh": annual_total,
        "annual_overhead_kwh": annual_overhead,
        "avg_pue": (annual_total / annual_it) if annual_it > 0 else 0.0,
        "max_pue": float(np.max(pue_30min)),
        "min_pue": float(np.min(pue_30min)),
    }
    return demand, breakdown


def resolve_grid_cap(mode, manual_kw=None):
    """受電上限の指定（UIの選択＋手入力値）から (上限[kW] or None, 表示ラベル) を返す。

    プリセットは受電電圧の階層境界の1kW下（GRID_CAP_PRESETS_KW）。手入力は正の有限値のみ。
    None・空欄の選択は「制限なし」として扱う。

    Raises:
        ValueError: 手入力の値が正の有限値でないとき
    """
    if not mode or mode == GRID_CAP_NONE:
        return None, GRID_CAP_NONE
    if mode in GRID_CAP_PRESETS_KW:
        return GRID_CAP_PRESETS_KW[mode], mode
    if mode == GRID_CAP_MANUAL:
        try:
            kw = float(manual_kw)
        except (TypeError, ValueError):
            kw = float("nan")
        if not np.isfinite(kw) or kw <= 0:
            raise ValueError("受電上限（手入力）は0より大きい数値[kW]で指定してください")
        return kw, GRID_CAP_MANUAL
    raise ValueError(f"受電上限の指定が不正です: {mode}")


def resolve_dc_demand(dc_args, temp_30min=None):
    """DC入力（UIの値をそのまま詰めた辞書）からDC需要 (365, 48) を生成する。

    run_simulation の需要ソース分岐（データセンター）から呼ばれる。
    None・空欄は DC_DEFAULTS で補う（数値入力を `is not None` で判定する既存の作法に合わせる）。
    下流（LP・料金計算・経済性）は (365, 48) 配列を受け取るだけなので、この関数の戻り値を
    産業用の load_combined_demand() の代わりに差し込める。

    Args:
        dc_args: DC_INPUT_KEYS をキーとする辞書
        temp_30min: (365, 48) 外気温 [℃]。気温連動PUE用（未実装）。PUE一定では不要

    Returns:
        (demand_30min, dc_info)
        dc_info: 結果テキスト用の解決済み入力・IT負荷・内訳・ピークデマンド

    Raises:
        ValueError: IT定格容量が0以下／IT負荷率が範囲外／PUEが1.0未満／受電上限（手入力）が不正
    """
    a = dict(dc_args or {})

    def pick(key, default):
        v = a.get(key)
        return default if v is None else v

    capacity_mode = a.get("capacity_mode") or "IT容量を直接入力"
    it_cap = resolve_it_capacity_kw(
        capacity_mode,
        size_preset=a.get("size_preset"),
        it_capacity_kw=pick("it_capacity_kw", DC_DEFAULTS["it_capacity_kw"]),
        n_racks=pick("n_racks", DC_DEFAULTS["n_racks"]),
        kw_per_rack=pick("kw_per_rack", DC_DEFAULTS["kw_per_rack"]),
    )
    if it_cap <= 0:
        raise ValueError("IT定格容量が0以下です。容量の指定方法と値を確認してください")

    it_lf = float(pick("it_load_factor_pct", DC_DEFAULTS["it_load_factor_pct"]))
    if not (0 < it_lf <= 100):
        raise ValueError(f"IT負荷率（年平均）は0より大きく100以下で指定してください（現在: {it_lf:g}%）")
    pue_c = float(pick("pue_const", DC_DEFAULTS["pue_const"]))
    if pue_c < 1.0:
        raise ValueError(
            f"PUEは1.0以上で指定してください（現在: {pue_c:g}）。"
            "PUE＝施設全体電力÷IT機器電力のため1.0が下限です"
        )
    it_pk = float(pick("it_peak_pct", DC_DEFAULTS["it_peak_pct"]))
    it_bt = float(pick("it_bottom_pct", DC_DEFAULTS["it_bottom_pct"]))
    it_ph = pick("it_peak_hour", DC_DEFAULTS["it_peak_hour"])
    grid_cap_kw, grid_cap_label = resolve_grid_cap(a.get("grid_cap_mode"), a.get("grid_cap_kw"))

    # 用途プリセットはプロファイルとノイズを一括設定する（「手動設定」なら個別指定を使う）
    workload = a.get("workload_preset")
    preset = DC_WORKLOAD_PRESETS.get(workload)
    if preset:
        profile_mode = preset["profile"]
        noise = preset["noise"]
    else:
        profile_mode = a.get("profile_mode") or PROFILE_FLAT
        noise = a.get("noise_level") or "なし"

    it_load_30min = build_it_load_30min(
        it_cap, profile_mode=profile_mode, load_factor_pct=it_lf,
        peak_pct=it_pk, bottom_pct=it_bt, peak_hour=it_ph, noise_level=noise,
    )
    # PUEは一定値のみ（気温連動は保留。docs/decision_log.md 第2段階）
    pue_30min = build_pue_30min(
        pue_mode="一定", temp_30min=temp_30min, pue_const=pue_c, pue_params=PUE_MODEL_DEFAULTS,
    )
    demand_30min, breakdown = build_dc_demand_30min(it_load_30min, pue_30min)

    dc_info = {
        "workload_preset": workload,
        "capacity_mode": capacity_mode,
        "size_preset": a.get("size_preset"),
        "n_racks": pick("n_racks", DC_DEFAULTS["n_racks"]),
        "kw_per_rack": pick("kw_per_rack", DC_DEFAULTS["kw_per_rack"]),
        "it_capacity_kw": it_cap,
        "profile_mode": profile_mode,
        "noise_level": noise,
        "it_load_factor_pct": it_lf,
        "it_peak_pct": it_pk,
        "it_bottom_pct": it_bt,
        "it_peak_hour": it_ph,
        "pue_const": pue_c,
        "it_load_30min": it_load_30min,
        "breakdown": breakdown,
        # 導入前ピークデマンド[kW] = 30分需要[kWh]の年間最大 ÷ 0.5h
        "peak_demand_kw": float(np.max(demand_30min)) * 2.0,
        # 系統受電上限（None=制限なし）。DCモードのときだけ存在する（産業用には上限を適用しない）
        "grid_cap_kw": grid_cap_kw,
        "grid_cap_label": grid_cap_label,
    }
    return demand_30min, dc_info


def format_dc_summary(dc_info, contract_type=None):
    """DC需要の結果テキスト（結果ボックス先頭に置く節）を返す。

    Args:
        dc_info: resolve_dc_demand が返す辞書
        contract_type: 電気料金設定の契約種別（"高圧"/"特別高圧"）。受電電圧の目安との食い違い警告に使う
    """
    d = dc_info
    bd = d["breakdown"]
    peak_kw = d["peak_demand_kw"]
    t = "══ データセンター需要 ══\n"
    if d["workload_preset"] and DC_WORKLOAD_PRESETS.get(d["workload_preset"]):
        t += f"  用途: {d['workload_preset']}\n"
    if d["capacity_mode"] == "規模プリセット" and d["size_preset"] in DC_SIZE_PRESETS:
        t += f"  規模: {d['size_preset']}（{DC_SIZE_PRESETS[d['size_preset']]['note']}）※区分は暫定値\n"
    elif d["capacity_mode"] == "ラック数×density":
        t += f"  容量指定: {float(d['n_racks']):,.0f} ラック × {float(d['kw_per_rack']):,.1f} kW/ラック\n"
    t += f"  IT定格容量: {d['it_capacity_kw']:,.0f} kW\n"
    t += f"  負荷プロファイル: {d['profile_mode']}"
    if d["profile_mode"] == PROFILE_DIURNAL:
        t += f"（ピーク{d['it_peak_pct']:.0f}% / ボトム{d['it_bottom_pct']:.0f}% / ピーク{int(d['it_peak_hour'])}時）"
    t += "\n"
    t += f"  IT負荷率（年平均）: {d['it_load_factor_pct']:.0f}%\n"
    t += f"  短周期変動: {d['noise_level']}\n"
    it_kw = d["it_load_30min"] * 2.0
    t += (f"  IT負荷 実効: 平均 {it_kw.mean():,.0f} kW / 最小 {it_kw.min():,.0f} kW"
          f" / 最大 {it_kw.max():,.0f} kW\n")
    t += f"  PUE: {d['pue_const']:.2f}（一定。空冷前提）\n"
    t += f"  年間IT電力量: {bd['annual_it_kwh']:,.0f} kWh/年\n"
    t += f"  年間施設総電力量: {bd['annual_total_kwh']:,.0f} kWh/年\n"
    t += f"  うち冷却＋電源設備損失: {bd['annual_overhead_kwh']:,.0f} kWh/年\n"
    t += f"  年間平均PUE（実効）: {bd['avg_pue']:.3f}\n"
    t += f"  導入前ピークデマンド: {peak_kw:,.1f} kW\n"
    t += (f"  推奨する申請受電容量の目安: {peak_kw / CEC_UTILIZATION_FACTOR:,.0f} kW"
          f"（施設最大需要 ÷ {CEC_UTILIZATION_FACTOR:.2f}）\n")
    t += ("    ※ CEC 2025 IEPR の utilization factor 67%（申請受電容量→最大運転需要）の逆算。\n"
          "       67%は「観測された上限」であり典型値ではないため、実際の申請容量は\n"
          "       これより大きくなる可能性がある（設計書 §5-6(2)）\n")

    # 契約種別（高圧/特別高圧）と、ピークデマンドから見た受電電圧区分の目安の食い違いを知らせる
    suggest_ehv = resolve_voltage_class(peak_kw) != "hv_6000"
    if contract_type == "高圧" and suggest_ehv:
        t += (f"  ⚠ 導入前ピーク {peak_kw:,.0f} kW は2,000kW以上のため、契約種別は「特別高圧」が目安です"
              "（現在は「高圧」。「電気料金設定」で切り替えられます）\n")
    elif contract_type == "特別高圧" and not suggest_ehv:
        t += (f"  ⚠ 導入前ピーク {peak_kw:,.0f} kW は2,000kW未満のため、契約種別は「高圧」が目安です"
              "（現在は「特別高圧」。「電気料金設定」で切り替えられます）\n")
    t += ("  ※ 電気料金は共通の「電気料金設定」を使用します"
          "（既定値は東京電力EP。北海道電力タリフ表との連動は未実装）\n")
    return t


def _grid_cap_reason_lines(diag, battery):
    """診断結果（diagnose_grid_cap）の違反ごとの説明行を返す。"""
    lines = []
    v = diag["violations"]
    if "energy" in v:
        lines.append(
            "  ・上限が低すぎます: PV差引後の負荷が上限を下回る時間の余力"
            f"（{diag['spare_energy_kwh']:,.0f} kWh/年）では、超過分（{diag['exceed_energy_kwh']:,.0f} kWh/年）を\n"
            "    蓄電池の効率損失込みで賄えません。蓄電池を大きくしても解決しません。\n"
            f"    → 上限を約 {diag['min_cap_kw_energy']:,.0f} kW 以上にする／PV容量を増やす／"
            "IT負荷（容量・負荷率）を下げる\n")
    if "power" in v:
        lines.append(
            f"  ・蓄電池の放電出力が不足: 上限内に収めるには最大 {diag['required_power_kw']:,.0f} kW の放電が必要"
            f"（指定 {battery['max_discharge_kw']:,.0f} kW）\n")
    if "capacity" in v:
        lines.append(
            f"  ・蓄電池の容量が不足: 使用可能容量で少なくとも {diag['required_usable_kwh']:,.0f} kWh"
            f"（公称容量で約 {diag['required_capacity_kwh']:,.0f} kWh）が必要"
            f"（指定 {battery['capacity_kwh']:,.0f} kWh）\n")
    return lines


def format_grid_cap(dc_info, diag, status, sc_result=None, battery=None, receive=None):
    """系統受電上限の結果テキスト（結果ボックスに置く節）を返す。

    status:
      "enforced"      最適充放電LPで上限を制約として強制し、解けた
      "not_enforced"  蓄電池が「なし」「ルールベース」のため上限は強制していない（超過判定のみ表示）
      "infeasible"    診断の必要条件が破れていて確実に守れない（LPは実行していない）
      "infeasible_lp" 診断の必要条件は満たすが、LPが実行不可能だった
    battery: {"enabled", "mode_label", "capacity_kwh", "max_charge_kw", "max_discharge_kw"}
    receive: 受電量 [kWh/30分] の配列。オフサイト電源（風力）があるとき、導入後ピークはこれで測る
        （sc_result["import_"] は小売から買う分だけで、風力の配達分を含まない）。None なら従来どおり import_
    """
    cap = diag["cap_kw"]
    before = dc_info["peak_demand_kw"]
    b = battery or {"enabled": False, "mode_label": "", "capacity_kwh": 0.0,
                    "max_charge_kw": 0.0, "max_discharge_kw": 0.0}
    t = "══ 受電上限 ══\n"
    t += f"  上限: {cap:,.0f} kW（{dc_info['grid_cap_label']}）\n"
    if before > cap:
        t += f"  導入前ピーク: {before:,.1f} kW（上限を {before - cap:,.1f} kW 超過）\n"
    else:
        t += f"  導入前ピーク: {before:,.1f} kW（上限内）\n"

    if status in ("infeasible", "infeasible_lp"):
        if status == "infeasible":
            t += "  ⚠ この条件では受電上限を守れません。蓄電池の最適化は実行していません。\n"
        else:
            t += ("  ⚠ 最適化（LP）が実行不可能でした（受電上限を守れません）。\n"
                  "    出力・容量・エネルギー収支の必要条件は満たしているため、主な原因は充電レート不足です\n"
                  f"    （最大充電電力 {b['max_charge_kw']:,.0f} kW では、超過後に蓄電池を回復できない時間帯があります）。\n"
                  "    最大充電電力・容量を増やして再試行してください。\n")
        t += (f"  PV差引後の負荷: 最大 {diag['peak_net_kw']:,.0f} kW ／ 上限超過のコマ {diag['exceed_slots']:,} 件"
              f"（年間の {diag['exceed_slots'] / diag['total_slots'] * 100:.1f}%）"
              f" ／ 超過エネルギー {diag['exceed_energy_kwh']:,.0f} kWh/年\n")
        for line in _grid_cap_reason_lines(diag, b):
            t += line
        if diag["energy_feasible"]:
            t += (f"  必要な蓄電池の下限の目安: 放電出力 ≥ {diag['required_power_kw']:,.0f} kW ／ "
                  f"使用可能容量 ≥ {diag['required_usable_kwh']:,.0f} kWh"
                  f"（公称容量で約 {diag['required_capacity_kwh']:,.0f} kWh）\n")
            t += ("    ※ 充放電レート・効率損失・SOC範囲を細かく考えない下限の目安です。\n"
                  "       実際にはこれ以上必要になる場合があります\n")
        return t

    peak_after = float(np.max(sc_result["import_"] if receive is None else receive)) * 2.0
    over = peak_after - cap
    within = over <= max(1e-6 * cap, 1e-6)
    after_txt = "上限内" if within else f"上限を {over:,.1f} kW 超過"
    if status == "enforced":
        t += f"  導入後ピーク: {peak_after:,.1f} kW（{after_txt}）\n"
        t += ("  最適充放電（LP）で上限を制約として強制しています"
              "（受電上限を指定した場合、蓄電池の初期SOCは年末SOCと一致させる周期条件にしています）\n")
        if diag["needs_battery"] and diag["energy_feasible"]:
            t += (f"  必要な蓄電池の下限の目安: 放電出力 ≥ {diag['required_power_kw']:,.0f} kW ／ "
                  f"使用可能容量 ≥ {diag['required_usable_kwh']:,.0f} kWh"
                  f"（公称容量で約 {diag['required_capacity_kwh']:,.0f} kWh）\n")
    else:  # not_enforced
        t += f"  導入後ピーク: {peak_after:,.1f} kW（{after_txt}）\n"
        if not diag["needs_battery"]:
            t += "  PV差引後の負荷は常に上限内で、蓄電池なしでも上限を守れます\n"
        else:
            why = "「なし」" if not b["enabled"] else f"「{b['mode_label']}」"
            t += (f"  ⚠ 蓄電池が{why}のため、上限は強制していません。"
                  "上限を守れるかは「最適充放電（LP）」で判定できます\n")
            for line in _grid_cap_reason_lines(diag, b):
                t += line
            if diag["energy_feasible"]:
                t += (f"  上限を守るために必要な蓄電池の下限の目安: 放電出力 ≥ {diag['required_power_kw']:,.0f} kW ／ "
                      f"使用可能容量 ≥ {diag['required_usable_kwh']:,.0f} kWh"
                      f"（公称容量で約 {diag['required_capacity_kwh']:,.0f} kWh）\n")
    return t


def grid_cap_infeasible_response(dc_info, contract_type, diag, status, battery):
    """受電上限を守れないときに run_simulation が返す7要素のタプル（エラーではなく診断として返す）。"""
    text = format_dc_summary(dc_info, contract_type) + "\n"
    text += format_grid_cap(dc_info, diag, status, battery=battery)
    dbg = (f"受電上限 {diag['cap_kw']:,.0f} kW を守れません（{status}）: "
           f"違反={diag['violations'] or ['LP実行不可能']}\n")
    return None, None, None, None, text, dbg, None


# ============================================================
# IRR計算（ニュートン法）
# ============================================================

def _calc_irr(cashflows, tol=1e-8, max_iter=100):
    """キャッシュフロー列からIRRを求める（ニュートン法＋二分法フォールバック）。"""
    def npv(r):
        return sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows))

    # 符号が変わらない（全期間の収支が正でない）キャッシュフローにIRRは存在しない。
    # このとき ニュートン法が巨大なrに発散して (1+r)**t がオーバーフローするため、先に打ち切る
    if not any(cf > 0 for cf in cashflows):
        return None

    # ニュートン法（r ≤ -1 に発散したら打ち切って二分法へ）
    r = 0.10
    try:
        for _ in range(max_iter):
            v = npv(r)
            dv = sum(-t * cf / (1 + r) ** (t + 1) for t, cf in enumerate(cashflows))
            if abs(dv) < 1e-15:
                break
            r_new = r - v / dv
            if r_new <= -0.999:
                break
            if abs(r_new - r) < tol:
                return r_new
            r = r_new
        if abs(npv(r)) < 1:
            return r
    except OverflowError:
        pass  # 発散した。二分法へ

    # 二分法フォールバック（[-0.99, 10]で符号が変わる場合のみ）
    lo, hi = -0.99, 10.0
    f_lo = npv(lo)
    if f_lo * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < tol or hi - lo < tol:
            return mid
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


# ============================================================
# Gradio UI
# ============================================================

def run_simulation(
    station_choice, csv_file,
    demand_custom_csv,
    KHD, KPD, KPM, KPA, eta_ino,
    alpha_pct, delta_t,
    bat_enabled, bat_mode, bat_capacity, bat_efficiency,
    bat_max_charge, bat_max_discharge,
    bat_soc_min, bat_soc_max,
    elec_basic, elec_summer, elec_other,
    elec_pf, elec_fuel, elec_renewable,
    contract_type,
    sell_mode, sell_price,
    pv_cost_per_kw, bat_cost_per_kwh, substation_cost_per_kva,
    subsidy_enabled, subsidy_pv_pct, subsidy_bat_pct,
    co2_factor,
    business_model, contract_years, target_irr,
    mg_enabled, mg_line_distance, mg_line_cost_per_km, mg_opex_ratio, mg_irr_period,
    bifacial_enabled, bifaciality_val, gcr_val, height_val, pitch_val,
    snow_albedo_enabled,
    facility_args, face_args,
    num_facilities=1, num_faces=1,
    display_month=1, display_day=1,
    demand_source=DEMAND_SOURCE_INDUSTRIAL, dc_args=None,
    pv_enabled=True, wind_args=None,
):
    """メイン計算コールバック

    demand_source で需要の生成方法を切り替える。下流（蓄電池・料金計算・経済性）は
    どちらも (365, 48) の需要配列を受け取るだけなので共通。
      - DEMAND_SOURCE_INDUSTRIAL: ComStock需要プリセット・複数施設合算（facility_args 等を使用）
      - DEMAND_SOURCE_DATACENTER: IT負荷 × PUE（dc_args を使用。facility_args 等は無視）

    発電源は太陽光と風力（オフサイトPPA）を任意に選べる（docs/wind_design_spec.md）。
    下流は (365, 48) の発電量配列を1本受け取るだけなので、風力は太陽光に足して gen_total として渡す。
      - pv_enabled: 太陽光を使うか（False なら face_args は無視。風力のみの運転）
      - wind_args : WIND_INPUT_KEYS をキーとする辞書（resolve_wind）。None/無効なら風力なし
    どちらも省略したときは従来どおり（出力は変わらない）。
    """
    try:
        # --- データ読み込み ---
        if csv_file is not None:
            lat, lon, ghi_df, temp_df = load_from_csv(csv_file)
            source_text = "CSVアップロード"
        elif station_choice:
            point_no = station_choice.split(" ")[0]
            lat, lon, ghi_df, temp_df = load_from_db(point_no)
            source_text = f"DB: {station_choice}"
        else:
            return None, None, None, None, "エラー: 地点を選択するかCSVをアップロードしてください", "", None

        # --- 需要データの生成（需要ソースで分岐） ---
        dc_info = None
        if demand_source == DEMAND_SOURCE_DATACENTER:
            # データセンター: IT負荷 × PUE。PUEは一定値のみのため外気温は使わない
            # （気温連動PUEを実装するときは prepare_30min_data の気温を temp_30min として渡す）
            try:
                demand_30min, dc_info = resolve_dc_demand(dc_args)
            except ValueError as e:
                return None, None, None, None, f"エラー: {e}", "", None
            # マイクログリッドの束ねメリット計算は施設ごとの需要リストを使う。DCは単一サイト
            individual_demands = [demand_30min]
        else:
            # 産業用・MG: 複数施設合算
            demand_30min, individual_demands = load_combined_demand(
                facility_args, num_facilities, custom_csv=demand_custom_csv,
            )

        # --- 風力（オフサイトPPA）: 需要地と同じエリアの形状から発電量と支払額を解決する ---
        wind_info = None
        if wind_args and wind_args.get("enabled"):
            if demand_30min is None:
                # 施設が未設定（施設タイプ「なし」）だと需要がなく、風力の容量（カバー率）も24/7も評価できない
                return None, None, None, None, ("エラー: 風力発電（オフサイトPPA）を使うには需要の設定が必要です"
                                                "（需要設定で施設を1つ以上選ぶか、データセンターの条件を設定してください）"), "", None
            try:
                wind_info = resolve_wind(
                    wind_args, None if csv_file is not None else point_no,
                    float(np.sum(demand_30min)), station_label=station_choice, contract_type=contract_type)
            except ValueError as e:
                return None, None, None, None, f"エラー: {e}", "", None

        # --- 面設定パース（5項目: Ppeak, 方位選択, 方位角, 傾斜角, PCS出力制限） ---
        # 太陽光を使わない（pv_enabled=False）ときは面設定を読まない（空欄・不正値があってもエラーにしない）
        faces = []
        for i in range(int(num_faces) if pv_enabled else 0):
            idx = i * 5
            ppeak = float(face_args[idx]) if face_args[idx] else 0
            orientation = face_args[idx + 1] if face_args[idx + 1] else "南"
            azi_direct = face_args[idx + 2]
            tilt = float(face_args[idx + 3]) if face_args[idx + 3] else 30
            pcs_kw = float(face_args[idx + 4]) if face_args[idx + 4] else 0

            # 方位角欄はテキスト入力（空欄=未指定→方位（選択）を使う）。gr.Number は value=None でも
            # 未操作で 0 を送信し空欄を表現できないため（Gradio 6.26で確認）、Textbox にしている。
            if isinstance(azi_direct, str):
                azi_direct = azi_direct.strip()
            if azi_direct is not None and azi_direct != "":
                try:
                    azimuth = float(azi_direct)
                except (TypeError, ValueError):
                    azimuth = float("nan")
                if not np.isfinite(azimuth):
                    return None, None, None, None, (
                        f"エラー: 面{i+1}の方位角「{azi_direct}」は数値で入力してください"
                        "（空欄なら「方位（選択）」を使います）"), "", None
                azimuth = azimuth % 360
            else:
                azimuth = float(ORIENTATION_TO_AZIMUTH.get(orientation, 180))

            if ppeak > 0:
                faces.append({
                    "ppeak": ppeak,
                    "orientation": orientation,
                    "azimuth": azimuth,
                    "tilt": tilt,
                    "pcs_limit_kw": pcs_kw if pcs_kw > 0 else None,
                })

        if not faces and wind_info is None:
            if not pv_enabled:
                return None, None, None, None, "エラー: 太陽光も風力発電（オフサイトPPA）も使わない設定です（どちらかを有効にしてください）", "", None
            return None, None, None, None, "エラー: 有効な面設定がありません（Ppeak > 0の面が必要です）", "", None
        # 太陽光を実際に使うか（チェックONでも、有効な面（Ppeak > 0）がなければ風力のみ）。風力なしでは常に True
        pv_used = bool(pv_enabled) and bool(faces)

        # --- 両面パネル: albedo時系列の準備 ---
        albedo_flat = None
        if bifacial_enabled:
            if snow_albedo_enabled and csv_file is None and station_choice:
                snow_df = load_snow_depth(point_no)
                albedo_flat = build_albedo_series(snow_df)
            else:
                albedo_flat = np.full(365 * 48, ALBEDO_NORMAL)

        # --- 計算実行 ---
        result = calculate_generation(
            lat, lon, ghi_df, temp_df,
            faces, KHD, KPD, KPM, KPA, eta_ino,
            alpha_pct, delta_t,
            bifacial=bool(bifacial_enabled),
            bifaciality=float(bifaciality_val) if bifaciality_val is not None else BIFACIAL_DEFAULTS["bifaciality"],
            gcr=float(gcr_val) if gcr_val is not None else BIFACIAL_DEFAULTS["gcr"],
            height=float(height_val) if height_val is not None else BIFACIAL_DEFAULTS["height"],
            pitch=float(pitch_val) if pitch_val is not None else BIFACIAL_DEFAULTS["pitch"],
            albedo_flat=albedo_flat,
        )

        # --- 発電の合成（太陽光 + 風力） ---
        # 風力なしのときは gen_total / chart_result が result そのもの（従来と同一オブジェクト）
        gen_total = result["total_gen_clipped"]
        chart_result = result
        if wind_info is not None:
            # 発電の合成は**到達可能量**（需要地点に届く分）で行う（W2f。損失分は届かない）。
            # 発電端の全量は gen_wind_sent に別途持つ（表示・出典の確認用）
            wind_deliverable = wind_info.get("deliverable_30min", wind_info["gen_30min"])
            gen_total = result["total_gen_clipped"] + wind_deliverable
            # グラフと result_state は合計を見せる。太陽光・風力の内訳は別キーで持つ
            chart_result = dict(
                result, total_gen_clipped=gen_total,
                monthly=monthly_sums(gen_total, result["month_day"]),
                gen_pv=result["total_gen_clipped"], gen_wind=wind_deliverable,
                gen_wind_sent=wind_info["gen_30min"], wind_info=wind_info,
            )

        # --- 自家消費計算 / 蓄電池 ---
        no_export = (sell_mode == "逆潮流禁止（売電なし）")
        sc_result = None
        # 蓄電池LP（最適充放電・最適容量探索）は、風力（オフサイト。送配電網で届く）を受電点の基準で扱う:
        # 敷地内の太陽光だけを generation として渡し、風力は offsite の仕様で渡す（設計書 wind §5-4・W2d）。
        # ルールベースは従来どおり、太陽光＋風力の合計の発電で運転する
        offsite_spec = offsite_lp_spec([wind_info]) if wind_info is not None else None
        gen_lp = result["total_gen_clipped"] if offsite_spec is not None else gen_total
        battery_mode_label = bat_mode if bat_mode else "ルールベース"
        # 系統受電上限（DCモードのときだけ存在。産業用には適用しない）と、診断に使う蓄電池の諸元。
        # 上限を強制できるのは最適充放電（LP）だけ。最適容量探索は上限を考慮しないため諸元は0扱い
        grid_cap_kw = dc_info["grid_cap_kw"] if dc_info is not None else None
        grid_cap_diag = None
        _bat_on = bool(bat_enabled and bat_capacity and bat_capacity > 0)
        _bat_spec = _bat_on and battery_mode_label != "最適容量探索"
        battery_info = {
            "enabled": _bat_on,
            "mode_label": battery_mode_label,
            "capacity_kwh": float(bat_capacity) if _bat_spec else 0.0,
            "efficiency_pct": float(bat_efficiency) if bat_efficiency is not None else 95.0,
            "max_charge_kw": float(bat_max_charge if bat_max_charge is not None else 2.5) if _bat_spec else 0.0,
            "max_discharge_kw": float(bat_max_discharge if bat_max_discharge is not None else 2.5) if _bat_spec else 0.0,
            "soc_min_pct": float(bat_soc_min) if bat_soc_min is not None else 20.0,
            "soc_max_pct": float(bat_soc_max) if bat_soc_max is not None else 95.0,
        }
        if demand_30min is not None:
            if bat_enabled and bat_capacity and bat_capacity > 0 and battery_mode_label != "最適容量探索":
                if battery_mode_label == "最適充放電（LP）":
                    # 受電上限あり: LPを解くまでもなく確実に守れない場合は、診断を返して終える
                    if grid_cap_kw:
                        # 受電上限は受電点（風力の配達分も含む）で見る。風力は上限を守る助けにならないので、太陽光だけで診断する
                        grid_cap_diag = diagnose_grid_cap(
                            gen_lp, demand_30min, grid_cap_kw,
                            battery_info["capacity_kwh"], battery_info["efficiency_pct"],
                            battery_info["max_charge_kw"], battery_info["max_discharge_kw"],
                            battery_info["soc_min_pct"], battery_info["soc_max_pct"],
                        )
                        if grid_cap_diag["violations"]:
                            return grid_cap_infeasible_response(
                                dc_info, contract_type, grid_cap_diag, "infeasible", battery_info)
                    # LP最適化: 電気料金パラメータが必要
                    is_ehv_tmp = (contract_type == "特別高圧")
                    defaults_tmp = ELECTRICITY_RATE_EHV if is_ehv_tmp else ELECTRICITY_RATE_HV
                    try:
                        sc_result = optimize_battery(
                            gen_lp, demand_30min, result["month_day"],
                            capacity_kwh=bat_capacity,
                            efficiency_pct=bat_efficiency if bat_efficiency is not None else 95,
                            max_charge_kw=bat_max_charge if bat_max_charge is not None else 2.5,
                            max_discharge_kw=bat_max_discharge if bat_max_discharge is not None else 2.5,
                            soc_min_pct=bat_soc_min if bat_soc_min is not None else 20,
                            soc_max_pct=bat_soc_max if bat_soc_max is not None else 95,
                            basic_charge_per_kw=elec_basic if elec_basic is not None else defaults_tmp["basic_charge_per_kw"],
                            energy_charge_summer=elec_summer if elec_summer is not None else defaults_tmp["energy_charge_summer"],
                            energy_charge_other=elec_other if elec_other is not None else defaults_tmp["energy_charge_other"],
                            power_factor_pct=elec_pf if elec_pf is not None else defaults_tmp["power_factor_pct"],
                            fuel_adjustment=elec_fuel if elec_fuel is not None else defaults_tmp["fuel_adjustment"],
                            renewable_surcharge=elec_renewable if elec_renewable is not None else defaults_tmp["renewable_surcharge"],
                            sell_price=sell_price if sell_price is not None else DEFAULT_SELL_PRICE,
                            no_export=no_export,
                            grid_import_cap_kw=grid_cap_kw,
                            offsite=offsite_spec,
                        )
                    except GridCapInfeasibleError:
                        # 診断の必要条件は満たしたがLPが実行不可能（主に充電レート不足）
                        return grid_cap_infeasible_response(
                            dc_info, contract_type, grid_cap_diag, "infeasible_lp", battery_info)
                else:
                    # ルールベース。風力あり: 蓄電池は太陽光だけで動かし、不足分を風力→小売が埋める（W2e）
                    sc_result = simulate_battery(
                        gen_lp, demand_30min, result["month_day"],
                        capacity_kwh=bat_capacity,
                        efficiency_pct=bat_efficiency if bat_efficiency is not None else 95,
                        max_charge_kw=bat_max_charge if bat_max_charge is not None else 2.5,
                        max_discharge_kw=bat_max_discharge if bat_max_discharge is not None else 2.5,
                        soc_min_pct=bat_soc_min if bat_soc_min is not None else 20,
                        soc_max_pct=bat_soc_max if bat_soc_max is not None else 95,
                        no_export=no_export,
                        offsite_gen=None if offsite_spec is None else offsite_spec["gen_30min"],
                    )
            else:
                sc_result = calculate_self_consumption(
                    gen_total, demand_30min, result["month_day"],
                    no_export=no_export,
                )

        # --- オフサイト電源（風力）を受電点の基準に組み替える ---
        # 蓄電池の運転は太陽光＋風力を合わせた発電で計算済み。料金・売電は受電点の基準で計算し直す:
        #  ・契約電力（基本料金）は受電量の最大で決まり、風力では下がらない
        #  ・風力の配達分にも、託送の従量分・再エネ賦課金・小売手数料がかかる
        #  ・売電・出力抑制の対象は敷地内の太陽光の余剰だけ（風力の余剰は売電できず無駄になる）
        wind_off = None
        offsite_sources = []
        if wind_info is not None and sc_result is not None:
            offsite_sources = [wind_info]
            wind_off = offsite_receiving(result["total_gen_clipped"], offsite_sources, demand_30min, sc_result)
            wind_info["delivered_kwh"] = float(wind_off["delivered_by_source"][0].sum())
            wind_info["wasted_kwh"] = float(wind_off["wasted_by_source"][0].sum())  # 発電端の余剰（W2f）
            wind_info["loss_kwh"] = float(wind_off["loss_by_source"][0].sum())  # 送電ロス量（W2f）
            pv_surplus_kwh = float(wind_off["pv_surplus"].sum())
            # 運転の結果（プール: 太陽光＋風力）の値は別キーに残し、表示・経済性は太陽光の余剰だけにする
            sc_result["annual_export_pooled"] = sc_result["annual_export"]
            sc_result["annual_curtailment_pooled"] = sc_result.get("annual_curtailment", 0.0)
            sc_result["annual_export"] = 0.0 if no_export else pv_surplus_kwh
            sc_result["annual_curtailment"] = pv_surplus_kwh if no_export else 0.0

        # --- 受電上限の結果（DCモードで上限が指定されたときのみ） ---
        # 風力を使うときは、受電点の基準（wind_off）に組み替えた後で判定する（受電量＝小売購入＋風力の配達分）
        # LPで強制した場合は事前診断をそのまま使う。それ以外（蓄電池なし/ルールベース/容量探索）は
        # 上限を強制していないので、導入後ピークが上限を超えるかの判定と、必要な蓄電池の目安を出す
        grid_cap_text = ""
        if grid_cap_kw and sc_result is not None:
            cap_enforced = bool(sc_result.get("grid_import_cap_kw"))
            cap_diag = grid_cap_diag if (cap_enforced and grid_cap_diag is not None) else diagnose_grid_cap(
                gen_lp, demand_30min, grid_cap_kw,
                battery_info["capacity_kwh"], battery_info["efficiency_pct"],
                battery_info["max_charge_kw"], battery_info["max_discharge_kw"],
                battery_info["soc_min_pct"], battery_info["soc_max_pct"],
            )
            grid_cap_text = format_grid_cap(
                dc_info, cap_diag, "enforced" if cap_enforced else "not_enforced", sc_result, battery_info,
                receive=None if wind_off is None else wind_off["receive"])

        # --- グラフ生成 ---
        fig_monthly = make_monthly_chart(chart_result, sc_result)
        fig_daily = make_daily_chart(
            chart_result, int(display_month), int(display_day),
            sc_result, demand_30min,
        )

        # --- 結果テキスト ---
        result_text = ""
        if dc_info is not None:
            # データセンター: 需要（IT負荷×PUE）の節を先頭に置く。受電上限を指定した場合はその結果が続く
            result_text += format_dc_summary(dc_info, contract_type) + "\n"
            if grid_cap_text:
                result_text += grid_cap_text + "\n"
        if not pv_used:
            result_text += "太陽光発電: 使用しない（風力発電（オフサイトPPA）のみ）\n"
        else:
            result_text += f"年間発電量{'' if wind_info is None else '（太陽光）'}: {result['annual']:.1f} kWh/年\n"
            result_text += f"K' = {result['K_prime']:.4f}\n"
            if bifacial_enabled:
                bif_val = float(bifaciality_val) if bifaciality_val is not None else BIFACIAL_DEFAULTS["bifaciality"]
                result_text += f"パネルタイプ: 両面（bifaciality={bif_val:.2f}）\n"
            result_text += "\n面別年間発電量（PCS制限後）:\n"
            for i, val in enumerate(result['face_annual']):
                pcs_str = f"{faces[i]['pcs_limit_kw']} kW" if faces[i].get('pcs_limit_kw') else "制限なし"
                result_text += f"  面{i+1}: {val:.1f} kWh（PCS: {pcs_str}）\n"

        if wind_info is not None:
            wa = WIND_AREA_META[wind_info["area_code"]]
            pv_annual = float(result["annual"])
            result_text += f"\n── 風力発電（オフサイトPPA）・{wind_info['area_name']}エリア ──\n"
            result_text += f"契約容量: {wind_info['capacity_kw']:,.1f} kW\n"
            result_text += (f"設備利用率: {wind_info['cf_pct']:.1f}%"
                            f"（クリップ後の実効 {wind_info['effective_cf_pct']:.2f}%）\n")
            # 風力の量は2つの基準がある（W2f）: 発電端（発電した全量。支払・発電側課金の基準）と、
            # 需要地点に届く分（到達可能量＝発電量×(1−損失率)。需給・自家消費率・グラフ・24/7の基準）。混ぜずに明記する
            w_deliverable = float(np.sum(wind_info.get("deliverable_30min", wind_info["gen_30min"])))
            result_text += f"年間発電量（発電端）: {wind_info['annual_kwh']:,.1f} kWh/年\n"
            result_text += (f"需要地に届く電力量: {w_deliverable:,.1f} kWh/年"
                            f"（発電量 − 送電ロス。損失率 {wind_info['loss_rate'] * 100:.1f}%）\n")
            if pv_used:
                result_text += (f"太陽光の年間発電量＋風力発電（オフサイトPPA）の需要地に届く電力量: {pv_annual + w_deliverable:,.1f} kWh/年"
                                "（以降の需給・自家消費率・グラフはこの基準）\n")
            else:
                result_text += "（以降の需給・自家消費率・グラフは、需要地に届く電力量の基準）\n"
            if wind_info["clipped_kwh"] > 0:
                result_text += (f"  ※ 契約容量で頭打ち: {wind_info['clipped_kwh']:,.1f} kWh/年（{wind_info['clipped_pct']:.2f}%）。"
                                "設備利用率が高いと形状のピークが定格を超えるため\n")
            result_text += (f"形状の出典: {wa['name']}エリア需給実績 2025年（風力発電実績＋風力出力制御量＝出力制御前）。"
                            f"エリアの出力制御 {wa['curtail_pct']:.2f}%\n")
            if offsite_spec is not None and bat_enabled and battery_mode_label == "最適充放電（LP）":
                result_text += ("以降の蓄電池の最適化（LP）は受電点の基準で行います"
                                "（風力発電（オフサイトPPA）は送配電網で届く電源で、受電量・契約電力・受電上限に含まれます）\n")
            elif offsite_spec is not None and bat_enabled and battery_mode_label == "ルールベース":
                result_text += ("以降の蓄電池（ルールベース）は太陽光の余剰だけを貯め、太陽光と蓄電池で賄えない分を"
                                "風力発電（オフサイトPPA）→小売の順に埋めます（風力発電（オフサイトPPA）は蓄電池に貯めません。貯める運転は最適充放電（LP）で扱います）\n")
            else:
                result_text += ("以降の需給・蓄電池・料金の計算は、太陽光の発電量と風力発電（オフサイトPPA）の需要地に届く電力量の合計で行います\n"
                                if pv_used else "以降の需給・蓄電池・料金の計算は、風力発電（オフサイトPPA）の需要地に届く電力量で行います\n")

        if sc_result is not None:
            result_text += "\n── 需給バランス ──\n"
            result_text += f"年間需要量: {sc_result['annual_demand']:.1f} kWh/年\n"
            result_text += f"自家消費量: {sc_result['annual_self']:.1f} kWh/年\n"
            if wind_info is None:
                result_text += f"自家消費率: {sc_result['self_consumption_rate']:.1f}%（自家消費÷発電）\n"
            else:
                result_text += (f"自家消費率: {sc_result['self_consumption_rate']:.1f}%"
                                "（自家消費÷発電。風力発電（オフサイトPPA）は需要地に届く電力量で数える）\n")
            result_text += f"自給率: {sc_result['self_sufficiency_rate']:.1f}%（自家消費÷需要）\n"
            if no_export:
                result_text += f"出力抑制量: {sc_result.get('annual_curtailment', 0):.1f} kWh/年\n"
                result_text += f"売電モード: 逆潮流禁止（売電なし）\n"
            else:
                result_text += f"余剰売電量: {sc_result['annual_export']:.1f} kWh/年\n"
                actual_sell_price = sell_price if sell_price is not None else DEFAULT_SELL_PRICE
                sell_revenue = sc_result['annual_export'] * actual_sell_price
                result_text += f"売電単価: {actual_sell_price:.2f} 円/kWh\n"
                result_text += f"売電収入: {sell_revenue:,.0f} 円/年\n"
            result_text += f"系統購入量: {sc_result['annual_import']:.1f} kWh/年\n"
            if bat_enabled and "annual_charge" in sc_result:
                result_text += f"\n── 蓄電池 ──\n"
                result_text += f"充放電モード: {battery_mode_label}\n"
                ch_kwh = sc_result['annual_charge']
                dc_kwh = sc_result['annual_discharge']
                loss_kwh = ch_kwh - dc_kwh
                if dc_info is not None:
                    # DC: LPのゼロ解が -0.0 と表示されるのを 0 に丸める（産業用の表示は従来どおり）
                    ch_kwh, dc_kwh, loss_kwh = (v if abs(v) > 1e-6 else 0.0 for v in (ch_kwh, dc_kwh, loss_kwh))
                result_text += f"年間充電量: {ch_kwh:.1f} kWh/年\n"
                result_text += f"年間放電量: {dc_kwh:.1f} kWh/年\n"
                result_text += f"充放電損失: {loss_kwh:.1f} kWh/年\n"
                if dc_info is not None and ch_kwh == 0.0 and dc_kwh == 0.0:
                    # DC特有: 需要が平坦だと契約電力を下げられず、料金に日内差もないため蓄電池の価値は
                    # 構造的にゼロになりうる（バグではない。設計書 §11）
                    result_text += ("  ※ 充放電量ゼロ: この条件では蓄電池に裁定余地がありません。DCの需要が平坦\n"
                                    "     （24時間一定）だと契約電力を下げられず、料金にも日内の差（時間帯別単価）が\n"
                                    "     ないためです（LPは正しく充放電ゼロを返しています）。\n"
                                    "     IT負荷を日変動／CEC実測形状にする・PV容量を増やして余剰を作る・\n"
                                    "     受電上限制約（DCタブの「系統受電上限」）で価値が出ます。\n")
                if sc_result.get("optimized") and wind_info is None:
                    result_text += f"最適化ピークデマンド: {sc_result['opt_peak_kw']:.1f} kW\n"
                    result_text += f"最適化年間コスト: {sc_result['opt_annual_cost']:,.0f} 円\n"
                elif sc_result.get("optimized"):
                    # 風力あり: LPは受電点の基準（ピーク＝受電量の最大、コスト＝小売＋風力配達分の託送等−売電）。
                    # 風力PPAの支払は運転によらない定数なので、LPの目的関数には含めない
                    result_text += f"最適化ピークデマンド（受電点）: {sc_result['opt_peak_kw']:.1f} kW\n"
                    result_text += (f"最適化年間コスト: {sc_result['opt_annual_cost']:,.0f} 円"
                                    "（受電点の電気代−売電収入。風力発電（オフサイトPPA）の支払は含まない）\n")

        # --- 24/7（時間単位の再エネ一致）: 風力を使うときの主指標 ---
        if wind_info is not None and sc_result is not None:
            result_text += format_247(result["total_gen_clipped"], wind_info, demand_30min, sc_result,
                                      result["month_day"], pv_used)

        # --- 電気料金計算（導入前後比較） ---
        is_ehv = (contract_type == "特別高圧")
        defaults = ELECTRICITY_RATE_EHV if is_ehv else ELECTRICITY_RATE_HV
        cost_before = None
        cost_after = None
        if demand_30min is not None:
            rate_params = dict(
                basic_charge_per_kw=elec_basic if elec_basic is not None else defaults["basic_charge_per_kw"],
                energy_charge_summer=elec_summer if elec_summer is not None else defaults["energy_charge_summer"],
                energy_charge_other=elec_other if elec_other is not None else defaults["energy_charge_other"],
                power_factor_pct=elec_pf if elec_pf is not None else defaults["power_factor_pct"],
                fuel_adjustment=elec_fuel if elec_fuel is not None else defaults["fuel_adjustment"],
                renewable_surcharge=elec_renewable if elec_renewable is not None else defaults["renewable_surcharge"],
            )
            # 導入前: 需要データそのまま
            cost_before = calc_electricity_cost(
                demand_30min, result["month_day"], **rate_params
            )
            # 導入後: 系統購入量（自家消費・蓄電池考慮後）
            if sc_result is not None:
                if wind_off is not None:
                    cost_after = offsite_cost_after(wind_off, offsite_sources, result["month_day"], rate_params)
                else:
                    cost_after = calc_electricity_cost(
                        sc_result["import_"], result["month_day"], **rate_params
                    )

            contract_label = "特別高圧" if is_ehv else "高圧"
            result_text += f"\n══ {contract_label}電気料金（年間） ══\n"
            result_text += f"【導入前】\n"
            result_text += f"  契約電力: {cost_before['contract_power_kw']:.1f} kW\n"
            result_text += f"  基本料金: {cost_before['annual_basic']:,.0f} 円/年（{cost_before['monthly_basic']:,.0f} 円/月）\n"
            result_text += f"  電力量料金: {cost_before['annual_energy_charge']:,.0f} 円/年\n"
            result_text += f"  合計: {cost_before['annual_total']:,.0f} 円/年\n"

            if cost_after is not None:
                result_text += f"【導入後】\n"
                result_text += f"  契約電力: {cost_after['contract_power_kw']:.1f} kW\n"
                result_text += f"  基本料金: {cost_after['annual_basic']:,.0f} 円/年（{cost_after['monthly_basic']:,.0f} 円/月）\n"
                result_text += f"  電力量料金: {cost_after['annual_energy_charge']:,.0f} 円/年\n"
                result_text += f"  合計: {cost_after['annual_total']:,.0f} 円/年\n"

                saving = cost_before['annual_total'] - cost_after['annual_total']
                result_text += f"【削減効果】\n"
                result_text += f"  契約電力削減: {cost_before['contract_power_kw'] - cost_after['contract_power_kw']:.1f} kW\n"
                result_text += f"  基本料金削減: {cost_before['annual_basic'] - cost_after['annual_basic']:,.0f} 円/年\n"
                result_text += f"  電力量料金削減: {cost_before['annual_energy_charge'] - cost_after['annual_energy_charge']:,.0f} 円/年\n"
                # 売電収入を含めた総合メリット
                if not no_export and sc_result is not None:
                    actual_sell_price = sell_price if sell_price is not None else DEFAULT_SELL_PRICE
                    sell_rev = sc_result['annual_export'] * actual_sell_price
                    total_merit = saving + sell_rev
                    result_text += f"  売電収入: {sell_rev:,.0f} 円/年\n"
                    result_text += f"  年間コスト削減: {saving:,.0f} 円/年\n"
                    if wind_info is None:
                        result_text += f"  総合経済メリット: {total_merit:,.0f} 円/年\n"
                    else:
                        # 風力ありでは、この額は風力発電（オフサイトPPA）の支払を引く前。損得は【年間の損得】で示す
                        result_text += (f"  電気代の削減＋売電収入: {total_merit:,.0f} 円/年"
                                        "（風力発電（オフサイトPPA）の支払の前。損得は下の【年間の損得】）\n")
                else:
                    result_text += f"  年間コスト削減: {saving:,.0f} 円/年\n"

        # --- 風力オフサイトPPAの支払と、風力込みの年間経済メリット（W2f。設計は wind_design_spec.md §9） ---
        # 発電側の支払（PPA・発電側課金・発電バランシング）は offsite_payment で計算する。年間経済メリットからこの支払を引く。
        # 届いた風力には託送の従量分・再エネ賦課金・小売グロスマージンもかかる（導入後の電力量料金に含めて計算済み）。
        # 初期投資（PV・蓄電池・受電設備）には風力を含めない（オフサイトPPAは設備を持たない）
        wind_payment = 0.0       # 発電側の支払（ppa＋gen_charge＋balancing）。年間経済メリットから引く定数
        wind_cost_total = 0.0    # 発電側の支払 ＋ 届いた分の費用（託送・賦課金・小売GM）。表示・MGの費用に使う
        if wind_info is not None:
            G = wind_info["annual_kwh"]
            w_del = wind_info.get("delivered_kwh", 0.0)
            w_waste = wind_info.get("wasted_kwh", 0.0)
            w_loss = wind_info.get("loss_kwh", 0.0)
            # 風力は需要なしではエラーで止まるので、ここでは rate_params が必ずある（MCPと同じ共通関数で計算する）
            sur_w = rate_params["renewable_surcharge"]
            wc = offsite_source_cost(wind_info, w_del, sur_w)
            pay = wc["payment"]
            wind_payment = pay["total"]
            delivered_extra_yen = wc["delivered_extra"]
            wind_cost_total = wc["total"]
            ps = wind_info["price_sources"]

            def src(key):
                # ps（wind_args で上書きされうる項目）に無いキーは PRICE_SOURCES の既定区分にフォールバックする
                # （renewable_surcharge は wind_args のキーではないため ps に無い。空の [] にならないように）
                cat = ps.get(key, PRICE_SOURCES.get(key, ("", ""))[0])
                return f"[{PRICE_SOURCE_LABELS.get(cat, cat)}]"

            result_text += f"\n══ 風力発電（オフサイトPPA）の電力量と費用（{wind_info['area_name']}エリア・送配電網で届く電源） ══\n"
            result_text += "【電力量】\n"
            result_text += f"  年間発電量（発電端）: {G:,.1f} kWh/年\n"
            result_text += (f"  送電ロス: {w_loss:,.1f} kWh/年（損失率 {wind_info['loss_rate'] * 100:.1f}%・"
                            f"{wind_info['area_name']}・{wind_info['contract_type']}） {src('loss_rate')}\n")
            result_text += f"  使用電力量: {w_del:,.1f} kWh/年（需要地に届く電力量のうち、需要・蓄電池で使った量）\n"
            result_text += (f"  余剰電力量（発電端）: {w_waste:,.1f} kWh/年"
                            "（売電できない。需要家の収入にはならない。余剰の処理は小売が行う）\n")
            basis_note = "（発電量の全量に払う）" if wind_info["payment_basis"] == "generated" else "（使用電力量に払う）"
            result_text += f"【費用】支払の対象: {wind_info['payment_basis_label']}{basis_note} {src('payment_basis')}\n"
            if pay["basis"] == "generated":
                result_text += (f"  PPA発電単価: {wind_info['ppa_price']:.2f} 円/kWh × {G:,.1f} kWh = "
                                f"{pay['ppa']:,.0f} 円 {src('ppa_price')}\n")
                if wind_info["gen_charge_mode"] == "add":
                    result_text += (f"  発電側課金: {pay['gen_charge']:,.0f} 円"
                                    f"（割引 {wind_info['gen_charge_discount_yen']:,.0f} 円控除後）{src('gen_charge')}"
                                    "（PPA単価に含まれないため加算）\n")
                else:
                    result_text += (f"  発電側課金: {pay['gen_charge']:,.0f} 円（割引 "
                                    f"{wind_info['gen_charge_discount_yen']:,.0f} 円控除後）{src('gen_charge')}"
                                    "（PPA単価に含むため参考額。合計には加算しない）\n")
                result_text += (f"  発電バランシング: {wind_info['balancing_yen']:.2f} 円/kWh × {G:,.1f} kWh = "
                                f"{pay['balancing']:,.0f} 円 {src('balancing')}\n")
                result_text += f"  風力発電（オフサイトPPA）の支払 計: {pay['total']:,.0f} 円\n"
            else:
                gc_note = (f"（発電側課金はPPA単価に含む。参考額 {pay['gen_charge']:,.0f} 円は使用電力量で配分した額で、合計には加算しない）"
                           if wind_info["gen_charge_mode"] == "included" else "（発電側課金を加算）")
                per_kwh_gen = pay["total"] / w_del if w_del > 0 else 0.0
                result_text += (f"  発電側単価: {per_kwh_gen:.2f} 円/kWh{gc_note}"
                                f"（PPA・損失の割り戻し・発電バランシングを含む。損失分 {pay['loss_part']:,.0f} 円）\n")
                result_text += f"  風力発電（オフサイトPPA）の支払 計: {pay['total']:,.0f} 円（使用電力量 {w_del:,.1f} kWh 分）\n"
            result_text += (f"  使用電力量にかかる託送・再エネ賦課金・小売グロスマージン: {w_del:,.1f} kWh ×"
                            f"（託送 {wind_info['wheeling_yen']:.2f} ＋ 再エネ賦課金 {sur_w:.2f} ＋ "
                            f"小売グロスマージン {wind_info['retail_fee_yen']:.2f}）= {delivered_extra_yen:,.0f} 円\n")
            result_text += (f"    託送 {src('wheeling')}・再エネ賦課金 {src('renewable_surcharge')}・"
                            f"小売グロスマージン {src('retail_fee')}\n")
            per_kwh_all = wind_cost_total / w_del if w_del > 0 else 0.0
            result_text += (f"  風力発電（オフサイトPPA）の費用（支払＋託送等）: {wind_cost_total:,.0f} 円 ／ "
                            f"使用電力量1kWhあたり {per_kwh_all:.2f} 円\n")
            if wind_info["gen_charge_discount_yen"] <= 0:
                result_text += "  ※ 未算入: 系統設備効率化割引（接続変電所で決まるため0円＝最大で計算）\n"
            if wind_info.get("gen_charge_auto_included"):
                result_text += "  " + GEN_CHARGE_AUTO_INCLUDED_NOTE + "\n"
            if pay["basis"] == "used":
                # §9-4: 使用量払いでは、常に余剰の割合とこの注記を出す（閾値は置かない。根拠のある閾値がないため）
                result_text += "  " + used_basis_surplus_note(w_waste, G, wind_info["retail_fee_yen"]) + "\n"
            if cost_before is not None and w_del > 0:
                avg_unit = ((rate_params["energy_charge_summer"] * 0.25 + rate_params["energy_charge_other"] * 0.75)
                            + rate_params["fuel_adjustment"] + sur_w)
                result_text += (f"  参考: 小売から買う場合の電力量単価は約 {avg_unit:.2f} 円/kWh"
                                f"（風力発電（オフサイトPPA）の使用電力量1kWhあたり {per_kwh_all:.2f} 円と比較）\n")
            # 契約電力を下げうるのは敷地内の太陽光・蓄電池だけ。使っているものだけを書く
            local_w = "・".join(n for n, on in (("太陽光", pv_used), ("蓄電池", bool(
                bat_enabled and bat_capacity and bat_capacity > 0 and battery_mode_label != "最適容量探索"))) if on)
            if cost_after is not None and cost_before is not None:
                result_text += ("【契約電力（基本料金）】風力発電（オフサイトPPA）では下がりません（受電点の最大は、送配電網で届く風力発電（オフサイトPPA）も含めて決まるため）\n"
                                f"  導入前 {cost_before['contract_power_kw']:.1f} kW → 導入後 {cost_after['contract_power_kw']:.1f} kW"
                                + (f"（{local_w}の効果のみ）\n" if local_w else "（太陽光・蓄電池を使っていないため変わらない）\n"))
            result_text += ("※ 売電・出力抑制の対象は敷地内の太陽光の余剰だけです（風力発電（オフサイトPPA）の余剰電力量は売電できません）\n"
                            if pv_used else "※ 風力発電（オフサイトPPA）の余剰電力量は売電できません\n")
            if cost_after is not None and cost_before is not None and sc_result is not None:
                # 【年間の損得】A（導入しない場合）から B（導入した場合）への増減を1行ずつ並べる（2026-09-26、ユーザー指摘）
                sell_rev_w = 0.0 if no_export else sc_result['annual_export'] * (
                    sell_price if sell_price is not None else DEFAULT_SELL_PRICE)
                bat_active_w = bool(bat_enabled and bat_capacity and bat_capacity > 0 and battery_mode_label != "最適容量探索")
                bal = annual_balance(cost_before, cost_after, wind_off, result["month_day"], rate_params,
                                     wind_payment, sell_rev_w)
                result_text += format_annual_balance(
                    bal, pv_used=pv_used, pv_kw=sum(f["ppeak"] for f in faces) if pv_used else 0.0,
                    bat_kwh=bat_capacity if bat_active_w else 0.0, wind_kw=wind_info["capacity_kw"],
                    used_kwh=w_del, no_export=no_export)

        # --- デマンド追跡サマリー ---
        if cost_before is not None:
            result_text += f"\n── デマンド追跡（月別最大kW） ──\n"
            result_text += f"  {'月':>3s}  {'導入前':>8s}"
            if cost_after is not None:
                result_text += f"  {'導入後':>8s}  {'削減':>8s}"
            result_text += "\n"
            for m in range(1, 13):
                bval = cost_before['monthly_max_demand_kw'].get(m, 0)
                result_text += f"  {m:2d}月  {bval:8.1f}"
                if cost_after is not None:
                    aval = cost_after['monthly_max_demand_kw'].get(m, 0)
                    result_text += f"  {aval:8.1f}  {bval - aval:8.1f}"
                result_text += "\n"

        # --- 初期投資・投資回収 ---
        total_ppeak = sum(f["ppeak"] for f in faces)
        pv_unit = pv_cost_per_kw if pv_cost_per_kw is not None else PV_COST_PER_KW
        bat_unit = bat_cost_per_kwh if bat_cost_per_kwh is not None else BATTERY_COST_PER_KWH
        pv_investment = total_ppeak * pv_unit
        bat_investment = 0
        # 最適容量探索モードでは、上部の需給・料金は蓄電池なしで計算している（蓄電池は下の探索で別に評価する）。
        # 入力欄の容量（別モードで入れた値が残りうる）で費用だけ数えると、動かしていない蓄電池の費用が乗ってしまう
        if bat_enabled and bat_capacity and bat_capacity > 0 and battery_mode_label != "最適容量探索":
            bat_investment = bat_capacity * bat_unit
        total_investment = pv_investment + bat_investment

        # 特別高圧受電設備工事費（高圧→特高変更時のみ）
        # 条件: ユーザーが特別高圧を選択 かつ 導入前契約電力が2000kW以下（元は高圧）
        substation_cost = 0
        sub_unit = substation_cost_per_kva if substation_cost_per_kva is not None else SUBSTATION_COST_PER_KVA
        if is_ehv and cost_before is not None and cost_before['contract_power_kw'] <= 2000:
            substation_cost = cost_before['contract_power_kw'] * sub_unit
            total_investment += substation_cost

        # 補助金計算
        subsidy_pv = 0
        subsidy_bat = 0
        if subsidy_enabled:
            pv_pct = subsidy_pv_pct if subsidy_pv_pct is not None else 0
            bat_pct = subsidy_bat_pct if subsidy_bat_pct is not None else 0
            subsidy_pv = pv_investment * pv_pct / 100.0
            subsidy_bat = bat_investment * bat_pct / 100.0
        total_subsidy = subsidy_pv + subsidy_bat
        net_investment = total_investment - total_subsidy

        result_text += f"\n══ 初期投資・投資回収 ══\n"
        result_text += f"【設備投資】\n"
        result_text += f"  PV: {total_ppeak:.1f} kW × {pv_unit:,.0f} 円/kW = {pv_investment:,.0f} 円\n"
        if bat_investment > 0:
            result_text += f"  蓄電池: {bat_capacity:.1f} kWh × {bat_unit:,.0f} 円/kWh = {bat_investment:,.0f} 円\n"
        elif bat_enabled and battery_mode_label == "最適容量探索":
            result_text += ("  蓄電池: 含めません（最適容量探索モードでは、この節は蓄電池なしの試算です。"
                            "蓄電池込みの投資額・回収年数は下の「最適蓄電池容量探索」を参照）\n")
        if substation_cost > 0:
            result_text += f"  特別高圧工事費: {cost_before['contract_power_kw']:.0f} kVA × {sub_unit:,.0f} 円/kVA = {substation_cost:,.0f} 円\n"
        result_text += f"  設備投資合計: {total_investment:,.0f} 円\n"
        if total_subsidy > 0:
            result_text += f"【補助金】\n"
            if subsidy_pv > 0:
                result_text += f"  PV補助金: {subsidy_pv_pct:.0f}% → {subsidy_pv:,.0f} 円\n"
            if subsidy_bat > 0:
                result_text += f"  蓄電池補助金: {subsidy_bat_pct:.0f}% → {subsidy_bat:,.0f} 円\n"
            result_text += f"  補助金合計: {total_subsidy:,.0f} 円\n"
            result_text += f"  実質投資額: {net_investment:,.0f} 円\n"

        # 投資回収年数（年間経済メリットがある場合）
        annual_merit = 0
        if cost_after is not None and cost_before is not None:
            saving = cost_before['annual_total'] - cost_after['annual_total']
            if not no_export and sc_result is not None:
                actual_sell_price_val = sell_price if sell_price is not None else DEFAULT_SELL_PRICE
                sell_rev = sc_result['annual_export'] * actual_sell_price_val
                annual_merit = saving + sell_rev
            else:
                annual_merit = saving
            if wind_info is not None:
                annual_merit -= wind_payment  # 風力PPA支払を引いた、風力込みの年間メリット

        if wind_info is not None and net_investment <= 0:
            # 風力のみ（PV・蓄電池を持たない）: 回収すべき初期投資がない。メリットが負（赤字）でも同じ
            result_text += f"【投資回収】\n"
            result_text += f"  年間経済メリット: {annual_merit:,.0f} 円/年\n"
            result_text += f"  初期投資がないため、投資回収年数は該当しません（風力発電（オフサイトPPA）のみ）\n"
        elif annual_merit > 0:
            payback_years = net_investment / annual_merit
            result_text += f"【投資回収】\n"
            result_text += f"  年間経済メリット: {annual_merit:,.0f} 円/年\n"
            result_text += f"  単純投資回収年数: {payback_years:.1f} 年\n"
        elif net_investment > 0:
            result_text += f"【投資回収】\n"
            result_text += f"  年間経済メリットが算出できないため回収年数は計算不可\n"

        # --- CO2削減量 ---
        if sc_result is not None:
            ef = co2_factor if co2_factor is not None else CO2_EMISSION_FACTOR
            # 系統購入削減量 = 導入前需要 - 導入後系統購入
            grid_reduction = sc_result['annual_demand'] - sc_result['annual_import']
            co2_reduction = grid_reduction * ef
            result_text += f"\n══ CO2削減量 ══\n"
            result_text += f"  排出係数: {ef:.6f} t-CO2/kWh\n"
            result_text += f"  系統購入削減量: {grid_reduction:,.1f} kWh/年\n"
            result_text += f"  CO2削減量: {co2_reduction:,.3f} t-CO2/年\n"

        # --- MG投資額の事前計算（事業モデル計算で使用） ---
        mg_line_cost = 0
        mg_total_investment = net_investment
        mg_opex_r = 0
        mg_annual_opex = 0
        if mg_enabled:
            mg_dist = mg_line_distance if mg_line_distance is not None else MG_LINE_DISTANCE_KM
            mg_cost_km = mg_line_cost_per_km if mg_line_cost_per_km is not None else MG_LINE_COST_PER_KM
            mg_opex_r = (mg_opex_ratio if mg_opex_ratio is not None else MG_OPEX_RATIO) / 100.0
            mg_line_cost = mg_dist * mg_cost_km
            mg_total_investment = net_investment + mg_line_cost
            mg_annual_opex = mg_total_investment * mg_opex_r

        # --- 事業モデル計算（リース/PPA） ---
        biz_model = business_model if business_model else "自己所有"
        ppa_price = None  # MG収益計算で参照
        # 風力を併用するときは、リース料・PPA単価の対象を「太陽光＋蓄電池の設備」に限る。
        # 需要家メリットは風力の費用（PPA支払・届いた分の託送等）を引いた値（annual_merit）。
        # PPA単価の分母は、自家消費量から風力の配達量を引いた敷地内の太陽光・蓄電池分
        # （自家消費 ＝ 敷地内の太陽光・蓄電池分 ＋ 風力の配達量。蓄電池なしなら厳密に成り立つ）
        merit_label = "電気代削減" if wind_info is None else "年間経済メリット（A − B）"
        pv_self_kwh = sc_result['annual_self'] if sc_result is not None else 0.0
        if wind_info is not None and sc_result is not None:
            pv_self_kwh = max(0.0, pv_self_kwh - wind_info.get("delivered_kwh", 0.0))
        # 風力の調達費用（発電側の支払＋届いた分の託送の従量分・再エネ賦課金・小売グロスマージン）。
        # wind_cost_total は上の【風力にかかる費用】で計算済み（offsite_payment。W2f）。MGの費用・PPA単価の逆算に使う
        if biz_model in ("リース", "PPA") and net_investment > 0:
            n_years = int(contract_years) if contract_years is not None else DEFAULT_CONTRACT_YEARS
            r = (target_irr if target_irr is not None else DEFAULT_TARGET_IRR) / 100.0
            # 資本回収係数（CRF）で年間リース料を逆算
            if r > 0:
                crf = r * (1 + r) ** n_years / ((1 + r) ** n_years - 1)
            else:
                crf = 1.0 / n_years
            # A案: MG有効時はMG投資全額（PV+蓄電池+自営線）をベースに、運営コストも含めて逆算
            if mg_enabled:
                lease_base_investment = mg_total_investment
                annual_lease = lease_base_investment * crf + mg_annual_opex
            else:
                lease_base_investment = net_investment
                annual_lease = lease_base_investment * crf

            result_text += f"\n══ 事業モデル: {biz_model} ══\n"
            result_text += f"【事業者側パラメータ】\n"
            result_text += f"  投資ベース: {lease_base_investment:,.0f} 円"
            if mg_enabled:
                result_text += f"（MG投資全額）"
            result_text += f"\n"
            if mg_enabled and mg_annual_opex > 0:
                result_text += f"  年間運営コスト: {mg_annual_opex:,.0f} 円/年（単価に織込み済）\n"
            result_text += f"  契約年数: {n_years} 年\n"
            result_text += f"  目標P-IRR: {r*100:.1f}%\n"

            if biz_model == "リース":
                monthly_lease = annual_lease / 12
                result_text += f"  必要リース料: {annual_lease:,.0f} 円/年（{monthly_lease:,.0f} 円/月）\n"
                result_text += f"\n【需要家メリット（{n_years}年間）】\n"
                if annual_merit > 0:
                    customer_annual = annual_merit - annual_lease
                    customer_total = customer_annual * n_years
                    result_text += f"  {merit_label}: {annual_merit:,.0f} 円/年\n"
                    result_text += f"  リース料負担: {annual_lease:,.0f} 円/年\n"
                    result_text += f"  需要家年間メリット: {customer_annual:,.0f} 円/年\n"
                    result_text += f"  需要家{n_years}年間合計: {customer_total:,.0f} 円\n"
                    if customer_annual >= 0:
                        result_text += f"  → 提案可能（需要家にメリットあり）\n"
                    else:
                        result_text += f"  → 条件見直し必要（需要家にデメリット）\n"
                else:
                    result_text += f"  電気代削減効果なし → 提案不可\n"

            elif biz_model == "PPA":
                # MG＋風力: 風力の調達費用も網内の需要家が負担するので、単価は
                #   （投資の回収 ＋ 風力の調達費用）÷ 網内に供給した全量 で逆算する。
                # それ以外（風力を併用しても、MGでなければ）は 投資の回収 ÷ 敷地内の太陽光・蓄電池分
                mg_wind = bool(mg_enabled and wind_info is not None)
                ppa_denom = sc_result['annual_self'] if (mg_wind and sc_result is not None) else pv_self_kwh
                if sc_result is not None and ppa_denom > 0:
                    ppa_required = annual_lease + (wind_cost_total if mg_wind else 0.0)
                    ppa_price = ppa_required / ppa_denom
                    result_text += f"  必要PPA単価: {ppa_price:.2f} 円/kWh\n"
                    result_text += f"  年間自家消費量: {ppa_denom:,.1f} kWh/年\n"
                    if mg_wind:
                        result_text += (f"    （網内に供給した全量。投資の回収 {annual_lease:,.0f} 円/年 ＋ 風力発電（オフサイトPPA）の費用 "
                                        f"{wind_cost_total:,.0f} 円/年 を割った単価）\n")
                    elif wind_info is not None:
                        result_text += "    （風力発電（オフサイトPPA）の使用電力量を除いた、敷地内の太陽光・蓄電池分）\n"
                    result_text += f"\n【需要家メリット（{n_years}年間）】\n"
                    ppa_annual_cost = ppa_price * ppa_denom
                    if mg_wind:
                        # 需要家は風力の費用をPPA単価に含めて払うので、電気代削減は風力の費用を除いた額で並べる
                        merit_shown = annual_merit + wind_cost_total
                        result_text += f"  電気代削減（風力発電（オフサイトPPA）の費用を除く）: {merit_shown:,.0f} 円/年\n"
                        result_text += (f"  PPA支払: {ppa_annual_cost:,.0f} 円/年（{ppa_price:.2f}円/kWh。"
                                        "投資の回収＋風力発電（オフサイトPPA）の費用）\n")
                    else:
                        merit_shown = annual_merit
                        result_text += f"  {merit_label}: {annual_merit:,.0f} 円/年\n"
                        result_text += f"  PPA支払: {ppa_annual_cost:,.0f} 円/年（{ppa_price:.2f}円/kWh）\n"
                    customer_annual = merit_shown - ppa_annual_cost
                    customer_total = customer_annual * n_years
                    result_text += f"  需要家年間メリット: {customer_annual:,.0f} 円/年\n"
                    result_text += f"  需要家{n_years}年間合計: {customer_total:,.0f} 円\n"
                    if customer_annual >= 0:
                        result_text += f"  → 提案可能（需要家にメリットあり）\n"
                    else:
                        result_text += f"  → 条件見直し必要（需要家にデメリット）\n"
                else:
                    result_text += f"  自家消費量が0のためPPA単価を算出できません\n"

        # --- MG収益計算（モードB） ---
        if mg_enabled and sc_result is not None and cost_before is not None:
            mg_period = int(mg_irr_period) if mg_irr_period is not None else MG_IRR_PERIOD

            # --- 基本料金の束ねメリット計算 ---
            # 個別施設がそれぞれ系統契約した場合の基本料金合計 vs MG合算の基本料金
            individual_basic_total = 0
            if individual_demands and len(individual_demands) > 1:
                for ind_demand in individual_demands:
                    ind_cost = calc_electricity_cost(
                        ind_demand, result["month_day"], **rate_params
                    )
                    individual_basic_total += ind_cost['annual_basic']
            else:
                # 施設1つの場合は束ねメリットなし
                individual_basic_total = cost_before['annual_basic']

            # MG合算後の基本料金（PV導入後）
            mg_combined_basic = cost_after['annual_basic'] if cost_after is not None else cost_before['annual_basic']
            # 束ねメリット = 個別基本料金合計 - MG合算後基本料金（PV導入効果含む）
            basic_saving = individual_basic_total - mg_combined_basic

            # --- PV売電収入（網内） ---
            # PPA選択時: PPA単価で計算（MG投資全額を回収する単価）
            # それ以外: 高圧電力量単価の加重平均で計算
            defaults_mg = ELECTRICITY_RATE_EHV if is_ehv else ELECTRICITY_RATE_HV
            avg_energy_price = (
                elec_summer if elec_summer is not None else defaults_mg["energy_charge_summer"]
            ) * 0.25 + (
                elec_other if elec_other is not None else defaults_mg["energy_charge_other"]
            ) * 0.75  # 夏季3ヶ月/12ヶ月の加重平均

            if ppa_price is not None:
                mg_sell_price = ppa_price
                mg_price_label = f"PPA単価 {ppa_price:.2f}円/kWh"
            else:
                mg_sell_price = avg_energy_price
                mg_price_label = f"電力量単価加重平均 {avg_energy_price:.2f}円/kWh"

            # 網内に供給した全量（敷地内の太陽光・蓄電池分＋風力の配達量）を網内単価で売る。
            # 風力は送配電網で届くので、契約電力（基本料金）は下がらず（basic_saving は受電点基準の cost_after から自動で決まる）、
            # 調達費用（PPA支払＋届いた分の託送・賦課金・手数料）がMG事業者の費用になる
            pv_revenue = sc_result['annual_self'] * mg_sell_price
            mg_annual_revenue = pv_revenue + basic_saving
            mg_annual_cashflow = mg_annual_revenue - mg_annual_opex - wind_cost_total

            result_text += f"\n══ マイクログリッド事業 ══\n"
            result_text += f"【初期投資】\n"
            result_text += f"  PV+蓄電池: {net_investment:,.0f} 円\n"
            result_text += f"  自営線: {mg_dist:.1f} km × {mg_cost_km:,.0f} 円/km = {mg_line_cost:,.0f} 円\n"
            result_text += f"  MG投資合計: {mg_total_investment:,.0f} 円\n"
            result_text += f"【年間収支】\n"
            result_text += (f"  {'PV売電収入（網内）' if wind_info is None else '網内売電収入（PV・風力発電（オフサイトPPA）の供給分）'}"
                            f": {pv_revenue:,.0f} 円/年（{mg_price_label}）\n")
            if individual_demands and len(individual_demands) > 1:
                result_text += f"  基本料金差額（束ねメリット含む）: {basic_saving:,.0f} 円/年\n"
                result_text += f"    個別契約時基本料金合計: {individual_basic_total:,.0f} 円/年\n"
                result_text += f"    MG合算後基本料金: {mg_combined_basic:,.0f} 円/年\n"
            else:
                result_text += f"  基本料金差額: {basic_saving:,.0f} 円/年\n"
            result_text += f"  年間収益合計: {mg_annual_revenue:,.0f} 円/年\n"
            result_text += f"  年間運営コスト: {mg_annual_opex:,.0f} 円/年（投資額の{mg_opex_r*100:.1f}%）\n"
            if wind_info is not None:
                w_dl = wind_info.get("delivered_kwh", 0.0)
                result_text += (f"  風力発電（オフサイトPPA）の費用: {wind_cost_total:,.0f} 円/年"
                                f"（支払 {wind_payment:,.0f} ＋ 使用電力量 {w_dl:,.1f} kWh にかかる託送・再エネ賦課金・小売グロスマージン "
                                f"{wind_cost_total - wind_payment:,.0f}）\n")
                result_text += "    ※ 契約電力（基本料金）は風力発電（オフサイトPPA）では下がらないため、束ねメリットは太陽光・蓄電池の効果のみ\n"
            result_text += f"  年間キャッシュフロー: {mg_annual_cashflow:,.0f} 円/年\n"

            # P-IRR計算（numpy IRRがないのでニュートン法で求める）
            cashflows = [-mg_total_investment] + [mg_annual_cashflow] * mg_period
            mg_irr = _calc_irr(cashflows)
            if mg_irr is not None:
                result_text += f"【P-IRR（{mg_period}年）】\n"
                result_text += f"  P-IRR: {mg_irr*100:.2f}%\n"
            else:
                result_text += f"【P-IRR（{mg_period}年）】\n"
                result_text += f"  P-IRR: 算出不可（キャッシュフローが負）\n"

            # 年次キャッシュフロー表
            result_text += f"\n【年次キャッシュフロー】\n"
            result_text += f"  {'年':>3s}  {'CF':>12s}  {'累計':>14s}\n"
            cumulative = -mg_total_investment
            result_text += f"  {'0':>3s}  {-mg_total_investment:>12,.0f}  {cumulative:>14,.0f}\n"
            for y in range(1, mg_period + 1):
                cumulative += mg_annual_cashflow
                result_text += f"  {y:>3d}  {mg_annual_cashflow:>12,.0f}  {cumulative:>14,.0f}\n"

        # --- 需要構成サマリー（産業用のみ。DCは冒頭の「データセンター需要」節が担う） ---
        if demand_30min is not None and dc_info is None:
            result_text += "\n── 需要構成 ──\n"
            for i in range(int(num_facilities)):
                idx = i * 3
                btype = facility_args[idx]
                area = facility_args[idx + 1]
                count = facility_args[idx + 2]
                if btype and btype != "なし" and area and float(area) > 0 and count and int(count) > 0:
                    result_text += f"  {btype}: {float(area):,.0f} m² × {int(count)} 棟\n"
            if demand_custom_csv is not None:
                result_text += f"  ＋ カスタムCSV\n"

        # --- デバッグ情報 ---
        debug_text = f"データソース: {source_text}\n"
        if dc_info is not None:
            bd = dc_info["breakdown"]
            debug_text += (f"需要ソース: データセンター（IT {dc_info['it_capacity_kw']:,.0f}kW, "
                           f"PUE {dc_info['pue_const']:.2f}, {dc_info['profile_mode']}, "
                           f"ノイズ={dc_info['noise_level']}）\n")
            debug_text += f"  年間平均PUE={bd['avg_pue']:.3f} / ピークデマンド={dc_info['peak_demand_kw']:,.1f}kW\n"
        debug_text += f"緯度: {lat:.4f}°  経度: {lon:.4f}°\n"
        debug_text += f"pvlib: {'利用' if HAS_PVLIB else '未使用（GHI直接）'}\n"
        debug_text += f"面数: {len(faces)}\n"
        for i, face in enumerate(faces):
            pcs_str = f"{face['pcs_limit_kw']} kW" if face.get('pcs_limit_kw') else "制限なし"
            debug_text += f"  面{i+1}: Ppeak={face['ppeak']}kW, {face['orientation']}({face['azimuth']:.1f}°), 傾斜{face['tilt']}°, PCS={pcs_str}\n"
        if bifacial_enabled:
            debug_text += f"両面パネル: ON (bifaciality={float(bifaciality_val) if bifaciality_val is not None else BIFACIAL_DEFAULTS['bifaciality']}, "
            debug_text += f"GCR={float(gcr_val) if gcr_val is not None else BIFACIAL_DEFAULTS['gcr']}, "
            debug_text += f"height={float(height_val) if height_val is not None else BIFACIAL_DEFAULTS['height']}m, "
            debug_text += f"pitch={float(pitch_val) if pitch_val is not None else BIFACIAL_DEFAULTS['pitch']}m)\n"
            debug_text += f"積雪albedo切替: {'ON' if snow_albedo_enabled else 'OFF'}\n"
        else:
            debug_text += "両面パネル: OFF（片面モード）\n"
        if wind_info is not None:
            debug_text += (f"風力発電（オフサイトPPA）: {wind_info['area_name']}エリア {wind_info['capacity_kw']:,.1f}kW, "
                           f"設備利用率 {wind_info['cf_pct']:.1f}%, PPA {wind_info['ppa_price']:.2f}円/kWh, "
                           f"託送(従量) {wind_info['wheeling_yen']:.2f}円/kWh, 小売グロスマージン {wind_info['retail_fee_yen']:.2f}円/kWh, "
                           f"損失率 {wind_info['loss_rate'] * 100:.1f}%, 発電側課金({wind_info['gen_charge_mode']}) "
                           f"{wind_info['gen_charge_yen']:,.0f}円/年, バランシング {wind_info['balancing_yen']:.2f}円/kWh, "
                           f"支払の対象={wind_info['payment_basis_label']}, "
                           f"太陽光={'使用' if pv_used else '使用しない'}\n")
        debug_text += f"月別発電量:\n"
        for m in range(1, 13):
            debug_text += f"  {m:2d}月: {result['monthly'].get(m, 0):8.1f} kWh\n"
        if sc_result is not None:
            debug_text += f"\n年間需要量(合算): {sc_result['annual_demand']:.1f} kWh\n"

        # --- デマンド追跡グラフ ---
        fig_demand = None
        if cost_before is not None:
            fig_demand = make_demand_chart(cost_before, cost_after, contract_label)

        # --- 最適容量探索（蓄電池ONのときのみ。非表示中のドロップダウン値の残留対策） ---
        fig_capacity = None
        if bat_enabled and battery_mode_label == "最適容量探索" and demand_30min is not None and cost_before is not None:
            defaults_cap = ELECTRICITY_RATE_EHV if is_ehv else ELECTRICITY_RATE_HV
            rate_p = dict(
                basic_charge_per_kw=elec_basic if elec_basic is not None else defaults_cap["basic_charge_per_kw"],
                energy_charge_summer=elec_summer if elec_summer is not None else defaults_cap["energy_charge_summer"],
                energy_charge_other=elec_other if elec_other is not None else defaults_cap["energy_charge_other"],
                power_factor_pct=elec_pf if elec_pf is not None else defaults_cap["power_factor_pct"],
                fuel_adjustment=elec_fuel if elec_fuel is not None else defaults_cap["fuel_adjustment"],
                renewable_surcharge=elec_renewable if elec_renewable is not None else defaults_cap["renewable_surcharge"],
            )
            bat_eff = bat_efficiency if bat_efficiency is not None else 95
            bat_mc = bat_max_charge if bat_max_charge is not None else 2.5
            bat_md = bat_max_discharge if bat_max_discharge is not None else 2.5
            bat_smin = bat_soc_min if bat_soc_min is not None else 20
            bat_smax = bat_soc_max if bat_soc_max is not None else 95
            sp = sell_price if sell_price is not None else DEFAULT_SELL_PRICE
            n_yrs = int(contract_years) if contract_years is not None else DEFAULT_CONTRACT_YEARS

            # PV・蓄電池の実質単価（補助金反映）
            bat_subsidy_pct = 0
            pv_subsidy_pct = 0
            if subsidy_enabled:
                bat_subsidy_pct = subsidy_bat_pct if subsidy_bat_pct is not None else 0
                pv_subsidy_pct = subsidy_pv_pct if subsidy_pv_pct is not None else 0
            bat_unit_net = bat_unit * (1 - bat_subsidy_pct / 100.0)
            pv_unit_net = pv_unit * (1 - pv_subsidy_pct / 100.0)

            # 段階1: LP一体化
            try:
                cap_result = optimize_battery_capacity(
                    gen_lp, demand_30min, result["month_day"],
                    efficiency_pct=bat_eff,
                    max_charge_kw=bat_mc, max_discharge_kw=bat_md,
                    soc_min_pct=bat_smin, soc_max_pct=bat_smax,
                    sell_price=sp, battery_cost_per_kwh=bat_unit_net, payback_years=n_yrs,
                    no_export=no_export, offsite=offsite_spec, **rate_p,
                )
                opt_cap = cap_result["optimal_capacity_kwh"]

                result_text += f"\n══ 最適蓄電池容量探索 ══\n"
                result_text += f"【段階1: LP一体化】\n"
                result_text += f"  最適蓄電池容量: {opt_cap:.1f} kWh\n"

                # 最適容量でLP最適化を実行して詳細指標を取得
                sc_opt = optimize_battery(
                    gen_lp, demand_30min, result["month_day"],
                    capacity_kwh=opt_cap,
                    efficiency_pct=bat_eff,
                    max_charge_kw=bat_mc, max_discharge_kw=bat_md,
                    soc_min_pct=bat_smin, soc_max_pct=bat_smax,
                    sell_price=sp, no_export=no_export, offsite=offsite_spec, **rate_p,
                )
                if offsite_spec is None:
                    cost_opt = calc_electricity_cost(
                        sc_opt["import_"], result["month_day"], **rate_p,
                    )
                else:
                    # 風力あり: 受電点の基準（契約電力は受電量の最大。届いた風力に託送・賦課金・手数料）
                    off_opt = offsite_receiving(gen_lp, offsite_spec["sources"], demand_30min, sc_opt)
                    cost_opt = offsite_cost_after(off_opt, offsite_spec["sources"], result["month_day"], rate_p)
                saving_opt = cost_before['annual_total'] - cost_opt['annual_total']
                sell_rev_opt = sc_opt['annual_export'] * sp if not no_export else 0
                merit_opt = saving_opt + sell_rev_opt
                if offsite_spec is not None:
                    # 風力の発電側の支払（W2f）。全量払いは容量によらない定数、使用量払いは配達量で決まる
                    merit_opt -= offsite_payment_total(offsite_spec["sources"], off_opt["delivered_by_source"])
                # 段階2グリッドサーチと同じ補助金控除後単価で投資額を計算（整合性）
                bat_inv_opt = opt_cap * bat_unit_net
                total_inv_opt = total_ppeak * pv_unit_net + bat_inv_opt
                payback_opt = total_inv_opt / merit_opt if merit_opt > 0 else 999
                cf_opt = [-total_inv_opt] + [merit_opt] * n_yrs
                irr_opt = _calc_irr(cf_opt)
                ef = co2_factor if co2_factor is not None else CO2_EMISSION_FACTOR
                co2_opt = (sc_opt['annual_demand'] - sc_opt['annual_import']) * ef

                result_text += f"  P-IRR: {irr_opt*100:.1f}%\n" if irr_opt else ""
                result_text += f"  契約電力: {cost_before['contract_power_kw']:.1f} kW → {cost_opt['contract_power_kw']:.1f} kW（{cost_before['contract_power_kw'] - cost_opt['contract_power_kw']:.1f} kW 削減）\n"
                result_text += f"  年間コスト削減: {merit_opt:,.0f} 円/年\n"
                result_text += f"  CO2削減量: {co2_opt:.3f} t-CO2/年\n"
                result_text += f"  投資回収見込み: {payback_opt:.1f} 年\n"

                # 段階2: グリッドサーチ
                search_results = grid_search_battery_capacity(
                    gen_lp, demand_30min, result["month_day"],
                    efficiency_pct=bat_eff,
                    max_charge_kw=bat_mc, max_discharge_kw=bat_md,
                    soc_min_pct=bat_smin, soc_max_pct=bat_smax,
                    sell_price=sp,
                    pv_cost=pv_unit_net,
                    battery_cost_per_kwh=bat_unit_net,
                    total_ppeak=total_ppeak,
                    co2_factor=ef,
                    cost_before_total=cost_before['annual_total'],
                    payback_years=n_yrs,
                    no_export=no_export,
                    optimal_capacity=opt_cap,
                    n_steps=10,
                    offsite=offsite_spec,
                    **rate_p,
                )
                fig_capacity = make_capacity_search_chart(search_results, opt_cap)
                result_text += f"【段階2: グリッドサーチ】探索完了（{len(search_results)}ステップ）\n"

            except Exception as e:
                result_text += f"\n最適容量探索エラー: {e}\n"

        # result_stateに需要関連も含める
        chart_result["sc_result"] = sc_result
        chart_result["demand_30min"] = demand_30min

        return fig_monthly, fig_daily, fig_demand, fig_capacity, result_text, debug_text, chart_result

    except Exception as e:
        import traceback
        return None, None, None, None, f"エラー: {e}", traceback.format_exc(), None


def build_ui():
    """Gradio UIを構築"""
    station_options = get_station_options()

    with gr.Blocks(title="産業用太陽光需給シミュレーター") as demo:
        gr.Markdown("# ☀️ 産業用太陽光需給シミュレーター（JIS C 8907準拠）")
        gr.Markdown("30分単位（48コマ/日）の高精度発電量・需給シミュレーション")

        with gr.Row():
            # ===== 左パネル =====
            with gr.Column(scale=2):
                gr.Markdown("### 📍 地点選択")
                station_input = gr.Dropdown(
                    label="プリセット地点",
                    choices=station_options,
                    value=station_options[0] if station_options else None,
                )
                csv_input = gr.File(label="またはCSVアップロード（NEDO形式）", file_types=[".csv"])

                # --- 需要設定（タブ: 産業用・MG / データセンター） ---
                # 需要設定だけをタブで分割し、地点・PV面・蓄電池・料金・経済性は共通のまま使う
                # （全部をタブ化するとPV面設定8面×5項目などが二重定義になるため。docs/decision_log.md 第5段階）。
                # 選択中のタブは gr.Tab.select → gr.State で保持し、計算時に demand_source として渡す。
                gr.Markdown("### ⚡ 需要設定")
                demand_source_state = gr.State(DEMAND_SOURCE_INDUSTRIAL)

                with gr.Tabs():
                    # ===== タブ1: 産業用・MG（従来の需要設定。複数施設合算） =====
                    with gr.Tab("🏭 産業用・MG") as tab_industrial:
                        gr.Markdown("施設を組み合わせて需要カーブを作成します")
                        num_facilities_input = gr.Slider(
                            label="施設数", minimum=1, maximum=MAX_FACILITIES, step=1, value=1,
                        )

                        facility_components = []  # [type, area, count] × MAX_FACILITIES
                        facility_groups = []

                        for i in range(MAX_FACILITIES):
                            visible = (i == 0)
                            with gr.Group(visible=visible) as grp:
                                gr.Markdown(f"**施設{i+1}**")
                                with gr.Row():
                                    ftype = gr.Dropdown(
                                        label="施設タイプ",
                                        choices=BUILDING_TYPE_CHOICES,
                                        value="なし",
                                    )
                                    farea = gr.Number(
                                        label="延床面積 [m²]",
                                        value=0,
                                        precision=0,
                                    )
                                    fcount = gr.Number(
                                        label="棟数",
                                        value=1,
                                        precision=0,
                                    )
                            facility_components.extend([ftype, farea, fcount])
                            facility_groups.append(grp)

                            # 施設タイプ変更 → デフォルト延床面積を自動設定
                            def on_building_type_change(btype):
                                info = BUILDING_TYPES.get(btype, {})
                                default_area = info.get("default_area_m2", 0)
                                return gr.update(value=default_area)

                            ftype.change(
                                fn=on_building_type_change,
                                inputs=[ftype],
                                outputs=[farea],
                                api_visibility="hidden",
                            )

                        # 施設数変更で表示切替
                        def update_facility_visibility(n):
                            return [gr.update(visible=(i < n)) for i in range(MAX_FACILITIES)]

                        num_facilities_input.change(
                            fn=update_facility_visibility,
                            inputs=[num_facilities_input],
                            outputs=facility_groups,
                            api_visibility="hidden",
                        )

                        demand_csv_input = gr.File(
                            label="カスタム需要CSV（追加合算、timestamp + demand_kWh）",
                            file_types=[".csv"],
                        )

                    # ===== タブ2: データセンター（IT負荷 × PUE） =====
                    with gr.Tab("🏢 データセンター") as tab_datacenter:
                        gr.Markdown(
                            "IT負荷とPUEからDCの電力需要を生成します（30分×365日）。"
                            "<br><small>空冷前提（液冷は対象外）。PUEは一定値のみ（気温連動は保留）。"
                            "地点・PV・蓄電池・電気料金・経済性は下の共通設定を使います。</small>"
                        )
                        workload_input = gr.Dropdown(
                            label="用途",
                            choices=list(DC_WORKLOAD_PRESETS.keys()),
                            value="ハウジング（コロケーション）",
                        )
                        gr.Markdown(
                            "<small>用途を選ぶと負荷プロファイルと短周期変動が自動設定されます"
                            "（「手動設定」で個別指定）。<br>"
                            "ハウジング＝CEC実測形状（米国商用DC約100施設のinterval meter由来）／"
                            "AI学習＝定常（LBNL: 大規模AI学習クラスタは連続バッチで時刻変動なし）</small>"
                        )

                        # --- 容量の指定（3方式。内部では常にIT容量[kW]に正規化） ---
                        capacity_mode_input = gr.Radio(
                            label="容量の指定方法",
                            choices=CAPACITY_MODES,
                            value="規模プリセット",
                        )
                        with gr.Row(visible=True) as cap_preset_row:
                            size_preset_input = gr.Dropdown(
                                label="規模",
                                choices=list(DC_SIZE_PRESETS.keys()),
                                value="中規模",
                            )
                        with gr.Row(visible=False) as cap_direct_row:
                            it_capacity_input = gr.Number(
                                label="IT定格容量 [kW]",
                                value=DC_DEFAULTS["it_capacity_kw"], precision=1,
                            )
                        with gr.Row(visible=False) as cap_rack_row:
                            n_racks_input = gr.Number(
                                label="ラック数", value=DC_DEFAULTS["n_racks"], precision=0,
                            )
                            kw_per_rack_input = gr.Number(
                                label="ラック電力密度 [kW/ラック]",
                                value=DC_DEFAULTS["kw_per_rack"], precision=1,
                            )
                        gr.Markdown(
                            "<small>⚠ 規模区分は<b>暫定値</b>（LBNL Shape Makerは large/medium/small を"
                            "用いるがMW区分は非公開）。エッジ 0.1〜0.5 ／ 小規模 0.5〜2 ／ "
                            "中規模 2〜20 ／ ハイパースケール 20〜100+ MW<br>"
                            f"ラック電力密度の目安（参考値）: {RACK_DENSITY_HINT} kW/ラック<br>"
                            "延床面積では指定しません（DCはラック密度で電力密度が1桁変わるため）</small>"
                        )

                        # --- 負荷プロファイル（用途=手動設定のときのみ表示） ---
                        with gr.Column(visible=False) as profile_manual_group:
                            it_profile_input = gr.Dropdown(
                                label="負荷プロファイル",
                                choices=IT_LOAD_PROFILE_MODES,
                                value=PROFILE_CEC,
                            )
                            noise_input = gr.Dropdown(
                                label="短周期変動（ノイズ）",
                                choices=NOISE_LEVELS,
                                value="低（3〜7%）",
                            )
                        it_load_factor_input = gr.Number(
                            label="IT負荷率（年平均）[%]",
                            value=DC_DEFAULTS["it_load_factor_pct"], precision=1,
                        )
                        with gr.Row(visible=False) as it_daily_row:
                            it_peak_input = gr.Number(
                                label="ピーク負荷率 [%]",
                                value=DC_DEFAULTS["it_peak_pct"], precision=1,
                            )
                            it_bottom_input = gr.Number(
                                label="ボトム負荷率 [%]",
                                value=DC_DEFAULTS["it_bottom_pct"], precision=1,
                            )
                            it_peak_hour_input = gr.Number(
                                label="ピーク時刻 [時]",
                                value=DC_DEFAULTS["it_peak_hour"], precision=0,
                                minimum=0, maximum=23,
                            )
                        gr.Markdown(
                            "<small>「IT負荷率（年平均）」が<b>水準</b>、プロファイルが<b>形状</b>を決めます"
                            "（形状は年平均=1.0に正規化）。CSVアップロードによるIT負荷指定は未実装</small>"
                        )

                        pue_const_input = gr.Number(
                            label="PUE（施設全体電力 ÷ IT機器電力）",
                            value=DC_DEFAULTS["pue_const"], precision=2,
                        )
                        gr.Markdown(
                            "<small>PUEは一定値です。既存DCの実績PUEを持っている場合はそれを直接入力してください"
                            "（気温連動PUEは出典が固まるまで保留）。</small>"
                        )

                        # --- 系統受電上限（受電電圧の階層境界。守る手段は蓄電池の最適充放電LP） ---
                        grid_cap_mode_input = gr.Dropdown(
                            label="系統受電上限",
                            choices=GRID_CAP_MODES,
                            value=GRID_CAP_NONE,
                        )
                        with gr.Row(visible=False) as grid_cap_manual_row:
                            grid_cap_kw_input = gr.Number(
                                label="受電上限 [kW]（手入力）",
                                value=GRID_CAP_MANUAL_DEFAULT_KW, precision=1,
                            )
                        gr.Markdown(
                            "<small>受電電圧は契約電力で決まります（高圧6.6kV: 2,000kW未満／22・33kV: 10,000kW未満／"
                            "それ以上は66kV。154kVは対象外）。「高圧に収める」を選ぶと、PV・蓄電池で系統からの受電を"
                            "1,999kW以下に抑えられるかを判定します（「未満」なので境界の1kW下）。<br>"
                            "上限を<b>守れるのは蓄電池「最適充放電（LP）」のときだけ</b>です（蓄電池「なし」「ルールベース」では"
                            "超過の判定と、必要な蓄電池の目安のみ表示）。守れない場合は理由と必要量の目安を表示します。</small>"
                        )

                # DC入力コンポーネント。DC_INPUT_KEYS と同じ並びにすること（on_click が zip で辞書に戻す）
                dc_components = [
                    workload_input, capacity_mode_input, size_preset_input, it_capacity_input,
                    n_racks_input, kw_per_rack_input, it_profile_input, noise_input,
                    it_load_factor_input, it_peak_input, it_bottom_input, it_peak_hour_input,
                    pue_const_input,
                    grid_cap_mode_input, grid_cap_kw_input,
                ]
                if len(dc_components) != len(DC_INPUT_KEYS):
                    raise RuntimeError("dc_components と DC_INPUT_KEYS の要素数が一致しません")

                # 選択中のタブを需要ソースとして保持する
                tab_industrial.select(
                    fn=lambda: DEMAND_SOURCE_INDUSTRIAL,
                    outputs=[demand_source_state],
                    api_visibility="hidden",
                )
                tab_datacenter.select(
                    fn=lambda: DEMAND_SOURCE_DATACENTER,
                    outputs=[demand_source_state],
                    api_visibility="hidden",
                )

                def on_capacity_mode_change(mode):
                    """容量の指定方法に応じて入力欄を切り替える。"""
                    return (
                        gr.update(visible=(mode == "規模プリセット")),
                        gr.update(visible=(mode == "IT容量を直接入力")),
                        gr.update(visible=(mode == "ラック数×density")),
                    )

                capacity_mode_input.change(
                    fn=on_capacity_mode_change,
                    inputs=[capacity_mode_input],
                    outputs=[cap_preset_row, cap_direct_row, cap_rack_row],
                    api_visibility="hidden",
                )

                def on_workload_change(preset, cur_profile):
                    """用途プリセットでプロファイルとノイズを一括設定する。

                    「手動設定」のときだけ個別指定の欄を出す。日変動の形状パラメータ
                    （ピーク/ボトム/時刻）は、実際に使われるプロファイルが日変動のときだけ表示する。
                    """
                    p = DC_WORKLOAD_PRESETS.get(preset)
                    manual = p is None
                    profile = cur_profile if manual else p["profile"]
                    return (
                        gr.update(visible=manual),
                        gr.update() if manual else gr.update(value=p["profile"]),
                        gr.update() if manual else gr.update(value=p["noise"]),
                        gr.update(visible=(profile == PROFILE_DIURNAL)),
                    )

                workload_input.change(
                    fn=on_workload_change,
                    inputs=[workload_input, it_profile_input],
                    outputs=[profile_manual_group, it_profile_input, noise_input, it_daily_row],
                    api_visibility="hidden",
                )

                it_profile_input.change(
                    fn=lambda mode: gr.update(visible=(mode == PROFILE_DIURNAL)),
                    inputs=[it_profile_input],
                    outputs=[it_daily_row],
                    api_visibility="hidden",
                )

                # 受電上限は「手入力」を選んだときだけ数値欄を出す
                grid_cap_mode_input.change(
                    fn=lambda mode: gr.update(visible=(mode == GRID_CAP_MANUAL)),
                    inputs=[grid_cap_mode_input],
                    outputs=[grid_cap_manual_row],
                    api_visibility="hidden",
                )

                # --- 電気料金設定 ---
                with gr.Accordion("💰 電気料金設定", open=False):
                    contract_type_input = gr.Dropdown(
                        label="契約種別",
                        choices=["高圧", "特別高圧"],
                        value="高圧",
                    )
                    with gr.Row():
                        elec_basic_input = gr.Number(
                            label="基本料金単価 [円/kW・月]",
                            value=ELECTRICITY_RATE_HV["basic_charge_per_kw"], precision=2,
                        )
                        elec_pf_input = gr.Number(
                            label="力率 [%]",
                            value=ELECTRICITY_RATE_HV["power_factor_pct"], precision=0,
                        )
                    with gr.Row():
                        elec_summer_input = gr.Number(
                            label="電力量料金 夏季7-9月 [円/kWh]",
                            value=ELECTRICITY_RATE_HV["energy_charge_summer"], precision=2,
                        )
                        elec_other_input = gr.Number(
                            label="電力量料金 その他季 [円/kWh]",
                            value=ELECTRICITY_RATE_HV["energy_charge_other"], precision=2,
                        )
                    with gr.Row():
                        elec_fuel_input = gr.Number(
                            label="燃料費調整単価 [円/kWh]",
                            value=ELECTRICITY_RATE_HV["fuel_adjustment"], precision=2,
                        )
                        elec_renewable_input = gr.Number(
                            label="再エネ賦課金 [円/kWh]",
                            value=ELECTRICITY_RATE_HV["renewable_surcharge"], precision=2,
                        )

                # --- 売電設定 ---
                with gr.Accordion("🔌 売電・逆潮流設定", open=False):
                    sell_mode_input = gr.Dropdown(
                        label="売電モード",
                        choices=SELL_MODES,
                        value="余剰売電",
                    )
                    sell_scheme_group = gr.Group(visible=True)
                    with sell_scheme_group:
                        sell_scheme_input = gr.Dropdown(
                            label="売電制度",
                            choices=SELL_SCHEMES,
                            value="FIT利用あり",
                        )
                        with gr.Row():
                            fit_year_input = gr.Number(
                                label="経過年数",
                                value=1, precision=0,
                                minimum=1, maximum=30,
                            )
                            sell_price_input = gr.Number(
                                label="売電単価 [円/kWh]",
                                value=FIT_PRICE_EARLY, precision=2,
                                interactive=False,
                            )

                # --- 設備単価設定 ---
                with gr.Accordion("💴 設備単価・補助金", open=False):
                    with gr.Row():
                        pv_cost_input = gr.Number(
                            label="PVシステム単価 [円/kW]",
                            value=PV_COST_PER_KW, precision=0,
                        )
                        bat_cost_input = gr.Number(
                            label="蓄電池単価 [円/kWh]",
                            value=BATTERY_COST_PER_KWH, precision=0,
                        )
                    substation_cost_input = gr.Number(
                        label="特別高圧工事費 [円/kVA]（高圧→特高変更時のみ適用）",
                        value=SUBSTATION_COST_PER_KVA, precision=0,
                    )
                    subsidy_enabled_input = gr.Checkbox(label="補助金を適用", value=False)
                    with gr.Column(visible=False) as subsidy_settings_group:
                        with gr.Row():
                            subsidy_pv_input = gr.Number(
                                label="PV補助率 [%]",
                                value=0, precision=0,
                                minimum=0, maximum=100,
                            )
                            subsidy_bat_input = gr.Number(
                                label="蓄電池補助率 [%]",
                                value=0, precision=0,
                                minimum=0, maximum=100,
                            )

                    subsidy_enabled_input.change(
                        fn=lambda x: gr.update(visible=x),
                        inputs=[subsidy_enabled_input],
                        outputs=[subsidy_settings_group],
                        api_visibility="hidden",
                    )
                    co2_factor_input = gr.Number(
                        label="CO2排出係数 [t-CO2/kWh]",
                        value=CO2_EMISSION_FACTOR, precision=6,
                    )

                # --- 事業モデル設定 ---
                with gr.Accordion("📋 事業モデル設定", open=False):
                    business_model_input = gr.Dropdown(
                        label="事業モデル",
                        choices=BUSINESS_MODELS,
                        value="自己所有",
                    )
                    with gr.Column(visible=False) as lease_ppa_group:
                        with gr.Row():
                            contract_years_input = gr.Number(
                                label="契約年数",
                                value=DEFAULT_CONTRACT_YEARS, precision=0,
                            )
                            target_irr_input = gr.Number(
                                label="目標P-IRR [%]",
                                value=DEFAULT_TARGET_IRR, precision=1,
                            )

                    def on_business_model_change(model):
                        return gr.update(visible=(model in ("リース", "PPA")))

                    business_model_input.change(
                        fn=on_business_model_change,
                        inputs=[business_model_input],
                        outputs=[lease_ppa_group],
                        api_visibility="hidden",
                    )

                # --- マイクログリッド設定 ---
                with gr.Accordion("🔗 マイクログリッド設定", open=False):
                    mg_enabled_input = gr.Checkbox(label="マイクログリッドモード", value=False)
                    with gr.Column(visible=False) as mg_settings_group:
                        with gr.Row():
                            mg_line_distance_input = gr.Number(
                                label="自営線距離 [km]",
                                value=MG_LINE_DISTANCE_KM, precision=1,
                            )
                            mg_line_cost_input = gr.Number(
                                label="自営線単価 [円/km]",
                                value=MG_LINE_COST_PER_KM, precision=0,
                            )
                        with gr.Row():
                            mg_opex_input = gr.Number(
                                label="年間運営コスト [%]（投資額比）",
                                value=MG_OPEX_RATIO, precision=1,
                            )
                            mg_irr_period_input = gr.Number(
                                label="P-IRR計算期間 [年]",
                                value=MG_IRR_PERIOD, precision=0,
                            )

                    mg_enabled_input.change(
                        fn=lambda x: gr.update(visible=x),
                        inputs=[mg_enabled_input],
                        outputs=[mg_settings_group],
                        api_visibility="hidden",
                    )

                # --- 太陽光発電設定 ---
                # --- 風力発電（オフサイトPPA）。設計は docs/wind_design_spec.md §5-6 ---
                # 入力の並びは WIND_INPUT_KEYS と同じ（on_click が build_wind_args に渡す）。
                # 任意入力は、空欄=既定値のテキスト欄（gr.Number は空欄を表せず未操作でも0を送るため）
                with gr.Accordion("🌀 風力発電（オフサイトPPA）の設定", open=False):
                    # 構成: 冒頭=単価構成の式（何をどう計算するか）／中=式の順に並べた入力欄／末尾=出典・注意書き（W2f-3改訂）
                    gr.Markdown(
                        "風力発電（オフサイトPPA）は送配電網で届く電源です。費用は次の2階建てで計算します:\n\n"
                        "```\n"
                        "風力発電（オフサイトPPA）の費用 = ① 風力発電（オフサイトPPA）の支払 + ② 使用電力量にかかる託送等\n"
                        "  ① = 発電量 ×（PPA発電単価 + 発電側課金 ÷ 発電量 + 発電バランシング単価）\n"
                        "  ② = 使用電力量 ×（託送の電力量料金 + 再エネ賦課金 + 小売グロスマージン）\n"
                        "```\n"
                        "※ 送電ロス（損失率）の分だけ、需要地に届く電力量は発電量より少なくなります。※ 支払の対象を「使用量払い」にすると"
                        "①も使用電力量ベースになります。契約電力（基本料金）は太陽光と違って下がらず、売電できるのは太陽光の余剰だけです。"
                    )
                    wind_enabled_input = gr.Checkbox(label="風力発電（オフサイトPPA）を併用する", value=False)
                    with gr.Group(visible=False) as wind_settings_group:
                        wind_area_md = gr.Markdown(
                            wind_area_note(station_input.value, False, contract_type_input.value))
                        wind_sizing_input = gr.Radio(
                            choices=WIND_SIZING_MODES, value=WIND_SIZING_CAPACITY, label="容量の指定方法")
                        wind_capacity_input = gr.Textbox(
                            label="契約容量 [kW]", placeholder="例: 1000", visible=True)
                        wind_coverage_input = gr.Textbox(
                            label="需要カバー率 [%]（年間発電量 ÷ 年間需要量）", placeholder="例: 100", visible=False)
                        with gr.Row():
                            wind_cf_input = gr.Number(
                                label="設備利用率 [%]（発電量を決める）", value=WIND_CF_DEFAULT_PCT, precision=1)
                            wind_loss_rate_input = gr.Textbox(
                                label="損失率 [%]（送電ロス。空欄=既定）", placeholder="空欄で既定値（上に表示）")
                        with gr.Row():
                            wind_ppa_input = gr.Number(
                                label="① PPA発電単価 [円/kWh]", value=WIND_PPA_PRICE_DEFAULT, precision=2)
                            wind_gen_charge_mode_input = gr.Radio(
                                choices=WIND_GEN_CHARGE_MODES, value=WIND_GEN_CHARGE_AUTO,
                                label="① 発電側課金は？（自動=PPAが既定値なら加算・自分で変えたら含む扱い）")
                            wind_gen_charge_discount_input = gr.Textbox(
                                label="① 系統設備効率化割引 [円/年]（空欄=0・未算入）", placeholder="空欄で0円")
                        with gr.Row():
                            wind_balancing_input = gr.Textbox(
                                label="① 発電バランシング単価 [円/kWh]（空欄=既定）", placeholder="空欄で既定値（上に表示）")
                            wind_payment_basis_input = gr.Radio(
                                choices=WIND_PAYMENT_BASES, value=WIND_PAYMENT_BASIS_GENERATED,
                                label="① 支払の対象（全量払い=発電量ベース／使用量払い=使用電力量ベース）")
                        with gr.Row():
                            wind_wheeling_input = gr.Textbox(
                                label="② 託送の電力量料金 [円/kWh]（空欄=既定）", placeholder="空欄で既定値（上に表示）")
                            wind_fee_input = gr.Textbox(
                                label="② 小売グロスマージン [円/kWh]（空欄=既定・暫定値）", placeholder="空欄で既定値（上に表示）")
                        gr.Markdown(
                            "<small>系統受電上限（データセンター）・最適容量探索とも併用できます。蓄電池「最適充放電（LP）」は"
                            "受電点の基準で最適化し、受電上限は風力発電（オフサイトPPA）も含めた受電量に対する上限です"
                            "（風力発電（オフサイトPPA）は上限を守る助けにならず、太陽光と蓄電池で守ります）。"
                            "各項目の出典・データの限界は docs/wind_design_spec.md（§9）を参照。</small>"
                        )
                wind_components = [
                    wind_enabled_input, wind_sizing_input, wind_capacity_input, wind_coverage_input,
                    wind_cf_input, wind_ppa_input, wind_wheeling_input, wind_fee_input,
                    wind_payment_basis_input, wind_gen_charge_mode_input, wind_gen_charge_discount_input,
                    wind_balancing_input, wind_loss_rate_input,
                ]
                if len(wind_components) != len(WIND_INPUT_KEYS):
                    raise RuntimeError("wind_components と WIND_INPUT_KEYS の要素数が一致しません")

                wind_enabled_input.change(
                    fn=lambda x: gr.update(visible=x),
                    inputs=[wind_enabled_input],
                    outputs=[wind_settings_group],
                    api_visibility="hidden",
                )
                wind_sizing_input.change(
                    fn=lambda m: (gr.update(visible=(m == WIND_SIZING_CAPACITY)),
                                  gr.update(visible=(m == WIND_SIZING_COVERAGE))),
                    inputs=[wind_sizing_input],
                    outputs=[wind_capacity_input, wind_coverage_input],
                    api_visibility="hidden",
                )
                # 調達エリアと既定値の表示は、地点・CSV・契約種別に追従する
                for _trigger in (station_input, csv_input, contract_type_input):
                    _trigger.change(
                        fn=lambda st, f, ct: wind_area_note(st, f is not None, ct),
                        inputs=[station_input, csv_input, contract_type_input],
                        outputs=[wind_area_md],
                        api_visibility="hidden",
                    )

                with gr.Accordion("⚙️ 太陽光発電設定", open=False):
                    pv_enabled_input = gr.Checkbox(
                        label="太陽光発電を使う（OFFなら下の面設定は使わず、風力発電（オフサイトPPA）のみで計算）", value=True)
                    with gr.Row():
                        KHD_input = gr.Number(label="KHD（日射量年変動）", value=DEFAULT_KHD, precision=3)
                        KPD_input = gr.Number(label="KPD（経時変化）", value=DEFAULT_KPD, precision=3)
                    with gr.Row():
                        KPM_input = gr.Number(label="KPM（負荷整合）", value=DEFAULT_KPM, precision=3)
                        KPA_input = gr.Number(label="KPA（回路補正）", value=DEFAULT_KPA, precision=3)
                    with gr.Row():
                        eta_input = gr.Number(label="ηINO（インバータ効率）", value=DEFAULT_ETA_INO, precision=3)
                    with gr.Row():
                        alpha_input = gr.Number(label="αPmax [%/℃]", value=DEFAULT_ALPHA, precision=3)
                        delta_t_input = gr.Number(label="ΔT [℃]", value=DEFAULT_DELTA_T, precision=1)
                    gr.Markdown(
                        "<small>ΔT参考値: 架台設置形=21.5℃ / 屋根置き形=28.1℃ / 屋根一体形=46.3℃</small>"
                    )

                # --- 蓄電池設定 ---
                gr.Markdown("### 🔋 蓄電池設定")
                battery_enabled = gr.Checkbox(label="蓄電池あり", value=False)
                with gr.Column(visible=False) as battery_settings_group:
                    battery_mode_input = gr.Dropdown(
                        label="充放電モード",
                        choices=BATTERY_MODES,
                        value="ルールベース",
                    )
                    with gr.Row(visible=True) as battery_cap_row:
                        battery_capacity_input = gr.Number(
                            label="蓄電池容量 [kWh]",
                            value=BATTERY_DEFAULTS["capacity_kwh"], precision=1,
                        )
                    with gr.Row():
                        battery_efficiency_input = gr.Number(
                            label="充放電効率 [%]",
                            value=BATTERY_DEFAULTS["efficiency_pct"], precision=0,
                        )
                    with gr.Row():
                        battery_max_charge_input = gr.Number(
                            label="最大充電電力 [kW]",
                            value=BATTERY_DEFAULTS["max_charge_kw"], precision=1,
                        )
                        battery_max_discharge_input = gr.Number(
                            label="最大放電電力 [kW]",
                            value=BATTERY_DEFAULTS["max_discharge_kw"], precision=1,
                        )
                    with gr.Row():
                        battery_soc_min_input = gr.Number(
                            label="SOC下限 [%]",
                            value=BATTERY_DEFAULTS["soc_min_pct"], precision=0,
                        )
                        battery_soc_max_input = gr.Number(
                            label="SOC上限 [%]",
                            value=BATTERY_DEFAULTS["soc_max_pct"], precision=0,
                        )

                battery_enabled.change(
                    fn=lambda x: gr.update(visible=x),
                    inputs=[battery_enabled],
                    outputs=[battery_settings_group],
                    api_visibility="hidden",
                )

                def on_battery_mode_change(mode):
                    # 最適容量探索時は容量・効率の行を非表示
                    hide_cap = (mode == "最適容量探索")
                    return gr.update(visible=not hide_cap)

                battery_mode_input.change(
                    fn=on_battery_mode_change,
                    inputs=[battery_mode_input],
                    outputs=[battery_cap_row],
                    api_visibility="hidden",
                )

                # --- 両面パネル設定 ---
                gr.Markdown("### ☀️ 両面パネル設定")
                bifacial_enabled_input = gr.Checkbox(label="両面パネルを使用", value=False)
                with gr.Column(visible=False) as bifacial_settings_group:
                    with gr.Row():
                        bifaciality_input = gr.Number(
                            label="背面効率比（bifaciality）",
                            value=BIFACIAL_DEFAULTS["bifaciality"], precision=2,
                        )
                        gcr_input = gr.Number(
                            label="GCR（地面被覆率）",
                            value=BIFACIAL_DEFAULTS["gcr"], precision=2,
                        )
                    with gr.Row():
                        height_input = gr.Number(
                            label="パネル中心地上高 [m]",
                            value=BIFACIAL_DEFAULTS["height"], precision=1,
                        )
                        pitch_input = gr.Number(
                            label="列間隔（pitch）[m]",
                            value=BIFACIAL_DEFAULTS["pitch"], precision=1,
                        )
                    snow_albedo_input = gr.Checkbox(
                        label="積雪アルベド自動切替（積雪時0.7 / 通常0.2）", value=True,
                    )
                    gr.Markdown(
                        "<small>bifaciality目安: TOPCon 0.70〜0.80 / HJT 0.85〜0.95。"
                        "GCR = パネル高さ ÷ 列間隔（0.3〜0.5が一般的）</small>"
                    )

                bifacial_enabled_input.change(
                    fn=lambda x: gr.update(visible=x),
                    inputs=[bifacial_enabled_input],
                    outputs=[bifacial_settings_group],
                    api_visibility="hidden",
                )

                # --- アレイ設定 ---
                gr.Markdown("### 🔲 太陽電池アレイ設定")
                num_faces_input = gr.Slider(
                    label="面数", minimum=1, maximum=MAX_FACES, step=1, value=1
                )

                gr.Markdown("<small>方位角: 北=0° → 東=90° → 南=180° → 西=270°（時計回り）。直接入力はドロップダウンより優先</small>")

                face_components = []
                face_groups = []
                for i in range(MAX_FACES):
                    visible = (i == 0)
                    with gr.Group(visible=visible) as grp:
                        gr.Markdown(f"**面{i+1}**")
                        with gr.Row():
                            pp = gr.Number(label="Ppeak [kW]", value=5.0 if i == 0 else 0, precision=2)
                            ori = gr.Dropdown(
                                label="方位（選択）",
                                choices=list(ORIENTATION_TO_AZIMUTH.keys()),
                                value="南",
                            )
                            # 空欄=「方位（選択）」を使う。gr.Number(value=None) だと未操作でも 0（北向き）が
                            # 送られてしまうため Textbox にしている（run_simulation が数値に変換・検証する）
                            azi = gr.Textbox(
                                label="方位角 [°]（直接入力優先）", value="", max_lines=1,
                                placeholder="空欄=方位（選択）を使用",
                            )
                            tlt = gr.Number(label="傾斜角 [°]", value=30, precision=1)
                            pcs = gr.Number(label="PCS出力制限 [kW]", value=5.5 if i == 0 else 0, precision=2)
                    face_components.extend([pp, ori, azi, tlt, pcs])
                    face_groups.append(grp)

                def update_face_visibility(n):
                    return [gr.update(visible=(i < n)) for i in range(MAX_FACES)]

                num_faces_input.change(
                    fn=update_face_visibility,
                    inputs=[num_faces_input],
                    outputs=face_groups,
                    api_visibility="hidden",
                )

                run_btn = gr.Button("▶️ 計算", variant="primary", size="lg")

            # ===== 右パネル =====
            with gr.Column(scale=3):
                with gr.Tabs():
                    with gr.Tab("📊 月別発電量"):
                        monthly_plot = gr.Plot(label="月別発電量")
                    with gr.Tab("📈 日別発電量（48コマ）"):
                        with gr.Row():
                            month_input = gr.Number(label="月 (1-12)", value=7, precision=0)
                            day_input = gr.Number(label="日 (1-31)", value=1, precision=0)
                        daily_plot = gr.Plot(label="日別発電量（48コマ）")
                    with gr.Tab("⚡ デマンド追跡"):
                        demand_plot = gr.Plot(label="月別最大デマンド比較")
                    with gr.Tab("🔍 最適容量探索"):
                        capacity_search_plot = gr.Plot(label="蓄電池容量 最適化探索")
                result_box = gr.Textbox(label="計算結果", lines=28, interactive=False)
                debug_box = gr.Textbox(label="デバッグ情報", lines=12, interactive=False)

        # 計算結果をセッション内に保持するState
        result_state = gr.State(value=None)

        # --- 契約種別変更時のデフォルト単価切替コールバック ---
        def on_contract_type_change(ctype):
            rates = ELECTRICITY_RATE_EHV if ctype == "特別高圧" else ELECTRICITY_RATE_HV
            return (
                rates["basic_charge_per_kw"],
                rates["energy_charge_summer"],
                rates["energy_charge_other"],
                rates["power_factor_pct"],
                rates["fuel_adjustment"],
                rates["renewable_surcharge"],
            )

        contract_type_input.change(
            fn=on_contract_type_change,
            inputs=[contract_type_input],
            outputs=[
                elec_basic_input, elec_summer_input, elec_other_input,
                elec_pf_input, elec_fuel_input, elec_renewable_input,
            ],
            api_visibility="hidden",
        )

        # --- 売電モード変更コールバック ---
        def on_sell_mode_change(mode):
            # 余剰売電時のみ売電制度グループを表示
            return gr.update(visible=(mode == "余剰売電"))

        sell_mode_input.change(
            fn=on_sell_mode_change,
            inputs=[sell_mode_input],
            outputs=[sell_scheme_group],
            api_visibility="hidden",
        )

        # --- 売電制度/経過年数変更コールバック ---
        def on_sell_scheme_change(scheme, year):
            if scheme == "FIT利用あり":
                year = year if year is not None else 1  # 空欄入力ガード
                if year <= 5:
                    price = FIT_PRICE_EARLY
                elif year <= 20:
                    price = FIT_PRICE_LATE
                else:
                    price = FIT_PRICE_POST
                return gr.update(value=price, interactive=False), gr.update(visible=True)
            else:
                return gr.update(value=DEFAULT_SELL_PRICE, interactive=True), gr.update(visible=False)

        sell_scheme_input.change(
            fn=on_sell_scheme_change,
            inputs=[sell_scheme_input, fit_year_input],
            outputs=[sell_price_input, fit_year_input],
            api_visibility="hidden",
        )
        fit_year_input.change(
            fn=on_sell_scheme_change,
            inputs=[sell_scheme_input, fit_year_input],
            outputs=[sell_price_input, fit_year_input],
            api_visibility="hidden",
        )

        # --- 計算ボタンのコールバック ---
        # 入力の構成:
        #   base (48): station, csv, demand_csv, KHD..delta_t(7),
        #              bat_enabled(1), bat_mode(1), bat_capacity..bat_soc_max(6),
        #              elec_basic..elec_renewable(6), contract_type(1),
        #              sell_mode(1), sell_price(1),
        #              pv_cost(1), bat_cost(1), substation_cost(1), subsidy_enabled(1), subsidy_pv(1), subsidy_bat(1),
        #              co2_factor(1),
        #              business_model(1), contract_years(1), target_irr(1),
        #              mg_enabled(1), mg_line_distance(1), mg_line_cost(1), mg_opex(1), mg_irr_period(1),
        #              bifacial_enabled(1), bifaciality(1), gcr(1), height(1), pitch(1), snow_albedo(1) = 48
        #   facility_components: MAX_FACILITIES * 3 = 18
        #   face_components: MAX_FACES * 5 = 40
        #   display: num_facilities, month, day, num_faces = 4
        #   demand_source_state (1): 選択中の需要タブ（industrial / datacenter）
        #   dc_components: len(DC_INPUT_KEYS) = 15（データセンタータブの入力＋受電上限。産業用のときは無視される）
        #   pv_enabled_input (1) ＋ wind_components: len(WIND_INPUT_KEYS) = 13（風力。W3・W2f-3）
        all_inputs = [
            station_input, csv_input,
            demand_csv_input,
            KHD_input, KPD_input, KPM_input, KPA_input, eta_input,
            alpha_input, delta_t_input,
            battery_enabled, battery_mode_input,
            battery_capacity_input, battery_efficiency_input,
            battery_max_charge_input, battery_max_discharge_input,
            battery_soc_min_input, battery_soc_max_input,
            elec_basic_input, elec_summer_input, elec_other_input,
            elec_pf_input, elec_fuel_input, elec_renewable_input,
            contract_type_input,
            sell_mode_input, sell_price_input,
            pv_cost_input, bat_cost_input, substation_cost_input,
            subsidy_enabled_input, subsidy_pv_input, subsidy_bat_input,
            co2_factor_input,
            business_model_input, contract_years_input, target_irr_input,
            mg_enabled_input, mg_line_distance_input, mg_line_cost_input,
            mg_opex_input, mg_irr_period_input,
            bifacial_enabled_input, bifaciality_input, gcr_input, height_input, pitch_input,
            snow_albedo_input,
        ] + facility_components + face_components

        def on_click(*args):
            n_base = 48
            n_fac = MAX_FACILITIES * 3   # 18
            n_face = MAX_FACES * 5       # 40

            base = args[:n_base]
            fac_args = args[n_base:n_base + n_fac]
            face_args = args[n_base + n_fac:n_base + n_fac + n_face]

            tail_start = n_base + n_fac + n_face
            num_fac = args[tail_start]
            month_val = args[tail_start + 1]
            day_val = args[tail_start + 2]
            num_f = args[tail_start + 3]
            demand_source = args[tail_start + 4]
            dc_vals = args[tail_start + 5: tail_start + 5 + len(DC_INPUT_KEYS)]
            dc_args = dict(zip(DC_INPUT_KEYS, dc_vals))

            # 末尾: 太陽光を使うか(1) ＋ 風力の入力(WIND_INPUT_KEYS と同じ並び)
            wind_start = tail_start + 5 + len(DC_INPUT_KEYS)
            pv_enabled = args[wind_start]
            try:
                wind_args = build_wind_args(*args[wind_start + 1: wind_start + 1 + len(WIND_INPUT_KEYS)])
            except ValueError as e:
                return None, None, None, None, f"エラー: {e}", "", None

            return run_simulation(
                *base, fac_args, face_args,
                num_facilities=num_fac, num_faces=num_f,
                display_month=month_val, display_day=day_val,
                demand_source=demand_source, dc_args=dc_args,
                pv_enabled=bool(pv_enabled), wind_args=wind_args,
            )

        all_inputs_with_display = all_inputs + [
            num_facilities_input, month_input, day_input, num_faces_input,
            demand_source_state,
        ] + dc_components + [pv_enabled_input] + wind_components

        run_btn.click(
            fn=on_click,
            inputs=all_inputs_with_display,
            outputs=[monthly_plot, daily_plot, demand_plot, capacity_search_plot, result_box, debug_box, result_state],
            api_visibility="hidden",
        )

        # 月日変更時のグラフ再描画コールバック（再計算なし）
        def on_date_change(stored_result, month_val, day_val):
            if stored_result is None:
                fig = go.Figure()
                fig.update_layout(title="先に「計算」を実行してください")
                return fig
            sc_result = stored_result.get("sc_result") if isinstance(stored_result, dict) else None
            demand_30min = stored_result.get("demand_30min") if isinstance(stored_result, dict) else None
            return make_daily_chart(stored_result, int(month_val), int(day_val), sc_result, demand_30min)

        month_input.change(
            fn=on_date_change,
            inputs=[result_state, month_input, day_input],
            outputs=[daily_plot],
            api_visibility="hidden",
        )
        day_input.change(
            fn=on_date_change,
            inputs=[result_state, month_input, day_input],
            outputs=[daily_plot],
            api_visibility="hidden",
        )

        # gr.api() でUI無しのAPI関数を登録。mcp_server=True 時にMCPツールとして公開される。
        import mcp_tools
        gr.api(mcp_tools.list_stations, api_name="list_stations")
        gr.api(mcp_tools.estimate_pv_generation, api_name="estimate_pv_generation")
        gr.api(mcp_tools.list_wind_areas, api_name="list_wind_areas")
        gr.api(mcp_tools.estimate_wind_generation, api_name="estimate_wind_generation")
        gr.api(mcp_tools.validate_industrial_params, api_name="validate_industrial_params")
        gr.api(mcp_tools.simulate_industrial_pv, api_name="simulate_industrial_pv")
        # データセンター（Phase 7 段階4）
        gr.api(mcp_tools.estimate_dc_demand, api_name="estimate_dc_demand")
        gr.api(mcp_tools.validate_dc_params, api_name="validate_dc_params")
        gr.api(mcp_tools.simulate_dc, api_name="simulate_dc")

    return demo


# ============================================================
# エントリポイント
# ============================================================

demo = build_ui()

if __name__ == "__main__":
    demo.launch(mcp_server=True)
