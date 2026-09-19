# データセンターの電力需要：公開時系列データ・負荷曲線の整理

作成日：2026-09-18

## 1. 調査目的

データセンター（DC）の電力需要について、単純な年間消費電力量ではなく、以下のような時間変動を把握できる公開データを整理する。

- 24時間 × 365日（8760時間）程度の年間時系列
- 15分値など、8760時間より高解像度の時系列
- 各季節について数百時間程度を切り出して分析できるデータ
- 季節別・平日休日別の代表24時間負荷曲線
- IT負荷と冷却・補機負荷を分離して確認できるデータ

現時点では、米国の国立研究所・州政府が公開しているデータが特に有力である。

---

## 2. 結論

今回の目的に対して特に有用なのは、以下の3つである。

| データ | 内容 | 時間粒度 | 実測 / モデル | 評価 |
|---|---|---:|---|---|
| NLR ESIF HPC Facility PUE Data | 実データセンターのIT、冷却、HVAC、ポンプ等 | 高頻度時系列 | 実測 | 最有力 |
| California Energy Commission 2025 IEPR | 多数の商用DCのinterval meterを基にした負荷形状 | 時間別・月別・季節別 | 実測ベース集約値 | 商用DCの代表性が高い |
| LBNL Data Center Load Shape Maker | 任意条件の年間DC需要プロファイル | 15分値×1年 | 合成モデル | 8760/35040時系列の作成に有効 |

補足として、米国DOEの **Sup3rLoad** は、州・系統レベルで将来のデータセンター需要を扱う場合に有用である。

---

# 3. NLR ESIF HPC Facility Power Usage Effectiveness Data

## 概要

National Laboratory of the Rockies（NLR、旧NREL）が公開している、Energy Systems Integration Facility（ESIF）のHPCデータセンター実測データ。

データセンター全体の電力だけではなく、電力用途別に時系列データが分離されている点が非常に重要である。

### 公開されている主な項目

- `it_power_kw`：IT機器電力
- `cooling_kw`：屋外冷却設備等
- `hvac_kw`：HVAC
- `pump_kw`：冷却水等のポンプ
- `plug_and_light_kw`：照明・コンセント等
- `pue`：Power Usage Effectiveness
- `energy_reuse`：Energy Reuse Effectiveness

別ファイルとして、

- 外気温
- 外気相対湿度

の時系列も公開されている。

### データ形式

- Parquet
- 圧縮CSV

したがって、Python等で読み込み、1時間平均にリサンプリングすれば8760時間データとして利用できる。

また、春・夏・秋・冬について任意の期間を抽出できるため、「各季節324時間程度」の分析にもそのまま利用できる。

### このデータで分析できること

例えば以下を直接検証できる。

1. IT負荷の日内変動
2. データセンター全体の負荷変動
3. IT負荷と冷却負荷の関係
4. 外気温と冷却消費電力の関係
5. PUEの季節変動
6. 夏季と冬季の受電電力差
7. 8760時間負荷率分布
8. 最大需要に対する各時間の負荷率

今回の目的では、最も直接的に利用できる実測データである。

### 留意点

ESIFは一般的なコロケーション型・ハイパースケール型DCではなく、HPC施設である。

したがって、

- 商用クラウド
- AI inference
- ハイパースケールDC
- コロケーションDC

をそのまま代表すると解釈するのは適切ではない。

一方、「実施設のIT負荷・冷却負荷・気象条件を同時に長期間確認できる」という点では非常に価値が高い。

### URL

NLR Data Catalog  
https://data.nlr.gov/submissions/300

DOI  
https://doi.org/10.7799/3015212

---

# 4. California Energy Commission（CEC）2025 IEPR

## 概要

California Energy Commission（CEC）は、2025 Integrated Energy Policy Report（IEPR）の需要予測において、既存データセンターの **interval meter data** を用いて時間別負荷曲線を作成している。

CECは各施設の時間別負荷を、

> 各施設で観測された年間最大需要

に対して正規化し、時間別Load Factorを計算している。

その後、複数施設を集約し、

- 平日
- 休日
- 月別
- 季節別

の代表負荷曲線を作成している。

## 実測データから確認された特徴

CECは、既存DCについて以下の特徴を整理している。

- 一日を通じて高い負荷率を維持
- 昼夜差が小さい
- 夏冬の季節差は存在するが比較的小さい
- 年間の大部分で継続的に高負荷

対象施設の平均的な時間別負荷率は、

**年間最大需要の約85～90%**

の範囲にあるとされている。

したがって、一般的な商用DCについては、

> 大きなベースロード + 比較的小さい日内変動 + 冷却等に起因する季節変動

という負荷特性を持つことを示す有力な実測根拠になる。

## PG&Eの約100施設データ

CECの補足的な調和関数（harmonic）モデルは、

- CEC保有のinterval meter sample
- PG&Eの約100データセンター
- 2020～2024年

の時間別実測データを用いて校正されている。

この点で、単一HPC施設であるNLR ESIFよりも、商用データセンター群の代表的な負荷形状を把握する用途に向いている。

## 季節・時間帯

CEC資料では、

- 夏季：August through October
- 冬季：February through April

について平日代表負荷曲線を比較している。

また、補足モデルでは、

- 平日ピーク：約14～15時（hour ending 15）
- 休日ピーク：約16～17時（hour ending 17）
- 晩夏～初秋に季節ピーク

という傾向が示されている。

## 公開されている時間別データ

CECは2025 IEPRの一部として、

**CED 2025 Data Center Forecast - Supplemental Hourly Load Profile Analysis**

も公開している。

Excel形式で時間別DC負荷モデルを取得可能であり、系統解析等に使用できる。

### URL

CEC Demand Side Modeling  
https://www.energy.ca.gov/data-reports/california-energy-planning-library/forecasts-and-system-planning/demand-side-3

Data Center Methodology Memo  
https://www.energy.ca.gov/sites/default/files/2026-04/Data_Center_Methodology_Memo_ada.pdf

Supplemental Hourly Load Profile Analysis  
https://www.energy.ca.gov/media/12647

Excelファイル  
https://www.energy.ca.gov/sites/default/files/2026-03/ALT_DATA_CENTER_GROWTH_HOURLY_MW_ada.xlsx

---

# 5. CECの「67%」と「85～90%」の違い

CEC資料には異なる意味の2種類の比率が出てくるため、混同しないことが重要である。

## 67%：Utilization Factor

これは、

**utilityへのRequested Service Capacity → 想定最大運転需要**

への変換係数。

CECはSilicon Valley Power等の実績を基に、要求された受電容量の67%を想定最大需要とする。

例：

100 MWの受電容量を申請  
→ 想定最大運転需要 約67 MW

これは「年間負荷率」ではない。

## 約85～90%：Hourly Load Factor

こちらは、

**観測された年間最大需要 → 各時間の実需要**

の比率。

例えば最大運転需要67 MWの施設でLoad Factorが90%なら、

67 MW × 90% ≒ 60 MW

程度の需要となる。

つまり概念的には、

Requested Capacity  
→ × 67%  
→ Maximum Operating Demand  
→ × 約85～90%  
→ Typical Hourly Demand

という関係になる。

---

# 6. LBNL Data Center Load Shape Maker

## 概要

Lawrence Berkeley National Laboratory（LBNL）が2026年に公開した、データセンター・産業需要向けの年間負荷曲線生成ツール。

公開実測データが不足していることを背景に、観測されたmeter dataと業界専門家の知見を基に、合成年間負荷曲線を生成する。

## 時間粒度

標準的には、

**15分 × 24時間 × 365日 = 35,040点/年**

の時系列を生成できる。

したがって、

- 15分値
- 1時間値（8760点）
- 季節別抽出

のいずれにも利用できる。

## DCの分類

ツールでは、データセンターについて複数の条件を設定できる。

### 規模

- Large
- Medium
- Small

### 用途

- Training
- Inference
- Mixed

### 冷却

- Air
- Water

### 日内負荷形状

- Flat
- Business diurnal
- Business high diurnal
- Customer diurnal
- Customer high diurnal

### 短周期変動

- Low noise
- High noise

これらを組み合わせることで、多数の年間負荷シナリオを生成できる。

## 特に重要な用途

今回のように、

「データセンターの代表的な8760時間需要データを作りたい」

場合には非常に有効である。

実測値そのものではないため、NLR・CEC実測データと比較して妥当性を確認しながら使うのが望ましい。

### URL

GitHub  
https://github.com/LBNL-DataCenter-CoE/shape_maker

---

# 7. DOE Sup3rLoad

## 概要

米国DOE系で公開されている、米国本土の将来時間別電力需要データセット。

以下を含む。

- 48州 + District of Columbia
- 1時間値
- 2025～2050年の複数シナリオ年
- 複数の気象年
- 14の需要subsector

データセンターについても独立した需要要素が組み込まれている。

特に **data center cooling** について、建物エネルギーシミュレーションと気象条件を使って時間別需要を推計している。

## 向いている用途

NLRやCECとは用途が異なる。

向いているのは、

- 州レベルのDC電力需要
- 系統負荷への影響
- 将来気候条件との関係
- 2030年、2040年、2050年等の将来需要
- 広域系統モデル

である。

個別DC施設の実測負荷曲線を知る目的では優先順位は低い。

### URL

DOE / Data.gov  
https://catalog.data.gov/dataset/sup3rload-dataset

---

# 8. 参考：NLR HPC Eagle Node Power Data

NLRでは、ESIF HPCの施設全体データとは別に、HPC計算ノード単位の電力データも公開している。

データ期間は2019年4月～2024年8月。

これは「データセンター受電需要」というより、

- HPC workload
- サーバー・ノード負荷
- IT需要変動

を詳細に分析する場合に有用である。

施設全体データと組み合わせれば、

**IT workloadの変動 → IT電力 → 冷却電力 → 施設全体電力**

という関係を検討できる可能性がある。

URL  
https://data.nlr.gov/submissions/288

DOI  
https://doi.org/10.7799/3015211

---

# 9. 今回の目的に対する使い分け

## A. 実際の年間負荷曲線を確認する

第一候補：

**NLR ESIF HPC Facility PUE Data**

実データを1時間平均化して8760時間化する。

---

## B. 一般的な商用DCの負荷形状を確認する

第一候補：

**CEC 2025 IEPR**

約100施設を含む実測データを背景とした代表負荷曲線なので、商用DCの一般的特徴を把握するのに適する。

---

## C. 8760時間の入力データを作る

第一候補：

**LBNL Data Center Load Shape Maker**

15分値35,040点を生成し、必要なら1時間値8,760点へ集約する。

例えば、

- Large / Training / Water cooling
- Large / Inference / Air cooling
- Large / Mixed / Water cooling

などの比較が可能。

---

## D. 系統レベルの将来DC需要を解析する

第一候補：

**DOE Sup3rLoad**

個別施設ではなく、州・広域系統の需要分析向け。

---

# 10. 現時点での推奨分析構成

公開データだけでDC負荷特性を整理する場合、以下の組み合わせが最も堅い。

### 実測ベース

NLR ESIF  
→ 実施設の高時間解像度データ  
→ IT・冷却・HVAC・ポンプを分解

### 商用DCの代表性確認

CEC  
→ 多数施設のinterval meter  
→ 年間最大需要比で約85～90%の高負荷運転  
→ 平日休日・季節別負荷形状

### 任意条件への展開

LBNL Shape Maker  
→ 15分年間時系列  
→ Training / Inference / Mixed等の比較

この3つを併用すれば、

1. 実測データ
2. 商用DC群の統計的な代表負荷
3. 任意ケースの8760時間モデル

を分けて扱うことができる。

---

# 11. 一次ソース一覧

1. National Laboratory of the Rockies, *HPC Facility Power Usage Effectiveness (PUE) Data*  
   https://data.nlr.gov/submissions/300

2. National Laboratory of the Rockies, *HPC Eagle Node Power Data*  
   https://data.nlr.gov/submissions/288

3. California Energy Commission, *Data Center Methodology Memo — Supporting Document for the 2025 IEPR Forecast*  
   https://www.energy.ca.gov/sites/default/files/2026-04/Data_Center_Methodology_Memo_ada.pdf

4. California Energy Commission, *CED 2025 Demand Side Modeling*  
   https://www.energy.ca.gov/data-reports/california-energy-planning-library/forecasts-and-system-planning/demand-side-3

5. California Energy Commission, *CED 2025 Data Center Forecast - Supplemental Hourly Load Profile Analysis*  
   https://www.energy.ca.gov/media/12647

6. Lawrence Berkeley National Laboratory, *Data Center and Industrial Electrical Load Shape Maker*  
   https://github.com/LBNL-DataCenter-CoE/shape_maker

7. U.S. Department of Energy / Data.gov, *Sup3rLoad Dataset*  
   https://catalog.data.gov/dataset/sup3rload-dataset


---

# 12. 検証記録（2026-09-19、Claude Codeによる一次資料確認）

本書の記載および追加候補3件（Virginia JLARC / NLR GenAI Workload Profiles / PNNL AI DC分析）を
一次資料で確認した。**全6件とも実在し、記載内容も正確だった。**

| 候補 | 実在 | 確認した内容 | 本プロジェクトでの評価 |
|---|:--:|---|---|
| **CEC 2025 IEPR** | ✅ | Methodology Memo（24頁）を通読。閉形式の調和モデル `LF(t,h) = μ + α·sin(θt) + β·sin(φh)(1 + γ·sin(θt))` を公開。288月-時刻点、平日/休日別、PG&E約100施設(2020-2024)＋CECサンプルでRMSE校正 | **◎ 第一候補** |
| **Virginia JLARC / PJM** | ✅ | 2024年12月公表、E3が分析（RECAP/RESOLVE）。"data centers run at **flat 24/7 utilization**, lifting overnight and shoulder hours" — CECと独立に同じ結論 | **◎ 裏取り用**。数値引用で足りる |
| **NLR ESIF HPC PUE** | ✅ | 列名・DOI一致。ただし**チラーレス温水直接液冷・PUE 1.06・排熱97%回収** | **△ に格下げ**（§5-6(3)参照）。PUE較正には使えない。構造確認に限れば○ |
| **LBNL Shape Maker** | ✅ | 15分×35,040点、Training/Inference/Mixed、ノイズ低3-7%/高12-18% | ◎ AIケース用。ただし合成値 |
| **NLR GenAI Workload Profiles** | ✅ | arXiv 2604.07345 / OSTI 3025227。Kestrel＋NVIDIA H100、**0.1秒（5/10Hz）分解能**、MLCommons＋vLLMベンチマーク | **△**。本モデルの30分粒度と**6桁違い**。UPS・系統擾乱の研究用 |
| **PNNL AI DC分析** | ✅ | PNNL-38601。SURF＋MIT Supercloud（TX-Gaia、224 GPUノード）分析＋LSTM合成器。"Active states … maintain elevated levels for **tens of minutes to hours**, consistent with large batch jobs/training runs" | **△ 補強資料**。ただし上記引用は「定常」モードをAI学習DCに当てる根拠になる |

## 本プロジェクトにとっての結論

**データ収集はここで打ち切ってよい。** 理由は、本プロジェクトが需要を**合成する**方針（設計書§5-1）であり、
外部データを同梱する予定がないため。必要なのは「合成モデルの形状とデフォルト値に**出典を与える**」ことだけで、
それには次の3点で足りる。

| 必要なもの | 入手元 | 状態 |
|---|---|---|
| 商用DCの負荷形状（正規化済み） | CEC調和モデルの係数 μ・α・β・γ | ⏳ **未入手**。Excelを取得すれば完結 |
| その裏取り | JLARC「flat 24/7 utilization」 | ✅ 引用可能 |
| AI学習DCを「定常」としてよい根拠 | PNNL「tens of minutes to hours」 | ✅ 引用可能 |

CECの係数が手に入れば**実装10行・データファイル同梱ゼロ**で出典付きの負荷プロファイルモードが作れる。
ESIF（104MB）・LBNL・GenAI・PNNLのデータ本体は簡易版には不要。

## 残る空白

**上記6件はすべて米国データであり、日本のものが1件も無い。**
IT負荷の「形」はワークロード起因なので転用できるが、**PUEは気候・設備依存**のため、
設計書§13の宿題「PUEデフォルト値の出典確認」はこの6件をどう組み合わせても解けない。
JDCC・環境省/経産省のDC実態調査が引き続き必要である。
（PUEをユーザー入力・既定1.40とした判断により、実害の小さい未解決事項に留まっている。）

## 追加の一次ソース

8. Virginia JLARC, *Data Centers in Virginia*（2024年12月）
   https://jlarc.virginia.gov/landing-2024-data-centers-in-virginia.asp ／
   報告書 https://jlarc.virginia.gov/pdfs/reports/Rpt598-2.pdf
9. NLR, *Dataset of Generative AI Workload Power Profiles*
   https://www.osti.gov/biblio/3025227 ／ 論文 https://arxiv.org/abs/2604.07345
10. PNNL, *Reliable Integration of AI Data Centers at Scale*（PNNL-38601）
    https://www.pnnl.gov/main/publications/external/technical_reports/PNNL-38601.pdf
