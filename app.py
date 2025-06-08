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
    """
    - uploaded_file: NEDO txt（日別スペース区切り）
    - K, PAS/Ppeak, GS, α, ΔT, 方位, 傾斜, 月・日, PCS出力上限
    """
    try:
        # --- 入力チェック ---
        if uploaded_file is None:
            return None, None, None, "エラー：ファイルがアップロードされていません。"
        # 月日
        try:
            month_sel = int(month_str)
            day_sel   = int(day_str)
            if not (1 <= month_sel <= 12 and 1 <= day_sel <= 31):
                raise
        except:
            return None, None, None, "エラー：月は1–12、日は1–31の整数で入力してください。"
        # α
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

        # --- TXT読み込み & 緯度経度取得 ---
        with open(uploaded_file.name, "r", encoding="shift_jis") as f:
            lines = [l.strip() for l in f if l.strip()]
        # 1行目: 地点情報
        hdr = lines[0].split()
        lat = float(hdr[2]) + float(hdr[3]) / 60.0
        lon = float(hdr[4]) + float(hdr[5]) / 60.0

        # --- 要素00001（全天日射量）と00005（気温）をパース ---
        solar_recs = []
        temp_recs  = []
        for L in lines[1:]:
            parts = L.split()
            if len(parts) < 4 + 24:
                continue
            elem = parts[0]
            if elem not in ("00001", "00005"):
                continue
            mon = int(parts[1])
            day = int(parts[2])
            # parts[3] は代表年（今回は捨てる）
            hours = parts[4:4+24]
            # 数値変換＆単位補正
            if elem == "00001":
                # 0.01 MJ/m2 → kWh/m2 : val*0.01/3.6
                ghi = [
                    0.0 if h in ("8888","----") else float(h) * 0.01 / 3.6
                    for h in hours
                ]
                rec = {"月": mon, "日": day}
                rec.update({f"{h}時": ghi[h-1] for h in range(1,25)})
                solar_recs.append(rec)
            else:  # elem == "00005"
                # 0.1 ℃
                tmp = [
                    0.0 if h in ("8888","----") else float(h) * 0.1
                    for h in hours
                ]
                rec = {"月": mon, "日": day}
                rec.update({f"{h}時": tmp[h-1] for h in range(1,25)})
                temp_recs.append(rec)

        # DataFrame 化
        time_labels = [f"{h}時" for h in range(1,25)]
        df_solar = pd.DataFrame(solar_recs).sort_values(["月","日"]).reset_index(drop=True)
        df_temp  = pd.DataFrame(temp_recs ).sort_values(["月","日"]).reset_index(drop=True)

        # --- pvlib で傾斜・方位補正 ---
        # 日付時刻インデックス（ダミー年=2020）
        tz = "Asia/Tokyo"
        times = []
        for _, row in df_solar.iterrows():
            for h in range(1,25):
                times.append(pd.Timestamp(2020, int(row["月"]), int(row["日"]), h-1, tz=tz))
        times = pd.DatetimeIndex(times)
        # GHI フラット
        ghi_flat = [ df_solar.iloc[i//24][f"{(i%24)+1}時"] * 1000 for i in range(len(times)) ]

        # pvlib 設定
        surface_tilt    = float(tilt)
        surface_azimuth = ORIENTATION_TO_AZIMUTH.get(orientation, 180)
        site = pvlib.location.Location(lat, lon, tz=tz)

        solpos   = site.get_solarposition(times)
        clearsky = site.get_clearsky(times, model="simplified_solis")
        dni = clearsky["dni"].values
        dhi = clearsky["dhi"].values

        poa = pvlib.irradiance.get_total_irradiance(
            surface_tilt=surface_tilt,
            surface_azimuth=surface_azimuth,
            dni=dni, ghi=ghi_flat, dhi=dhi,
            solar_zenith=solpos["zenith"].values,
            solar_azimuth=solpos["azimuth"].values,
            model="isotropic"
        )
        # kWh/m2
        poa_kwh = poa["poa_global"] / 1000.0

        # 戻し
        poa_mat = poa_kwh.values.reshape(len(df_solar), 24)
        for idx in range(len(df_solar)):
            for h in range(1,25):
                df_solar.at[idx, f"{h}時"] = poa_mat[idx, h-1]

        # --- 発電量計算（JIS式＋PCS制限）---
        df_hourly = df_solar[["月","日"] + time_labels].copy()
        for h in time_labels:
            df_hourly[h] = (
                K
                * effective_PAS
                * df_solar[h]
                * (1 + alpha * (df_temp[h] + delta_T))
                / GS
            )
        # 日発電量
        df_hourly["日発電量 [kWh]"] = df_hourly[time_labels].sum(axis=1)

        # 月別積算（PCS制限前）
        eph_monthly = (
            df_hourly.groupby("月")["日発電量 [kWh]"]
            .sum().reset_index().rename(columns={"日発電量 [kWh]":"発電量 [kWh]"})
        )
        # PCS制限（時間ごと）
        for h in time_labels:
            df_hourly[h] = df_hourly[h].clip(upper=PCS_output_kw)
        # 月別再計算
        eph_monthly = (
            df_hourly.groupby("月")[time_labels]
            .sum().reset_index()
            .melt(id_vars=["月"], value_name="発電量 [kWh]")
        )

        # グラフ描画
        fig_bar = px.bar(
            eph_monthly, x="月", y="発電量 [kWh]",
            title="月別発電量（物理ベース補正＋PCS制限後）"
        )
        df_day = df_hourly[
            (df_hourly["月"]==month_sel)&(df_hourly["日"]==day_sel)
        ]
        if df_day.empty:
            fig_line = px.line(title="該当データなし")
        else:
            hourly = df_day[time_labels].iloc[0]
            df_plot = pd.DataFrame({
                "時刻": list(range(1,25)),
                "発電量 [kWh]": hourly.values
            })
            fig_line = px.line(
                df_plot, x="時刻", y="発電量 [kWh]",
                markers=True,
                title=f"{month_sel}月{day_sel}日の24h発電量"
            ).update_layout(xaxis=dict(dtick=1))

        # 年間合計
        annual_total = df_hourly[time_labels].sum().sum()
        annual_str   = f"年間発電量: {annual_total:.2f} kWh"

        return fig_bar, fig_line, annual_str, ""

    except Exception as e:
        return None, None, None, f"内部エラー: {e}"

# ───── Gradio UI ─────
with gr.Blocks() as demo:
    gr.Markdown("# NEDO 日射量シミュレーション（txt版）")
    with gr.Row():
        with gr.Column(scale=2):
            file_input        = gr.File(label="NEDO形式TXT", file_types=[".txt"])
            K_input           = gr.Number(label="K（係数）", value=0.95)
            PAS_input         = gr.Number(label="PAS（m²）", value=None)
            Ppeak_input       = gr.Number(label="Ppeak（kWₚ）", value=None)
            GS_input          = gr.Number(label="GS", value=1.0)
            alpha_input       = gr.Number(label="α[%/℃]", value=-0.35)
            deltaT_input      = gr.Number(label="ΔT[℃]", value=25.0)
            orientation_input = gr.Dropdown(
                label="方位", choices=list(ORIENTATION_TO_AZIMUTH.keys()), value="南"
            )
            tilt_input        = gr.Dropdown(
                label="傾斜角(°)", choices=[str(i) for i in range(0,91,10)], value="30"
            )
            PCS_input         = gr.Number(label="PCS出力[kW]", value=99)
            month_input       = gr.Textbox(label="月(1–12)", placeholder="例:6")
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
