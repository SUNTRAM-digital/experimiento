"""Point 10 — R:R favorable: max_entry_price cap + profit_offset default bumped.
Validates that strategy rejects entries above max_entry_price."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_trading_params_has_max_entry_price():
    from strategy_trading import TradingParams
    p = TradingParams()
    assert hasattr(p, "max_entry_price")
    assert p.max_entry_price > 0
    assert p.max_entry_price < 1.0
    # R:R favorable: profit_offset/max_entry_price >= 1 (ideally >=2)
    rr = p.profit_offset / p.max_entry_price
    assert rr >= 1.0, f"R:R worst case {rr} < 1 — params no favorecen al bot"


def test_config_defaults_bumped():
    from config import BotParams
    bp = BotParams()
    # R:R worst case = profit_offset / max_entry_price debe ser ≥ 1
    rr = bp.trading_profit_offset / bp.trading_max_entry_price
    assert rr >= 1.0


def test_strategy_rejects_price_above_max():
    """evaluate_entry_verbose rechaza UP cuando price > max_entry_price (cheapest mode)."""
    from strategy_trading import TradingParams, evaluate_entry_verbose
    p = TradingParams(entry_threshold=0.80, min_entry_price=0.05, max_entry_price=0.20,
                      profit_offset=0.30, min_entry_minutes_left=1.0, one_open_at_a_time=False,
                      buy_probable=False)
    market = {
        "slug": "x", "end_ts": 9999999999, "minutes_to_close": 10.0,
        "up_price": 0.50, "down_price": 0.50,
        "yes_token_id": "a", "no_token_id": "b",
    }
    sig, reason = evaluate_entry_verbose(market, [], p)
    assert sig is None, f"debería rechazar ambos lados; signal={sig}, reason={reason}"
    assert "ceiling" in reason or "R:R" in reason, f"reason inesperada: {reason}"


def test_ceiling_reason_differs_from_threshold():
    """ceiling (max_entry_price) rechaza antes que threshold — reason debe decir 'ceiling'."""
    from strategy_trading import TradingParams, evaluate_entry_verbose
    p = TradingParams(entry_threshold=0.80, min_entry_price=0.05, max_entry_price=0.25,
                      profit_offset=0.30, min_entry_minutes_left=1.0, one_open_at_a_time=False,
                      buy_probable=False)
    market = {
        "slug": "x", "end_ts": 9999999999, "minutes_to_close": 10.0,
        "up_price": 0.50, "down_price": 0.50,
        "yes_token_id": "a", "no_token_id": "b",
    }
    sig, reason = evaluate_entry_verbose(market, [], p)
    assert sig is None
    assert "ceiling" in reason, f"debería mencionar 'ceiling' en reason: {reason}"


def test_max_price_included_in_to_dict():
    from config import BotParams
    bp = BotParams()
    d = bp.to_dict()
    assert "trading_max_entry_price" in d
