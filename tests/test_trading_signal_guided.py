"""v9.5.8 — Trading mode signal-guided hold-to-resolution.

Cambios clave:
  - buy_probable=True, probable_min_price=0.45 → acepta mercados 50/50
  - probable_profit_offset=0.45 → target ≈0.95, hold-to-resolution
  - signal_direction (inyectado desde opp) guía qué lado comprar
  - Con predictor 77.4% WR: EV ≈ +$1.58/trade vs $5 stake
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_trading import TradingParams, evaluate_entry_verbose


def _make_market(up=0.495, down=0.505, minutes=10.0, signal_dir=None, signal_conf=0):
    m = {
        "slug": "btc-updown-15m-test",
        "up_price":  up,
        "down_price": down,
        "up_token":   "tok_up",
        "down_token": "tok_down",
        "minutes_to_close": minutes,
    }
    if signal_dir:
        m["signal_direction"]  = signal_dir
        m["signal_confidence"] = signal_conf
    return m


def _params(**kwargs):
    p = TradingParams(
        buy_probable          = True,
        probable_min_price    = 0.45,
        probable_max_price    = 0.85,
        probable_profit_offset= 0.45,
        min_entry_minutes_left= 1.0,
        max_entries_per_market= 8,
        max_open_per_side     = 1,
        one_open_at_a_time    = False,
    )
    for k, v in kwargs.items():
        setattr(p, k, v)
    return p


def test_signal_up_buys_up_side():
    """Signal dice UP → bot compra UP (no el más caro DOWN)."""
    m = _make_market(up=0.495, down=0.505, signal_dir="UP", signal_conf=60)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None, f"esperaba entry, got: {reason}"
    assert sig.side == "UP", f"esperaba UP, got {sig.side}"


def test_signal_down_buys_down_side():
    """Signal dice DOWN → bot compra DOWN."""
    m = _make_market(up=0.495, down=0.505, signal_dir="DOWN", signal_conf=60)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None, f"esperaba entry, got: {reason}"
    assert sig.side == "DOWN"


def test_no_signal_buys_highest_price():
    """Sin signal_direction → modo probable estándar → lado con precio más alto."""
    m = _make_market(up=0.475, down=0.505)  # no signal_dir; DOWN > UP
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None, f"esperaba entry, got: {reason}"
    assert sig.side == "DOWN"  # 0.505 > 0.475


def test_50_50_market_gets_entry_with_signal():
    """Mercados 50/50 (0.495/0.505) ahora generan entradas con señal."""
    m = _make_market(up=0.495, down=0.505, signal_dir="DOWN", signal_conf=45)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None, f"50/50 debería generar entry ahora: {reason}"
    assert sig.entry_price == round(0.505, 3)


def test_signal_blocked_by_old_ceiling_30():
    """Modo cheapest con max_entry=0.30 sigue bloqueando 50/50."""
    m = _make_market(up=0.495, down=0.505, signal_dir="DOWN", signal_conf=60)
    p = TradingParams(
        buy_probable=False,
        min_entry_price=0.10,
        max_entry_price=0.30,
        min_entry_minutes_left=1.0,
        max_entries_per_market=8,
        max_open_per_side=1,
        one_open_at_a_time=False,
    )
    sig, reason = evaluate_entry_verbose(m, [], p)
    assert sig is None
    assert "0.30" in reason or "ceiling" in reason


def test_target_is_hold_to_resolution():
    """Con offset=0.45, target ≈ entry+0.45 (efectivamente hold-to-resolution)."""
    m = _make_market(up=0.495, down=0.505, signal_dir="DOWN", signal_conf=55)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None
    assert sig.target_price == round(sig.entry_price + 0.45, 3)


def test_no_entry_when_signal_side_price_out_of_range():
    """Signal dice UP pero UP price cae fuera de [floor,ceiling] → no entry."""
    m = _make_market(up=0.40, down=0.60, signal_dir="UP", signal_conf=60)
    # floor=0.45, UP=0.40 < floor → rechazado
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is None
    assert "UP" in reason


def test_no_entry_when_signal_side_already_open():
    """No entrar si el lado de la señal ya tiene posición abierta."""
    m = _make_market(up=0.495, down=0.505, signal_dir="DOWN", signal_conf=60)
    open_pos = [{"side": "DOWN", "status": "OPEN", "entry_price": 0.50}]
    p = _params(one_open_at_a_time=True)
    sig, reason = evaluate_entry_verbose(m, open_pos, p)
    assert sig is None


def test_config_defaults_updated():
    """BotParams expone los nuevos defaults de v9.5.8."""
    from config import BotParams
    bp = BotParams()
    assert bp.trading_buy_probable          is True
    assert bp.trading_probable_min_price    == 0.45
    assert bp.trading_probable_profit_offset == 0.45


def test_mode_tag_includes_signal_info():
    """reason_str incluye 'signal(' cuando se usa la dirección del predictor."""
    m = _make_market(up=0.495, down=0.505, signal_dir="DOWN", signal_conf=62)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None
    assert "signal" in sig.reason.lower() or "DOWN" in sig.reason
