import os
import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib  # 必要ライブラリ
import numpy as np

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0

# DBパス設定（スクリプトと同じディレクトリ内のファイルを参照）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "radiation.db")

ORIENTATION_TO_AZIMUTH = {
    "北":   0, "東":  90, "西": 270,
    "南東":135, "南西":225, "南":180
}

# 地点リスト取得
def get_station_options():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"データベースファイルが見つかりません: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT DISTINCT point_no AS station_no, point_name AS station_name FROM radiation_data",
            conn
        )
    finally:
        conn.close()
    options = [f"{row['station_no']}_{row['station_name']}" for _, row in df.iterrows()]
    return options

# 放射量データ読み込み
def load_radiation_df(station_no):
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT element_no, month, day, hour, value FROM radiation_data WHERE point_no = ?",
            conn, params=(station_no,)
        )
    finally:
        conn.close()
    return df

# メイン処理
def process_and_plot(
    station,
    K, PAS, Ppeak, GS,
    alpha_percentage, delta_T,
    orientation, tilt,
    month_str, day_str,
    PCS_output_kw
):
    try:
        # 入力チェック
        if not station:
            return None, None, None, "", "エラー：地点を選択してください。"
        try:
            month_selected = int(month_str)
            day_selected   = int(day_str)
            if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
                raise ValueError
        except:
            return None, None, None, "", "エラー：月は1～12、日は1～31の整数で入力してください。"

        alpha = alpha_percentage / 100.0

        # 有効PASの算出
        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, None, "", "エラー：PAS または Ppeak を入力してください。"

        # PCSデフォルト
        if PCS_output_kw in (None, 0):
            PCS_output_kw = DEFAULT_PCS_OUTPUT

        station_no = station.split('_')[0]
        conn = sqlite3.connect(DB_PATH)
        try:
            df_info = pd.read_sql_query(
                "SELECT point_lat AS latitude, point_lon AS longitude FROM radiation_data WHERE point_no = ?",
                conn, params=(station_no,)
            )
        finally:
            conn.close()
        if df_info.empty:
            return None, None, None, "", f"エラー：地点情報が見つかりません ({station_no})。"
        lat = float(df_info.iloc[0]['latitude'])
        lon = float(df_info.iloc[0]['longitude'])

        df = load_radiation_df(station_no)

        # 時系列整形
        df_solar = df[df['element_no']=='00001'].pivot_table(
            index=['month','day'], columns='hour', values='value'
        ).reset_index()
        df_temp  = df[df['element_no']=='00005'].pivot_table(
            index=['month','day'], columns='hour', values='value'
        ).reset_index()
        for h in range(1,25):
            df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce') * 0.01 * 277.778
            df_temp[h]  = pd.to_numeric(df_temp[h],  errors='coerce') * 0.1
        df_solar = df_solar.fillna(0)
        df_temp  = df_temp.fillna(0)

        # NEDO GHI合計
        raw_ghi_flat = df_solar[list(range(1,25))].values.flatten()
        raw_ghi_sum  = raw_ghi_flat.sum()

        # pvlib POA計算
        surface_tilt    = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")

        times = []
        for _, row in df_solar.iterrows():
            for h in range(1,25):
                times.append(pd.Timestamp(
                    year=2020,
                    month=int(row['month']),
                    day=int(row['day']),
                    hour=h-1,
                    tz="Asia/Tokyo"
                ))
        times = pd.DatetimeIndex(times)

        solpos   = site.get_solarposition(times)
        clearsky = site.get_clearsky(times, model="simplified_solis")
        dni      = np.asarray(clearsky["dni"])
        dhi      = np.asarray(clearsky["dhi"])
        solar_zenith  = np.asarray(solpos["zenith"])
        solar_azimuth = np.asarray(solpos["azimuth"])
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=dni, ghi=raw_ghi_flat, dhi=dhi,
            solar_zenith=solar_zenith, solar_azimuth=solar_azimuth,
            model='isotropic'
        )
        poa_vals = np.asarray(poa["poa_global"])
        poa_sum  = poa_vals.sum()

        # 温度補正係数計算
        correction_factors = 1 + alpha * (df_temp[list(range(1,25))] + delta_T)
        correction_avg     = correction_factors.values.mean()
        correction_max     = correction_factors.values.max()

        # 月日別発電量（POAベース）
        df_hourly = pd.DataFrame(poa_vals.reshape(len(df_solar),24), columns=list(range(1,25)))
        for h in range(1,25):
            df_hourly[h] = (K * effective_PAS * df_hourly[h] * correction_factors[h] / GS)
            df_hourly[h] = df_hourly[h].clip(upper=PCS_output_kw)
        df_hourly['日発電量'] = df_hourly[list(range(1,25))].sum(axis=1)

        # 年間簡易推定発電量（kWh）
        raw_energy_simple = (K * effective_PAS * raw_ghi_flat * (1 + alpha * (df_temp[list(range(1,25))].values.flatten() + delta_T)) / GS).sum() / 1000.0
        clipped_energy    = raw_energy_simple

        # 月別集計とグラフ
        eph_monthly = df_hourly.groupby('month')['日発電量'].sum().reset_index()
        fig_bar = px.bar(eph_monthly, x='month', y='日発電量', title='月別発電量（補正済み）')

        # 日別計算とグラフ
        df_day = df_hourly[(df_hourly['month']==month_selected)&(df_hourly['day']==day_selected)]
        if df_day.empty:
            fig_line = px.line(title='該当データなし')
        else:
            df_plot = pd.DataFrame({'時刻':list(range(1,25)),'発電量':df_day[list(range(1,25))].iloc[0].values})
            fig_line = px.line(df_plot, x='時刻', y='発電量', markers=True,
                               title=f'{month_selected}月{day_selected}日の24h発電量').update_layout(xaxis=dict(dtick=1))

        annual_str = f"年間発電量: {clipped_energy:.2f} kWh"
        debug_info = (
            f"raw_ghi_sum={raw_ghi_sum:.2f}, poa_sum={poa_sum:.2f}, "
            f"correction_avg={correction_avg:.3f}, correction_max={correction_max:.3f}, "
            f"simple_no_pvlib_energy={raw_energy_simple:.2f}"
        )

        return fig_bar, fig_line, annual_str, debug_info, ""

    except Exception as e:
        return None, None, None, "", f'内部エラー: {e}'

with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（SQL版）')
    with gr.Row():
        with gr.Column(scale=2):
            station_input    = gr.Dropdown(label='地点選択', choices=get_station_options())
            K_input          = gr.Number(label='K', value=0.95)
            PAS_input        = gr.Number(label='PAS', value=None)
            Ppeak_input      = gr.Number(label='Ppeak', value=None)
            GS_input         = gr.Number(label='GS', value=1.0)
            alpha_input      = gr.Number(label='α[%/℃]', value=-0.35)
            deltaT_input     = gr.Number(label='ΔT[℃]', value=25.0)
            orientation_input= gr.Dropdown(label='方位', choices=list(ORIENTATION_TO_AZIMUTH.keys()), value='南')
