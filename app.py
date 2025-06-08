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

# メイン処理関数（4パターン集計＋グローバルPCS制限）
def process_and_plot(
    station, K, PAS, GS, alpha_pct, delta_T,
    # パターン1〜4のPpeak, 方位, 傾斜角
    Ppeak1, ori1, tilt1,
    Ppeak2, ori2, tilt2,
    Ppeak3, ori3, tilt3,
    Ppeak4, ori4, tilt4,
    month_str, day_str, PCS_capacity_kw
):
    # 入力検証
    if not station:
        return None, None, "エラー：地点を選択してください。"
    month_selected = int(month_str)
    day_selected = int(day_str)
    alpha = alpha_pct / 100.0
    PCS_capacity_kw = PCS_capacity_kw or 0

    # 地点情報取得
    station_no = station.split('_')[0]
    conn = sqlite3.connect(DB_PATH)
    df_info = pd.read_sql_query(
        "SELECT DISTINCT point_lat AS latitude, point_lon AS longitude FROM radiation_data WHERE point_no = ?",
        conn, params=(station_no,)
    )
    conn.close()
    lat = float(df_info.iloc[0]['latitude'])
    lon = float(df_info.iloc[0]['longitude'])

    # データ整形
    df = load_radiation_df(station_no)
    df_solar = df[df['element_no']=='00001'].pivot_table(
        index=['month','day'], columns='hour', values='value'
    ).fillna(0)
    df_temp  = df[df['element_no']=='00005'].pivot_table(
        index=['month','day'], columns='hour', values='value'
    ).fillna(0)
    for h in range(1,25):
        df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce') * 0.01 / 3.6
        df_temp[h]  = pd.to_numeric(df_temp[h], errors='coerce') * 0.1

    # 時系列化
    site  = pvlib.location.Location(lat, lon, tz='Asia/Tokyo')
    times = pd.date_range(start='2020-01-01', periods=len(df_solar)*24, freq='H', tz='Asia/Tokyo')
    ghi_flat   = df_solar[list(range(1,25))].values.flatten() * 1000.0
    ghi_series = pd.Series(ghi_flat, index=times)
    solpos     = site.get_solarposition(times)

    # 各パターンの出力を計算し、リストに格納
    pattern_outs = []
    for Ppeak, ori, tilt in [
        (Ppeak1, ori1, int(tilt1)),
        (Ppeak2, ori2, int(tilt2)),
        (Ppeak3, ori3, int(tilt3)),
        (Ppeak4, ori4, int(tilt4)),
    ]:
        # ErbsモデルでDNI/DHI推定
        sep = pvlib.irradiance.erbs(ghi_series, solpos['zenith'], times)
        dni = sep['dni']; dhi = sep['dhi']
        # 傾斜面POA算出
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=tilt,
            surface_azimuth=ORIENTATION_TO_AZIMUTH.get(ori,180),
            dni=dni, ghi=ghi_series, dhi=dhi,
            solar_zenith=solpos['zenith'], solar_azimuth=solpos['azimuth'], model='isotropic'
        )['poa_global'] / 1000.0
        # DataFrame化
        poa_mat = poa.to_numpy().reshape(len(df_solar), 24)
        df_pv = df_solar.copy()
        df_pv[list(range(1,25))] = pd.DataFrame(poa_mat, index=df_solar.index)
        # 出力計算（個別Ppeak制限はなし）
        df_out = df_pv.copy()
        for h in range(1,25):
            df_out[h] = K * PAS * df_pv[h] * (1 + alpha * (df_temp[h] + delta_T)) / GS
        pattern_outs.append(df_out)

    # 4パターン合計
    df_sum = pattern_outs[0].copy()
    for df_out in pattern_outs[1:]:
        for h in range(1,25):
            df_sum[h] += df_out[h]
    # グローバルPCS制限適用
    for h in range(1,25):
        df_sum[h] = df_sum[h].clip(upper=PCS_capacity_kw)
    # 日次・月次集計
    df_sum['日発電量'] = df_sum[list(range(1,25))].sum(axis=1)
    monthly = df_sum.groupby('month')['日発電量'].sum().reset_index()

    # プロット作成
    fig_bar = px.bar(monthly, x='month', y='日発電量', title='月別発電量（PCS容量制限付き）')
    df_day = df_sum.reset_index()[
        (df_sum.index.get_level_values('month')==month_selected) &
        (df_sum.index.get_level_values('day')==day_selected)
    ]
    if df_day.empty:
        fig_line = px.line(title='該当データなし')
    else:
        hourly = df_day[list(range(1,25))].iloc[0]
        df_plot = pd.DataFrame({'時刻': list(range(1,25)), '発電量': hourly.values})
        fig_line = px.line(df_plot, x='時刻', y='発電量', markers=True,
                            title=f'{month_selected}月{day_selected}日の発電量（制限後）')

    return fig_bar, fig_line

# Gradio UI定義
with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（4パターン合計PCS制限）')
    with gr.Row():
        with gr.Column(scale=2):
            station_input = gr.Dropdown(label='地点選択', choices=get_station_options())
            K_input       = gr.Number(label='K', value=0.95)
            PAS_input     = gr.Number(label='PAS', value=1)
            GS_input      = gr.Number(label='GS', value=1)
            alpha_input   = gr.Number(label='α[%/℃]', value=-0.35)
            deltaT_input  = gr.Number(label='ΔT[℃]', value=25)
            # パターン1～4入力
            for i in range(1,5):
                exec(f"Ppeak{i} = gr.Number(label='Ppeak_{i}[kW]', value=1)")
                exec(f"ori{i} = gr.Dropdown(label='方位_{i}', choices=list(ORIENTATION_TO_AZIMUTH.keys()), value='南')")
                exec(f"tilt{i} = gr.Dropdown(label='傾斜角_{i}(°)', choices=[str(x) for x in range(0,91,10)], value='30')")
            month_input  = gr.Textbox(label='月(1–12)', placeholder='例:4')
            day_input    = gr.Textbox(label='日(1–31)', placeholder='例:3')
            PCS_input    = gr.Number(label='総PCS容量[kW]', value=99)
            run_button   = gr.Button('▶️ 計算')
        with gr.Column(scale=3):
            bar_plot  = gr.Plot(label='月別発電量')
            line_plot = gr.Plot(label='日別発電量')

    run_button.click(
        fn=process_and_plot,
        inputs=[
            station_input, K_input, PAS_input, GS_input, alpha_input, deltaT_input,
            Ppeak1, ori1, tilt1,
            Ppeak2, ori2, tilt2,
            Ppeak3, ori3, tilt3,
            Ppeak4, ori4, tilt4,
            month_input, day_input, PCS_input
        ],
        outputs=[bar_plot, line_plot]
    )

if __name__ == '__main__':
    demo.launch()
