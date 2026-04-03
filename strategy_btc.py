"""
Estrategia para mercados de precio de Bitcoin.

Modelo: el log-retorno de BTC en una ventana de T minutos sigue una distribución normal:
    ln(P_T / P_0) ~ N(μ·T, σ²·T)

donde:
    σ  = volatilidad log-normal por minuto (calculada de datos históricos de Binance)
    μ  ≈ 0  (el drift es despreciable en ventanas cortas de 15 min)

Probabilidad de que BTC esté por encima del umbral X al cierre:
    P(P_T > X) = 1 - Φ( ln(X/P_0) / (σ·√T) )

Si el mercado cotiza esa probabilidad a un precio diferente, hay edge.
"""
import math
from typing import Optional
from scipy.stats import norm
from config import bot_params


def calc_btc_probability(
    current_price: float,
    threshold: float,
    minutes_to_close: float,
    direction: str,          # "above" o "below"
    vol_per_minute: float,   # desv. estándar del log-retorno por minuto
) -> float:
    """
    Calcula la probabilidad de que BTC esté por encima/debajo del umbral al cierre.

    Args:
        current_price:    precio actual de BTC en USD
        threshold:        precio umbral del mercado
        minutes_to_close: minutos hasta resolución
        direction:        "above" (YES = BTC > umbral) o "below" (YES = BTC < umbral)
        vol_per_minute:   volatilidad log-normal por minuto

    Returns: probabilidad [0, 1]
    """
    if current_price <= 0 or threshold <= 0 or minutes_to_close <= 0:
        return 0.5  # sin datos suficientes, asumir 50/50

    # Desplazamiento logarítmico normalizado
    log_ratio = math.log(threshold / current_price)
    sigma_t   = vol_per_minute * math.sqrt(minutes_to_close)

    if sigma_t <= 0:
        # Si no hay volatilidad, resultado determinista
        if direction == "above":
            return 1.0 if current_price > threshold else 0.0
        else:
            return 1.0 if current_price < threshold else 0.0

    # z-score: cuántas sigmas está el umbral del precio actual
    z = log_ratio / sigma_t

    # P(P_T > umbral) = 1 - Φ(z)
    prob_above = float(1 - norm.cdf(z))

    if direction == "above":
        return max(0.01, min(0.99, prob_above))
    else:
        return max(0.01, min(0.99, 1 - prob_above))


def calc_ev(our_prob: float, market_price: float, spread_pct: float = 0.0) -> float:
    """EV como fracción del capital invertido, descontando spread."""
    if market_price <= 0 or market_price >= 1:
        return 0.0
    effective_cost = market_price * (1 + spread_pct * 0.5)
    effective_cost = min(effective_cost, 0.99)
    ev = (our_prob * 1.0) - effective_cost
    return ev / effective_cost


def calc_kelly_size(our_prob: float, market_price: float, balance_usdc: float) -> float:
    """Kelly Criterion fraccionado para el tamaño de posición."""
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1.0 / market_price) - 1
    p, q = our_prob, 1.0 - our_prob
    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0
    raw_size = kelly_full * bot_params.kelly_fraction * balance_usdc
    size = max(bot_params.min_position_usdc, min(bot_params.btc_max_position_usdc, raw_size))
    return round(size, 2)


def evaluate_btc_market(
    market: dict,
    btc_price: float,
    vol_per_minute: float,
    balance_usdc: float,
) -> Optional[dict]:
    """
    Evalúa si un mercado de BTC ofrece edge suficiente para operar.

    Returns dict con la oportunidad o None si no hay edge.
    """
    threshold      = market["threshold"]
    direction      = market["side"]        # "above" / "below"
    hours_to_close = market["hours_to_close"]
    minutes_to_close = hours_to_close * 60

    # No operar si el mercado cierra en menos de 2 minutos (sin tiempo para ejecución)
    if minutes_to_close < 2:
        return None

    # No operar si el mercado cierra en más del límite configurado
    if hours_to_close > bot_params.btc_max_hours_to_resolution:
        return None

    spread_pct  = market.get("spread_pct", 0.0)
    yes_price   = market["yes_price"]

    # Calcular nuestra probabilidad para el lado YES (que es el que cotiza el mercado)
    our_prob_yes = calc_btc_probability(
        current_price    = btc_price,
        threshold        = threshold,
        minutes_to_close = minutes_to_close,
        direction        = direction,
        vol_per_minute   = vol_per_minute,
    )

    ev_yes = calc_ev(our_prob_yes, yes_price, spread_pct)

    # Evaluar también el lado NO
    our_prob_no = 1 - our_prob_yes
    no_price    = 1 - yes_price
    ev_no = calc_ev(our_prob_no, no_price, spread_pct) if no_price > 0.01 else 0.0

    # Elegir el mejor lado
    if ev_yes >= ev_no and ev_yes >= bot_params.min_ev_threshold:
        side          = "YES"
        size_usdc     = calc_kelly_size(our_prob_yes, yes_price, balance_usdc)
        final_ev      = ev_yes
        token_id      = market["yes_token_id"]
        entry_price   = yes_price
        our_edge_prob = our_prob_yes
    elif ev_no > ev_yes and ev_no >= bot_params.min_ev_threshold:
        side          = "NO"
        size_usdc     = calc_kelly_size(our_prob_no, no_price, balance_usdc)
        final_ev      = ev_no
        token_id      = market["no_token_id"]
        entry_price   = no_price
        our_edge_prob = our_prob_no
    else:
        return None

    if size_usdc < bot_params.min_position_usdc:
        return None

    if token_id is None:
        return None

    # Distancia del precio actual al umbral (útil para UI y análisis)
    pct_from_threshold = (btc_price - threshold) / threshold * 100

    return {
        # Identificación
        "market_title":      market["title"],
        "condition_id":      market["condition_id"],
        "asset":             "BTC",
        "threshold":         threshold,
        "direction":         direction,
        "btc_price_at_eval": round(btc_price, 2),
        "pct_from_threshold": round(pct_from_threshold, 2),
        # Trade
        "side":        side,
        "token_id":    token_id,
        "entry_price": entry_price,
        # Probabilidades y edge
        "our_prob":    our_edge_prob,
        "market_prob": yes_price if side == "YES" else no_price,
        "ev_pct":      round(final_ev * 100, 1),
        # Sizing
        "size_usdc": size_usdc,
        "shares":    round(size_usdc / entry_price, 2),
        # Volatilidad usada
        "vol_per_minute": round(vol_per_minute * 100, 4),   # en %
        "minutes_to_close": round(minutes_to_close, 1),
        # Calidad de mercado
        "hours_to_close":    market["hours_to_close"],
        "liquidity":         market["liquidity"],
        "volume_24h":        market.get("volume_24h", 0),
        "volume_7d":         market.get("volume_7d", 0),
        "volume_total":      market.get("volume_total", 0),
        "spread_pct":        spread_pct,
        "best_bid":          market.get("best_bid", 0),
        "best_ask":          market.get("best_ask", 0),
        "last_trade_price":  market.get("last_trade_price", entry_price),
        "competitive_score": market.get("competitive_score", 0),
        "min_order_size":    market.get("min_order_size", 5),
    }
