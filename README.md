---
title: 産業用太陽光需給シミュレーター
emoji: ☀️
colorFrom: yellow
colorTo: yellow
sdk: gradio
sdk_version: 6.26.0
app_file: app.py
pinned: false
---

# pv-sim-biz — Industrial Solar PV & Battery Simulator / 産業用太陽光発電＋蓄電池シミュレーター

[![Hugging Face Space](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Spaces-yellow)](https://huggingface.co/spaces/hachinai/pv-sim-biz)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Gradio](https://img.shields.io/badge/Gradio-6.26-orange)](https://gradio.app/)
[![MCP Compatible](https://img.shields.io/badge/MCP-compatible-9cf)](https://github.com/KENT-ueno/pv-sim-fip/blob/main/docs/mcp_guide.md)

A web-based simulator for **industrial-scale solar PV + battery storage systems** in Japan, designed for high-voltage and extra-high-voltage customers. Built on JIS C 8907 generation modeling, NEDO METPV-20 weather data for 50 Japanese sites, and PuLP/CBC linear programming for optimal battery dispatch.

日本国内の**高圧・特別高圧**需要家向けに設計された、産業用太陽光発電＋蓄電池の需給シミュレーターです。JIS C 8907 準拠の発電量計算、NEDO METPV-20 の50地点気象データ、PuLP/CBC 線形計画法による蓄電池最適化を統合し、年間17,520コマ（30分×365日）の需給バランスを一括シミュレートします。

**🔗 Live Demo / ライブデモ:** https://huggingface.co/spaces/hachinai/pv-sim-biz

**📝 Article (Qiita) / 解説記事:** https://qiita.com/jizou/items/8fc134752a4eabeaa6de

---

## ✨ Features / 機能

### English

- **JIS C 8907 compliant generation model** — Tilted-plane irradiance via pvlib (Erbs decomposition + isotropic transposition), per-array PCS clipping, JIS C 8907 temperature correction with hourly ambient data, and bifacial gain via `pvlib.bifacial.infinite_sheds` with snow-aware albedo switching.
- **50-site Japanese weather database** — NEDO METPV-20 dataset (10 elements: GHI, temperature, snow depth, etc.) bundled as SQLite (`radiation.db`).
- **Industrial demand presets** — 6 building types from NREL ComStock EULP (medium office, primary/secondary school, hospital, hotel, retail), normalized to per-m² intensity. Multi-facility aggregation for microgrid scenarios. Custom CSV upload supported.
- **High-voltage / extra-high-voltage tariffs** — Tokyo Electric Power EP rates with demand-charge tracking, power-factor adjustment, fuel cost surcharge, and renewable energy surcharge. Substation construction cost included.
- **Industrial FIT sell-back** — Automatic price switching by year (1–5y: ¥19, 6–20y: ¥8.3, 21y+: ¥8.5). Reverse-power-flow prohibition mode with battery curtailment.
- **Two business modes**:
  - **Mode A (Self-consumption)** — Lease/PPA reverse calculation from target P-IRR, customer-side benefit visualization. For solar/EPC sales teams.
  - **Mode B (Microgrid)** — Project IRR calculation with self-built distribution line, bundling-merit (combined contract demand vs sum of individual contracts), annual cash flow. For microgrid project planners.
- **Battery dispatch optimization (PuLP/CBC LP)** — Annual cost minimization (basic charge + energy charge − sell revenue) over all 17,520 timeslots with peak shaving via demand linearization, SOC continuity constraint, and curtailment variable for no-export mode.
- **Optimal battery sizing** — Two-stage approach: (1) one-shot LP with battery capacity as a decision variable for fast optimum, (2) grid search across capacity range to visualize the cost/IRR/payback curves.
- **CO₂ reduction calculation** — Default emission factor 0.000431 t-CO₂/kWh (Japan grid average).
- **Data center mode** — Demand is generated from IT load × PUE (constant, air-cooled) instead of building presets. Capacity by scale preset (edge / small / medium / hyperscale — *provisional tiers*), direct IT kW, or racks × density; load shape from a CEC-derived commercial-DC profile, flat (AI training) or diurnal. Optional **grid receiving cap** (stay within 6.6 kV < 2,000 kW / 22·33 kV < 10,000 kW): enforced by the battery LP, and if the cap cannot be met the tool explains why and gives a lower-bound estimate of the battery needed.
- **Wind power (offsite PPA)** — Add wind as a second generation source (solar only / wind only / both), for **Hokkaido and Tohoku only** (the demand site must be in the same area; cross-area wheeling is not modeled). Wind output = contract capacity × capacity factor × shape, where the shape is the 2025 half-hourly actual wind output of the area's transmission operator (normalized to mean 1.0) and the capacity factor (29.1%) and PPA price (¥11.96/kWh) come from the METI procurement-price committee. Wind is treated as an **offsite** source delivered over the grid: the delivered energy still incurs the **wheeling energy charge, renewable surcharge and retailer fee**, the **contract demand does not fall**, and only onsite solar surplus can be sold. The headline outputs are the **volume-based vs hourly (24/7) matching rates**, which show how much wind and battery are needed to get close to 24/7. Works with the industrial/microgrid modes (incl. P-IRR) and the data center mode (not with the grid receiving cap). Solar (Typical Meteorological Year) and wind (2025 actuals) are different datasets, so day-level coincidence is not reflected.

### 日本語

- **JIS C 8907 準拠の発電量モデル** — pvlib による傾斜面日射量変換（Erbsモデル＋isotropicモデル）、面別PCSクリップ、毎時外気温反映の温度補正、`pvlib.bifacial.infinite_sheds` による両面パネル対応、積雪深データに基づく動的アルベド切替（積雪時0.7／通常0.2）。
- **50地点の日本気象データベース** — NEDO METPV-20 の10要素データ（日射量・気温・積雪深ほか）を SQLite (`radiation.db`) として同梱。
- **産業用需要プリセット** — NREL ComStock EULP から取得した6建物タイプ（役所・小学校・中高・病院・ホテル・小売店）を m² 原単位に正規化。複数施設合算でマイクログリッドシナリオに対応。カスタムCSVアップロードも可能。
- **高圧・特別高圧の料金体系** — 東京電力EPの実料金（基本料金、夏季・他季電力量料金、力率割引、燃料費調整、再エネ賦課金）に対応。デマンド追跡による契約電力推定、受電設備工事費の試算も含む。
- **産業用FIT売電** — 経過年数で自動単価切替（1〜5年: 19円、6〜20年: 8.3円、21年〜: 8.5円）。逆潮流禁止モードでは蓄電池充電優先＋出力抑制を実装。
- **2つのビジネスモード**:
  - **モードA（自家消費型）** — 目標P-IRRから年間リース料・PPA単価を逆算し、需要家視点のメリットを可視化。営業担当・EPC事業者向け。
  - **モードB（マイクログリッド）** — 自営線投資を含むP-IRR算出、束ねメリット（個別契約電力合計 vs 合算後契約電力）、年次キャッシュフロー表示。MG事業企画者向け。
- **蓄電池最適充放電（PuLP/CBC LP）** — 17,520コマ全体で年間電気代（基本料金＋電力量料金−売電収入）を最小化。最大デマンドの線形化によるピークカット、SOC連続性制約、逆潮流禁止時のカーテイルメント変数を含む完全な定式化。
- **最適容量探索** — 2段階アプローチ：(1) 蓄電池容量を決定変数に含めたLP一体化で高速に最適解を取得、(2) 容量範囲のグリッドサーチで コスト削減・P-IRR・投資回収年数のカーブを可視化。
- **CO2削減量算出** — 排出係数デフォルト 0.000431 t-CO2/kWh（全国平均）。
- **データセンターモード** — 施設プリセットの代わりに「IT負荷×PUE（一定値・空冷前提）」から需要を生成。容量は規模プリセット（エッジ／小規模／中規模／ハイパースケール。**区分は暫定値**）・IT容量の直接入力・ラック数×密度から指定し、負荷の形状はCEC由来の商用DC形状・定常（AI学習）・日変動から選択。**系統受電上限**（高圧6.6kV＝2,000kW未満／22・33kV＝10,000kW未満に収める）を指定でき、蓄電池の最適充放電（LP）で強制します。守れない場合は理由と、必要な蓄電池の下限の目安を表示します。
- **風力発電（オフサイトPPA）** — 太陽光に加えて風力を第2の発電源として使えます（太陽光のみ／風力のみ／併用）。**北海道・東北のみ**（需要地も同じエリアに限り、越境託送はモデル化していません）。風力＝契約容量×設備利用率×形状で、形状は各エリアの一般送配電事業者が公表する2025年の風力発電実績（30分値。年平均=1.0に正規化）、設備利用率（29.1%）とPPA単価（11.96円/kWh）は調達価格等算定委員会の資料を出典にしています。風力は送配電網で届く**オフサイト電源**として扱い、届いた分にも**託送の電力量料金・再エネ賦課金・小売手数料**がかかり、**契約電力（基本料金）は下がらず**、売電できるのは敷地内の太陽光の余剰だけです。主役の出力は**量ベース達成率と時間一致率（24/7）**で、風力と蓄電池をどれだけ足すと24/7に近づくかを見られます。産業用・マイクログリッド（P-IRRを含む）とデータセンターで使え、系統受電上限（DC）・蓄電池の最適容量探索とも併用できます（蓄電池の最適充放電LPは受電点の基準で最適化。受電上限は届く風力も含めた受電量に対する上限です）。太陽光（平年値）と風力（2025年実績）は別のデータのため、日単位の同時性は反映されません。

---

## 🎯 Who is this for? / 想定ユーザー

| Persona / ペルソナ | Mode | 主な用途 |
|---|---|---|
| 太陽光・蓄電池の営業担当 / EPC事業者 | A: 自家消費型 | 需要家への提案書作成、年間コスト削減額・投資回収年数の試算 |
| MG事業の企画担当 / 特定送配電事業の検討者 | B: マイクログリッド | MG事業の投資判断、P-IRRと年次キャッシュフローの算出 |

---

## 🛠 Tech Stack / 技術スタック

- **Language**: Python 3.10+
- **UI**: Gradio 6.26（MCP対応）
- **Plotting**: Plotly
- **Solar modeling**: pvlib (Erbs, isotropic transposition, infinite_sheds for bifacial)
- **Optimization**: PuLP + CBC (linear programming)
- **Data**: SQLite (radiation.db, NEDO METPV-20 50-site dataset)
- **Numerical**: pandas, numpy
- **Deployment**: Hugging Face Spaces

---

## 🚀 Quick Start / クイックスタート

### Online (Recommended)

Use the live demo on Hugging Face Spaces — no installation required:

→ **https://huggingface.co/spaces/hachinai/pv-sim-biz**

### Local

```bash
git clone https://github.com/KENT-ueno/pv-sim-biz.git
cd pv-sim-biz
pip install -r requirements.txt
python app.py
```

The Gradio UI will start at `http://127.0.0.1:7860`.

> **Note**: `radiation.db` (~20 MB) is managed via Git LFS. You may need `git lfs pull` after cloning.

---

## 🤖 Use from an AI Agent (MCP) / AIエージェントから使う（MCP対応）

This Space exposes its calculations as an [MCP](https://modelcontextprotocol.io/) server. Any MCP-compatible AI agent (Claude Code, Claude Desktop, OpenAI Codex CLI, etc.) can call it directly with natural language — no manual UI clicking required. It also connects cleanly alongside the [pv-sim-fip](https://huggingface.co/spaces/hachinai/pv-sim-fip) and [pv-sim-gh](https://huggingface.co/spaces/hachinai/pv-sim-gh) servers at the same time (tool names are auto-namespaced per Space, e.g. `pv_sim_biz_list_stations`) — no hub app needed.

本Spaceは計算機能を [MCP](https://modelcontextprotocol.io/) サーバーとして公開しています。Claude Code・Claude Desktop・OpenAI Codex CLI など MCP対応の任意のAIエージェントから、自然言語のまま直接呼び出せます。[pv-sim-fip](https://huggingface.co/spaces/hachinai/pv-sim-fip)・[pv-sim-gh](https://huggingface.co/spaces/hachinai/pv-sim-gh) と同時接続しても、ツール名がSpace単位で自動的に名前空間化される（例: `pv_sim_biz_list_stations`）ためハブアプリなしで横断利用できます。

**Endpoint:** `https://hachinai-pv-sim-biz.hf.space/gradio_api/mcp/`

**Tools:** `list_stations` / `estimate_pv_generation` / `validate_industrial_params` → `simulate_industrial_pv`

**Data center tools:** `estimate_dc_demand` / `validate_dc_params` → `simulate_dc`（データセンター用。受電上限を守れない条件では、エラーではなく理由と必要量の目安を返します）

**Wind tools (Hokkaido / Tohoku only):** `list_wind_areas` / `estimate_wind_generation`。`validate_industrial_params` / `simulate_industrial_pv` / `validate_dc_params` / `simulate_dc` には任意引数 `wind`（`{"capacity_kw": 1000}` または `{"coverage_pct": 100}` ほか）と `pv_enabled` があり、省略すると従来と同じ結果です（風力を併用すると結果に `wind` 節と24/7の一致率が加わります）

```bash
# Claude Code
claude mcp add --scope user --transport http pv-sim-biz https://hachinai-pv-sim-biz.hf.space/gradio_api/mcp/
```

→ Full setup guide (Claude Desktop / OpenAI Codex CLI) and ready-to-paste example prompts:
**[pv-sim-fip: docs/mcp_guide.md](https://github.com/KENT-ueno/pv-sim-fip/blob/main/docs/mcp_guide.md)** ・ **[docs/example_prompts.md](https://github.com/KENT-ueno/pv-sim-fip/blob/main/docs/example_prompts.md)**

---

## 📂 Repository Structure / リポジトリ構成

```
pv-sim-biz/
├── app.py                      # Main application (Gradio UI + calculation engine)
├── mcp_tools.py                # MCP tool definitions (validate/simulate API layer)
├── radiation.db                # NEDO METPV-20 weather DB (Git LFS)
├── comstock_*.csv              # 6 industrial demand presets
├── wind_shape.csv              # Wind output shape (Hokkaido/Tohoku, 2025 half-hourly, mean 1.0)
├── tools/build_wind_shape.py   # Dev script that builds wind_shape.csv (not needed at runtime)
├── requirements.txt
├── README.md                   # This file
├── LICENSE                     # MIT
├── docs/
│   ├── architecture.md         # Detailed architecture & implementation notes
│   ├── design_spec.md          # Data center mode: design spec (canonical)
│   ├── decision_log.md         # Data center mode: decision history
│   ├── dc_load_data_sources.md # Data center load data: source survey
│   ├── hokkaido_power_voltage_tariff.md  # Hokkaido Electric voltage/tariff sources
│   ├── wind_design_spec.md     # Wind power (offsite PPA): design spec (canonical)
│   └── wind_agent_verification.md  # Wind MCP tools: agent verification procedure
├── test_mcp_tools.py / test_mcp_dc_tools.py   # MCP tool tests (industrial / data center)
├── test_dc_mode.py / test_grid_cap.py         # Data center demand, UI wiring, grid cap
├── test_wind_shape.py / test_wind_mode.py     # Wind: data & calculation layer / run_simulation integration
├── test_wind_ui.py / test_mcp_wind_tools.py   # Wind: UI wiring / MCP tools
├── prompt_verify_battery_bug.md      # Battery LP verification request
├── test_verify_battery_bug.py        # Battery LP pass-through bug check
└── test_verify_battery_bug_result.txt
```

---

## 🖼 Screenshots / スクリーンショット

![](https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-image-store.s3.ap-northeast-1.amazonaws.com%2F0%2F4384558%2Ffe4f6ac3-c937-47d7-8a7e-97eafed0d70f.png?ixlib=rb-4.0.0&auto=format&gif-q=60&q=75&s=71e5517448e750a75d6568907ce21aec)

![](https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-image-store.s3.ap-northeast-1.amazonaws.com%2F0%2F4384558%2F9ac5b3a2-305d-4031-8e07-2ebbb7c3a0c3.png?ixlib=rb-4.0.0&auto=format&gif-q=60&q=75&s=0d98cbea739b491b3f910d79d70a9b47)

![](https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-image-store.s3.ap-northeast-1.amazonaws.com%2F0%2F4384558%2Fcc353dc5-300b-4c53-a324-d0bfd44e8ff4.png?ixlib=rb-4.0.0&auto=format&gif-q=60&q=75&s=21801af2d90c98345914428ce148e1b6)

![](https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-image-store.s3.ap-northeast-1.amazonaws.com%2F0%2F4384558%2Fcc5e322b-58fc-42f0-8f9a-077d7b416da2.png?ixlib=rb-4.0.0&auto=format&gif-q=60&q=75&s=ada071631bfaa0fb399a95ee98a16889)

![](https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-image-store.s3.ap-northeast-1.amazonaws.com%2F0%2F4384558%2F968450d9-0ffe-427a-bb79-a433fb24e03d.png?ixlib=rb-4.0.0&auto=format&gif-q=60&q=75&s=9d99461a1f639c94e9efe1ef70efc2a9)

![](https://qiita-user-contents.imgix.net/https%3A%2F%2Fqiita-image-store.s3.ap-northeast-1.amazonaws.com%2F0%2F4384558%2Fc58b366a-7b90-4657-b81e-df2b4e6317da.png?ixlib=rb-4.0.0&auto=format&gif-q=60&q=75&s=a86695383ca43bf0a7e8f344e0e3049d)

---

## 📚 References / 参考文献

- **JIS C 8907:2005** — Estimation method of generating electric energy by PV power systems: https://kikakurui.com/c8/C8907-2005-01.html
- **pvlib-python** — PV modeling library: https://pvlib-python.readthedocs.io/
- **NEDO METPV-20** — Japanese solar irradiance database
- **NREL ComStock EULP** — End-Use Load Profiles for the U.S. building stock: https://comstock.nrel.gov/page/datasets
- **PuLP** — Python LP modeler: https://coin-or.github.io/pulp/
- **CEC 2025 IEPR** — *Data Center Methodology Memo* (source of the commercial data-center load shape used in data center mode): https://www.energy.ca.gov/media/12647
- **LBNL Data Center and Industrial Electrical Load Shape Maker** — definitions of the flat profile and short-cycle noise bands: https://github.com/LBNL-DataCenter-CoE/shape_maker
- **調達価格等算定委員会（資源エネルギー庁）** — 陸上風力（新設）の設備利用率の想定値29.1%、入札の平均落札価格11.96円/kWh（第112回、2026年1月）
- **一般送配電事業者「エリア需給実績」（北海道電力ネットワーク・東北電力ネットワーク）** — 風力発電実績・風力出力制御量（30分値）。風力の形状の出典
- **託送料金（北海道電力ネットワーク・東北電力ネットワーク）** — 高圧・特別高圧の電力量料金単価（標準接続送電、2025年10月〜）
- **自然エネルギー財団「コーポレートPPA 日本の最新動向」** — フィジカルPPAの費用構造（託送・賦課金・小売手数料）。小売手数料の推定値の出典

---

## 🌐 Sister Projects / 姉妹プロジェクト

- **Residential version / 家庭用**: [pv-sim-gh](https://huggingface.co/spaces/hachinai/pv-sim-gh) — 家庭用太陽光需給シミュレーター
- **FIP transition + grid battery / FIP転・系統用蓄電池**: [pv-sim-fip](https://huggingface.co/spaces/hachinai/pv-sim-fip) — 太陽光＋蓄電池 FIP転事業性シミュレーター

---

## 📄 License / ライセンス

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

本プロジェクトは **MITライセンス** のもとで公開されています。詳細は [LICENSE](LICENSE) を参照してください。

---

## 🙏 Acknowledgments / 謝辞

- NEDO (New Energy and Industrial Technology Development Organization) for the METPV-20 dataset
- NREL for the ComStock EULP dataset
- The pvlib-python and PuLP open-source communities
