"""v9.6.2 — SL inmediato + profit offset chico + stake base $3.

Problemas corregidos:
  - SL esperaba 3 min que precio RECUPERARA a entry/2 → nunca disparaba si precio
    seguía cayendo → pérdida total en FORCED_EXIT.
  - offset=0.45 = hold-to-resolution → target casi nunca alcanzado antes del cierre.
  - stake_base=7.5 para señales débiles → riesgo excesivo.

Fix:
  - SL dispara en el siguiente scan (~15s) sin esperar recovery.
  - offset=0.15 → tomar ganancia rápido.
  - stake_base=3.0.
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_trading import TradingParams, should_exit_position


def _pos(entry=0.505, target=0.655, status="OPEN"):
    return {"entry_price": entry, "target_price": target, "status": status}


def _params(**kw):
    p = TradingParams(
        sl_enabled=True,
        sl_trigger_drop=0.45,
        sl_wait_min=0.0,
        sl_min_recover_factor=0.50,
        panic_trigger_drop=0.80,
        panic_min_recover_factor=0.33,
        exit_deadline_min=2.0,
        probable_profit_offset=0.15,
    )
    for k, v in kw.items():
        setattr(p, k, v)
    return p


# ── TARGET_HIT ────────────────────────────────────────────────────────────────

def test_target_hit():
    pos = _pos(entry=0.505, target=0.655)
    result = should_exit_position(pos, 0.660, minutes_to_close=5.0, params=_params())
    assert result == "TARGET_HIT"


# ── FORCED_EXIT ───────────────────────────────────────────────────────────────

def test_forced_exit_at_deadline():
    pos = _pos()
    result = should_exit_position(pos, 0.50, minutes_to_close=1.5, params=_params())
    assert result == "FORCED_EXIT"


# ── STOP_LOSS inmediato ───────────────────────────────────────────────────────

def test_sl_arms_on_first_trigger():
    """Primer ciclo con caída ≥45% → arma (None), no dispara todavía."""
    pos = _pos(entry=0.505)
    trigger_price = 0.505 * 0.54  # drop 46% → sobre sl_trigger_drop=0.45
    result = should_exit_position(pos, trigger_price, minutes_to_close=5.0,
                                  params=_params(), now_ts=1000)
    assert result is None
    assert pos.get("sl_armed_ts") == 1000


def test_sl_fires_on_second_cycle():
    """Segundo ciclo con sl_armed_ts ya seteado → STOP_LOSS sin importar precio."""
    pos = _pos(entry=0.505)
    pos["sl_armed_ts"] = 1000  # ya armado en ciclo previo
    trigger_price = 0.505 * 0.54
    result = should_exit_position(pos, trigger_price, minutes_to_close=5.0,
                                  params=_params(), now_ts=1015)
    assert result == "STOP_LOSS"


def test_sl_fires_even_when_price_crashed_further():
    """SL o PANIC dispara cuando precio cayó más del 45%. Si cae >80% → PANIC_EXIT."""
    pos = _pos(entry=0.505)
    pos["sl_armed_ts"] = 1000
    # drop=60% (entre sl_thr=45% y panic_thr=80%) → STOP_LOSS
    crashed_price = 0.505 * 0.40
    result = should_exit_position(pos, crashed_price, minutes_to_close=5.0,
                                  params=_params(), now_ts=1015)
    assert result == "STOP_LOSS"


def test_sl_does_not_fire_without_trigger():
    """Sin caída ≥45% no hay SL."""
    pos = _pos(entry=0.505)
    price_ok = 0.505 * 0.60  # caída 40% — bajo el threshold
    result = should_exit_position(pos, price_ok, minutes_to_close=5.0,
                                  params=_params())
    assert result is None


def test_sl_does_not_fire_when_disabled():
    pos = _pos(entry=0.505)
    trigger_price = 0.505 * 0.50  # caída 50% → debería armar pero SL off
    result = should_exit_position(pos, trigger_price, minutes_to_close=5.0,
                                  params=_params(sl_enabled=False))
    assert result is None


# ── PANIC_EXIT ────────────────────────────────────────────────────────────────

def test_panic_exit_at_80_drop():
    pos = _pos(entry=0.505)
    panic_price = 0.505 * 0.15   # caída 85% → panic
    result = should_exit_position(pos, panic_price, minutes_to_close=5.0,
                                  params=_params())
    assert result == "PANIC_EXIT"


# ── Stake base $3 ─────────────────────────────────────────────────────────────

def test_config_stake_base_is_3():
    from config import BotParams
    assert BotParams().trading_stake_usdc == 3.0


def test_config_profit_offset_is_015():
    from config import BotParams
    assert BotParams().trading_probable_profit_offset == 0.15


# ── Recuperación ~55% con sl_trigger=0.45 ────────────────────────────────────

def test_sl_recovery_math():
    """Con entry=0.505, trigger_drop=0.45, precio trigger = 0.278.
    Shares = stake / entry = 3.0 / 0.505 = 5.94.
    Venta a 0.278 → 5.94 * 0.278 = $1.65 = 55% de $3. ≥50% ✓"""
    stake  = 3.0
    entry  = 0.505
    drop   = 0.45
    trigger_price = entry * (1 - drop)
    shares = stake / entry
    recovered = shares * trigger_price
    recovery_pct = recovered / stake
    assert recovery_pct >= 0.50, f"recovery {recovery_pct:.1%} < 50%"
