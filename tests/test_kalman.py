"""
Tests: weather_kalman.py
Prueba la logica de Kalman Gain y los descuentos por nubosidad y viento.
Todos los tests son unitarios — no hacen llamadas a APIs externas.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from weather_kalman import _kalman_weight, apply_kalman_correction


# ── _kalman_weight ─────────────────────────────────────────────────────────────

class TestKalmanWeight:
    def test_before_6am_minimal_obs_weight(self):
        assert _kalman_weight(3) == 0.10

    def test_at_6am_weight(self):
        assert _kalman_weight(6) == 0.20

    def test_at_8am_weight(self):
        assert _kalman_weight(8) == 0.35

    def test_at_noon_weight(self):
        assert _kalman_weight(12) == 0.72

    def test_at_2pm_max_weight(self):
        assert _kalman_weight(14) == 0.85

    def test_late_afternoon_max_weight(self):
        assert _kalman_weight(18) == 0.85

    def test_weights_monotonically_increase(self):
        hours = [0, 4, 6, 8, 10, 11, 12, 13, 14, 20]
        weights = [_kalman_weight(h) for h in hours]
        for i in range(len(weights) - 1):
            assert weights[i] <= weights[i + 1], (
                f"Peso no monotono en hora {hours[i]}: {weights[i]} > {weights[i+1]}"
            )

    def test_all_weights_in_valid_range(self):
        for h in range(24):
            w = _kalman_weight(h)
            assert 0.0 <= w <= 1.0, f"Peso fuera de rango en hora {h}: {w}"


# ── apply_kalman_correction ────────────────────────────────────────────────────

class TestKalmanCorrection:
    def test_no_obs_returns_forecast_unchanged(self):
        result = apply_kalman_correction(80.0, None, 8)
        assert result["corrected_high_f"] == 80.0
        assert result["kalman_weight_obs"] == 0.0

    def test_blend_at_noon(self):
        # A mediodia (hora 12): peso obs = 0.72
        result = apply_kalman_correction(80.0, 76.0, 12)
        expected = 0.28 * 80.0 + 0.72 * 76.0
        assert abs(result["corrected_high_f"] - expected) < 0.1

    def test_peak_locked_after_2pm(self):
        # Despues de las 2pm: peak_locked=True, usar max(obs, forecast*0.95)
        result = apply_kalman_correction(80.0, 85.0, 15)
        assert result["peak_locked"] is True
        assert result["corrected_high_f"] == 85.0  # obs > forecast

    def test_peak_locked_uses_forecast_when_obs_lower(self):
        result = apply_kalman_correction(80.0, 70.0, 15)
        assert result["peak_locked"] is True
        # forecast*0.95 = 76 > obs 70
        assert result["corrected_high_f"] == pytest.approx(80.0 * 0.95, abs=0.1)

    def test_cloud_discount_below_60pct_no_effect(self):
        result = apply_kalman_correction(80.0, 78.0, 10, cloud_cover_pct=50)
        assert result["cloud_discount_f"] == 0.0

    def test_cloud_discount_at_100pct(self):
        result = apply_kalman_correction(80.0, 78.0, 10, cloud_cover_pct=100)
        assert result["cloud_discount_f"] == pytest.approx(2.5, abs=0.01)

    def test_cloud_discount_partial(self):
        # 80% nubes -> exceso = (80-60)/40 = 0.5 -> descuento = 0.5*2.5 = 1.25
        result = apply_kalman_correction(80.0, 78.0, 10, cloud_cover_pct=80)
        assert result["cloud_discount_f"] == pytest.approx(1.25, abs=0.01)

    def test_wind_discount_below_15mph_no_effect(self):
        result = apply_kalman_correction(80.0, 78.0, 10, wind_speed_mph=10)
        assert result["wind_discount_f"] == 0.0

    def test_wind_discount_at_35mph_max(self):
        result = apply_kalman_correction(80.0, 78.0, 10, wind_speed_mph=35)
        assert result["wind_discount_f"] == pytest.approx(2.0, abs=0.01)

    def test_both_discounts_reduce_temperature(self):
        result_clean = apply_kalman_correction(80.0, 80.0, 10)
        result_adverse = apply_kalman_correction(
            80.0, 80.0, 10, cloud_cover_pct=100, wind_speed_mph=35
        )
        assert result_adverse["corrected_high_f"] < result_clean["corrected_high_f"]

    def test_std_dev_lower_when_peak_locked(self):
        early = apply_kalman_correction(80.0, 78.0, 8)
        late = apply_kalman_correction(80.0, 78.0, 15)
        assert late["std_dev_adjusted"] < early["std_dev_adjusted"]

    def test_std_dev_4_when_no_obs(self):
        result = apply_kalman_correction(80.0, None, 8)
        assert result["std_dev_adjusted"] == 4.0


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
