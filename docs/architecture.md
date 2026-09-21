# Architecture & Implementation Notes / 実装ドキュメント

This document describes the internal architecture, design decisions, and implementation details of **pv-sim-biz** (Industrial Solar PV & Battery Simulator).

本ドキュメントは pv-sim-biz の内部アーキテクチャ・設計判断・実装詳細を解説します。

---

## 1. Overview / 概要

pv-sim-biz is a standalone Gradio web application that simulates the energy balance and economics of industrial-scale solar PV + battery storage systems for high-voltage and extra-high-voltage customers in Japan. It is forked from the residential version (`pv-sim-gh`) and adapted to industrial use cases.

The application supports two business modes within a single UI:

- **Mode A — Self-consumption (Lease/PPA sales tool)**: For solar/EPC sales teams creating proposals to end customers.
- **Mode B — Microgrid (MG project planning tool)**: For microgrid project planners evaluating investment decisions.

産業用（高圧・特別高圧）需要家向けの太陽光＋蓄電池シミュレーター。家庭用版（`pv-sim-gh`）からフォークし、産業用に特化。1つのUI内でモードA（自家消費型・営業ツール）とモードB（マイクログリッド・事業検討ツール）の2モードを提供します。

---

## 2. Tech Stack / 技術スタック

| Layer | Technology |
|---|---|
| Language | Python 3.10+ |
| UI Framework | Gradio 6.6 |
| Plotting | Plotly |
| Solar Modeling | pvlib (Erbs decomposition, isotropic transposition, `bifacial.infinite_sheds`) |
| Optimization | PuLP + CBC (linear programming) |
| Data Storage | SQLite (`radiation.db`, NEDO METPV-20 dataset) |
| Numerical | pandas, numpy |
| Deployment | Hugging Face Spaces |

---

## 3. File Structure / ファイル構成

```
pv-sim-biz/
├── app.py                # Standalone main application
├── mcp_tools.py          # MCP tool definitions (validate/simulate API layer)
├── radiation.db          # NEDO METPV-20 weather DB (50 sites, 10 elements, Git LFS)
├── comstock_*.csv        # 6 industrial demand presets (per-m² intensity)
├── wind_shape.csv        # Wind output shape, Hokkaido/Tohoku (2025 half-hourly, mean 1.0)
├── tools/build_wind_shape.py  # Dev script that builds wind_shape.csv (not needed at runtime)
├── requirements.txt
├── README.md             # HF Spaces metadata + project description
├── LICENSE               # MIT
└── docs/
    └── architecture.md   # This file
```

The application is designed as a **standalone single-file** (`app.py`), with weather and demand data files as the only required external dependencies. This simplifies deployment and version management.

---

## 4. Core Generation Model (JIS C 8907) / 発電量モデル

The application implements the JIS C 8907:2005 standard for PV power generation estimation in Japan.

### 4.1 Time Resolution

- **Granularity**: 30 minutes × 365 days = **17,520 timeslots per year**
- **Per-array support**: Up to 8 PV arrays with independent tilt, azimuth, and PCS output limits
- **Per-array PCS clipping**: Each array is clipped to its PCS rated output before aggregation

### 4.2 Irradiance Conversion

GHI (global horizontal irradiance) from the NEDO METPV-20 database is converted to POA (plane-of-array) irradiance via pvlib:

1. **DNI/DHI decomposition**: Erbs model (`pvlib.irradiance.erbs`)
2. **Tilted-plane transposition**: Isotropic sky model (`pvlib.irradiance.get_total_irradiance`)
3. **Bifacial gain (optional)**: `pvlib.bifacial.infinite_sheds.get_irradiance` for two-sided panels

### 4.3 Temperature Correction

JIS C 8907 temperature correction is applied per timeslot using hourly ambient temperature from the database:

```
P_corrected = P_nominal × (1 + α × (T_cell − 25))
```

Where `α` is the temperature coefficient (default −0.41 %/°C) and `T_cell = T_ambient + ΔT` (default ΔT = 18.4°C for crystalline silicon).

### 4.4 Bifacial Panel Support

When bifacial mode is enabled, the rear-side irradiance is computed via `pvlib.bifacial.infinite_sheds`. Snow depth data from NEDO METPV-20 (element #9) drives **dynamic albedo switching**:

- Snow depth ≥ threshold → albedo = 0.7 (snow)
- Otherwise → albedo = 0.2 (default ground)

Default parameters: `bifaciality=0.75`, `GCR=0.4`, ground height=2.0 m, pitch=5.0 m.

---

## 5. Industrial Demand Presets / 産業用需要プリセット

The 6 demand presets are derived from the **NREL ComStock EULP** dataset (ASHRAE Climate Zone 4A, equivalent to Tokyo). Each preset is normalized to **kWh per m² floor area** and stored as `comstock_*.csv`.

| # | ComStock Type | Japanese Use Case | Default Floor Area |
|---|---|---|---|
| 1 | Medium Office | 役所・自治体庁舎 | 4,500 m² |
| 2 | Primary School | 公立小学校 | 6,900 m² |
| 3 | Secondary School | 公立中学校・高校 | 19,600 m² |
| 4 | Hospital | 公立病院 | 22,400 m² |
| 5 | Large Hotel | ホテル | 11,300 m² |
| 6 | Retail | コンビニ・小売店 | 2,300 m² |

### 5.1 Multi-facility Aggregation

For microgrid scenarios, users can combine multiple facility types:

```
Building 1: Medium Office × 1   floor area: 4500 m²
Building 2: Primary School × 2  floor area: 6900 m²
─────────────
Combined demand curve (30-min × 365 days)
```

- Each preset is a 30-minute time series → aggregated on the same time axis
- Scaled by building count and floor area
- The combined curve and the per-facility individual curves are both retained for **bundling-merit calculation** in MG mode

### 5.2 Custom CSV Upload

Users with their own 30-minute demand data can upload a CSV to add to or replace the preset-based curve.

---

## 6. Electricity Tariff Model / 電気料金体系

### 6.1 High-voltage (HV) and Extra-high-voltage (EHV)

Default tariff values (Tokyo Electric Power EP):

| Item | HV (高圧) | EHV (特別高圧) |
|---|---|---|
| Basic charge / 基本料金 | 1,890 ¥/kW | 1,770 ¥/kW |
| Energy charge (summer) / 夏季電力量料金 | 19.93 ¥/kWh | 18.48 ¥/kWh |
| Energy charge (other) / 他季電力量料金 | 18.77 ¥/kWh | 17.47 ¥/kWh |
| Renewable surcharge / 再エネ賦課金 | 4.18 ¥/kWh | 4.18 ¥/kWh |
| Power factor / 力率 | 85% | 85% |

All values are user-editable in the UI.

### 6.2 Demand Tracking

The contracted demand (`契約電力`) is estimated as the **maximum of 30-minute demand values** over the year (実量制 actual-measurement basis). The application tracks per-month maximum demand and visualizes the before/after comparison as a bar chart.

### 6.3 Substation Construction Cost

When upgrading from HV to EHV, a one-time construction cost is added: default **27,500 ¥/kVA × contracted kVA**.

### 6.4 Industrial FIT

- **FIT-enabled**: Automatic price switching by year
  - 1–5 years: ¥19/kWh
  - 6–20 years: ¥8.3/kWh
  - 21+ years: ¥8.5/kWh
- **FIT-disabled**: Default ¥8.50/kWh (editable)
- **Reverse-power-flow prohibition**: Battery charge prioritized → curtailment when full

---

## 7. Battery Dispatch Optimization / 蓄電池最適充放電

The application provides three battery modes:

1. **Rule-based** (`simulate_battery`): Greedy charge/discharge with surplus/deficit logic
2. **LP optimization** (`optimize_battery`): PuLP/CBC linear programming
3. **Optimal capacity search** (`optimize_battery_capacity` + grid search): Two-stage approach

### 7.1 LP Formulation

The LP minimizes annual electricity cost over all 17,520 timeslots:

**Objective**:
```
minimize: basic_charge × peak_demand × 12 × pf_factor
        + Σ (grid_import[t] × unit_price[t])
        − Σ (grid_export[t] × sell_price)
```

**Decision variables**:
- `charge[t]`, `discharge[t]` ∈ [0, max_per_slot]
- `grid_import[t]`, `grid_export[t]` ≥ 0
- `curtailment[t]` ≥ 0 (PV surplus when no_export mode)
- `soc[t]` ∈ [SOC_min, SOC_max]
- `peak_demand` ≥ 0 (linearized via per-slot constraints)

**Constraints**:
- Energy balance: `grid_import[t] + gen[t] + discharge[t] == demand[t] + charge[t] + grid_export[t] + curtailment[t]`
- Peak demand linearization: `peak_demand ≥ grid_import[t] / dt` for all t
- SOC transition: `soc[t] = soc[t−1] + charge[t] × η − discharge[t] / η`
- Terminal SOC equality: `soc[T−1] == soc[0]` (for fair year-over-year comparison)

**Solver**: CBC (bundled with PuLP). Typical solve time on a modest CPU is tens of seconds for the full 17,520-timeslot problem.

### 7.2 Peak Shaving

The peak demand `peak_demand` is **linearized** as a single LP variable bounded below by all 17,520 grid-import slot values divided by the time step. Minimizing the basic-charge term in the objective drives the LP to flatten the demand curve.

### 7.3 Optimal Capacity Search

The two-stage approach balances accuracy and runtime:

**Stage 1 — One-shot LP** (~15 seconds):
- Battery capacity becomes a decision variable: `capacity_var ∈ [0, capacity_upper]`
- SOC bounds become linear in capacity: `soc[t] ≤ capacity_var × soc_max_pct`
- Objective adds annualized battery investment: `+ capacity_var × battery_cost / payback_years`
- Returns the optimal capacity directly, equivalent to maximizing P-IRR

**Stage 2 — Grid search** (~2–3 minutes):
- Sweeps battery capacity around the Stage 1 optimum (e.g., 0–1500 kWh in 15–20 steps)
- For each capacity, runs the standard `optimize_battery` LP
- Records: annual cost saving, investment, P-IRR, payback years, contracted demand reduction, CO₂ reduction
- Visualizes the curves and marks the optimum

### 7.4 Subsidy Handling

PV and battery subsidies (as percentages) reduce the **net unit cost** before being passed to the LP and grid search. This ensures the optimal capacity correctly responds to subsidy levels.

---

## 8. Two Business Modes / 2つのビジネスモード

### 8.1 Mode A — Self-consumption (Lease/PPA)

**Persona**: Solar/EPC sales representatives creating customer proposals.

**Output**: Annual cost savings, payback years, CO₂ reduction. **P-IRR is not shown** (the operator's revenue structure is hidden from the customer).

**Lease/PPA reverse calculation**:
```
annual_lease = investment × CRF
             = investment × r(1+r)^N / ((1+r)^N − 1)
PPA_unit_price = annual_lease / annual_self_consumption
```
where `r` = target P-IRR, `N` = payback years.

When MG is enabled, the investment base includes the full MG cost (PV + battery + distribution line) and operating cost is folded into the lease/PPA pricing.

### 8.2 Mode B — Microgrid (MG)

**Persona**: Microgrid project planners and 特定送配電事業 (specified distribution business) evaluators.

**Output**: Project IRR (P-IRR) and annual cash flow.

**MG-specific inputs**:
- Self-built distribution line distance (default 2 km)
- Distribution line unit cost (default 30 M¥/km)
- Annual operating cost (default 2% of initial cost)
- P-IRR calculation period (default 20 years)

**Revenue model**:
- PV-to-grid (intra-MG): Priced at PPA unit price (when PPA mode) or energy charge (otherwise)
- **Bundling merit**: Difference between sum of individual facility basic charges and combined-MG basic charge
- Operating cost is included in the PPA pricing → MG P-IRR ≈ target P-IRR + bundling merit

---

## 9. CO₂ Reduction Calculation / CO2削減量

```
CO2_reduction [t-CO2/year] = grid_import_reduction [kWh/year] × emission_factor
```

Default emission factor: **0.000431 t-CO₂/kWh** (Japan grid average, user-editable).

---

## 10. Default Cost Parameters / デフォルト単価

| Item | Default Value |
|---|---|
| PV system cost / PVシステム単価 | 158,000 ¥/kW |
| Battery cost / 蓄電池単価 | 200,000 ¥/kWh |
| Substation construction / 受電設備工事費 | 27,500 ¥/kVA |
| MG distribution line / 自営線単価 | 30 M¥/km |
| Annual operating cost (MG) | 2% of initial cost |

All values are editable in the UI.

---

## 11. Implementation Notes / 実装上の注意

### 11.1 Standalone Architecture

`app.py` is intentionally kept as a single standalone file. The only external file dependencies are:

- `radiation.db` (Git LFS, ~20 MB)
- `comstock_*.csv` (6 demand preset files)

This simplifies deployment to Hugging Face Spaces and avoids module import complexities.

### 11.2 Numerical Input Handling

All numerical inputs use `is not None` checks (rather than truthy checks) to allow valid `0` inputs. For example:
```python
sell_price_val = sell_price if sell_price is not None else DEFAULT
```

### 11.3 Missing Data Handling

The NEDO METPV-20 dataset uses `8888` as a missing-value marker. The database loader (`load_from_db`) converts these to NaN before processing.

### 11.4 Calendar Base Year

The 30-minute time series is built on a non-leap year (2023) base to avoid Feb 29 indexing issues.

### 11.5 Battery LP — Pass-through Constraint

The current LP formulation does **not** include an explicit mutual exclusion constraint `charge[t] + discharge[t] ≤ max_power_per_slot`. Under the current objective (`unit_price > sell_price` always), this is theoretically safe because pass-through (simultaneous charge and discharge) costs SOC due to round-trip efficiency loss. Empirical verification confirms no pass-through occurs (`both_pos = 0` across all test cases). See `test_verify_battery_bug.py` for the verification script.

If future extensions add per-time-slot purchase prices, VPP/ancillary revenue, or symmetric SOC dynamics, the mutual exclusion constraint should be added defensively.

---

## 12. Roadmap / 今後の拡張余地

- **Subsidy presets**: Catalog of major Japanese PV/battery subsidy programs with auto-fill
- **Demand data refinement**: Replace NREL ComStock-based presets with Japan-native demand curves
- **Tariff library**: Multi-region tariff support (Kansai EP, Chubu EP, Kyushu EP, etc.)
- **Solver options**: Optional HiGHS solver for faster LP solves
- **Sensitivity analysis**: Tornado charts for input parameter sensitivity on P-IRR
- **Wind + grid receiving cap / optimal battery sizing** (W2d): add a "delivered wind" variable to the LP so that the grid cap and the sizing economics are evaluated at the receiving point
- **Offsite solar** (W6): the offsite-source list is already multi-source; add a generation site's weather to it

---

## 13. References / 参考文献

- **JIS C 8907:2005** — Estimation method of generating electric energy by PV power systems
- **pvlib-python** — https://pvlib-python.readthedocs.io/
- **NEDO METPV-20** — Japanese solar irradiance database
- **NREL ComStock EULP** — https://comstock.nrel.gov/page/datasets
- **PuLP** — https://coin-or.github.io/pulp/

---

## 14. Wind Power (Offsite PPA) / 風力発電（オフサイトPPA）

設計の正典は [`wind_design_spec.md`](wind_design_spec.md)、経緯は `decision_log.md` 第13段階。ここでは実装の要点だけを記す。

### 14.1 Model / モデル

```
wind(t) [kW] = min( capacity × capacity factor × shape(t), capacity )
```

- **水準と形状の分離**（データセンターの需要モデルと同じ）。形状は `wind_shape.csv`（北海道・東北。一般送配電事業者の「エリア需給実績」2025年の
  `風力発電実績 + 風力出力制御量` ＝出力制御前を年平均1.0に正規化）。設備利用率29.1%・PPA単価11.96円/kWhは調達価格等算定委員会（第112回）
- **METPV-20の風速は使わない**: 日射との日内相関が+0.78で、ハブ高への外挿指数の仮定（0.10〜0.30）で設備利用率が3倍動き、仮定が答えを決めてしまうため
- 対象エリアは北海道・東北のみ。需要地（観測地点）も同じエリアに限る（`WIND_STATION_AREA`。東北は「東北6県＋新潟」で8地点）

### 14.2 Offsite billing / オフサイトの料金計算

風力は送配電網で届くので、敷地内の太陽光とは経済性が違う（`offsite_receiving` / `offsite_cost_after`）。

```
受電量        R(t) = max(0, 需要 + 充電 − 放電 − 敷地内の太陽光)     ← 契約電力・受電上限の基準
小売から買う量      = 運転結果の系統購入 import_(t)
風力の配達量  D(t) = clip(R − import_, 0, 風力発電量)
導入後の電気代 = 基本料金(R の最大) + 電力量料金(小売から買う量) + Σ D × (託送の電力量料金 + 再エネ賦課金 + 小売手数料)
風力PPA支払    = 風力の年間発電量 × PPA単価         （pay-as-produced。無駄になった分も支払う）
```

- **契約電力は風力では下がらない**（受電点の最大は届いた風力も含む）。売電・出力抑制の対象は敷地内の太陽光の余剰だけ
- 蓄電池の**運転**は太陽光＋風力を合わせた発電で行い（風力の余剰を貯めて凪の時間に使う）、**料金だけ**受電点基準で計算し直す
- 託送の電力量料金は一次資料（北海道電力NW・東北電力NW、2025年10月〜、税込表示）。小売手数料は自然エネルギー財団の推定値
- マイクログリッド（W2b）: 収益＝網内に供給した全量×網内単価＋束ねメリット、費用＝運営コスト＋風力の調達費用。
  PPA（MG）の単価は（投資の回収＋風力の調達費用）÷ 網内に供給した全量で逆算

### 14.3 Outputs / 出力

主役は **量ベース達成率**（年間の発電量 ÷ 年間の需要量）と **時間一致率**（1 − 系統購入/需要。24/7の実力）。両者の差が
「年間では足りていてもその時間には足りていない分」。蓄電池なしの参考（太陽光のみ／風力のみ／合計）と月別の表を出す。

### 14.4 Not supported together / 併用できないもの

蓄電池LP・最適容量探索は風力を敷地内の発電と同じに扱って最適化するため、受電点基準の制約や経済性を評価できない。次は**明示エラー**にする:
データセンターの系統受電上限、最適容量探索（解決は W2d: LPに「風力の配達量」の変数を足す）。

### 14.5 Files / ファイル

| ファイル | 役割 |
|---|---|
| `app.py` | `WIND_AREA_META` / `resolve_wind` / `build_wind_30min` / `offsite_receiving` / `offsite_cost_after` / `format_247` / UI |
| `mcp_tools.py` | `list_wind_areas` / `estimate_wind_generation` と、`simulate_*` / `validate_*` の `wind`・`pv_enabled` |
| `wind_shape.csv`, `tools/build_wind_shape.py` | 形状データと、その生成スクリプト |
| `test_wind_shape.py` / `test_wind_mode.py` / `test_wind_ui.py` / `test_mcp_wind_tools.py` | 計算層 / `run_simulation` 統合 / UI配線 / MCPツール |
