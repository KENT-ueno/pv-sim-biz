# -*- coding: utf-8 -*-
"""W2f（オフサイト電源の費用構造の作り直し）のテスト。設計は docs/wind_design_spec.md §9。

段階ごとに増やす（W2f-1 は計算層の primitives: resolve_wind の拡張・offsite_payment・
offsite_receiving/offsite_lp_spec の到達可能量）。別経路で再計算した値と照合する。
"""
import numpy as np
import pytest

import app


# --- 共通の風力入力（仙台・東北エリア。§9-5 の手計算と同じ条件） ---
STATION_SENDAI = "34392"


def _wind_args(**overrides):
    d = dict(enabled=True, sizing_mode=app.WIND_SIZING_CAPACITY, capacity_kw=109.4)
    d.update(overrides)
    return d


class TestGeneralization:
    """一般化: 損失率0・発電側課金「含む」・バランシング0・小売GM(旧Y)3.0・全量払い で旧構造の式に一致する。"""

    def test_payment_yen_matches_old_formula(self):
        w = app.resolve_wind(
            _wind_args(loss_rate_pct=0.0, gen_charge_mode=app.WIND_GEN_CHARGE_INCLUDED,
                       balancing_yen=0.0, retail_fee_yen=3.0),
            STATION_SENDAI, annual_demand_kwh=1_000_000.0, contract_type="高圧")
        # 旧構造: payment_yen = annual_kwh * ppa_price（発電した全量 × PPA単価。gen_charge・balancing なし）
        assert w["payment_yen"] == pytest.approx(w["annual_kwh"] * w["ppa_price"], rel=1e-12)
        assert w["gen_charge_mode"] == "included"  # 「含む」なので加算しない
        assert w["loss_rate"] == 0.0
        assert np.allclose(w["deliverable_30min"], w["gen_30min"])  # 損失0なら到達可能量=発電量

    def test_offsite_receiving_matches_old_when_loss_zero(self):
        w = app.resolve_wind(_wind_args(loss_rate_pct=0.0), STATION_SENDAI,
                              annual_demand_kwh=1_000_000.0, contract_type="高圧")
        demand = np.full((365, 48), 5.0)
        pv_gen = np.zeros((365, 48))
        sc_result = {"import_": np.maximum(0.0, demand - w["gen_30min"])}
        off = app.offsite_receiving(pv_gen, [w], demand, sc_result)
        # 損失0のとき: wasted_by_source は旧式どおり gen_30min - delivered、loss_by_source は全ゼロ
        old_wasted = w["gen_30min"] - off["delivered_by_source"][0]
        assert np.allclose(off["wasted_by_source"][0], old_wasted)
        assert np.allclose(off["loss_by_source"][0], 0.0)


class TestOffsitePayment:
    """offsite_payment の全量払い／使用量払いを、別経路の式で再計算して照合する。"""

    def _source(self, **kw):
        base = dict(annual_kwh=278_753.5, ppa_price=11.96, gen_charge_yen=202_927.0,
                    gen_charge_mode="add", balancing_yen=1.1, loss_rate=0.052,
                    payment_basis="generated")
        base.update(kw)
        return base

    def test_generated_basis_total(self):
        s = self._source(payment_basis="generated")
        r = app.offsite_payment(s, delivered_kwh=0.0)  # 全量払いは delivered_kwh を使わない
        expect_ppa = s["annual_kwh"] * s["ppa_price"]
        expect_gc = s["gen_charge_yen"]
        expect_bal = s["annual_kwh"] * s["balancing_yen"]
        assert r["ppa"] == pytest.approx(expect_ppa, rel=1e-12)
        assert r["gen_charge"] == pytest.approx(expect_gc, rel=1e-12)
        assert r["balancing"] == pytest.approx(expect_bal, rel=1e-12)
        assert r["loss_part"] == 0.0
        assert r["total"] == pytest.approx(expect_ppa + expect_gc + expect_bal, rel=1e-12)

    def test_generated_basis_gen_charge_not_added_when_included(self):
        """『含む』でも gen_charge は0ではなく生の計算値を返す（参考額。合計には入らない。2026-09-22 Codexの実機検証で発見）。"""
        s = self._source(gen_charge_mode="included")
        r = app.offsite_payment(s, delivered_kwh=0.0)
        assert r["gen_charge"] == pytest.approx(s["gen_charge_yen"], rel=1e-12)  # 参考額（生の計算値）
        assert r["total"] == pytest.approx(s["annual_kwh"] * (s["ppa_price"] + s["balancing_yen"]), rel=1e-12)
        # 「加算」のときと参考額そのものは同じ値（合計に入るかどうかだけが違う）
        r_add = app.offsite_payment(self._source(gen_charge_mode="add"), delivered_kwh=0.0)
        assert r["gen_charge"] == pytest.approx(r_add["gen_charge"], rel=1e-12)
        assert r["total"] == pytest.approx(r_add["total"] - r_add["gen_charge"], rel=1e-12)

    def test_used_basis_total_and_hand_calc(self):
        """§9-5 の手計算: 発電側単価 ≈ 14.48 円/kWh（届いた 115,281.6 kWh に対して）。"""
        s = self._source(payment_basis="used")
        delivered = 115_281.6
        r = app.offsite_payment(s, delivered_kwh=delivered)
        per_kwh = r["total"] / delivered
        assert per_kwh == pytest.approx(14.48, abs=0.01)
        # 内訳の整合性: ppa + gen_charge + balancing == total（loss_part は内訳の表示用で加算しない）
        assert r["ppa"] + r["gen_charge"] + r["balancing"] == pytest.approx(r["total"], rel=1e-12)
        # 独立の式で再計算: 発電側単価 = (ppa + gen_charge/G) / (1-loss) + balancing
        gen_unit = (s["ppa_price"] + s["gen_charge_yen"] / s["annual_kwh"]) / (1 - s["loss_rate"]) + s["balancing_yen"]
        assert r["total"] == pytest.approx(delivered * gen_unit, rel=1e-9)
        # loss_part: 損失の割り戻し分だけ（ppa+gen_charge の基礎単価に対する上乗せ）
        base_unit = s["ppa_price"] + s["gen_charge_yen"] / s["annual_kwh"]
        expect_loss_part = delivered * base_unit * (s["loss_rate"] / (1 - s["loss_rate"]))
        assert r["loss_part"] == pytest.approx(expect_loss_part, rel=1e-9)

    def test_used_basis_gen_charge_excluded_when_included_mode(self):
        s = self._source(payment_basis="used", gen_charge_mode="included")
        delivered = 100_000.0
        r = app.offsite_payment(s, delivered_kwh=delivered)
        gen_unit = s["ppa_price"] / (1 - s["loss_rate"]) + s["balancing_yen"]
        assert r["total"] == pytest.approx(delivered * gen_unit, rel=1e-9)
        # #2（2026-09-26）: 「含む」でも0ではなく、届いた量で配分した参考額を返す（合計には入らない）
        expect_ref = delivered * (s["gen_charge_yen"] / s["annual_kwh"]) / (1 - s["loss_rate"])
        assert r["gen_charge"] == pytest.approx(expect_ref, rel=1e-12)
        assert r["gen_charge"] > 0
        r_add = app.offsite_payment(dict(s, gen_charge_mode="add"), delivered_kwh=delivered)
        assert r["gen_charge"] == pytest.approx(r_add["gen_charge"], rel=1e-12)   # 参考額 = 「加算」のときの額
        assert r["total"] == pytest.approx(r_add["total"] - r_add["gen_charge"], rel=1e-12)
        # 損失の割り戻し分は、実際に払う PPA 分だけ（参考額の発電側課金は含めない）
        assert r["loss_part"] == pytest.approx(delivered * s["ppa_price"] * s["loss_rate"] / (1 - s["loss_rate"]), rel=1e-9)


class TestResolveWindHandCalc:
    """§9-5 の手計算（仙台・東北・高圧・契約容量109.4kW）を resolve_wind で再現する。"""

    def _w(self, **overrides):
        return app.resolve_wind(_wind_args(**overrides), STATION_SENDAI,
                                 annual_demand_kwh=1_000_000.0, contract_type="高圧")

    def test_area_and_constants(self):
        assert app.station_to_wind_area(STATION_SENDAI) == "02"
        assert app.GEN_SIDE_CHARGE_YEN["02"] == (93.04, 0.29)
        assert app.LOSS_RATE_PCT[("02", "高圧")] == 5.2
        assert app.WIND_RETAIL_FEE_YEN == {"高圧": 4.1, "特別高圧": 4.1}
        assert app.RETAIL_GROSS_MARGIN_YEN is app.WIND_RETAIL_FEE_YEN

    def test_gen_charge_yen_matches_hand_calc(self):
        w = self._w()
        # 手計算: 93.04×12×109.4 + 0.29×278,753.5 ≈ 202,927円/年（実際のannual_kwhは形状の丸めでわずかに違いうる）
        base, energy = app.GEN_SIDE_CHARGE_YEN["02"]
        expect = 109.4 * base * 12.0 + w["annual_kwh"] * energy
        assert w["gen_charge_yen"] == pytest.approx(expect, rel=1e-9)
        assert w["gen_charge_yen"] == pytest.approx(202_927.0, rel=0.01)  # 手計算の値と概ね一致（丸め差を許容）

    def test_deliverable_and_loss_rate(self):
        w = self._w()
        assert w["loss_rate"] == pytest.approx(0.052)
        assert np.allclose(w["deliverable_30min"], w["gen_30min"] * (1 - 0.052))

    def test_defaults(self):
        w = self._w()
        assert w["payment_basis"] == "generated"
        assert w["payment_basis_label"] == app.WIND_PAYMENT_BASIS_GENERATED
        assert w["gen_charge_mode"] == "add"  # PPA単価が既定値のまま→自動で加算
        assert w["gen_charge_mode_label"] == app.WIND_GEN_CHARGE_AUTO
        assert w["balancing_yen"] == app.OFFSITE_BALANCING_YEN
        assert w["gen_charge_discount_yen"] == 0.0
        assert w["retail_fee_yen"] == 4.1

    def test_auto_gen_charge_mode_included_when_ppa_overridden(self):
        w = self._w(ppa_price=20.0)
        assert w["gen_charge_mode"] == "included"

    def test_price_sources_ledger(self):
        w = self._w()
        ps = w["price_sources"]
        assert ps["ppa_price"] == "A"
        assert ps["gen_charge"] == "B"
        assert ps["gen_charge_discount"] == "D"
        assert ps["loss_rate"] == "B"
        assert ps["balancing"] == "C"
        assert ps["wheeling"] == "B"
        assert ps["retail_fee"] == "C"
        for k, (cat, _desc) in app.PRICE_SOURCES.items():
            assert cat in ("A", "B", "C", "D", "—")

    def test_price_sources_flip_to_u_when_overridden(self):
        w = self._w(loss_rate_pct=3.0, balancing_yen=2.0, wheeling_yen=1.0, retail_fee_yen=5.0,
                    gen_charge_discount_yen=1000.0, payment_basis=app.WIND_PAYMENT_BASIS_USED)
        ps = w["price_sources"]
        assert ps["loss_rate"] == "U"
        assert ps["balancing"] == "U"
        assert ps["wheeling"] == "U"
        assert ps["retail_fee"] == "U"
        assert ps["gen_charge_discount"] == "U"
        assert ps["payment_basis"] == "U"
        assert ps["gen_charge"] == "B"  # 上書きする入力が無い項目は既定のまま

    def test_used_basis_payment_yen_is_none(self):
        w = self._w(payment_basis=app.WIND_PAYMENT_BASIS_USED)
        assert w["payment_yen"] is None


class TestValidation:
    """入力検証（§9-8 item 8）: 範囲外・未知の値はエラーにする。"""

    def test_loss_rate_100_is_error(self):
        with pytest.raises(ValueError, match="損失率"):
            app.resolve_wind(_wind_args(loss_rate_pct=100.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")

    def test_loss_rate_negative_is_error(self):
        with pytest.raises(ValueError, match="損失率"):
            app.resolve_wind(_wind_args(loss_rate_pct=-1.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")

    def test_gen_charge_discount_negative_is_error(self):
        with pytest.raises(ValueError, match="系統設備効率化割引"):
            app.resolve_wind(_wind_args(gen_charge_discount_yen=-1.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")

    def test_balancing_negative_is_error(self):
        with pytest.raises(ValueError, match="発電バランシング"):
            app.resolve_wind(_wind_args(balancing_yen=-1.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")

    def test_unknown_payment_basis_is_error(self):
        with pytest.raises(ValueError, match="支払の対象"):
            app.resolve_wind(_wind_args(payment_basis="謎"), STATION_SENDAI, 1_000_000.0, contract_type="高圧")

    def test_unknown_gen_charge_mode_is_error(self):
        with pytest.raises(ValueError, match="発電側課金"):
            app.resolve_wind(_wind_args(gen_charge_mode="謎"), STATION_SENDAI, 1_000_000.0, contract_type="高圧")

    def test_loss_rate_boundary_zero_ok(self):
        w = app.resolve_wind(_wind_args(loss_rate_pct=0.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        assert w["loss_rate"] == 0.0


class TestOffsiteLpSpec:
    """offsite_lp_spec の gen_30min が到達可能量の合計になること。発電側の支払は持たない（offsite_payment_total で計算）。"""

    def test_gen_30min_uses_deliverable(self):
        w = app.resolve_wind(_wind_args(loss_rate_pct=10.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        spec = app.offsite_lp_spec([w])
        assert np.allclose(spec["gen_30min"], w["gen_30min"] * 0.9)

    def test_gen_30min_falls_back_to_gen_when_no_deliverable_key(self):
        gen = np.full((365, 48), 1.0)
        src = {"gen_30min": gen, "wheeling_yen": 1.0, "retail_fee_yen": 1.0, "payment_yen": 0.0}
        spec = app.offsite_lp_spec([src])
        assert np.allclose(spec["gen_30min"], gen)

    def test_spec_has_no_payment(self):
        """以前の payment_yen は本番のコードが読まず、使用量払いで None になる罠だったので削除した（2026-09-26）。"""
        w = app.resolve_wind(_wind_args(payment_basis=app.WIND_PAYMENT_BASIS_USED), STATION_SENDAI,
                              1_000_000.0, contract_type="高圧")
        assert w["payment_yen"] is None
        assert "payment_yen" not in app.offsite_lp_spec([w])

    def test_payment_total_sums_sources_by_their_own_delivered(self):
        """offsite_payment_total: 電源ごとに、その電源の届いた量で支払を計算して合計する（全量払い・使用量払いの混在）。"""
        w1 = app.resolve_wind(_wind_args(capacity_kw=50.0), STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        w2 = app.resolve_wind(_wind_args(capacity_kw=30.0, payment_basis=app.WIND_PAYMENT_BASIS_USED),
                              STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        d1 = w1["deliverable_30min"] * 0.5
        d2 = w2["deliverable_30min"] * 0.3
        got = app.offsite_payment_total([w1, w2], [d1, d2])
        # 別経路: 全量払いは発電量で、使用量払いは §9-2 の式で届いた量から
        g2 = w2["annual_kwh"]
        exp1 = w1["annual_kwh"] * (w1["ppa_price"] + w1["balancing_yen"]) + w1["gen_charge_yen"]
        exp2 = float(d2.sum()) * ((w2["ppa_price"] + w2["gen_charge_yen"] / g2) / (1 - w2["loss_rate"]) + w2["balancing_yen"])
        assert got == pytest.approx(exp1 + exp2, rel=1e-9)


class TestOffsiteSourceCost:
    """offsite_source_cost: UI・MCPが共有する費用の内訳（①発電側の支払＋②届いた分の費用）。"""

    def test_breakdown(self):
        w = app.resolve_wind(_wind_args(), STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        dl = 12_345.6
        c = app.offsite_source_cost(w, dl, 4.18)
        assert c["unit_extra"] == pytest.approx(w["wheeling_yen"] + 4.18 + w["retail_fee_yen"], rel=1e-12)
        assert c["delivered_extra"] == pytest.approx(dl * c["unit_extra"], rel=1e-12)
        assert c["total"] == pytest.approx(app.offsite_payment(w, dl)["total"] + c["delivered_extra"], rel=1e-12)

    def test_used_basis_note_has_surplus_pct(self):
        note = app.used_basis_surplus_note(250.0, 1000.0, 4.1)
        assert "発電量の25.0%" in note and "4.1円" in note and "小売が負う" in note


class TestOffsiteReceivingLossAccounting:
    """offsite_receiving の量の不変条件: gen_30min == delivered/(1-loss) + wasted（発電端で釣り合う）。"""

    def test_quantity_invariant_with_loss(self):
        w = app.resolve_wind(_wind_args(loss_rate_pct=5.2), STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        demand = np.full((365, 48), 20.0)
        pv_gen = np.zeros((365, 48))
        sc_result = {"import_": np.maximum(0.0, demand - w["deliverable_30min"])}
        off = app.offsite_receiving(pv_gen, [w], demand, sc_result)
        gen_equiv = off["delivered_by_source"][0] / (1 - 0.052)
        assert np.allclose(gen_equiv + off["wasted_by_source"][0], w["gen_30min"], atol=1e-6)
        assert np.allclose(off["loss_by_source"][0], gen_equiv - off["delivered_by_source"][0], atol=1e-9)
        assert np.all(off["loss_by_source"][0] >= -1e-9)
        # 各コマで届いた量は到達可能量を超えない（§9-8 item 4）
        assert np.all(off["delivered_by_source"][0] <= w["deliverable_30min"] + 1e-9)

    def test_delivered_capped_at_deliverable_not_generation(self):
        """需要が十分大きく小売購入が0でも、届く量は到達可能量（発電量×(1−損失率)）で頭打ちになる。

        上の恒等式は offsite_receiving の定義をなぞるだけなので、上限を gen_30min に戻す誤りを検出できない
        （2026-09-26 のコードレビューで指摘）。小売購入を0にした別の状況で、上限そのものを直接確かめる。
        """
        w = app.resolve_wind(_wind_args(loss_rate_pct=5.2), STATION_SENDAI, 1_000_000.0, contract_type="高圧")
        demand = np.full((365, 48), 1e6)
        pv_gen = np.zeros((365, 48))
        sc_result = {"import_": np.zeros((365, 48))}
        off = app.offsite_receiving(pv_gen, [w], demand, sc_result)
        assert np.allclose(off["delivered_by_source"][0], w["deliverable_30min"])
        assert off["delivered"].sum() < w["gen_30min"].sum() * (1 - 0.052) + 1e-6
        assert np.all(off["wasted_by_source"][0] >= -1e-9)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
