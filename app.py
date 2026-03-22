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
        np.array (365, 48) or None（需要データなし時）
    """
    combined = None

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
            if combined is None:
                combined = demand.copy()
            else:
                combined += demand

    # カスタムCSV（追加合算）
    if custom_csv is not None:
        custom_demand = load_custom_demand_csv(custom_csv)
        if custom_demand is not None:
            if combined is None:
                combined = custom_demand
            else:
                combined += custom_demand

    return combined


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

def compute_poa_30min(ghi_30min, lat, lon, surface_tilt, surface_azimuth):
    """30分GHI配列からpvlibを使ってPOA（傾斜面日射量）を計算する。"""
    if not HAS_PVLIB:
        return ghi_30min.copy()

    n_days = ghi_30min.shape[0]
    total_slots = n_days * 48

    times = pd.date_range(
        start="2023-01-01 00:00", periods=total_slots, freq="30min", tz="Asia/Tokyo"
    )

    ghi_flat = ghi_30min.flatten() * 2.0 * 1000.0
    ghi_series = pd.Series(ghi_flat, index=times)

    site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")
    solpos = site.get_solarposition(times)

    erbs = pvlib.irradiance.erbs(ghi_series, solpos["zenith"], times)
    dni = erbs["dni"].fillna(0).clip(lower=0)
    dhi = erbs["dhi"].fillna(0).clip(lower=0)

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

    poa_kwh = poa_global.values / 1000.0 / 2.0
    poa_30min = poa_kwh.reshape(n_days, 48)

    return poa_30min


# ============================================================
# 発電量計算（JIS C 8907準拠・30分48コマ）
# ============================================================

def calculate_generation(
    lat, lon, ghi_df, temp_df,
    faces, KHD, KPD, KPM, KPA, eta_ino,
    alpha_pct, delta_t
):
    """メイン発電量計算関数。"""
    K_prime = KHD * KPD * KPM * KPA * eta_ino

    ghi_30min, temp_30min, month_day = prepare_30min_data(ghi_df, temp_df)
    n_days = len(month_day)

    face_poa_list = []
    for face in faces:
        azimuth = face.get("azimuth", ORIENTATION_TO_AZIMUTH.get(face["orientation"], 180))
        tilt = face["tilt"]
        poa = compute_poa_30min(ghi_30min, lat, lon, tilt, azimuth)
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
                     no_export=False):
    """蓄電池の充放電シミュレーション（30分×365日）。"""
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
            self_consumption[d, s] = pv_direct + discharge
            export_grid[d, s] = surplus
            import_grid[d, s] = deficit

    annual_self = float(np.sum(self_consumption))
    annual_export = float(np.sum(export_grid))
    annual_import = float(np.sum(import_grid))
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

    return {
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


# ============================================================
# 蓄電池最適充放電（PuLP/CBC線形計画法）
# ============================================================

def optimize_battery(generation_30min, demand_30min, month_day,
                     capacity_kwh, efficiency_pct,
                     max_charge_kw, max_discharge_kw,
                     soc_min_pct, soc_max_pct,
                     basic_charge_per_kw, energy_charge_summer,
                     energy_charge_other, power_factor_pct,
                     fuel_adjustment, renewable_surcharge,
                     sell_price, no_export=False):
    """蓄電池の最適充放電スケジュールをLP（線形計画法）で求める。

    目的関数: 年間電気代（基本料金＋電力量料金−売電収入）の最小化
    ソルバー: CBC（PuLP同梱）
    """
    if not HAS_PULP:
        raise RuntimeError("PuLPがインストールされていません。pip install PuLP を実行してください。")

    n_days, n_slots = generation_30min.shape
    T = n_days * n_slots  # 17,520コマ
    dt = 0.5  # 30分 = 0.5時間

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
    grid_import = [pulp.LpVariable(f"gi_{t}", lowBound=0) for t in range(T)]
    if no_export:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0, upBound=0) for t in range(T)]
    else:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0) for t in range(T)]
    # 出力抑制変数（逆潮流禁止時にPV余剰を捨てる）
    curtailment = [pulp.LpVariable(f"ct_{t}", lowBound=0) for t in range(T)]
    soc_var = [pulp.LpVariable(f"soc_{t}", lowBound=soc_min, upBound=soc_max) for t in range(T)]
    peak_demand = pulp.LpVariable("peak_kw", lowBound=0)  # ピークデマンド（kW）

    # 目的関数: 基本料金 + 電力量料金 - 売電収入
    pf_factor = (185 - power_factor_pct) / 100.0
    annual_basic = basic_charge_per_kw * peak_demand * 12 * pf_factor  # 年間基本料金
    annual_energy = pulp.lpSum([grid_import[t] * unit_price[t] for t in range(T)])
    sell_price_val = sell_price if sell_price is not None else 0
    annual_sell = pulp.lpSum([grid_export[t] * sell_price_val for t in range(T)])
    prob += annual_basic + annual_energy - annual_sell

    # 制約条件
    for t in range(T):
        # エネルギーバランス: 系統購入 + PV発電 + 放電 = 需要 + 充電 + 系統売電 + 出力抑制
        prob += grid_import[t] + gen_flat[t] + discharge[t] == dem_flat[t] + charge[t] + grid_export[t] + curtailment[t]

        # ピークデマンド制約: peak_demand ≥ 系統購入の瞬時電力(kW)
        # grid_import[t]はkWh/30分なので、kWに変換するには÷0.5
        prob += peak_demand >= grid_import[t] / dt

        # SOC遷移
        if t == 0:
            prob += soc_var[t] == soc_min + charge[t] * eff - discharge[t] / eff
        else:
            prob += soc_var[t] == soc_var[t - 1] + charge[t] * eff - discharge[t] / eff

    # 終端SOC制約: 年末SOCを初期SOCに戻す（年次比較の公平性）
    # ※現在の初期SOC = soc_min（固定）。初期SOCを可変にする場合は要見直し
    prob += soc_var[T - 1] == soc_min

    # === ソルバー実行 ===
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=120)
    prob.solve(solver)

    if prob.status != pulp.constants.LpStatusOptimal:
        raise RuntimeError(f"最適化に失敗しました（ステータス: {pulp.LpStatus[prob.status]}）")

    # === 結果取得 ===
    charge_vals = np.array([ch.varValue for ch in charge]).reshape(n_days, n_slots)
    discharge_vals = np.array([dc.varValue for dc in discharge]).reshape(n_days, n_slots)
    gi_vals = np.array([gi.varValue for gi in grid_import]).reshape(n_days, n_slots)
    ge_vals = np.array([ge.varValue for ge in grid_export]).reshape(n_days, n_slots)
    soc_vals = np.array([s.varValue for s in soc_var]).reshape(n_days, n_slots)

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

    return {
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
                              no_export=False, capacity_upper=2000):
    """段階1: LP一体化で蓄電池の最適容量を求める。

    蓄電池容量を決定変数に含め、年間運用コスト＋蓄電池投資年額換算の
    合計を最小化する。P-IRR最大化の近似探索として機能する。
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
    if no_export:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0, upBound=0) for t in range(T)]
    else:
        grid_export = [pulp.LpVariable(f"ge_{t}", lowBound=0) for t in range(T)]
    # 出力抑制変数（逆潮流禁止時にPV余剰を捨てる）
    curtailment = [pulp.LpVariable(f"ct_{t}", lowBound=0) for t in range(T)]
    soc_var = [pulp.LpVariable(f"soc_{t}", lowBound=0) for t in range(T)]
    peak_demand = pulp.LpVariable("peak_kw", lowBound=0)

    # SOC下限 = capacity * soc_min_pct/100 → 非線形なので固定値0とする（容量が変数のため）
    # SOC上限 = capacity * soc_max_pct/100 → capacity_var * soc_max_pct/100 で線形
    soc_max_ratio = soc_max_pct / 100.0
    soc_min_ratio = soc_min_pct / 100.0

    # 目的関数: 年間運用コスト + 蓄電池投資年額換算
    pf_factor = (185 - power_factor_pct) / 100.0
    annual_basic = basic_charge_per_kw * peak_demand * 12 * pf_factor
    annual_energy = pulp.lpSum([grid_import[t] * unit_price[t] for t in range(T)])
    sell_price_val = sell_price if sell_price is not None else 0
    annual_sell = pulp.lpSum([grid_export[t] * sell_price_val for t in range(T)])
    # 蓄電池投資の年額換算（容量が変数）
    annual_battery_cost = capacity_var * battery_cost_per_kwh / payback_years
    prob += annual_basic + annual_energy - annual_sell + annual_battery_cost

    # 制約条件
    for t in range(T):
        # エネルギーバランス: 系統購入 + PV発電 + 放電 = 需要 + 充電 + 系統売電 + 出力抑制
        prob += grid_import[t] + gen_flat[t] + discharge[t] == dem_flat[t] + charge[t] + grid_export[t] + curtailment[t]
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

    return {
        "optimal_capacity_kwh": optimal_capacity,
        "opt_peak_kw": opt_peak_kw,
        "opt_annual_cost": pulp.value(prob.objective),
    }


def grid_search_battery_capacity(generation_30min, demand_30min, month_day,
                                 efficiency_pct, max_charge_kw, max_discharge_kw,
                                 soc_min_pct, soc_max_pct,
                                 sell_price, pv_cost, battery_cost_per_kwh,
                                 total_ppeak, co2_factor,
                                 cost_before_total, payback_years,
                                 no_export=False,
                                 optimal_capacity=None, n_steps=10,
                                 **rate_kwargs):
    """段階2: グリッドサーチで容量ごとの指標を計算。

    段階1の最適容量を基準に探索範囲を決め、各容量でLP最適化を実行。
    rate_kwargs: basic_charge_per_kw, energy_charge_summer, etc.
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
                **rate_kwargs,
            )
            # 導入後の電気料金
            cost_after = calc_electricity_cost(
                sc["import_"], month_day,
                **rate_kwargs,
            )
            # 年間コスト削減
            saving = cost_before_total - cost_after["annual_total"]
            sell_rev = sc["annual_export"] * (sell_price if sell_price is not None else 0)
            annual_merit = saving + sell_rev if not no_export else saving

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

def make_monthly_chart(result, sc_result=None):
    """月別発電量棒グラフ（需要データがあれば並列表示）"""
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
# IRR計算（ニュートン法）
# ============================================================

def _calc_irr(cashflows, tol=1e-8, max_iter=100):
    """キャッシュフロー列からIRRをニュートン法で求める。"""
    # 初期値
    r = 0.10
    for _ in range(max_iter):
        npv = sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows))
        dnpv = sum(-t * cf / (1 + r) ** (t + 1) for t, cf in enumerate(cashflows))
        if abs(dnpv) < 1e-15:
            return None
        r_new = r - npv / dnpv
        if abs(r_new - r) < tol:
            return r_new
        r = r_new
    return r if abs(sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows))) < 1 else None


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
    facility_args, face_args,
    num_facilities=1, num_faces=1,
    display_month=1, display_day=1,
):
    """メイン計算コールバック"""
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

        # --- 需要データ読み込み（複数施設合算） ---
        demand_30min = load_combined_demand(
            facility_args, num_facilities, custom_csv=demand_custom_csv,
        )

        # --- 面設定パース（5項目: Ppeak, 方位選択, 方位角, 傾斜角, PCS出力制限） ---
        faces = []
        for i in range(int(num_faces)):
            idx = i * 5
            ppeak = float(face_args[idx]) if face_args[idx] else 0
            orientation = face_args[idx + 1] if face_args[idx + 1] else "南"
            azi_direct = face_args[idx + 2]
            tilt = float(face_args[idx + 3]) if face_args[idx + 3] else 30
            pcs_kw = float(face_args[idx + 4]) if face_args[idx + 4] else 0

            if azi_direct is not None and azi_direct != "":
                azimuth = float(azi_direct) % 360
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

        if not faces:
            return None, None, None, None, "エラー: 有効な面設定がありません（Ppeak > 0の面が必要です）", "", None

        # --- 計算実行 ---
        result = calculate_generation(
            lat, lon, ghi_df, temp_df,
            faces, KHD, KPD, KPM, KPA, eta_ino,
            alpha_pct, delta_t
        )

        # --- 自家消費計算 / 蓄電池 ---
        no_export = (sell_mode == "逆潮流禁止（売電なし）")
        sc_result = None
        battery_mode_label = bat_mode if bat_mode else "ルールベース"
        if demand_30min is not None:
            if bat_enabled and bat_capacity and bat_capacity > 0 and battery_mode_label != "最適容量探索":
                if battery_mode_label == "最適充放電（LP）":
                    # LP最適化: 電気料金パラメータが必要
                    is_ehv_tmp = (contract_type == "特別高圧")
                    defaults_tmp = ELECTRICITY_RATE_EHV if is_ehv_tmp else ELECTRICITY_RATE_HV
                    sc_result = optimize_battery(
                        result["total_gen_clipped"], demand_30min, result["month_day"],
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
                    )
                else:
                    # ルールベース
                    sc_result = simulate_battery(
                        result["total_gen_clipped"], demand_30min, result["month_day"],
                        capacity_kwh=bat_capacity,
                        efficiency_pct=bat_efficiency if bat_efficiency is not None else 95,
                        max_charge_kw=bat_max_charge if bat_max_charge is not None else 2.5,
                        max_discharge_kw=bat_max_discharge if bat_max_discharge is not None else 2.5,
                        soc_min_pct=bat_soc_min if bat_soc_min is not None else 20,
                        soc_max_pct=bat_soc_max if bat_soc_max is not None else 95,
                        no_export=no_export,
                    )
            else:
                sc_result = calculate_self_consumption(
                    result["total_gen_clipped"], demand_30min, result["month_day"],
                    no_export=no_export,
                )

        # --- グラフ生成 ---
        fig_monthly = make_monthly_chart(result, sc_result)
        fig_daily = make_daily_chart(
            result, int(display_month), int(display_day),
            sc_result, demand_30min,
        )

        # --- 結果テキスト ---
        result_text = f"年間発電量: {result['annual']:.1f} kWh/年\n"
        result_text += f"K' = {result['K_prime']:.4f}\n"
        result_text += "\n面別年間発電量（PCS制限後）:\n"
        for i, val in enumerate(result['face_annual']):
            pcs_str = f"{faces[i]['pcs_limit_kw']} kW" if faces[i].get('pcs_limit_kw') else "制限なし"
            result_text += f"  面{i+1}: {val:.1f} kWh（PCS: {pcs_str}）\n"

        if sc_result is not None:
            result_text += "\n── 需給バランス ──\n"
            result_text += f"年間需要量: {sc_result['annual_demand']:.1f} kWh/年\n"
            result_text += f"自家消費量: {sc_result['annual_self']:.1f} kWh/年\n"
            result_text += f"自家消費率: {sc_result['self_consumption_rate']:.1f}%（自家消費÷発電）\n"
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
                result_text += f"年間充電量: {sc_result['annual_charge']:.1f} kWh/年\n"
                result_text += f"年間放電量: {sc_result['annual_discharge']:.1f} kWh/年\n"
                result_text += f"充放電損失: {sc_result['annual_charge'] - sc_result['annual_discharge']:.1f} kWh/年\n"
                if sc_result.get("optimized"):
                    result_text += f"最適化ピークデマンド: {sc_result['opt_peak_kw']:.1f} kW\n"
                    result_text += f"最適化年間コスト: {sc_result['opt_annual_cost']:,.0f} 円\n"

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
                    result_text += f"  総合経済メリット: {total_merit:,.0f} 円/年\n"
                else:
                    result_text += f"  年間コスト削減: {saving:,.0f} 円/年\n"

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
        if bat_enabled and bat_capacity and bat_capacity > 0:
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

        if annual_merit > 0:
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

        # --- 事業モデル計算（モードA: リース/PPA） ---
        biz_model = business_model if business_model else "自己所有"
        if biz_model in ("リース", "PPA") and net_investment > 0:
            n_years = int(contract_years) if contract_years is not None else DEFAULT_CONTRACT_YEARS
            r = (target_irr if target_irr is not None else DEFAULT_TARGET_IRR) / 100.0
            # 資本回収係数（CRF）で年間リース料を逆算
            if r > 0:
                crf = r * (1 + r) ** n_years / ((1 + r) ** n_years - 1)
            else:
                crf = 1.0 / n_years
            annual_lease = net_investment * crf

            result_text += f"\n══ 事業モデル: {biz_model} ══\n"
            result_text += f"【事業者側パラメータ】\n"
            result_text += f"  実質投資額: {net_investment:,.0f} 円\n"
            result_text += f"  契約年数: {n_years} 年\n"
            result_text += f"  目標P-IRR: {r*100:.1f}%\n"

            if biz_model == "リース":
                monthly_lease = annual_lease / 12
                result_text += f"  必要リース料: {annual_lease:,.0f} 円/年（{monthly_lease:,.0f} 円/月）\n"
                result_text += f"\n【需要家メリット（{n_years}年間）】\n"
                if annual_merit > 0:
                    customer_annual = annual_merit - annual_lease
                    customer_total = customer_annual * n_years
                    result_text += f"  電気代削減: {annual_merit:,.0f} 円/年\n"
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
                if sc_result is not None and sc_result['annual_self'] > 0:
                    ppa_price = annual_lease / sc_result['annual_self']
                    result_text += f"  必要PPA単価: {ppa_price:.2f} 円/kWh\n"
                    result_text += f"  年間自家消費量: {sc_result['annual_self']:,.1f} kWh/年\n"
                    result_text += f"\n【需要家メリット（{n_years}年間）】\n"
                    ppa_annual_cost = ppa_price * sc_result['annual_self']
                    customer_annual = annual_merit - ppa_annual_cost
                    customer_total = customer_annual * n_years
                    result_text += f"  電気代削減: {annual_merit:,.0f} 円/年\n"
                    result_text += f"  PPA支払: {ppa_annual_cost:,.0f} 円/年（{ppa_price:.2f}円/kWh）\n"
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
            mg_dist = mg_line_distance if mg_line_distance is not None else MG_LINE_DISTANCE_KM
            mg_cost_km = mg_line_cost_per_km if mg_line_cost_per_km is not None else MG_LINE_COST_PER_KM
            mg_opex_r = (mg_opex_ratio if mg_opex_ratio is not None else MG_OPEX_RATIO) / 100.0
            mg_period = int(mg_irr_period) if mg_irr_period is not None else MG_IRR_PERIOD

            # MG初期投資 = PV+蓄電池 + 自営線
            mg_line_cost = mg_dist * mg_cost_km
            mg_total_investment = net_investment + mg_line_cost
            mg_annual_opex = mg_total_investment * mg_opex_r

            # MG収益:
            # PV発電→網内売電: 自家消費量 × 電力量単価 = 収益（仕入れゼロ）
            # 系統購入→網内売電: 基本料金差額のみ
            defaults_mg = ELECTRICITY_RATE_EHV if is_ehv else ELECTRICITY_RATE_HV
            avg_energy_price = (
                elec_summer if elec_summer is not None else defaults_mg["energy_charge_summer"]
            ) * 0.25 + (
                elec_other if elec_other is not None else defaults_mg["energy_charge_other"]
            ) * 0.75  # 夏季3ヶ月/12ヶ月の加重平均

            # PV由来の売電収入（自家消費分 × 電力量単価）
            pv_revenue = sc_result['annual_self'] * avg_energy_price
            # 基本料金差額（MG内集約によるスケールメリット）
            if cost_after is not None:
                basic_saving = cost_before['annual_basic'] - cost_after['annual_basic']
            else:
                basic_saving = 0
            mg_annual_revenue = pv_revenue + basic_saving
            mg_annual_cashflow = mg_annual_revenue - mg_annual_opex

            result_text += f"\n══ マイクログリッド事業 ══\n"
            result_text += f"【初期投資】\n"
            result_text += f"  PV+蓄電池: {net_investment:,.0f} 円\n"
            result_text += f"  自営線: {mg_dist:.1f} km × {mg_cost_km:,.0f} 円/km = {mg_line_cost:,.0f} 円\n"
            result_text += f"  MG投資合計: {mg_total_investment:,.0f} 円\n"
            result_text += f"【年間収支】\n"
            result_text += f"  PV売電収入（網内）: {pv_revenue:,.0f} 円/年\n"
            result_text += f"  基本料金差額: {basic_saving:,.0f} 円/年\n"
            result_text += f"  年間収益合計: {mg_annual_revenue:,.0f} 円/年\n"
            result_text += f"  年間運営コスト: {mg_annual_opex:,.0f} 円/年（投資額の{mg_opex_r*100:.1f}%）\n"
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

        # --- 需要構成サマリー ---
        if demand_30min is not None:
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
        debug_text += f"緯度: {lat:.4f}°  経度: {lon:.4f}°\n"
        debug_text += f"pvlib: {'利用' if HAS_PVLIB else '未使用（GHI直接）'}\n"
        debug_text += f"面数: {len(faces)}\n"
        for i, face in enumerate(faces):
            pcs_str = f"{face['pcs_limit_kw']} kW" if face.get('pcs_limit_kw') else "制限なし"
            debug_text += f"  面{i+1}: Ppeak={face['ppeak']}kW, {face['orientation']}({face['azimuth']:.1f}°), 傾斜{face['tilt']}°, PCS={pcs_str}\n"
        debug_text += f"月別発電量:\n"
        for m in range(1, 13):
            debug_text += f"  {m:2d}月: {result['monthly'].get(m, 0):8.1f} kWh\n"
        if sc_result is not None:
            debug_text += f"\n年間需要量(合算): {sc_result['annual_demand']:.1f} kWh\n"

        # --- デマンド追跡グラフ ---
        fig_demand = None
        if cost_before is not None:
            fig_demand = make_demand_chart(cost_before, cost_after, contract_label)

        # --- 最適容量探索 ---
        fig_capacity = None
        if battery_mode_label == "最適容量探索" and demand_30min is not None and cost_before is not None:
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

            # 蓄電池の実質単価（補助金反映）
            bat_subsidy_pct = 0
            if subsidy_enabled:
                bat_subsidy_pct = subsidy_bat_pct if subsidy_bat_pct is not None else 0
            bat_unit_net = bat_unit * (1 - bat_subsidy_pct / 100.0)

            # 段階1: LP一体化
            try:
                cap_result = optimize_battery_capacity(
                    result["total_gen_clipped"], demand_30min, result["month_day"],
                    efficiency_pct=bat_eff,
                    max_charge_kw=bat_mc, max_discharge_kw=bat_md,
                    soc_min_pct=bat_smin, soc_max_pct=bat_smax,
                    sell_price=sp, battery_cost_per_kwh=bat_unit_net, payback_years=n_yrs,
                    no_export=no_export, **rate_p,
                )
                opt_cap = cap_result["optimal_capacity_kwh"]

                result_text += f"\n══ 最適蓄電池容量探索 ══\n"
                result_text += f"【段階1: LP一体化】\n"
                result_text += f"  最適蓄電池容量: {opt_cap:.1f} kWh\n"

                # 最適容量でLP最適化を実行して詳細指標を取得
                sc_opt = optimize_battery(
                    result["total_gen_clipped"], demand_30min, result["month_day"],
                    capacity_kwh=opt_cap,
                    efficiency_pct=bat_eff,
                    max_charge_kw=bat_mc, max_discharge_kw=bat_md,
                    soc_min_pct=bat_smin, soc_max_pct=bat_smax,
                    sell_price=sp, no_export=no_export, **rate_p,
                )
                cost_opt = calc_electricity_cost(
                    sc_opt["import_"], result["month_day"], **rate_p,
                )
                saving_opt = cost_before['annual_total'] - cost_opt['annual_total']
                sell_rev_opt = sc_opt['annual_export'] * sp if not no_export else 0
                merit_opt = saving_opt + sell_rev_opt
                bat_inv_opt = opt_cap * bat_unit
                total_inv_opt = total_ppeak * pv_unit + bat_inv_opt
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
                    result["total_gen_clipped"], demand_30min, result["month_day"],
                    efficiency_pct=bat_eff,
                    max_charge_kw=bat_mc, max_discharge_kw=bat_md,
                    soc_min_pct=bat_smin, soc_max_pct=bat_smax,
                    sell_price=sp,
                    pv_cost=pv_unit * (1 - (subsidy_pv_pct if subsidy_enabled and subsidy_pv_pct else 0) / 100.0),
                    battery_cost_per_kwh=bat_unit_net,
                    total_ppeak=total_ppeak,
                    co2_factor=ef,
                    cost_before_total=cost_before['annual_total'],
                    payback_years=n_yrs,
                    no_export=no_export,
                    optimal_capacity=opt_cap,
                    n_steps=10,
                    **rate_p,
                )
                fig_capacity = make_capacity_search_chart(search_results, opt_cap)
                result_text += f"【段階2: グリッドサーチ】探索完了（{len(search_results)}ステップ）\n"

            except Exception as e:
                result_text += f"\n最適容量探索エラー: {e}\n"

        # result_stateに需要関連も含める
        result["sc_result"] = sc_result
        result["demand_30min"] = demand_30min

        return fig_monthly, fig_daily, fig_demand, fig_capacity, result_text, debug_text, result

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

                # --- 需要設定（複数施設合算） ---
                gr.Markdown("### ⚡ 需要設定（施設を組み合わせて需要カーブを作成）")
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
                    )

                # 施設数変更で表示切替
                def update_facility_visibility(n):
                    return [gr.update(visible=(i < n)) for i in range(MAX_FACILITIES)]

                num_facilities_input.change(
                    fn=update_facility_visibility,
                    inputs=[num_facilities_input],
                    outputs=facility_groups,
                )

                demand_csv_input = gr.File(
                    label="カスタム需要CSV（追加合算、timestamp + demand_kWh）",
                    file_types=[".csv"],
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
                    )

                # --- 太陽光発電設定 ---
                with gr.Accordion("⚙️ 太陽光発電設定", open=False):
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
                )

                def on_battery_mode_change(mode):
                    # 最適容量探索時は容量・効率の行を非表示
                    hide_cap = (mode == "最適容量探索")
                    return gr.update(visible=not hide_cap)

                battery_mode_input.change(
                    fn=on_battery_mode_change,
                    inputs=[battery_mode_input],
                    outputs=[battery_cap_row],
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
                            azi = gr.Number(label="方位角 [°]（直接入力優先）", value=None, precision=1)
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
        )

        # --- 売電モード変更コールバック ---
        def on_sell_mode_change(mode):
            # 余剰売電時のみ売電制度グループを表示
            return gr.update(visible=(mode == "余剰売電"))

        sell_mode_input.change(
            fn=on_sell_mode_change,
            inputs=[sell_mode_input],
            outputs=[sell_scheme_group],
        )

        # --- 売電制度/経過年数変更コールバック ---
        def on_sell_scheme_change(scheme, year):
            if scheme == "FIT利用あり":
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
        )
        fit_year_input.change(
            fn=on_sell_scheme_change,
            inputs=[sell_scheme_input, fit_year_input],
            outputs=[sell_price_input, fit_year_input],
        )

        # --- 計算ボタンのコールバック ---
        # 入力の構成:
        #   base (41): station, csv, demand_csv, KHD..delta_t(7),
        #              bat_enabled(1), bat_mode(1), bat_capacity..bat_soc_max(6),
        #              elec_basic..elec_renewable(6), contract_type(1),
        #              sell_mode(1), sell_price(1),
        #              pv_cost(1), bat_cost(1), subsidy_enabled(1), subsidy_pv(1), subsidy_bat(1),
        #              co2_factor(1),
        #              business_model(1), contract_years(1), target_irr(1),
        #              mg_enabled(1), mg_line_distance(1), mg_line_cost(1), mg_opex(1), mg_irr_period(1) = 41
        #   facility_components: MAX_FACILITIES * 3 = 18
        #   face_components: MAX_FACES * 5 = 40
        #   display: num_facilities, month, day, num_faces = 4
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
        ] + facility_components + face_components

        def on_click(*args):
            n_base = 42
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

            return run_simulation(
                *base, fac_args, face_args,
                num_facilities=num_fac, num_faces=num_f,
                display_month=month_val, display_day=day_val,
            )

        all_inputs_with_display = all_inputs + [
            num_facilities_input, month_input, day_input, num_faces_input,
        ]

        run_btn.click(
            fn=on_click,
            inputs=all_inputs_with_display,
            outputs=[monthly_plot, daily_plot, demand_plot, capacity_search_plot, result_box, debug_box, result_state],
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
        )
        day_input.change(
            fn=on_date_change,
            inputs=[result_state, month_input, day_input],
            outputs=[daily_plot],
        )

    return demo


# ============================================================
# エントリポイント
# ============================================================

if __name__ == "__main__":
    demo = build_ui()
    demo.launch()
