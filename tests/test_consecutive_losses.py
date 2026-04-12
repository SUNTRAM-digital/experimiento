"""
Tests: _sync_consecutive_losses_from_history en bot.py.
Verifica que la función lee updown_recent_trades (más reciente en índice 0)
y calcula la racha de pérdidas correctamente.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import importlib
import types


def _make_trades(*results_newest_first, interval=15):
    """Genera lista de trades con el resultado más reciente en índice 0."""
    return [
        {"interval": interval, "result": r}
        for r in results_newest_first
    ]


def _run_sync(trades_15m=None, trades_5m=None):
    """
    Importa bot con state mockeado, inyecta trades y corre
    _sync_consecutive_losses_from_history; retorna (streak_5m, streak_15m).
    """
    import bot as _bot_module

    # Guardar estado real
    original_trades = _bot_module.state.updown_recent_trades
    original_5m  = _bot_module.state.updown_5m_consecutive_losses
    original_15m = _bot_module.state.updown_15m_consecutive_losses

    combined = []
    if trades_15m:
        combined.extend(trades_15m)
    if trades_5m:
        combined.extend(trades_5m)

    _bot_module.state.updown_recent_trades = combined
    _bot_module._sync_consecutive_losses_from_history()

    streak_5m  = _bot_module.state.updown_5m_consecutive_losses
    streak_15m = _bot_module.state.updown_15m_consecutive_losses

    # Restaurar estado real
    _bot_module.state.updown_recent_trades = original_trades
    _bot_module.state.updown_5m_consecutive_losses  = original_5m
    _bot_module.state.updown_15m_consecutive_losses = original_15m

    return streak_5m, streak_15m


# ── Casos de 15m ────────────────────────────────────────────────────────────

def test_win_resets_streak_to_zero():
    """Si el trade más reciente es WIN, la racha debe ser 0."""
    trades = _make_trades("WIN", "LOSS", "LOSS")
    _, streak = _run_sync(trades_15m=trades)
    assert streak == 0, f"Esperado 0, obtenido {streak}"


def test_single_loss():
    """Un LOSS reciente → racha 1."""
    trades = _make_trades("LOSS")
    _, streak = _run_sync(trades_15m=trades)
    assert streak == 1


def test_consecutive_losses():
    """Tres LOSS consecutivos más recientes → racha 3."""
    trades = _make_trades("LOSS", "LOSS", "LOSS", "WIN")
    _, streak = _run_sync(trades_15m=trades)
    assert streak == 3


def test_win_after_losses():
    """WIN más reciente aunque haya LOSSes anteriores → racha 0."""
    trades = _make_trades("WIN", "LOSS", "LOSS", "LOSS")
    _, streak = _run_sync(trades_15m=trades)
    assert streak == 0


def test_all_wins():
    """Todos WIN → racha 0."""
    trades = _make_trades("WIN", "WIN", "WIN")
    _, streak = _run_sync(trades_15m=trades)
    assert streak == 0


def test_empty_history():
    """Sin trades → racha 0."""
    _, streak = _run_sync(trades_15m=[])
    assert streak == 0


def test_loss_win_loss_pattern():
    """LOSS, WIN, LOSS (más reciente primero) → racha 1 (solo el LOSS reciente)."""
    trades = _make_trades("LOSS", "WIN", "LOSS")
    _, streak = _run_sync(trades_15m=trades)
    assert streak == 1


# ── Casos de 5m ─────────────────────────────────────────────────────────────

def test_5m_independent_from_15m():
    """Los intervalos 5m y 15m son independientes."""
    trades_5m  = _make_trades("LOSS", "LOSS", interval=5)
    trades_15m = _make_trades("WIN", "LOSS", interval=15)
    streak_5m, streak_15m = _run_sync(trades_15m=trades_15m, trades_5m=trades_5m)
    assert streak_5m  == 2, f"5m esperado 2, obtenido {streak_5m}"
    assert streak_15m == 0, f"15m esperado 0, obtenido {streak_15m}"


def test_5m_win_resets():
    """WIN reciente en 5m → racha 0 para 5m."""
    trades_5m = _make_trades("WIN", "LOSS", "LOSS", interval=5)
    streak_5m, _ = _run_sync(trades_5m=trades_5m)
    assert streak_5m == 0
