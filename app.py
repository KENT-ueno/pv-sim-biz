import pandas as pd
import gradio as gr
import plotly.express as px


def process_and_plot(
    uploaded_file,
    K, 
    PAS, 
    GS, 
    alpha_percentage, 
    delta_T,
    month_str,
    day_str
):
    """
    Gradio のコールバック関数。
    - uploaded_file: gr.File でアップロードされた NEDO 形式 CSV
    - K, PAS, GS, alpha_percentage, delta_T: 数値パラメータ
    - month_str, day_str: Textbox で入力された「月」「日」（文字列）
    
    戻り値:
    - fig_bar: 月別発電量の棒グラフ (Plotly Figure)
    - fig_line: 任意日の 24 時間カーブ (Plotly Figure)
    - error_message: エラーや警告があれば文字列を返す（正常時は "" を返す）
    """

    # (1) CSV ファイルがアップロードされているかチェック
    if uploaded_file is None:
        return None, None, "エラー：CSV ファイルがアップロードされていません。"

    # (2) 月・日入力を整数に変換
    try:
        month_selected = int(month_str)
        day_selected   = int(day_str)
        if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
            return None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"
    except:
        return None, None, "エラー：月・日には整数を入力してください。"

    # (3) α の割合を実数に変換
    alpha = alpha_percentage / 100.0

    # (4) CSV 読み込み
    try:
        # NEDO 形式の CSV は 1行目がメタ情報なので skiprows=1, header=None
        # encoding="shift_jis" として文字化けを防ぐ
        time_labels = [f"{h}時" for h in range(1, 25)]
        col_names = ["要素番号", "月", "日", "年"] + time_labels + ["最大", "最小", "積算", "平均", "通算日"]

        df = pd.read_csv(
            uploaded_file.name,
            header=None,
            skiprows=1,
            names=col_names,
            encoding="shift_jis"
        )
    except Exception as e:
        return None, None, f"CSV 読み込みエラー: {e}"

    # (5) 要素番号ごとにデータを分離
    try:
        df_solar = df[df["要素番号"] == 1].copy().reset_index(drop=True)  # 全天日射量
        df_temp  = df[df["要素番号"] == 5].copy().reset_index(drop=True)  # 気温
    except KeyError as e:
        return None, None, f"エラー：想定した列がありません ({e})"

    # (6) 単位換算
    # df_solar の「h時」の列は 0.01 MJ/m² → kWh/m² にするために *0.01/3.6
    # df_temp の「h時」の列は 0.1 ℃ → 実数℃ にするために *0.1
    for h in time_labels:
        df_solar[h] = pd.to_numeric(df_solar[h], errors="coerce") * 0.01 / 3.6
        df_temp[h]  = pd.to_numeric(df_temp[h],  errors="coerce") * 0.1

    # (7) 日毎の「時刻別発電量（補正前）」を計算
    # df_hourly に「月, 日, 1時～24時」の列をコピー
    df_hourly = df_solar[["月", "日"] + time_labels].copy().reset_index(drop=True)
    df_temp   = df_temp.reset_index(drop=True)

    # 各時間帯ごとに (K * PAS * df_solar[h] * (1 + alpha * (df_temp[h] + delta_T)) / GS) を計算
    for h in time_labels:
        df_hourly[h] = (
            K
            * PAS
            * df_solar[h]
            * (1 + alpha * (df_temp[h] + delta_T))
            / GS
        )

    # (8) 日発電量 [kWh] を 24 時間分合計
    df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

    # (9) 月別「積分値（補正前）」を計算
    eph_monthly = (
        df_hourly
        .groupby("月")["日発電量 [kWh]"]
        .sum()
        .reset_index()
    )
    eph_monthly.columns = ["月", "積分値（補正前）"]

    # (10) 月別「発電量①（基準）」を計算
    df_solar["日積算 [kWh/m²]"] = df_solar[time_labels].sum(axis=1)
    df_temp["日平均気温 [℃]"]   = df_temp[time_labels].mean(axis=1)

    ham = df_solar.groupby("月")["日積算 [kWh/m²]"].sum().reset_index()
    tam = df_temp.groupby("月")["日平均気温 [℃]"].mean().reset_index()

    df_monthly1 = pd.merge(ham, tam, on="月")
    df_monthly1["発電量① [kWh]"] = (
        K
        * PAS
        * df_monthly1["日積算 [kWh/m²]"]
        * (1 + alpha * delta_T)
        / GS
    )

    # (11) 補正係数を算出して df_hourly にマッピング
    df_monthly2 = pd.merge(
        eph_monthly,
        df_monthly1[["月", "発電量① [kWh]"]],
        on="月"
    )
    df_monthly2["補正係数"] = df_monthly2["発電量① [kWh]"] / df_monthly2["積分値（補正前）"]

    df_hourly["補正係数"] = df_hourly["月"].map(
        df_monthly2.set_index("月")["補正係数"]
    )

    # (12) 任意日の「補正後 24 時間発電量」を計算
    for h in time_labels:
        df_hourly[f"{h}_補正後"] = df_hourly[h] * df_hourly["補正係数"]

    # —————— 棒グラフ：月別発電量（発電量① vs 積分値） ——————
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

    # —————— 折れ線グラフ：任意日の 24 時間カーブ（補正後） ——————
    df_day = df_hourly[
        (df_hourly["月"] == month_selected) & (df_hourly["日"] == day_selected)
    ]

    if df_day.empty:
        # データが該当しない場合は空の図を返す
        fig_line = px.line(title="該当するデータが見つかりません")
    else:
        hourly_corrected = df_day[[f"{h}_補正後" for h in time_labels]].iloc[0]
        if hourly_corrected.isnull().any():
            fig_line = px.line(title="補正後データに NaN が含まれています")
        else:
            df_plot = pd.DataFrame({
                "時刻": list(range(1, 25)),
                "発電量（補正後）[kWh]": hourly_corrected.values
            })
            fig_line = px.line(
                df_plot,
                x="時刻",
                y="発電量（補正後）[kWh]",
                markers=True,
                title=f"{int(month_selected)}月{int(day_selected)}日の24時間発電量カーブ"
            )
            fig_line.update_layout(xaxis=dict(dtick=1))

    # 正常終了なのでエラーメッセージは空文字を返す
    return fig_bar, fig_line, ""


# ─────────────── Gradio UI 定義 ───────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（Gradio 版）")
    gr.Markdown(
        """
        **説明**: NEDO 形式の CSV ファイルをアップロードし、
        K, PAS, GS, α, ΔT のパラメータを入力したあと、月と日を整数で入力して
        「計算してグラフ表示」ボタンを押すと、  
        - 月別発電量（発電量① vs 積分値）の棒グラフ  
        - 任意日の 24 時間発電量カーブ（補正後）の折れ線グラフ  
        が表示されます。  
        """
    )

    with gr.Row():
        with gr.Column(scale=2):
            # ファイルアップローダー
            file_input = gr.File(
                label="NEDO 形式 CSV ファイル（1 行目はメタ情報）",
                file_types=[".csv"]
            )
            # 数値パラメータ入力
            K_input      = gr.Number(label="K（係数）", value=0.95)
            PAS_input    = gr.Number(label="PAS（受光面積 m²）", value=10.0)
            GS_input     = gr.Number(label="GS（基準日射量）[kWh/m²]", value=1.0)
            alpha_input  = gr.Number(label="αpmax（[%/℃]）", value=-0.35)
            deltaT_input = gr.Number(label="ΔT (℃)", value=25.0)
            # 月・日を Textbox で直接入力
            month_input  = gr.Textbox(
                label="月 (1～12 の整数で入力)",
                placeholder="例: 1"
            )
            day_input    = gr.Textbox(
                label="日 (1～31 の整数で入力)",
                placeholder="例: 15"
            )
            # 実行ボタン
            run_button   = gr.Button("▶️ 計算してグラフ表示")
            # エラーを表示するテキストボックス
            error_box    = gr.Textbox(label="エラー", interactive=False)

        with gr.Column(scale=3):
            # グラフ出力用コンポーネント
            bar_plot  = gr.Plot(label="月別発電量（基準 vs 積分値）")
            line_plot = gr.Plot(label="任意日の24時間発電量カーブ")

    # 「計算」ボタンが押されたら process_and_plot を呼び出す
    run_button.click(
        fn=process_and_plot,
        inputs=[
            file_input, 
            K_input, 
            PAS_input, 
            GS_input, 
            alpha_input, 
            deltaT_input, 
            month_input, 
            day_input
        ],
        outputs=[bar_plot, line_plot, error_box]
    )

if __name__ == "__main__":
    demo.launch()
