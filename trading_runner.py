"""
Trading runner — ejecuta la estrategia de trading (buy cheap / sell target)
sobre mercados UP/DOWN activos. Modo phantom + real.

Flujo por mercado por ciclo:
  1. Monitorear posiciones abiertas:
     - Obtener precio actual del token (CLOB live price)
     - Si target alcanzado → cerrar (TARGET_HIT)
     - Si quedan < exit_deadline_min min → cerrar (FORCED_EXIT)
  2. Evaluar entrada nueva:
     - Si hay capacidad (< max_entries) y quedan >= min_entry_minutes_left
     - Si token UP o DOWN <= entry_threshold → abrir posición

Flujo al cierre de mercado:
  - Cualquier posición aún OPEN se resuelve binario vía CLOB token price
  - RESOLVED_WIN (token ≈ 1.00) o RESOLVED_LOSS (≈ 0.00)
"""
import logging
import time
from typing import Optional

from strategy_trading import (
    TradingParams,
    evaluate_entry,
    should_exit_position,
    calc_pnl,
    resolve_unsold_position,
)
import trading_positions as tp

logger = logging.getLogger("weatherbot")


def params_from_config(bot_params) -> TradingParams:
    return TradingParams(
        entry_threshold        = float(getattr(bot_params, "trading_entry_threshold", 0.35)),
        profit_offset          = float(getattr(bot_params, "trading_profit_offset", 0.20)),
        exit_deadline_min      = float(getattr(bot_params, "trading_exit_deadline_min", 3.0)),
        min_entry_minutes_left = float(getattr(bot_params, "trading_min_entry_minutes_left", 6.0)),
        max_entries_per_market = int(getattr(bot_params, "trading_max_entries_per_market", 3)),
        max_open_per_side      = int(getattr(bot_params, "trading_max_open_per_side", 2)),
        stake_usdc             = float(getattr(bot_params, "trading_stake_usdc", 5.0)),
    )


async def _get_token_price(token_id: str) -> Optional[float]:
    """Obtiene precio live del token desde CLOB."""
    try:
        from markets import get_live_price
        return await get_live_price(token_id)
    except Exception as e:
        logger.warning(f"[TRADING] Error obteniendo precio token {token_id[:12]}: {e}")
        return None


async def monitor_and_close(market: dict, is_real: bool, params: TradingParams) -> list:
    """
    Revisa posiciones abiertas del mercado y las cierra si corresponde.
    Retorna lista de posiciones cerradas (para logging).
    """
    slug = market["slug"]
    minutes_to_close = float(market.get("minutes_to_close", 0))
    open_positions = tp.get_open_positions(slug, is_real=is_real)
    if not open_positions:
        return []

    closed = []
    for pos in open_positions:
        token_id = pos.get("token_id")
        if not token_id:
            continue
        current = await _get_token_price(token_id)
        if current is None:
            continue

        reason = should_exit_position(pos, current, minutes_to_close, params)
        if not reason:
            continue

        # Ejecutar salida:
        # - Phantom: simulamos venta al current price
        # - Real: TODO — en fase 3, colocar market sell via CLOB
        exit_price = current
        pnl = calc_pnl(pos, exit_price)

        if is_real:
            # Fase 3 — ejecutar market sell real. Por ahora solo log.
            logger.warning(f"[TRADING] Real sell NO IMPLEMENTED aún, solo marcando: {pos.get('id')}")

        updated = tp.close_position(
            slug=slug,
            position_id=pos["id"],
            exit_price=exit_price,
            pnl_usdc=pnl,
            exit_reason=reason,
            is_real=is_real,
        )
        if updated:
            tag = "REAL" if is_real else "PHANTOM"
            logger.info(
                f"[TRADING {tag}] {reason} | {pos['side']} entry={pos['entry_price']:.3f} "
                f"exit={exit_price:.3f} pnl={pnl:+.2f} USDC | slug={slug[:30]}"
            )
            closed.append(updated)

    return closed


async def evaluate_and_open(market: dict, is_real: bool, params: TradingParams) -> Optional[dict]:
    """
    Evalúa si abrir nueva posición en el mercado.
    Retorna posición abierta o None.
    """
    slug = market["slug"]
    interval = int(market.get("interval_minutes", 15))
    end_ts = int(market.get("end_ts", 0))
    all_positions = tp.get_positions(slug, is_real=is_real)

    signal = evaluate_entry(market, all_positions, params)
    if not signal:
        return None

    # En real: verificar que tengamos USDC suficiente (fase 3)
    # Por ahora phantom + real usan el mismo camino de "abrir"; real se ejecuta en fase 3
    if is_real:
        logger.warning(f"[TRADING] Real buy NO IMPLEMENTED aún — skipping real entry {signal.side}")
        return None

    pos = tp.open_position(
        slug=slug,
        interval=interval,
        end_ts=end_ts,
        side=signal.side,
        token_id=signal.token_id,
        entry_price=signal.entry_price,
        target_price=signal.target_price,
        stake_usdc=signal.stake_usdc,
        is_real=is_real,
    )
    tag = "REAL" if is_real else "PHANTOM"
    logger.info(
        f"[TRADING {tag}] OPEN {signal.side} @ {signal.entry_price:.3f} target={signal.target_price:.3f} "
        f"stake={signal.stake_usdc:.2f} | {signal.reason} | slug={slug[:30]}"
    )
    return pos


async def resolve_stale_positions(is_real: bool = False) -> list:
    """
    Revisa TODAS las posiciones abiertas globalmente y resuelve las de mercados
    ya cerrados (mercado expiró pero nunca se vendió/forzó). Usa CLOB para
    determinar outcome binario.

    Llamar periódicamente en el loop principal.
    """
    from markets import get_live_price

    now = int(time.time())
    all_open = tp.all_open_positions(is_real=is_real)
    resolved = []

    for pos in all_open:
        end_ts = pos.get("end_ts", 0)
        # Solo resolver si el mercado ya cerró hace > 30s (para darle tiempo al CLOB)
        if now < end_ts + 30:
            continue

        token_id = pos.get("token_id")
        if not token_id:
            continue

        try:
            price = await get_live_price(token_id)
        except Exception:
            price = None

        if price is None:
            continue

        # Decidir outcome binario
        if price >= 0.95:
            status = "RESOLVED_WIN"
            won = True
        elif price <= 0.05:
            status = "RESOLVED_LOSS"
            won = False
        else:
            # Mercado aún no resolvió — esperar
            continue

        exit_price, pnl = resolve_unsold_position(pos, won)
        updated = tp.close_position(
            slug=pos["slug"],
            position_id=pos["id"],
            exit_price=exit_price,
            pnl_usdc=pnl,
            exit_reason=status,
            is_real=is_real,
        )
        if updated:
            tag = "REAL" if is_real else "PHANTOM"
            logger.info(
                f"[TRADING {tag}] {status} | {pos['side']} entry={pos['entry_price']:.3f} "
                f"exit={exit_price:.2f} pnl={pnl:+.2f} USDC | slug={pos['slug'][:30]}"
            )
            resolved.append(updated)

    return resolved


async def run_cycle(market: dict, bot_params) -> dict:
    """
    Ciclo completo por mercado: monitor posiciones + evaluar entrada.
    Se ejecuta DOS veces — una para phantom, otra para real si está habilitado.

    Retorna dict con acciones tomadas (para logging/UI).
    """
    params = params_from_config(bot_params)
    enabled = bool(getattr(bot_params, "trading_mode_enabled", True))
    if not enabled:
        return {"skipped": "trading_mode_disabled"}

    real_on = bool(getattr(bot_params, "trading_real_enabled", False))

    result = {
        "phantom": {"closed": [], "opened": None},
        "real":    {"closed": [], "opened": None},
    }

    # 1. Phantom (siempre activo si trading_mode_enabled)
    result["phantom"]["closed"] = await monitor_and_close(market, is_real=False, params=params)
    result["phantom"]["opened"] = await evaluate_and_open(market, is_real=False, params=params)

    # 2. Real (solo si habilitado)
    if real_on:
        result["real"]["closed"] = await monitor_and_close(market, is_real=True, params=params)
        result["real"]["opened"] = await evaluate_and_open(market, is_real=True, params=params)

    return result
