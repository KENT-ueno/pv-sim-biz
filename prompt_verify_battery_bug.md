# 検証依頼: 蓄電池 LP「パススルー乱用」バグの波及チェック

## あなたへの依頼

`E:\jupyterlab\nedo_solar\pv-sim-fip\app.py` に最近見つかった蓄電池 LP のバグが、
**他の場所にも同じパターンで残っていないか / 派生して別の症状を出していないか** を
網羅的にチェックしてください。修正は不要、**調査と報告**だけお願いします。

調査方針はあなたが決めてOK。以下は背景情報と、最低限カバーしてほしい観点です。

---

## 1. バグの正体（既に発見・修正済み）

**症状**: 小容量蓄電池（例: 7.34 kWh）で `optimize_battery_fip` を実行すると、
年間充電量が 511,726 kWh（容量比 69,717 = 約 191 サイクル/日）という
**物理的にあり得ない値**が返ってきていた。健全な LP なら `cycles/day ≤ 2` 程度
（= ch/cap ≤ ~730）が上限のはず。

**根本原因**: LP の決定変数 `charge[t]`, `discharge[t]` に「同一スロットで両方が
正値を取れない」制約がなかった。エネルギーバランスは
```
gen[t] + discharge[t] = export[t] + charge[t] + curtail[t]
```
だが、`charge[t]` と `discharge[t]` が**同時に正**でも収支は成立する。
さらに SOC 遷移が `+ ch * η_ch − dc / η_dc` で線形のため、効率損失
（5% × 5% = 9.75%）を払ってでも JEPX 価格の高いコマで「PV → 充電 → 即放電 → 売電」を
**パススルー配管**として使うほうが収益最大化上有利になるケースがあった。
結果として SOC は変動せず（同時に上下するため相殺）、容量制約が事実上
無効化されて巨大スループットを生む。

**適用済み修正**: 両 LP に以下の制約を追加（mutual exclusion ソフト版）:
```python
prob += charge[t] + discharge[t] <= max_power_per_slot
```
ただし `max_power_per_slot = max(max_charge_per_slot, max_discharge_per_slot)` で、
1 台の PCS を充電または放電のどちらかに使うという物理を表現。

修正済み箇所:
- `optimize_battery_fip`  app.py L739
- `optimize_capacity_fip` app.py L882

---

## 2. 必ず確認してほしい観点

### 観点 A: LP 定式化の網羅チェック
- app.py には他にも LP（`pulp.LpProblem` ベース）が存在するか？
  → grep `LpProblem` で確認し、見つかったら **すべて** 同じ mutual exclusion 制約が
    入っているかを確認してほしい。
- 入っていない LP がある場合、その LP は `charge` / `discharge` 変数を持つか？
  持つなら同じバグが潜在している可能性が高い。

### 観点 B: LP 結果を後段で再利用している箇所
LP の戻り値 `battery_charge`, `battery_discharge` を、
LP を経由せず**手で**集計・スケーリング・劣化反映している処理はないか？
（例: `apply_battery_degradation` や `grid_search_capacity_pirr` の中で
LP の充放電量を年次劣化係数で割り算し直していないか）
そこでパススルーが既に発生していると、後段の集計では検出できない。

### 観点 C: エネルギーバランスの一貫性
- `gen[t] + discharge[t] = export[t] + charge[t] + curtail[t]` の右辺・左辺は
  全コードパスで同じか？
- `baseline_no_battery`（蓄電池なし路線）は LP を使わずに直接集計しているはず。
  そこでは `discharge[t]` が無いので関係ないが、念のため
  **「LP を呼ばずに蓄電池あり計算をしている箇所」**が無いことを確認してほしい。

### 観点 D: 出力制御（curtailment）と蓄電池の相互作用
バグ修正の付帯として、`export[t] ≤ gen[t] × (1 − curtail_prob[t])` という
**POI ベースの輸出上限**制約が入っている（L734-735, L879-880）。
この制約は「放電もカウントされる」前提だが、`export[t] = pv_to_grid + discharge[t]`
という分解はされていない。**理論的には**蓄電池放電が上限の枠を消費するため、
カートテイル時間帯に「PV から充電 → 放電」しても export は増えないはず。
- 実装が本当にこの理論通り動いているか？
- 蓄電池がカートテイル回避に効くのは「カートテイル時間帯に **充電** して
  非カートテイル時間帯に **放電** する経路」のみで、同スロット放電は無効、
  という不変条件を確認してほしい。

### 観点 E: SOC の物理一貫性
小容量ケース（7.34, 100, 1000 kWh）で `optimize_battery_fip` を直接呼び、
以下を集計してほしい:
- `(soc > soc_max + 1e-6)` の発生コマ数（あれば LP バグ）
- `(soc < soc_min - 1e-6)` の発生コマ数（あれば LP バグ）
- `(charge > 1e-6) & (discharge > 1e-6)` の発生コマ数（パススルー残党）
- `(charge + discharge > max_power_per_slot + 1e-4)` の発生コマ数（mutual exclusion 違反）
- 終端 SOC = 初期 SOC か（`soc[T-1] == soc_min` 制約が効いているか）

### 観点 F: ケースB（FIT→FIP転）での蓄電池運用
ケースB は LP を 1 回呼んで結果を全フェーズ（FIT 期間 + FIP 期間 + プレミアム終了後）に
**コピー**して使っている可能性がある。FIT 期間中は実際には蓄電池が無いのに、
LP 結果（蓄電池あり）の充放電量が紛れ込んでいないか確認してほしい。
- `run_simulation` のケースB ブランチ（app.py L1259 前後 `build_caseb_display_rows` 周辺）
- 各フェーズの `annual_charge`, `annual_discharge` が「蓄電池が存在するフェーズ」のみで
  正値、それ以外でゼロになっているか

### 観点 G: 最適容量探索の段階1（LP一体化）
`optimize_capacity_fip` は容量を決定変数に含める。
- 容量が極端に小さい（例 1 kWh）解で停止していないか？
- 容量が `capacity_upper_kwh` に張り付く解（境界解）を返していないか？
- 容量 = 0 を返した場合、後段のグリッドサーチがどう動くか？

---

## 3. 具体的に走らせてほしい確認テスト

ローカルで実行可能（環境はセットアップ済み）。
ファイル名は `test_verify_battery_bug.py` などにして、
結果は `test_verify_battery_bug_result.txt` に書き出してください（gitignore 済み）。

```python
# test_verify_battery_bug.py の骨子

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import app

def build_pv(pno="82182", ppeak_kw=5000.0):
    lat, lon, ghi_df, temp_df = app.load_from_db(pno)
    snow_df = app.load_snow_depth(pno)
    albedo_flat = app.build_albedo_series(snow_df)
    faces = [{"ppeak": ppeak_kw, "tilt": 30.0, "orientation": "S",
              "azimuth": 180.0, "pcs_limit_kw": ppeak_kw}]
    g = app.calculate_generation(
        lat=lat, lon=lon, ghi_df=ghi_df, temp_df=temp_df, faces=faces,
        KHD=app.DEFAULT_KHD, KPD=app.DEFAULT_KPD, KPM=app.DEFAULT_KPM,
        KPA=app.DEFAULT_KPA, eta_ino=app.DEFAULT_ETA_INO,
        alpha_pct=app.DEFAULT_ALPHA, delta_t=app.DEFAULT_DELTA_T,
        bifacial=False, albedo_flat=albedo_flat,
    )
    return g["total_gen_clipped"], g["month_day"]

# 1. 観点 E: 物理一貫性チェック（小・中・大の3容量）
gen, md = build_pv()
jepx = app.load_jepx_prices("九州", [2023, 2024, 2025], md)
curt = app.build_curtail_prob_30min(md, 15.0, generation_30min=gen)

for cap in [7.34, 100.0, 1000.0]:
    res = app.optimize_battery_fip(
        generation_30min=gen, jepx_prices=jepx, month_day=md,
        capacity_kwh=cap,
        max_charge_kw=5000.0, max_discharge_kw=5000.0,
        eff_charge_pct=95.0, eff_discharge_pct=95.0,
        soc_min_pct=10.0, soc_max_pct=90.0,
        premium=9.6, nonfossil_price=0.6, bg_fee=2.0,
        curtail_prob=curt,
    )
    ch = res["battery_charge"].flatten()
    dc = res["battery_discharge"].flatten()
    soc = res["soc"].flatten()
    soc_max_exp = cap * 0.9
    soc_min_exp = cap * 0.1

    # 物理チェック
    soc_violation_high = (soc > soc_max_exp + 1e-6).sum()
    soc_violation_low  = (soc < soc_min_exp - 1e-6).sum()
    both_pos = ((ch > 1e-6) & (dc > 1e-6)).sum()
    pcs_violation = ((ch + dc) > 2500.0 + 1e-4).sum()  # 2500=max_kw*0.5h
    cycles_per_day = (ch.sum() / cap) / 365 if cap > 0 else 0

    # 期待: 全てゼロ、cycles_per_day ≤ ~2
    print(f"cap={cap:8.2f} kWh | soc>max:{soc_violation_high:4d}  "
          f"soc<min:{soc_violation_low:4d}  "
          f"both_pos:{both_pos:5d}  "
          f"pcs_violation:{pcs_violation:5d}  "
          f"cycles/day:{cycles_per_day:6.2f}")

# 2. 観点 G: 容量探索の境界解チェック
stage1 = app.optimize_capacity_fip(
    generation_30min=gen, jepx_prices=jepx, month_day=md,
    max_charge_kw=5000.0, max_discharge_kw=5000.0,
    eff_charge_pct=95.0, eff_discharge_pct=95.0,
    soc_min_pct=10.0, soc_max_pct=90.0,
    premium=9.6, nonfossil_price=0.6, bg_fee=2.0,
    bat_cost_per_kwh_net=app.ECON_DEFAULTS["bat_cost_per_kwh"],
    irr_period_years=20,
    capacity_upper_kwh=10000.0,
    curtail_prob=curt,
)
print(f"stage1 optimal_capacity = {stage1['optimal_capacity_kwh']:.1f} kWh "
      f"(must be > 0 and < 10000)")

# 3. 観点 F: ケースB の LP 結果コピー漏れチェック
#    → run_simulation を呼び出して、各年の charge/discharge を年次別に出力
#    詳細はあなたが調査してください。
```

---

## 4. 報告フォーマット

調査結果は以下の形でまとめてください:

1. **観点ごとの結論**（A〜G それぞれ「OK / 要確認 / バグあり」）
2. **発見した懸念**: ファイル:行番号 と該当コード片、なぜ怪しいか
3. **追加で走らせるべきテスト**（あれば）
4. **修正提案はせず、あなたが書いたコードもコミットしない**
   （修正方針は人間が決めるため）

---

## 5. 触ってよい / 触ってはいけないファイル

- ✅ 読んで OK: `app.py`, `test_phase3_smoke.py`, `test_mri_slide47.py`,
  `test_lp_fix*.py`, `test_debug_soc.py`, `CLAUDE.md`
- ✅ 新規作成 OK: `test_verify_battery_bug.py`,
  `test_verify_battery_bug_result.txt`（どちらも gitignore 済み）
- ❌ 編集禁止: `app.py` 本体（調査のみ。修正はバグ確定後に人間判断で）
- ❌ デプロイ禁止: `git push` は禁止。コミットも作らない。

以上、よろしくお願いします。
