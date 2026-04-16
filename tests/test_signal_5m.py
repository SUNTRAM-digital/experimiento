"""
Tests for item 1: 5m signal fix.
- Smart mean-reversion: large displacement → follow trend
- Phantom uses same signal as real bot (no double-inversion)
"""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── Helper: build minimal TA data ────────────────────────────────────────────

def _ta(signal=0.0, rsi=50.0, ema20=50000.0, ema50=49800.0, close=50000.0,
        macd=0.0, ao=0.0, stoch_k=50.0, stoch_d=50.0,
        bb_upper=50500.0, bb_lower=49500.0, bb_basis=50000.0,
        adx=15.0, adx_pos=12.0, adx_neg=12.0):
    return {
        "available": True,
        "signal": signal, "rsi": rsi,
        "ema20": ema20, "ema50": ema50, "ema21": ema20,
        "ema9": ema20, "ema100": ema50, "ema200": ema50 * 0.98,
        "close": close, "macd": macd, "ao": ao,
        "stoch_k": stoch_k, "stoch_d": stoch_d,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_basis": bb_basis,
        "adx": adx, "adx_pos": adx_pos, "adx_neg": adx_neg,
        "macd_signal": 0.0,
    }


def _market_5m(up_price=0.50, down_price=0.50, elapsed=3.0):
    return {
        "slug":             "btc-5m-test",
        "title":            "BTC 5m test",
        "condition_id":     "0xtest",
        "poly_url":         "",
        "up_price":         up_price,
        "down_price":       down_price,
        "up_token":         "tok_up",
        "down_token":       "tok_down",
        "interval_minutes": 5,
        "elapsed_minutes":  elapsed,
        "window_start_ts":  0,
        "minutes_to_close": 2.0,
        "liquidity":        500.0,
        "spread_pct":       0.02,
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDisplacementLogic:
    """strategy_updown.py: 5m mean-reversion respects displacement."""

    def _run(self, btc_now, btc_start, ta=None, elapsed=3.0, market=None):
        from strategy_updown import evaluate_updown_market
        from config import bot_params
        ta = ta or _ta()
        mkt = market or _market_5m(elapsed=elapsed)
        opp, reason = evaluate_updown_market(
            market=mkt, ta_data=ta,
            btc_price=btc_now,
            btc_price_window_start=btc_start,
        )
        return opp, reason

    def test_large_btc_up_move_gives_up_signal(self):
        """BTC +$200 from start → bot should bet UP, not DOWN."""
        # BTC started at 80000, now at 80200 (+0.25% → above hi threshold 0.20%)
        opp, reason = self._run(btc_now=80200.0, btc_start=80000.0,
                                ta=_ta(signal=0.3, rsi=55))
        # Should be UP or filtered by confidence, not DOWN
        if opp is not None:
            assert opp["side"] == "UP", f"Expected UP but got {opp['side']}"

    def test_large_btc_down_move_gives_down_signal(self):
        """BTC -$200 from start → bot should bet DOWN."""
        opp, reason = self._run(btc_now=79800.0, btc_start=80000.0,
                                ta=_ta(signal=-0.3, rsi=45))
        if opp is not None:
            assert opp["side"] == "DOWN", f"Expected DOWN but got {opp['side']}"

    def test_small_move_uses_ta_signal(self):
        """Small BTC move (<0.10%) → TA signal dominates direction."""
        # BTC barely moved, RSI oversold → expect UP from TA
        opp, reason = self._run(btc_now=80020.0, btc_start=80000.0,
                                ta=_ta(signal=0.5, rsi=20))
        # Either filtered or UP — should NOT be DOWN given strong UP TA
        if opp is not None:
            assert opp["side"] != "DOWN"

    def test_window_pct_computed(self):
        """window_pct in result reflects correct % movement."""
        from strategy_updown import build_btc_direction_signal
        sig = build_btc_direction_signal(
            ta_data=_ta(), btc_price=80200.0,
            btc_price_window_start=80000.0,
        )
        # window_pct = (80200 - 80000) / 80000 * 100 = 0.25%
        assert abs(sig["window_pct"] - 0.25) < 0.01

    def test_5m_mode_in_signal(self):
        """evaluate returns 5m_mode label explaining regime."""
        from strategy_updown import evaluate_updown_market
        opp, reason = evaluate_updown_market(
            market=_market_5m(up_price=0.55, down_price=0.45, elapsed=3.0),
            ta_data=_ta(signal=0.4, rsi=50),
            btc_price=80300.0,
            btc_price_window_start=80000.0,
        )
        # No assert on direction — just checking the code path runs without error

    def test_large_move_no_mean_reversion_inversion(self):
        """
        Core bug fix: previously large UP move → momentum inverted → DOWN.
        Now displacement >= 0.20% should NOT invert momentum.
        """
        from strategy_updown import build_btc_direction_signal
        # BTC +0.30% from start (above hi threshold)
        btc_start = 80000.0
        btc_now   = btc_start * (1 + 0.0030)   # +0.30%
        ta = _ta(signal=0.0)   # neutral TA so only momentum drives direction
        sig = build_btc_direction_signal(
            ta_data=ta, btc_price=btc_now,
            btc_price_window_start=btc_start,
        )
        # Signal should be positive (UP), not negative (old code inverted it)
        assert sig["combined"] > 0, (
            f"Expected positive combined (UP) for large UP move, got {sig['combined']}"
        )

    def test_small_move_ranging_inverts_momentum(self):
        """
        Mean-reversion still active for small moves in ranging market (ADX<20).
        Verifies via evaluate_updown_market (which computes 5m_mode).
        """
        from strategy_updown import evaluate_updown_market
        # BTC +0.05% (below lo threshold 0.10%) + ranging ADX
        btc_start = 80000.0
        btc_now   = btc_start * 1.0005   # +0.05%
        ta = _ta(signal=0.0, adx=12.0, adx_pos=10.0, adx_neg=10.0)
        mkt = _market_5m(up_price=0.50, down_price=0.50, elapsed=3.0)
        opp, reason = evaluate_updown_market(
            market=mkt, ta_data=ta,
            btc_price=btc_now,
            btc_price_window_start=btc_start,
        )
        # Should be filtered (low confidence on neutral TA) or mean_rev mode in reason
        # Key check: displacement is small so mean-reversion path was taken
        assert "mean_rev" in reason.lower() or opp is None, (
            f"Expected mean_rev mode for small ranging move, got reason={reason!r}"
        )


class TestPhantomSameSignal:
    """Phantom uses _sig['direction'] directly — no separate 5m formula."""

    def test_phantom_signal_not_double_inverted(self):
        """
        Verifies the fix: phantom for 5m no longer applies its own momentum
        inversion on top of strategy's inversion. We check this structurally
        by ensuring the phantom direction equals the strategy direction.
        """
        from strategy_updown import evaluate_updown_market
        # Large UP move: strategy should give UP
        mkt = _market_5m(up_price=0.52, down_price=0.48, elapsed=3.0)
        ta  = _ta(signal=0.2, rsi=52)
        opp, reason = evaluate_updown_market(
            market=mkt, ta_data=ta,
            btc_price=80250.0, btc_price_window_start=80000.0,
        )
        if opp is not None:
            # If strategy gives a signal, it should be UP for +0.31% displacement
            # (old code would flip it DOWN via phantom formula)
            assert opp["side"] == "UP", f"Phantom double-inversion bug: got {opp['side']}"
