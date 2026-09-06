"""
mcp_tools.py — pv-sim-biz MCPツール群（Phase 4d）
==================================================
Gradio `mcp_server=True` + `gr.api()` 経由でAIエージェント（Claude等）に公開する
API関数を定義する。計算ロジック本体は app.py の関数をimportして使用し、
本モジュールは「型付きパラメータの受け口＋検証＋構造化JSONの組み立て」に徹する。

pv-sim-fip / pv-sim-gh で確立したパターンを踏襲:
  - パラメータ名に単位を含める（例: battery_capacity_kwh）
  - validate → ユーザー確認 → simulate の2段階プロトコル
  - 全simulate結果に assumptions（入力エコー）と caveats（免責）を同梱

スコープ（Phase 4d-1、MCP_HANDOFF.md の段階的実装方針に従う）:
  対象 — 単体施設・複数施設合算、PV発電、高圧/特別高圧料金、蓄電池
        （ルールベース or LP最適化）、投資回収、事業モデル（自己所有/リース/PPA）、CO2削減量
  未対応（後続フェーズで追加予定） — マイクログリッド、両面パネル、
        最適容量探索（2〜3分かかるため）、カスタム需要CSVアップロード
"""

import os
import sqlite3

import numpy as np

# NOTE: 計算ロジック本体（app.py）は _get_app() で遅延解決する。
# fip/ghと同じ理由（HF Spacesは `python app.py` 起動のため app が "__main__" 名になり、
# 関数内で素朴に `import app` すると app.py が二重実行されて build_ui() が再度走り、
# Gradioがクラッシュする）。既にロード済みのモジュールを探して再利用するのが唯一安全な方法。


def _get_app():
    """計算ロジック本体（app.pyモジュール）を返す。二重import・二重build_uiを防ぐ。"""
    import sys
    for name in ("app", "__main__"):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "optimize_battery"):
            return mod
    import app
    return app


# ============================================================
# 定数
# ============================================================

# 入力上限（無料CPUでの巨大LP実行・DB探索の資源枯渇防止）
MAX_PPEAK_KW = 100_000.0
MAX_PCS_KW = 50_000.0
MAX_FACES = 8
MAX_FACILITIES = 6
MAX_BATTERY_KWH = 100_000.0
MAX_PCS_BATTERY_KW = 50_000.0
MAX_FLOOR_AREA_M2 = 1_000_000.0

# MCPエージェント向けの簡易キー → app.py内部の日本語ラベル
BUILDING_TYPE_MAP = {
    "office": "役所・自治体庁舎",
    "primary_school": "公立小学校",
    "secondary_school": "公立中学校・高校",
    "hospital": "公立病院",
    "hotel": "ホテル",
    "retail": "コンビニ・小売店",
}
CONTRACT_TYPE_MAP = {
    "high_voltage": "高圧",
    "extra_high_voltage": "特別高圧",
}
SELL_MODE_MAP = {
    "surplus_export": "余剰売電",
    "no_export": "逆潮流禁止（売電なし）",
}
SELL_SCHEME_MAP = {
    "fit": "FIT利用あり",
    "no_fit": "FIT利用なし",
}
BATTERY_MODE_MAP = {
    "rule_based": "ルールベース",
    "lp_optimized": "最適充放電（LP）",
}
BUSINESS_MODEL_MAP = {
    "self_owned": "自己所有",
    "lease": "リース",
    "ppa": "PPA",
}

_COMMON_CAVEATS = [
    "本結果は投資判断の参考情報であり、収益・投資回収年数を保証するものではありません",
    "本ツールはマイクログリッド事業（網内売電・P-IRR）、両面パネル、最適容量探索"
    "（2〜3分かかる蓄電池容量のグリッドサーチ）には未対応です。これらはGradio UI側でのみ利用できます",
    "電気料金・FIT単価等のデフォルト値は東京電力EPの公表値を参考にした一例であり、"
    "契約中の電力会社・料金メニューにより実際の単価は異なります",
]


# ============================================================
# 内部ヘルパー
# ============================================================

def _resolve_station(station_no: str):
    """地点番号から (lat, lon, ghi_df, temp_df, station_name) を返す。"""
    app = _get_app()
    conn = sqlite3.connect(app.DB_PATH)
    row = conn.execute(
        "SELECT point_name FROM points WHERE point_no = ?", (str(station_no),)
    ).fetchone()
    conn.close()
    if row is None:
        raise ValueError(
            f"地点番号 {station_no} が見つかりません。list_stations で一覧を確認してください。"
        )
    lat, lon, ghi_df, temp_df = app.load_from_db(str(station_no))
    return lat, lon, ghi_df, temp_df, row[0]


def _normalize_faces(faces):
    """faces（list[dict]）を検証し、calculate_generation用のface dictリストに変換する。

    Returns:
        (normalized_faces, errors)
    """
    errors = []
    normalized = []
    if not faces:
        errors.append("faces を1つ以上指定してください（例: "
                       '[{"ppeak_kw": 500.0, "tilt_deg": 30, "azimuth_deg": 180, "pcs_limit_kw": 500}]）')
        return normalized, errors
    if len(faces) > MAX_FACES:
        errors.append(f"faces は最大{MAX_FACES}面までです")
        return normalized, errors

    for i, f in enumerate(faces):
        if not isinstance(f, dict):
            errors.append(f"faces[{i}] はオブジェクト（辞書）で指定してください")
            continue
        ppeak = f.get("ppeak_kw")
        if ppeak is None or not (0 < ppeak <= MAX_PPEAK_KW):
            errors.append(f"faces[{i}].ppeak_kw は 0 < x <= {MAX_PPEAK_KW:.0f} で指定してください")
            continue
        tilt = f.get("tilt_deg", 30.0)
        if not (0 <= tilt <= 90):
            errors.append(f"faces[{i}].tilt_deg は 0〜90 で指定してください")
            continue
        azimuth = float(f.get("azimuth_deg", 180.0)) % 360
        pcs = f.get("pcs_limit_kw")
        if pcs is not None and not (0 <= pcs <= MAX_PCS_KW):
            errors.append(f"faces[{i}].pcs_limit_kw は 0〜{MAX_PCS_KW:.0f} で指定してください")
            continue
        normalized.append({
            "ppeak_kw": float(ppeak),
            "tilt_deg": float(tilt),
            "azimuth_deg": float(azimuth),
            "pcs_limit_kw": float(pcs) if pcs and pcs > 0 else None,
        })
    return normalized, errors


def _faces_to_app_format(faces):
    """正規化済みfacesをapp.calculate_generation用の形式に変換する。"""
    return [{
        "ppeak": f["ppeak_kw"],
        "orientation": "南",  # azimuth直接指定が優先されるためプレースホルダ
        "azimuth": f["azimuth_deg"],
        "tilt": f["tilt_deg"],
        "pcs_limit_kw": f["pcs_limit_kw"],
    } for f in faces]


def _normalize_facilities(facilities):
    """facilities（list[dict]）を検証し、正規化済みリストを返す。

    Returns:
        (normalized_facilities, errors)
    """
    errors = []
    normalized = []
    if not facilities:
        errors.append(
            "facilities を1つ以上指定してください（例: "
            '[{"building_type": "office", "floor_area_m2": 4500, "building_count": 1}]）。'
            "蓄電池・電気料金試算には需要データが必須です"
        )
        return normalized, errors
    if len(facilities) > MAX_FACILITIES:
        errors.append(f"facilities は最大{MAX_FACILITIES}件までです")
        return normalized, errors

    for i, fac in enumerate(facilities):
        if not isinstance(fac, dict):
            errors.append(f"facilities[{i}] はオブジェクト（辞書）で指定してください")
            continue
        btype_key = fac.get("building_type")
        if btype_key not in BUILDING_TYPE_MAP:
            errors.append(
                f"facilities[{i}].building_type は {list(BUILDING_TYPE_MAP.keys())} から選択してください"
            )
            continue
        area = fac.get("floor_area_m2")
        if area is None or not (0 < area <= MAX_FLOOR_AREA_M2):
            errors.append(f"facilities[{i}].floor_area_m2 は 0 < x <= {MAX_FLOOR_AREA_M2:.0f} で指定してください")
            continue
        count = fac.get("building_count", 1)
        if not (isinstance(count, (int, float)) and count >= 1):
            errors.append(f"facilities[{i}].building_count は1以上の整数で指定してください")
            continue
        normalized.append({
            "building_type": btype_key,
            "floor_area_m2": float(area),
            "building_count": int(count),
        })
    return normalized, errors


def _facilities_to_facility_args(facilities):
    """正規化済みfacilitiesをapp.load_combined_demand用のfacility_argsタプルに変換する。"""
    app = _get_app()
    args = []
    for fac in facilities:
        args.extend([
            BUILDING_TYPE_MAP[fac["building_type"]],
            fac["floor_area_m2"],
            fac["building_count"],
        ])
    # MAX_FACILITIES件になるまで「なし」で埋める（app.load_combined_demandの前提に合わせる）
    while len(args) < app.MAX_FACILITIES * 3:
        args.extend(["なし", 0, 0])
    return tuple(args)


# ============================================================
# パラメータ検証
# ============================================================

def _normalize_and_validate(
    station_no,
    faces,
    facilities,
    contract_type,
    basic_charge_yen_per_kw,
    energy_charge_summer_yen_per_kwh,
    energy_charge_other_yen_per_kwh,
    power_factor_pct,
    fuel_adjustment_yen_per_kwh,
    renewable_surcharge_yen_per_kwh,
    sell_mode,
    sell_scheme,
    fit_elapsed_years,
    sell_price_yen_per_kwh,
    battery_enabled,
    battery_mode,
    battery_capacity_kwh,
    battery_efficiency_pct,
    battery_max_charge_kw,
    battery_max_discharge_kw,
    battery_soc_min_pct,
    battery_soc_max_pct,
    pv_cost_yen_per_kw,
    battery_cost_yen_per_kwh,
    substation_cost_yen_per_kva,
    subsidy_enabled,
    subsidy_pv_pct,
    subsidy_bat_pct,
    co2_factor_t_per_kwh,
    business_model,
    contract_years,
    target_irr_pct,
):
    """パラメータを正規化し (params, warnings, errors) を返す。重い計算は実行しない。"""
    app = _get_app()
    errors = []
    warnings = []

    station_name = None
    try:
        conn = sqlite3.connect(app.DB_PATH)
        row = conn.execute(
            "SELECT point_name FROM points WHERE point_no = ?", (str(station_no),)
        ).fetchone()
        conn.close()
        if row is None:
            errors.append(f"地点番号 {station_no} がDBに存在しません（list_stations 参照）")
        else:
            station_name = row[0]
    except Exception as e:
        errors.append(f"地点DB照会エラー: {e}")

    normalized_faces, face_errors = _normalize_faces(faces)
    errors.extend(face_errors)

    normalized_facilities, fac_errors = _normalize_facilities(facilities)
    errors.extend(fac_errors)

    if contract_type not in CONTRACT_TYPE_MAP:
        errors.append(f"contract_type は {list(CONTRACT_TYPE_MAP.keys())} から選択してください")
    if sell_mode not in SELL_MODE_MAP:
        errors.append(f"sell_mode は {list(SELL_MODE_MAP.keys())} から選択してください")
    if sell_scheme not in SELL_SCHEME_MAP:
        errors.append(f"sell_scheme は {list(SELL_SCHEME_MAP.keys())} から選択してください")
    if business_model not in BUSINESS_MODEL_MAP:
        errors.append(f"business_model は {list(BUSINESS_MODEL_MAP.keys())} から選択してください")

    if battery_enabled:
        if battery_mode not in BATTERY_MODE_MAP:
            errors.append(f"battery_mode は {list(BATTERY_MODE_MAP.keys())} から選択してください")
        if not (0 < battery_capacity_kwh <= MAX_BATTERY_KWH):
            errors.append(f"battery_capacity_kwh は 0 < x <= {MAX_BATTERY_KWH:.0f} で指定してください")
        if not (0 < battery_max_charge_kw <= MAX_PCS_BATTERY_KW):
            errors.append(f"battery_max_charge_kw は 0 < x <= {MAX_PCS_BATTERY_KW:.0f} で指定してください")
        if not (0 < battery_max_discharge_kw <= MAX_PCS_BATTERY_KW):
            errors.append(f"battery_max_discharge_kw は 0 < x <= {MAX_PCS_BATTERY_KW:.0f} で指定してください")
        if not (50 <= battery_efficiency_pct <= 100):
            errors.append("battery_efficiency_pct は 50〜100 で指定してください")
        if not (0 <= battery_soc_min_pct < battery_soc_max_pct <= 100):
            errors.append("SOC範囲が不正です（0 <= soc_min < soc_max <= 100）")

    if power_factor_pct is not None and not (0 <= power_factor_pct <= 100):
        errors.append("power_factor_pct は 0〜100 で指定してください")
    if sell_scheme == "fit" and not (1 <= fit_elapsed_years <= 30):
        errors.append("fit_elapsed_years は 1〜30 で指定してください")
    if sell_scheme == "no_fit" and sell_price_yen_per_kwh < 0:
        errors.append("sell_price_yen_per_kwh は0以上で指定してください")
    if pv_cost_yen_per_kw < 0 or battery_cost_yen_per_kwh < 0:
        errors.append("pv_cost_yen_per_kw / battery_cost_yen_per_kwh は0以上で指定してください")
    if subsidy_enabled:
        if not (0 <= subsidy_pv_pct <= 100):
            errors.append("subsidy_pv_pct は 0〜100 で指定してください")
        if not (0 <= subsidy_bat_pct <= 100):
            errors.append("subsidy_bat_pct は 0〜100 で指定してください")
    if business_model in ("lease", "ppa"):
        if not (1 <= int(contract_years) <= 40):
            errors.append("contract_years は 1〜40 で指定してください")
        if not (0 <= target_irr_pct <= 50):
            errors.append("target_irr_pct は 0〜50 で指定してください")

    # --- 警告 ---
    if battery_enabled and battery_capacity_kwh > 0 and battery_max_charge_kw > battery_capacity_kwh:
        warnings.append(
            f"充電レート {battery_max_charge_kw:.0f}kW が容量 {battery_capacity_kwh:.0f}kWh を超えています"
            "（1C超の高速蓄電池想定になっています）"
        )
    if len(normalized_facilities) == 1 and business_model != "self_owned":
        pass  # 単一施設でもリース/PPAは成立するため警告不要
    if sell_mode == "no_export" and sell_scheme == "fit":
        warnings.append(
            "逆潮流禁止（売電なし）を選択しているため、FIT単価は投資回収計算に反映されません"
            "（売電収入がゼロになります）"
        )

    # FIT/売電単価の解決（UIのon_sell_scheme_changeロジックを踏襲）
    if sell_scheme == "fit":
        year = int(fit_elapsed_years)
        if year <= 5:
            resolved_sell_price = app.FIT_PRICE_EARLY
        elif year <= 20:
            resolved_sell_price = app.FIT_PRICE_LATE
        else:
            resolved_sell_price = app.FIT_PRICE_POST
    else:
        resolved_sell_price = float(sell_price_yen_per_kwh)

    contract_type_label = CONTRACT_TYPE_MAP[contract_type]
    defaults = app.ELECTRICITY_RATE_EHV if contract_type_label == "特別高圧" else app.ELECTRICITY_RATE_HV

    params = {
        "station_no": str(station_no),
        "station_name": station_name,
        "faces": normalized_faces,
        "facilities": normalized_facilities,
        "contract_type": contract_type,
        "basic_charge_yen_per_kw": float(basic_charge_yen_per_kw) if basic_charge_yen_per_kw is not None else defaults["basic_charge_per_kw"],
        "energy_charge_summer_yen_per_kwh": float(energy_charge_summer_yen_per_kwh) if energy_charge_summer_yen_per_kwh is not None else defaults["energy_charge_summer"],
        "energy_charge_other_yen_per_kwh": float(energy_charge_other_yen_per_kwh) if energy_charge_other_yen_per_kwh is not None else defaults["energy_charge_other"],
        "power_factor_pct": float(power_factor_pct) if power_factor_pct is not None else defaults["power_factor_pct"],
        "fuel_adjustment_yen_per_kwh": float(fuel_adjustment_yen_per_kwh) if fuel_adjustment_yen_per_kwh is not None else defaults["fuel_adjustment"],
        "renewable_surcharge_yen_per_kwh": float(renewable_surcharge_yen_per_kwh) if renewable_surcharge_yen_per_kwh is not None else defaults["renewable_surcharge"],
        "sell_mode": sell_mode,
        "sell_scheme": sell_scheme,
        "fit_elapsed_years": int(fit_elapsed_years) if sell_scheme == "fit" else None,
        "sell_price_yen_per_kwh": round(resolved_sell_price, 2),
        "battery_enabled": bool(battery_enabled),
        "battery_mode": battery_mode if battery_enabled else None,
        "battery_capacity_kwh": float(battery_capacity_kwh) if battery_enabled else 0.0,
        "battery_efficiency_pct": float(battery_efficiency_pct),
        "battery_max_charge_kw": float(battery_max_charge_kw),
        "battery_max_discharge_kw": float(battery_max_discharge_kw),
        "battery_soc_min_pct": float(battery_soc_min_pct),
        "battery_soc_max_pct": float(battery_soc_max_pct),
        "pv_cost_yen_per_kw": float(pv_cost_yen_per_kw),
        "battery_cost_yen_per_kwh": float(battery_cost_yen_per_kwh),
        "substation_cost_yen_per_kva": float(substation_cost_yen_per_kva),
        "subsidy_enabled": bool(subsidy_enabled),
        "subsidy_pv_pct": float(subsidy_pv_pct) if subsidy_enabled else 0.0,
        "subsidy_bat_pct": float(subsidy_bat_pct) if subsidy_enabled else 0.0,
        "co2_factor_t_per_kwh": float(co2_factor_t_per_kwh),
        "business_model": business_model,
        "contract_years": int(contract_years) if business_model in ("lease", "ppa") else None,
        "target_irr_pct": float(target_irr_pct) if business_model in ("lease", "ppa") else None,
    }
    return params, warnings, errors


# ============================================================
# シミュレーション本体
# ============================================================

def _run_industrial_simulation(p: dict):
    """検証済みパラメータ p でシミュレーションを実行し、構造化dictを返す。"""
    app = _get_app()
    lat, lon, ghi_df, temp_df, _ = _resolve_station(p["station_no"])

    faces_app = _faces_to_app_format(p["faces"])
    result = app.calculate_generation(
        lat, lon, ghi_df, temp_df, faces_app,
        app.DEFAULT_KHD, app.DEFAULT_KPD, app.DEFAULT_KPM,
        app.DEFAULT_KPA, app.DEFAULT_ETA_INO,
        app.DEFAULT_ALPHA, app.DEFAULT_DELTA_T,
    )
    gen = result["total_gen_clipped"]
    month_day = result["month_day"]

    facility_args = _facilities_to_facility_args(p["facilities"])
    demand_30min, individual_demands = app.load_combined_demand(
        facility_args, len(p["facilities"]),
    )

    no_export = (p["sell_mode"] == "no_export")
    battery_active = p["battery_enabled"] and p["battery_capacity_kwh"] > 0

    sc_result = None
    if battery_active:
        if p["battery_mode"] == "lp_optimized":
            sc_result = app.optimize_battery(
                gen, demand_30min, month_day,
                capacity_kwh=p["battery_capacity_kwh"],
                efficiency_pct=p["battery_efficiency_pct"],
                max_charge_kw=p["battery_max_charge_kw"],
                max_discharge_kw=p["battery_max_discharge_kw"],
                soc_min_pct=p["battery_soc_min_pct"],
                soc_max_pct=p["battery_soc_max_pct"],
                basic_charge_per_kw=p["basic_charge_yen_per_kw"],
                energy_charge_summer=p["energy_charge_summer_yen_per_kwh"],
                energy_charge_other=p["energy_charge_other_yen_per_kwh"],
                power_factor_pct=p["power_factor_pct"],
                fuel_adjustment=p["fuel_adjustment_yen_per_kwh"],
                renewable_surcharge=p["renewable_surcharge_yen_per_kwh"],
                sell_price=p["sell_price_yen_per_kwh"],
                no_export=no_export,
            )
        else:
            sc_result = app.simulate_battery(
                gen, demand_30min, month_day,
                capacity_kwh=p["battery_capacity_kwh"],
                efficiency_pct=p["battery_efficiency_pct"],
                max_charge_kw=p["battery_max_charge_kw"],
                max_discharge_kw=p["battery_max_discharge_kw"],
                soc_min_pct=p["battery_soc_min_pct"],
                soc_max_pct=p["battery_soc_max_pct"],
                no_export=no_export,
            )
    else:
        sc_result = app.calculate_self_consumption(gen, demand_30min, month_day, no_export=no_export)

    rate_params = dict(
        basic_charge_per_kw=p["basic_charge_yen_per_kw"],
        energy_charge_summer=p["energy_charge_summer_yen_per_kwh"],
        energy_charge_other=p["energy_charge_other_yen_per_kwh"],
        power_factor_pct=p["power_factor_pct"],
        fuel_adjustment=p["fuel_adjustment_yen_per_kwh"],
        renewable_surcharge=p["renewable_surcharge_yen_per_kwh"],
    )
    cost_before = app.calc_electricity_cost(demand_30min, month_day, **rate_params)
    cost_after = app.calc_electricity_cost(sc_result["import_"], month_day, **rate_params)

    # --- 投資額・補助金（app.run_simulationと同じ計算式） ---
    total_ppeak = sum(f["ppeak_kw"] for f in p["faces"])
    pv_investment = total_ppeak * p["pv_cost_yen_per_kw"]
    bat_investment = p["battery_capacity_kwh"] * p["battery_cost_yen_per_kwh"] if battery_active else 0.0
    total_investment = pv_investment + bat_investment

    is_ehv = (p["contract_type"] == "extra_high_voltage")
    substation_cost = 0.0
    if is_ehv and cost_before["contract_power_kw"] <= 2000:
        substation_cost = cost_before["contract_power_kw"] * p["substation_cost_yen_per_kva"]
        total_investment += substation_cost

    subsidy_pv = pv_investment * p["subsidy_pv_pct"] / 100.0 if p["subsidy_enabled"] else 0.0
    subsidy_bat = bat_investment * p["subsidy_bat_pct"] / 100.0 if p["subsidy_enabled"] else 0.0
    total_subsidy = subsidy_pv + subsidy_bat
    net_investment = total_investment - total_subsidy

    # --- 年間経済メリット（app.run_simulationと同じ式） ---
    saving = cost_before["annual_total"] - cost_after["annual_total"]
    sell_revenue = 0.0
    if not no_export:
        sell_revenue = sc_result["annual_export"] * p["sell_price_yen_per_kwh"]
    annual_merit = saving + sell_revenue

    payback_years = (net_investment / annual_merit) if annual_merit > 0 else None

    # --- CO2削減量 ---
    grid_reduction = sc_result["annual_demand"] - sc_result["annual_import"]
    co2_reduction = grid_reduction * p["co2_factor_t_per_kwh"]

    # --- 事業モデル（自己所有/リース/PPA、app.run_simulationのCRF式を踏襲） ---
    business_out = {"business_model": p["business_model"]}
    if p["business_model"] in ("lease", "ppa") and net_investment > 0:
        n_years = p["contract_years"]
        r = p["target_irr_pct"] / 100.0
        crf = r * (1 + r) ** n_years / ((1 + r) ** n_years - 1) if r > 0 else 1.0 / n_years
        annual_lease = net_investment * crf
        business_out["investment_base_yen"] = round(net_investment)
        business_out["contract_years"] = n_years
        business_out["target_irr_pct"] = p["target_irr_pct"]
        if p["business_model"] == "lease":
            business_out["required_lease_yen_per_year"] = round(annual_lease)
            if annual_merit > 0:
                customer_annual = annual_merit - annual_lease
                business_out["customer_annual_benefit_yen"] = round(customer_annual)
                business_out["customer_total_benefit_yen"] = round(customer_annual * n_years)
                business_out["proposal_viable"] = customer_annual >= 0
            else:
                business_out["customer_annual_benefit_yen"] = None
                business_out["proposal_viable"] = False
        else:  # ppa
            if sc_result["annual_self"] > 0:
                ppa_price = annual_lease / sc_result["annual_self"]
                ppa_annual_cost = ppa_price * sc_result["annual_self"]
                customer_annual = annual_merit - ppa_annual_cost
                business_out["required_ppa_price_yen_per_kwh"] = round(ppa_price, 2)
                business_out["annual_self_consumption_kwh"] = round(sc_result["annual_self"])
                business_out["customer_annual_benefit_yen"] = round(customer_annual)
                business_out["customer_total_benefit_yen"] = round(customer_annual * n_years)
                business_out["proposal_viable"] = customer_annual >= 0
            else:
                business_out["required_ppa_price_yen_per_kwh"] = None
                business_out["proposal_viable"] = False
    else:
        business_out["note"] = "自己所有のため事業者側パラメータ（投資ベース・リース料等）は算出していません"

    caveats = list(_COMMON_CAVEATS)
    if p["business_model"] in ("lease", "ppa"):
        caveats.append(
            "リース料/PPA単価は事業者の目標P-IRRから資本回収係数（CRF）で逆算した値であり、"
            "需要家に開示するP-IRRそのものではありません（CLAUDE.mdの設計方針：P-IRRは需要家に非開示）"
        )
    if no_export:
        caveats.append("逆潮流禁止モードのため出力抑制（カーテイルメント）が発生する場合があります")

    return {
        "assumptions": p,
        "annual": {
            "generation_kwh": round(result["annual"]),
            "capacity_factor_pct": round(result["annual"] / (total_ppeak * 8760) * 100, 2) if total_ppeak > 0 else None,
            "face_generation_kwh": [round(v) for v in result["face_annual"]],
            "demand_kwh": round(sc_result["annual_demand"]),
            "self_consumption_kwh": round(sc_result["annual_self"]),
            "self_consumption_rate_pct": round(sc_result["self_consumption_rate"], 1),
            "self_sufficiency_rate_pct": round(sc_result["self_sufficiency_rate"], 1),
            "export_kwh": round(sc_result["annual_export"]) if not no_export else 0,
            "curtailed_kwh": round(sc_result.get("annual_curtailment", 0.0)) if no_export else 0,
            "import_kwh": round(sc_result["annual_import"]),
            "battery_charge_kwh": round(sc_result["annual_charge"]) if battery_active else None,
            "battery_discharge_kwh": round(sc_result["annual_discharge"]) if battery_active else None,
            "co2_reduction_t_per_year": round(co2_reduction, 3),
        },
        "electricity_cost": {
            "contract_power_before_kw": round(cost_before["contract_power_kw"], 1),
            "contract_power_after_kw": round(cost_after["contract_power_kw"], 1),
            "contract_power_reduction_kw": round(cost_before["contract_power_kw"] - cost_after["contract_power_kw"], 1),
            "annual_cost_before_yen": round(cost_before["annual_total"]),
            "annual_cost_after_yen": round(cost_after["annual_total"]),
            "annual_cost_saving_yen": round(saving),
            "annual_sell_revenue_yen": round(sell_revenue),
            "annual_economic_merit_yen": round(annual_merit),
            "contract_power_note": "contract_power_*_kw はLP/ルールベース計算の結果（年間30分デマンド最大値）"
                                   "であり、入力パラメータではありません",
        },
        "investment": {
            "pv_investment_yen": round(pv_investment),
            "battery_investment_yen": round(bat_investment),
            "substation_cost_yen": round(substation_cost),
            "total_investment_yen": round(total_investment),
            "subsidy_total_yen": round(total_subsidy),
            "net_investment_yen": round(net_investment),
            "simple_payback_years": round(payback_years, 1) if payback_years is not None else None,
        },
        "business": business_out,
        "caveats": caveats,
    }


# ============================================================
# 公開MCPツール
# ============================================================

def list_stations() -> dict:
    """気象観測地点（NEDO METPV-20、DB収録の全地点）の一覧を返す。

    シミュレーションの station_no 引数にはここで返る地点番号を使用する。

    Returns:
        dict: {"stations": [{"station_no", "name", "latitude", "longitude"}, ...]}
    """
    app = _get_app()
    conn = sqlite3.connect(app.DB_PATH)
    rows = conn.execute(
        "SELECT point_no, point_name, lat, lon FROM points ORDER BY point_no"
    ).fetchall()
    conn.close()
    return {
        "stations": [
            {"station_no": str(no), "name": name,
             "latitude": float(lat), "longitude": float(lon)}
            for no, name, lat, lon in rows
        ],
        "count": len(rows),
    }


def estimate_pv_generation(
    station_no: str = "44132",
    faces: list = [{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
) -> dict:
    """太陽光発電量のみを試算する（JIS C 8907準拠、需要・電気料金・経済性を含まない、数秒）。

    Args:
        station_no: 地点番号（list_stations で取得。例 "44132"=東京, "82182"=福岡）
        faces: 太陽電池アレイ面のリスト（最大8面）。各要素は
            {"ppeak_kw": 容量[kW], "tilt_deg": 傾斜角[度], "azimuth_deg": 方位角[度]
             (北=0,東=90,南=180,西=270), "pcs_limit_kw": PCS出力制限[kW]（任意、0=制限なし）}

    Returns:
        dict: 年間発電量・面別発電量・設備利用率
    """
    try:
        normalized_faces, errors = _normalize_faces(faces)
        if errors:
            return {"error": "パラメータ検証エラー", "errors": errors}
        app = _get_app()
        lat, lon, ghi_df, temp_df, name = _resolve_station(station_no)
        faces_app = _faces_to_app_format(normalized_faces)
        g = app.calculate_generation(
            lat, lon, ghi_df, temp_df, faces_app,
            app.DEFAULT_KHD, app.DEFAULT_KPD, app.DEFAULT_KPM,
            app.DEFAULT_KPA, app.DEFAULT_ETA_INO,
            app.DEFAULT_ALPHA, app.DEFAULT_DELTA_T,
        )
        total_pv_kw = sum(f["ppeak_kw"] for f in normalized_faces)
        return {
            "station_no": str(station_no),
            "station_name": name,
            "total_pv_kw": total_pv_kw,
            "annual_generation_kwh": round(g["annual"]),
            "capacity_factor_pct": round(g["annual"] / (total_pv_kw * 8760) * 100, 2) if total_pv_kw > 0 else None,
            "face_generation_kwh": [round(v) for v in g["face_annual"]],
            "monthly_generation_kwh": {str(m): round(g["monthly"].get(m, 0)) for m in range(1, 13)},
            "note": "JIS C 8907準拠（標準補正係数使用、片面パネル）。需要・蓄電池・電気料金・経済性は含まない",
        }
    except Exception as e:
        return {"error": str(e)}


def validate_industrial_params(
    station_no: str = "44132",
    faces: list = [{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities: list = [{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    contract_type: str = "high_voltage",
    basic_charge_yen_per_kw: float = None,
    energy_charge_summer_yen_per_kwh: float = None,
    energy_charge_other_yen_per_kwh: float = None,
    power_factor_pct: float = None,
    fuel_adjustment_yen_per_kwh: float = None,
    renewable_surcharge_yen_per_kwh: float = None,
    sell_mode: str = "surplus_export",
    sell_scheme: str = "fit",
    fit_elapsed_years: int = 1,
    sell_price_yen_per_kwh: float = 8.5,
    battery_enabled: bool = False,
    battery_mode: str = "rule_based",
    battery_capacity_kwh: float = 100.0,
    battery_efficiency_pct: float = 95.0,
    battery_max_charge_kw: float = 50.0,
    battery_max_discharge_kw: float = 50.0,
    battery_soc_min_pct: float = 20.0,
    battery_soc_max_pct: float = 95.0,
    pv_cost_yen_per_kw: float = 158000.0,
    battery_cost_yen_per_kwh: float = 200000.0,
    substation_cost_yen_per_kva: float = 27500.0,
    subsidy_enabled: bool = False,
    subsidy_pv_pct: float = 0.0,
    subsidy_bat_pct: float = 0.0,
    co2_factor_t_per_kwh: float = 0.000431,
    business_model: str = "self_owned",
    contract_years: int = 15,
    target_irr_pct: float = 10.0,
) -> dict:
    """産業用PV+蓄電池シミュレーションのパラメータを検証する（即答）。

    **simulate_industrial_pv を呼ぶ前に必ずこのツールで検証し、返ってきた
    normalized_params をユーザーに提示して確認を得てから実行すること。**
    マイクログリッド事業・両面パネル・最適容量探索は本ツールでは扱わない
    （Gradio UI側のみで利用可能）。

    Args:
        station_no: 地点番号（list_stations で取得）
        faces: 太陽電池アレイ面のリスト（最大8面）。各要素は
            {"ppeak_kw", "tilt_deg", "azimuth_deg"（北=0,東=90,南=180,西=270）, "pcs_limit_kw"}
        facilities: 需要施設のリスト（最大6件、複数指定で需要カーブを合算）。各要素は
            {"building_type": office/primary_school/secondary_school/hospital/hotel/retail,
             "floor_area_m2": 延床面積, "building_count": 棟数}
        contract_type: 契約種別。high_voltage(高圧) / extra_high_voltage(特別高圧)
        basic_charge_yen_per_kw: 基本料金単価 [円/kW・月]（未指定=契約種別のデフォルト値）
        energy_charge_summer_yen_per_kwh: 電力量料金 夏季7-9月 [円/kWh]（未指定=デフォルト）
        energy_charge_other_yen_per_kwh: 電力量料金 その他季 [円/kWh]（未指定=デフォルト）
        power_factor_pct: 力率 [%]（未指定=デフォルト、85%=割引なし）
        fuel_adjustment_yen_per_kwh: 燃料費調整単価 [円/kWh]（未指定=デフォルト）
        renewable_surcharge_yen_per_kwh: 再エネ賦課金 [円/kWh]（未指定=デフォルト）
        sell_mode: 売電モード。surplus_export(余剰売電) / no_export(逆潮流禁止・売電なし)
        sell_scheme: 売電制度。fit(FIT利用あり) / no_fit(FIT利用なし)
        fit_elapsed_years: FIT経過年数（sell_scheme=fit時のみ有効。1-5年目=19円、
            6-20年目=8.3円、21年目以降=8.5円に自動換算される）
        sell_price_yen_per_kwh: 売電単価 [円/kWh]（sell_scheme=no_fit時のみ有効）
        battery_enabled: 蓄電池を導入するか
        battery_mode: 充放電モード。rule_based(ルールベース) / lp_optimized(最適充放電LP、
            17,520コマの線形計画法で年間電気代を最小化。実行に数秒〜十数秒)
        battery_capacity_kwh: 蓄電池容量 [kWh]
        battery_efficiency_pct: 充放電効率 [%]（片道）
        battery_max_charge_kw: 最大充電電力 [kW]
        battery_max_discharge_kw: 最大放電電力 [kW]
        battery_soc_min_pct: SOC下限 [%]
        battery_soc_max_pct: SOC上限 [%]
        pv_cost_yen_per_kw: PVシステム単価 [円/kW]
        battery_cost_yen_per_kwh: 蓄電池単価 [円/kWh]
        substation_cost_yen_per_kva: 特別高圧受電設備工事費 [円/kVA]
            （extra_high_voltage選択時のみ、契約電力2000kW以下の場合に加算）
        subsidy_enabled: 補助金を適用するか
        subsidy_pv_pct: PV補助率 [%]
        subsidy_bat_pct: 蓄電池補助率 [%]
        co2_factor_t_per_kwh: CO2排出係数 [t-CO2/kWh]
        business_model: 事業モデル。self_owned(自己所有) / lease(リース) / ppa(PPA)
        contract_years: 契約年数（lease/ppa時のみ有効）
        target_irr_pct: 事業者目標P-IRR [%]（lease/ppa時のみ有効。需要家には開示しない
            内部パラメータで、リース料/PPA単価の逆算にのみ使用する）

    Returns:
        dict: {"valid": bool, "normalized_params": {...}, "warnings": [...], "errors": [...]}
    """
    try:
        params, warnings, errors = _normalize_and_validate(
            station_no, faces, facilities, contract_type,
            basic_charge_yen_per_kw, energy_charge_summer_yen_per_kwh,
            energy_charge_other_yen_per_kwh, power_factor_pct,
            fuel_adjustment_yen_per_kwh, renewable_surcharge_yen_per_kwh,
            sell_mode, sell_scheme, fit_elapsed_years, sell_price_yen_per_kwh,
            battery_enabled, battery_mode, battery_capacity_kwh,
            battery_efficiency_pct, battery_max_charge_kw, battery_max_discharge_kw,
            battery_soc_min_pct, battery_soc_max_pct,
            pv_cost_yen_per_kw, battery_cost_yen_per_kwh, substation_cost_yen_per_kva,
            subsidy_enabled, subsidy_pv_pct, subsidy_bat_pct,
            co2_factor_t_per_kwh, business_model, contract_years, target_irr_pct,
        )
        runtime = "1-3秒" if not battery_enabled or battery_mode == "rule_based" else "5-20秒（LP最適化）"
        return {
            "valid": len(errors) == 0,
            "normalized_params": params,
            "warnings": warnings,
            "errors": errors,
            "estimated_runtime_seconds": runtime,
            "next_step": "normalized_params をユーザーに提示して確認後、"
                         "simulate_industrial_pv を同じ引数で呼び出す",
        }
    except Exception as e:
        return {"valid": False, "errors": [str(e)], "warnings": []}


def simulate_industrial_pv(
    station_no: str = "44132",
    faces: list = [{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    facilities: list = [{"building_type": "office", "floor_area_m2": 4500.0, "building_count": 1}],
    contract_type: str = "high_voltage",
    basic_charge_yen_per_kw: float = None,
    energy_charge_summer_yen_per_kwh: float = None,
    energy_charge_other_yen_per_kwh: float = None,
    power_factor_pct: float = None,
    fuel_adjustment_yen_per_kwh: float = None,
    renewable_surcharge_yen_per_kwh: float = None,
    sell_mode: str = "surplus_export",
    sell_scheme: str = "fit",
    fit_elapsed_years: int = 1,
    sell_price_yen_per_kwh: float = 8.5,
    battery_enabled: bool = False,
    battery_mode: str = "rule_based",
    battery_capacity_kwh: float = 100.0,
    battery_efficiency_pct: float = 95.0,
    battery_max_charge_kw: float = 50.0,
    battery_max_discharge_kw: float = 50.0,
    battery_soc_min_pct: float = 20.0,
    battery_soc_max_pct: float = 95.0,
    pv_cost_yen_per_kw: float = 158000.0,
    battery_cost_yen_per_kwh: float = 200000.0,
    substation_cost_yen_per_kva: float = 27500.0,
    subsidy_enabled: bool = False,
    subsidy_pv_pct: float = 0.0,
    subsidy_bat_pct: float = 0.0,
    co2_factor_t_per_kwh: float = 0.000431,
    business_model: str = "self_owned",
    contract_years: int = 15,
    target_irr_pct: float = 10.0,
) -> dict:
    """産業用（高圧・特別高圧）太陽光＋蓄電池の需給・電気料金・投資回収を試算する
    （実行1〜3秒、蓄電池LP最適化時は5〜20秒）。

    JIS C 8907準拠の発電量計算、複数施設合算需要との自家消費シミュレーション
    （蓄電池はルールベース or LP最適化）、高圧/特別高圧電気料金の導入前後比較、
    投資額・補助金・投資回収年数、事業モデル（自己所有/リース/PPA）を計算する。
    マイクログリッド事業・両面パネル・最適容量探索には対応しない。

    **事前に validate_industrial_params で検証し、パラメータをユーザーに
    確認してから呼び出すこと。** 引数の意味は validate_industrial_params と同一。

    Returns:
        dict: assumptions（入力エコー）/ annual（発電・自家消費・蓄電池・CO2）/
              electricity_cost（導入前後の電気料金比較）/ investment（投資額・回収年数）/
              business（事業モデル計算結果）/ caveats（免責事項）
    """
    v = validate_industrial_params(
        station_no=station_no, faces=faces, facilities=facilities,
        contract_type=contract_type,
        basic_charge_yen_per_kw=basic_charge_yen_per_kw,
        energy_charge_summer_yen_per_kwh=energy_charge_summer_yen_per_kwh,
        energy_charge_other_yen_per_kwh=energy_charge_other_yen_per_kwh,
        power_factor_pct=power_factor_pct,
        fuel_adjustment_yen_per_kwh=fuel_adjustment_yen_per_kwh,
        renewable_surcharge_yen_per_kwh=renewable_surcharge_yen_per_kwh,
        sell_mode=sell_mode, sell_scheme=sell_scheme,
        fit_elapsed_years=fit_elapsed_years, sell_price_yen_per_kwh=sell_price_yen_per_kwh,
        battery_enabled=battery_enabled, battery_mode=battery_mode,
        battery_capacity_kwh=battery_capacity_kwh,
        battery_efficiency_pct=battery_efficiency_pct,
        battery_max_charge_kw=battery_max_charge_kw,
        battery_max_discharge_kw=battery_max_discharge_kw,
        battery_soc_min_pct=battery_soc_min_pct,
        battery_soc_max_pct=battery_soc_max_pct,
        pv_cost_yen_per_kw=pv_cost_yen_per_kw,
        battery_cost_yen_per_kwh=battery_cost_yen_per_kwh,
        substation_cost_yen_per_kva=substation_cost_yen_per_kva,
        subsidy_enabled=subsidy_enabled, subsidy_pv_pct=subsidy_pv_pct, subsidy_bat_pct=subsidy_bat_pct,
        co2_factor_t_per_kwh=co2_factor_t_per_kwh,
        business_model=business_model, contract_years=contract_years, target_irr_pct=target_irr_pct,
    )
    if not v.get("valid"):
        return {"error": "パラメータ検証エラー", "errors": v.get("errors", []),
                "warnings": v.get("warnings", [])}
    try:
        out = _run_industrial_simulation(v["normalized_params"])
        out["validation_warnings"] = v.get("warnings", [])
        return out
    except Exception as e:
        return {"error": str(e)}
