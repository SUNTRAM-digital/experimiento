"""
Tests: category_tracker.py
Prueba el modelo de Win Rate Decay y la logica de bloqueo por categoria.
Usa un archivo de stats temporal para no contaminar los datos reales.
"""
import sys
import os
import json
import tempfile
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import category_tracker as ct


# Redirigir el archivo de stats a un temporal durante los tests
@pytest.fixture(autouse=True)
def temp_stats_file(tmp_path, monkeypatch):
    fake_file = tmp_path / "category_stats_test.json"
    monkeypatch.setattr(ct, "STATS_FILE", fake_file)
    yield fake_file


# ── win_rate_decay_model ───────────────────────────────────────────────────────

class TestWinRateDecayModel:
    def test_zero_categories_returns_base(self):
        assert ct.win_rate_decay_model(0.80, 0) == pytest.approx(0.80, abs=0.001)

    def test_decay_with_one_category(self):
        # WR(1) = 0.80 * e^(-0.065) ≈ 0.75
        result = ct.win_rate_decay_model(0.80, 1)
        assert result == pytest.approx(0.80 * 2.718281828 ** (-0.065), rel=0.001)

    def test_decay_increases_with_more_categories(self):
        wr1 = ct.win_rate_decay_model(0.80, 1)
        wr3 = ct.win_rate_decay_model(0.80, 3)
        wr8 = ct.win_rate_decay_model(0.80, 8)
        assert wr1 > wr3 > wr8

    def test_known_values(self):
        # Valores del docstring del modulo
        assert ct.win_rate_decay_model(1.0, 1) == pytest.approx(0.9371, rel=0.01)
        assert ct.win_rate_decay_model(1.0, 5) == pytest.approx(0.7228, rel=0.01)

    def test_result_positive(self):
        for n in range(10):
            assert ct.win_rate_decay_model(0.60, n) > 0


# ── record_trade_result ────────────────────────────────────────────────────────

class TestRecordTradeResult:
    def test_first_win_recorded(self):
        ct.record_trade_result("weather", won=True, pnl_usdc=5.0)
        status = ct.get_category_status("weather")
        assert status["total_trades"] == 1
        assert status["win_rate"] == pytest.approx(1.0, abs=0.001)

    def test_first_loss_recorded(self):
        ct.record_trade_result("weather", won=False, pnl_usdc=-3.0)
        status = ct.get_category_status("weather")
        assert status["total_trades"] == 1
        assert status["win_rate"] == 0.0

    def test_win_rate_updates_correctly(self):
        for _ in range(3):
            ct.record_trade_result("weather", won=True)
        for _ in range(1):
            ct.record_trade_result("weather", won=False)
        status = ct.get_category_status("weather")
        assert status["win_rate"] == pytest.approx(0.75, abs=0.01)

    def test_category_blocked_after_threshold(self):
        # 20 trades con WR=30% (debajo del umbral 45%)
        for _ in range(6):
            ct.record_trade_result("btc", won=True)
        for _ in range(14):
            ct.record_trade_result("btc", won=False)
        status = ct.get_category_status("btc")
        assert status["allowed"] is False

    def test_category_not_blocked_before_min_trades(self):
        # Solo 5 trades malos — aun no hay suficientes para bloquear
        for _ in range(5):
            ct.record_trade_result("updown", won=False)
        status = ct.get_category_status("updown")
        assert status["allowed"] is True

    def test_category_unblocked_when_recovers(self):
        # Primero bloquear (20 trades, WR=10%)
        for _ in range(2):
            ct.record_trade_result("weather", won=True)
        for _ in range(18):
            ct.record_trade_result("weather", won=False)
        assert ct.get_category_status("weather")["allowed"] is False

        # Luego recuperar con muchas victorias (WR sube > 50%)
        for _ in range(60):
            ct.record_trade_result("weather", won=True)
        status = ct.get_category_status("weather")
        assert status["allowed"] is True


# ── get_category_status ────────────────────────────────────────────────────────

class TestGetCategoryStatus:
    def test_no_history_returns_allowed(self):
        status = ct.get_category_status("weather")
        assert status["allowed"] is True
        assert status["total_trades"] == 0
        assert status["win_rate"] is None

    def test_warning_when_wr_below_warn_threshold(self):
        # 20 trades con WR=50% (entre 45% y 52% → advertencia)
        for _ in range(10):
            ct.record_trade_result("weather", won=True)
        for _ in range(10):
            ct.record_trade_result("weather", won=False)
        status = ct.get_category_status("weather")
        assert status["warning"] is True
        assert status["allowed"] is True   # advertencia pero no bloqueada

    def test_no_warning_before_min_trades(self):
        for _ in range(5):
            ct.record_trade_result("btc", won=False)
        status = ct.get_category_status("btc")
        assert status["warning"] is False

    def test_status_has_required_fields(self):
        status = ct.get_category_status("weather")
        for field in ["allowed", "win_rate", "total_trades", "warning", "message"]:
            assert field in status


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
