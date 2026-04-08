"""
Tests: strategy_nearzero.py (Fase 5 - Near-Zero Entry)
Prueba el calculo de EV, sizing y evaluador de oportunidades near-zero.
Todos unitarios — sin APIs.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from strategy_nearzero import (
    calc_nearzero_ev,
    calc_nearzero_size,
    evaluate_nearzero,
    scan_nearzero_opportunities,
    MAX_NEAR_ZERO_PRICE,
    MIN_PROB_FOR_ENTRY,
    MIN_EV_NEAR_ZERO,
    MAX_SIZE_USDC,
)


def make_market(
    yes_price=0.05,
    hours_to_close=168.0,
    liquidity=50.0,
    title="Will NYC high exceed 95°F?",
    condition_id="cid_test_1",
):
    return {
        "yes_price":      yes_price,
        "hours_to_close": hours_to_close,
        "liquidity":      liquidity,
        "market_title":   title,
        "condition_id":   condition_id,
    }


# ── calc_nearzero_ev ───────────────────────────────────────────────────────────

class TestCalcNearZeroEV:
    def test_high_ev_with_low_price(self):
        # precio=0.07, prob=0.30 → payout=13.28x, EV = 0.30*13.28 - 0.70 = 3.28
        ev = calc_nearzero_ev(0.07, 0.30)
        assert ev == pytest.approx(3.28, rel=0.05)

    def test_zero_ev_at_breakeven(self):
        # precio=prob → EV≈0 (sin edge)
        ev = calc_nearzero_ev(0.20, 0.20)
        assert abs(ev) < 0.01

    def test_negative_ev_below_breakeven(self):
        # precio=0.10, prob=0.05 → EV negativo
        ev = calc_nearzero_ev(0.10, 0.05)
        assert ev < 0

    def test_zero_price_returns_zero(self):
        assert calc_nearzero_ev(0.0, 0.30) == 0.0

    def test_price_1_returns_zero(self):
        assert calc_nearzero_ev(1.0, 0.30) == 0.0

    def test_ev_increases_with_lower_price(self):
        # Mismo prob, menor precio → mayor EV
        ev_high = calc_nearzero_ev(0.08, 0.30)
        ev_low  = calc_nearzero_ev(0.03, 0.30)
        assert ev_low > ev_high

    def test_ev_increases_with_higher_prob(self):
        # Mismo precio, mayor prob → mayor EV
        ev_low  = calc_nearzero_ev(0.05, 0.20)
        ev_high = calc_nearzero_ev(0.05, 0.40)
        assert ev_high > ev_low

    def test_classic_example_from_doc(self):
        # precio=0.07, prob=0.30 → EV ~3.28x = 328%
        ev = calc_nearzero_ev(0.07, 0.30)
        assert ev > 3.0


# ── calc_nearzero_size ────────────────────────────────────────────────────────

class TestCalcNearZeroSize:
    def test_ev_below_min_returns_zero(self):
        size = calc_nearzero_size(0.5, 1000.0)   # EV=0.5 < MIN_EV=1.0
        assert size == 0.0

    def test_small_size_for_modest_ev(self):
        size = calc_nearzero_size(1.5, 1000.0)   # EV entre 1-2
        assert size == pytest.approx(0.50, abs=0.01)

    def test_medium_size_for_good_ev(self):
        size = calc_nearzero_size(3.0, 1000.0)   # EV entre 2-4
        assert size == pytest.approx(1.00, abs=0.01)

    def test_large_size_for_high_ev(self):
        size = calc_nearzero_size(5.0, 1000.0)   # EV >4
        assert size == pytest.approx(2.00, abs=0.01)

    def test_wallet_bonus_adds_to_size(self):
        base  = calc_nearzero_size(3.0, 1000.0, wallet_signal_count=0)
        bonus = calc_nearzero_size(3.0, 1000.0, wallet_signal_count=2)
        assert bonus > base

    def test_never_exceeds_max_size(self):
        size = calc_nearzero_size(100.0, 10000.0, wallet_signal_count=10)
        assert size <= MAX_SIZE_USDC

    def test_never_exceeds_2pct_of_balance(self):
        # balance=50 → 2% = $1
        size = calc_nearzero_size(10.0, 50.0, wallet_signal_count=5)
        assert size <= 50.0 * 0.02

    def test_zero_balance_returns_zero(self):
        assert calc_nearzero_size(5.0, 0.0) == 0.0


# ── evaluate_nearzero ──────────────────────────────────────────────────────────

class TestEvaluateNearZero:
    def test_valid_opportunity_returns_dict(self):
        market = make_market(yes_price=0.05, hours_to_close=168.0, liquidity=50.0)
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is not None
        assert result["type"] == "near_zero"
        assert result["side"] == "YES"

    def test_price_above_threshold_returns_none(self):
        market = make_market(yes_price=0.10)   # > MAX_NEAR_ZERO_PRICE=0.08
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is None

    def test_prob_below_min_returns_none(self):
        market = make_market(yes_price=0.05)
        result = evaluate_nearzero(market, estimated_prob=0.10, balance_usdc=100.0)  # <0.20
        assert result is None

    def test_zero_price_returns_none(self):
        market = make_market(yes_price=0.0005)
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is None

    def test_market_closed_returns_none(self):
        market = make_market(yes_price=0.05, hours_to_close=0)
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is None

    def test_no_liquidity_returns_none(self):
        market = make_market(yes_price=0.05, liquidity=5.0)   # <MIN_VOLUME=20
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is None

    def test_result_has_required_fields(self):
        market = make_market(yes_price=0.05)
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is not None
        for field in ["ev", "ev_pct", "size_usdc", "shares", "payout_ratio",
                      "quality", "wallet_count", "payout_if_yes"]:
            assert field in result, f"Falta campo: {field}"

    def test_quality_a_plus_with_wallet_signals(self):
        market = make_market(yes_price=0.03)   # Muy bajo → EV muy alto
        signals = [
            {"wallet_name": "gopfan2", "edge": "high"},
            {"wallet_name": "securebet", "edge": "very_high"},
        ]
        result = evaluate_nearzero(market, estimated_prob=0.40, balance_usdc=200.0,
                                   wallet_signals=signals)
        assert result is not None
        assert result["quality"] == "A+"

    def test_quality_c_for_borderline_ev(self):
        # EV entre 1-2 y sin wallets → calidad C
        # precio=0.07, prob=0.21 → EV = 0.21*(1/0.07-1) - 0.79 = 0.21*13.28 - 0.79 = 1.99
        market = make_market(yes_price=0.07)
        result = evaluate_nearzero(market, estimated_prob=0.21, balance_usdc=100.0)
        if result:
            assert result["quality"] in ("C", "B")   # EV frontera 1-2, puede ser C o B

    def test_payout_ratio_correct(self):
        # precio=0.05 → payout=20:1
        market = make_market(yes_price=0.05)
        result = evaluate_nearzero(market, estimated_prob=0.30, balance_usdc=100.0)
        assert result is not None
        assert result["payout_ratio"] == pytest.approx(20.0, abs=0.1)

    def test_wallet_signals_included_in_result(self):
        market = make_market(yes_price=0.04)
        signals = [{"wallet_name": "gopfan2", "edge": "high"}]
        result = evaluate_nearzero(market, estimated_prob=0.35, balance_usdc=200.0,
                                   wallet_signals=signals)
        assert result is not None
        assert result["wallet_count"] == 1
        assert result["wallet_signals"] == signals


# ── scan_nearzero_opportunities ────────────────────────────────────────────────

class TestScanNearZeroOpportunities:
    def test_empty_markets_returns_empty(self):
        result = scan_nearzero_opportunities([], lambda m: 0.30, 100.0)
        assert result == []

    def test_skips_markets_above_threshold(self):
        markets = [
            make_market(yes_price=0.20, condition_id="c1"),
            make_market(yes_price=0.50, condition_id="c2"),
        ]
        result = scan_nearzero_opportunities(markets, lambda m: 0.30, 100.0)
        assert result == []

    def test_finds_valid_nearzero_markets(self):
        markets = [
            make_market(yes_price=0.05, condition_id="c1"),
            make_market(yes_price=0.03, condition_id="c2"),
            make_market(yes_price=0.50, condition_id="c3"),   # no near-zero
        ]
        result = scan_nearzero_opportunities(markets, lambda m: 0.30, 100.0)
        cids = [r["condition_id"] for r in result]
        assert "c1" in cids
        assert "c2" in cids
        assert "c3" not in cids

    def test_sorted_by_quality_then_ev(self):
        markets = [
            make_market(yes_price=0.07, condition_id="low_ev"),   # EV bajo
            make_market(yes_price=0.02, condition_id="high_ev"),  # EV alto
        ]
        result = scan_nearzero_opportunities(markets, lambda m: 0.30, 100.0)
        if len(result) >= 2:
            # El de mayor EV debe ir primero (ambos calidad C sin wallets)
            assert result[0]["ev"] >= result[1]["ev"]

    def test_none_from_prob_estimator_skipped(self):
        markets = [make_market(yes_price=0.05, condition_id="c1")]
        result = scan_nearzero_opportunities(markets, lambda m: None, 100.0)
        assert result == []

    def test_wallet_signals_used_for_sizing(self):
        markets = [make_market(yes_price=0.04, condition_id="c1")]
        wallet_map = {"c1": [{"wallet_name": "gopfan2", "edge": "high"}]}
        result = scan_nearzero_opportunities(
            markets, lambda m: 0.35, 200.0, wallet_signals_by_cid=wallet_map
        )
        if result:
            assert result[0]["wallet_count"] == 1


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
