import pandas as pd
import sqlite3
import gradio as gr
import plotly.express as px

# 定数: STC条件下の基準照度 (kW/m²)
G_STC = 1.0
# PCS出力のデフォルト値 (kW)
DEFAULT_PCS_OUTPUT = 99.0
# SQLite DB パス（適宜変更してください）
DB_PATH = r"C:\Users\Kent\Desktop\pvprgram\radiation.db"

def get_station_list():
    """DBから利用可能な地点名リストを取得"""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT DISTINCT station_name FROM radiation_data",
        conn
    )
    conn.close()
    return df['station_name'].tolist()

def load_data_from_db(station_name):
    """
    指定地点の全天日射量(element_no=1)と気温(element_no=5) を
    リマークに関わらずすべて読み込み、pivotして返す
    """
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT element_no, month, day, hour, value
        FROM radiation_data
        WHERE station_name = ?
          AND element_no IN (1, 5)
        """,
        conn,
        params=(station_name,)
    )
    conn.close()

    time_labels = [f"{h}時" for h in range(1, 25)]

    # 全天日射量（element_no=1）をpivot
    df_solar = (
        df[df['element_no'] == 1]
        .pivot_table(index=['month','day'], columns='hour', values='value')
        .reset_index()
    )
    # 気温（element_no=5）をpivot
    df_temp = (
        df[df['element_no'] == 5]
        .pivot_table(index=['month','day'], columns='hour', values='value')
        .reset_index()
    )

    # 列名を ['月','日','1時',...,'24時'] に変更
    df_solar.columns = ['月','日'] + time_labels
    df_temp.columns  = ['月','日'] + time_labels

    # 単位補正: MJ→kWh（全天日射量）、0.1℃→℃（気温）
    for h in time_labels:
        df_solar[h] = pd.to_numeric(df_solar[h], errors="coerce") * 0.01 / 3.6
        df_temp[h]  = pd.to_numeric(df_temp[h], errors="coerce")  * 0.1

    return df_solar, df_temp

def process_and_plot(
    station_name,
    K,
    PAS,
    Ppeak,
    GS,
    alpha_percentage,
    delta_T,
    orientation,
    tilt,
    month_str,
    day_str,
    PCS_output_kw
):
    # --- 入力チェック ---
    if not station_name:
        return None, None, None, "エラー：地点を選択してください。"
    try:
        month_selected = int(month_str)
        day_selected   = int(day_str)
        if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
            raise ValueError
    except:
        return None, None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"

    # αを割合に変換
    alpha = alpha_percentage / 100.0

    # PAS/Ppeak で有効受光面積を決定
    if Ppeak not in (None, 0):
        effective_PAS = Ppeak / (K * G_STC)
    elif PAS not in (None, 0):
        effective_PAS = PAS
    else:
        return None, None, None, "エラー：PAS または Ppeak のいずれかを入力してください。"

    # PCS出力のデフォルト適用
    if PCS_output_kw in (None, 0):
        PCS_output_kw = DEFAULT_PCS_OUTPUT

    # --- DB読み込み＆単位補正 ---
    try:
        df_solar, df_temp = load_data_from_db(station_name)
    except Exception as e:
        return None, None, None, f"DB読み込みエラー: {e}"

    # --- 方位・傾斜角の簡易補正 ---
    time_labels = [f"{h}時" for h in range(1, 25)]
    orientation_factors = {
        "北": 0.62, "東": 0.83, "西": 0.83,
        "南東": 0.96, "南西": 0.96, "南": 1.00
    }
    tilt_factors = {
         0: 0.90, 10: 1.00, 20: 1.02, 30: 1.00, 40: 0.95,
        50: 0.90, 60: 0.85, 70: 0.80, 80: 0.75, 90: 0.65
    }
    ori_factor  = orientation_factors.get(orientation, 1.0)
    tilt_factor = tilt_factors.get(int(tilt), 1.0)
    corr_factor = ori_factor * tilt_factor
    for h in time_labels:
        df_solar[h] *= corr_factor

    # --- 時刻別発電量計算 ---
    df_hourly = df_solar[["月", "日"] + time_labels].copy()
    for h in time_labels:
        df_hourly[h] = (
            K
            * effective_PAS
            * df_solar[h]
            * (1 + alpha * (df_temp[h] + delta_T))
            / GS
        )
    df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

    # --- 月別積分値（PCS制限前）---
    eph_monthly = (
        df_hourly.groupby("月")["日発電量 [kWh]"]
        .sum().reset_index()
        .rename(columns={"日発電量 [kWh]": "発電量 [kWh]"})
    )

    # --- PCS出力制限（1時間ごとにクリップ）---
    for h in time_labels:
        df_hourly[h] = df_hourly[h].clip(upper=PCS_output_kw)

    # 月別再計算（クリップ後）
    eph_monthly = (
        df_hourly.groupby("月")[time_labels]
        .sum().reset_index()
        .melt(id_vars=["月"], value_name="発電量 [kWh]")
    )

    # --- グラフ描画 ---
    fig_bar = px.bar(
        eph_monthly,
        x="月",
        y="発電量 [kWh]",
        title="月別発電量（方位・傾斜補正＋PCS制限後）"
    )

    df_day = df_hourly[
        (df_hourly["月"] == month_selected) &
        (df_hourly["日"] == day_selected)
    ]
    if df_day.empty:
        fig_line = px.line(title="該当データなし")
    else:
        hourly = df_day[time_labels].iloc[0]
        df_plot = pd.DataFrame({
            "時刻": list(range(1, 25)),
            "発電量 [kWh]": hourly.values
        })
        fig_line = px.line(
            df_plot,
            x="時刻",
            y="発電量 [kWh]",
            markers=True,
            title=f"{month_selected}月{day_selected}日の24h発電量"
        ).update_layout(xaxis=dict(dtick=1))

    annual_total = df_hourly[time_labels].sum(axis=1).sum()
    annual_str = f"年間発電量: {annual_total:.2f} kWh"

    return fig_bar, fig_line, annual_str, ""

# ─────────────── Gradio UI 定義 ───────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（SQLite版）")
    gr.Markdown(
        """
        地点選択 → 各種パラメータ (K, PAS/Ppeak, GS, α, ΔT, 方位, 傾斜角, PCS容量) → 月/日 → 計算
        """
    )
    station_list = get_station_list()
    with gr.Row():
        with gr.Column(scale=2):
            station_input    = gr.Dropdown(label="地点", choices=station_list, value=station_list[0])
            K_input          = gr.Number(label="K（係数）", value=0.95)
            PAS_input        = gr.Number(label="PAS（受光面積 m²）", value=None)
            Ppeak_input      = gr.Number(label="Ppeak（定格出力 kWₚ）", value=None)
            GS_input         = gr.Number(label="GS（基準日射量）", value=1.0)
            alpha_input      = gr.Number(label="αpmax（[%/℃]）", value=-0.35)
            deltaT_input     = gr.Number(label="ΔT (℃)", value=25.0)
            orientation_input= gr.Dropdown(
                                   label="方位",
                                   choices=["北","東","西","南東","南西","南"],
                                   value="南")
            tilt_input       = gr.Dropdown(
                                   label="傾斜角 (°)",
                                   choices=[str(i) for i in range(0,91,10)],
                                   value="30")
            PCS_input        = gr.Number(label="PCS出力（kW）", value=99)
            month_input      = gr.Textbox(label="月 (1–12)", placeholder="例:1")
            day_input        = gr.Textbox(label="日 (1–31)", placeholder="例:15")
            run_button       = gr.Button("▶️ 計算")
            annual_box       = gr.Textbox(label="年間発電量", interactive=False)
            error_box        = gr.Textbox(label="エラー", interactive=False)

        with gr.Column(scale=3):
            bar_plot         = gr.Plot(label="月別発電量")
            line_plot        = gr.Plot(label="24h発電量カーブ")

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
