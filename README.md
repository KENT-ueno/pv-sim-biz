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

A web-based simulator for **industrial-scale solar PV + battery storage systems** in Japan, designed for high-voltage and extra-high-voltage customers. Built on JIS C 8907 generation modeling, NEDO METPV-20 weather data for 47 Japanese sites, and PuLP/CBC linear programming for optimal battery dispatch.

日本国内の**高圧・特別高圧**需要家向けに設計された、産業用太陽光発電＋蓄電池の需給シミュレーターです。JIS C 8907 準拠の発電量計算、NEDO METPV-20 の47地点気象データ、PuLP/CBC 線形計画法による蓄電池最適化を統合し、年間17,520コマ（30分×365日）の需給バランスを一括シミュレートします。

**🔗 Live Demo / ライブデモ:** https://huggingface.co/spaces/hachinai/pv-sim-biz

**📝 Article (Qiita) / 解説記事:** https://qiita.com/jizou/items/8fc134752a4eabeaa6de

---

## ✨ Features / 機能

### English

- **JIS C 8907 compliant generation model** — Tilted-plane irradiance via pvlib (Erbs decomposition + isotropic transposition), per-array PCS clipping, JIS C 8907 temperature correction with hourly ambient data, and bifacial gain via `pvlib.bifacial.infinite_sheds` with snow-aware albedo switching.
- **47-site Japanese weather database** — NEDO METPV-20 dataset (10 elements: GHI, temperature, snow depth, etc.) bundled as SQLite (`radiation.db`).
- **Industrial demand presets** — 6 building types from NREL ComStock EULP (medium office, primary/secondary school, hospital, hotel, retail), normalized to per-m² intensity. Multi-facility aggregation for microgrid scenarios. Custom CSV upload supported.
- **High-voltage / extra-high-voltage tariffs** — Tokyo Electric Power EP rates with demand-charge tracking, power-factor adjustment, fuel cost surcharge, and renewable energy surcharge. Substation construction cost included.
- **Industrial FIT sell-back** — Automatic price switching by year (1–5y: ¥19, 6–20y: ¥8.3, 21y+: ¥8.5). Reverse-power-flow prohibition mode with battery curtailment.
- **Two business modes**:
  - **Mode A (Self-consumption)** — Lease/PPA reverse calculation from target P-IRR, customer-side benefit visualization. For solar/EPC sales teams.
  - **Mode B (Microgrid)** — Project IRR calculation with self-built distribution line, bundling-merit (combined contract demand vs sum of individual contracts), annual cash flow. For microgrid project planners.
- **Battery dispatch optimization (PuLP/CBC LP)** — Annual cost minimization (basic charge + energy charge − sell revenue) over all 17,520 timeslots with peak shaving via demand linearization, SOC continuity constraint, and curtailment variable for no-export mode.
- **Optimal battery sizing** — Two-stage approach: (1) one-shot LP with battery capacity as a decision variable for fast optimum, (2) grid search across capacity range to visualize the cost/IRR/payback curves.
- **CO₂ reduction calculation** — Default emission factor 0.000431 t-CO₂/kWh (Japan grid average).

### 日本語

- **JIS C 8907 準拠の発電量モデル** — pvlib による傾斜面日射量変換（Erbsモデル＋isotropicモデル）、面別PCSクリップ、毎時外気温反映の温度補正、`pvlib.bifacial.infinite_sheds` による両面パネル対応、積雪深データに基づく動的アルベド切替（積雪時0.7／通常0.2）。
- **47地点の日本気象データベース** — NEDO METPV-20 の10要素データ（日射量・気温・積雪深ほか）を SQLite (`radiation.db`) として同梱。
- **産業用需要プリセット** — NREL ComStock EULP から取得した6建物タイプ（役所・小学校・中高・病院・ホテル・小売店）を m² 原単位に正規化。複数施設合算でマイクログリッドシナリオに対応。カスタムCSVアップロードも可能。
- **高圧・特別高圧の料金体系** — 東京電力EPの実料金（基本料金、夏季・他季電力量料金、力率割引、燃料費調整、再エネ賦課金）に対応。デマンド追跡による契約電力推定、受電設備工事費の試算も含む。
- **産業用FIT売電** — 経過年数で自動単価切替（1〜5年: 19円、6〜20年: 8.3円、21年〜: 8.5円）。逆潮流禁止モードでは蓄電池充電優先＋出力抑制を実装。
- **2つのビジネスモード**:
  - **モードA（自家消費型）** — 目標P-IRRから年間リース料・PPA単価を逆算し、需要家視点のメリットを可視化。営業担当・EPC事業者向け。
  - **モードB（マイクログリッド）** — 自営線投資を含むP-IRR算出、束ねメリット（個別契約電力合計 vs 合算後契約電力）、年次キャッシュフロー表示。MG事業企画者向け。
- **蓄電池最適充放電（PuLP/CBC LP）** — 17,520コマ全体で年間電気代（基本料金＋電力量料金−売電収入）を最小化。最大デマンドの線形化によるピークカット、SOC連続性制約、逆潮流禁止時のカーテイルメント変数を含む完全な定式化。
- **最適容量探索** — 2段階アプローチ：(1) 蓄電池容量を決定変数に含めたLP一体化で高速に最適解を取得、(2) 容量範囲のグリッドサーチで コスト削減・P-IRR・投資回収年数のカーブを可視化。
- **CO2削減量算出** — 排出係数デフォルト 0.000431 t-CO2/kWh（全国平均）。

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
- **Data**: SQLite (radiation.db, NEDO METPV-20 47-site dataset)
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

## 📂 Repository Structure / リポジトリ構成

```
pv-sim-biz/
├── app.py                      # Main application (standalone)
├── radiation.db                # NEDO METPV-20 weather DB (Git LFS)
├── comstock_*.csv              # 6 industrial demand presets
├── requirements.txt
├── README.md                   # This file
├── LICENSE                     # MIT
├── docs/
│   └── architecture.md         # Detailed architecture & implementation notes
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

---

## 🌐 Sister Projects / 姉妹プロジェクト

- **Residential version**: [pv-sim-gh](https://huggingface.co/spaces/hachinai/pv-sim-gh) — 家庭用太陽光需給シミュレーター

---

## 📄 License / ライセンス

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

本プロジェクトは **MITライセンス** のもとで公開されています。詳細は [LICENSE](LICENSE) を参照してください。

---

## 🙏 Acknowledgments / 謝辞

- NEDO (New Energy and Industrial Technology Development Organization) for the METPV-20 dataset
- NREL for the ComStock EULP dataset
- The pvlib-python and PuLP open-source communities
