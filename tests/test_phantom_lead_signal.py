"""v9.6.3 — Phantom usa lead signal (Browniano) cuando elapsed >= 8min.

Cambios clave:
  - phantom_dir/phantom_conf vienen del lead signal (65-95%) no TA (30-50%)
  - _ph_mom_conflict gate se omite cuando _ph_using_lead=True
  - _ph_too_early gate se omite cuando _ph_using_lead=True (lead ya requiere T>=8)
  - Fallback: TA signal cuando lead = NEUTRAL (mercado temprano / no hay lead)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_updown import calc_lead_confidence


# ── calc_lead_confidence ──────────────────────────────────────────────────────

def test_lead_neutral_before_8min():
    r = calc_lead_confidence(
        btc_now=50300, price_to_beat=50000,
        elapsed_minutes=5.0, minutes_remaining=10.0
    )
    assert r["direction"] == "NEUTRAL"
    assert r["confidence"] == 0.0


def test_lead_up_at_8min():
    r = calc_lead_confidence(
        btc_now=50300, price_to_beat=50000,
        elapsed_minutes=8.0, minutes_remaining=7.0
    )
    assert r["direction"] == "UP"
    assert r["confidence"] >= 55.0


def test_lead_down_at_8min():
    r = calc_lead_confidence(
        btc_now=49700, price_to_beat=50000,
        elapsed_minutes=8.0, minutes_remaining=7.0
    )
    assert r["direction"] == "DOWN"
    assert r["confidence"] >= 55.0


def test_lead_high_conf_large_lead():
    """Lead grande a T=8min → confianza alta."""
    r = calc_lead_confidence(
        btc_now=50500, price_to_beat=50000,
        elapsed_minutes=10.0, minutes_remaining=5.0
    )
    assert r["confidence"] >= 80.0


def test_lead_neutral_tiny_move():
    """BTC casi no se movió → NEUTRAL."""
    r = calc_lead_confidence(
        btc_now=50001, price_to_beat=50000,
        elapsed_minutes=10.0, minutes_remaining=5.0
    )
    assert r["direction"] == "NEUTRAL"


def test_lead_neutral_no_price_to_beat():
    r = calc_lead_confidence(
        btc_now=50000, price_to_beat=0,
        elapsed_minutes=10.0, minutes_remaining=5.0
    )
    assert r["direction"] == "NEUTRAL"
    assert r["confidence"] == 0.0


def test_lead_neutral_tiny_time_remaining():
    r = calc_lead_confidence(
        btc_now=50500, price_to_beat=50000,
        elapsed_minutes=14.0, minutes_remaining=0.3
    )
    assert r["direction"] == "NEUTRAL"


def test_lead_confidence_increases_with_lead():
    """Mayor lead_pct → mayor confianza."""
    r_small = calc_lead_confidence(50100, 50000, 10.0, 5.0)
    r_large = calc_lead_confidence(50500, 50000, 10.0, 5.0)
    assert r_large["confidence"] > r_small["confidence"]


def test_lead_confidence_increases_less_time():
    """Menos tiempo restante → misma ventaja es más significativa → mayor conf."""
    r_more_time = calc_lead_confidence(50300, 50000, 10.0, 10.0)
    r_less_time = calc_lead_confidence(50300, 50000, 10.0, 3.0)
    assert r_less_time["confidence"] > r_more_time["confidence"]
