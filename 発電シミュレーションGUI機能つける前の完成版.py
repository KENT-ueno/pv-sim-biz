import os
import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib

# 定数
G_STC = 1.0

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

# メイン処理関数（4パターン合計PCS制限）
def process_and_plot(
    station, K, GS, alpha_pct, delta_T,
    Ppeak1, ori1, tilt1,
    Ppeak2, ori2, tilt2,
    Ppeak3, ori3, tilt3,
    Ppeak4, ori4, tilt4,
    month_str, day_str, PCS_capacity_kw
):
    if not station:
        return None, None, "エラー：地点を選択してください。", ""
    month_selected = int(month_str)
    day_selected = int(day_str)
    alpha = alpha_pct / 100.0
    PCS_capacity = PCS_capacity_kw or 0

    station_no = station.split('_')[0]
    conn = sqlite3.connect(DB_PATH)
    df_info = pd.read_sql_query(
        "SELECT DISTINCT point_lat AS latitude, point_lon AS longitude FROM radiation_data WHERE point_no = ?",
        conn, params=(station_no,)
    )
    conn.close()
    lat, lon = float(df_info.iloc[0]['latitude']), float(df_info.iloc[0]['longitude'])

    df = load_radiation_df(station_no)
    df_solar = df[df['element_no']=='00001'].pivot_table(index=['month','day'], columns='hour', values='value').fillna(0)
    df_temp  = df[df['element_no']=='00005'].pivot_table(index=['month','day'], columns='hour', values='value').fillna(0)
    for h in range(1,25):
        df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce') * 0.01 / 3.6
        df_temp[h]  = pd.to_numeric(df_temp[h], errors='coerce') * 0.1

    site = pvlib.location.Location(lat, lon, tz='Asia/Tokyo')
    times = pd.date_range(start='2020-01-01', periods=len(df_solar)*24, freq='H', tz='Asia/Tokyo')
    ghi = pd.Series(df_solar[list(range(1,25))].values.flatten() * 1000.0, index=times)
    solpos = site.get_solarposition(times)

    pattern_outs = []
    for Ppeak, ori, tilt in [(Ppeak1,ori1,int(tilt1)),(Ppeak2,ori2,int(tilt2)),(Ppeak3,ori3,int(tilt3)),(Ppeak4,ori4,int(tilt4))]:
        sep = pvlib.irradiance.erbs(ghi, solpos['zenith'], times)
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=tilt,
            surface_azimuth=ORIENTATION_TO_AZIMUTH.get(ori,180),
            dni=sep['dni'], ghi=ghi, dhi=sep['dhi'],
            solar_zenith=solpos['zenith'], solar_azimuth=solpos['azimuth'], model='isotropic'
        )['poa_global'] / 1000.0
        mat = poa.to_numpy().reshape(len(df_solar),24)
        df_pv = df_solar.copy(); df_pv[list(range(1,25))] = pd.DataFrame(mat, index=df_solar.index)
        df_out = df_pv.copy()
        for h in range(1,25):
            df_out[h] = K * Ppeak * df_pv[h] * (1 + alpha * (df_temp[h]+delta_T)) / GS
        df_out['日発電量'] = df_out[list(range(1,25))].sum(axis=1)
        pattern_outs.append(df_out)

    annual_pre = [df['日発電量'].sum() for df in pattern_outs]
    df_sum = pattern_outs[0].copy()
    for df in pattern_outs[1:]:
        for h in range(1,25): df_sum[h] += df[h]
    for h in range(1,25): df_sum[h] = df_sum[h].clip(upper=PCS_capacity)
    df_sum['日発電量'] = df_sum[list(range(1,25))].sum(axis=1)
    annual_post = df_sum['日発電量'].sum()

    monthly = df_sum.groupby('month')['日発電量'].sum().reset_index()
    fig_bar = px.bar(monthly, x='month', y='日発電量', title='月別発電量（PCS容量制限付き）')
    df_day = df_sum.reset_index()[
        (df_sum.index.get_level_values('month')==month_selected)&
        (df_sum.index.get_level_values('day')==day_selected)
    ]
    if df_day.empty:
        fig_line = px.line(title='該当データなし')
    else:
        series = df_day[list(range(1,25))].iloc[0]
        fig_line = px.line(pd.DataFrame({'時刻':range(1,25),'発電量':series.values}), x='時刻', y='発電量', markers=True,
                           title=f'{month_selected}月{day_selected}日の発電量（制限後）')

    debug_lines = [f"年間合計発電量(PCS後): {annual_post:.2f} kWh"]
    for i,v in enumerate(annual_pre, start=1): debug_lines.append(f"パターン{i}年間合計(PCS前): {v:.2f} kWh")
    debug_text = "\n".join(debug_lines)

    return fig_bar, fig_line, debug_text

with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（4パターン＋PCS制限）')
    with gr.Row():
        with gr.Column(scale=2):
            station_input = gr.Dropdown(label='地点選択', choices=get_station_options())
            K_input       = gr.Number(label='K', value=0.95)
            GS_input      = gr.Number(label='GS', value=1)
            alpha_input   = gr.Number(label='α[%/℃]', value=-0.35)
            deltaT_input  = gr.Number(label='ΔT[℃]', value=25)

            with gr.Row():
                for i in range(1,5):
                    with gr.Column():
                        vars()[f'Ppeak{i}'] = gr.Number(label=f'Ppeak_{i}[kW]', value=1)
                        vars()[f'ori{i}']   = gr.Dropdown(label=f'方位_{i}', choices=list(ORIENTATION_TO_AZIMUTH.keys()), value='南')
                        vars()[f'tilt{i}']  = gr.Dropdown(label=f'傾斜角_{i}(°)', choices=[str(x) for x in range(0,91,10)], value='30')

            month_input  = gr.Textbox(label='月(1–12)', placeholder='例:4')
            day_input    = gr.Textbox(label='日(1–31)', placeholder='例:3')
            PCS_input    = gr.Number(label='総PCS容量[kW]', value=99)
            run_button   = gr.Button('▶️ 計算')
            debug_box    = gr.Textbox(label='デバッグ情報', interactive=False)
        with gr.Column(scale=3):
            bar_plot  = gr.Plot(label='月別発電量')
            line_plot = gr.Plot(label='日別発電量')

    run_button.click(
        fn=process_and_plot,
        inputs=[
            station_input, K_input, GS_input, alpha_input, deltaT_input,
            Ppeak1, ori1, tilt1, Ppeak2, ori2, tilt2,
            Ppeak3, ori3, tilt3, Ppeak4, ori4, tilt4,
            month_input, day_input, PCS_input
        ],
        outputs=[bar_plot, line_plot, debug_box]
    )

if __name__ == '__main__':
    demo.launch()