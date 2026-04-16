"""
Tests for item 7: Real market signal trading (not alternating sides).
- invert_signal requires MIN_INVERT_SAMP samples (not just 5)
- TA consensus penalty reduces confidence when indicators are noisy
- Bot trades based on actual market data, not recent loss patterns
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ta(signal=0.0, rsi=50.0, buy=10, sell=3, neutral=14,
        ema20=80200.0, ema50=80000.0, close=80200.0,
        macd=0.0, ao=0.0, stoch_k=52.0, stoch_d=50.0,
        bb_upper=80700.0, bb_lower=79700.0, bb_basis=80200.0,
        adx=18.0, adx_pos=15.0, adx_neg=12.0):
    return {
        "available": True, "signal": signal, "rsi": rsi,
        "buy": buy, "sell": sell, "neutral": neutral,
        "ema20": ema20, "ema50": ema50, "ema21": ema20,
        "ema9": ema20, "ema100": ema50, "ema200": ema50 * 0.98,
        "close": close, "macd": macd, "ao": ao,
        "stoch_k": stoch_k, "stoch_d": stoch_d,
        "bb_upper": bb_upper, "bb_lower": bb_lower, "bb_basis": bb_basis,
        "adx": adx, "adx_pos": adx_pos, "adx_neg": adx_neg,
        "macd_signal": 0.0, "recommendation": "NEUTRAL",
    }


class TestInvertSignalThreshold:
    """invert_signal requires MIN_INVERT_SAMP samples, not just MIN_SAMPLES."""

    def test_min_invert_samples_higher_than_min_samples(self):
        """_MIN_INVERT_SAMP must be > _MIN_SAMPLES to prevent oscillation."""
        from updown_learner import _MIN_SAMPLES, _MIN_INVERT_SAMP
        assert _MIN_INVERT_SAMP > _MIN_SAMPLES, (
            f"_MIN_INVERT_SAMP ({_MIN_INVERT_SAMP}) must be > _MIN_SAMPLES ({_MIN_SAMPLES})"
        )
        assert _MIN_INVERT_SAMP >= 20, f"Need at least 20 samples to invert, got {_MIN_INVERT_SAMP}"

    def test_min_samples_at_least_10(self):
        """_MIN_SAMPLES >= 10 to avoid adapting on noise."""
        from updown_learner import _MIN_SAMPLES
        assert _MIN_SAMPLES >= 10, f"_MIN_SAMPLES should be >=10 to avoid overfitting, got {_MIN_SAMPLES}"

    def test_invert_threshold_below_35pct(self):
        """_INVERT_THRESHOLD < 0.35 to require clearer evidence before inverting."""
        from updown_learner import _INVERT_THRESHOLD
        assert _INVERT_THRESHOLD <= 0.32, (
            f"_INVERT_THRESHOLD should be <= 0.32 (conservative), got {_INVERT_THRESHOLD}"
        )

    def test_few_trades_no_invert(self):
        """With <25 trades, invert_signal stays False even if TA was wrong."""
        from updown_learner import get_adaptive_params, _stats, _bucket
        import copy
        # Save and mock stats
        orig = copy.deepcopy(_stats)
        try:
            _stats["5"] = {
                "total": 10, "wins": 4,
                "recent": [0,0,0,1,0,1,0,0,0,1],
                "dir_correct": 3, "dir_total": 10,  # 30% accuracy — would trigger old logic
                "by_signal": {"weak": _bucket(), "med": _bucket(), "strong": _bucket()},
                "by_elapsed": {"early": _bucket(), "mid": _bucket(), "late": _bucket()},
                "by_side": {"UP": _bucket(), "DOWN": _bucket()},
                "by_momentum": {"weak": _bucket(), "strong_agree": _bucket(), "strong_conflict": _bucket()},
                "by_ta_mom": {"agree": _bucket(), "conflict": _bucket()},
            }
            params = get_adaptive_params(5)
            assert params.get("invert_signal") is False, (
                f"invert_signal should be False with only 10 samples, got {params.get('invert_signal')}"
            )
        finally:
            _stats.clear()
            _stats.update(orig)


class TestTAConsensusQuality:
    """TA consensus penalty: weak TA + no displacement = reduced confidence."""

    def test_ta_consensus_in_signal(self):
        """build_btc_direction_signal returns ta_consensus field."""
        from strategy_updown import build_btc_direction_signal
        # buy=10, sell=3, neutral=14 → consensus = 13/27 ≈ 0.48
        ta = _ta(buy=10, sell=3, neutral=14)
        sig = build_btc_direction_signal(
            ta_data=ta, btc_price=80000.0, btc_price_window_start=80000.0
        )
        assert "ta_consensus" in sig
        assert 0.40 < sig["ta_consensus"] < 0.55

    def test_weak_ta_no_displacement_reduces_confidence(self):
        """buy=1, sell=1, neutral=25 + no BTC move → confidence penalized."""
        from strategy_updown import build_btc_direction_signal
        # Extremely noisy TA: 2 directional out of 27
        ta = _ta(signal=0.3, buy=1, sell=1, neutral=25)
        sig_noisy = build_btc_direction_signal(
            ta_data=ta, btc_price=80000.0, btc_price_window_start=80000.0  # no displacement
        )
        # Strong TA: 15 directional out of 27
        ta_strong = _ta(signal=0.3, buy=15, sell=3, neutral=9)
        sig_strong = build_btc_direction_signal(
            ta_data=ta_strong, btc_price=80000.0, btc_price_window_start=80000.0
        )
        assert sig_noisy["ta_consensus"] < 0.15
        # Noisy signal should have lower confidence than strong signal
        assert sig_noisy["confidence"] < sig_strong["confidence"], (
            f"Noisy TA should have lower confidence: {sig_noisy['confidence']} vs {sig_strong['confidence']}"
        )

    def test_weak_ta_with_displacement_not_penalized(self):
        """Weak TA but strong BTC displacement → no penalty (displacement drives signal)."""
        from strategy_updown import build_btc_direction_signal
        # Very noisy TA but BTC moved +0.5% (above hi threshold 0.20%)
        ta = _ta(signal=0.0, buy=1, sell=1, neutral=25)
        sig = build_btc_direction_signal(
            ta_data=ta, btc_price=80400.0,   # +0.5% from 80000
            btc_price_window_start=80000.0
        )
        # window_pct = 0.5%, above _lo=0.10%, no penalty applied
        assert sig["window_pct"] > 0.10
        # Confidence should not be zeroed (displacement compensates)
        # ta_consensus is low but penalty not applied
        assert sig["ta_consensus"] < 0.15  # confirm TA is indeed noisy
        # No strong assertion on direction here (5m mode handles it separately)
        # Just verify it doesn't crash and returns reasonable values
        assert "confidence" in sig
        assert "ta_consensus" in sig

    def test_strong_ta_consensus_high(self):
        """TradingView strong BUY (buy=20, sell=2, neutral=5) → high consensus."""
        from strategy_updown import build_btc_direction_signal
        ta = _ta(signal=0.8, buy=20, sell=2, neutral=5)
        sig = build_btc_direction_signal(
            ta_data=ta, btc_price=80000.0, btc_price_window_start=80000.0
        )
        assert sig["ta_consensus"] > 0.70
        assert sig["direction"] == "UP"
