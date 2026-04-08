"""
Tests de INTEGRACION: weather_ensemble.py
Hace llamadas reales a NOAA y Open-Meteo.
Requiere conexion a internet.

Ejecutar con:  pytest tests/test_ensemble_integration.py -v -s
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from datetime import date
import pytest
from weather_ensemble import get_ensemble_high
from weather_openmeteo import get_ensemble_forecast
from weather_kalman import apply_kalman_correction


# Marcador para tests de integracion (se pueden saltar con -m "not integration")
pytestmark = pytest.mark.integration


# ── Open-Meteo (sin NOAA, solo multi-modelo) ───────────────────────────────────

class TestOpenMeteoIntegration:
    def test_returns_forecast_for_klga(self):
        result = asyncio.run(get_ensemble_forecast("KLGA", date.today()))
        assert result is not None, "Open-Meteo no devolvio resultado para KLGA"
        assert "high_f" in result
        assert 0 < result["high_f"] < 130, f"Temperatura fuera de rango: {result['high_f']}"

    def test_returns_multiple_models(self):
        result = asyncio.run(get_ensemble_forecast("KLGA", date.today()))
        if result:
            assert result["models_available"] >= 1

    def test_confidence_boost_in_valid_range(self):
        result = asyncio.run(get_ensemble_forecast("KORD", date.today()))
        if result:
            assert 0.0 <= result["confidence_boost"] <= 0.30

    def test_unknown_station_returns_none(self):
        result = asyncio.run(get_ensemble_forecast("XXXX", date.today()))
        assert result is None


# ── Ensemble completo (NOAA + OpenMeteo + Kalman) ─────────────────────────────

class TestEnsembleIntegration:
    def test_returns_result_for_new_york(self):
        result = asyncio.run(get_ensemble_high("KLGA", date.today()))
        assert result is not None, "Ensemble no devolvio resultado para KLGA"

    def test_temperature_in_realistic_range(self):
        result = asyncio.run(get_ensemble_high("KLGA", date.today()))
        if result:
            assert -20 < result["high_f"] < 130, f"Temp irreal: {result['high_f']}"

    def test_std_dev_positive(self):
        result = asyncio.run(get_ensemble_high("KLGA", date.today()))
        if result:
            assert result["std_dev"] > 0

    def test_confidence_is_valid_value(self):
        result = asyncio.run(get_ensemble_high("KLGA", date.today()))
        if result:
            assert result["confidence"] in ("high", "medium", "low")

    def test_sources_used_not_empty(self):
        result = asyncio.run(get_ensemble_high("KLGA", date.today()))
        if result:
            assert len(result["sources_used"]) >= 1
            assert "NOAA" in result["sources_used"] or "OpenMeteo" in result["sources_used"]

    def test_chicago_station_works(self):
        result = asyncio.run(get_ensemble_high("KORD", date.today()))
        assert result is not None, "Ensemble fallo para Chicago (KORD)"

    def test_dallas_station_works(self):
        result = asyncio.run(get_ensemble_high("KDAL", date.today()))
        assert result is not None, "Ensemble fallo para Dallas (KDAL)"

    def test_returns_none_for_invalid_station(self):
        result = asyncio.run(get_ensemble_high("XXXX", date.today()))
        assert result is None


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
