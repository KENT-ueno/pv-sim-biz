import pandas as pd
import numpy as np
import gradio as gr
import plotly.express as px
import pvlib  # 必要ライブラリ

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0

ORIENTATION_TO_AZIMUTH = {
    "北":   0, "東":  90, "西": 270,
    "南東":135, "南西":225, "南":180
}

def process_and_plot(
    uploaded_file,
    K, PAS, Ppeak, GS,
    alpha_percentage, delta_T,
    orientation, tilt,
    month_str, day_str,
    PCS_output_kw
):
    try:
        # 入力チェック
        if uploaded_file is None:
            return None, None, None, "エラー：TXTがアップロードされていません。"
        try:
            month_selected = int(month_str)
            day_selected   = int(day_str)
            if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
                raise ValueError
        except:
            return None, None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"

        alpha = alpha_percentage / 100.0

        # 有効PAS
        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, None, "エラー：PAS または Ppeak を入力してください。"

        # PCSデフォルト
        if PCS_output_kw in (None, 0):
            PCS_output_kw = DEFAULT_PCS_OUTPUT

        # 緯度経度取得（1行目: NEDO形式TXT）
        with open(uploaded_file.name, "r", encoding="shift_jis") as f:
            parts = f.readline().strip().split()
        lat = float(parts[2]) + float(parts[3]) / 60.0
        lon = float(parts[4]) + float(parts[5]) / 60.0

        # 時刻ラベル準備
        time_labels = [f"{h}時" for h in range(1,25)]
        # 全列名（使用しない列も読み込むため定義）
        col_names = ["要素番号","月","日","年"] + time_labels + ["最大","最小","積算","平均","通算日"]

        # TXT読み込み（空白区切り、2行目以降）
        df = pd.read_csv(
            uploaded_file.name,
            header=None, skiprows=1,
            names=col_names,
            delim_whitespace=True,
            dtype=str  # まず文字列として読み込む
        )

        # 全天日射量（要素番号00001）と気温（00005）だけ抽出
        df_solar = df[df["要素番号"]=="00001"].reset_index(drop=True)
        df_temp  = df[df["要素番号"]=="00005"].reset_index(drop=True)

        # リマーク付きデータから数値部だけ取り出すヘルパー
        def strip_remark(s):
            s = str(s).strip()
            # 4文字以上なら末尾1文字をリマークとみなす
            return s[:-1] if len(s) > 3 else s

        # 単位変換 & リマーク除去
        for h in time_labels:
            df_solar[h] = (
                df_solar[h]
                .apply(strip_remark)
                .astype(float) * 0.01 / 3.6
            )
            df_temp[h]  = (
                df_temp[h]
                .apply(strip_remark)
                .astype(float) * 0.1
            )

        # 欠損があれば0に
        df_solar[time_labels] = df_solar[time_labels].fillna(0.0)
        df_temp[time_labels]  = df_temp[time_labels].fillna(0.0)

        # pvlib 設定
        surface_tilt    = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        site = pvlib.location.Location(lat, lon, tz="Asia/Tokyo")

        # 時刻インデックス生成 (ダミー年=2020, hour=0..23)
        n = len(df_solar)
        hours  = np.tile(np.arange(24), n)
        months = np.repeat(df_solar["月"].astype(int).values, 24)
        days   = np.repeat(df_solar["日"].astype(int).values, 24)
        years  = np.repeat(2020, n*24)  # ←ダミー2020年
        times = pd.to_datetime({
            "year":  years,
            "month": months,
            "day":   days,
            "hour":  hours
        }).tz_localize("Asia/Tokyo")

        # GHI flatten (kWh/m²→W/m²)
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
        poa_kwh = poa["poa_global"] / 1000.0  # numpy.ndarray

        # df_solar へ戻す
        poa_mat = poa_kwh.reshape(len(df_solar), 24)
        df_solar[time_labels] = pd.DataFrame(poa_mat, index=df_solar.index)

        # 発電量計算 (JIS式＋PCS制限)
        df_hourly = df_solar[["月","日"]+time_labels].copy()
        for h in time_labels:
            df_hourly[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

        # 月別集計 (PCS制限前→後)
        eph_monthly = (
            df_hourly.groupby("月")["日発電量 [kWh]"]
            .sum().reset_index().rename(columns={"日発電量 [kWh]":"発電量 [kWh]"})
        )
        # PCSクリップ
        for h in time_labels:
            df_hourly[h] = df_hourly[h].clip(upper=PCS_output_kw)
        eph_monthly = (
            df_hourly.groupby("月")[time_labels]
            .sum().reset_index()
            .melt(id_vars=["月"], value_name="発電量 [kWh]")
        )

        # グラフ描画
        fig_bar = px.bar(eph_monthly, x="月", y="発電量 [kWh]",
                         title="月別発電量（物理ベース補正＋PCS制限後）")
        df_day = df_hourly[
            (df_hourly["月"]==month_selected)&(df_hourly["日"]==day_selected)
        ]
        if df_day.empty:
            fig_line = px.line(title="該当データなし")
        else:
            hourly = df_day[time_labels].iloc[0]
            df_plot = pd.DataFrame({"時刻":list(range(1,25)),"発電量 [kWh]":hourly.values})
            fig_line = px.line(df_plot, x="時刻", y="発電量 [kWh]",
                               markers=True,
                               title=f"{month_selected}月{day_selected}日の24h発電量")\
                         .update_layout(xaxis=dict(dtick=1))

        annual_total = df_hourly[time_labels].sum().sum()
        annual_str   = f"年間発電量: {annual_total:.2f} kWh"

        return fig_bar, fig_line, annual_str, ""

    except Exception as e:
        return None, None, None, f"内部エラー: {e}"

# ───────────────── Gradio UI ─────────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（Gradio 版）")
    gr.Markdown("TXT→パラメータ→計算")

    with gr.Row():
        with gr.Column(scale=2):
            file_input        = gr.File(label="NEDO形式TXT", file_types=[".txt"])
            K_input           = gr.Number(label="K", value=0.95)
            PAS_input         = gr.Number(label="PAS", value=None)
            Ppeak_input       = gr.Number(label="Ppeak", value=None)
            GS_input          = gr.Number(label="GS", value=1.0)
            alpha_input       = gr.Number(label="α[%/℃]", value=-0.35)
            deltaT_input      = gr.Number(label="ΔT[℃]", value=25.0)
            orientation_input = gr.Dropdown(label="方位",
                                           choices=list(ORIENTATION_TO_AZIMUTH.keys()),
                                           value="南")
            tilt_input        = gr.Dropdown(label="傾斜角(°)",
                                           choices=[str(i) for i in range(0,91,10)],
                                           value="30")
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
            file_input, K_input, PAS_input, Ppeak_input,
            GS_input, alpha_input, deltaT_input,
            orientation_input, tilt_input,
            month_input, day_input, PCS_input
        ],
        outputs=[bar_plot, line_plot, annual_box, error_box]
    )

if __name__ == "__main__":
    demo.launch()