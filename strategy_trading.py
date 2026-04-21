"""
Estrategia de TRADING en mercados UP/DOWN de Polymarket.

Reemplaza la estrategia de PREDICCIÓN (UP/DOWN por señal técnica) por TRADING
de volatilidad del precio del token: comprar barato y vender caro durante la
ventana del mercado, sin esperar resolución.

Regla base:
  - Entrada: token_price <= entry_threshold (ej: 0.35)
  - Salida:  entry_price + profit_offset (ej: 0.30 -> 0.50)
  - Forzar salida a T-exit_deadline_min del cierre del mercado
  - Múltiples entradas por mercado permitidas (max_entries_per_market)

Si no se alcanza target antes del T-deadline, se cierra al precio actual
(al bid, zona de riesgo). Si el mercado cierra con posición abierta, se
resuelve binario: token_price -> 0.00 o 1.00.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass
class TradingParams:
    """Parámetros de la estrategia de trading — configurables vía UI."""
    entry_threshold: float        = 0.35   # comprar si token_price <= este
    profit_offset: float          = 0.20   # vender en entry + esto
    exit_deadline_min: float      = 3.0    # forzar salida a T-Xmin del cierre
    min_entry_minutes_left: float = 6.0    # no abrir si quedan menos minutos
    max_entries_per_market: int   = 3      # tope de entradas por mercado
    max_open_per_side: int        = 2      # tope de posiciones abiertas simultáneas por lado
    min_spread: float             = 0.02   # spread mínimo bid/ask para entrar
    stake_usdc: float             = 5.0    # tamaño por entrada (USDC)


@dataclass
class EntrySignal:
    """Señal de entrada detectada."""
    side: str                 # "UP" o "DOWN"
    token_id: str             # token a comprar
    entry_price: float        # precio actual del token (ask)
    target_price: float       # precio de venta objetivo
    stake_usdc: float         # tamaño
    reason: str               # descripción humana


def evaluate_entry(
    market: dict,
    open_positions: list,
    params: TradingParams,
) -> Optional[EntrySignal]:
    """
    Evalúa si hay oportunidad de ENTRADA en el mercado.

    Retorna EntrySignal si se debe abrir nueva posición, None si no.
    """
    minutes_to_close = market.get("minutes_to_close", 0)
    if minutes_to_close < params.min_entry_minutes_left:
        return None

    if len(open_positions) >= params.max_entries_per_market:
        return None

    open_up   = sum(1 for p in open_positions if p.get("side") == "UP"   and p.get("status") == "OPEN")
    open_down = sum(1 for p in open_positions if p.get("side") == "DOWN" and p.get("status") == "OPEN")

    up_price   = float(market.get("up_price",   0.5))
    down_price = float(market.get("down_price", 0.5))
    spread_pct = float(market.get("spread_pct", 1.0))

    if spread_pct < params.min_spread:
        # Spread demasiado estrecho — target no se alcanzará
        pass

    # Buscar el lado más barato que esté bajo el umbral
    candidates = []
    if up_price <= params.entry_threshold and open_up < params.max_open_per_side:
        candidates.append(("UP", up_price, market.get("up_token")))
    if down_price <= params.entry_threshold and open_down < params.max_open_per_side:
        candidates.append(("DOWN", down_price, market.get("down_token")))

    if not candidates:
        return None

    # Si ambos califican, elegir el más barato
    candidates.sort(key=lambda c: c[1])
    side, price, token_id = candidates[0]
    if not token_id:
        return None

    # No re-entrar al mismo precio que una posición aún abierta del mismo lado
    for p in open_positions:
        if p.get("side") == side and p.get("status") == "OPEN":
            if abs(float(p.get("entry_price", 0)) - price) < 0.03:
                return None  # ya tenemos posición a precio similar

    target = round(price + params.profit_offset, 3)
    if target >= 0.97:
        # Target imposible (muy cerca de 1.00, no hay buyer) — no entrar
        return None

    return EntrySignal(
        side=side,
        token_id=token_id,
        entry_price=round(price, 3),
        target_price=target,
        stake_usdc=params.stake_usdc,
        reason=f"{side} barato {price:.3f} <= {params.entry_threshold:.2f} | target {target:.3f}",
    )


def should_exit_position(
    position: dict,
    current_token_price: float,
    minutes_to_close: float,
    params: TradingParams,
) -> Optional[str]:
    """
    Evalúa si una posición abierta debe cerrarse AHORA.

    Retorna la razón de cierre o None si debe mantenerse abierta:
      - "TARGET_HIT": precio alcanzó o superó target
      - "FORCED_EXIT": quedan menos de exit_deadline_min minutos
      - None: mantener abierta
    """
    if position.get("status") != "OPEN":
        return None

    target = float(position.get("target_price", 1.0))
    if current_token_price >= target:
        return "TARGET_HIT"

    if minutes_to_close <= params.exit_deadline_min:
        return "FORCED_EXIT"

    return None


def calc_pnl(position: dict, exit_price: float) -> float:
    """P&L realizado en USDC. qty = stake_usdc / entry_price (shares)."""
    entry = float(position.get("entry_price", 0))
    stake = float(position.get("stake_usdc", 0))
    if entry <= 0:
        return 0.0
    shares = stake / entry
    return round(shares * (exit_price - entry), 4)


def resolve_unsold_position(position: dict, token_won: bool) -> tuple:
    """
    Si el mercado cerró con la posición aún abierta:
      - Si el token que compramos ganó: shares * (1.0 - entry)
      - Si perdió: shares * (0.0 - entry) = -stake

    Retorna (exit_price, pnl).
    """
    entry = float(position.get("entry_price", 0))
    stake = float(position.get("stake_usdc", 0))
    exit_price = 1.0 if token_won else 0.0
    if entry <= 0:
        return exit_price, 0.0
    shares = stake / entry
    pnl = shares * (exit_price - entry)
    return exit_price, round(pnl, 4)
