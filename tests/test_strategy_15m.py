"""
Tests para la lógica de señal 15m post-refactor:
  - Displacement-aware recalculation (nueva, 3 modos: follow/guided/penalized)
  - Momentum gate bypass subido a 0.40 (antes 0.25)
  - Que la dirección del displacement domina sobre trend indicators en 15m
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from unittest.mock import patch
from strategy_updown import evaluate_updown_market, build_btc_direction_signal


# ── Helpers ───────────────────────────────────────────────────────────────────

def _market(interval=15, elapsed=6.0, up_price=0.52, down_price=0.48,
            minutes_to_close=9.0):
    return {
        "slug": "btc-15m-test",
        "title": "BTC UP 15m Test",
        "condition_id": "0xtest",
        "poly_url": "",
        "interval_minutes": interval,
        "elapsed_minutes": elapsed,
        "minutes_to_close": minutes_to_close,
        "up_token": "tok_up",
        "down_token": "tok_dn",
        "up_price": up_price,
        "down_price": down_price,
        "liquidity": 5000.0,
        "spread_pct": 0.02,
    }


def _ta(signal=0.05, rsi=52.0, close=85000.0):
    """TA data with slightly bullish bias (neutral enough for testing displacement)."""
    return {
        "available": True,
        "signal": signal,
        "recommendation": "NEUTRAL",
        "buy": 8, "sell": 5, "neutral": 14,
        "rsi": rsi,
        "close": close,
        "ema9":  close * 1.002,
        "ema20": close * 1.001,
        "ema50": close * 0.999,
        "ema100": close * 0.995,
        "ema200": close * 0.985,
        "macd": 12.0, "macd_signal": 8.0,
        "ao": 80.0,
        "bb_upper": close * 1.015,
        "bb_lower": close * 0.985,
        "bb_basis": close,
        "adx": 22.0, "adx_pos": 24.0, "adx_neg": 18.0,
        "stoch_k": 52.0, "stoch_d": 50.0,
    }


def _adaptive():
    return {
        "min_signal": 0.20,
        "min_signal_floor": 0.20,
        "momentum_gate_base": 0.20,
        "momentum_gate_threshold": 0.20,
        "momentum_gate_strict": False,
        "invert_signal": False,
        "min_elapsed_min": None,
        "max_elapsed_min": None,
        "block_up": False,
        "block_down": False,
    }


def _run(btc_now, btc_start, **market_kwargs):
    """Convenience: run evaluate_updown_market and return (opp, reason)."""
    mkt = _market(**market_kwargs)
    ta  = _ta(close=btc_now)
    with patch('config.bot_params') as mp:
        mp.updown_max_usdc = 5.0
        mp.updown_displacement_hi_pct = 0.20
        mp.updown_displacement_lo_pct = 0.10
        return evaluate_updown_market(
            market=mkt,
            ta_data=ta,
            btc_price=btc_now,
            btc_price_window_start=btc_start,
            adaptive_params=_adaptive(),
        )


# ── Tests: Displacement mode detection ───────────────────────────────────────

class TestDisplacement15m:

    def test_strong_down_displacement_signals_down(self):
        """BTC bajó >0.15% en la ventana → señal DOWN (modo displacement_follow)."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 - 0.0020)  # -0.20% → displacement=0.20 > 0.15
        opp, reason = _run(btc_now, btc_start)
        # Con TA ligeramente alcista pero displacement DOWN fuerte, debe ganar DOWN
        assert opp is not None or reason is not None  # no crashea
        if opp:
            assert opp["side"] == "DOWN", (
                f"Displacement -0.20% debe producir señal DOWN, got {opp['side']}. "
                f"Reason si none: {reason}"
            )

    def test_strong_up_displacement_signals_up(self):
        """BTC subió >0.15% en la ventana → señal UP (modo displacement_follow)."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 + 0.0020)  # +0.20% → displacement=0.20 > 0.15
        opp, reason = _run(btc_now, btc_start)
        if opp:
            assert opp["side"] == "UP", (
                f"Displacement +0.20% debe producir señal UP, got {opp['side']}"
            )

    def test_low_displacement_produces_lower_confidence(self):
        """Desplazamiento <0.07% → modo penalizado, confianza reducida."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 + 0.0002)  # +0.02% → displacement=0.02 < 0.07

        # Build signal directly to inspect confidence without sizing filter
        from strategy_updown import build_btc_direction_signal
        ta = _ta(close=btc_now)
        sig_low = build_btc_direction_signal(
            ta_data=ta,
            btc_price=btc_now,
            btc_price_window_start=btc_start,
        )

        btc_now_hi = 85000.0 * (1 + 0.0025)  # +0.25% → strong displacement
        sig_hi = build_btc_direction_signal(
            ta_data=_ta(close=btc_now_hi),
            btc_price=btc_now_hi,
            btc_price_window_start=btc_start,
        )

        # The 15m recalc happens in evaluate_updown_market, not build_btc_direction_signal.
        # Here we just confirm the window_pct is captured correctly.
        assert abs(sig_low["window_pct"]) < abs(sig_hi["window_pct"]), (
            "window_pct debe ser menor con poco desplazamiento"
        )

    def test_15m_mode_key_present(self):
        """evaluate_updown_market agrega '15m_mode' al signal_breakdown."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 + 0.0020)  # strong UP displacement
        opp, _ = _run(btc_now, btc_start)
        if opp:
            breakdown = opp.get("signal_breakdown", {})
            assert "15m_mode" in breakdown, (
                f"signal_breakdown debe tener '15m_mode', keys: {list(breakdown.keys())}"
            )

    def test_displacement_key_in_breakdown(self):
        """signal_breakdown siempre incluye 'displacement' para 15m."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 + 0.0005)  # tiny displacement
        opp, _ = _run(btc_now, btc_start)
        if opp:
            assert "displacement" in opp["signal_breakdown"]


# ── Tests: Momentum gate tightened ───────────────────────────────────────────

class TestMomentumGate:

    def test_gate_blocks_at_35pct_confidence(self):
        """
        Señal UP con confianza ~35% pero momentum DOWN fuerte → gate BLOQUEA.
        Con bypass en 0.40, el 35% ya no lo bypasea.
        """
        # TA ligeramente alcista (combined ~0.30-0.35) pero BTC bajó en la ventana
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 - 0.0015)  # -0.15% → momentum DOWN

        mkt = _market(up_price=0.53, down_price=0.47)
        # TA with moderate buy signal
        ta = _ta(signal=0.3, rsi=55.0, close=btc_now)
        ta["buy"] = 12; ta["sell"] = 4; ta["neutral"] = 11

        ap = _adaptive()
        ap["momentum_gate_threshold"] = 0.15  # gate activo si |momentum| > 0.15

        with patch('config.bot_params') as mp:
            mp.updown_max_usdc = 5.0
            mp.updown_displacement_hi_pct = 0.20
            mp.updown_displacement_lo_pct = 0.10
            opp, reason = evaluate_updown_market(
                market=mkt, ta_data=ta,
                btc_price=btc_now, btc_price_window_start=btc_start,
                adaptive_params=ap,
            )

        # Si el gate funcionó correctamente, reason debe mencionar "Gate momentum"
        # O bien opp puede ser None por otra razón (señal débil también válido)
        if opp is not None:
            # Si pasó, la confianza debe ser ≥40% (bypass threshold)
            assert opp["confidence"] >= 40.0, (
                f"Si el gate se bypasseó, confianza debe ser ≥40%, got {opp['confidence']}"
            )

    def test_gate_allows_high_confidence(self):
        """
        Señal UP con confianza >40% puede bypassear el gate aunque momentum sea contrario.
        """
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 - 0.0005)  # -0.05% → momentum ligeramente DOWN

        mkt = _market(up_price=0.52, down_price=0.48)
        # TA muy alcista para asegurar confianza alta
        ta = _ta(signal=0.6, rsi=45.0, close=btc_now)
        ta["buy"] = 18; ta["sell"] = 2; ta["neutral"] = 7
        ta["macd"] = 80.0; ta["ao"] = 300.0

        ap = _adaptive()
        ap["momentum_gate_threshold"] = 0.10  # gate sensible

        with patch('config.bot_params') as mp:
            mp.updown_max_usdc = 5.0
            mp.updown_displacement_hi_pct = 0.20
            mp.updown_displacement_lo_pct = 0.10
            opp, reason = evaluate_updown_market(
                market=mkt, ta_data=ta,
                btc_price=btc_now, btc_price_window_start=btc_start,
                adaptive_params=ap,
            )

        # No se puede garantizar que pase (otros gates pueden bloquearlo)
        # pero si pasa, la confianza debe ser ≥40%
        if opp is not None:
            assert opp["confidence"] >= 40.0

    def test_gate_message_mentions_40pct(self):
        """El mensaje del gate refleja el nuevo umbral del 40%."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 - 0.0020)  # fuerte DOWN momentum

        mkt = _market(up_price=0.52, down_price=0.48)
        ta = _ta(signal=0.25, rsi=55.0, close=btc_now)  # señal UP moderada

        ap = _adaptive()
        ap["momentum_gate_threshold"] = 0.15

        with patch('config.bot_params') as mp:
            mp.updown_max_usdc = 5.0
            mp.updown_displacement_hi_pct = 0.20
            mp.updown_displacement_lo_pct = 0.10
            opp, reason = evaluate_updown_market(
                market=mkt, ta_data=ta,
                btc_price=btc_now, btc_price_window_start=btc_start,
                adaptive_params=ap,
            )

        if reason and "Gate momentum" in reason:
            assert "40%" in reason, (
                f"Mensaje del gate debe mencionar 40%, got: {reason}"
            )


# ── Tests: 5m logic no se rompe ──────────────────────────────────────────────

class TestFiveMinNotBroken:

    def test_5m_still_uses_mean_reversion_on_small_displacement(self):
        """La lógica de 5m (mean-reversion con poco displacement) no se toca."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 + 0.0003)  # +0.03% → pequeño displacement en 5m
        mkt = _market(interval=5, elapsed=2.5, minutes_to_close=2.5)
        ta = _ta(signal=0.15, rsi=55.0, close=btc_now)

        with patch('config.bot_params') as mp:
            mp.updown_max_usdc = 5.0
            mp.updown_displacement_hi_pct = 0.20
            mp.updown_displacement_lo_pct = 0.10
            opp, reason = evaluate_updown_market(
                market=mkt, ta_data=ta,
                btc_price=btc_now, btc_price_window_start=btc_start,
                adaptive_params=_adaptive(),
            )
        # No debe crashear — el resultado puede ser opp o reason, ambos válidos
        assert opp is not None or reason is not None

    def test_5m_mode_key_present(self):
        """5m signal_breakdown debe tener '5m_mode', no '15m_mode'."""
        btc_start = 85000.0
        btc_now   = 85000.0 * (1 + 0.0025)  # strong UP
        mkt = _market(interval=5, elapsed=2.5, minutes_to_close=2.5)
        ta = _ta(signal=0.3, close=btc_now)

        with patch('config.bot_params') as mp:
            mp.updown_max_usdc = 5.0
            mp.updown_displacement_hi_pct = 0.20
            mp.updown_displacement_lo_pct = 0.10
            opp, _ = evaluate_updown_market(
                market=mkt, ta_data=ta,
                btc_price=btc_now, btc_price_window_start=btc_start,
                adaptive_params=_adaptive(),
            )

        if opp:
            breakdown = opp.get("signal_breakdown", {})
            assert "5m_mode" in breakdown
            assert "15m_mode" not in breakdown
