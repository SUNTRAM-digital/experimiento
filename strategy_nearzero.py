"""
Fase 5 — Near-Zero Entry Strategy

Los wallets mas rentables de Polymarket acumulan contratos a 2-8 centavos
semanas antes en outcomes near-certain. Patron tipico: $8 → $200, repetido
cientos de veces.

Por que funciona:
  - El mercado subestima outcomes que "se ven imposibles" a 30 dias
  - El EV puede ser enorme: precio 0.07, prob real 0.30 → EV = +328%
  - El riesgo absoluto es minimo ($1-3 por posicion)
  - La asimetria es extrema: perdes $1 o ganas $15-40

Umbrales:
  - MAX_NEAR_ZERO_PRICE = 0.08   (maximo 8 centavos para considerar near-zero)
  - MIN_PROB_FOR_ENTRY  = 0.20   (prob minima para que valga el riesgo)
  - MIN_EV_NEAR_ZERO    = 1.00   (EV minimo del 100% — bar muy bajo dado el riesgo)
  - MAX_SIZE_USDC       = 3.00   (nunca mas de $3 por posicion near-zero)
  - MIN_SIZE_USDC       = 0.50   (minimo para que valga la comision)

Uso:
  from strategy_nearzero import evaluate_nearzero, scan_nearzero_opportunities
"""
from typing import Optional


# ── Umbrales ───────────────────────────────────────────────────────────────────

MAX_NEAR_ZERO_PRICE  = 0.08   # Precio maximo para ser considerado near-zero
MIN_PROB_FOR_ENTRY   = 0.20   # Prob minima estimada para entrar
MIN_EV_NEAR_ZERO     = 1.00   # EV minimo del 100% (como decimal = 1.0)
MAX_SIZE_USDC        = 3.00   # Maximo por posicion near-zero
MIN_SIZE_USDC        = 0.50   # Minimo por posicion
MAX_HOURS_HORIZON    = 720.0  # Maximo 30 dias de horizonte (mas largo que lo normal)
MIN_VOLUME_USDC      = 20.0   # Liquidez minima en el libro


# ── Calculo de EV near-zero ────────────────────────────────────────────────────

def calc_nearzero_ev(price: float, estimated_prob: float) -> float:
    """
    Calcula el Expected Value de una entrada near-zero.

    EV = P_true * (1/price - 1) - (1 - P_true)
       = P_true * payout_ratio - (1 - P_true)

    Retorna EV como multiplo del capital arriesgado.
    Ej: EV=3.28 significa que en esperanza ganamos 3.28x lo que arriesgamos.
    """
    if price <= 0 or price >= 1:
        return 0.0
    payout_ratio = (1.0 / price) - 1.0
    ev = estimated_prob * payout_ratio - (1.0 - estimated_prob)
    return round(ev, 4)


def calc_nearzero_size(
    ev: float,
    balance_usdc: float,
    wallet_signal_count: int = 0,
) -> float:
    """
    Sizing para entradas near-zero — siempre conservador.

    Base: MIN_SIZE_USDC
    Escala segun EV y confirmacion de wallets:
      - EV 100-200%: $0.50
      - EV 200-400%: $1.00
      - EV >400%:    $2.00
      - + $0.50 por cada wallet de referencia que ya este posicionado (max +$1)

    Nunca supera MAX_SIZE_USDC ni 2% del balance.
    """
    if ev < MIN_EV_NEAR_ZERO:
        return 0.0

    if ev < 2.0:
        base = 0.50
    elif ev < 4.0:
        base = 1.00
    else:
        base = 2.00

    # Bonus por confirmacion de smart wallets
    wallet_bonus = min(wallet_signal_count * 0.50, 1.00)
    size = base + wallet_bonus

    # Limites absolutos
    size = min(size, MAX_SIZE_USDC)
    size = min(size, balance_usdc * 0.02)   # nunca mas del 2% del balance
    size = max(size, MIN_SIZE_USDC) if balance_usdc >= MIN_SIZE_USDC * 2 else 0.0

    return round(size, 2)


# ── Evaluador principal ────────────────────────────────────────────────────────

def evaluate_nearzero(
    market: dict,
    estimated_prob: float,
    balance_usdc: float,
    wallet_signals: list[dict] | None = None,
) -> Optional[dict]:
    """
    Evalua si un mercado es una buena entrada near-zero.

    Args:
        market:         dict del mercado (necesita yes_price, hours_to_close,
                        liquidity, market_title, condition_id)
        estimated_prob: nuestra estimacion de la prob real (0-1)
        balance_usdc:   balance disponible
        wallet_signals: lista de señales de smart wallets en este mercado

    Returns:
        dict con la oportunidad si cumple los criterios, None si no.
    """
    yes_price    = market.get("yes_price", 1.0)
    hours        = market.get("hours_to_close", 0.0)
    liquidity    = market.get("liquidity", 0.0)
    title        = market.get("market_title", "")
    condition_id = market.get("condition_id", "")

    # ── Filtros de elegibilidad ────────────────────────────────────────────────
    if yes_price > MAX_NEAR_ZERO_PRICE:
        return None   # No es near-zero

    if yes_price <= 0.001:
        return None   # Precio irreal / mercado muerto

    if estimated_prob < MIN_PROB_FOR_ENTRY:
        return None   # Nuestra estimacion es demasiado baja

    if hours <= 0 or hours > MAX_HOURS_HORIZON:
        return None   # Fuera de horizonte temporal

    if liquidity < MIN_VOLUME_USDC:
        return None   # Sin liquidez suficiente para salir

    # ── Calculos ───────────────────────────────────────────────────────────────
    ev = calc_nearzero_ev(yes_price, estimated_prob)

    if ev < MIN_EV_NEAR_ZERO:
        return None   # EV insuficiente

    # Contar confirmaciones de smart wallets
    wallet_count = len(wallet_signals) if wallet_signals else 0
    size_usdc    = calc_nearzero_size(ev, balance_usdc, wallet_count)

    if size_usdc <= 0:
        return None

    shares = round(size_usdc / yes_price, 1)

    # Clasificar calidad de la oportunidad
    if ev >= 5.0 and wallet_count >= 2:
        quality = "A+"
    elif ev >= 3.0 and wallet_count >= 1:
        quality = "A"
    elif ev >= 2.0:
        quality = "B"
    else:
        quality = "C"

    payout_if_yes = round(shares * (1.0 - yes_price), 2)   # ganancia neta si resuelve YES

    return {
        "type":           "near_zero",
        "market_title":   title,
        "condition_id":   condition_id,
        "side":           "YES",
        "entry_price":    yes_price,
        "estimated_prob": estimated_prob,
        "ev":             ev,
        "ev_pct":         round(ev * 100, 1),
        "size_usdc":      size_usdc,
        "shares":         shares,
        "payout_if_yes":  payout_if_yes,
        "payout_ratio":   round(1.0 / yes_price, 1),   # "X to 1"
        "hours_to_close": hours,
        "wallet_signals": wallet_signals or [],
        "wallet_count":   wallet_count,
        "quality":        quality,
        "liquidity":      liquidity,
    }


def scan_nearzero_opportunities(
    markets: list[dict],
    prob_estimator,   # callable(market) -> float | None
    balance_usdc: float,
    wallet_signals_by_cid: dict | None = None,
) -> list[dict]:
    """
    Escanea una lista de mercados y retorna las oportunidades near-zero ordenadas.

    Args:
        markets:               lista de mercados activos
        prob_estimator:        funcion que recibe un market dict y retorna prob estimada
        balance_usdc:          capital disponible
        wallet_signals_by_cid: {condition_id: [wallet_signals]} si se tiene info de wallets

    Returns:
        lista de oportunidades near-zero ordenadas por EV desc.
    """
    opportunities = []
    wmap = wallet_signals_by_cid or {}

    for market in markets:
        yes_price = market.get("yes_price", 1.0)
        if yes_price > MAX_NEAR_ZERO_PRICE:
            continue   # Skip rapido: no near-zero

        estimated_prob = prob_estimator(market)
        if estimated_prob is None:
            continue

        cid     = market.get("condition_id", "")
        signals = wmap.get(cid, [])

        opp = evaluate_nearzero(market, estimated_prob, balance_usdc, signals)
        if opp:
            opportunities.append(opp)

    # Ordenar: primero calidad, luego EV
    quality_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
    opportunities.sort(key=lambda x: (quality_order.get(x["quality"], 4), -x["ev"]))
    return opportunities
