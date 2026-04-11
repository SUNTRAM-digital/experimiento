"""
Tests para el motor de backtesting en tiempo real (Fase 10).
Ejecutar con: python -m pytest tests/test_backtesting.py -v
"""
import json
import sys
import tempfile
from pathlib import Path
import pytest

# ── Setup: usar directorio temporal para datos ────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_data_dir(monkeypatch, tmp_path):
    """Redirige _DATA_DIR a un directorio temporal para que los tests no contaminen datos reales."""
    import backtesting_rt as bt
    monkeypatch.setattr(bt, "_DATA_DIR",   tmp_path)
    monkeypatch.setattr(bt, "_CSV_FILE",   tmp_path / "backtest_trades.csv")
    monkeypatch.setattr(bt, "_STATE_FILE", tmp_path / "backtest_state.json")
    # Forzar reinicio del motor
    engine = bt.BacktestEngine()
    monkeypatch.setattr(bt, "backtest_engine", engine)
    return engine


def _make_updown_market(interval=15, elapsed=4.0, minutes_to_close=11.0,
                        up_price=0.52, down_price=0.48, btc_start=85000,
                        use_future_window=False):
    import time
    now_ts = int(time.time())
    if use_future_window:
        # Ventana activa: inició hace 4 min, cierra en 11 min
        window_start_ts = now_ts - int(elapsed * 60)
    else:
        # Ventana pasada (por defecto) — para tests que necesitan resolver
        window_start_ts = now_ts - int((elapsed + minutes_to_close + 5) * 60)
    return {
        "title":            f"BTC Up or Down {interval}m",
        "condition_id":     "cond123",
        "interval_minutes": interval,
        "elapsed_minutes":  elapsed,
        "minutes_to_close": minutes_to_close,
        "window_start_ts":  window_start_ts,
        "up_price":         up_price,
        "down_price":       down_price,
        "liquidity":        500.0,
        "spread_pct":       0.02,
    }


def _make_signal(combined=0.35, ofi=0.1, rsi=45.0):
    return {
        "combined":         combined,
        "ta":               combined * 0.8,
        "ta_raw":           combined * 0.8,
        "ta_recommendation": "BUY" if combined > 0 else "SELL",
        "direction":        "UP" if combined > 0 else "DOWN",
        "confidence":       round(abs(combined) * 100, 1),
        "ofi":              ofi,
        "rsi":              rsi,
        "ema20":            85000.0,
        "momentum":         0.05,
        "macro":            0.1,
        "market_sig":       0.05,
        "window_pct":       0.05,
        "macd_sig":         0.02,
        "ema_sig":          0.1,
    }


def _make_weather_opp(ev_pct=8.5, side="YES", market_prob=0.55):
    return {
        "condition_id":  "weather_cond456",
        "market_title":  "Will NYC max temp be above 70°F on April 15?",
        "side":          side,
        "market_prob":   market_prob,
        "ev_pct":        ev_pct,
        "hours_to_close": 24.0,
    }


# ── Tests: record_updown ──────────────────────────────────────────────────────

class TestRecordUpdown:

    def test_records_trade_with_sufficient_confidence(self, tmp_data_dir):
        engine = tmp_data_dir
        market = _make_updown_market()
        signal = _make_signal(combined=0.35)  # 35% confianza > 10% mínimo

        trade = engine.record_updown(
            market=market,
            opp=None,
            signal=signal,
            btc_start_price=85000.0,
        )
        assert trade is not None
        assert trade["side"] == "UP"
        assert trade["asset"] == "BTC_UPDOWN_15M"
        assert trade["confidence"] == pytest.approx(0.35, abs=0.01)
        assert trade["btc_start_price"] == 85000.0
        assert trade["resolved"] is False
        assert trade["result"] == "PENDING"
        assert len(engine.sim_positions) == 1

    def test_rejects_low_confidence(self, tmp_data_dir):
        engine = tmp_data_dir
        market = _make_updown_market()
        signal = _make_signal(combined=0.05)  # 5% < 10% mínimo

        trade = engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)
        assert trade is None
        assert len(engine.sim_positions) == 0

    def test_deducts_cost_from_balance(self, tmp_data_dir):
        engine = tmp_data_dir
        initial = engine.sim_balance
        market = _make_updown_market()
        signal = _make_signal(combined=0.30)

        trade = engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)
        assert trade is not None
        assert engine.sim_balance == pytest.approx(initial - trade["cost_sim"], abs=0.001)

    def test_side_up_when_combined_positive(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = engine.record_updown(
            market=_make_updown_market(),
            opp=None,
            signal=_make_signal(combined=0.40),
            btc_start_price=85000.0,
        )
        assert trade["side"] == "UP"
        assert trade["entry_price"] == pytest.approx(0.52, abs=0.001)

    def test_side_down_when_combined_negative(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = engine.record_updown(
            market=_make_updown_market(),
            opp=None,
            signal=_make_signal(combined=-0.40),
            btc_start_price=85000.0,
        )
        assert trade["side"] == "DOWN"
        assert trade["entry_price"] == pytest.approx(0.48, abs=0.001)

    def test_marks_real_trade_placed(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = engine.record_updown(
            market=_make_updown_market(),
            opp={"side": "UP", "ev_pct": 12.0},
            signal=_make_signal(combined=0.35),
            btc_start_price=85000.0,
            real_trade_placed=True,
        )
        assert trade["real_trade_placed"] is True

    def test_rejects_degenerate_market(self, tmp_data_dir):
        engine = tmp_data_dir
        market = _make_updown_market(up_price=0.99, down_price=0.01)
        trade = engine.record_updown(
            market=market, opp=None,
            signal=_make_signal(combined=0.50),
            btc_start_price=85000.0,
        )
        assert trade is None


# ── Tests: record_weather ─────────────────────────────────────────────────────

class TestRecordWeather:

    def test_records_weather_trade(self, tmp_data_dir):
        engine = tmp_data_dir
        opp = _make_weather_opp(ev_pct=8.5)
        trade = engine.record_weather(opp=opp)
        assert trade is not None
        assert trade["asset"] == "WEATHER"
        assert trade["side"] == "YES"
        assert trade["condition_id"] == "weather_cond456"
        assert trade["resolved"] is False

    def test_rejects_low_ev(self, tmp_data_dir):
        engine = tmp_data_dir
        opp = _make_weather_opp(ev_pct=0.5)  # 0.5% < 1% mínimo
        trade = engine.record_weather(opp=opp)
        assert trade is None

    def test_deducts_cost(self, tmp_data_dir):
        engine = tmp_data_dir
        initial = engine.sim_balance
        opp = _make_weather_opp(ev_pct=10.0)
        trade = engine.record_weather(opp=opp)
        assert trade is not None
        assert engine.sim_balance < initial


# ── Tests: resolve_updown_trades ─────────────────────────────────────────────

class TestResolveUpdown:

    def _record_and_expire(self, engine, combined=0.35, btc_start=85000.0):
        """Registra un trade y manipula su end_ts para que ya haya expirado."""
        market = _make_updown_market()
        signal = _make_signal(combined=combined)
        trade = engine.record_updown(
            market=market, opp=None, signal=signal, btc_start_price=btc_start,
        )
        if trade:
            # Forzar expiración: end_ts = hace 60 segundos
            import time
            tid = trade["trade_id"]
            engine.sim_positions[tid]["_end_ts"] = int(time.time()) - 60
        return trade

    def test_resolves_win_when_btc_went_up(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = self._record_and_expire(engine, combined=0.35, btc_start=85000.0)
        assert trade is not None

        n = engine.resolve_updown_trades(btc_price_now=86000.0)  # BTC subió
        assert n == 1
        assert len(engine.sim_positions) == 0
        assert len(engine.sim_trades) == 1
        assert engine.sim_trades[0]["result"] == "WIN"
        assert engine.sim_trades[0]["pnl_usdc"] > 0

    def test_resolves_loss_when_btc_went_down(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = self._record_and_expire(engine, combined=0.35, btc_start=85000.0)
        assert trade is not None

        n = engine.resolve_updown_trades(btc_price_now=84000.0)  # BTC bajó pero apostamos UP
        assert n == 1
        assert engine.sim_trades[0]["result"] == "LOSS"
        assert engine.sim_trades[0]["pnl_usdc"] < 0

    def test_restores_balance_on_win(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = self._record_and_expire(engine, combined=0.35, btc_start=85000.0)
        balance_after_trade = engine.sim_balance

        engine.resolve_updown_trades(btc_price_now=86000.0)
        # Tras WIN: balance = balance_after_trade + size_sim * 1.0
        assert engine.sim_balance > balance_after_trade

    def test_does_not_resolve_if_window_not_closed(self, tmp_data_dir):
        engine = tmp_data_dir
        # use_future_window=True → window_start_ts is recent, end_ts is still in the future
        market = _make_updown_market(use_future_window=True)
        signal = _make_signal(combined=0.35)
        engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)

        # end_ts está en el futuro → no debe resolverse
        n = engine.resolve_updown_trades(btc_price_now=86000.0)
        assert n == 0
        assert len(engine.sim_positions) == 1

    def test_down_side_wins_when_btc_falls(self, tmp_data_dir):
        engine = tmp_data_dir
        trade = self._record_and_expire(engine, combined=-0.40, btc_start=85000.0)  # DOWN
        assert trade is not None
        assert trade["side"] == "DOWN"

        n = engine.resolve_updown_trades(btc_price_now=84000.0)  # BTC bajó → DOWN gana
        assert n == 1
        assert engine.sim_trades[0]["result"] == "WIN"


# ── Tests: resolve_weather_trade ─────────────────────────────────────────────

class TestResolveWeather:

    def test_resolves_weather_win(self, tmp_data_dir):
        engine = tmp_data_dir
        opp = _make_weather_opp(ev_pct=8.5)
        engine.record_weather(opp=opp)

        ok = engine.resolve_weather_trade("weather_cond456", won=True)
        assert ok is True
        assert len(engine.sim_positions) == 0
        assert engine.sim_trades[0]["result"] == "WIN"

    def test_resolves_weather_loss(self, tmp_data_dir):
        engine = tmp_data_dir
        opp = _make_weather_opp(ev_pct=8.5)
        engine.record_weather(opp=opp)

        ok = engine.resolve_weather_trade("weather_cond456", won=False)
        assert ok is True
        assert engine.sim_trades[0]["result"] == "LOSS"

    def test_returns_false_if_not_found(self, tmp_data_dir):
        engine = tmp_data_dir
        ok = engine.resolve_weather_trade("nonexistent_cond", won=True)
        assert ok is False


# ── Tests: get_stats ──────────────────────────────────────────────────────────

class TestGetStats:

    def test_empty_stats(self, tmp_data_dir):
        engine = tmp_data_dir
        s = engine.get_stats()
        assert s["total"] == 0
        assert s["win_rate"] is None
        assert s["sim_balance"] == pytest.approx(100.0, abs=0.01)

    def test_stats_after_trades(self, tmp_data_dir):
        import time
        engine = tmp_data_dir

        # Registrar 2 trades y resolverlos
        for _ in range(2):
            market = _make_updown_market()
            signal = _make_signal(combined=0.35)
            trade = engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)
            if trade:
                engine.sim_positions[trade["trade_id"]]["_end_ts"] = int(time.time()) - 60

        engine.resolve_updown_trades(btc_price_now=86000.0)  # ambos UP ganan

        s = engine.get_stats()
        assert s["resolved"] == 2
        assert s["wins"] == 2
        assert s["win_rate"] == pytest.approx(1.0, abs=0.01)
        assert s["total_pnl"] > 0
        assert "BTC_UPDOWN_15M" in s["by_asset"]

    def test_by_real_breakdown(self, tmp_data_dir):
        import time
        engine = tmp_data_dir

        market = _make_updown_market()
        signal = _make_signal(combined=0.35)
        trade = engine.record_updown(
            market=market, opp=None, signal=signal,
            btc_start_price=85000.0, real_trade_placed=True,
        )
        engine.sim_positions[trade["trade_id"]]["_end_ts"] = int(time.time()) - 60
        engine.resolve_updown_trades(btc_price_now=86000.0)

        s = engine.get_stats()
        assert s["by_real"]["when_real_traded"]["total"] == 1
        assert s["by_real"]["when_real_skipped"]["total"] == 0


# ── Tests: persistencia ───────────────────────────────────────────────────────

class TestPersistence:

    def test_state_saved_and_loaded(self, tmp_data_dir, tmp_path, monkeypatch):
        import backtesting_rt as bt
        engine = tmp_data_dir

        market = _make_updown_market()
        signal = _make_signal(combined=0.30)
        engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)
        initial_balance = engine.sim_balance

        # Crear nuevo engine desde los mismos archivos (simula reinicio)
        engine2 = bt.BacktestEngine()
        assert engine2.sim_balance == pytest.approx(initial_balance, abs=0.001)
        assert len(engine2.sim_positions) == 1

    def test_csv_created_with_header(self, tmp_data_dir, tmp_path):
        csv_path = tmp_path / "backtest_trades.csv"
        assert csv_path.exists()
        content = csv_path.read_text()
        assert "trade_id" in content
        assert "pnl_usdc" in content

    def test_csv_appended_on_resolution(self, tmp_data_dir, tmp_path):
        import time
        engine = tmp_data_dir
        csv_path = tmp_path / "backtest_trades.csv"

        market = _make_updown_market()
        signal = _make_signal(combined=0.35)
        trade = engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)
        engine.sim_positions[trade["trade_id"]]["_end_ts"] = int(time.time()) - 60
        engine.resolve_updown_trades(btc_price_now=86000.0)

        lines = csv_path.read_text().strip().split("\n")
        assert len(lines) == 2  # header + 1 trade


# ── Tests: reset ─────────────────────────────────────────────────────────────

class TestReset:

    def test_reset_restores_capital(self, tmp_data_dir):
        engine = tmp_data_dir
        engine.sim_balance = 45.0
        engine.sim_trades = [{"trade_id": "x"}]
        engine.sim_positions = {"y": {"trade_id": "y"}}

        engine.reset()
        assert engine.sim_balance == pytest.approx(100.0, abs=0.001)
        assert engine.sim_trades == []
        assert engine.sim_positions == {}

    def test_reset_clears_csv(self, tmp_data_dir, tmp_path):
        import time
        engine = tmp_data_dir
        market = _make_updown_market()
        signal = _make_signal(combined=0.35)
        trade = engine.record_updown(market=market, opp=None, signal=signal, btc_start_price=85000.0)
        engine.sim_positions[trade["trade_id"]]["_end_ts"] = int(time.time()) - 60
        engine.resolve_updown_trades(btc_price_now=86000.0)

        engine.reset()
        csv_path = tmp_path / "backtest_trades.csv"
        lines = csv_path.read_text().strip().split("\n")
        assert len(lines) == 1  # solo header
