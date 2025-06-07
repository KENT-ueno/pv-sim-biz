import pandas as pd
import gradio as gr
import plotly.express as px

# 定数: STC条件下の基準照度 (kW/m²)
G_STC = 1.0

def process_and_plot(
    uploaded_file,
    K,
    PAS,
    Ppeak,
    GS,
    alpha_percentage,
    delta_T,
    month_str,
    day_str
):
    """
    Gradio のコールバック関数。
    - uploaded_file: gr.File でアップロードされた NEDO 形式 CSV
    - K, PAS, Ppeak, GS, alpha_percentage, delta_T: 数値パラメータ
    - month_str, day_str: Textbox で入力された「月」「日」（文字列）

    PAS または Ppeak のいずれかで受光面積を決定します。
    Ppeak が入力されていれば、PAS = Ppeak / (K * G_STC) として計算します。

    戻り値:
    - fig_bar: 月別発電量の棒グラフ (Plotly Figure)
    - fig_line: 任意日の 24 時間カーブ (Plotly Figure)
    - error_message: エラーや警告があれば文字列を返す（正常時は "" を返す）
    """

    # CSV ファイルチェック
    if uploaded_file is None:
        return None, None, "エラー：CSV ファイルがアップロードされていません。"

    # 月・日入力チェック
    try:
        month_selected = int(month_str)
        day_selected = int(day_str)
        if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
            return None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"
    except:
        return None, None, "エラー：月・日には整数を入力してください。"

    # 温度係数変換
    alpha = alpha_percentage / 100.0

    # PAS or Ppeak いずれか入力判定
    if Ppeak is not None and Ppeak != 0:
        effective_PAS = Ppeak / (K * G_STC)
    elif PAS is not None and PAS != 0:
        effective_PAS = PAS
    else:
        return None, None, "エラー：PAS または Ppeak のいずれかを入力してください。"

    # CSV読み込み
    time_labels = [f"{h}時" for h in range(1, 25)]
    col_names = ["要素番号", "月", "日", "年"] + time_labels + ["最大", "最小", "積算", "平均", "通算日"]
    try:
        df = pd.read_csv(
            uploaded_file.name,
            header=None,
            skiprows=1,
            names=col_names,
            encoding="shift_jis"
        )
    except Exception as e:
        return None, None, f"CSV読み込みエラー: {e}"

    # データ抽出
    df_solar = df[df["要素番号"] == 1].reset_index(drop=True)
    df_temp = df[df["要素番号"] == 5].reset_index(drop=True)

    # 単位補正: 日射量(0.01 MJ/m²→kWh/m²), 気温(0.1℃→℃)
    for h in time_labels:
        df_solar[h] = pd.to_numeric(df_solar[h], errors="coerce") * 0.01 / 3.6
        df_temp[h] = pd.to_numeric(df_temp[h], errors="coerce") * 0.1

    # 時刻別発電量(補正前)
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

    # 月別積分値
    eph_monthly = df_hourly.groupby("月")["日発電量 [kWh]"].sum().reset_index()
    eph_monthly.columns = ["月", "積分値（補正前）"]

    # 基準方式 (月合計日射量)
    df_solar["日積算 [kWh/m²]"] = df_solar[time_labels].sum(axis=1)
    df_temp["日平均気温 [℃]"] = df_temp[time_labels].mean(axis=1)
    ham = df_solar.groupby("月")["日積算 [kWh/m²]"].sum().reset_index()
    tam = df_temp.groupby("月")["日平均気温 [℃]"].mean().reset_index()
    df_monthly1 = pd.merge(ham, tam, on="月")
    df_monthly1["発電量① [kWh]"] = (
        K
        * effective_PAS
        * df_monthly1["日積算 [kWh/m²]"]
        * (1 + alpha * delta_T)
        / GS
    )

    # 補正係数マッピング
    df_monthly2 = pd.merge(eph_monthly, df_monthly1[["月", "発電量① [kWh]"]], on="月")
    df_monthly2["補正係数"] = df_monthly2["発電量① [kWh]"] / df_monthly2["積分値（補正前）"]
    df_hourly["補正係数"] = df_hourly["月"].map(df_monthly2.set_index("月")["補正係数"])

    # 補正後時間別
    for h in time_labels:
        df_hourly[f"{h}_補正後"] = df_hourly[h] * df_hourly["補正係数"]

    # グラフ作成
    bar_df = df_monthly2.rename(columns={
        "発電量① [kWh]": "発電量①（基準）",
        "積分値（補正前）": "発電量②（積分値）"
    })
    fig_bar = px.bar(
        bar_df,
        x="月",
        y=["発電量①（基準）", "発電量②（積分値）"],
        barmode="group",
        labels={"value": "発電量 [kWh]", "variable": "種類"},
        title="月別発電量（基準 vs 積分値）"
    )

    df_day = df_hourly[(df_hourly["月"] == month_selected) & (df_hourly["日"] == day_selected)]
    if df_day.empty:
        fig_line = px.line(title="該当するデータが見つかりません")
    else:
        hourly_corrected = df_day[[f"{h}_補正後" for h in time_labels]].iloc[0]
        df_plot = pd.DataFrame({"時刻": list(range(1, 25)), "発電量（補正後）[kWh]": hourly_corrected.values})
        fig_line = px.line(
            df_plot,
            x="時刻",
            y="発電量（補正後）[kWh]",
            markers=True,
            title=f"{int(month_selected)}月{int(day_selected)}日の24時間発電量カーブ"
        )
        fig_line.update_layout(xaxis=dict(dtick=1))

    return fig_bar, fig_line, ""

# ─────────────── Gradio UI 定義 ───────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（Gradio 版）")
    gr.Markdown(
        """
        **説明**: CSVファイルをアップロードし、K / PAS または Ppeak / GS / α / ΔT を入力後、月・日を入力して「計算」
        - `PAS` または `Ppeak` のいずれかで受光面積を指定可能
        - 月別発電量＆任意日の24時間発電量を表示
        """
    )

    with gr.Row():
        with gr.Column(scale=2):
            file_input = gr.File(label="NEDO 形式 CSV ファイル", file_types=[".csv"])
            K_input = gr.Number(label="K（係数）", value=0.95)
            PAS_input = gr.Number(label="PAS（受光面積 m²）", value=None)
            Ppeak_input = gr.Number(label="Ppeak（定格出力 kWₚ）", value=None)
            GS_input = gr.Number(label="GS（基準日射量 kWh/m²）", value=1.0)
            alpha_input = gr.Number(label="αpmax（[%/℃]）", value=-0.35)
            deltaT_input = gr.Number(label="ΔT (℃)", value=25.0)
            month_input = gr.Textbox(label="月 (1–12)", placeholder="例: 1")
            day_input = gr.Textbox(label="日 (1–31)", placeholder="例: 15")
            run_button = gr.Button("▶️ 計算")
            error_box = gr.Textbox(label="エラー", interactive=False)

        with gr.Column(scale=3):
            bar_plot = gr.Plot(label="月別発電量")
            line_plot = gr.Plot(label="24時間発電量カーブ")

    run_button.click(
        fn=process_and_plot,
        inputs=[file_input, K_input, PAS_input, Ppeak_input, GS_input, alpha_input, deltaT_input, month_input, day_input],
        outputs=[bar_plot, line_plot, error_box]
    )

if __name__ == "__main__":
    demo.launch()
