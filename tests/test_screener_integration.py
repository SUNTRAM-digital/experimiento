"""
Tests de INTEGRACION: screener.py + backtesting/data_fetcher.py
Hace llamadas reales a la API de Polymarket (Gamma).
Requiere conexion a internet.

Ejecutar con:  pytest tests/test_screener_integration.py -v -s
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest
from screener import _fetch_active_weather_markets, value_screen, momentum_screen
from backtesting.data_fetcher import get_resolved_weather_markets, get_price_history


pytestmark = pytest.mark.integration


# ── Gamma API — mercados activos ───────────────────────────────────────────────

class TestGammaActiveMarkets:
    def test_fetch_returns_list(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=20))
        assert isinstance(markets, list)

    def test_each_market_has_required_fields(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=10))
        for m in markets:
            assert "title" in m
            assert "condition_id" in m
            assert "yes_price" in m
            assert 0 < m["yes_price"] < 1, f"Precio fuera de rango: {m['yes_price']}"

    def test_markets_have_minimum_volume(self):
        from screener import MIN_VOLUME
        markets = asyncio.run(_fetch_active_weather_markets(limit=20))
        for m in markets:
            assert m["volume"] >= MIN_VOLUME


# ── Gamma API — mercados resueltos (para backtest) ─────────────────────────────

class TestGammaResolvedMarkets:
    def test_fetch_resolved_returns_list(self):
        markets = asyncio.run(get_resolved_weather_markets(limit=10))
        assert isinstance(markets, list)

    def test_resolved_markets_have_outcome(self):
        markets = asyncio.run(get_resolved_weather_markets(limit=5))
        for m in markets:
            assert "resolved_yes" in m
            assert isinstance(m["resolved_yes"], bool)

    def test_resolved_markets_have_price_range(self):
        markets = asyncio.run(get_resolved_weather_markets(limit=5))
        for m in markets:
            if m.get("final_price") is not None:
                assert 0.0 <= m["final_price"] <= 1.0


# ── Price History ──────────────────────────────────────────────────────────────

class TestPriceHistory:
    def test_price_history_empty_for_invalid_id(self):
        history = asyncio.run(get_price_history("invalid_condition_id_xyz", days_back=7))
        assert isinstance(history, list)
        assert len(history) == 0

    def test_price_history_with_real_market(self):
        # Obtener un condition_id real primero
        markets = asyncio.run(get_resolved_weather_markets(limit=1))
        if not markets:
            pytest.skip("No hay mercados resueltos disponibles para probar")
        cid = markets[0]["condition_id"]
        history = asyncio.run(get_price_history(cid, days_back=30))
        assert isinstance(history, list)
        # Si hay historial, verificar estructura
        if history:
            assert "price" in history[0]
            assert "date" in history[0]
            assert 0.0 <= history[0]["price"] <= 1.0


# ── Value Screener ─────────────────────────────────────────────────────────────

class TestValueScreener:
    def test_value_screen_returns_list(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=15))
        if not markets:
            pytest.skip("No hay mercados activos")
        opps = asyncio.run(value_screen(markets))
        assert isinstance(opps, list)

    def test_value_opps_sorted_by_gap(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=20))
        if not markets:
            pytest.skip("No hay mercados activos")
        opps = asyncio.run(value_screen(markets))
        for i in range(len(opps) - 1):
            assert opps[i]["gap"] >= opps[i + 1]["gap"]

    def test_value_opps_meet_min_gap(self):
        from screener import VALUE_GAP_MIN
        markets = asyncio.run(_fetch_active_weather_markets(limit=20))
        if not markets:
            pytest.skip("No hay mercados activos")
        opps = asyncio.run(value_screen(markets, gap_threshold=VALUE_GAP_MIN))
        for op in opps:
            assert op["gap"] >= VALUE_GAP_MIN


# ── Momentum Screener ──────────────────────────────────────────────────────────

class TestMomentumScreener:
    def test_momentum_screen_returns_list(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=15))
        if not markets:
            pytest.skip("No hay mercados activos")
        opps = asyncio.run(momentum_screen(markets))
        assert isinstance(opps, list)

    def test_momentum_opps_all_positive_in_all_timeframes(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=20))
        if not markets:
            pytest.skip("No hay mercados activos")
        opps = asyncio.run(momentum_screen(markets))
        for op in opps:
            for lb in (7, 14, 30):
                key = f"mom_{lb}d"
                if op.get(key) is not None:
                    assert op[key] > 0, f"Momentum negativo en {key}: {op[key]}"

    def test_momentum_sorted_by_strength(self):
        markets = asyncio.run(_fetch_active_weather_markets(limit=20))
        if not markets:
            pytest.skip("No hay mercados activos")
        opps = asyncio.run(momentum_screen(markets))
        for i in range(len(opps) - 1):
            assert opps[i]["momentum_strength"] >= opps[i + 1]["momentum_strength"]


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
