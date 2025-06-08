import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib

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
        # --- 入力チェック ---
        if uploaded_file is None:
            return None, None, None, "エラー：ファイルがアップロードされていません。"

        # 月日チェック
        try:
            month_selected = int(month_str)
            day_selected   = int(day_str)
            if not (1 <= month_selected <= 12 and 1 <= day_selected <= 31):
                raise ValueError
        except:
            return None, None, None, "エラー：月は1～12、日は1～31の整数で入力してください。"

        # α を割合化
        alpha = alpha_percentage / 100.0

        # 有効受光面積
        if Ppeak not in (None, 0):
            effective_PAS = Ppeak / (K * G_STC)
        elif PAS not in (None, 0):
            effective_PAS = PAS
        else:
            return None, None, None, "エラー：PAS または Ppeak のいずれかを入力してください。"

        # PCSデフォルト
        if PCS_output_kw in (None, 0):
            PCS_output_kw = DEFAULT_PCS_OUTPUT

        # --- 1行目から緯度経度取得 ---
        # 1行目: "53196 OBATA 34 31.7 136 39.9 10.0"
        with open(uploaded_file.name, "r", encoding="shift_jis") as f:
            parts = f.readline().strip().split()
        # parts = ['53196','OBATA','34','31.7','136','39.9','10.0']
        lat = float(parts[2]) + float(parts[3]) / 60.0
        lon = float(parts[4]) + float(parts[5]) / 60.0

        # --- txt（月別）読み込み ---
        # 列名を日別CSV用と同じ数に揃える（要素番号, 月, 日, 年）+ 1時～24時 + (最大, 最小, 積算, 平均, 通算日数)
        time_labels = [f"{h}時" for h in range(1,25)]
        col_names = ["要素番号","月","日","年"] + time_labels + ["最大","最小","積算","平均","通算日"]

        df = pd.read_csv(
            uploaded_file.name,
            header=None,
            skiprows=1,
            sep=r"\s+",
            names=col_names,
            encoding="shift_jis"
        )

        # 要素1（全天日射量）と要素5（気温）だけ抽出
        df_solar = df[df["要素番号"] == 1].reset_index(drop=True)
        df_temp  = df[df["要素番号"] == 5].reset_index(drop=True)

        # --- リマーク除去＋単位補正 ---
        # 全天日射量: 単位 0.01MJ/m2 → kWh/m2 へ (1 MJ/m2 = 0.2778 kWh/m2, 0.01MJ→0.002778kWh)
        # ここでは (値//10) * 0.01 [MJ/m2] → /3.6 で kWh/m2 に
        for h in time_labels:
            # まず文字列→整数、リマーク切捨て
            vals = df_solar[h].astype(str).str.replace(r"[^\d]", "", regex=True).astype(float)
            numeric = (vals // 10)  # 例: '1066'→106.0
            df_solar[h] = numeric * 0.01 / 3.6

            # 気温: 単位 0.1℃ → ℃
            vals_t = df_temp[h].astype(str).str.replace(r"[^\d\-]", "", regex=True).astype(float)
            numeric_t = (vals_t // 10)
            df_temp[h] = numeric_t * 0.1

        # --- pvlib 計算前準備 ---
        surface_tilt    = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        tz = "Asia/Tokyo"
        site = pvlib.location.Location(lat, lon, tz=tz)

        # --- 時刻インデックス生成 (hour=0..23) ---
        times = []
        for _, row in df_solar.iterrows():
            y = int(row["年"])
            m = int(row["月"])
            d = int(row["日"])
            for h in range(24):
                times.append(pd.Timestamp(y, m, d, h, tz=tz))
        times = pd.DatetimeIndex(times)

        # --- GHI Flatten → W/m2 に変換 (kWh/m2→Wh/m2→W/m2相当) ---
        ghi_flat = df_solar[time_labels].to_numpy().flatten() * 1000.0

        # --- 太陽位置・Clearsky ---
        solpos   = site.get_solarposition(times)
        clearsky = site.get_clearsky(times, model="simplified_solis")
        dhi      = clearsky["dhi"].values
        dni      = clearsky["dni"].values
        zenith   = solpos["zenith"].values
        azimuth  = solpos["azimuth"].values

        # --- POA計算 → 1h あたり kWh/m2 に戻す ---
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=dni, ghi=ghi_flat, dhi=dhi,
            solar_zenith=zenith, solar_azimuth=azimuth,
            model='isotropic'
        )
        poa_kwh = poa["poa_global"] / 1000.0

        # --- df_solar に戻す ---
        poa_mat = poa_kwh.reshape(len(df_solar), 24)
        df_solar[time_labels] = pd.DataFrame(poa_mat, index=df_solar.index)

        # --- 発電量計算 (JIS式＋PCS制限) ---
        df_hourly = df_solar[["月","日"] + time_labels].copy()
        for h in time_labels:
            df_hourly[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

        # 月別集計（PCS制限前）
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

        # --- グラフ描画 ---
        fig_bar = px.bar(eph_monthly, x="月", y="発電量 [kWh]",
                         title="月別発電量（物理ベース補正＋PCS制限後）")
        df_day = df_hourly[
            (df_hourly["月"] == month_selected) &
            (df_hourly["日"] == day_selected)
        ]
        if df_day.empty:
            fig_line = px.line(title="該当データなし")
        else:
            hourly = df_day[time_labels].iloc[0]
            df_plot = pd.DataFrame({
                "時刻": list(range(1,25)),
                "発電量 [kWh]": hourly.values
            })
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
    gr.Markdown("# NEDO txt形式（月別）サンプルシミュレーション")
    gr.Markdown("txt→パラメータ→物理ベース補正＋pvlib→発電量算定")

    with gr.Row():
        with gr.Column(scale=2):
            file_input        = gr.File(label="NEDO txt（月別）", file_types=[".txt"])
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
            month_input       = gr.Textbox(label="月(1–12)", placeholder="例:1")
            day_input         = gr.Textbox(label="日(1–31)", placeholder="例:15")
            run_button        = gr.Button("▶️ 計算")
            annual_box        = gr.Textbox(label="年間発電量", interactive=False)
            error_box         = gr.Textbox(label="エラー", interactive=False)

        with gr.Column(scale=3):
            bar_plot  = gr.Plot(label="月別発電量")
            line_plot = gr.Plot(label="24h発電量カーブ")

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
