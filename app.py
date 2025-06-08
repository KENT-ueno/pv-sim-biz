import pandas as pd
import sqlite3
import gradio as gr
import plotly.express as px
import pvlib

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0
DB_PATH = "radiation.db"  # HuggingFaceにアップ済みのDBファイル

ORIENTATION_TO_AZIMUTH = {
    "北": 0, "東": 90, "西": 270,
    "南東": 135, "南西": 225, "南": 180
}

def get_station_list():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT DISTINCT station_name FROM radiation_data", conn)
    conn.close()
    return df["station_name"].tolist()

def load_radiation_and_temp(station_name):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT month, day, hour, element_no, value
        FROM radiation_data
        WHERE station_name = ? AND element_no IN (1, 5)
        """,
        conn,
        params=(station_name,)
    )
    loc = pd.read_sql_query(
        "SELECT latitude, longitude FROM radiation_data WHERE station_name = ? LIMIT 1",
        conn,
        params=(station_name,)
    )
    conn.close()
    lat, lon = float(loc.iloc[0]["latitude"]), float(loc.iloc[0]["longitude"])

    df_pivot = df.pivot_table(index=["month", "day", "hour"], columns="element_no", values="value").reset_index()
    df_pivot.columns.name = None
    df_pivot = df_pivot.rename(columns={1: "ghi", 5: "temp"})

    # 単位補正
    df_pivot["ghi"] = df_pivot["ghi"] * 0.01 / 3.6  # MJ→kWh
    df_pivot["temp"] = df_pivot["temp"] * 0.1       # 0.1℃→℃
    return df_pivot, lat, lon

def process_and_plot(
    station_name,
    K, PAS, Ppeak, GS,
    alpha_percentage, delta_T,
    orientation, tilt,
    month_str, day_str,
    PCS_output_kw
):
    try:
        if not station_name:
            return None, None, None, "エラー：地点を選択してください。"
        try:
            month_selected = int(month_str)
            day_selected   = int(day_str)
            if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
                raise ValueError
        except:
            return None, None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"

        alpha = alpha_percentage / 100.0
        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, None, "エラー：PAS または Ppeak を入力してください。"

        if PCS_output_kw in (None, 0):
            PCS_output_kw = DEFAULT_PCS_OUTPUT

        # データ読み込み
        df, lat, lon = load_radiation_and_temp(station_name)

        # 日付インデックス生成（年は固定: 2020）
        df["time"] = pd.to_datetime({
            "year": 2020,
            "month": df["month"],
            "day": df["day"],
            "hour": df["hour"] - 1  # 1時→00:00
        }, utc=True).dt.tz_convert("Asia/Tokyo")

        # GHI→W/m²
        ghi = df["ghi"] * 1000

        site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")
        solpos = site.get_solarposition(df["time"])
        clearsky = site.get_clearsky(df["time"], model="simplified_solis")

        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=float(tilt),
            surface_azimuth=ORIENTATION_TO_AZIMUTH.get(orientation, 180),
            dni=clearsky["dni"],
            ghi=ghi,
            dhi=clearsky["dhi"],
            solar_zenith=solpos["zenith"],
            solar_azimuth=solpos["azimuth"],
            model="isotropic"
        )
        df["poa"] = poa["poa_global"] / 1000.0  # W→kWh

        # 発電量計算（kWh）
        df["power"] = (
            K * effective_PAS * df["poa"]
            * (1 + alpha * (df["temp"] + delta_T))
            / GS
        ).clip(upper=PCS_output_kw)

        df["date"] = df["time"].dt.date
        df["month"] = df["time"].dt.month
        df["hour"] = df["time"].dt.hour + 1

        # 日単位・時刻別マトリクス化
        df_pivot = df.pivot_table(index=["month", "date"], columns="hour", values="power", aggfunc="first")
        df_pivot = df_pivot.fillna(0.0)
        df_pivot["日発電量 [kWh]"] = df_pivot.sum(axis=1)

        # 月別棒グラフ
        eph_monthly = (
            df_pivot.groupby("month")["日発電量 [kWh]"]
            .sum().reset_index()
            .rename(columns={"日発電量 [kWh]": "発電量 [kWh]"})
        )
        fig_bar = px.bar(eph_monthly, x="month", y="発電量 [kWh]",
                         title="月別発電量（SQLite＋pvlib＋PCS制限）")

        # 時刻別グラフ
        day_row = df_pivot.reset_index()
        df_day = day_row[(day_row["month"] == month_selected) & (day_row["date"].dt.day == day_selected)]
        if df_day.empty:
            fig_line = px.line(title="該当データなし")
        else:
            hourly = df_day.iloc[0, 2:-1]  # 時刻列のみ
            df_plot = pd.DataFrame({"時刻": hourly.index, "発電量 [kWh]": hourly.values})
            fig_line = px.line(df_plot, x="時刻", y="発電量 [kWh]",
                               markers=True,
                               title=f"{month_selected}月{day_selected}日の24h発電量")

        annual_total = df_pivot.drop(columns="日発電量 [kWh]", errors="ignore").sum().sum()
        annual_str = f"年間発電量: {annual_total:.2f} kWh"

        return fig_bar, fig_line, annual_str, ""

    except Exception as e:
        return None, None, None, f"内部エラー: {e}"

# ─────────────── Gradio UI ───────────────
with gr.Blocks() as demo:
    gr.Markdown("# SQLite＋pvlib版 太陽光発電シミュレーション")
    station_list = get_station_list()
    with gr.Row():
        with gr.Column(scale=2):
            station_input = gr.Dropdown(label="地点", choices=station_list, value=station_list[0])
            K_input       = gr.Number(label="K", value=0.95)
            PAS_input     = gr.Number(label="PAS", value=None)
            Ppeak_input   = gr.Number(label="Ppeak", value=None)
            GS_input      = gr.Number(label="GS", value=1.0)
            alpha_input   = gr.Number(label="α[%/℃]", value=-0.35)
            deltaT_input  = gr.Number(label="ΔT[℃]", value=25.0)
            orientation_input = gr.Dropdown(label="方位", choices=list(ORIENTATION_TO_AZIMUTH.keys()), value="南")
            tilt_input    = gr.Dropdown(label="傾斜角(°)", choices=[str(i) for i in range(0,91,10)], value="30")
            PCS_input     = gr.Number(label="PCS出力[kW]", value=99)
            month_input   = gr.Textbox(label="月(1–12)", placeholder="例:6")
            day_input     = gr.Textbox(label="日(1–31)", placeholder="例:15")
            run_button    = gr.Button("▶️ 計算")
            annual_box    = gr.Textbox(label="年間発電量", interactive=False)
            error_box     = gr.Textbox(label="エラー", interactive=False)

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
