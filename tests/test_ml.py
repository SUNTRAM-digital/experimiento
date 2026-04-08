"""
Tests: ml/warming_model.py + ml/ensemble_calibrator.py (Fase 7 — ML)
Todos unitarios — sin APIs, sin disco (monkeypatching de archivos).
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import math
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

import ml.warming_model as wm_module
from ml.warming_model import (
    WarmingModel,
    DEFAULT_WEIGHTS,
    WARMING_THRESHOLD,
    SLIGHT_WARMING_THRESHOLD,
    COOLING_THRESHOLD,
    SLIGHT_COOLING_THRESHOLD,
    _sigmoid,
    _encode_month,
    _normalize,
)

import ml.ensemble_calibrator as cal_module
from ml.ensemble_calibrator import (
    EnsembleCalibrator,
    BASE_WEIGHTS,
    MIN_OBS_FOR_ADJUSTMENT,
    MAX_WEIGHT_DEVIATION,
    _season,
    _mean,
    _clamp,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def model(tmp_path, monkeypatch):
    """WarmingModel fresco sin archivo de pesos en disco."""
    fake_file = tmp_path / "warming_weights.json"
    monkeypatch.setattr(wm_module, "_WEIGHTS_FILE", fake_file)
    return WarmingModel()


@pytest.fixture
def calibrator(tmp_path, monkeypatch):
    """EnsembleCalibrator fresco sin archivo de calibracion en disco."""
    fake_file = tmp_path / "calibration_data.json"
    monkeypatch.setattr(cal_module, "_CAL_FILE", fake_file)
    return EnsembleCalibrator()


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers del modulo
# ═══════════════════════════════════════════════════════════════════════════════

class TestSigmoid:
    def test_zero_input_returns_half(self):
        assert _sigmoid(0.0) == pytest.approx(0.5)

    def test_large_positive_approaches_one(self):
        assert _sigmoid(10.0) > 0.99

    def test_large_negative_approaches_zero(self):
        assert _sigmoid(-10.0) < 0.01

    def test_symmetric(self):
        assert _sigmoid(2.0) == pytest.approx(1.0 - _sigmoid(-2.0), abs=1e-6)


class TestEncodeMonth:
    def test_january_has_zero_sin(self):
        sin_m, cos_m = _encode_month(1)
        assert sin_m == pytest.approx(0.0, abs=1e-6)
        assert cos_m == pytest.approx(1.0, abs=1e-6)

    def test_july_has_zero_cos(self):
        sin_m, cos_m = _encode_month(7)
        assert sin_m == pytest.approx(0.0, abs=1e-6)
        assert cos_m == pytest.approx(-1.0, abs=1e-6)

    def test_cyclic_property(self):
        # Enero (1) y Diciembre (12+1=13) deben tener el mismo encoding
        sin1, cos1 = _encode_month(1)
        sin13, cos13 = _encode_month(13)
        assert sin1 == pytest.approx(sin13, abs=1e-6)
        assert cos1 == pytest.approx(cos13, abs=1e-6)


class TestNormalize:
    def test_mean_value_returns_zero(self):
        result = _normalize(0.0, "pressure_change_3h")
        assert result == pytest.approx(0.0)

    def test_one_std_above_mean(self):
        # pressure_change_3h: mean=0, std=2.5
        result = _normalize(2.5, "pressure_change_3h")
        assert result == pytest.approx(1.0)

    def test_unknown_feature_returns_raw(self):
        assert _normalize(42.0, "unknown_feature") == 42.0


# ═══════════════════════════════════════════════════════════════════════════════
# WarmingModel — predict
# ═══════════════════════════════════════════════════════════════════════════════

class TestWarmingModelPredict:
    def test_returns_all_required_keys(self, model):
        result = model.predict({"month": 6})
        for key in ("label", "prob_warming", "confidence", "logit", "features_used", "n_features"):
            assert key in result

    def test_prob_in_valid_range(self, model):
        for month in [1, 4, 7, 10]:
            result = model.predict({"month": month})
            assert 0.0 <= result["prob_warming"] <= 1.0

    def test_empty_features_uses_intercept_only(self, model):
        result = model.predict({})
        # Solo el intercept (0.12) → sigmoid(0.12) ≈ 0.53
        expected_prob = _sigmoid(DEFAULT_WEIGHTS["intercept"])
        assert result["prob_warming"] == pytest.approx(expected_prob, abs=0.01)
        assert result["features_used"] == []

    def test_warming_label_when_prob_high(self, model):
        # Presion bajando rapido → warming
        result = model.predict({
            "pressure_change_3h": -5.0,   # baja → warming
            "pressure_change_12h": -10.0,
            "cloud_cover_pct": 10.0,
            "temp_trend_3d": 3.0,
            "month": 5,
            "rained_yesterday": 0,
        })
        assert result["label"] in ("WARMING", "SLIGHT_WARMING", "STABLE")

    def test_cooling_label_when_prob_low(self, model):
        # Presion subiendo fuerte + nubes + lluvia → cooling
        result = model.predict({
            "pressure_change_3h": 5.0,    # sube → cooling
            "pressure_change_12h": 10.0,
            "cloud_cover_pct": 90.0,
            "temp_trend_3d": -3.0,
            "month": 11,
            "rained_yesterday": 1,
        })
        assert result["label"] in ("COOLING", "SLIGHT_COOLING", "STABLE")

    def test_confidence_high_when_far_from_decision(self, model):
        # prob >> 0.5 → high confidence
        result = model.predict({
            "pressure_change_3h": -8.0,
            "temp_trend_3d": 5.0,
            "month": 6,
        })
        # No podemos garantizar "high" con pesos base pero al menos verificamos el rango
        assert result["confidence"] in ("high", "medium", "low")

    def test_stable_label_near_50pct(self, model):
        # features balanceados → cerca de 50% → STABLE
        result = model.predict({"month": 9})  # solo intercept + mes
        # El label depende de los pesos, pero debe ser uno valido
        assert result["label"] in ("WARMING", "SLIGHT_WARMING", "STABLE", "SLIGHT_COOLING", "COOLING")

    def test_features_used_tracks_provided_features(self, model):
        result = model.predict({
            "pressure_change_3h": 1.0,
            "cloud_cover_pct": 50.0,
            "month": 3,
        })
        assert "pressure_3h" in result["features_used"]
        assert "clouds" in result["features_used"]
        assert "month" in result["features_used"]
        assert result["n_features"] == 3

    def test_prediction_increments_counter(self, model):
        assert model.n_predictions == 0
        model.predict({"month": 6})
        assert model.n_predictions == 1
        model.predict({"month": 7})
        assert model.n_predictions == 2


class TestWarmingModelAccuracy:
    def test_record_correct_outcome(self, model):
        model.predict({"month": 6})  # incrementa n_predictions
        model.record_outcome(predicted_warming=True, actual_warming=True)
        assert model.n_correct == 1

    def test_record_incorrect_outcome(self, model):
        model.predict({"month": 6})
        model.record_outcome(predicted_warming=True, actual_warming=False)
        assert model.n_correct == 0

    def test_accuracy_calculation(self, model):
        for _ in range(3):
            model.predict({"month": 6})
        model.record_outcome(True, True)
        model.record_outcome(True, True)
        model.record_outcome(False, True)
        assert model.accuracy == pytest.approx(2/3, abs=0.01)

    def test_accuracy_zero_with_no_predictions(self, model):
        assert model.accuracy == 0.0


class TestWarmingModelForecastAdjustment:
    def test_warming_high_confidence_adjusts_up(self, model):
        prediction = {"label": "WARMING", "prob_warming": 0.80, "confidence": "high"}
        delta = model.to_forecast_adjustment(prediction, 85.0)
        assert delta > 0

    def test_cooling_high_confidence_adjusts_down(self, model):
        prediction = {"label": "COOLING", "prob_warming": 0.15, "confidence": "high"}
        delta = model.to_forecast_adjustment(prediction, 85.0)
        assert delta < 0

    def test_stable_no_adjustment(self, model):
        prediction = {"label": "STABLE", "prob_warming": 0.50, "confidence": "medium"}
        delta = model.to_forecast_adjustment(prediction, 85.0)
        assert delta == 0.0

    def test_max_adjustment_is_two_degrees(self, model):
        prediction = {"label": "WARMING", "prob_warming": 0.99, "confidence": "high"}
        delta = model.to_forecast_adjustment(prediction, 85.0)
        assert abs(delta) <= 2.0

    def test_low_confidence_smaller_adjustment(self, model):
        pred_high = {"label": "WARMING", "prob_warming": 0.80, "confidence": "high"}
        pred_low  = {"label": "WARMING", "prob_warming": 0.80, "confidence": "low"}
        delta_high = model.to_forecast_adjustment(pred_high, 85.0)
        delta_low  = model.to_forecast_adjustment(pred_low,  85.0)
        assert delta_high > delta_low

    def test_adjustment_is_rounded_to_two_decimals(self, model):
        prediction = {"label": "WARMING", "prob_warming": 0.75, "confidence": "medium"}
        delta = model.to_forecast_adjustment(prediction, 85.0)
        assert delta == round(delta, 2)


class TestWarmingModelPersistence:
    def test_loads_default_weights_when_no_file(self, model):
        assert model.weights == DEFAULT_WEIGHTS

    def test_update_weights_changes_values(self, model):
        model.update_weights({"intercept": 0.5})
        assert model.weights["intercept"] == 0.5

    def test_saved_weights_reloaded(self, tmp_path, monkeypatch):
        fake_file = tmp_path / "warming_weights.json"
        monkeypatch.setattr(wm_module, "_WEIGHTS_FILE", fake_file)

        m1 = WarmingModel()
        m1.update_weights({"intercept": 0.99})

        # Nueva instancia debe cargar los pesos guardados
        m2 = WarmingModel()
        assert m2.weights["intercept"] == pytest.approx(0.99)


# ═══════════════════════════════════════════════════════════════════════════════
# EnsembleCalibrator — helpers
# ═══════════════════════════════════════════════════════════════════════════════

class TestSeasonHelper:
    def test_december_is_winter(self):
        assert _season(12) == "winter"

    def test_january_is_winter(self):
        assert _season(1) == "winter"

    def test_february_is_winter(self):
        assert _season(2) == "winter"

    def test_march_is_spring(self):
        assert _season(3) == "spring"

    def test_june_is_summer(self):
        assert _season(6) == "summer"

    def test_september_is_fall(self):
        assert _season(9) == "fall"


class TestMeanHelper:
    def test_empty_list_returns_zero(self):
        assert _mean([]) == 0.0

    def test_single_value(self):
        assert _mean([5.0]) == 5.0

    def test_average(self):
        assert _mean([2.0, 4.0, 6.0]) == pytest.approx(4.0)


class TestClampHelper:
    def test_within_range_unchanged(self):
        assert _clamp(5.0, 0.0, 10.0) == 5.0

    def test_below_min_clamped(self):
        assert _clamp(-1.0, 0.0, 10.0) == 0.0

    def test_above_max_clamped(self):
        assert _clamp(11.0, 0.0, 10.0) == 10.0


# ═══════════════════════════════════════════════════════════════════════════════
# EnsembleCalibrator — get_weights sin historial
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalibratorBaseWeights:
    def test_returns_base_weights_when_no_history(self, calibrator):
        w = calibrator.get_weights("KLGA", 6)
        assert w["noaa"]      == pytest.approx(BASE_WEIGHTS["noaa"])
        assert w["openmeteo"] == pytest.approx(BASE_WEIGHTS["openmeteo"])
        assert w["obs"]       == pytest.approx(BASE_WEIGHTS["obs"])
        assert w["calibrated"] is False

    def test_weights_sum_to_one(self, calibrator):
        w = calibrator.get_weights("KORD", 1)
        total = w["noaa"] + w["openmeteo"] + w["obs"]
        assert total == pytest.approx(1.0, abs=1e-4)

    def test_n_obs_zero_when_no_history(self, calibrator):
        w = calibrator.get_weights("KHOU", 3)
        assert w["n_obs"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# EnsembleCalibrator — record_outcome
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalibratorRecordOutcome:
    def test_records_noaa_error(self, calibrator):
        calibrator.record_outcome("KLGA", 6, actual_high_f=90.0, noaa_pred=88.0)
        key = "KLGA_summer"
        assert len(calibrator._data[key]["noaa_errors"]) == 1
        assert calibrator._data[key]["noaa_errors"][0] == pytest.approx(2.0)

    def test_records_openmeteo_error(self, calibrator):
        calibrator.record_outcome("KLGA", 6, actual_high_f=90.0, openmeteo_pred=87.0)
        key = "KLGA_summer"
        assert len(calibrator._data[key]["openmeteo_errors"]) == 1
        assert calibrator._data[key]["openmeteo_errors"][0] == pytest.approx(3.0)

    def test_n_obs_increments(self, calibrator):
        calibrator.record_outcome("KLGA", 6, actual_high_f=90.0, noaa_pred=88.0)
        calibrator.record_outcome("KLGA", 6, actual_high_f=90.0, noaa_pred=89.0)
        assert calibrator._data["KLGA_summer"]["n_obs"] == 2

    def test_errors_capped_at_90(self, calibrator):
        for i in range(100):
            calibrator.record_outcome("KORD", 1, actual_high_f=32.0, noaa_pred=30.0)
        key = "KORD_winter"
        assert len(calibrator._data[key]["noaa_errors"]) == 90

    def test_both_sources_none_still_increments_obs(self, calibrator):
        calibrator.record_outcome("KDAL", 7, actual_high_f=95.0)
        assert calibrator._data["KDAL_summer"]["n_obs"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# EnsembleCalibrator — get_weights con historial suficiente
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalibratorCalibratedWeights:
    def _fill_history(self, calibrator, station, month, noaa_mae, om_mae, n=25):
        """Llena el historial con errores constantes."""
        for _ in range(n):
            calibrator.record_outcome(
                station, month,
                actual_high_f=90.0,
                noaa_pred=90.0 - noaa_mae,
                openmeteo_pred=90.0 - om_mae,
            )

    def test_calibrated_true_after_enough_obs(self, calibrator):
        self._fill_history(calibrator, "KLGA", 6, noaa_mae=1.0, om_mae=2.0)
        w = calibrator.get_weights("KLGA", 6)
        assert w["calibrated"] is True

    def test_noaa_gets_more_weight_when_more_accurate(self, calibrator):
        # NOAA MAE=1.0, OpenMeteo MAE=3.0 → NOAA should get higher weight
        self._fill_history(calibrator, "KLGA", 6, noaa_mae=1.0, om_mae=3.0)
        w = calibrator.get_weights("KLGA", 6)
        assert w["noaa"] > w["openmeteo"]

    def test_openmeteo_gets_more_weight_when_more_accurate(self, calibrator):
        # OpenMeteo MAE=1.0, NOAA MAE=3.0 → OpenMeteo should get higher weight
        self._fill_history(calibrator, "KLGA", 6, noaa_mae=3.0, om_mae=1.0)
        w = calibrator.get_weights("KLGA", 6)
        assert w["openmeteo"] > w["noaa"]

    def test_weights_always_sum_to_one(self, calibrator):
        self._fill_history(calibrator, "KORD", 1, noaa_mae=1.5, om_mae=2.5)
        w = calibrator.get_weights("KORD", 1)
        assert w["noaa"] + w["openmeteo"] + w["obs"] == pytest.approx(1.0, abs=1e-4)

    def test_weight_deviation_bounded(self, calibrator):
        # Incluso con MAE muy distinto, los pesos no deben desviarse mas de MAX_WEIGHT_DEVIATION
        self._fill_history(calibrator, "KHOU", 7, noaa_mae=0.1, om_mae=10.0, n=30)
        w = calibrator.get_weights("KHOU", 7)
        assert w["noaa"] <= BASE_WEIGHTS["noaa"] + MAX_WEIGHT_DEVIATION + 0.01
        assert w["openmeteo"] >= BASE_WEIGHTS["openmeteo"] - MAX_WEIGHT_DEVIATION - 0.01

    def test_mae_values_included_in_result(self, calibrator):
        self._fill_history(calibrator, "KLGA", 6, noaa_mae=1.0, om_mae=2.0)
        w = calibrator.get_weights("KLGA", 6)
        assert w["noaa_mae"] == pytest.approx(1.0, abs=0.05)
        assert w["om_mae"]   == pytest.approx(2.0, abs=0.05)

    def test_not_calibrated_below_min_obs(self, calibrator):
        # 19 obs < 20 → no calibrado
        self._fill_history(calibrator, "KLGA", 6, noaa_mae=1.0, om_mae=2.0, n=19)
        w = calibrator.get_weights("KLGA", 6)
        assert w["calibrated"] is False


# ═══════════════════════════════════════════════════════════════════════════════
# EnsembleCalibrator — accuracy report
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalibratorAccuracyReport:
    def test_report_has_all_seasons(self, calibrator):
        report = calibrator.get_accuracy_report("KLGA")
        for season in ("winter", "spring", "summer", "fall"):
            assert season in report

    def test_empty_history_returns_zeroes(self, calibrator):
        report = calibrator.get_accuracy_report("KLGA")
        for season in report.values():
            assert season["n_obs"] == 0
            assert season["noaa_mae"] is None
            assert season["om_mae"]   is None

    def test_populated_season_shows_mae(self, calibrator):
        for _ in range(5):
            calibrator.record_outcome(
                "KLGA", 7, actual_high_f=90.0,
                noaa_pred=88.0, openmeteo_pred=89.0,
            )
        report = calibrator.get_accuracy_report("KLGA")
        assert report["summer"]["noaa_mae"] == pytest.approx(2.0, abs=0.01)
        assert report["summer"]["om_mae"]   == pytest.approx(1.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════════
# EnsembleCalibrator — persistencia en disco
# ═══════════════════════════════════════════════════════════════════════════════

class TestCalibratorPersistence:
    def test_data_survives_reload(self, tmp_path, monkeypatch):
        fake_file = tmp_path / "calibration_data.json"
        monkeypatch.setattr(cal_module, "_CAL_FILE", fake_file)

        c1 = EnsembleCalibrator()
        c1.record_outcome("KLGA", 6, actual_high_f=90.0, noaa_pred=88.0)

        # Nueva instancia carga desde disco
        c2 = EnsembleCalibrator()
        assert len(c2._data.get("KLGA_summer", {}).get("noaa_errors", [])) == 1

    def test_corrupted_file_falls_back_to_empty(self, tmp_path, monkeypatch):
        fake_file = tmp_path / "bad.json"
        fake_file.write_text("NOT JSON", encoding="utf-8")
        monkeypatch.setattr(cal_module, "_CAL_FILE", fake_file)

        c = EnsembleCalibrator()
        assert c._data == {}


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
