import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib  # 追加

# 定数: STC条件下の基準照度 (kW/m²)
G_STC = 1.0
# PCS出力のデフォルト値 (kW)
DEFAULT_PCS_OUTPUT = 99.0

# 方位→azimuth（pvlib流）の変換辞書
ORIENTATION_TO_AZIMUTH = {
    "北":   0,
    "東":  90,
    "西": 270,
    "南東": 135,
    "南西": 225,
    "南": 180
}

def process_and_plot(
    uploaded_file,
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
    """
    Gradio のコールバック関数。
    - uploaded_file: gr.File でアップロードされた NEDO 形式 CSV
    - K, PAS, Ppeak, GS, α, ΔT: 数値パラメータ
    - orientation: 方位（北/東/西/南東/南西/南）
    - tilt: 傾斜角 (0–90°, 10度刻み)
    - month_str, day_str: 月・日（文字列）
    - PCS_output_kw: PCS出力上限（kW）、クリップに使用。未入力時はデフォルト値を適用。
    """
    try:
        # --- 入力チェック ---
        if uploaded_file is None:
            return None, None, None, "エラー：CSV ファイルがアップロードされていません。"
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

        # --- CSVから緯度経度取得（NEDO形式: 1行目の3,4列目が緯度、5,6列目が経度） ---
        with open(uploaded_file.name, "r", encoding="shift_jis") as f:
            first_line = f.readline().strip()
        parts = first_line.split(",")
        lat = float(parts[2]) + float(parts[3]) / 60    # 緯度
        lon = float(parts[4]) + float(parts[5]) / 60    # 経度

        # --- CSV読み込み＆単位補正 ---
        time_labels = [f"{h}時" for h in range(1, 25)]
        col_names = ["要素番号", "月", "日", "年"] + time_labels + ["最大", "最小", "積算", "平均", "通算日"]
        df = pd.read_csv(
            uploaded_file.name,
            header=None,
            skiprows=1,
            names=col_names,
            encoding="shift_jis"
        )

        df_solar = df[df["要素番号"] == 1].reset_index(drop=True)
        df_temp  = df[df["要素番号"] == 5].reset_index(drop=True)

        # MJ→kWh, 0.1℃→℃
        for h in time_labels:
            df_solar[h] = pd.to_numeric(df_solar[h], errors="coerce") * 0.01 / 3.6
            df_temp[h]  = pd.to_numeric(df_temp[h], errors="coerce")  * 0.1

        # --- pvlibでPOA（傾斜・方位補正後）日射量を計算 ---
        surface_tilt = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        tz = "Asia/Tokyo"
        site = pvlib.location.Location(lat, lon, tz=tz)

        # datetime インデックス作成
        times, idx_map = [], []
        for idx, row in df_solar.iterrows():
            y, m, d = int(row["年"]), int(row["月"]), int(row["日"])
            for h in range(1, 25):
                dt = pd.Timestamp(year=y, month=m, day=d, hour=h, tz=tz)
                times.append(dt)
                idx_map.append((idx, h))
        times = pd.DatetimeIndex(times)

        # GHI配列生成 (W/m²)
        ghi_flat = [df_solar.iloc[idx][f"{h}時"] * 1000 for idx, h in idx_map]

        # 太陽位置 & clearsky
        solpos = site.get_solarposition(times)
        clearsky = site.get_clearsky(times, model="simplified_solis")
        dhi = clearsky["dhi"].values
        dni = clearsky["dni"].values

        # POA面照度計算
        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=dni,
            ghi=ghi_flat,
            dhi=dhi,
            solar_zenith=solpos["zenith"].values,
            solar_azimuth=solpos["azimuth"].values,
            model='isotropic'
        )
        poa_kwh = poa['poa_global'] / 1000

        # df_solar 値置換
        flat_idx = 0
        for idx, row in df_solar.iterrows():
            for h in range(1, 25):
                df_solar.at[idx, f"{h}時"] = poa_kwh.iloc[flat_idx]
                flat_idx += 1

        # --- 発電量計算 ---
        df_hourly = df_solar[["月", "日"] + time_labels].copy()
        for h in time_labels:
            df_hourly[h] = (
                K * effective_PAS * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

        # --- 月別積分 & PCSクリップ ---
        eph_monthly = df_hourly.groupby("月")[time_labels].sum().reset_index().melt(
            id_vars=["月"], value_name="発電量 [kWh]"
        )
        for h in time_labels:
            df_hourly[h] = df_hourly[h].clip(upper=PCS_output_kw)
        eph_monthly = df_hourly.groupby("月")[time_labels].sum().reset_index().melt(
            id_vars=["月"], value_name="発電量 [kWh]"
        )

        # --- グラフ描画 ---
        fig_bar = px.bar(eph_monthly, x="月", y="発電量 [kWh]",
                         title="月別発電量（物理ベース傾斜・方位補正＋PCS制限後）")
        df_day = df_hourly[(df_hourly["月"]==month_selected)&(df_hourly["日"]==day_selected)]
        if df_day.empty:
            fig_line = px.line(title="該当データなし")
        else:
            hourly = df_day[time_labels].iloc[0]
            df_plot = pd.DataFrame({"時刻": list(range(1,25)), "発電量 [kWh]": hourly.values})
            fig_line = px.line(df_plot, x="時刻", y="発電量 [kWh]", markers=True,
                               title=f"{month_selected}月{day_selected}日の24h発電量").update_layout(xaxis=dict(dtick=1))

        annual_total = df_hourly[time_labels].sum(axis=1).sum()
        annual_str = f"年間発電量: {annual_total:.2f} kWh"

        return fig_bar, fig_line, annual_str, ""

    except Exception as e:
        # 例外時はエラー文字列のみ返す
        return None, None, None, f"内部エラー: {e}"

# ─────────────── Gradio UI 定義 ───────────────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（Gradio 版）")
    gr.Markdown(
        """
        CSVアップロード → 各種パラメータ (K, PAS/Ppeak, GS, α, ΔT, 方位, 傾斜角, PCS容量) → 月/日 → 計算
        """
    )
    with gr.Row():
        with gr.Column(scale=2):
            file_input       = gr.File(label="NEDO形式CSV", file_types=[".csv"])
            K_input          = gr.Number(label="K（係数）", value=0.95)
            PAS_input        = gr.Number(label="PAS（受光面積 m²）", value=None)
            Ppeak_input      = gr.Number(label="Ppeak（定格出力 kWₚ）", value=None)
            GS_input         = gr.Number(label="GS（基準日射量）", value=1.0)
            alpha_input      = gr.Number(label="αpmax（[%/℃]）", value=-0.35)
            deltaT_input     = gr.Number(label="ΔT (℃)", value=25.0)
            orientation_input= gr.Dropdown(label="方位",
                                           choices=["北","東","西","南東","南西","南"],
                                           value="南")
            tilt_input       = gr.Dropdown(label="傾斜角 (°)", choices=[str(i) for i in range(0,91,10)], value="30")
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
            file_input, K_input, PAS_input, Ppeak_input,
            GS_input, alpha_input, deltaT_input,
            orientation_input, tilt_input,
            month_input, day_input, PCS_input
        ],
        outputs=[bar_plot, line_plot, annual_box, error_box]
    )

if __name__ == "__main__":
    demo.launch()
