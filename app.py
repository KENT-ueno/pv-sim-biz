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
    month_selected,
    day_selected
):
    """
    Gradio のコールバック関数。
    - uploaded_file: gr.File でアップロードされた NEDO 形式 CSV
    - K, PAS, GS, alpha_percentage, delta_T: 数値パラメータ
    - month_selected, day_selected: ユーザーが選んだ「月・日」
    
    戻り値:
    - fig_bar: 月別発電量の棒グラフ (Plotly Figure)
    - fig_line: 任意日の 24 時間カーブ (Plotly Figure)
    - error_message: エラーや警告があれば文字列を返す（正常時は None）
    """
    # (1) ファイルチェック
    if uploaded_file is None:
        return None, None, "エラー：CSV ファイルがアップロードされていません。"

    try:
        # (2) パラメータ alpha を割合から実数に変換
        alpha = alpha_percentage / 100.0
        # (3) CSV 読み込み：1行目はメタ情報なので skiprows=1, header=None
        #     NEDO 形式なので encoding="shift_jis"
        #     列名は「要素番号, 月, 日, 年, 1時, 2時, ..., 24時, 最大, 最小, 積算, 平均, 通算日」
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

    # (4) 要素番号 1=全天日射量, 5=気温 を抽出
    try:
        df_solar = df[df["要素番号"] == 1].copy().reset_index(drop=True)
        df_temp  = df[df["要素番号"] == 5].copy().reset_index(drop=True)
    except KeyError as e:
        return None, None, f"エラー：データに想定した列がありません。{e}"

    # (5) 単位換算
    #     - df_solar[各時間] は 0.01 MJ/m²→ kWh/m² にする (0.01/3.6)
    #     - df_temp[各時間] は 0.1 ℃→ 実数℃
    for h in time_labels:
        df_solar[h] = pd.to_numeric(df_solar[h], errors="coerce") * 0.01 / 3.6
        df_temp[h]  = pd.to_numeric(df_temp[h],  errors="coerce") * 0.1

    # (6) 日毎の「時刻別発電量（補正前）」を計算
    #     - df_hourly に「月, 日, 1時～24時」のフレームをコピー
    df_hourly = df_solar[["月", "日"] + time_labels].copy().reset_index(drop=True)
    df_temp   = df_temp.reset_index(drop=True)

    #     - 各セルに (K × PAS × df_solar[h] × (1 + α × (df_temp[h] + delta_T)) / GS) を適用
    for h in time_labels:
        df_hourly[h] = (
            K
            * PAS
            * df_solar[h]
            * (1 + alpha * (df_temp[h] + delta_T))
            / GS
        )

    # (7) 日発電量 [kWh] を 24 時間分合計
    df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

    # (8) 月別「積分値（補正前）」を計算
    eph_monthly = (
        df_hourly
        .groupby("月")["日発電量 [kWh]"]
        .sum()
        .reset_index()
    )
    eph_monthly.columns = ["月", "積分値（補正前）"]

    # (9) 「発電量①（基準）」を計算
    #     - df_solar の日積算を合計して月別
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

    # (10) 補正係数を求める & df_hourly にマッピング
    df_monthly2 = pd.merge(
        eph_monthly,
        df_monthly1[["月", "発電量① [kWh]"]],
        on="月"
    )
    df_monthly2["補正係数"] = df_monthly2["発電量① [kWh]"] / df_monthly2["積分値（補正前）"]

    df_hourly["補正係数"] = df_hourly["月"].map(
        df_monthly2.set_index("月")["補正係数"]
    )

    # (11) 任意日の補正後 24 時間発電量を計算
    for h in time_labels:
        df_hourly[f"{h}_補正後"] = df_hourly[h] * df_hourly["補正係数"]

    # —— 棒グラフ：月別発電量（発電量① vs 積分値）——
    bar_df = df_monthly2.rename(columns={
        "発電量① [kWh]": "発電量①（基準）",
        "積分値（補正前）": "発電量②（積分値）"
    })

    # Plotly でグラフを作成
    fig_bar = px.bar(
        bar_df,
        x="月",
        y=["発電量①（基準）", "発電量②（積分値）"],
        barmode="group",
        labels={"value": "発電量 [kWh]", "variable": "種類"},
        title="月別発電量（基準 vs 積分値）"
    )

    # —— 折れ線グラフ：任意日の 24 時間発電量カーブ（補正後）——
    # 指定された月・日に該当する行を抽出
    df_day = df_hourly[
        (df_hourly["月"] == month_selected) & (df_hourly["日"] == day_selected)
    ]
    if df_day.empty:
        # 「該当なし」の場合は空の Figure を返すか、メッセージを返す
        fig_line = px.line(title="該当するデータがありません")
    else:
        # 該当行の補正後 24 時間データを取得（Series で24要素）
        hourly_corrected = df_day[[f"{h}_補正後" for h in time_labels]].iloc[0]

        # NaN チェック
        if hourly_corrected.isnull().any():
            fig_line = px.line(title="この日の補正後データに NaN が含まれています")
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

    # (12) エラーなしなら None を返す
    return fig_bar, fig_line, None


# ──────────────────── Gradio UI 定義 ────────────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（Gradio版）")
    gr.Markdown(
        """
        **説明**：NEDO 形式の CSV をアップロードし、  
        K, PAS, GS, α, ΔT のパラメータを入力すると、  
        - 月別発電量（基準 vs 積分値）の棒グラフ  
        - 任意日の24時間発電量カーブ（補正後）の折れ線グラフ  
        が表示されます。  
        """
    )

    with gr.Row():
        with gr.Column(scale=2):
            # — 左カラム：入力項目 —
            file_input = gr.File(
                label="NEDO 形式 CSV ファイル（1 行目はメタ情報）",
                file_types=[".csv"]
            )
            K_input = gr.Number(label="K（係数）", value=0.95)
            PAS_input = gr.Number(label="PAS（受光面積 m²）", value=10.0)
            GS_input = gr.Number(label="GS（基準日射量）[kWh/m²]", value=1.0)
            alpha_input = gr.Number(label="αpmax（[%/℃]）", value=-0.35)
            deltaT_input = gr.Number(label="ΔT (℃)", value=25.0)

            # 「月・日」は空のままにしておき、コールバック中に選択肢を更新する
            month_dropdown = gr.Dropdown(
                choices=[], label="月を選択", interactive=True
            )
            day_dropdown = gr.Dropdown(
                choices=[], label="日を選択", interactive=True
            )

            # 実行ボタン
            run_button = gr.Button("▶️ 計算してグラフ表示")

            # エラーや警告を出すテキスト
            error_box = gr.Textbox(label="エラー", interactive=False)

        with gr.Column(scale=3):
            # — 右カラム：グラフ出力 —
            bar_plot = gr.Plot(label="月別発電量（基準 vs 積分値）")
            line_plot = gr.Plot(label="任意日の24時間発電量カーブ")

    # ──────────────────────────────────────────
    # (A) CSV ファイルをアップロードしたときに「月・日」の選択肢を更新
    # ──────────────────────────────────────────
    def update_month_day(uploaded_file):
        """
        CSV を読むだけして「月」「日」のドロップダウンの選択肢を返す関数。
        """
        if uploaded_file is None:
            return [], [], "CSV ファイルをアップロードしてください。"

        try:
            # 1行目スキップ、header=None
            time_labels = [f"{h}時" for h in range(1, 25)]
            col_names = ["要素番号", "月", "日", "年"] + time_labels + ["最大", "最小", "積算", "平均", "通算日"]

            df = pd.read_csv(
                uploaded_file.name,
                header=None,
                skiprows=1,
                names=col_names,
                encoding="shift_jis"
            )
            # 「要素番号=1」だけでもいいが、月・日だけ取るなら全行から unique で OK
            months = sorted(df["月"].dropna().unique().tolist())
            days   = sorted(df["日"].dropna().unique().tolist())
        except Exception as e:
            return [], [], f"選択肢更新時のエラー: {e}"

        return months, days, None

    # CSV をアップロードしたら month_dropdown, day_dropdown の choices を更新
    file_input.change(
        fn=update_month_day,
        inputs=[file_input],
        outputs=[month_dropdown, day_dropdown, error_box]
    )

    # ──────────────────────────────────────────
    # (B) 実行ボタンを押したときに「計算＆グラフ表示」を行う
    # ──────────────────────────────────────────
    run_button.click(
        fn=process_and_plot,
        inputs=[
            file_input, 
            K_input, 
            PAS_input, 
            GS_input, 
            alpha_input, 
            deltaT_input, 
            month_dropdown, 
            day_dropdown
        ],
        outputs=[bar_plot, line_plot, error_box]
    )

    # 初期表示ではまだ何も選択肢がないので「実行できません」とメッセージだけ入れておく
    gr.Markdown(
        "<i>まずは左側で CSV をアップロードし、月・日を選択して「計算してグラフ表示」ボタンを押してください。</i>"
    )

# Gradio アプリを起動
if __name__ == "__main__":
    demo.launch()
