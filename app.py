import os
import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib  # 必要ライブラリ

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "radiation.db")

ORIENTATION_TO_AZIMUTH = {
    "北":   0, "東":  90, "西": 270,
    "南東":135, "南西":225, "南":180
}

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

def process_and_plot(
    station,
    K, PAS, Ppeak, GS,
    alpha_percentage, delta_T,
    orientation, tilt,
    month_str, day_str,
    PCS_output_kw
):
    try:
        # --- 入力チェック ---
        if not station:
            return None, None, "", "", "エラー：地点を選択してください。"
        try:
            month_selected = int(month_str)
            day_selected   = int(day_str)
            if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
                raise ValueError
        except:
            return None, None, "", "", "エラー：月は1～12、日は1～31の整数で入力してください。"

        alpha = alpha_percentage / 100.0

        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, "", "", "エラー：PAS または Ppeak を入力してください。"

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
            return None, None, "", "", f"エラー：地点情報が見つかりません ({station_no})。"
        lat = float(df_info.iloc[0]['latitude'])
        lon = float(df_info.iloc[0]['longitude'])

        df = load_radiation_df(station_no)

        df_solar = df[df['element_no']=='00001'].pivot_table(
            index=['month','day'], columns='hour', values='value'
        ).reset_index()
        df_temp  = df[df['element_no']=='00005'].pivot_table(
            index=['month','day'], columns='hour', values='value'
        ).reset_index()
        for h in range(1,25):
            df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce') * 0.01 / 3.6
            df_temp[h]  = pd.to_numeric(df_temp[h],  errors='coerce') * 0.1
        df_solar = df_solar.fillna(0)
        df_temp  = df_temp.fillna(0)

        # デバッグ用：水平GHI（POA補正なし）
        df_solar_raw = df_solar.copy()

        # --- PVLIB傾斜面日射量 ---
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
        ghi_flat = df_solar_raw[list(range(1,25))].values.flatten() * 1000.0

        solpos   = site.get_solarposition(times)
        clearsky = site.get_clearsky(times, model="simplified_solis")
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=clearsky['dni'].values,
            ghi=ghi_flat,
            dhi=clearsky['dhi'].values,
            solar_zenith=solpos['zenith'].values,
            solar_azimuth=solpos['azimuth'].values,
            model='isotropic'
        )
        poa_kwh = poa['poa_global'] / 1000.0
        poa_mat = poa_kwh.reshape(len(df_solar), 24)
        df_solar[list(range(1,25))] = pd.DataFrame(poa_mat, index=df_solar.index)

        # --- 1. PVLIBなし・PCS制限なし（毎時積算）---
        df_hourly_nopv_nopcs = df_solar_raw.copy()
        for h in range(1,25):
            df_hourly_nopv_nopcs[h] = (
                K * effective_PAS * df_solar_raw[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly_nopv_nopcs['日発電量'] = df_hourly_nopv_nopcs[list(range(1,25))].sum(axis=1)
        annual_no_pvlib_nopcs = df_hourly_nopv_nopcs['日発電量'].sum()

        # --- 2. PVLIBあり・PCS制限なし（毎時積算）---
        df_hourly_pvlib_nopcs = df_solar.copy()
        for h in range(1,25):
            df_hourly_pvlib_nopcs[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly_pvlib_nopcs['日発電量'] = df_hourly_pvlib_nopcs[list(range(1,25))].sum(axis=1)
        annual_pvlib_nopcs = df_hourly_pvlib_nopcs['日発電量'].sum()

        # --- 3. PVLIBあり・PCS制限あり（毎時積算）---
        df_hourly_pvlib_pcs = df_solar.copy()
        for h in range(1,25):
            df_hourly_pvlib_pcs[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            ).clip(upper=PCS_output_kw)
        df_hourly_pvlib_pcs['日発電量'] = df_hourly_pvlib_pcs[list(range(1,25))].sum(axis=1)
        annual_pvlib_pcs = df_hourly_pvlib_pcs['日発電量'].sum()

        # --- 補正係数 ---
        if annual_pvlib_nopcs > 0:
            scale_ratio = annual_no_pvlib_nopcs / annual_pvlib_nopcs
        else:
            scale_ratio = 1.0  # 回避策

        # --- 棒グラフ（補正後） ---
        eph_monthly = df_hourly_pvlib_pcs.groupby('month')['日発電量'].sum().reset_index()
        eph_monthly['補正後発電量'] = eph_monthly['日発電量'] * scale_ratio
        fig_bar  = px.bar(eph_monthly, x='month', y='補正後発電量', title='月別発電量（補正後：PCS制限あり）')

        # 日別24h発電量（補正後）
        df_day = df_hourly_pvlib_pcs[(df_hourly_pvlib_pcs['month']==month_selected)&(df_hourly_pvlib_pcs['day']==day_selected)]
        if df_day.empty:
            fig_line = px.line(title='該当データなし')
        else:
            hourly  = df_day[list(range(1,25))].iloc[0] * scale_ratio
            df_plot = pd.DataFrame({'時刻': list(range(1,25)), '発電量': hourly.values})
            fig_line = px.line(
                df_plot, x='時刻', y='発電量', markers=True,
                title=f'{month_selected}月{day_selected}日の24h発電量（補正後）'
            ).update_layout(xaxis=dict(dtick=1))

        # 年間発電量（補正後）
        annual_total_corrected = annual_pvlib_pcs * scale_ratio
        annual_str   = f"{annual_total_corrected:.2f} kWh"

        # --- デバッグ欄 ---
        debug_lines = [
            f"年間発電量(PVLIBなし・PCS制限なし・毎時積算): {annual_no_pvlib_nopcs:.2f} kWh",
            f"年間発電量(PVLIBあり・PCS制限なし・毎時積算): {annual_pvlib_nopcs:.2f} kWh",
            f"年間発電量(PVLIBあり・PCS制限あり・毎時積算): {annual_pvlib_pcs:.2f} kWh",
            f"補正係数: {scale_ratio:.4f}",
            f"年間発電量(補正後): {annual_total_corrected:.2f} kWh"
        ]
        debug_text = "\n".join(debug_lines)

        return fig_bar, fig_line, annual_str, debug_text, ""

    except Exception as e:
        return None, None, "", "", f'内部エラー: {e}'

with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（PCS補正付き）')
    with gr.Row():
        with gr.Column(scale=2):
            station_input     = gr.Dropdown(label='地点選択', choices=get_station_options())
            K_input           = gr.Number(label='K', value=0.95)
            PAS_input         = gr.Number(label='PAS', value=None)
            Ppeak_input       = gr.Number(label='Ppeak', value=None)
            GS_input          = gr.Number(label='GS', value=1.0)
            alpha_input       = gr.Number(label='α[%/℃]', value=-0.35)
            deltaT_input      = gr.Number(label='ΔT[℃]', value=25.0)
            orientation_input = gr.Dropdown(label='方位', choices=list(ORIENTATION_TO_AZIMUTH.keys()), value='南')
            tilt_input        = gr.Dropdown(label='傾斜角(°)', choices=[str(i) for i in range(0,91,10)], value='30')
            month_input       = gr.Textbox(label='月(1–12)', placeholder='例:6')
            day_input         = gr.Textbox(label='日(1–31)', placeholder='例:15')
            PCS_input         = gr.Number(label='PCS出力[kW]', value=99)
            run_button        = gr.Button('▶️ 計算')

            annual_box        = gr.Textbox(label='年間発電量（補正後）', interactive=False)
            debug_box         = gr.Textbox(label='デバッグ情報', interactive=False)
            error_box         = gr.Textbox(label='エラー', interactive=False)

        with gr.Column(scale=3):
            bar_plot = gr.Plot(label='月別発電量')
            line_plot= gr.Plot(label='24h発電量')

    run_button.click(
        fn=process_and_plot,
        inputs=[
            station_input, K_input, PAS_input, Ppeak_input, GS_input,
            alpha_input, deltaT_input, orientation_input, tilt_input,
            month_input, day_input, PCS_input
        ],
        outputs=[bar_plot, line_plot, annual_box, debug_box, error_box]
    )

if __name__ == '__main__':
    demo.launch()
