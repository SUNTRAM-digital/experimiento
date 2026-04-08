"""
Tests: backtesting/metrics.py
Prueba el calculo de win rate, profit factor, Sharpe, drawdown y comparacion de estrategias.
Todos unitarios — sin APIs.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from backtesting.metrics import calc_metrics, compare_strategies, _empty_metrics


def make_trades(wins: int, losses: int, win_pnl=0.10, loss_pnl=-0.05) -> list[dict]:
    trades = []
    for _ in range(wins):
        trades.append({"pnl": win_pnl, "won": True})
    for _ in range(losses):
        trades.append({"pnl": loss_pnl, "won": False})
    return trades


# ── calc_metrics ───────────────────────────────────────────────────────────────

class TestCalcMetrics:
    def test_empty_trades_returns_empty_metrics(self):
        result = calc_metrics([])
        assert result["n_trades"] == 0
        assert result["valid"] is False
        assert "summary" in result

    def test_win_rate_calculation(self):
        trades = make_trades(wins=70, losses=30)
        result = calc_metrics(trades)
        assert result["win_rate"] == pytest.approx(0.70, abs=0.01)

    def test_profit_factor_calculation(self):
        # 70 wins x 0.10 = 7.0 / (30 x 0.05 = 1.5) = 4.67
        trades = make_trades(wins=70, losses=30, win_pnl=0.10, loss_pnl=-0.05)
        result = calc_metrics(trades)
        assert result["profit_factor"] == pytest.approx(7.0 / 1.5, rel=0.01)

    def test_all_wins_profit_factor_is_inf(self):
        trades = make_trades(wins=10, losses=0)
        result = calc_metrics(trades)
        assert result["profit_factor"] == float("inf") or result["profit_factor"] > 100

    def test_max_drawdown_zero_when_always_win(self):
        trades = make_trades(wins=50, losses=0)
        result = calc_metrics(trades)
        assert result["max_drawdown"] == 0.0

    def test_max_drawdown_positive_when_losses(self):
        trades = make_trades(wins=5, losses=10)
        result = calc_metrics(trades)
        assert result["max_drawdown"] > 0

    def test_total_return_is_sum_of_pnls(self):
        trades = make_trades(wins=3, losses=2, win_pnl=0.10, loss_pnl=-0.05)
        result = calc_metrics(trades)
        expected = 3 * 0.10 + 2 * (-0.05)
        assert result["total_return"] == pytest.approx(expected, abs=0.001)

    def test_n_winners_and_losers(self):
        trades = make_trades(wins=60, losses=40)
        result = calc_metrics(trades)
        assert result["n_winners"] == 60
        assert result["n_losers"] == 40

    def test_valid_strategy_passes_all_thresholds(self):
        # WR=70%, PF>1.5, MaxDD<20%, 100 trades
        # losses pequenas (-0.03) para mantener drawdown bajo 20%
        unit = [{"pnl": 0.10, "won": True}] * 7 + [{"pnl": -0.03, "won": False}] * 3
        trades = unit * 10   # 100 trades
        result = calc_metrics(trades)
        assert result["valid"] is True, f"Issues: {result['issues']}"
        assert result["issues"] == []

    def test_invalid_when_too_few_trades(self):
        trades = make_trades(wins=7, losses=3)  # solo 10 trades
        result = calc_metrics(trades)
        assert result["valid"] is False
        assert any("muestra" in issue for issue in result["issues"])

    def test_invalid_when_low_win_rate(self):
        trades = make_trades(wins=40, losses=60) * 2  # 80 trades, WR=40%
        result = calc_metrics(trades)
        assert result["valid"] is False

    def test_summary_always_present(self):
        result = calc_metrics(make_trades(wins=50, losses=50))
        assert "summary" in result
        assert isinstance(result["summary"], str)
        assert len(result["summary"]) > 0

    def test_equity_curve_starts_at_zero(self):
        trades = make_trades(wins=5, losses=5)
        result = calc_metrics(trades)
        assert result["equity_curve"][0] == 0.0

    def test_equity_curve_length(self):
        n = 20
        trades = make_trades(wins=10, losses=10)
        result = calc_metrics(trades)
        assert len(result["equity_curve"]) == n + 1  # n trades + punto inicial


# ── compare_strategies ─────────────────────────────────────────────────────────

class TestCompareStrategies:
    def test_better_strategy_ranked_first(self):
        results = {
            "bad":  calc_metrics(make_trades(wins=40, losses=60) * 2),
            "good": calc_metrics(make_trades(wins=70, losses=30)),
        }
        ranked = compare_strategies(results)
        assert ranked[0][0] == "good"

    def test_empty_strategies_returns_empty(self):
        assert compare_strategies({}) == []

    def test_zero_trade_strategies_excluded(self):
        results = {
            "empty": _empty_metrics("sin trades"),
            "good":  calc_metrics(make_trades(wins=60, losses=40)),
        }
        ranked = compare_strategies(results)
        names = [r[0] for r in ranked]
        assert "empty" not in names
        assert "good" in names

    def test_returns_list_of_tuples(self):
        results = {"a": calc_metrics(make_trades(wins=55, losses=45))}
        ranked = compare_strategies(results)
        assert isinstance(ranked, list)
        assert isinstance(ranked[0], tuple)
        assert ranked[0][0] == "a"


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
