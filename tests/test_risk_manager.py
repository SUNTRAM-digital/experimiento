"""
Tests: risk_manager.py (Fase 6 - Risk Manager Pro)
Prueba circuit breaker, cash buffer, auto-sizing escalonado y heatmap.
Todos unitarios — sin APIs ni archivos externos.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import risk_manager as rm_module
from risk_manager import (
    RiskManager,
    MAX_RISK_PER_TRADE_PCT,
    MIN_CASH_BUFFER_PCT,
    MAX_WEEKLY_DRAWDOWN_PCT,
    MAX_CITY_CONCENTRATION,
    AUTOSIZING_LEVELS,
)


@pytest.fixture
def rm(tmp_path, monkeypatch):
    """RiskManager fresco con archivo de estado temporal."""
    fake_file = tmp_path / "risk_state_test.json"
    monkeypatch.setattr(rm_module, "_RISK_STATE_FILE", fake_file)
    return RiskManager()


def make_position(cost_usdc=5.0, city="new york", hours_to_close=24.0, condition_id="c1"):
    return {
        "cost_usdc":      cost_usdc,
        "market_title":   f"Will {city} high exceed 90F?",
        "city":           city,
        "hours_to_close": hours_to_close,
        "condition_id":   condition_id,
        "asset":          "WEATHER",
    }


# ── check_trade — max risk per trade ──────────────────────────────────────────

class TestMaxRiskPerTrade:
    def test_trade_within_limit_allowed(self, rm):
        result = rm.check_trade(
            size_usdc=5.0, total_account_value=200.0,
            cash_available=100.0, open_positions=[],
        )
        assert result["allowed"] is True

    def test_oversized_trade_gets_adjusted(self, rm):
        # 5% de 100 = $5 por max-risk, luego auto-sizing lo reduce a $1 (sin racha)
        # El tamaño final es el mas restrictivo de los dos controles
        result = rm.check_trade(
            size_usdc=20.0, total_account_value=100.0,
            cash_available=80.0, open_positions=[],
        )
        assert result["allowed"] is True
        assert result["adjusted_size"] <= 5.0   # nunca supera max-risk
        assert len(result["warnings"]) >= 1     # al menos una advertencia de ajuste

    def test_small_trade_not_adjusted(self, rm):
        result = rm.check_trade(
            size_usdc=1.0, total_account_value=200.0,
            cash_available=150.0, open_positions=[],
        )
        assert result["adjusted_size"] == 1.0
        assert result["warnings"] == []


# ── check_trade — cash buffer ──────────────────────────────────────────────────

class TestCashBuffer:
    def test_trade_leaving_enough_cash_allowed(self, rm):
        # 30% de 100 = $30. Cash despues = 80 - 5 = 75 > 30 → OK
        result = rm.check_trade(
            size_usdc=5.0, total_account_value=100.0,
            cash_available=80.0, open_positions=[],
        )
        assert result["allowed"] is True

    def test_trade_depleting_cash_buffer_blocked(self, rm):
        # Total=100, cash=33, min_cash=30. Trade de $10:
        # max-risk ajusta a $5 → cash_after=28 < 30 → bloqueado
        result = rm.check_trade(
            size_usdc=10.0, total_account_value=100.0,
            cash_available=33.0, open_positions=[],
        )
        assert result["allowed"] is False
        assert "buffer" in result["reason"].lower()

    def test_exactly_at_buffer_limit_blocked(self, rm):
        # Cash=31, min=30, trade de $2 → queda $29 < $30
        result = rm.check_trade(
            size_usdc=2.0, total_account_value=100.0,
            cash_available=31.0, open_positions=[],
        )
        assert result["allowed"] is False


# ── check_trade — auto-sizing escalonado ──────────────────────────────────────

class TestAutoSizing:
    def test_no_streak_limits_to_first_level(self, rm):
        # Sin racha: max $1
        result = rm.check_trade(
            size_usdc=10.0, total_account_value=1000.0,
            cash_available=900.0, open_positions=[],
        )
        assert result["adjusted_size"] == AUTOSIZING_LEVELS[0]["max_usdc"]

    def test_streak_3_unlocks_5_usdc(self, rm):
        rm.current_streak = 3
        result = rm.check_trade(
            size_usdc=10.0, total_account_value=1000.0,
            cash_available=900.0, open_positions=[],
        )
        assert result["adjusted_size"] == pytest.approx(5.0, abs=0.01)

    def test_streak_15_unlocks_50_usdc(self, rm):
        rm.current_streak = 15
        result = rm.check_trade(
            size_usdc=100.0, total_account_value=5000.0,
            cash_available=4000.0, open_positions=[],
        )
        assert result["adjusted_size"] == pytest.approx(50.0, abs=0.01)

    def test_streak_25_unlocks_100_usdc(self, rm):
        rm.current_streak = 25
        result = rm.check_trade(
            size_usdc=100.0, total_account_value=10000.0,
            cash_available=8000.0, open_positions=[],
        )
        assert result["adjusted_size"] == pytest.approx(100.0, abs=0.01)

    def test_win_increments_streak(self, rm):
        assert rm.current_streak == 0
        rm.record_trade_result(won=True)
        assert rm.current_streak == 1
        rm.record_trade_result(won=True)
        assert rm.current_streak == 2

    def test_loss_resets_streak(self, rm):
        rm.current_streak = 10
        rm.record_trade_result(won=False)
        assert rm.current_streak == 0

    def test_get_autosizing_max_scales_correctly(self, rm):
        levels = [(0, 1.0), (3, 5.0), (7, 10.0), (15, 50.0), (25, 100.0)]
        for streak, expected_max in levels:
            rm.current_streak = streak
            assert rm.get_autosizing_max() == expected_max


# ── circuit breaker ───────────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_circuit_breaker_activates_on_weekly_drawdown(self, rm):
        rm.weekly_start_value = 1000.0
        rm.weekly_start_date  = datetime.now(timezone.utc)
        # Simular perdida del 16% (> 15% threshold)
        rm.update(total_account_value=840.0, open_positions=[])
        assert rm.circuit_breaker_active is True
        assert "drawdown" in rm.circuit_breaker_reason.lower()

    def test_circuit_breaker_not_activated_below_threshold(self, rm):
        rm.weekly_start_value = 1000.0
        rm.weekly_start_date  = datetime.now(timezone.utc)
        # Perdida del 10% (< 15%)
        rm.update(total_account_value=900.0, open_positions=[])
        assert rm.circuit_breaker_active is False

    def test_circuit_breaker_blocks_all_trades(self, rm):
        rm.circuit_breaker_active = True
        rm.circuit_breaker_reason = "Test"
        result = rm.check_trade(
            size_usdc=1.0, total_account_value=1000.0,
            cash_available=900.0, open_positions=[],
        )
        assert result["allowed"] is False
        assert "CIRCUIT BREAKER" in result["reason"]

    def test_circuit_breaker_resets_on_new_week(self, rm):
        rm.weekly_start_value  = 1000.0
        rm.circuit_breaker_active = True
        # Simular nueva semana (fecha de inicio hace 8 dias)
        rm.weekly_start_date = datetime.now(timezone.utc) - timedelta(days=8)
        rm.update(total_account_value=1000.0, open_positions=[])
        assert rm.circuit_breaker_active is False

    def test_manual_reset_clears_circuit_breaker(self, rm):
        rm.circuit_breaker_active = True
        rm.reset_circuit_breaker()
        assert rm.circuit_breaker_active is False


# ── city concentration ────────────────────────────────────────────────────────

class TestCityConcentration:
    def test_high_city_concentration_generates_warning(self, rm):
        rm.current_streak = 25   # desbloquear sizing
        positions = [
            make_position(cost_usdc=40.0, city="new york", condition_id="c1"),
            make_position(cost_usdc=40.0, city="new york", condition_id="c2"),
        ]
        result = rm.check_trade(
            size_usdc=20.0, total_account_value=5000.0,
            cash_available=4000.0, open_positions=positions,
            city="new york",
        )
        assert result["allowed"] is True
        assert any("concentracion" in w.lower() or "new york" in w.lower()
                   for w in result["warnings"])

    def test_diversified_portfolio_no_warning(self, rm):
        rm.current_streak = 25
        positions = [
            make_position(cost_usdc=5.0, city="new york",  condition_id="c1"),
            make_position(cost_usdc=5.0, city="chicago",   condition_id="c2"),
            make_position(cost_usdc=5.0, city="miami",     condition_id="c3"),
        ]
        result = rm.check_trade(
            size_usdc=5.0, total_account_value=5000.0,
            cash_available=4000.0, open_positions=positions,
            city="dallas",
        )
        city_warnings = [w for w in result["warnings"] if "concentracion" in w.lower()]
        assert len(city_warnings) == 0


# ── portfolio heatmap ─────────────────────────────────────────────────────────

class TestPortfolioHeatmap:
    def test_empty_positions_returns_empty(self, rm):
        result = rm.portfolio_heatmap([])
        assert result["total_deployed"] == 0.0
        assert result["by_city"] == {}

    def test_calculates_by_city(self, rm):
        positions = [
            make_position(cost_usdc=10.0, city="new york"),
            make_position(cost_usdc=5.0,  city="chicago"),
        ]
        result = rm.portfolio_heatmap(positions)
        assert "new york" in result["by_city"]
        assert result["by_city"]["new york"]["usdc"] == 10.0
        assert result["by_city"]["new york"]["pct"] == pytest.approx(10/15, rel=0.01)

    def test_calculates_by_horizon(self, rm):
        positions = [
            make_position(cost_usdc=10.0, hours_to_close=12.0),   # <24h
            make_position(cost_usdc=10.0, hours_to_close=48.0),   # 24-72h
            make_position(cost_usdc=10.0, hours_to_close=96.0),   # >72h
        ]
        result = rm.portfolio_heatmap(positions)
        assert result["by_horizon"]["<24h"]["usdc"] == 10.0
        assert result["by_horizon"]["24-72h"]["usdc"] == 10.0
        assert result["by_horizon"][">72h"]["usdc"] == 10.0

    def test_total_deployed_correct(self, rm):
        positions = [
            make_position(cost_usdc=10.0),
            make_position(cost_usdc=20.0, condition_id="c2"),
        ]
        result = rm.portfolio_heatmap(positions)
        assert result["total_deployed"] == 30.0

    def test_alerts_on_high_city_concentration(self, rm):
        positions = [
            make_position(cost_usdc=50.0, city="new york", condition_id="c1"),
            make_position(cost_usdc=5.0,  city="chicago",  condition_id="c2"),
        ]
        result = rm.portfolio_heatmap(positions)
        assert len(result["alerts"]) > 0
        assert any("new york" in a.lower() for a in result["alerts"])

    def test_no_alerts_when_diversified(self, rm):
        # Diversificado por ciudad Y por horizonte temporal
        positions = [
            make_position(cost_usdc=10.0, city="new york",    hours_to_close=12.0,  condition_id="c1"),
            make_position(cost_usdc=10.0, city="chicago",     hours_to_close=48.0,  condition_id="c2"),
            make_position(cost_usdc=10.0, city="miami",       hours_to_close=96.0,  condition_id="c3"),
            make_position(cost_usdc=10.0, city="los angeles", hours_to_close=120.0, condition_id="c4"),
        ]
        result = rm.portfolio_heatmap(positions)
        assert result["alerts"] == []


# ── status summary ─────────────────────────────────────────────────────────────

class TestStatusSummary:
    def test_summary_returns_string(self, rm):
        result = rm.status_summary(1000.0)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_summary_shows_circuit_breaker(self, rm):
        rm.circuit_breaker_active = True
        rm.circuit_breaker_reason = "Test CB"
        result = rm.status_summary(1000.0)
        assert "CIRCUIT BREAKER" in result

    def test_summary_shows_streak(self, rm):
        rm.current_streak = 7
        result = rm.status_summary(1000.0)
        assert "7" in result


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
