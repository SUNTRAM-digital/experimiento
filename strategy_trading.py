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
    min_entry_price: float        = 0.10   # no comprar si token_price < este (dead market)
    max_entry_price: float        = 0.30   # (punto 10) ceiling para R:R favorable — no comprar si token > este
    trend_prefer_winning: bool    = True   # preferir lado trending (token en zona 0.35-0.55) vs cheapest
    trend_reversal_gap: float     = 0.05   # opposite side solo si ganador cayó >= este en última lectura
    profit_offset: float          = 0.30   # vender en entry + esto (punto 10: 0.20→0.30 para R:R≥1:1 con max_entry=0.30)
    exit_deadline_min: float      = 3.0    # forzar salida a T-Xmin del cierre
    min_entry_minutes_left: float = 6.0    # no abrir si quedan menos minutos
    max_entries_per_market: int   = 3      # tope de entradas por mercado
    max_open_per_side: int        = 2      # tope de posiciones abiertas simultáneas por lado
    min_spread: float             = 0.02   # spread mínimo bid/ask para entrar
    stake_usdc: float             = 5.0    # tamaño por entrada (USDC)
    one_open_at_a_time: bool      = True   # no abrir si ya hay 1+ OPEN — primero vender
    # Punto 14 — comprar el lado más PROBABLE (favorito), no el más barato.
    buy_probable: bool            = True   # True: comprar lado trending/favorito (price alto); False: cheapest
    probable_min_price: float     = 0.55   # piso del favorito (evita 50/50 sin edge)
    probable_max_price: float     = 0.85   # techo del favorito (evita pagar casi fill)
    probable_profit_offset: float = 0.08   # offset más pequeño en modo probable (quick flip)
    # Punto 12 — stop-loss escalonado (enfoque A)
    sl_enabled: bool              = True   # activar stop-loss escalonado
    sl_trigger_drop: float        = 0.50   # caída vs entry que arma SL (0.50 = precio <= entry*0.50)
    sl_wait_min: float            = 3.0    # min a esperar tras trigger antes de vender
    sl_min_recover_factor: float  = 0.50   # vender si hay bid >= entry * este factor (entry/2)
    panic_trigger_drop: float     = 0.80   # caída catastrófica (precio <= entry*0.20)
    panic_min_recover_factor: float = 0.33 # vender si hay bid >= entry * este factor (entry/3)


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
    """Evalúa si hay oportunidad de ENTRADA. Retorna EntrySignal o None."""
    sig, _ = evaluate_entry_verbose(market, open_positions, params)
    return sig


def evaluate_entry_verbose(
    market: dict,
    open_positions: list,
    params: TradingParams,
) -> tuple:
    """
    Como evaluate_entry pero retorna (EntrySignal|None, reason_str).
    reason_str explica por qué se aceptó/rechazó (para logs).
    """
    minutes_to_close = market.get("minutes_to_close", 0)
    up_price   = float(market.get("up_price",   0.5))
    down_price = float(market.get("down_price", 0.5))

    if minutes_to_close < params.min_entry_minutes_left:
        return None, f"min_entry_min: quedan {minutes_to_close:.1f}m < {params.min_entry_minutes_left:.1f}m requeridos"

    open_now = [p for p in open_positions if p.get("status") == "OPEN"]

    if params.one_open_at_a_time and len(open_now) >= 1:
        return None, f"one_open_at_a_time: ya hay {len(open_now)} posición OPEN (vender primero)"

    if len(open_positions) >= params.max_entries_per_market:
        return None, f"max_entries: {len(open_positions)} >= cap {params.max_entries_per_market} en este mercado"

    open_up   = sum(1 for p in open_positions if p.get("side") == "UP"   and p.get("status") == "OPEN")
    open_down = sum(1 for p in open_positions if p.get("side") == "DOWN" and p.get("status") == "OPEN")

    # Punto 14 — modo PROBABLE (comprar favorito) vs cheapest
    if getattr(params, "buy_probable", False):
        floor   = getattr(params, "probable_min_price", 0.55)
        ceiling = getattr(params, "probable_max_price", 0.85)
        offset  = getattr(params, "probable_profit_offset", 0.08)
        mode_tag = "probable"
    else:
        floor   = params.min_entry_price
        ceiling = params.max_entry_price
        offset  = params.profit_offset
        mode_tag = "cheapest"

    # Candidatos: precio dentro de [floor, ceiling]
    candidates = []
    rejected = []
    if open_up >= params.max_open_per_side:
        rejected.append(f"UP@{up_price:.3f} (cap side {params.max_open_per_side})")
    elif up_price < floor:
        rejected.append(f"UP@{up_price:.3f} < floor {floor:.2f} ({mode_tag})")
    elif up_price > ceiling:
        rejected.append(f"UP@{up_price:.3f} > ceiling {ceiling:.2f} ({mode_tag})")
    else:
        candidates.append(("UP", up_price, market.get("up_token")))

    if open_down >= params.max_open_per_side:
        rejected.append(f"DOWN@{down_price:.3f} (cap side {params.max_open_per_side})")
    elif down_price < floor:
        rejected.append(f"DOWN@{down_price:.3f} < floor {floor:.2f} ({mode_tag})")
    elif down_price > ceiling:
        rejected.append(f"DOWN@{down_price:.3f} > ceiling {ceiling:.2f} ({mode_tag})")
    else:
        candidates.append(("DOWN", down_price, market.get("down_token")))

    if not candidates:
        return None, f"sin candidatos ({mode_tag}) | " + " | ".join(rejected)

    # Selección del lado:
    #   - probable: HIGHEST price (favorito/trending) — quick flip a price+offset_small
    #   - cheapest: LOWEST price — esperar movimiento grande
    if getattr(params, "buy_probable", False):
        candidates.sort(key=lambda c: -c[1])  # desc (favorito primero)
    else:
        candidates.sort(key=lambda c: c[1])   # asc (cheapest primero)
    side, price, token_id = candidates[0]

    # Reversal gate (solo modo cheapest): si el barato es muy barato y opposite
    # es aplastante favorito, mercado prácticamente muerto del lado barato.
    if not getattr(params, "buy_probable", False) and price < 0.35:
        opposite_price = down_price if side == "UP" else up_price
        if opposite_price > 0.80:
            return None, (
                f"{side}@{price:.3f} perdedor vs opposite@{opposite_price:.3f} "
                f"trending fuerte — sin reversal signal, bloqueado"
            )
    if not token_id:
        return None, f"{side}@{price:.3f} sin token_id"

    # No re-entrar al mismo precio que una posición aún abierta del mismo lado
    for p in open_positions:
        if p.get("side") == side and p.get("status") == "OPEN":
            if abs(float(p.get("entry_price", 0)) - price) < 0.03:
                return None, f"{side}@{price:.3f} muy cerca de posición OPEN existente @{p.get('entry_price')}"

    target = round(price + offset, 3)
    if target >= 0.97:
        return None, f"{side}@{price:.3f} target {target:.3f} >= 0.97 (sin buyer)"

    sig = EntrySignal(
        side=side,
        token_id=token_id,
        entry_price=round(price, 3),
        target_price=target,
        stake_usdc=params.stake_usdc,
        reason=f"{mode_tag}: {side}@{price:.3f} → target {target:.3f} (+{offset:.2f})",
    )
    return sig, f"OK {side}@{price:.3f} target={target:.3f}"


def should_exit_position(
    position: dict,
    current_token_price: float,
    minutes_to_close: float,
    params: TradingParams,
    now_ts: Optional[int] = None,
) -> Optional[str]:
    """
    Evalúa si una posición abierta debe cerrarse AHORA.

    Retorna la razón de cierre o None si debe mantenerse abierta:
      - "TARGET_HIT":   precio alcanzó o superó target
      - "FORCED_EXIT":  quedan menos de exit_deadline_min minutos
      - "STOP_LOSS":    SL escalonado nivel 1 (caída >=50%, esperó N min, bid >= entry/2)
      - "PANIC_EXIT":   SL escalonado nivel 2 (caída >=80%, bid >= entry/3)
      - None: mantener abierta

    Stop-loss escalonado (punto 12) — arma un timer cuando el precio cae bajo
    el trigger y vende cuando el bid cruza de vuelta el min_recover, o a tiempo
    agotado. La posición registra `sl_armed_ts` en la primera lectura bajo trigger.
    """
    if position.get("status") != "OPEN":
        return None

    target = float(position.get("target_price", 1.0))
    if current_token_price >= target:
        return "TARGET_HIT"

    if minutes_to_close <= params.exit_deadline_min:
        return "FORCED_EXIT"

    # Stop-loss escalonado (A)
    if getattr(params, "sl_enabled", False):
        import time as _t
        entry = float(position.get("entry_price", 0))
        if entry > 0:
            drop_pct = 1.0 - (current_token_price / entry)  # fracción caída
            panic_thr = getattr(params, "panic_trigger_drop", 0.80)
            sl_thr    = getattr(params, "sl_trigger_drop",    0.50)
            sl_min    = entry * getattr(params, "sl_min_recover_factor",    0.50)

            # Nivel 2 (panic): caída catastrófica — salvar lo que quede (vende al bid actual).
            if drop_pct >= panic_thr and current_token_price > 0:
                return "PANIC_EXIT"

            # Nivel 1 (SL): caída >=50% — armar timer; vender si recupera a entry/2 o tiempo cumplido
            now = now_ts if now_ts is not None else int(_t.time())
            wait_min = getattr(params, "sl_wait_min", 3.0)
            armed_ts = position.get("sl_armed_ts")

            if armed_ts is None:
                # Sólo armamos si estamos bajo trigger ahora
                if drop_pct >= sl_thr:
                    position["sl_armed_ts"] = now
                    return None  # recién armado
            else:
                mins_armed = (now - int(armed_ts)) / 60.0
                if mins_armed >= wait_min and current_token_price >= sl_min:
                    return "STOP_LOSS"

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
