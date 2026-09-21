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

スコープ（Phase 4d-2、MCP_HANDOFF.md の段階的実装方針に従う）:
  対象 — 単体施設・複数施設合算、PV発電（片面/両面）、高圧/特別高圧料金、蓄電池
        （ルールベース or LP最適化）、投資回収、事業モデル（自己所有/リース/PPA）、CO2削減量、
        マイクログリッド事業（網内売電・束ねメリット・P-IRR）
  未対応（後続フェーズで追加予定） — 最適容量探索（2〜3分かかるため）、
        カスタム需要CSVアップロード

データセンター（Phase 7 段階4）: estimate_dc_demand / validate_dc_params / simulate_dc。
  需要をIT負荷×PUEから生成し、以降は産業用と同じ計算経路を使う。系統受電上限（蓄電池LPで強制）に対応。
  未対応 — 気温連動PUE、マイクログリッド事業、需要側の調整（IT負荷のシフト。保留）
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


def _jsonable(o):
    """戻り値をJSON標準の型に揃える（公開ツールの return で通す）。

    numpy のスカラー（numpy.float64 など）が混ざると、GradioのMCP応答では数値でなく文字列
    （"11.5"）になる。round() の入力が numpy 由来だと結果も numpy のままなので、個別のキャストではなく
    返す直前にここで1か所で正規化する。値は変えない（型だけ）。
    """
    if isinstance(o, dict):
        return {_jsonable(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return o


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

# --- データセンター（Phase 7 段階4）の入力上限 ---
MAX_IT_CAPACITY_KW = 500_000.0
MAX_RACKS = 100_000
MAX_KW_PER_RACK = 200.0
MAX_GRID_CAP_KW = 1_000_000.0


def _dc_maps(app):
    """DCツールの簡易キー → app.py内部の日本語ラベル。定数があるものは app のものを使う。"""
    return {
        "workload": {
            "housing": "ハウジング（コロケーション）",
            "ai_training": "AI（学習中心）",
            "ai_inference": "AI（推論中心）",
            "in_house_server_room": "自社サーバー室",
            "manual": "手動設定",
        },
        "capacity_mode": {
            "size_preset": "規模プリセット",
            "it_capacity": "IT容量を直接入力",
            "rack_density": "ラック数×density",
        },
        "size_preset": {
            "edge": "エッジ", "small": "小規模", "medium": "中規模", "hyperscale": "ハイパースケール",
        },
        "profile": {
            "cec": app.PROFILE_CEC, "flat": app.PROFILE_FLAT, "diurnal": app.PROFILE_DIURNAL,
        },
        "noise_level": {"none": "なし", "low": "低（3〜7%）", "high": "高（12〜18%）"},
        "grid_cap": {
            "none": app.GRID_CAP_NONE,
            "hv_under_2000kw": app.GRID_CAP_HV,
            "ehv_under_10000kw": app.GRID_CAP_EHV33,
            "manual": app.GRID_CAP_MANUAL,
        },
    }


_COMMON_CAVEATS = [
    "本結果は投資判断の参考情報であり、収益・投資回収年数を保証するものではありません",
    "本ツールは最適容量探索（2〜3分かかる蓄電池容量のグリッドサーチ）には未対応です。"
    "これはGradio UI側でのみ利用できます",
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


# 辞書の要素で許すキーと、よくある誤りの別名 → 正しいキー（未知のキーを黙って無視すると、既定値で計算して誤った数字を返すため）
_FACE_KEYS = ("ppeak_kw", "tilt_deg", "azimuth_deg", "pcs_limit_kw")
_FACE_ALIASES = {
    "capacity_kw": "ppeak_kw", "capacity": "ppeak_kw", "power_kw": "ppeak_kw", "kw": "ppeak_kw", "kwp": "ppeak_kw",
    "peak_kw": "ppeak_kw", "pv_kw": "ppeak_kw", "ppeak": "ppeak_kw",
    "tilt": "tilt_deg", "angle": "tilt_deg", "slope": "tilt_deg", "tilt_angle": "tilt_deg",
    "azimuth": "azimuth_deg", "azimuth_angle": "azimuth_deg", "orientation": "azimuth_deg", "direction": "azimuth_deg",
    "pcs_kw": "pcs_limit_kw", "pcs_limit": "pcs_limit_kw", "pcs": "pcs_limit_kw", "pcs_limit_kw ": "pcs_limit_kw",
}
_FACILITY_KEYS = ("building_type", "floor_area_m2", "building_count")
_FACILITY_ALIASES = {
    "type": "building_type", "facility_type": "building_type", "kind": "building_type", "building": "building_type",
    "area": "floor_area_m2", "floor_area": "floor_area_m2", "area_m2": "floor_area_m2", "m2": "floor_area_m2",
    "floor_area_sqm": "floor_area_m2",
    "count": "building_count", "num": "building_count", "buildings": "building_count", "n_buildings": "building_count",
}


def _unknown_key_errors(where, obj, allowed, aliases, note=""):
    """辞書 obj の未知のキーを、正しいキーの候補つきのエラー文にする（無ければ空リスト）。

    未知のキーを黙って無視すると、既定値（例: 方位角180°）で計算して誤った数字を返すため、明示エラーにする。
    候補は、よくある誤りの別名表 → なければ綴りの近いキー（difflib）の順で探す。
    """
    import difflib
    errs = []
    for k in obj:
        if k in allowed:
            continue
        hint = aliases.get(k) or aliases.get(str(k).strip().lower())
        if hint is None:
            close = difflib.get_close_matches(str(k), allowed, n=1, cutoff=0.6)
            hint = close[0] if close else None
        msg = f"{where} に未知のキー {k!r} があります"
        if hint:
            msg += f"。{hint!r} の誤りではありませんか"
        msg += f"（使えるキー: {list(allowed)}）"
        errs.append(msg)
    if errs and note:
        errs[-1] += note   # 補足は要素ごとに1回だけ
    return errs


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
        unknown = _unknown_key_errors(f"faces[{i}]", f, _FACE_KEYS, _FACE_ALIASES,
                                      note="。方位角は 北=0,東=90,南=180,西=270 の数値（azimuth_deg）")
        if unknown:
            errors.extend(unknown)
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
        unknown = _unknown_key_errors(f"facilities[{i}]", fac, _FACILITY_KEYS, _FACILITY_ALIASES)
        if unknown:
            errors.extend(unknown)
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
    bifacial_enabled,
    bifaciality,
    gcr,
    panel_height_m,
    pitch_m,
    snow_albedo_enabled,
    mg_enabled,
    mg_line_distance_km,
    mg_line_cost_yen_per_km,
    mg_opex_ratio_pct,
    mg_irr_period_years,
    dc_mode=False,
):
    """パラメータを正規化し (params, warnings, errors) を返す。重い計算は実行しない。

    dc_mode=True（データセンターツール）のときは需要施設（facilities）を検証しない。
    需要はDC入力（_normalize_dc_params）から生成するため。
    """
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

    if dc_mode:
        normalized_facilities, fac_errors = [], []
    else:
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

    if bifacial_enabled:
        if not (0 <= bifaciality <= 1):
            errors.append("bifaciality は 0〜1 で指定してください")
        if not (0 < gcr <= 1):
            errors.append("gcr は 0〜1 で指定してください")
        if not (0 < panel_height_m <= 100):
            errors.append("panel_height_m は 0〜100 で指定してください")
        if not (0 < pitch_m <= 100):
            errors.append("pitch_m は 0〜100 で指定してください")

    if mg_enabled:
        if mg_line_distance_km < 0:
            errors.append("mg_line_distance_km は0以上で指定してください")
        if mg_line_cost_yen_per_km < 0:
            errors.append("mg_line_cost_yen_per_km は0以上で指定してください")
        if not (0 <= mg_opex_ratio_pct <= 100):
            errors.append("mg_opex_ratio_pct は 0〜100 で指定してください")
        if not (1 <= int(mg_irr_period_years) <= 40):
            errors.append("mg_irr_period_years は 1〜40 で指定してください")

    # --- 警告 ---
    if battery_enabled and battery_capacity_kwh > 0 and battery_max_charge_kw > battery_capacity_kwh:
        warnings.append(
            f"充電レート {battery_max_charge_kw:.0f}kW が容量 {battery_capacity_kwh:.0f}kWh を超えています"
            "（1C超の高速蓄電池想定になっています）"
        )
    if sell_mode == "no_export" and sell_scheme == "fit":
        warnings.append(
            "逆潮流禁止（売電なし）を選択しているため、FIT単価は投資回収計算に反映されません"
            "（売電収入がゼロになります）"
        )
    if mg_enabled and len(normalized_facilities) <= 1:
        warnings.append(
            "施設が1つのため、マイクログリッドの束ねメリット（複数施設合算による基本料金差額）"
            "は発生しません（PV導入効果のみが基本料金差額として計上されます）"
        )
    if mg_enabled and business_model == "self_owned":
        warnings.append(
            "事業モデルがself_ownedのため、MG網内売電単価は電力量単価の加重平均で計算されます"
            "（PPA選択時はPPA単価で計算されます）"
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
        "bifacial_enabled": bool(bifacial_enabled),
        "bifaciality": float(bifaciality) if bifacial_enabled else None,
        "gcr": float(gcr) if bifacial_enabled else None,
        "panel_height_m": float(panel_height_m) if bifacial_enabled else None,
        "pitch_m": float(pitch_m) if bifacial_enabled else None,
        "snow_albedo_enabled": bool(snow_albedo_enabled) if bifacial_enabled else None,
        "mg_enabled": bool(mg_enabled),
        "mg_line_distance_km": float(mg_line_distance_km) if mg_enabled else None,
        "mg_line_cost_yen_per_km": float(mg_line_cost_yen_per_km) if mg_enabled else None,
        "mg_opex_ratio_pct": float(mg_opex_ratio_pct) if mg_enabled else None,
        "mg_irr_period_years": int(mg_irr_period_years) if mg_enabled else None,
    }
    return params, warnings, errors


# ============================================================
# 風力発電（オフサイトPPA）。設計は docs/wind_design_spec.md
# ============================================================
# 風力は送配電網で届くオフサイト電源。経済性の計算（受電点の基準・託送・賦課金・手数料）は UI と同じ
# app.offsite_receiving / app.offsite_cost_after / app.resolve_wind を再利用する（二重実装しない）。

_WIND_KEYS = ("capacity_kw", "coverage_pct", "cf_pct", "ppa_price_yen_per_kwh",
              "wheeling_yen_per_kwh", "retail_fee_yen_per_kwh")
_NO_PV_FACES_STUB = [{"ppeak_kw": 1.0, "tilt_deg": 30.0, "azimuth_deg": 180.0}]  # pv_enabled=false のとき faces検証を通すだけ


def _faces_arg(faces, pv_enabled):
    """pv_enabled=false のときは faces を使わない（空・不正でも検証を通す）。"""
    return faces if pv_enabled else _NO_PV_FACES_STUB


def _normalize_wind(wind, station_no, contract_type):
    """wind 辞書を検証して既定値を解決する。戻り値: (正規化した辞書 or None, エラー一覧)。"""
    app = _get_app()
    errors = []
    if not isinstance(wind, dict):
        return None, ["wind は辞書で指定してください（例: {\"capacity_kw\": 1000}）"]
    unknown = sorted(set(wind) - set(_WIND_KEYS))
    if unknown:
        errors.append(f"wind に未知のキーがあります: {unknown}（使えるキー: {list(_WIND_KEYS)}）")

    def num(name, lo, hi, lo_open=True):
        v = wind.get(name)
        try:
            f = float(v)
        except (TypeError, ValueError):
            errors.append(f"wind.{name} は数値で指定してください（{v!r}）")
            return None
        if not np.isfinite(f) or (f <= lo if lo_open else f < lo) or f > hi:
            errors.append(f"wind.{name} は {lo}{'<' if lo_open else '≦'} x ≦ {hi} で指定してください（{f}）")
            return None
        return f

    has_cap, has_cov = wind.get("capacity_kw") is not None, wind.get("coverage_pct") is not None
    if has_cap == has_cov:
        errors.append("wind は capacity_kw か coverage_pct のどちらか1つだけを指定してください")
    area = app.station_to_wind_area(str(station_no).strip())
    if area is None:
        errors.append(f"風力は北海道・東北の観測地点でのみ使えます（station_no={station_no}）。"
                      "他エリアの需要地に対する越境託送はモデル化していません（対象地点は list_wind_areas で確認）")
    out = {}
    if has_cap:
        out["capacity_kw"] = num("capacity_kw", 0.0, 1e6)
    if has_cov:
        out["coverage_pct"] = num("coverage_pct", 0.0, 1000.0)
    ct = "特別高圧" if CONTRACT_TYPE_MAP.get(contract_type) == "特別高圧" else "高圧"
    out["cf_pct"] = num("cf_pct", 0.0, 100.0) if wind.get("cf_pct") is not None else app.WIND_CF_DEFAULT_PCT
    out["ppa_price_yen_per_kwh"] = (num("ppa_price_yen_per_kwh", 0.0, 1000.0, lo_open=False)
                                    if wind.get("ppa_price_yen_per_kwh") is not None else app.WIND_PPA_PRICE_DEFAULT)
    if wind.get("wheeling_yen_per_kwh") is not None:
        out["wheeling_yen_per_kwh"] = num("wheeling_yen_per_kwh", 0.0, 1000.0, lo_open=False)
    else:
        out["wheeling_yen_per_kwh"] = app.WIND_WHEELING_ENERGY_YEN.get((area, ct)) if area else None
    out["retail_fee_yen_per_kwh"] = (num("retail_fee_yen_per_kwh", 0.0, 1000.0, lo_open=False)
                                     if wind.get("retail_fee_yen_per_kwh") is not None else app.WIND_RETAIL_FEE_YEN[ct])
    if errors:
        return None, errors
    out["area"] = app.WIND_AREA_META[area]["name"]
    return out, []


def _apply_wind(params, warnings, errors, wind, pv_enabled, station_no, contract_type):
    """検証済みの params に、風力と pv_enabled を反映する（省略時は何も足さない＝従来の出力）。"""
    if not pv_enabled:
        params["pv_enabled"] = False
        params["faces"] = []
        if wind is None:
            errors.append("pv_enabled=false のときは wind を指定してください（太陽光も風力も使わない設定です）")
        else:
            warnings.append("pv_enabled=false のため faces は使いません（風力のみで計算します）")
    if wind is None:
        return
    w, werrs = _normalize_wind(wind, station_no, contract_type)
    errors.extend(werrs)
    if w is not None:
        params["wind"] = w


def _wind_args_for_app(app, w):
    """正規化した wind（MCPのキー）を app.resolve_wind の wind_args にする。"""
    return {
        "enabled": True,
        "sizing_mode": app.WIND_SIZING_CAPACITY if w.get("capacity_kw") is not None else app.WIND_SIZING_COVERAGE,
        "capacity_kw": w.get("capacity_kw"), "coverage_pct": w.get("coverage_pct"),
        "cf_pct": w["cf_pct"], "ppa_price": w["ppa_price_yen_per_kwh"],
        "wheeling_yen": w["wheeling_yen_per_kwh"], "retail_fee_yen": w["retail_fee_yen_per_kwh"],
    }


_WIND_CAVEATS = [
    "風力はオフサイト電源（送配電網で届く）として計算しています。届いた風力にも託送の電力量料金・再エネ賦課金・小売手数料が"
    "かかり、契約電力（基本料金）は風力では下がりません。売電・出力抑制の対象は敷地内の太陽光の余剰だけです（風力の余剰は無駄になる扱い）",
    "太陽光は平年値（METPV-20）、風力は2025年の実績で、別のデータです。同じ日に凪と曇天が重なるような日単位・30分単位の同時性は"
    "反映されません。月別・時間帯別の平均的な補完関係の目安です",
    "託送の電力量料金は一次資料（北海道電力NW・東北電力NW、2025年10月〜、税込表示）の値です。小売手数料は自然エネルギー財団の"
    "推定値（2023年度・全国平均）で公的な料金表はありません。PPA単価の既定値（11.96円/kWh）は入札の平均落札価格で、"
    "PPAの相対契約の単価そのものではありません",
    "蓄電池LP（lp_optimized）は受電点の基準で最適化します（風力は受電量の一部。売電・出力抑制は太陽光の余剰だけ）。"
    "ルールベース（rule_based）の蓄電池は太陽光の余剰だけを貯め、太陽光と蓄電池で賄えない分を風力（届いた分）→小売が埋めます"
    "（風力は貯めません）。LPは電気代の最小化で、24/7の一致率の最大化ではありません",
]


def _wind_section(app, wind_info, gen_pv, demand_30min, sc_result, month_day, rate_params, pv_used):
    """結果の wind 節（発電・配達・費用・24/7の一致率・月別）を作る。"""
    gen_w = wind_info["gen_30min"]
    gen_all = gen_pv + gen_w
    demand = float(sc_result["annual_demand"])
    sur = rate_params["renewable_surcharge"]
    dl = wind_info.get("delivered_kwh", 0.0)
    extra = dl * (wind_info["wheeling_yen"] + sur + wind_info["retail_fee_yen"])
    total_cost = wind_info["payment_yen"] + extra

    def rates(g):
        vol, hourly = app.matching_rates(g, demand_30min)
        return {"volume_pct": round(vol, 1), "hourly_match_pct": round(hourly, 1)}

    reference = {"wind_only": rates(gen_w), "pv_plus_wind": rates(gen_all)}
    if pv_used:
        reference = {"pv_only": rates(gen_pv), **reference}
    m_pv, m_w = app.monthly_sums(gen_pv, month_day), app.monthly_sums(gen_w, month_day)
    m_imp = app.monthly_sums(sc_result["import_"], month_day)
    monthly = []
    for m in range(1, 13):
        dem = float(sc_result["monthly_demand"].get(m, 0.0))
        monthly.append({
            "month": m, "pv_kwh": round(m_pv.get(m, 0.0)), "wind_kwh": round(m_w.get(m, 0.0)),
            "demand_kwh": round(dem), "import_kwh": round(m_imp.get(m, 0.0)),
            "hourly_match_pct": round((1.0 - m_imp.get(m, 0.0) / dem) * 100.0, 1) if dem > 0 else None,
        })
    vol_all = min(1.0, float(np.sum(gen_all)) / demand) * 100.0 if demand > 0 else None
    hourly_set = (1.0 - float(sc_result["annual_import"]) / demand) * 100.0 if demand > 0 else None
    return {
        "area": wind_info["area_name"],
        "capacity_kw": round(wind_info["capacity_kw"], 1),
        "cf_pct": wind_info["cf_pct"],
        "effective_cf_pct": round(wind_info["effective_cf_pct"], 2),
        "generation_kwh": round(wind_info["annual_kwh"]),
        "clipped_kwh": round(wind_info["clipped_kwh"]),
        "delivered_kwh": round(dl),
        "wasted_kwh": round(wind_info.get("wasted_kwh", 0.0)),
        "cost": {
            "ppa_price_yen_per_kwh": wind_info["ppa_price"],
            "ppa_payment_yen_per_year": round(wind_info["payment_yen"]),
            "wheeling_yen_per_kwh": wind_info["wheeling_yen"],
            "renewable_surcharge_yen_per_kwh": sur,
            "retail_fee_yen_per_kwh": wind_info["retail_fee_yen"],
            "delivered_extra_cost_yen_per_year": round(extra),
            "total_yen_per_year": round(total_cost),
            "effective_yen_per_delivered_kwh": round(total_cost / dl, 2) if dl > 0 else None,
            "note": "PPA支払は発電した全量に（無駄になった分も）、託送・賦課金・手数料は届いた分にかかる",
        },
        "matching_24_7": {
            "volume_pct": round(vol_all, 1) if vol_all is not None else None,
            "hourly_match_pct": round(hourly_set, 1) if hourly_set is not None else None,
            "gap_points": round(max(0.0, vol_all - hourly_set), 1) if vol_all is not None else None,
            "reference_without_battery": reference,
            "note": "hourly_match_pct = 1 − 系統購入/需要（蓄電池・設定どおり）。volume_pct との差が、年間では足りていても"
                    "その時間には足りていない分。reference_without_battery は蓄電池なしの参考値",
        },
        "monthly": monthly,
    }


# ============================================================
# シミュレーション本体
# ============================================================

class _GridCapInfeasible(Exception):
    """系統受電上限を守れない（診断の必要条件が破れた、またはLPが実行不可能）。

    エラーではなく診断結果として返すため、呼び出し側（simulate_dc）が捕まえて応答を組み立てる。
    status: "infeasible"（必要条件が破れ、LPは実行していない）/ "infeasible_lp"（LPが実行不可能）
    """

    def __init__(self, diag, status):
        super().__init__(f"受電上限を守れません（{status}）")
        self.diag = diag
        self.status = status


def _grid_cap_section(cap_kw, diag, status, peak_before_kw, peak_after_kw=None):
    """受電上限の結果（JSON）を組み立てる。UIの「受電上限」節（app.format_grid_cap）と同じ内容。"""
    sec = {
        "cap_kw": round(float(cap_kw), 1),
        "status": status,
        "enforced": status == "enforced",
        "peak_before_kw": round(float(peak_before_kw), 1),
        "within_cap_before": bool(peak_before_kw <= cap_kw),
        "peak_after_kw": None,
        "within_cap_after": None,
        "needs_battery": bool(diag["needs_battery"]),
        "violations": list(diag["violations"]),
        "exceed_slots": int(diag["exceed_slots"]),
        "exceed_energy_kwh": round(diag["exceed_energy_kwh"]),
        "min_cap_kw_if_battery_unlimited": (
            round(diag["min_cap_kw_energy"], 1) if diag["min_cap_kw_energy"] is not None else None),
        "required_battery_lower_bound": None,
    }
    if peak_after_kw is not None:
        sec["peak_after_kw"] = round(float(peak_after_kw), 1)
        sec["within_cap_after"] = bool(peak_after_kw - cap_kw <= max(1e-6 * cap_kw, 1e-6))
    if diag["needs_battery"] and diag["energy_feasible"]:
        sec["required_battery_lower_bound"] = {
            "discharge_power_kw": round(diag["required_power_kw"]),
            "usable_capacity_kwh": round(diag["required_usable_kwh"]),
            "nominal_capacity_kwh": round(diag["required_capacity_kwh"]),
            "note": "充放電レート・効率損失・SOC範囲を細かく考えない下限の目安。実際にはこれ以上必要になる場合がある",
        }
    return sec


def _run_industrial_simulation(p: dict, demand_override=None, grid_cap_kw=None):
    """検証済みパラメータ p でシミュレーションを実行し、構造化dictを返す。

    demand_override: (demand_30min, individual_demands)。指定するとComStock需要の代わりに使う
        （データセンターツールがIT負荷×PUEの需要を差し込む）。None=従来どおり facilities から生成。
    grid_cap_kw: 系統受電上限[kW]（None=制限なし。DCだけが指定する）。上限を強制できるのは
        蓄電池LPのみ。守れないときは _GridCapInfeasible を送出する。上限ありのときだけ
        結果に "grid_cap" 節を加える（産業用の出力は変えない）。
    """
    app = _get_app()
    lat, lon, ghi_df, temp_df, _ = _resolve_station(p["station_no"])

    # --- 両面パネル: albedo時系列の準備（app.run_simulationと同じロジック） ---
    albedo_flat = None
    if p["bifacial_enabled"]:
        if p["snow_albedo_enabled"]:
            snow_df = app.load_snow_depth(p["station_no"])
            albedo_flat = app.build_albedo_series(snow_df)
        else:
            albedo_flat = np.full(365 * 48, app.ALBEDO_NORMAL)

    faces_app = _faces_to_app_format(p["faces"])
    result = app.calculate_generation(
        lat, lon, ghi_df, temp_df, faces_app,
        app.DEFAULT_KHD, app.DEFAULT_KPD, app.DEFAULT_KPM,
        app.DEFAULT_KPA, app.DEFAULT_ETA_INO,
        app.DEFAULT_ALPHA, app.DEFAULT_DELTA_T,
        bifacial=p["bifacial_enabled"],
        bifaciality=p["bifaciality"] if p["bifacial_enabled"] else app.BIFACIAL_DEFAULTS["bifaciality"],
        gcr=p["gcr"] if p["bifacial_enabled"] else app.BIFACIAL_DEFAULTS["gcr"],
        height=p["panel_height_m"] if p["bifacial_enabled"] else app.BIFACIAL_DEFAULTS["height"],
        pitch=p["pitch_m"] if p["bifacial_enabled"] else app.BIFACIAL_DEFAULTS["pitch"],
        albedo_flat=albedo_flat,
    )
    gen = result["total_gen_clipped"]
    month_day = result["month_day"]

    if demand_override is not None:
        demand_30min, individual_demands = demand_override
    else:
        facility_args = _facilities_to_facility_args(p["facilities"])
        demand_30min, individual_demands = app.load_combined_demand(
            facility_args, len(p["facilities"]),
        )

    # --- 風力（オフサイトPPA。省略時は何もしない＝従来の出力） ---
    wind_p = p.get("wind")
    wind_info = None
    gen_pv = gen
    if wind_p:
        wind_info = app.resolve_wind(
            _wind_args_for_app(app, wind_p), p["station_no"], float(np.sum(demand_30min)),
            station_label=p["station_no"], contract_type=CONTRACT_TYPE_MAP[p["contract_type"]])
        gen = gen_pv + wind_info["gen_30min"]   # ルールベース・蓄電池なしは太陽光＋風力を合わせた発電で運転する
    # 蓄電池LPは風力（送配電網で届く電源）を受電点の基準で扱う: 太陽光だけを generation に、風力は offsite で渡す
    # （app.run_simulation と同じ。設計書 wind §5-4・W2d）
    offsite_spec = app.offsite_lp_spec([wind_info]) if wind_info is not None else None
    gen_lp = gen_pv if offsite_spec is not None else gen

    no_export = (p["sell_mode"] == "no_export")
    battery_active = p["battery_enabled"] and p["battery_capacity_kwh"] > 0
    battery_lp = battery_active and p["battery_mode"] == "lp_optimized"

    # 受電上限あり: PV差引後の負荷から必要条件で診断する（UIの run_simulation と同じ流れ）。
    # 上限を強制できるのはLPだけ。LPで確実に守れないなら、LPを解かずに診断を返す
    grid_cap_diag = None
    if grid_cap_kw:
        # 受電上限は受電点（風力の配達分も含む）で見る。風力は上限を守る助けにならないので、太陽光だけで診断する
        grid_cap_diag = app.diagnose_grid_cap(
            gen_lp, demand_30min, grid_cap_kw,
            p["battery_capacity_kwh"] if battery_active else 0.0,
            p["battery_efficiency_pct"],
            p["battery_max_charge_kw"] if battery_active else 0.0,
            p["battery_max_discharge_kw"] if battery_active else 0.0,
            p["battery_soc_min_pct"], p["battery_soc_max_pct"],
        )
        if battery_lp and grid_cap_diag["violations"]:
            raise _GridCapInfeasible(grid_cap_diag, "infeasible")

    sc_result = None
    if battery_active:
        if battery_lp:
            try:
                sc_result = app.optimize_battery(
                    gen_lp, demand_30min, month_day,
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
                    grid_import_cap_kw=grid_cap_kw,
                    offsite=offsite_spec,
                )
            except app.GridCapInfeasibleError:
                # 診断の必要条件は満たしたがLPが実行不可能（主に充電レート不足）
                raise _GridCapInfeasible(grid_cap_diag, "infeasible_lp")
        else:
            # ルールベース。風力あり: 蓄電池は太陽光だけで動かし、不足分を風力→小売が埋める（W2e。UIと同じ）
            sc_result = app.simulate_battery(
                gen_lp, demand_30min, month_day,
                capacity_kwh=p["battery_capacity_kwh"],
                efficiency_pct=p["battery_efficiency_pct"],
                max_charge_kw=p["battery_max_charge_kw"],
                max_discharge_kw=p["battery_max_discharge_kw"],
                soc_min_pct=p["battery_soc_min_pct"],
                soc_max_pct=p["battery_soc_max_pct"],
                no_export=no_export,
                offsite_gen=None if offsite_spec is None else offsite_spec["gen_30min"],
            )
    else:
        sc_result = app.calculate_self_consumption(gen, demand_30min, month_day, no_export=no_export)

    # 風力: 料金・売電は受電点の基準に組み替える（契約電力は風力で下がらない／届いた風力に託送等がかかる／
    # 売電・抑制は敷地内の太陽光の余剰だけ）。app.run_simulation と同じ関数を使う
    wind_off = None
    if wind_info is not None:
        wind_off = app.offsite_receiving(gen_pv, [wind_info], demand_30min, sc_result)
        wind_info["delivered_kwh"] = float(wind_off["delivered_by_source"][0].sum())
        wind_info["wasted_kwh"] = float(wind_off["wasted_by_source"][0].sum())
        pv_surplus_kwh = float(wind_off["pv_surplus"].sum())
        sc_result["annual_export_pooled"] = sc_result["annual_export"]
        sc_result["annual_curtailment_pooled"] = sc_result.get("annual_curtailment", 0.0)
        sc_result["annual_export"] = 0.0 if no_export else pv_surplus_kwh
        sc_result["annual_curtailment"] = pv_surplus_kwh if no_export else 0.0

    rate_params = dict(
        basic_charge_per_kw=p["basic_charge_yen_per_kw"],
        energy_charge_summer=p["energy_charge_summer_yen_per_kwh"],
        energy_charge_other=p["energy_charge_other_yen_per_kwh"],
        power_factor_pct=p["power_factor_pct"],
        fuel_adjustment=p["fuel_adjustment_yen_per_kwh"],
        renewable_surcharge=p["renewable_surcharge_yen_per_kwh"],
    )
    cost_before = app.calc_electricity_cost(demand_30min, month_day, **rate_params)
    if wind_off is not None:
        cost_after = app.offsite_cost_after(wind_off, [wind_info], month_day, rate_params)
    else:
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
    if wind_info is not None:
        annual_merit -= wind_info["payment_yen"]   # 風力PPA支払（届いた分の託送等は cost_after に含まれる）

    payback_years = (net_investment / annual_merit) if annual_merit > 0 else None
    if wind_info is not None and net_investment <= 0:
        payback_years = None   # 風力のみ（初期投資なし）: 回収すべき投資がない

    # --- CO2削減量 ---
    grid_reduction = sc_result["annual_demand"] - sc_result["annual_import"]
    co2_reduction = grid_reduction * p["co2_factor_t_per_kwh"]

    # --- 事業モデル（自己所有/リース/PPA、app.run_simulationのCRF式を踏襲） ---
    # MG有効時はMG投資全額（PV+蓄電池+自営線）をベースに、運営コストも含めて逆算する
    # （app.run_simulationの「A案」ロジックをそのまま踏襲）
    mg_line_cost = 0.0
    mg_total_investment = net_investment
    mg_annual_opex = 0.0
    if p["mg_enabled"]:
        mg_line_cost = p["mg_line_distance_km"] * p["mg_line_cost_yen_per_km"]
        mg_total_investment = net_investment + mg_line_cost
        mg_annual_opex = mg_total_investment * (p["mg_opex_ratio_pct"] / 100.0)

    # 風力: 需要家メリットは風力の費用を引いた annual_merit。PPA単価の分母は 自家消費 − 風力の配達量（敷地内の太陽光・蓄電池分）。
    # MG＋風力は、風力の調達費用も網内の需要家が負担するので、単価は（投資の回収＋風力の調達費用）÷ 網内に供給した全量
    pv_self_kwh = sc_result["annual_self"]
    wind_cost_total = 0.0
    if wind_info is not None:
        pv_self_kwh = max(0.0, pv_self_kwh - wind_info["delivered_kwh"])
        wind_cost_total = (wind_info["payment_yen"] + wind_info["delivered_kwh"] * (
            wind_info["wheeling_yen"] + rate_params["renewable_surcharge"] + wind_info["retail_fee_yen"]))

    business_out = {"business_model": p["business_model"]}
    ppa_price = None  # MG収益計算で参照（PPA選択時のみ値が入る）
    if p["business_model"] in ("lease", "ppa") and net_investment > 0:
        n_years = p["contract_years"]
        r = p["target_irr_pct"] / 100.0
        crf = r * (1 + r) ** n_years / ((1 + r) ** n_years - 1) if r > 0 else 1.0 / n_years
        if p["mg_enabled"]:
            lease_base_investment = mg_total_investment
            annual_lease = lease_base_investment * crf + mg_annual_opex
        else:
            lease_base_investment = net_investment
            annual_lease = lease_base_investment * crf
        business_out["investment_base_yen"] = round(lease_base_investment)
        business_out["contract_years"] = n_years
        business_out["target_irr_pct"] = p["target_irr_pct"]
        if p["mg_enabled"]:
            business_out["mg_opex_included_yen_per_year"] = round(mg_annual_opex)
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
            mg_wind = bool(p["mg_enabled"] and wind_info is not None)
            ppa_denom = sc_result["annual_self"] if (wind_info is None or mg_wind) else pv_self_kwh
            if ppa_denom > 0:
                ppa_price = (annual_lease + (wind_cost_total if mg_wind else 0.0)) / ppa_denom
                ppa_annual_cost = ppa_price * ppa_denom
                customer_annual = annual_merit - ppa_annual_cost + (wind_cost_total if mg_wind else 0.0)
                business_out["required_ppa_price_yen_per_kwh"] = round(ppa_price, 2)
                business_out["annual_self_consumption_kwh"] = round(ppa_denom)
                if mg_wind:
                    business_out["ppa_basis"] = "（投資の回収＋風力の調達費用）÷ 網内に供給した全量"
                elif wind_info is not None:
                    business_out["ppa_basis"] = "投資の回収 ÷ 敷地内の太陽光・蓄電池分の自家消費量（風力の配達分を除く）"
                business_out["customer_annual_benefit_yen"] = round(customer_annual)
                business_out["customer_total_benefit_yen"] = round(customer_annual * n_years)
                business_out["proposal_viable"] = customer_annual >= 0
            else:
                business_out["required_ppa_price_yen_per_kwh"] = None
                business_out["proposal_viable"] = False
    else:
        business_out["note"] = "自己所有のため事業者側パラメータ（投資ベース・リース料等）は算出していません"

    # --- マイクログリッド事業（モードB、app.run_simulationのMG収益計算セクションを踏襲） ---
    # モードA（lease/ppa）と異なり、MGはP-IRRの算出自体が事業者側のゴールのため
    # project_irr_pct をそのまま開示する（CLAUDE.md: モードBはP-IRR算出がゴール）
    microgrid_out = None
    if p["mg_enabled"]:
        if len(individual_demands) > 1:
            individual_basic_total = 0.0
            for ind_demand in individual_demands:
                ind_cost = app.calc_electricity_cost(ind_demand, month_day, **rate_params)
                individual_basic_total += ind_cost["annual_basic"]
        else:
            individual_basic_total = cost_before["annual_basic"]
        mg_combined_basic = cost_after["annual_basic"]
        bundling_merit = individual_basic_total - mg_combined_basic

        avg_energy_price = (
            p["energy_charge_summer_yen_per_kwh"] * 0.25
            + p["energy_charge_other_yen_per_kwh"] * 0.75
        )
        if ppa_price is not None:
            mg_sell_price = ppa_price
            mg_sell_price_basis = "ppa_price"
        else:
            mg_sell_price = avg_energy_price
            mg_sell_price_basis = "energy_charge_weighted_average"

        pv_revenue = sc_result["annual_self"] * mg_sell_price
        mg_annual_revenue = pv_revenue + bundling_merit
        mg_annual_cashflow = mg_annual_revenue - mg_annual_opex - wind_cost_total   # 風力なしは 0.0

        mg_period = p["mg_irr_period_years"]
        cashflows = [-mg_total_investment] + [mg_annual_cashflow] * mg_period
        mg_irr = app._calc_irr(cashflows)

        microgrid_out = {
            "mg_line_cost_yen": round(mg_line_cost),
            "mg_total_investment_yen": round(mg_total_investment),
            "mg_annual_opex_yen": round(mg_annual_opex),
            "pv_sell_price_yen_per_kwh": round(mg_sell_price, 2),
            "pv_sell_price_basis": mg_sell_price_basis,
            "pv_revenue_yen_per_year": round(pv_revenue),
            "bundling_merit_yen_per_year": round(bundling_merit),
            "individual_basic_charge_total_yen": round(individual_basic_total),
            "mg_combined_basic_charge_yen": round(mg_combined_basic),
            "mg_annual_revenue_yen": round(mg_annual_revenue),
            "mg_annual_cashflow_yen": round(mg_annual_cashflow),
            "irr_period_years": mg_period,
            "project_irr_pct": round(mg_irr * 100, 2) if mg_irr is not None else None,
            "project_irr_note": "モードB（マイクログリッド事業）ではP-IRRの算出自体が事業者向けの"
                                "ゴールのため、モードA（lease/ppa）の目標P-IRRとは異なりそのまま開示しています",
        }
        if wind_info is not None:
            microgrid_out["wind_procurement_cost_yen_per_year"] = round(wind_cost_total)

    caveats = list(_COMMON_CAVEATS)
    if p["business_model"] in ("lease", "ppa"):
        caveats.append(
            "リース料/PPA単価は事業者の目標P-IRRから資本回収係数（CRF）で逆算した値であり、"
            "需要家に開示するP-IRRそのものではありません（CLAUDE.mdの設計方針：P-IRRは需要家に非開示）"
        )
    if no_export:
        caveats.append("逆潮流禁止モードのため出力抑制（カーテイルメント）が発生する場合があります")
    if p["bifacial_enabled"]:
        caveats.append(
            "両面パネルモードのためinfinite_shedsモデル（GCR/パネル高/列間隔考慮）でPOAを計算しています。"
            f"積雪アルベド自動切替: {'ON（積雪時0.7/通常0.2）' if p['snow_albedo_enabled'] else 'OFF（常時0.2）'}"
        )
    if p["mg_enabled"]:
        caveats.append(
            "マイクログリッド事業の網内売電単価はPPA単価（PPA選択時）または電力量単価の"
            "加重平均（それ以外）で近似計算しています。実際の託送料金相当額・特定送配電事業の"
            "認可条件は考慮していません"
        )

    out = {
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
        "microgrid": microgrid_out,
        "caveats": caveats,
    }
    if wind_info is not None:
        pv_used = bool(p.get("faces"))
        out["wind"] = _wind_section(app, wind_info, gen_pv, demand_30min, sc_result, month_day, rate_params, pv_used)
        out["annual"]["pv_generation_kwh"] = round(result["annual"])
        out["annual"]["total_generation_kwh"] = round(result["annual"] + wind_info["annual_kwh"])
        out["annual"]["self_consumption_note"] = "self_consumption_kwh は敷地内の太陽光・蓄電池分＋届いた風力の合計"
        out["electricity_cost"]["annual_wind_ppa_payment_yen"] = round(wind_info["payment_yen"])
        out["electricity_cost"]["contract_power_note_wind"] = (
            "風力（送配電網で届く）では契約電力は下がらない。contract_power_after_kw は太陽光・蓄電池の効果のみ")
        out["caveats"] = list(out["caveats"]) + _WIND_CAVEATS
    if grid_cap_kw:
        # 上限を強制したか（LP）／強制せず判定のみか（蓄電池なし・ルールベース）でstatusが変わる
        status = "enforced" if sc_result.get("grid_import_cap_kw") else "not_enforced"
        # 導入後ピーク: 風力あり（オフサイト）は受電量（小売購入＋風力の配達分）の最大。なしは従来どおり系統購入
        after = wind_off["receive"] if wind_off is not None else sc_result["import_"]
        out["grid_cap"] = _grid_cap_section(
            grid_cap_kw, grid_cap_diag, status,
            peak_before_kw=float(np.max(demand_30min)) * 2.0,
            peak_after_kw=float(np.max(after)) * 2.0,
        )
    return out


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
    return _jsonable({
        "stations": [
            {"station_no": str(no), "name": name,
             "latitude": float(lat), "longitude": float(lon)}
            for no, name, lat, lon in rows
        ],
        "count": len(rows),
    })


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
        return _jsonable({
            "station_no": str(station_no),
            "station_name": name,
            "total_pv_kw": total_pv_kw,
            "annual_generation_kwh": round(g["annual"]),
            "capacity_factor_pct": round(g["annual"] / (total_pv_kw * 8760) * 100, 2) if total_pv_kw > 0 else None,
            "face_generation_kwh": [round(v) for v in g["face_annual"]],
            "monthly_generation_kwh": {str(m): round(g["monthly"].get(m, 0)) for m in range(1, 13)},
            "note": "JIS C 8907準拠（標準補正係数使用、片面パネル）。需要・蓄電池・電気料金・経済性は含まない",
        })
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
    bifacial_enabled: bool = False,
    bifaciality: float = 0.75,
    gcr: float = 0.4,
    panel_height_m: float = 2.0,
    pitch_m: float = 5.0,
    snow_albedo_enabled: bool = True,
    mg_enabled: bool = False,
    mg_line_distance_km: float = 2.0,
    mg_line_cost_yen_per_km: float = 30000000.0,
    mg_opex_ratio_pct: float = 2.0,
    mg_irr_period_years: int = 20,
    wind: dict = None,
    pv_enabled: bool = True,
) -> dict:
    """産業用PV+蓄電池シミュレーションのパラメータを検証する（即答）。

    **simulate_industrial_pv を呼ぶ前に必ずこのツールで検証し、返ってきた
    normalized_params をユーザーに提示して確認を得てから実行すること。**
    最適容量探索（2〜3分かかる蓄電池容量のグリッドサーチ）は本ツールでは扱わない
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
        bifacial_enabled: 両面パネルを使用するか（infinite_shedsモデルで背面日射を計算）
        bifaciality: 背面/前面効率比（bifacial_enabled時のみ有効。TOPCon 0.70〜0.80、
            HJT 0.85〜0.95が目安）
        gcr: 地面被覆率＝パネル高さ÷列間隔（bifacial_enabled時のみ有効、0.3〜0.5が一般的）
        panel_height_m: パネル中心地上高 [m]（bifacial_enabled時のみ有効）
        pitch_m: 列間隔 [m]（bifacial_enabled時のみ有効）
        snow_albedo_enabled: 積雪深データに応じてアルベドを自動切替するか
            （bifacial_enabled時のみ有効。ON=積雪時0.7/通常0.2、OFF=常時0.2）
        mg_enabled: マイクログリッド事業（モードB）を有効にするか。ONにするとPV余剰を
            網内（施設間）で融通し、束ねメリット・P-IRRを算出する事業者向け試算になる
            （モードA=自家消費/lease/ppaとは異なり、P-IRRをそのまま開示する）
        mg_line_distance_km: 自営線距離 [km]（mg_enabled時のみ有効）
        mg_line_cost_yen_per_km: 自営線単価 [円/km]（mg_enabled時のみ有効）
        mg_opex_ratio_pct: 年間運営コスト [%]（投資額比、mg_enabled時のみ有効）
        mg_irr_period_years: P-IRR計算期間 [年]（mg_enabled時のみ有効）
        wind: 風力発電（オフサイトPPA。送配電網で届く電源）。省略で風力なし（従来と同じ結果）。辞書で指定:
              {"capacity_kw": 1000} または {"coverage_pct": 100}（どちらか1つ。coverage_pct は年間の風力発電量を
              年間需要量の何%にするか）。任意: "cf_pct"（設備利用率[%]、既定29.1）、"ppa_price_yen_per_kwh"（PPA単価、既定11.96）、
              "wheeling_yen_per_kwh"（託送の電力量料金、既定=エリア×契約種別の一次資料の値）、
              "retail_fee_yen_per_kwh"（小売手数料、既定=推定値）。**観測地点（station_no）が北海道・東北のときだけ**使える
              （風力の調達エリアは需要地と同じ。list_wind_areas 参照）。届いた風力にも託送・賦課金・手数料がかかり、契約電力は
              下がらず、売電できるのは敷地内の太陽光の余剰だけ。系統受電上限（DC）とも併用できる（上限は届く風力も含めた
              受電量に対する上限。風力は上限を守る助けにならず、太陽光と蓄電池（lp_optimized）で守る）
        pv_enabled: 太陽光を使うか（既定true）。falseなら faces を使わず風力のみで計算（wind の指定が必要）

    Returns:
        dict: {"valid": bool, "normalized_params": {...}, "warnings": [...], "errors": [...]}
    """
    try:
        params, warnings, errors = _normalize_and_validate(
            station_no, _faces_arg(faces, pv_enabled), facilities, contract_type,
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
            bifacial_enabled, bifaciality, gcr, panel_height_m, pitch_m, snow_albedo_enabled,
            mg_enabled, mg_line_distance_km, mg_line_cost_yen_per_km,
            mg_opex_ratio_pct, mg_irr_period_years,
        )
        _apply_wind(params, warnings, errors, wind, pv_enabled, station_no, contract_type)
        runtime = "1-3秒" if not battery_enabled or battery_mode == "rule_based" else "5-20秒（LP最適化）"
        if bifacial_enabled:
            runtime += "。両面パネル計算のため数秒程度余分にかかる場合があります"
        return _jsonable({
            "valid": len(errors) == 0,
            "normalized_params": params,
            "warnings": warnings,
            "errors": errors,
            "estimated_runtime_seconds": runtime,
            "next_step": "normalized_params をユーザーに提示して確認後、"
                         "simulate_industrial_pv を同じ引数で呼び出す",
        })
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
    bifacial_enabled: bool = False,
    bifaciality: float = 0.75,
    gcr: float = 0.4,
    panel_height_m: float = 2.0,
    pitch_m: float = 5.0,
    snow_albedo_enabled: bool = True,
    mg_enabled: bool = False,
    mg_line_distance_km: float = 2.0,
    mg_line_cost_yen_per_km: float = 30000000.0,
    mg_opex_ratio_pct: float = 2.0,
    mg_irr_period_years: int = 20,
    wind: dict = None,
    pv_enabled: bool = True,
) -> dict:
    """産業用（高圧・特別高圧）太陽光＋蓄電池の需給・電気料金・投資回収を試算する
    （実行1〜3秒、蓄電池LP最適化時は5〜20秒）。

    JIS C 8907準拠の発電量計算（片面 or 両面パネル）、複数施設合算需要との
    自家消費シミュレーション（蓄電池はルールベース or LP最適化）、高圧/特別高圧
    電気料金の導入前後比較、投資額・補助金・投資回収年数、事業モデル
    （自己所有/リース/PPA、モードA）、マイクログリッド事業（網内売電・束ねメリット・
    P-IRR、モードB）を計算する。最適容量探索（2〜3分かかるグリッドサーチ）には対応しない。

    **事前に validate_industrial_params で検証し、パラメータをユーザーに
    確認してから呼び出すこと。** 引数の意味は validate_industrial_params と同一。

    風力（オフサイトPPA。送配電網で届く電源）を併用するには wind（辞書）を、風力のみにするには pv_enabled=false と
    wind を指定する（結果に wind 節と24/7の一致率が加わる）。wind の書式: {"capacity_kw": 1000}（契約容量）または
    {"coverage_pct": 100}（年間の風力発電量を年間需要量の何%にするか。どちらか1つ）。任意で "cf_pct"（設備利用率、既定29.1）、
    "ppa_price_yen_per_kwh"（既定11.96）、"wheeling_yen_per_kwh"（託送の電力量料金、既定=エリア×契約種別）、
    "retail_fee_yen_per_kwh"（小売手数料、既定=推定値）。**station_no が北海道・東北のときだけ**使える（list_wind_areas 参照）。
    届いた風力にも託送・賦課金・手数料がかかり、契約電力は下がらず、売電できるのは敷地内の太陽光の余剰だけ。
    詳細は validate_* の wind 引数。事前に estimate_wind_generation で風力単体の発電量を確認できる。

    Returns:
        dict: assumptions（入力エコー）/ annual（発電・自家消費・蓄電池・CO2）/
              electricity_cost（導入前後の電気料金比較）/ investment（投資額・回収年数）/
              business（事業モデル計算結果、モードA）/
              microgrid（mg_enabled時のみ、束ねメリット・P-IRR等、モードB）/
              caveats（免責事項）
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
        bifacial_enabled=bifacial_enabled, bifaciality=bifaciality, gcr=gcr,
        panel_height_m=panel_height_m, pitch_m=pitch_m, snow_albedo_enabled=snow_albedo_enabled,
        mg_enabled=mg_enabled, mg_line_distance_km=mg_line_distance_km,
        mg_line_cost_yen_per_km=mg_line_cost_yen_per_km,
        mg_opex_ratio_pct=mg_opex_ratio_pct, mg_irr_period_years=mg_irr_period_years,
        wind=wind, pv_enabled=pv_enabled,
    )
    if not v.get("valid"):
        return {"error": "パラメータ検証エラー", "errors": v.get("errors", []),
                "warnings": v.get("warnings", [])}
    try:
        out = _run_industrial_simulation(v["normalized_params"])
        out["validation_warnings"] = v.get("warnings", [])
        return _jsonable(out)
    except Exception as e:
        return {"error": str(e)}


def list_wind_areas() -> dict:
    """風力発電（オフサイトPPA）を使える調達エリアと、その地点・形状・既定値を返す（シミュレーション前の確認用）。

    風力は北海道・東北のみ。調達エリアは需要地（観測地点）と同じエリアに限る（越境託送はモデル化していない）。
    各エリアの風力の形状（一般送配電事業者のエリア需給実績 2025年の風力発電実績＋出力制御量を年平均1.0に正規化）の
    月別・時間帯別の傾向、対象の観測地点、託送の電力量料金の既定値を返す。

    Returns:
        dict: areas（エリアごとの code/name/stations/monthly_shape/diurnal_shape/curtailment_pct/wheeling_yen_per_kwh）、
              defaults（設備利用率・PPA単価・小売手数料の既定値と出典）、notes（限界・注意）
    """
    try:
        import sqlite3
        app = _get_app()
        con = sqlite3.connect(app.DB_PATH)
        names = {str(r[0]): r[1] for r in con.execute("select point_no, point_name from points")}
        con.close()
        days = _STD_DAYS_IN_MONTH
        areas = []
        for code, meta in app.WIND_AREA_META.items():
            shape = app.load_wind_shape(code)                       # (365, 48)。年平均 1.0
            month_mean, i = [], 0
            for d in days:
                month_mean.append(round(float(shape[i:i + d].mean()), 3))
                i += d
            diurnal = [round(float(shape[:, 2 * h:2 * h + 2].mean()), 3) for h in range(24)]
            areas.append({
                "code": code, "name": meta["name"],
                "stations": [{"station_no": pn, "name": names.get(pn)}
                             for pn, a in app.WIND_STATION_AREA.items() if a == code],
                "monthly_shape": month_mean,
                "diurnal_shape": diurnal,
                "curtailment_pct": meta["curtail_pct"],
                "shape_max": meta["shape_max"],
                "wheeling_yen_per_kwh": {
                    "high_voltage": app.WIND_WHEELING_ENERGY_YEN[(code, "高圧")],
                    "extra_high_voltage": app.WIND_WHEELING_ENERGY_YEN[(code, "特別高圧")],
                },
            })
        return _jsonable({
            "areas": areas,
            "defaults": {
                "cf_pct": app.WIND_CF_DEFAULT_PCT,
                "cf_source": "資源エネルギー庁 調達価格等算定委員会 第112回（2026年1月）。陸上風力（新設）の想定値",
                "ppa_price_yen_per_kwh": app.WIND_PPA_PRICE_DEFAULT,
                "ppa_price_source": "同 第112回。2025年度入札の平均落札価格（PPAの相対契約の単価そのものではない）",
                "retail_fee_yen_per_kwh": {"high_voltage": app.WIND_RETAIL_FEE_YEN["高圧"],
                                           "extra_high_voltage": app.WIND_RETAIL_FEE_YEN["特別高圧"]},
                "retail_fee_source": "自然エネルギー財団の推定（2023年度・全国平均）。公的な料金表はない",
                "wheeling_source": "北海道電力ネットワーク／東北電力ネットワーク 託送料金（標準接続送電、2025年10月〜、税込表示）",
            },
            "notes": [
                "monthly_shape は月ごとの平均（年平均=1.0）。冬に強く夏に弱い。diurnal_shape は0〜23時の平均でほぼ平坦",
                _WIND_CAVEATS[1],
                "風力は北海道・東北の観測地点でのみ使える。simulate_industrial_pv / simulate_dc の wind 引数で指定する",
            ],
        })
    except Exception as e:
        return {"error": str(e)}


_STD_DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def estimate_wind_generation(
    station_no: str = "34392",
    capacity_kw: float = 1000.0,
    cf_pct: float = None,
) -> dict:
    """風力発電（オフサイトPPA）の年間・月別発電量を、需要やPVと切り離して試算する（estimate_pv_generation の風力版）。

    風力(t) = min(契約容量 × 設備利用率 × 形状(t), 契約容量)。形状は station_no のエリア（北海道／東北）の
    エリア需給実績（2025年）から。**観測地点が北海道・東北のときだけ**使える。

    Args:
        station_no: 観測地点番号（list_wind_areas の stations から選ぶ。例: 34392=仙台, 14163=札幌）
        capacity_kw: 契約容量 [kW]（0 < x ≦ 1,000,000）
        cf_pct: 設備利用率 [%]（省略で29.1。出典: 調達価格等算定委員会 第112回の陸上風力（新設）想定値）

    Returns:
        dict: area / annual_kwh / monthly_kwh（12個）/ effective_cf_pct / clipped_kwh（定格で頭打ちになった分）/ notes
    """
    try:
        app = _get_app()
        area = app.station_to_wind_area(str(station_no).strip())
        if area is None:
            return {"error": f"風力は北海道・東北の観測地点でのみ使えます（station_no={station_no}）。"
                             "対象地点は list_wind_areas で確認してください"}
        try:
            cap = float(capacity_kw)
        except (TypeError, ValueError):
            return {"error": f"capacity_kw は数値で指定してください（{capacity_kw!r}）"}
        if not (np.isfinite(cap) and 0 < cap <= 1e6):
            return {"error": f"capacity_kw は 0 < x ≦ 1,000,000 で指定してください（{capacity_kw}）"}
        cf = app.WIND_CF_DEFAULT_PCT if cf_pct is None else cf_pct
        try:
            cf = float(cf)
        except (TypeError, ValueError):
            return {"error": f"cf_pct は数値で指定してください（{cf_pct!r}）"}
        if not (np.isfinite(cf) and 0 < cf <= 100):
            return {"error": f"cf_pct は 0 < x ≦ 100 で指定してください（{cf_pct}）"}
        w = app.build_wind_30min(area, cap, cf)
        month_day = [(m, d) for m, n in enumerate(_STD_DAYS_IN_MONTH, start=1) for d in range(1, n + 1)]
        monthly = app.monthly_sums(w["gen_30min"], month_day)
        return _jsonable({
            "station_no": str(station_no), "area": w["area_name"], "capacity_kw": cap, "cf_pct": cf,
            "annual_kwh": round(w["annual_kwh"]),
            "monthly_kwh": [round(monthly[m]) for m in range(1, 13)],
            "effective_cf_pct": round(w["effective_cf_pct"], 2),
            "clipped_kwh": round(w["clipped_kwh"]),
            "notes": [_WIND_CAVEATS[1],
                      "設備利用率が高いと形状のピークが契約容量を超え、定格で頭打ちになる（clipped_kwh）"],
        })
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# データセンター（Phase 7 段階4）
# ============================================================
# 需要をIT負荷×PUEから生成し、以降（PV・蓄電池・電気料金・経済性）は産業用と同じ計算経路を使う。
# 受電上限（系統からの受電を電圧階層の境界内に収める制約）は蓄電池LPでのみ強制できる。
# 設計書: docs/design_spec.md §5-7（需要）・§7（受電上限）・§10（MCP）

def _normalize_dc_params(
    workload, capacity_mode, size_preset, it_capacity_kw, n_racks, kw_per_rack,
    profile, noise_level, it_load_factor_pct, it_peak_pct, it_bottom_pct, it_peak_hour,
    pue, grid_cap, grid_cap_kw,
):
    """DC入力を検証・正規化し (dc, warnings, errors) を返す。dc は正規化済みの入力エコー。"""
    app = _get_app()
    m = _dc_maps(app)
    errors = []
    warnings = []

    def num(name, v, lo, hi, lo_open=True):
        """数値の範囲検査。範囲外・非数値は errors に積んで None を返す。"""
        try:
            x = float(v)
        except (TypeError, ValueError):
            errors.append(f"{name} は数値で指定してください")
            return None
        if not np.isfinite(x) or (x <= lo if lo_open else x < lo) or x > hi:
            errors.append(f"{name} は{lo:g}{'より大きく' if lo_open else '以上'}{hi:g}以下で指定してください")
            return None
        return x

    def choice(name, v, mapping):
        if v not in mapping:
            errors.append(f"{name} は {list(mapping.keys())} から選択してください")
            return False
        return True

    dc = {"workload": workload, "capacity_mode": capacity_mode}

    # --- 用途（プロファイル・ノイズを一括設定。manualのときだけ個別指定を使う） ---
    workload_ok = choice("workload", workload, m["workload"])
    resolved_profile = resolved_noise = None
    if workload_ok:
        preset = app.DC_WORKLOAD_PRESETS.get(m["workload"][workload])
        if preset:
            resolved_profile, resolved_noise = preset["profile"], preset["noise"]
            dc["profile"] = None
            dc["noise_level"] = None
        else:  # manual
            if choice("profile", profile, m["profile"]):
                resolved_profile = m["profile"][profile]
            if choice("noise_level", noise_level, m["noise_level"]):
                resolved_noise = m["noise_level"][noise_level]
            dc["profile"] = profile
            dc["noise_level"] = noise_level
    dc["resolved_profile"] = resolved_profile
    dc["resolved_noise_level"] = resolved_noise

    # --- 容量（3方式。内部では常に IT容量[kW] に正規化） ---
    dc["size_preset"] = dc["n_racks"] = dc["kw_per_rack"] = None
    resolved_it_kw = None
    size_preset_warning = None   # 入力にエラーがあるときは出さない（末尾で判定。エラー時は直す点だけを伝える）
    if choice("capacity_mode", capacity_mode, m["capacity_mode"]):
        if capacity_mode == "size_preset":
            if choice("size_preset", size_preset, m["size_preset"]):
                dc["size_preset"] = size_preset
                resolved_it_kw = float(app.DC_SIZE_PRESETS[m["size_preset"][size_preset]]["it_capacity_kw"])
                size_preset_warning = ("規模プリセットの区分（エッジ300kW/小規模1,000kW/中規模5,000kW/"
                                       "ハイパースケール40,000kW）は暫定値です（公開された定義に基づく数値ではありません）")
        elif capacity_mode == "it_capacity":
            resolved_it_kw = num("it_capacity_kw", it_capacity_kw, 0, MAX_IT_CAPACITY_KW)
        else:  # rack_density
            n = num("n_racks", n_racks, 0, MAX_RACKS)
            d = num("kw_per_rack", kw_per_rack, 0, MAX_KW_PER_RACK)
            if n is not None and d is not None:
                dc["n_racks"], dc["kw_per_rack"] = int(n), d
                resolved_it_kw = n * d
                if resolved_it_kw > MAX_IT_CAPACITY_KW:
                    errors.append(f"ラック数×密度 = {resolved_it_kw:,.0f}kW が上限 {MAX_IT_CAPACITY_KW:,.0f}kW を超えています")
                    resolved_it_kw = None
    dc["it_capacity_kw"] = resolved_it_kw

    # --- 負荷率・PUE ---
    dc["it_load_factor_pct"] = num("it_load_factor_pct", it_load_factor_pct, 0, 100)
    pue_v = num("pue", pue, 0, 5.0)
    if pue_v is not None and pue_v < 1.0:
        errors.append("pue は1.0以上で指定してください（PUE＝施設全体電力÷IT機器電力のため1.0が下限）")
        pue_v = None
    if pue_v is not None and pue_v > 2.0:
        warnings.append(f"PUE {pue_v:g} は空冷DCとしてはかなり高い値です（一般に1.2〜1.6程度）")
    dc["pue"] = pue_v

    # --- 日変動の形状（プロファイルが日変動のときだけ使う） ---
    dc["it_peak_pct"] = dc["it_bottom_pct"] = dc["it_peak_hour"] = None
    if resolved_profile == app.PROFILE_DIURNAL:
        pk = num("it_peak_pct", it_peak_pct, 0, 100)
        bt = num("it_bottom_pct", it_bottom_pct, 0, 100, lo_open=False)
        hr = num("it_peak_hour", it_peak_hour, 0, 23, lo_open=False)
        if pk is not None and bt is not None and bt > pk:
            errors.append("it_bottom_pct は it_peak_pct 以下で指定してください")
        dc["it_peak_pct"], dc["it_bottom_pct"] = pk, bt
        dc["it_peak_hour"] = int(hr) if hr is not None else None

    # --- 系統受電上限 ---
    dc["grid_cap"] = grid_cap
    dc["grid_cap_kw"] = None
    if choice("grid_cap", grid_cap, m["grid_cap"]):
        label = m["grid_cap"][grid_cap]
        if label in app.GRID_CAP_PRESETS_KW:
            dc["grid_cap_kw"] = float(app.GRID_CAP_PRESETS_KW[label])
        elif grid_cap == "manual":
            dc["grid_cap_kw"] = num("grid_cap_kw", grid_cap_kw, 0, MAX_GRID_CAP_KW)
    if size_preset_warning and not errors:
        warnings.insert(0, size_preset_warning)
    return dc, warnings, errors


def _dc_args_from_params(dc):
    """正規化済みDC入力を app.resolve_dc_demand の dc_args（UIの入力と同じ形）に戻す。"""
    app = _get_app()
    m = _dc_maps(app)
    return {
        "workload_preset": m["workload"][dc["workload"]],
        "capacity_mode": m["capacity_mode"][dc["capacity_mode"]],
        "size_preset": m["size_preset"].get(dc["size_preset"]),
        "it_capacity_kw": dc["it_capacity_kw"],
        "n_racks": dc["n_racks"],
        "kw_per_rack": dc["kw_per_rack"],
        "profile_mode": dc["resolved_profile"],
        "noise_level": dc["resolved_noise_level"],
        "it_load_factor_pct": dc["it_load_factor_pct"],
        "it_peak_pct": dc["it_peak_pct"],
        "it_bottom_pct": dc["it_bottom_pct"],
        "it_peak_hour": dc["it_peak_hour"],
        "pue_const": dc["pue"],
        "grid_cap_mode": m["grid_cap"][dc["grid_cap"]],
        "grid_cap_kw": dc["grid_cap_kw"],
    }


def _dc_summary(dc_info):
    """DC需要の要約（JSON）。UIの「データセンター需要」節（app.format_dc_summary）と同じ内容。"""
    app = _get_app()
    bd = dc_info["breakdown"]
    peak = dc_info["peak_demand_kw"]
    it_kw = dc_info["it_load_30min"] * 2.0
    inv_profile = {v: k for k, v in _dc_maps(app)["profile"].items()}
    inv_noise = {v: k for k, v in _dc_maps(app)["noise_level"].items()}
    return {
        "it_capacity_kw": round(dc_info["it_capacity_kw"], 1),
        "load_profile": inv_profile.get(dc_info["profile_mode"], dc_info["profile_mode"]),
        "noise_level": inv_noise.get(dc_info["noise_level"], dc_info["noise_level"]),
        "it_load_factor_pct": dc_info["it_load_factor_pct"],
        "pue": dc_info["pue_const"],
        "it_load_kw": {"mean": round(float(it_kw.mean()), 1), "min": round(float(it_kw.min()), 1),
                       "max": round(float(it_kw.max()), 1)},
        "annual_it_kwh": round(bd["annual_it_kwh"]),
        "annual_facility_kwh": round(bd["annual_total_kwh"]),
        "annual_overhead_kwh": round(bd["annual_overhead_kwh"]),
        "effective_avg_pue": round(bd["avg_pue"], 3),
        "peak_demand_kw": round(peak, 1),
        "voltage_class_hint": app.VOLTAGE_CLASS_LABELS[app.resolve_voltage_class(peak)],
        "suggested_application_capacity_kw": round(peak / app.CEC_UTILIZATION_FACTOR),
        "suggested_application_capacity_note":
            f"施設最大需要÷{app.CEC_UTILIZATION_FACTOR:.2f}（CEC 2025 IEPR の utilization factor 67%の逆算）。"
            "67%は観測された上限であり典型値ではないため、実際の申請容量はこれより大きくなる可能性がある",
    }


def _dc_caveats(dc, size_preset_used):
    """DCツール共通の免責。"""
    c = [
        "需要はIT負荷×PUE（一定値）から生成した仮想需要であり、実測のDC負荷ではありません。"
        "水準（IT負荷率）はサイト固有の値で、ユーザー入力に依存します",
        "PUEは一定値です（気温連動PUEは未実装）。空冷前提で、液冷・排熱利用は対象外です",
        "電気料金は産業用と共通です（既定は東京電力EPの公表値。北海道電力などのタリフ表は連動していません）。"
        "契約先の単価を tariff 引数で指定してください",
        "マイクログリッド事業（mg_*）はデータセンターツールでは扱いません",
    ]
    if size_preset_used:
        c.append("規模プリセットの区分は暫定値です（公開された定義に基づく数値ではありません）")
    if dc.get("resolved_profile") and "CEC" in dc["resolved_profile"]:
        c.append("CEC実測形状は米国の商用DC（PG&E管内約100施設）由来で、日本のDCの実測ではありません")
    return c


def _dc_month_index():
    """365日の各日が属する月（1-12）。NEDO METPV-20は非うるう年の代表年。"""
    days = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return np.repeat(np.arange(1, 13), days)


def estimate_dc_demand(
    workload: str = "housing",
    capacity_mode: str = "size_preset",
    size_preset: str = "medium",
    it_capacity_kw: float = 1000.0,
    n_racks: int = 100,
    kw_per_rack: float = 10.0,
    profile: str = "cec",
    noise_level: str = "low",
    it_load_factor_pct: float = 80.0,
    it_peak_pct: float = 90.0,
    it_bottom_pct: float = 50.0,
    it_peak_hour: int = 14,
    pue: float = 1.40,
) -> dict:
    """データセンターの電力需要（IT負荷×PUE）だけを見積もる（PV・電気料金・経済性を含まない、1秒未満）。

    施設全体の年間電力量・ピークデマンド・受電電圧区分の目安・申請受電容量の目安・月別電力量・
    平均的な1日の需要カーブを返す。「規模や用途を変えると需要はどうなるか」を軽く比較するのに使う。
    IT負荷率（年平均）は水準、負荷プロファイルは形状（年平均=1.0に正規化）で、両者を分けて扱う。

    Args:
        workload: 用途。housing(ハウジング/コロケーション: CEC実測形状) / ai_training(AI学習: 定常) /
            ai_inference(AI推論: CEC形状＋高ノイズ) / in_house_server_room(自社サーバー室: 日変動) /
            manual(profile・noise_levelを個別指定)
        capacity_mode: 容量の指定方法。size_preset(規模プリセット) / it_capacity(IT容量を直接入力) /
            rack_density(ラック数×ラック電力密度)。延床面積では指定しない（DCはラック密度で電力密度が1桁変わるため）
        size_preset: 規模（capacity_mode=size_preset時のみ）。edge=300kW / small=1,000kW / medium=5,000kW /
            hyperscale=40,000kW（**区分は暫定値**）
        it_capacity_kw: IT定格容量 [kW]（capacity_mode=it_capacity時のみ）
        n_racks: ラック数（capacity_mode=rack_density時のみ）
        kw_per_rack: ラック電力密度 [kW/ラック]（capacity_mode=rack_density時のみ。
            目安: 従来型4〜8 / 高密度10〜20 / AI・GPU 40〜130）
        profile: 負荷プロファイル（workload=manual時のみ）。cec(CEC実測形状) / flat(定常) / diurnal(日変動)
        noise_level: 短周期変動（workload=manual時のみ）。none / low(3〜7%) / high(12〜18%)
        it_load_factor_pct: IT負荷率（年平均）[%]。水準。サイト固有の値
        it_peak_pct: ピーク負荷率 [%]（日変動プロファイルのときのみ）
        it_bottom_pct: ボトム負荷率 [%]（同上）
        it_peak_hour: ピーク時刻 [時]（同上）
        pue: PUE（施設全体電力÷IT機器電力）。一定値。既存DCの実績値があればそれを入力

    Returns:
        dict: datacenter（需要の要約）/ monthly_facility_kwh / average_daily_profile_kw（48コマ）/
              assumptions / warnings / caveats。入力が不正なら {"error", "errors"}
    """
    try:
        dc, warnings, errors = _normalize_dc_params(
            workload, capacity_mode, size_preset, it_capacity_kw, n_racks, kw_per_rack,
            profile, noise_level, it_load_factor_pct, it_peak_pct, it_bottom_pct, it_peak_hour,
            pue, "none", None,
        )
        if errors:
            return {"error": "パラメータ検証エラー", "errors": errors}
        app = _get_app()
        demand, dc_info = app.resolve_dc_demand(_dc_args_from_params(dc))
        month_idx = _dc_month_index()
        monthly = {str(mo): round(float(demand[month_idx == mo].sum())) for mo in range(1, 13)}
        dc_out = dict(dc)
        dc_out.pop("grid_cap"), dc_out.pop("grid_cap_kw")
        return _jsonable({
            "datacenter": _dc_summary(dc_info),
            "monthly_facility_kwh": monthly,
            "average_daily_profile_kw": [round(float(v), 1) for v in demand.mean(axis=0) * 2.0],
            "average_daily_profile_note": "1年平均の1日の施設需要[kW]。48点（0:00〜23:30の30分刻み）",
            "assumptions": dc_out,
            "warnings": warnings,
            "caveats": _dc_caveats(dc, capacity_mode == "size_preset"),
        })
    except Exception as e:
        return {"error": str(e)}


def validate_dc_params(
    station_no: str = "44132",
    faces: list = [{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    workload: str = "housing",
    capacity_mode: str = "size_preset",
    size_preset: str = "medium",
    it_capacity_kw: float = 1000.0,
    n_racks: int = 100,
    kw_per_rack: float = 10.0,
    profile: str = "cec",
    noise_level: str = "low",
    it_load_factor_pct: float = 80.0,
    it_peak_pct: float = 90.0,
    it_bottom_pct: float = 50.0,
    it_peak_hour: int = 14,
    pue: float = 1.40,
    grid_cap: str = "none",
    grid_cap_kw: float = None,
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
    bifacial_enabled: bool = False,
    bifaciality: float = 0.75,
    gcr: float = 0.4,
    panel_height_m: float = 2.0,
    pitch_m: float = 5.0,
    snow_albedo_enabled: bool = True,
    wind: dict = None,
    pv_enabled: bool = True,
) -> dict:
    """データセンター＋PV＋蓄電池シミュレーションのパラメータを検証する（即答）。

    **simulate_dc を呼ぶ前に必ずこのツールで検証し、返ってきた normalized_params をユーザーに提示して
    確認を得てから実行すること。** 需要はIT負荷×PUEから生成し、PV・蓄電池・電気料金・経済性は
    産業用（validate_industrial_params）と同じ計算経路を使う。最適容量探索と、マイクログリッド事業は扱わない。

    Args:
        station_no: 地点番号（list_stations で取得）
        faces: 太陽電池アレイ面のリスト（最大8面）。各要素は
            {"ppeak_kw", "tilt_deg", "azimuth_deg"（北=0,東=90,南=180,西=270）, "pcs_limit_kw"}
        workload: 用途。housing / ai_training / ai_inference / in_house_server_room / manual
            （詳細は estimate_dc_demand を参照）
        capacity_mode: 容量の指定方法。size_preset / it_capacity / rack_density
        size_preset: 規模（capacity_mode=size_preset時）。edge / small / medium / hyperscale（区分は暫定値）
        it_capacity_kw: IT定格容量 [kW]（capacity_mode=it_capacity時）
        n_racks: ラック数（capacity_mode=rack_density時）
        kw_per_rack: ラック電力密度 [kW/ラック]（capacity_mode=rack_density時）
        profile: 負荷プロファイル（workload=manual時）。cec / flat / diurnal
        noise_level: 短周期変動（workload=manual時）。none / low / high
        it_load_factor_pct: IT負荷率（年平均）[%]。水準。サイト固有の値
        it_peak_pct / it_bottom_pct / it_peak_hour: 日変動プロファイルのピーク%・ボトム%・ピーク時刻
        pue: PUE。一定値
        grid_cap: 系統受電上限。none(制限なし) / hv_under_2000kw(高圧6.6kVに収める＝1,999kW) /
            ehv_under_10000kw(22・33kVに収める＝9,999kW) / manual(grid_cap_kwで指定)。
            日本の受電電圧は契約電力で階層化される（高圧6.6kV:2,000kW未満／22・33kV:10,000kW未満／
            それ以上は66kV）。上限を**強制できるのは battery_mode=lp_optimized のときだけ**
            （蓄電池なし・rule_basedでは超過の判定と必要な蓄電池の目安のみ返す）
        grid_cap_kw: 受電上限 [kW]（grid_cap=manual時のみ）
        contract_type: 契約種別。high_voltage(高圧) / extra_high_voltage(特別高圧)。
            ピーク2,000kW以上は特別高圧が目安（食い違いは警告で知らせる）
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
            17,520コマの線形計画法で年間電気代を最小化。実行に数秒〜十数秒。受電上限を強制できるのはこちらのみ)
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
        bifacial_enabled: 両面パネルを使用するか（infinite_shedsモデルで背面日射を計算）
        bifaciality: 背面/前面効率比（bifacial_enabled時のみ有効）
        gcr: 地面被覆率＝パネル高さ÷列間隔（bifacial_enabled時のみ有効）
        panel_height_m: パネル中心地上高 [m]（bifacial_enabled時のみ有効）
        pitch_m: 列間隔 [m]（bifacial_enabled時のみ有効）
        snow_albedo_enabled: 積雪深データに応じてアルベドを自動切替するか（bifacial_enabled時のみ有効）

        wind: 風力発電（オフサイトPPA。送配電網で届く電源）。省略で風力なし（従来と同じ結果）。辞書で指定:
              {"capacity_kw": 1000} または {"coverage_pct": 100}（どちらか1つ。coverage_pct は年間の風力発電量を
              年間需要量の何%にするか）。任意: "cf_pct"（設備利用率[%]、既定29.1）、"ppa_price_yen_per_kwh"（PPA単価、既定11.96）、
              "wheeling_yen_per_kwh"（託送の電力量料金、既定=エリア×契約種別の一次資料の値）、
              "retail_fee_yen_per_kwh"（小売手数料、既定=推定値）。**観測地点（station_no）が北海道・東北のときだけ**使える
              （風力の調達エリアは需要地と同じ。list_wind_areas 参照）。届いた風力にも託送・賦課金・手数料がかかり、契約電力は
              下がらず、売電できるのは敷地内の太陽光の余剰だけ。系統受電上限（DC）とも併用できる（上限は届く風力も含めた
              受電量に対する上限。風力は上限を守る助けにならず、太陽光と蓄電池（lp_optimized）で守る）
        pv_enabled: 太陽光を使うか（既定true）。falseなら faces を使わず風力のみで計算（wind の指定が必要）

    Returns:
        dict: {"valid": bool, "normalized_params": {...}, "warnings": [...], "errors": [...],
               "estimated_runtime_seconds": str, "next_step": str}
    """
    try:
        dc, dc_warnings, dc_errors = _normalize_dc_params(
            workload, capacity_mode, size_preset, it_capacity_kw, n_racks, kw_per_rack,
            profile, noise_level, it_load_factor_pct, it_peak_pct, it_bottom_pct, it_peak_hour,
            pue, grid_cap, grid_cap_kw,
        )
        params, warnings, errors = _normalize_and_validate(
            station_no, _faces_arg(faces, pv_enabled), [], contract_type,
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
            bifacial_enabled, bifaciality, gcr, panel_height_m, pitch_m, snow_albedo_enabled,
            False, 0.0, 0.0, 0.0, 1,  # マイクログリッドはDCツールでは扱わない
            dc_mode=True,
        )
        _apply_wind(params, warnings, errors, wind, pv_enabled, station_no, contract_type)
        errors = dc_errors + errors
        warnings = dc_warnings + warnings
        params["demand_source"] = "datacenter"
        params["dc"] = dc

        # 需要を生成して、需要に依存する警告を出す（生成は1秒未満。入力の不備はここでも捕まえる）
        lp_on = bool(battery_enabled) and battery_mode == "lp_optimized"
        if not dc_errors:
            app = _get_app()
            try:
                _, dc_info = app.resolve_dc_demand(_dc_args_from_params(dc))
            except ValueError as e:
                errors.append(str(e))
                dc_info = None
            if dc_info is not None:
                peak = dc_info["peak_demand_kw"]
                suggest_ehv = app.resolve_voltage_class(peak) != "hv_6000"
                if contract_type == "high_voltage" and suggest_ehv:
                    warnings.append(
                        f"導入前ピーク {peak:,.0f}kW は2,000kW以上のため、契約種別は extra_high_voltage が目安です"
                        "（現在は high_voltage）")
                elif contract_type == "extra_high_voltage" and not suggest_ehv:
                    warnings.append(
                        f"導入前ピーク {peak:,.0f}kW は2,000kW未満のため、契約種別は high_voltage が目安です"
                        "（現在は extra_high_voltage）")
                cap = dc_info["grid_cap_kw"]
                if cap and wind is not None:
                    # 風力は受電点に届くので、受電上限は風力の配達分も含めて守る（LPは受電点の基準で最適化する）
                    warnings.append(
                        "風力を使うときの受電上限は、送配電網で届く風力も含めた受電量に対する上限です。"
                        "風力は上限を守る助けにならず（太陽光と蓄電池だけで守る）、契約電力も風力では下がりません")
                if cap:
                    if peak <= cap:
                        warnings.append(
                            f"受電上限 {cap:,.0f}kW は導入前ピーク {peak:,.0f}kW 以上のため、上限は拘束しません")
                    elif not lp_on:
                        warnings.append(
                            "受電上限は battery_enabled=true かつ battery_mode=lp_optimized のときだけ強制されます。"
                            "現在の設定では、導入後ピークが上限を超えるかの判定と必要な蓄電池の目安のみ返します")
                    else:
                        warnings.append(
                            "受電上限が厳しいと最適化（LP）に数十秒〜2分かかる場合があります。"
                            "上限を守れない条件では、LPを解かずに診断（理由と必要量の目安）を返します")

        runtime = "1-3秒" if not lp_on else ("5-20秒（LP最適化）" if not dc["grid_cap_kw"]
                                            else "10秒〜2分（LP最適化＋受電上限）")
        if bifacial_enabled:
            runtime += "。両面パネル計算のため数秒程度余分にかかる場合があります"
        return _jsonable({
            "valid": len(errors) == 0,
            "normalized_params": params,
            "warnings": warnings,
            "errors": errors,
            "estimated_runtime_seconds": runtime,
            "next_step": "normalized_params をユーザーに提示して確認後、simulate_dc を同じ引数で呼び出す",
        })
    except Exception as e:
        return {"valid": False, "errors": [str(e)], "warnings": []}


def simulate_dc(
    station_no: str = "44132",
    faces: list = [{"ppeak_kw": 500.0, "tilt_deg": 30.0, "azimuth_deg": 180.0, "pcs_limit_kw": 500.0}],
    workload: str = "housing",
    capacity_mode: str = "size_preset",
    size_preset: str = "medium",
    it_capacity_kw: float = 1000.0,
    n_racks: int = 100,
    kw_per_rack: float = 10.0,
    profile: str = "cec",
    noise_level: str = "low",
    it_load_factor_pct: float = 80.0,
    it_peak_pct: float = 90.0,
    it_bottom_pct: float = 50.0,
    it_peak_hour: int = 14,
    pue: float = 1.40,
    grid_cap: str = "none",
    grid_cap_kw: float = None,
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
    bifacial_enabled: bool = False,
    bifaciality: float = 0.75,
    gcr: float = 0.4,
    panel_height_m: float = 2.0,
    pitch_m: float = 5.0,
    snow_albedo_enabled: bool = True,
    wind: dict = None,
    pv_enabled: bool = True,
) -> dict:
    """データセンター＋PV＋蓄電池の需給・電気料金・受電上限・投資回収を試算する
    （実行1〜3秒、蓄電池LP最適化時は5〜20秒、受電上限が厳しいLPは最大2分）。

    需要をIT負荷×PUEから生成し（datacenter節）、JIS C 8907準拠のPV発電、蓄電池（ルールベース or LP最適化）、
    高圧/特別高圧電気料金の導入前後比較、投資額・補助金・投資回収年数、事業モデル（自己所有/リース/PPA）、
    CO2削減量を計算する。**系統受電上限**（grid_cap）を指定すると、LP最適化では上限を制約として強制し
    （grid_cap節に導入後ピーク等）、守れない場合は grid_cap_infeasible=true と理由・必要な蓄電池の下限の
    目安を返す（エラーではなく診断結果）。

    **事前に validate_dc_params で検証し、パラメータをユーザーに確認してから呼び出すこと。**
    引数の意味は validate_dc_params と同一。
    風力（オフサイトPPA。送配電網で届く電源）を併用するには wind（辞書）を、風力のみにするには pv_enabled=false と
    wind を指定する（結果に wind 節と24/7の一致率が加わる）。wind の書式: {"capacity_kw": 1000}（契約容量）または
    {"coverage_pct": 100}（年間の風力発電量を年間需要量の何%にするか。どちらか1つ）。任意で "cf_pct"（設備利用率、既定29.1）、
    "ppa_price_yen_per_kwh"（既定11.96）、"wheeling_yen_per_kwh"（託送の電力量料金、既定=エリア×契約種別）、
    "retail_fee_yen_per_kwh"（小売手数料、既定=推定値）。**station_no が北海道・東北のときだけ**使える（list_wind_areas 参照）。
    届いた風力にも託送・賦課金・手数料がかかり、契約電力は下がらず、売電できるのは敷地内の太陽光の余剰だけ。
    詳細は validate_* の wind 引数。事前に estimate_wind_generation で風力単体の発電量を確認できる。

    Returns:
        dict: assumptions / datacenter（需要の要約）/ annual / electricity_cost / investment / business /
              grid_cap（grid_cap指定時のみ）/ caveats。受電上限を守れないときは
              {"grid_cap_infeasible": true, "message", "grid_cap", "datacenter", ...} で経済性は含まない
    """
    v = validate_dc_params(
        station_no=station_no, faces=faces,
        workload=workload, capacity_mode=capacity_mode, size_preset=size_preset,
        it_capacity_kw=it_capacity_kw, n_racks=n_racks, kw_per_rack=kw_per_rack,
        profile=profile, noise_level=noise_level, it_load_factor_pct=it_load_factor_pct,
        it_peak_pct=it_peak_pct, it_bottom_pct=it_bottom_pct, it_peak_hour=it_peak_hour,
        pue=pue, grid_cap=grid_cap, grid_cap_kw=grid_cap_kw,
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
        bifacial_enabled=bifacial_enabled, bifaciality=bifaciality, gcr=gcr,
        panel_height_m=panel_height_m, pitch_m=pitch_m, snow_albedo_enabled=snow_albedo_enabled,
        wind=wind, pv_enabled=pv_enabled,
    )
    if not v.get("valid"):
        return {"error": "パラメータ検証エラー", "errors": v.get("errors", []),
                "warnings": v.get("warnings", [])}
    try:
        app = _get_app()
        p = v["normalized_params"]
        dc = p["dc"]
        demand, dc_info = app.resolve_dc_demand(_dc_args_from_params(dc))
        cap = dc_info["grid_cap_kw"]
        caveats = _dc_caveats(dc, dc["capacity_mode"] == "size_preset")
        try:
            out = _run_industrial_simulation(p, demand_override=(demand, [demand]), grid_cap_kw=cap)
        except _GridCapInfeasible as e:
            battery = {
                "enabled": True, "mode_label": "最適充放電（LP）",
                "capacity_kwh": p["battery_capacity_kwh"],
                "max_charge_kw": p["battery_max_charge_kw"],
                "max_discharge_kw": p["battery_max_discharge_kw"],
            }
            return _jsonable({
                "grid_cap_infeasible": True,
                "reason": ("受電上限を守れないことが必要条件の診断で確定（LPは実行していない）"
                           if e.status == "infeasible" else
                           "診断の必要条件は満たすがLPが実行不可能（主に最大充電電力の不足）"),
                "message": app.format_grid_cap(dc_info, e.diag, e.status, battery=battery),
                "grid_cap": _grid_cap_section(cap, e.diag, e.status, dc_info["peak_demand_kw"]),
                "datacenter": _dc_summary(dc_info),
                "assumptions": p,
                "next_step": "上限を上げる／蓄電池の容量・充放電電力を増やす／PV容量を増やす／"
                             "IT負荷（容量・負荷率）を下げる、のいずれかで再検証する",
                "caveats": caveats,
                "validation_warnings": v.get("warnings", []),
            })
        ann = out["annual"]
        if p["battery_enabled"] and ann["battery_charge_kwh"] == 0 and ann["battery_discharge_kwh"] == 0:
            caveats.append(
                "蓄電池の充放電がゼロです。DCの需要が平坦（24時間一定）だと契約電力を下げられず、料金にも日内の差"
                "（時間帯別単価）がないため、蓄電池の価値は構造的にゼロになりえます（バグではありません）。"
                "CEC実測形状／日変動の選択、PV容量を増やして余剰を作る、受電上限（grid_cap）の指定で価値が出ます")
        result = {"assumptions": out.pop("assumptions"), "datacenter": _dc_summary(dc_info)}
        result.update(out)
        result["caveats"] = list(out["caveats"]) + caveats
        result["validation_warnings"] = v.get("warnings", [])
        return _jsonable(result)
    except Exception as e:
        return {"error": str(e)}
