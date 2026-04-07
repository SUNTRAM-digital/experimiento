"""
Logica de trading: calcula EV, sizing con Kelly Criterion, y aplica los
5 patrones de los top wallets de Polymarket.

Patrones implementados:
  P1 - 72-Hour Rule:           priorizar mercados que resuelven en <72h
  P2 - Win Rate Decay:         penalizar mercados fuera de categoria dominante
  P3 - Disposition Coefficient: exit cuando edge agotado (en exit_manager.py)
  P4 - Contrarian Entry:       detectar extremos de sentimiento para entrar al reves
  P5 - Swing Trading:          no hold-to-settlement (en exit_manager.py)
"""
from datetime import date
from typing import Optional
from scipy.stats import norm
from config import bot_params
import math


# ── Patron 1: Regla de las 72 Horas ──────────────────────────────────────────
# Bonus de prioridad segun horas al cierre.
# Los top wallets entran a 72h antes de resolucion cuando el outcome ya es ~80% claro.
# Mismo edge + menos tiempo = mucho mayor retorno anualizado.
#
# Annualized = (1 + edge)^(365/hold_days) - 1
# A 2 dias: una posicion con 15% de edge genera 182x ciclos vs 30 dias → 435%

def calc_time_priority_bonus(hours_to_close: float) -> float:
    """
    Bonus multiplicativo sobre el EV segun proximidad a la resolucion.
    Retorna un valor entre 0.0 y 0.5 que se suma al EV efectivo para priorizar.

    No cambia si se ENTRA o no (el umbral min_ev_threshold sigue igual),
    solo afecta el ORDEN en que se evaluan las oportunidades.
    """
    if hours_to_close <= 0:
        return 0.0
    if hours_to_close <= 6:
        return 0.50   # Resolucion inminente: maxima prioridad
    if hours_to_close <= 24:
        return 0.35   # Mismo dia: alta prioridad
    if hours_to_close <= 48:
        return 0.20   # Manana: prioridad media-alta
    if hours_to_close <= 72:
        return 0.10   # Dentro de 72h: leve bonus
    return 0.0        # >72h: sin bonus, el outcome no es aun suficientemente claro


# ── Patron 4: Entrada Contrarian ─────────────────────────────────────────────
# Cuando el mercado lleva el precio a extremos (>88% o <12%), el crowd esta
# sobreposicionado. Si nuestra prob real difiere >6%, hay edge contrarian.
#
# Top wallets detectan esto y van en contra del sentimiento extremo.
# Win rate en estas entradas: mas estable que entradas normales porque
# el mercado ya "sobrepriced" el outcome popular.

CONTRARIAN_HIGH_THRESHOLD = 0.88   # Si YES > 88%, evaluar vender YES / comprar NO
CONTRARIAN_LOW_THRESHOLD  = 0.12   # Si YES < 12%, evaluar comprar YES
CONTRARIAN_MIN_DEVIATION  = 0.06   # Minima diferencia entre precio y prob real


def detect_contrarian_signal(
    yes_price: float,
    our_prob_yes: float,
) -> Optional[dict]:
    """
    Detecta si hay una entrada contrarian valida.

    Returns None si no hay señal, o dict con los detalles si la hay.
    """
    deviation = abs(yes_price - our_prob_yes)
    if deviation < CONTRARIAN_MIN_DEVIATION:
        return None

    # Crowd sobrecalentado al alza: YES muy caro, mercado sobreestima el outcome
    if yes_price > CONTRARIAN_HIGH_THRESHOLD and our_prob_yes < yes_price - CONTRARIAN_MIN_DEVIATION:
        return {
            "signal": "SELL_YES",   # Comprar NO
            "market_price": yes_price,
            "our_prob": our_prob_yes,
            "deviation": deviation,
            "description": f"Crowd sobrecomprado: YES a {yes_price:.0%} pero prob real {our_prob_yes:.0%}",
        }

    # Crowd sobrecalentado a la baja: YES muy barato, mercado subestima el outcome
    if yes_price <= CONTRARIAN_LOW_THRESHOLD and our_prob_yes > yes_price + CONTRARIAN_MIN_DEVIATION:
        return {
            "signal": "BUY_YES",
            "market_price": yes_price,
            "our_prob": our_prob_yes,
            "deviation": deviation,
            "description": f"Crowd sobrevendido: YES a {yes_price:.0%} pero prob real {our_prob_yes:.0%}",
        }

    return None


# ── Calculos base ─────────────────────────────────────────────────────────────

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
        p = norm.cdf(temp_high, loc=forecast_high, scale=std_dev) - \
            norm.cdf(temp_low,  loc=forecast_high, scale=std_dev)
    elif bucket_type == "below":
        p = norm.cdf(temp_high, loc=forecast_high, scale=std_dev)
    elif bucket_type == "above":
        p = 1 - norm.cdf(temp_low, loc=forecast_high, scale=std_dev)
    else:
        return 0.0
    return max(0.0, min(1.0, float(p)))


def calc_ev(our_prob: float, market_price: float, spread_pct: float = 0.0) -> float:
    """
    Calcula el Expected Value de comprar a market_price, descontando el spread.
    EV como porcentaje del capital invertido.
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
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
    Calcula el tamano de posicion usando Kelly Criterion fraccionado (Quarter Kelly).

    f* = (b*p - q) / b  × kelly_fraction
    donde b = (1/price - 1), p = nuestra prob, q = 1-p
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1.0 / market_price) - 1
    p = our_prob
    q = 1.0 - p
    kelly_full = (b * p - q) / b
    if kelly_full <= 0:
        return 0.0
    raw_size = kelly_full * bot_params.kelly_fraction * balance_usdc
    size = max(bot_params.min_position_usdc, min(bot_params.max_position_usdc, raw_size))
    return round(size, 2)


def calc_annualized_return(edge: float, hold_days: float) -> float:
    """
    Retorno anualizado segun velocidad de capital (Patron 1).
    Annualized = (1 + edge)^(365/hold_days) - 1
    """
    if hold_days <= 0 or edge <= -1:
        return 0.0
    return (1 + edge) ** (365 / hold_days) - 1


# ── Evaluacion principal ──────────────────────────────────────────────────────

def evaluate_market(market: dict, forecast: dict, balance_usdc: float) -> Optional[dict]:
    """
    Evalua si un mercado ofrece edge suficiente para tradear.

    Aplica los 5 patrones: prioridad temporal (P1), deteccion contrarian (P4),
    y prepara los datos necesarios para exit_manager (P3, P5).

    forecast puede ser el resultado de get_ensemble_high() (campos extendidos)
    o el antiguo get_forecast_high() (compatibilidad hacia atras).
    """
    high_f  = forecast["high_f"]
    std_dev = forecast["std_dev"]

    # Reducir std_dev si hay alto consenso multi-modelo (Fase 1: confidence_boost)
    confidence_boost = forecast.get("confidence_boost", 0.0)
    if confidence_boost > 0:
        std_dev = std_dev * (1.0 - confidence_boost * 0.5)
        std_dev = max(std_dev, 1.0)

    our_prob = calc_bucket_probability(
        forecast_high=high_f,
        std_dev=std_dev,
        temp_low=market["temp_low"],
        temp_high=market["temp_high"],
        bucket_type=market["bucket_type"],
    )

    market_price = market["yes_price"]
    spread_pct   = market.get("spread_pct", 0.0)
    hours_to_close = market.get("hours_to_close", 999)

    ev    = calc_ev(our_prob, market_price, spread_pct)
    no_prob  = 1 - our_prob
    no_price = 1 - market_price
    ev_no = calc_ev(no_prob, no_price, spread_pct) if no_price > 0 else 0.0

    # ── Patron 4: Señal contrarian ────────────────────────────────────────────
    contrarian = detect_contrarian_signal(market_price, our_prob)

    # Elegir el mejor lado
    if ev >= ev_no and ev >= bot_params.min_ev_threshold:
        side         = "YES"
        size_usdc    = calc_kelly_size(our_prob, market_price, balance_usdc)
        final_ev     = ev
        token_id     = market["yes_token_id"]
        entry_price  = market_price
        our_edge_prob = our_prob
    elif ev_no > ev and ev_no >= bot_params.min_ev_threshold:
        side         = "NO"
        size_usdc    = calc_kelly_size(no_prob, no_price, balance_usdc)
        final_ev     = ev_no
        token_id     = market["no_token_id"]
        entry_price  = no_price
        our_edge_prob = no_prob
    else:
        return None

    if size_usdc < bot_params.min_position_usdc:
        return None
    if token_id is None:
        return None

    # ── Patron 1: Score de prioridad temporal ─────────────────────────────────
    time_bonus    = calc_time_priority_bonus(hours_to_close)
    priority_score = final_ev + time_bonus   # Usado para ordenar oportunidades

    # ── Retorno anualizado estimado (para logging y UI) ───────────────────────
    hold_days_estimate = max(hours_to_close / 24, 0.1)
    annualized = calc_annualized_return(final_ev, hold_days_estimate)

    return {
        # Identificacion
        "market_title":  market["title"],
        "condition_id":  market["condition_id"],
        "city":          market["city"],
        "station":       market["station"],
        # Trade
        "side":          side,
        "token_id":      token_id,
        "entry_price":   entry_price,
        # Probabilidades y edge
        "our_prob":      our_edge_prob,
        "market_prob":   market_price if side == "YES" else no_price,
        "ev_pct":        round(final_ev * 100, 1),
        # Patron 1: Prioridad temporal
        "priority_score":   round(priority_score, 4),
        "time_bonus":       round(time_bonus, 3),
        "annualized_return": round(annualized * 100, 1),
        # Patron 4: Contrarian
        "contrarian_signal": contrarian,
        "is_contrarian":     contrarian is not None,
        # Sizing
        "size_usdc":  size_usdc,
        "shares":     round(size_usdc / entry_price, 2),
        # Forecast meteorologico
        "forecast_high": high_f,
        "forecast_std":  std_dev,
        "temp_low":      market["temp_low"],
        "temp_high":     market["temp_high"],
        # Ensemble multi-modelo (Fase 1)
        "forecast_confidence": forecast.get("confidence", "unknown"),
        "forecast_sources":    forecast.get("sources_used", []),
        "noaa_high_f":         forecast.get("noaa_high_f"),
        "openmeteo_high_f":    forecast.get("openmeteo_high_f"),
        "current_obs_f":       forecast.get("current_obs_f"),
        "models_available":    forecast.get("models_available", 0),
        "consensus_std":       forecast.get("consensus_std", 0),
        "peak_locked":         forecast.get("peak_locked", False),
        # Calidad de mercado
        "hours_to_close":      hours_to_close,
        "liquidity":           market["liquidity"],
        "volume_24h":          market.get("volume_24h", 0),
        "volume_7d":           market.get("volume_7d", 0),
        "volume_total":        market.get("volume_total", 0),
        "spread_pct":          spread_pct,
        "best_bid":            market.get("best_bid", 0),
        "best_ask":            market.get("best_ask", 0),
        "last_trade_price":    market.get("last_trade_price", entry_price),
        "competitive_score":   market.get("competitive_score", 0),
        "accepting_orders":    market.get("accepting_orders", True),
        "min_order_size":      market.get("min_order_size", 5),
    }
