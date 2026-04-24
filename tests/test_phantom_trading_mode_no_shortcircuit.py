"""v9.5.2 — Phantom legacy registration funciona aun con trading_mode_enabled.

Bug: scan_updown hacía `return` después de trading_runner.run_cycle, matando
el bloque de registro phantom (vps_experiment + phantom_learner). Resultado:
sección phantom de la UI sin operaciones.

Fix: no hacer return en trading mode; dejar caer al flujo legacy para registrar
phantom; solo bloquear el _execute_trade(opp) real legacy.
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _read_scan_updown_source() -> str:
    import bot
    return inspect.getsource(bot._scan_updown)


def test_trading_mode_branch_does_not_return_early():
    """En la rama trading_mode no debe haber `return` antes del bloque phantom."""
    src = _read_scan_updown_source()
    # Localizar el bloque "TRADING MODE"
    idx = src.find("TRADING MODE")
    assert idx != -1, "no encontré la sección TRADING MODE"
    # Localizar el siguiente comentario que ya está fuera del bloque
    next_section = src.find("# No operar dos veces", idx)
    assert next_section != -1, "no encontré marcador de fin de bloque"
    block = src[idx:next_section]
    # NO debe haber un `return` (sin condición) inmediatamente — fix v9.5.2
    # Permitimos `return` dentro del except si lo hubiera (no lo hay).
    lines = [ln.strip() for ln in block.splitlines()]
    standalone_return = [ln for ln in lines if ln == "return"]
    assert standalone_return == [], (
        "trading_mode branch tiene `return` plano: "
        "phantom legacy nunca se registra. lines=" + str(standalone_return)
    )


def test_trading_mode_active_flag_present():
    """Debe existir variable _trading_mode_active accesible en el flujo posterior."""
    src = _read_scan_updown_source()
    assert "_trading_mode_active" in src, "falta la flag _trading_mode_active"
    # Debe usarse para bloquear el path real legacy
    assert "if _trading_mode_active" in src, (
        "_trading_mode_active no se usa para bloquear el real legacy"
    )


def test_phantom_block_runs_after_trading_mode():
    """El bloque phantom debe estar más abajo que el bloque trading_mode."""
    src = _read_scan_updown_source()
    tm_idx = src.find("TRADING MODE")
    ph_idx = src.find("Apuesta fantasma")
    assert tm_idx > 0 and ph_idx > 0
    assert ph_idx > tm_idx, "phantom block debe ejecutarse después de trading_mode"


def test_real_legacy_blocked_when_trading_mode_active():
    """Cuando _trading_mode_active=True, el path real legacy hace return antes
    de _execute_trade — para no doblar entradas (trading_runner ya las hizo)."""
    src = _read_scan_updown_source()
    # Buscar el guard
    guard_idx = src.find("if _trading_mode_active:")
    exec_idx  = src.find("_execute_trade, opp")
    assert guard_idx > 0
    assert exec_idx > 0
    assert guard_idx < exec_idx, (
        "el guard _trading_mode_active debe estar antes del _execute_trade"
    )
