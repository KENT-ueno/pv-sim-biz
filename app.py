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
        "SELECT element_no, month, day, hour, value, point_lat AS latitude, point_lon AS longitude "
        "FROM radiation_data WHERE point_no = ?", conn, params=(station_no,)
    )
    conn.close()
    return df

# メイン処理関数（4パターン対応）
def process_and_plot(
    station, K, PAS, GS, alpha_pct, delta_T,
    # パターン1〜4のPpeak, 方位, 傾斜角
    Ppeak1, ori1, tilt1,
    Ppeak2, ori2, tilt2,
    Ppeak3, ori3, tilt3,
    Ppeak4, ori4, tilt4,
    month_str, day_str
):
    # --- 前処理は従来通り ---
    month_selected = int(month_str)
    day_selected = int(day_str)
    alpha = alpha_pct / 100.0

    # 有効PAS算出
    if Ppeak1 or Ppeak2 or Ppeak3 or Ppeak4:
        effective_PAS = PAS  # 今回はPAS固定
    else:
        return None, None, "", "Ppeakを1つ以上指定してください。"

    # 地点＆緯度経度取得
    station_no = station.split('_')[0]
    df_info = pd.read_sql_query(
        "SELECT DISTINCT point_lat AS latitude, point_lon AS longitude FROM radiation_data WHERE point_no = ?",
        sqlite3.connect(DB_PATH), params=(station_no,)
    )
    lat = float(df_info.iloc[0]['latitude'])
    lon = float(df_info.iloc[0]['longitude'])

    df_raw = load_radiation_df(station_no)
    # GHIと温度データ整形
    df_solar = df_raw[df_raw['element_no']=='00001']
    df_temp  = df_raw[df_raw['element_no']=='00005']
    df_solar = df_solar.pivot_table(index=['month','day'], columns='hour', values='value').reset_index().fillna(0)
    df_temp  = df_temp .pivot_table(index=['month','day'], columns='hour', values='value').reset_index().fillna(0)
    for h in range(1,25):
        df_solar[h] = pd.to_numeric(df_solar[h], errors='coerce')*0.01/3.6
        df_temp[h]  = pd.to_numeric(df_temp[h], errors='coerce')*0.1

    # 時系列化
    site  = pvlib.location.Location(lat, lon, tz='Asia/Tokyo')
    times = pd.date_range(start='2020-01-01', periods=len(df_solar)*24, freq='H', tz='Asia/Tokyo')
    ghi_flat   = df_solar[list(range(1,25))].values.flatten()*1000
    ghi_series = pd.Series(ghi_flat, index=times)
    solpos     = site.get_solarposition(times)

    # パターンごとにPOA計算 & 発電量計算
    results = []
    patterns = [
        (Ppeak1, ori1, int(tilt1)),
        (Ppeak2, ori2, int(tilt2)),
        (Ppeak3, ori3, int(tilt3)),
        (Ppeak4, ori4, int(tilt4)),
    ]
    for idx,(Ppeak_i, ori_i, tilt_i) in enumerate(patterns, start=1):
        # ErbsによるDNI/DHI推定
        sep = pvlib.irradiance.erbs(ghi_series, solpos['zenith'], times)
        dni = sep['dni']; dhi = sep['dhi']
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=tilt_i,
            surface_azimuth=ORIENTATION_TO_AZIMUTH.get(ori_i,180),
            dni=dni, ghi=ghi_series, dhi=dhi,
            solar_zenith=solpos['zenith'], solar_azimuth=solpos['azimuth'], model='isotropic'
        )['poa_global']/1000
        poa_mat = poa.to_numpy().reshape(len(df_solar),24)
        df_pv = df_solar.copy()
        df_pv[list(range(1,25))] = pd.DataFrame(poa_mat, index=df_solar.index)

        # 温度・補正係数・PCS制限適用
        df_out = df_pv.copy()
        for h in range(1,25):
            out_h = K*effective_PAS*df_pv[h]*(1+alpha*(df_temp[h]+delta_T))/GS
            df_out[h] = out_h.clip(upper=Ppeak_i)
        df_out['日発電量'] = df_out[list(range(1,25))].sum(axis=1)
        monthly = df_out.groupby('month')['日発電量'].sum().reset_index()
        monthly['pattern'] = f'パターン{idx}'
        results.append(monthly)

    df_monthly = pd.concat(results, ignore_index=True)
    fig_bar = px.bar(df_monthly, x='month', y='日発電量', color='pattern', barmode='group',
                     title='月別発電量（4パターン比較）')

    # 選択日表示（パターン1のみ）
    df_day1 = df_out[(df_out['month']==month_selected)&(df_out['day']==day_selected)]
    if df_day1.empty:
        fig_line = px.line(title='該当データなし')
    else:
        hourly = df_out[list(range(1,25))].iloc[0]
        df_plot = pd.DataFrame({'時刻':list(range(1,25)), '発電量':hourly.values})
        fig_line = px.line(df_plot, x='時刻', y='発電量', markers=True,
                           title=f'{month_selected}月{day_selected}日の発電量（パターン1）')

    return fig_bar, fig_line

# Gradio UI定義（4パターン入力対応）
with gr.Blocks() as demo:
    gr.Markdown('# NEDO 日射量シミュレーション（4パターン対応）')
    with gr.Row():
        with gr.Column(scale=2):
            station_input = gr.Dropdown(label='地点選択', choices=get_station_options())
            K_input       = gr.Number(label='K', value=0.95)
            PAS_input     = gr.Number(label='PAS', value=1)
            GS_input      = gr.Number(label='GS', value=1)
            alpha_input   = gr.Number(label='α[%/℃]', value=-0.35)
            deltaT_input  = gr.Number(label='ΔT[℃]', value=25)

            # 4パターン分のPpeak, 方位, 傾斜入力
            for i in range(1,5):
                with gr.Row():
                    vars()[f'Ppeak{i}']      = gr.Number(label=f'Ppeak_{i}[kW]', value=1)
                    vars()[f'ori{i}']        = gr.Dropdown(label=f'方位_{i}', choices=list(ORIENTATION_TO_AZIMUTH.keys()), value='南')
                    vars()[f'tilt{i}']       = gr.Dropdown(label=f'傾斜角_{i}(°)', choices=[str(x) for x in range(0,91,10)], value='30')

            month_input   = gr.Textbox(label='月(1–12)', placeholder='例:4')
            day_input     = gr.Textbox(label='日(1–31)', placeholder='例:3')
            run_button    = gr.Button('▶️ 計算')
        with gr.Column(scale=3):
            bar_plot  = gr.Plot(label='月別発電量（4パターン）')
            line_plot = gr.Plot(label='日別発電量（パターン1）')

    run_button.click(
        fn=process_and_plot,
        inputs=[
            station_input, K_input, PAS_input, GS_input, alpha_input, deltaT_input,
            Ppeak1, ori1, tilt1,
            Ppeak2, ori2, tilt2,
            Ppeak3, ori3, tilt3,
            Ppeak4, ori4, tilt4,
            month_input, day_input
        ],
        outputs=[bar_plot, line_plot]
    )

if __name__ == '__main__':
    demo.launch()
