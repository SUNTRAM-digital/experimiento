"""Point 12 — stop-loss escalonado (enfoque A).
Niveles:
  - SL nivel 1: caída >=50% vs entry, espera N min, vende si bid >= entry/2.
  - SL nivel 2 (panic): caída >=80%, vende al primer bid >= entry/3.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_trading import TradingParams, should_exit_position


def _pos(entry=0.50, sl_armed_ts=None, status="OPEN"):
    p = {"status": status, "entry_price": entry, "target_price": entry + 0.30, "stake_usdc": 5.0, "side": "UP"}
    if sl_armed_ts is not None:
        p["sl_armed_ts"] = sl_armed_ts
    return p


def _params():
    return TradingParams(
        entry_threshold=0.55, min_entry_price=0.05, max_entry_price=0.30,
        profit_offset=0.30, exit_deadline_min=1.0, min_entry_minutes_left=1.0,
        sl_enabled=True, sl_trigger_drop=0.50, sl_wait_min=3.0,
        sl_min_recover_factor=0.50, panic_trigger_drop=0.80,
        panic_min_recover_factor=0.33,
    )


def test_no_exit_when_price_stable():
    p = _pos(entry=0.50)
    r = should_exit_position(p, current_token_price=0.45, minutes_to_close=10, params=_params())
    assert r is None


def test_target_hit_takes_precedence():
    p = _pos(entry=0.50)
    p["target_price"] = 0.80
    r = should_exit_position(p, current_token_price=0.85, minutes_to_close=10, params=_params())
    assert r == "TARGET_HIT"


def test_sl_arms_on_first_trigger_no_exit():
    """Primera lectura bajo sl_trigger_drop arma timer pero no sale aún."""
    p = _pos(entry=0.50)  # precio <= 0.25 arma
    r = should_exit_position(p, current_token_price=0.20, minutes_to_close=10, params=_params(), now_ts=1000)
    assert r is None
    assert p.get("sl_armed_ts") == 1000


def test_sl_waits_before_exit():
    """Aún no pasaron sl_wait_min minutos → no vende."""
    p = _pos(entry=0.50, sl_armed_ts=1000)
    # 1 min después, precio recuperó a 0.26 (>= entry/2=0.25)
    r = should_exit_position(p, current_token_price=0.26, minutes_to_close=10, params=_params(), now_ts=1060)
    assert r is None


def test_sl_exits_when_wait_met_and_recovered():
    """Después de sl_wait_min y con bid >= entry/2 → STOP_LOSS."""
    p = _pos(entry=0.50, sl_armed_ts=1000)
    # 4 min después, precio recuperó a 0.26
    r = should_exit_position(p, current_token_price=0.26, minutes_to_close=10, params=_params(), now_ts=1000 + 4*60)
    assert r == "STOP_LOSS"


def test_sl_no_exit_if_still_below_recover():
    """Tiempo cumplido pero precio aún bajo entry/2 → no vende (espera mejor bid)."""
    p = _pos(entry=0.50, sl_armed_ts=1000)
    r = should_exit_position(p, current_token_price=0.15, minutes_to_close=10, params=_params(), now_ts=1000 + 4*60)
    assert r is None


def test_panic_exits_immediately_on_big_drop():
    """Caída catastrófica (≥80%) → PANIC_EXIT salvage (vende al bid actual)."""
    p = _pos(entry=0.50)
    # drop 0.50→0.05 = 90%
    r = should_exit_position(p, current_token_price=0.05, minutes_to_close=10, params=_params(), now_ts=1000)
    assert r == "PANIC_EXIT"


def test_panic_no_exit_if_market_dead():
    """Si bid = 0 no hay comprador → no se fuerza salida."""
    p = _pos(entry=0.50)
    r = should_exit_position(p, current_token_price=0.0, minutes_to_close=10, params=_params(), now_ts=1000)
    assert r is None


def test_sl_disabled_no_effect():
    params = _params()
    params.sl_enabled = False
    p = _pos(entry=0.50)
    r = should_exit_position(p, current_token_price=0.10, minutes_to_close=10, params=params, now_ts=1000)
    assert r is None
    assert "sl_armed_ts" not in p


def test_forced_exit_takes_precedence_over_sl():
    p = _pos(entry=0.50)
    r = should_exit_position(p, current_token_price=0.10, minutes_to_close=0.5, params=_params())
    assert r == "FORCED_EXIT"


def test_config_has_sl_params():
    from config import BotParams
    bp = BotParams()
    assert hasattr(bp, "trading_sl_enabled")
    assert hasattr(bp, "trading_sl_trigger_drop")
    assert hasattr(bp, "trading_sl_wait_min")
    assert hasattr(bp, "trading_sl_min_recover_factor")
    assert hasattr(bp, "trading_panic_trigger_drop")
    assert hasattr(bp, "trading_panic_min_recover_factor")
    # to_dict incluye
    d = bp.to_dict()
    assert "trading_sl_enabled" in d
    assert "trading_panic_trigger_drop" in d


def test_params_from_config_wires_sl():
    from config import BotParams
    from trading_runner import params_from_config
    bp = BotParams()
    bp.trading_sl_enabled = True
    bp.trading_sl_wait_min = 5.0
    p = params_from_config(bp)
    assert p.sl_enabled is True
    assert p.sl_wait_min == 5.0


def test_patch_position_persists_sl_armed_ts(tmp_path, monkeypatch):
    import json
    import trading_positions as tp
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"phantom": {}, "real": {}, "meta": {"phantom_balance": 1000.0}}))
    monkeypatch.setattr(tp, "_DATA_PATH", str(f), raising=False)

    pos = tp.open_position(
        slug="mkt-sl", interval=5, end_ts=9999999999,
        side="UP", token_id="tkn",
        entry_price=0.50, target_price=0.80, stake_usdc=5.0,
        is_real=False,
    )
    updated = tp.patch_position("mkt-sl", pos["id"], {"sl_armed_ts": 1234567890}, is_real=False)
    assert updated is not None
    assert updated["sl_armed_ts"] == 1234567890
    # verifica persistencia
    opens = tp.get_open_positions("mkt-sl", is_real=False)
    assert opens[0]["sl_armed_ts"] == 1234567890
