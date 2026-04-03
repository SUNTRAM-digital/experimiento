"""
Logica de trading: calcula EV y sizing con Kelly Criterion.
"""
from datetime import date
from typing import Optional
from scipy.stats import norm
from config import bot_params


def calc_bucket_probability(
    forecast_high: float,
    std_dev: float,
    temp_low: float,
    temp_high: float,
    bucket_type: str,
) -> float:
    """
    Calcula la probabilidad de que la temperatura maxima caiga en el bucket.
    Usa distribucion normal centrada en el forecast con desviacion std_dev.
    """
    if bucket_type == "range":
        # P(temp_low <= X <= temp_high)
        p = norm.cdf(temp_high, loc=forecast_high, scale=std_dev) - \
            norm.cdf(temp_low, loc=forecast_high, scale=std_dev)
    elif bucket_type == "below":
        # P(X <= temp_high)
        p = norm.cdf(temp_high, loc=forecast_high, scale=std_dev)
    elif bucket_type == "above":
        # P(X >= temp_low)
        p = 1 - norm.cdf(temp_low, loc=forecast_high, scale=std_dev)
    else:
        return 0.0

    return max(0.0, min(1.0, float(p)))


def calc_ev(our_prob: float, market_price: float, spread_pct: float = 0.0) -> float:
    """
    Calcula el Expected Value de comprar a market_price, descontando el costo del spread.
    El spread representa el costo real de entrada: pagamos ask en vez del midpoint.
    EV como porcentaje del capital invertido.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    # Costo efectivo = precio pagado + mitad del spread (slippage estimado)
    effective_cost = market_price * (1 + spread_pct * 0.5)
    effective_cost = min(effective_cost, 0.99)
    ev = (our_prob * 1.0) - effective_cost
    return ev / effective_cost


def calc_kelly_size(
    our_prob: float,
    market_price: float,
    balance_usdc: float,
) -> float:
    """
    Calcula el tamano de posicion usando Kelly Criterion fraccionado.

    Kelly formula para mercados binarios:
    f = (b*p - q) / b
    donde b = odds (1/price - 1), p = nuestra prob, q = 1-p
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    b = (1.0 / market_price) - 1  # odds netos
    p = our_prob
    q = 1.0 - p

    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0

    # Aplicar fraccion de Kelly y limites configurados
    kelly_fraction = bot_params.kelly_fraction
    raw_size = kelly_full * kelly_fraction * balance_usdc

    # Clamp entre min y max configurados
    size = max(bot_params.min_position_usdc, min(bot_params.max_position_usdc, raw_size))
    return round(size, 2)


def evaluate_market(market: dict, forecast: dict, balance_usdc: float) -> Optional[dict]:
    """
    Evalua si un mercado ofrece edge suficiente para tradear.

    Returns dict con la oportunidad o None si no hay edge.
    """
    high_f = forecast["high_f"]
    std_dev = forecast["std_dev"]

    our_prob = calc_bucket_probability(
        forecast_high=high_f,
        std_dev=std_dev,
        temp_low=market["temp_low"],
        temp_high=market["temp_high"],
        bucket_type=market["bucket_type"],
    )

    market_price = market["yes_price"]
    spread_pct = market.get("spread_pct", 0.0)
    ev = calc_ev(our_prob, market_price, spread_pct)

    # Determinar si es mejor comprar YES o NO
    no_prob = 1 - our_prob
    no_price = 1 - market_price
    ev_no = calc_ev(no_prob, no_price, spread_pct) if no_price > 0 else 0.0

    if ev >= ev_no and ev >= bot_params.min_ev_threshold:
        side = "YES"
        size_usdc = calc_kelly_size(our_prob, market_price, balance_usdc)
        final_ev = ev
        token_id = market["yes_token_id"]
        entry_price = market_price
        our_edge_prob = our_prob
    elif ev_no > ev and ev_no >= bot_params.min_ev_threshold:
        side = "NO"
        size_usdc = calc_kelly_size(no_prob, no_price, balance_usdc)
        final_ev = ev_no
        token_id = market["no_token_id"]
        entry_price = no_price
        our_edge_prob = no_prob
    else:
        return None

    if size_usdc < bot_params.min_position_usdc:
        return None

    if token_id is None:
        return None

    return {
        # Identificacion
        "market_title": market["title"],
        "condition_id": market["condition_id"],
        "city": market["city"],
        "station": market["station"],
        # Trade
        "side": side,
        "token_id": token_id,
        "entry_price": entry_price,
        # Probabilidades y edge
        "our_prob": our_edge_prob,
        "market_prob": market_price if side == "YES" else no_price,
        "ev_pct": round(final_ev * 100, 1),
        # Sizing
        "size_usdc": size_usdc,
        "shares": round(size_usdc / entry_price, 2),
        # Forecast meteorologico
        "forecast_high": high_f,
        "forecast_std": std_dev,
        "temp_low": market["temp_low"],
        "temp_high": market["temp_high"],
        # Calidad de mercado (para Claude y UI)
        "hours_to_close": market["hours_to_close"],
        "liquidity": market["liquidity"],
        "volume_24h": market.get("volume_24h", 0),
        "volume_7d": market.get("volume_7d", 0),
        "volume_total": market.get("volume_total", 0),
        "spread_pct": spread_pct,
        "best_bid": market.get("best_bid", 0),
        "best_ask": market.get("best_ask", 0),
        "last_trade_price": market.get("last_trade_price", entry_price),
        "competitive_score": market.get("competitive_score", 0),
        "accepting_orders": market.get("accepting_orders", True),
        "min_order_size": market.get("min_order_size", 5),
    }
