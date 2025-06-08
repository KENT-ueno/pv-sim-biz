import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib  # 必要ライブラリ

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0
DB_PATH = "/mnt/data/radiation.db"  # SQLite DBファイルパス
ORIENTATION_TO_AZIMUTH = {
    "北":   0, "東":  90, "西": 270,
    "南東":135, "南西":225, "南":180
}

# DBから地点リストを取得するヘルパー関数
def get_station_options():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT DISTINCT station_no, station_name FROM radiation", conn
    )
    conn.close()
    options = df.apply(
        lambda row: f"{row['station_no']} - {row['station_name']}", axis=1
    ).tolist()
    return options

station_options = get_station_options()

def process_and_plot(
    station_selected,
    K, PAS, Ppeak, GS,
    alpha_percentage, delta_T,
    orientation, tilt,
    month_str, day_str,
    PCS_output_kw
):
    try:
        # 入力チェック: 地点選択
        if not station_selected:
            return None, None, None, "エラー：地点が選択されていません。"
        # 月日チェック
        try:
            month_selected = int(month_str)
            day_selected   = int(day_str)
            if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
                raise ValueError
        except:
            return None, None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"

        # PAS/Ppeak
        alpha = alpha_percentage / 100.0
        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, None, "エラー：PAS または Ppeak を入力してください。"

        # PCSデフォルト
        if PCS_output_kw in (None, 0):
            PCS_output_kw = DEFAULT_PCS_OUTPUT

        # 選択されたstationからNoとNameを分割
        station_no, station_name = station_selected.split(" - ", 1)

        # DB接続・データ取得
        conn = sqlite3.connect(DB_PATH)
        # 緯度経度取得
        latlon = pd.read_sql_query(
            "SELECT latitude, longitude FROM radiation WHERE station_no = ? LIMIT 1", 
            conn, params=(station_no,)
        )
        lat = float(latlon['latitude'].iloc[0])
        lon = float(latlon['longitude'].iloc[0])
        # 全日射量データ
        df_solar_raw = pd.read_sql_query(
            "SELECT month, day, hour, value FROM radiation WHERE station_no = ? AND element_no = 1",
            conn, params=(station_no,)
        )
        # 気温データ
        df_temp_raw  = pd.read_sql_query(
            "SELECT month, day, hour, value FROM radiation WHERE station_no = ? AND element_no = 5",
            conn, params=(station_no,)
        )
        conn.close()

        # pivotで横持ち化
        df_solar = df_solar_raw.pivot(index=["month", "day"], columns="hour", values="value").reset_index()
        df_temp  = df_temp_raw .pivot(index=["month", "day"], columns="hour", values="value").reset_index()

        # 時間ラベル
        time_labels = [f"{h}時" for h in range(1,25)]
        # カラム名変更 (1→"1時"...)
        df_solar.rename(columns={h: f"{h}時" for h in range(1,25)}, inplace=True)
        df_temp .rename(columns={h: f"{h}時" for h in range(1,25)}, inplace=True)

        # 単位補正
        for h in time_labels:
            df_solar[h] = pd.to_numeric(df_solar[h], errors="coerce").fillna(0.0) * 0.01 / 3.6
            df_temp [h] = pd.to_numeric(df_temp [h], errors="coerce").fillna(0.0) * 0.1

        # pvlib 設定
        surface_tilt    = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")

        # 時刻インデックス生成 (hour=0..23), 年はダミー2020年
        year_dummy = 2020
        times = []
        for _, row in df_solar.iterrows():
            m = int(row["month"])
            d = int(row["day"])
            for h in range(1,25):
                times.append(pd.Timestamp(year_dummy, m, d, h-1, tz="Asia/Tokyo"))
        times = pd.DatetimeIndex(times)

        # GHI flatten (kWh/m² → W/m²)
        ghi_flat = df_solar[time_labels].to_numpy().flatten() * 1000.0

        # 太陽位置・Clearsky
        solpos   = site.get_solarposition(times)
        clearsky = site.get_clearsky(times, model="simplified_solis")
        dhi      = clearsky["dhi"].values
        dni      = clearsky["dni"].values
        solar_zenith  = solpos["zenith"].values
        solar_azimuth = solpos["azimuth"].values

        # POA 計算 → kWh/m²
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=dni, ghi=ghi_flat, dhi=dhi,
            solar_zenith=solar_zenith,
            solar_azimuth=solar_azimuth,
            model='isotropic'
        )
        poa_kwh = poa["poa_global"] / 1000.0

        # df_solar へ戻す
        poa_mat = poa_kwh.reshape(len(df_solar), 24)
        df_solar[time_labels] = pd.DataFrame(poa_mat, index=df_solar.index)

        # 発電量計算 (JIS式＋PCS制限)
        df_hourly = df_solar[["month","day"] + time_labels].copy()
        for h in time_labels:
            df_hourly[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

        # 月別集計 (PCS制限前)
        eph_monthly = (
            df_hourly.groupby("month")["日発電量 [kWh]"]
            .sum().reset_index().rename(columns={"日発電量 [kWh]":"発電量 [kWh]"})
        )
        # PCSクリップ
        for h in time_labels:
            df_hourly[h] = df_hourly[h].clip(upper=PCS_output_kw)
        eph_monthly = (
            df_hourly.groupby("month")[time_labels]
            .sum().reset_index()
            .melt(id_vars=["month"], value_name="発電量 [kWh]")
        )

        # グラフ描画
        fig_bar = px.bar(
            eph_monthly, x="month", y="発電量 [kWh]",
            title="月別発電量（物理ベース補正＋PCS制限後）"
        )
        df_day = df_hourly[(df_hourly["month"]==month_selected)&(df_hourly["day"]==day_selected)]
        if df_day.empty:
            fig_line = px.line(title="該当データなし")
        else:
            hourly = df_day[time_labels].iloc[0]
            df_plot = pd.DataFrame({"時刻":list(range(1,25)),"発電量 [kWh]":hourly.values})
            fig_line = px.line(
                df_plot, x="時刻", y="発電量 [kWh]",
                markers=True,
                title=f"{month_selected}月{day_selected}日の24h発電量"
            ).update_layout(xaxis=dict(dtick=1))

        annual_total = df_hourly[time_labels].sum().sum()
        annual_str   = f"年間発電量: {annual_total:.2f} kWh"

        return fig_bar, fig_line, annual_str, ""
    except Exception as e:
        return None, None, None, f"内部エラー: {e}"

# ───────────────── Gradio UI ─────────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（SQL版）")
    gr.Markdown("DBから地点を選択して計算を行います")

    with gr.Row():
        with gr.Column(scale=2):
            station_input    = gr.Dropdown(
                label="地点 (station_no - station_name)",
                choices=station_options,
                value=station_options[0] if station_options else None
            )
            K_input           = gr.Number(label="K", value=0.95)
            PAS_input         = gr.Number(label="PAS", value=None)
            Ppeak_input       = gr.Number(label="Ppeak", value=None)
            GS_input          = gr.Number(label="GS", value=1.0)
            alpha_input       = gr.Number(label="α[%/℃]", value=-0.35)
            deltaT_input      = gr.Number(label="ΔT[℃]", value=25.0)
            orientation_input = gr.Dropdown(
                label="方位",
                choices=list(ORIENTATION_TO_AZIMUTH.keys()),
                value="南"
            )
            tilt_input        = gr.Dropdown(
                label="傾斜角(°)",
                choices=[str(i) for i in range(0,91,10)],
                value="30"
            )
            PCS_input         = gr.Number(label="PCS出力[kW]", value=99)
            month_input       = gr.Textbox(label="月(1–12)", placeholder="例:6")
            day_input         = gr.Textbox(label="日(1–31)", placeholder="例:15")
            run_button        = gr.Button("▶️ 計算")
            annual_box        = gr.Textbox(label="年間発電量", interactive=False)
            error_box         = gr.Textbox(label="エラー", interactive=False)

        with gr.Column(scale=3):
            bar_plot  = gr.Plot(label="月別発電量")
            line_plot = gr.Plot(label="24h発電量")

    run_button.click(
        fn=process_and_plot,
        inputs=[
            station_input, K_input, PAS_input, Ppeak_input,
            GS_input, alpha_input, deltaT_input,
            orientation_input, tilt_input,
            month_input, day_input, PCS_input
        ],
        outputs=[bar_plot, line_plot, annual_box, error_box]
    )

if __name__ == "__main__":
    demo.launch()
