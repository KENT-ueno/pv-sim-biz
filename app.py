import os
import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib  # 必要ライブラリ

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

        # 有効PAS
        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, "", "", "エラー：PAS または Ppeak を入力してください。"

        # PCSデフォルト
        if PCS_output_kw in (None, 0):
            PCS_output_kw = DEFAULT_PCS_OUTPUT

        station_no = station.split('_')[0]

        # 緯度経度取得
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

        # データ取得
        df = load_radiation_df(station_no)

        # --- 時系列整形 ---
        df_solar = df[df['element_no']=='00001'].pivot_table(
            index=['month','day'], columns='hour', values='value'
        ).reset_index()
        df_temp  = df[df['element_no']=='00005'].pivot_table(
            index=['month','day'], columns='hour', values='value'
        ).reset_index()
        for h in range(1,25):
            # 全天日射量：0.01→kJ→kWh、気温：0.1→℃
            df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce') * 0.01 / 3.6
            df_temp[h]  = pd.to_numeric(df_temp[h],  errors='coerce') * 0.1
        df_solar = df_solar.fillna(0)
        df_temp  = df_temp.fillna(0)

        # デバッグ用にPVLIB前の水平全天日射量データを保存
        df_solar_raw = df_solar.copy()

        # --- PVLIBによる傾斜面日射量算出 ---
        surface_tilt    = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")

        # ダミー年=2020 で時刻インデックス作成
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

        # 水平GHIをW/m²へ
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

        # --- 発電量計算（PVLIBあり） & PCS制限 ---
        df_hourly = df_solar.copy()
        for h in range(1,25):
            df_hourly[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            ).clip(upper=PCS_output_kw)
        df_hourly['日発電量(制限後)'] = df_hourly[list(range(1,25))].sum(axis=1)

        # PCS制限前の年間発電量
        df_hourly_np = df_solar_raw.copy()
        for h in range(1,25):
            df_hourly_np[h] = (
                K * effective_PAS * df_solar_raw[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly_np['日発電量(制限前)'] = df_hourly_np[list(range(1,25))].sum(axis=1)

        # 年間合計
        annual_with_pvlib   = df_hourly['日発電量(制限後)'].sum()
        annual_no_pvlib     = df_hourly_np['日発電量(制限前)'].sum()

        # --- 月別集計 & グラフ描画 ---
        eph_monthly = df_hourly.groupby('month')['日発電量(制限後)'].sum().reset_index()
        fig_bar  = px.bar(eph_monthly, x='month', y='日発電量(制限後)', title='月別発電量（PVLIBあり・PCS制限後）')

        # 日別24h発電量
        df_day = df_hourly[(df_hourly['month']==month_selected)&(df_hourly['day']==day_selected)]
        if df_day.empty:
            fig_line = px.line(title='該当データなし')
        else:
            hourly  = df_day[list(range(1,25))].iloc[0]
            df_plot = pd.DataFrame({'時刻': list(range(1,25)), '発電量': hourly.values})
            fig_line = px.line(
                df_plot, x='時刻', y='発電量', markers=True,
                title=f'{month_selected}月{day_selected}日の24h発電量'
            ).update_layout(xaxis=dict(dtick=1))

        # --- デバッグ情報作成 ---
        # 選択日の全天日射量（PVLIB前）
        df_sel = df_solar_raw[
            (df_solar_raw['month']==month_selected)&
            (df_solar_raw['day']==day_selected)
        ]
        if not df_sel.empty:
            ghi_list = df_sel[list(range(1,25))].iloc[0].tolist()
            ghi_text = ",".join(f"{v:.3f}" for v in ghi_list)
            debug_ghi = f"選択日の全天日射量(1-24h): {ghi_text}"
        else:
            debug_ghi = "選択日の全天日射量データなし"

        debug_annual_pvlib = f"年間発電量(PVLIBあり・PCS制限後): {annual_with_pvlib:.2f} kWh"
        debug_annual_nopv = f"年間発電量(PVLIBなし・PCS制限前): {annual_no_pvlib:.2f} kWh"

        debug_text = "\n".join([debug_ghi, debug_annual_nopv, debug_annual_pvlib])

        # 年間発電量表示（制限後）
        annual_str   = f"{annual_with_pvlib:.2f} kWh"

        return fig_bar, fig_line, annual_str, debug_text, ""

    except Exception as e:
        return None, None, "", "", f'内部エラー: {e}'

# ───────────────── Gradio UI ─────────────────
with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（SQL版・デバッグ付き）')
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

            annual_box        = gr.Textbox(label='年間発電量(PVLIBあり・PCS制限後)', interactive=False)
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
