"""
Tests: strategy.py
Prueba las funciones de calculo de EV, Kelly, probabilidades, contrarian y Bayesian.
Todos unitarios — sin APIs ni archivos externos.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from strategy import (
    calc_bucket_probability,
    calc_ev,
    calc_kelly_size,
    calc_time_priority_bonus,
    detect_contrarian_signal,
    bayesian_update,
    calc_annualized_return,
)


# ── calc_bucket_probability ────────────────────────────────────────────────────

class TestBucketProbability:
    def test_range_bucket_center_prob(self):
        # forecast=90, std=3, bucket 88-92 → debe ser ~50% (forecast en centro del bucket)
        p = calc_bucket_probability(90.0, 3.0, 88.0, 92.0, "range")
        assert 0.45 <= p <= 0.55

    def test_above_bucket_high_forecast(self):
        # forecast=95, std=3, bucket >90 → prob alta (forecast bien por encima)
        p = calc_bucket_probability(95.0, 3.0, 90.0, 999.0, "above")
        assert p > 0.8

    def test_below_bucket_low_forecast(self):
        # forecast=70, std=3, bucket <80 → prob alta
        p = calc_bucket_probability(70.0, 3.0, 0.0, 80.0, "below")
        assert p > 0.9

    def test_prob_always_between_0_and_1(self):
        for forecast in [50, 70, 90, 110]:
            for std in [1, 3, 5]:
                p = calc_bucket_probability(float(forecast), float(std), 85.0, 95.0, "range")
                assert 0.0 <= p <= 1.0

    def test_invalid_bucket_type_returns_0(self):
        p = calc_bucket_probability(90.0, 3.0, 85.0, 95.0, "invalid_type")
        assert p == 0.0


# ── calc_ev ───────────────────────────────────────────────────────────────────

class TestCalcEV:
    def test_positive_ev_when_our_prob_higher(self):
        # Nuestra prob=70%, precio mercado=50% → EV positivo
        ev = calc_ev(0.70, 0.50)
        assert ev > 0

    def test_negative_ev_when_our_prob_lower(self):
        # Nuestra prob=40%, precio mercado=60% → EV negativo
        ev = calc_ev(0.40, 0.60)
        assert ev < 0

    def test_zero_ev_when_equal(self):
        ev = calc_ev(0.60, 0.60)
        assert abs(ev) < 0.01

    def test_spread_reduces_ev(self):
        ev_no_spread = calc_ev(0.70, 0.50, spread_pct=0.0)
        ev_with_spread = calc_ev(0.70, 0.50, spread_pct=0.02)
        assert ev_with_spread < ev_no_spread


# ── calc_kelly_size ───────────────────────────────────────────────────────────

class TestCalcKellySize:
    def test_positive_edge_returns_positive_size(self):
        # prob=0.70, precio=0.50, balance=1000 → size positivo
        size = calc_kelly_size(0.70, 0.50, 1000.0)
        assert size > 0

    def test_negative_edge_returns_min_or_zero(self):
        # prob=0.30, precio=0.60 → edge negativo → size=0
        size = calc_kelly_size(0.30, 0.60, 1000.0)
        assert size == 0.0

    def test_result_never_exceeds_max_position(self):
        from config import bot_params
        size = calc_kelly_size(0.99, 0.01, 10000.0)
        assert size <= bot_params.max_position_usdc

    def test_result_at_least_min_position_when_edge_exists(self):
        from config import bot_params
        size = calc_kelly_size(0.70, 0.50, 1000.0)
        assert size >= bot_params.min_position_usdc

    def test_invalid_price_returns_zero(self):
        assert calc_kelly_size(0.70, 0.0,  1000.0) == 0.0
        assert calc_kelly_size(0.70, 1.0,  1000.0) == 0.0
        assert calc_kelly_size(0.70, 1.01, 1000.0) == 0.0


# ── calc_time_priority_bonus ──────────────────────────────────────────────────

class TestTimePriorityBonus:
    def test_zero_hours_returns_zero(self):
        assert calc_time_priority_bonus(0) == 0.0

    def test_negative_hours_returns_zero(self):
        assert calc_time_priority_bonus(-5) == 0.0

    def test_under_6h_max_bonus(self):
        assert calc_time_priority_bonus(3) == 0.50

    def test_under_24h_high_bonus(self):
        assert calc_time_priority_bonus(12) == 0.35

    def test_under_48h_medium_bonus(self):
        assert calc_time_priority_bonus(36) == 0.20

    def test_under_72h_small_bonus(self):
        assert calc_time_priority_bonus(60) == 0.10

    def test_over_72h_no_bonus(self):
        assert calc_time_priority_bonus(100) == 0.0


# ── detect_contrarian_signal ──────────────────────────────────────────────────

class TestContrarianSignal:
    def test_sell_yes_when_price_too_high(self):
        # Precio YES=92%, nuestra prob=80% → crowd sobrecomprado → SELL_YES
        signal = detect_contrarian_signal(0.92, 0.80)
        assert signal is not None
        assert signal["signal"] == "SELL_YES"

    def test_buy_yes_when_price_too_low(self):
        # Precio YES=5%, nuestra prob=25% → crowd sobrevendido → BUY_YES
        signal = detect_contrarian_signal(0.05, 0.25)
        assert signal is not None
        assert signal["signal"] == "BUY_YES"

    def test_no_signal_when_price_is_mid(self):
        # Precio=50%, sin extremo → no señal
        signal = detect_contrarian_signal(0.50, 0.45)
        assert signal is None

    def test_no_signal_when_deviation_too_small(self):
        # Precio=90% pero prob=87% → diferencia de solo 3%, menor al umbral de 6%
        signal = detect_contrarian_signal(0.90, 0.87)
        assert signal is None

    def test_no_signal_at_threshold_boundary(self):
        # Precio=88% exacto pero prob=83% → diferencia=5%, < 6%
        signal = detect_contrarian_signal(0.88, 0.83)
        assert signal is None

    def test_buy_yes_at_exact_low_threshold(self):
        # Precio=12% exacto (<=), prob=19% → diferencia=7% → señal
        signal = detect_contrarian_signal(0.12, 0.19)
        assert signal is not None
        assert signal["signal"] == "BUY_YES"

    def test_signal_includes_deviation(self):
        signal = detect_contrarian_signal(0.92, 0.80)
        assert "deviation" in signal
        assert signal["deviation"] == pytest.approx(0.12, abs=0.001)


# ── bayesian_update ───────────────────────────────────────────────────────────

class TestBayesianUpdate:
    def test_obs_matching_bucket_increases_prob(self):
        # Bucket: temp entre 88-92°F. Observamos 90°F → prob debe subir
        updated = bayesian_update(
            prior_prob=0.50,
            obs_temp_f=90.0,
            bucket_low=88.0, bucket_high=92.0,
            bucket_type="range",
            forecast_high=85.0, std_dev=4.0,
        )
        assert updated > 0.50

    def test_obs_opposing_bucket_decreases_prob(self):
        # Bucket: temp >90°F. Observamos 60°F → prob debe bajar
        updated = bayesian_update(
            prior_prob=0.60,
            obs_temp_f=60.0,
            bucket_low=90.0, bucket_high=999.0,
            bucket_type="above",
            forecast_high=88.0, std_dev=4.0,
        )
        assert updated < 0.60

    def test_result_always_between_0_and_1(self):
        for prior in [0.1, 0.3, 0.5, 0.7, 0.9]:
            for obs in [50.0, 70.0, 90.0, 110.0]:
                updated = bayesian_update(
                    prior_prob=prior,
                    obs_temp_f=obs,
                    bucket_low=85.0, bucket_high=95.0,
                    bucket_type="range",
                    forecast_high=90.0, std_dev=4.0,
                )
                assert 0.0 <= updated <= 1.0, (
                    f"Prob fuera de rango: prior={prior}, obs={obs}, result={updated}"
                )

    def test_smoothing_prevents_zero_or_one(self):
        # Con obs muy extrema, no debe llegar a exactamente 0 o 1
        p_high = bayesian_update(0.99, 200.0, 85.0, 95.0, "range", 90.0, 4.0)
        p_low  = bayesian_update(0.01, 90.0,  85.0, 95.0, "range", 90.0, 4.0)
        assert p_high < 1.0
        assert p_low  > 0.0

    def test_unknown_bucket_type_returns_prior(self):
        updated = bayesian_update(0.65, 90.0, 85.0, 95.0, "unknown", 90.0, 4.0)
        assert updated == 0.65


# ── calc_annualized_return ────────────────────────────────────────────────────

class TestAnnualizedReturn:
    def test_short_hold_amplifies_return(self):
        # 15% edge en 2 dias vs 30 dias → retorno anualizado mucho mayor
        ann_2d  = calc_annualized_return(0.15, 2)
        ann_30d = calc_annualized_return(0.15, 30)
        assert ann_2d > ann_30d

    def test_zero_edge_returns_zero(self):
        ann = calc_annualized_return(0.0, 7)
        assert ann == pytest.approx(0.0, abs=0.001)

    def test_positive_edge_positive_return(self):
        ann = calc_annualized_return(0.10, 7)
        assert ann > 0


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
