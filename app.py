# NEDO日射量データを使った太陽光発電量シミュレーション
# 補正係数をPCS制限前に適用し、その後PCS制限を各時間ごとに適用する構成

import os
import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0

# データベースファイルパス
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "radiation.db")

# 方位からアジマスへのマッピング
ORIENTATION_TO_AZIMUTH = {
    "北": 0, "東": 90, "西": 270,
    "南東": 135, "南西": 225, "南": 180
}

# 地点選択用ドロップダウンの選択肢取得
def get_station_options():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT DISTINCT point_no AS station_no, point_name AS station_name FROM radiation_data",
        conn
    )
    conn.close()
    return [f"{row['station_no']}_{row['station_name']}" for _, row in df.iterrows()]

# 日射量データ読み込み
def load_radiation_df(station_no):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT element_no, month, day, hour, value FROM radiation_data WHERE point_no = ?",
        conn, params=(station_no,)
    )
    conn.close()
    return df

# メイン処理関数
def process_and_plot(
    station, K, PAS, Ppeak, GS, alpha_pct, delta_T,
    orientation, tilt, month_str, day_str, PCS_output_kw
):
    try:
        if not station:
            return None, None, "", "", "エラー：地点を選択してください。"
        month_selected = int(month_str)
        day_selected = int(day_str)
        alpha = alpha_pct / 100.0

        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, "", "", "エラー：PAS または Ppeak を入力してください。"

        PCS_output_kw = PCS_output_kw or DEFAULT_PCS_OUTPUT

        station_no = station.split('_')[0]
        conn = sqlite3.connect(DB_PATH)
        df_info = pd.read_sql_query(
            "SELECT point_lat AS latitude, point_lon AS longitude FROM radiation_data WHERE point_no = ?",
            conn, params=(station_no,)
        )
        conn.close()
        lat = float(df_info.iloc[0]['latitude'])
        lon = float(df_info.iloc[0]['longitude'])

        df = load_radiation_df(station_no)
        df_solar = df[df['element_no'] == '00001'].pivot_table(
            index=['month', 'day'], columns='hour', values='value'
        ).reset_index()
        df_temp = df[df['element_no'] == '00005'].pivot_table(
            index=['month', 'day'], columns='hour', values='value'
        ).reset_index()
        for h in range(1, 25):
            df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce') * 0.01 / 3.6
            df_temp[h] = pd.to_numeric(df_temp[h], errors='coerce') * 0.1
        df_solar = df_solar.fillna(0)
        df_temp = df_temp.fillna(0)
        df_solar_raw = df_solar.copy()

        surface_tilt = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")
        times = pd.date_range(
            start="2020-01-01", periods=len(df_solar) * 24,
            freq="H", tz="Asia/Tokyo"
        )
        ghi_flat = df_solar_raw[list(range(1, 25))].values.flatten() * 1000.0
        ghi_series = pd.Series(ghi_flat, index=times)
        solpos = site.get_solarposition(times)

                # 傾斜面日射量計算: GHIからDNI/DHIを推定（Erbsモデル）し、POAを算出
        sep = pvlib.irradiance.erbs(
            ghi_series,
            solpos['zenith'],
            times
        )
        dni = sep['dni']
        dhi = sep['dhi']
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=dni,
            ghi=ghi_series,
            dhi=dhi,
            solar_zenith=solpos['zenith'],
            solar_azimuth=solpos['azimuth'],
            model='isotropic'
        )
        # kWhに変換してDataFrameに戻す
        poa_kwh = poa['poa_global'] / 1000.0
        poa_mat = poa_kwh.to_numpy().reshape(len(df_solar), 24)
        df_solar[list(range(1, 25))] = pd.DataFrame(poa_mat, index=df_solar.index)

        # 年間発電量算出 (PCS制限なし)
        df_hourly_nopv = df_solar_raw.copy()
        df_hourly_pvlib = df_solar.copy()
        for h in range(1, 25):
            df_hourly_nopv[h] = (
                K * effective_PAS * df_solar_raw[h] *
                (1 + alpha * (df_temp[h] + delta_T)) / GS
            )
            df_hourly_pvlib[h] = (
                K * effective_PAS * df_solar[h] *
                (1 + alpha * (df_temp[h] + delta_T)) / GS
            )
        df_hourly_nopv['日発電量'] = df_hourly_nopv[list(range(1, 25))].sum(axis=1)
        df_hourly_pvlib['日発電量'] = df_hourly_pvlib[list(range(1, 25))].sum(axis=1)
        X = df_hourly_nopv['日発電量'].sum()
        A_pre = df_hourly_pvlib['日発電量'].sum()
        r = X / A_pre if A_pre > 0 else 1.0

        df_hourly_corrected = df_solar.copy()
        for h in range(1, 25):
            raw_output = (
                K * effective_PAS * df_solar[h] *
                (1 + alpha * (df_temp[h] + delta_T)) / GS
            )
            corrected = raw_output * r
            df_hourly_corrected[h] = corrected.clip(upper=PCS_output_kw)
        df_hourly_corrected['日発電量'] = df_hourly_corrected[list(range(1, 25))].sum(axis=1)
        annual_corrected = df_hourly_corrected['日発電量'].sum()

        eph_monthly = df_hourly_corrected.groupby('month')['日発電量'].sum().reset_index()
        fig_bar = px.bar(eph_monthly, x='month', y='日発電量', title='月別発電量（補正後・PCS制限あり）')

        df_day = df_hourly_corrected[
            (df_hourly_corrected['month'] == month_selected) &
            (df_hourly_corrected['day'] == day_selected)
        ]
        if df_day.empty:
            fig_line = px.line(title='該当データなし')
        else:
            hourly = df_day[list(range(1, 25))].iloc[0]
            df_plot = pd.DataFrame({'時刻': list(range(1, 25)), '発電量': hourly.values})
            fig_line = px.line(
                df_plot, x='時刻', y='発電量', markers=True,
                title=f'{month_selected}月{day_selected}日の24h発電量（補正後）'
            ).update_layout(xaxis=dict(dtick=1))

        annual_str = f"{annual_corrected:.2f} kWh"
        debug_text = "\n".join([
            f"年間発電量(GHI, PCSなし): {X:.2f} kWh",
            f"年間発電量(POA, PCSなし): {A_pre:.2f} kWh",
            f"補正係数: {r:.4f}",
            f"年間発電量(補正後＋PCS制限): {annual_corrected:.2f} kWh"
        ])
        return fig_bar, fig_line, annual_str, debug_text, ""

    except Exception as e:
        return None, None, "", "", f"内部エラー: {e}"

with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（PCS補正付き）')
    with gr.Row():
        with gr.Column(scale=2):
            station_input = gr.Dropdown(label='地点選択', choices=get_station_options())
            K_input = gr.Number(label='K', value=0.95)
            PAS_input = gr.Number(label='PAS', value=None)
            Ppeak_input = gr.Number(label='Ppeak', value=10)
            GS_input = gr.Number(label='GS', value=1.0)
            alpha_input = gr.Number(label='α[%/℃]', value=-0.35)
            deltaT_input = gr.Number(label='ΔT[℃]', value=25.0)
            orientation_input = gr.Dropdown(label='方位', choices=list(ORIENTATION_TO_AZIMUTH.keys()), value='南')
            tilt_input = gr.Dropdown(label='傾斜角(°)', choices=[str(i) for i in range(0, 91, 10)], value='30')
            month_input = gr.Textbox(label='月(1–12)', placeholder='例:1')
            day_input = gr.Textbox(label='日(1–31)', placeholder='例:1')
            PCS_input = gr.Number(label='PCS出力[kW]', value=1)
            run_button = gr.Button('▶️ 計算')

            annual_box = gr.Textbox(label='年間発電量（補正後）', interactive=False)
            debug_box = gr.Textbox(label='デバッグ情報', interactive=False)
            error_box = gr.Textbox(label='エラー', interactive=False)
        with gr.Column(scale=3):
            bar_plot = gr.Plot(label='月別発電量')
            line_plot = gr.Plot(label='24h発電量')

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
