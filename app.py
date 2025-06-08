import os
import sqlite3
import pandas as pd
import gradio as gr
import plotly.express as px
import pvlib
import numpy as np

# 定数
G_STC = 1.0
DEFAULT_PCS_OUTPUT = 99.0

# DBパス
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "radiation.db")

# 方位マッピング
ORIENTATION_TO_AZIMUTH = {
    "北": 0, "東": 90, "西":270,
    "南東":135, "南西":225, "南":180
}

# 地点一覧取得
def get_station_options():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"DBが見つかりません: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT DISTINCT point_no AS station_no, point_name AS station_name FROM radiation_data",
        conn
    )
    conn.close()
    return [f"{r.station_no}_{r.station_name}" for r in df.itertuples()]

# データ読み込み
def load_data(no):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT element_no, month, day, hour, value FROM radiation_data WHERE point_no=?",
        conn, params=(no,)
    )
    conn.close()
    return df

# メイン

def process_and_plot(
    station, K, PAS, Ppeak, GS,
    alpha_pct, delta_T, orientation, tilt,
    month_str, day_str, PCS_kw
):
    # 入力チェック
    if not station:
        return None, None, None, "", "地点選択してください"
    try:
        m = int(month_str); d = int(day_str)
        assert 1<=m<=12 and 1<=d<=31
    except:
        return None, None, None, "", "月1-12 日1-31の整数を"

    alpha = alpha_pct/100.0
    # PAS設定
    if Ppeak not in (None,0): PAS_eff = Ppeak/(K*G_STC)
    elif PAS not in (None,0): PAS_eff = PAS
    else: return None,None,None,"","PAS or Ppeak を"
    # PCS
    if PCS_kw in (None,0): PCS_kw = DEFAULT_PCS_OUTPUT

    no = station.split('_')[0]
    # 緯度経度
    conn = sqlite3.connect(DB_PATH)
    df_info = pd.read_sql_query(
        "SELECT point_lat AS lat, point_lon AS lon FROM radiation_data WHERE point_no=? LIMIT 1",
        conn, params=(no,)
    )
    conn.close()
    if df_info.empty:
        return None,None,None,"",f"地点情報なし {no}"
    lat, lon = df_info.iloc[0][['lat','lon']]

    df = load_data(no)
    # pivot
    df_s = df[df.element_no=='00001'].pivot_table(index=['month','day'], columns='hour', values='value').reset_index()
    df_t = df[df.element_no=='00005'].pivot_table(index=['month','day'], columns='hour', values='value').reset_index()
    # 単位変換
    for h in range(1,25):
        df_s[h] = pd.to_numeric(df_s[h],errors='coerce')*0.01*277.778
        df_t[h] = pd.to_numeric(df_t[h],errors='coerce')*0.1
    df_s.fillna(0,inplace=True); df_t.fillna(0,inplace=True)

    # GHI flat
    ghi_flat = df_s[list(range(1,25))].values.flatten()
    ghi_sum  = ghi_flat.sum()

    # 日別POA計算
    times=[]
    for _,r in df_s.iterrows():
        for h in range(1,25):
            times.append(pd.Timestamp(year=2020,month=int(r.month),day=int(r.day),hour=h-1,tz='Asia/Tokyo'))
    times = pd.DatetimeIndex(times)
    site = pvlib.location.Location(lat,lon,tz='Asia/Tokyo')
    sol = site.get_solarposition(times)
    cs  = site.get_clearsky(times,model='simplified_solis')
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tilt, surface_azimuth=ORIENTATION_TO_AZIMUTH[orientation],
        dni=np.asarray(cs.dni), ghi=ghi_flat, dhi=np.asarray(cs.dhi),
        solar_zenith=np.asarray(sol.zenith), solar_azimuth=np.asarray(sol.azimuth),
        model='isotropic'
    )
    poa_flat = np.asarray(poa.poa_global).reshape(len(df_s),24)

    # 温度補正係数
    corr = 1+alpha*(df_t[list(range(1,25))]+delta_T)
    c_avg = corr.values.mean(); c_max = corr.values.max()
    # 年間発電量簡易
    energy_simple = (K*PAS_eff*ghi_flat*(1+alpha*(df_t[list(range(1,25))].values.flatten()+delta_T))/GS).sum()/1000.0

    # 形状のみ用いる
    df_h = pd.DataFrame(poa_flat,columns=list(range(1,25)))
    for h in range(1,25):
        df_h[h] = (K*PAS_eff*df_h[h]*corr[h]/GS)
    df_h['month']=df_s['month']; df_h['day']=df_s['day']
    df_h['daily']=df_h[list(range(1,25))].sum(axis=1)

    # 月別
    eph = df_h.groupby('month')['daily'].sum().reset_index()
    fig_bar = px.bar(eph,x='month',y='daily',labels={'month':'月','daily':'月別発電量 [kWh]'},title='月別発電量')

    # 日別
    df_d = df_h[(df_h.month==m)&(df_h.day==d)]
    if df_d.empty:
        fig_line=px.line(title='該当データなし')
    else:
        df_pl = pd.DataFrame({'時刻':list(range(1,25)),'発電量 [kWh]':df_d[list(range(1,25))].iloc[0]})
        fig_line=px.line(df_pl,x='時刻',y='発電量 [kWh]',markers=True,labels={'時刻':'時刻'},title=f'{m}月{d}日24h発電量')
        fig_line.update_yaxes(title_text='発電量 [kWh]')

    return fig_bar,fig_line,f'年間発電量: {energy_simple:.2f} kWh',f'ghi_sum={ghi_sum:.2f}, simple={energy_simple:.2f}, c_avg={c_avg:.3f}, c_max={c_max:.3f}',""

# UI
with gr.Blocks() as demo:
    gr.Markdown('## NEDO 日射量シミュレーション (SQL版)')
    with gr.Row():
        c1,c2=gr.Column(scale=2),gr.Column(scale=3)
        with c1:
            st=gr.Dropdown(label='地点',choices=get_station_options())
            Kf,PASf,Pp=gr.Number(value=0.95,label='K'),gr.Number(value=None,label='PAS'),gr.Number(value=None,label='Ppeak')
            GSf,aT,dT=gr.Number(value=1.0,label='GS'),gr.Number(value=-0.35,label='α[%/℃]'),gr.Number(value=25.0,label='ΔT[℃]')
            ori,til=gr.Dropdown(label='方位',choices=list(ORIENTATION_TO_AZIMUTH.keys()),value='南'),gr.Dropdown(label='傾斜',choices=[str(i) for i in range(0,91,10)],value='0')
            mm,dd=gr.Textbox(label='月'),gr.Textbox(label='日')
            pcs=gr.Number(value=99,label='PCS[kW]')
            btn=gr.Button('▶ 計算')
            out1=gr.Textbox(label='年間発電量',interactive=False); out2=gr.Textbox(label='DEBUG',interactive=False)
        with c2:
            pbar=gr.Plot(label='月別発電量'); pline=gr.Plot(label='24h発電量')
    btn.click(process_and_plot,inputs=[st,Kf,PASf,Pp,GSf,aT,dT,ori,til,mm,dd,pcs],outputs=[pbar,pline,out1,out2])
    demo.launch()
