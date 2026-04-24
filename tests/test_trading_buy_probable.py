"""Point 14 — bot compra el lado más PROBABLE (favorito), no el más barato.
Modo cheapest (legacy): compra el precio más bajo en [min_entry, max_entry].
Modo probable (nuevo default): compra el precio más alto en [probable_min, probable_max].
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_trading import TradingParams, evaluate_entry_verbose


def _mkt(up, down, mins=10):
    return {
        "slug": "x", "end_ts": 9999999999, "minutes_to_close": mins,
        "up_price": up, "down_price": down,
        "yes_token_id": "u", "no_token_id": "d",
        "up_token": "u", "down_token": "d",
    }


def _probable_params():
    return TradingParams(
        min_entry_price=0.10, max_entry_price=0.30, profit_offset=0.30,
        entry_threshold=0.80, min_entry_minutes_left=1.0, one_open_at_a_time=False,
        buy_probable=True, probable_min_price=0.55, probable_max_price=0.85,
        probable_profit_offset=0.08,
    )


def _cheapest_params():
    return TradingParams(
        min_entry_price=0.10, max_entry_price=0.30, profit_offset=0.30,
        entry_threshold=0.80, min_entry_minutes_left=1.0, one_open_at_a_time=False,
        buy_probable=False,
    )


def test_probable_picks_highest_side():
    """UP=0.70, DOWN=0.30 → modo probable compra UP (favorito)."""
    sig, reason = evaluate_entry_verbose(_mkt(0.70, 0.30), [], _probable_params())
    assert sig is not None, f"should enter; reason={reason}"
    assert sig.side == "UP"
    assert abs(sig.entry_price - 0.70) < 0.001


def test_probable_uses_small_offset():
    sig, _ = evaluate_entry_verbose(_mkt(0.70, 0.30), [], _probable_params())
    # target = 0.70 + 0.08 = 0.78
    assert abs(sig.target_price - 0.78) < 0.001


def test_probable_rejects_below_floor():
    """Ambos lados 50/50 — ninguno supera probable_min_price=0.55."""
    sig, reason = evaluate_entry_verbose(_mkt(0.50, 0.50), [], _probable_params())
    assert sig is None
    assert "probable" in reason or "floor" in reason


def test_probable_rejects_above_ceiling():
    """UP=0.95 es casi filled — probable_max=0.85 lo bloquea."""
    sig, reason = evaluate_entry_verbose(_mkt(0.95, 0.05), [], _probable_params())
    assert sig is None


def test_probable_picks_down_when_favorite():
    """DOWN=0.75, UP=0.25 → modo probable compra DOWN."""
    sig, _ = evaluate_entry_verbose(_mkt(0.25, 0.75), [], _probable_params())
    assert sig is not None
    assert sig.side == "DOWN"
    assert abs(sig.entry_price - 0.75) < 0.001


def test_cheapest_mode_still_works():
    """Modo cheapest legacy — UP=0.15, DOWN=0.70 → compra UP (reversal gate no gatilla con opp<0.80)."""
    sig, _ = evaluate_entry_verbose(_mkt(0.15, 0.70), [], _cheapest_params())
    assert sig is not None
    assert sig.side == "UP"
    assert abs(sig.entry_price - 0.15) < 0.001
    # target = 0.15 + 0.30 (profit_offset default)
    assert abs(sig.target_price - 0.45) < 0.001


def test_reason_contains_mode_tag():
    sig, reason = evaluate_entry_verbose(_mkt(0.70, 0.30), [], _probable_params())
    assert sig is not None
    assert "probable" in sig.reason


def test_config_has_probable_params():
    from config import BotParams
    bp = BotParams()
    assert hasattr(bp, "trading_buy_probable")
    assert hasattr(bp, "trading_probable_min_price")
    assert hasattr(bp, "trading_probable_max_price")
    assert hasattr(bp, "trading_probable_profit_offset")
    d = bp.to_dict()
    assert "trading_buy_probable" in d


def test_params_from_config_wires_probable():
    from config import BotParams
    from trading_runner import params_from_config
    bp = BotParams()
    bp.trading_buy_probable = True
    bp.trading_probable_min_price = 0.60
    p = params_from_config(bp)
    assert p.buy_probable is True
    assert p.probable_min_price == 0.60
