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
    evaluate_entry_verbose,
    should_exit_position,
    calc_pnl,
    resolve_unsold_position,
)
import trading_positions as tp

logger = logging.getLogger("weatherbot")


def _tlog(level: str, msg: str) -> None:
    """Envía log al panel UI (via bot._log) y también al logger estándar.
    Lazy import para evitar ciclo con bot.py."""
    try:
        import bot as _bot
        _bot._log(level, msg)
    except Exception:
        # Fallback al logger base
        lvl = (level or "INFO").upper()
        if lvl == "ERROR":
            logger.error(msg)
        elif lvl == "WARN":
            logger.warning(msg)
        else:
            logger.info(msg)


# Mismo slippage/cap que el path real de UpDown
_REAL_SLIPPAGE        = 0.02
_REAL_MAX_ENTRY_PRICE = 0.89
_REAL_MIN_SHARES      = 6.0   # Polymarket min-order size efectivo: usar 6 para holgura sobre 5
_MIN_BID_DEPTH_SHARES = 6.0   # bids agregados mínimos para permitir entry (liquidez de salida)


async def _execute_real_buy(token_id: str, stake_usdc: float, side_label: str, slug: str):
    """
    Ejecuta compra real vía CLOB. Retorna (ok, fill_price, fill_shares, cost) o (False, None, None, None).
    Ajusta precio al ask live + slippage. Respeta min 5 shares (bump stake si hace falta).
    Descuenta cost de state.balance_usdc y guarda state.
    """
    import bot as _bot  # lazy para evitar ciclo
    if getattr(_bot, "_clob_client", None) is None:
        logger.warning("[TRADING REAL] No hay cliente CLOB — buy abortado")
        return False, None, None, None

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
    except Exception as e:
        logger.error(f"[TRADING REAL] py_clob_client no disponible: {e}")
        return False, None, None, None

    # Precio real del CLOB (ask)
    try:
        raw_price = _bot._clob_client.get_price(token_id, "BUY")
        if isinstance(raw_price, dict):
            live_ask = float(raw_price.get("price", raw_price.get("ask", 0)) or 0)
        else:
            live_ask = float(raw_price or 0)
    except Exception as e:
        logger.warning(f"[TRADING REAL] get_price fail {token_id[:12]}: {e}")
        return False, None, None, None

    if live_ask <= 0:
        logger.warning(f"[TRADING REAL] Ask invalido ({live_ask}) para {token_id[:12]}")
        return False, None, None, None
    if live_ask >= _REAL_MAX_ENTRY_PRICE:
        logger.warning(f"[TRADING REAL] Ask {live_ask:.3f} >= cap {_REAL_MAX_ENTRY_PRICE} — skip")
        return False, None, None, None

    price = round(min(live_ask + _REAL_SLIPPAGE, 0.99), 4)
    shares = round(stake_usdc / price, 2)
    if shares < _REAL_MIN_SHARES:
        stake_usdc = round(_REAL_MIN_SHARES * price + 0.01, 2)
        shares = round(stake_usdc / price, 2)

    cost = round(shares * price, 4)
    if _bot.state.balance_usdc < cost:
        logger.warning(
            f"[TRADING REAL] Balance insuficiente: need ${cost:.2f} have ${_bot.state.balance_usdc:.2f}"
        )
        return False, None, None, None

    try:
        order = OrderArgs(
            token_id=token_id,
            price=price,
            size=round(shares, 2),
            side=BUY,
            fee_rate_bps=1000,
        )
        signed = _bot._clob_client.create_order(order)
        resp = _bot._clob_client.post_order(signed, OrderType.GTC)
    except Exception as e:
        logger.error(f"[TRADING REAL] BUY post fail: {e}")
        return False, None, None, None

    if resp.get("success") or resp.get("status") in ("live", "matched"):
        _bot.state.balance_usdc -= cost
        try:
            _bot._save_state()
        except Exception:
            pass
        logger.info(
            f"[TRADING REAL] BUY FILLED {side_label} {shares:.2f}@{price:.3f}=${cost:.2f} | {slug[:30]}"
        )
        return True, price, shares, cost

    logger.warning(f"[TRADING REAL] BUY rechazado: {resp.get('errorMsg', resp)}")
    return False, None, None, None


async def _post_passive_sell_gtc(token_id: str, size: float, price: float, slug: str):
    """
    Pone orden SELL GTC passive (maker) en el libro al precio target.
    Se queda viva hasta que alguien la compre. Retorna order_id o None.
    """
    import bot as _bot
    if getattr(_bot, "_clob_client", None) is None:
        return None
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL
    except Exception as e:
        logger.error(f"[TRADING REAL] imports passive sell fail: {e}")
        return None
    size = max(float(size), _REAL_MIN_SHARES)
    try:
        order = OrderArgs(
            token_id=token_id,
            price=round(min(max(price, 0.02), 0.99), 4),
            size=round(size, 2),
            side=SELL,
            fee_rate_bps=1000,
        )
        signed = _bot._clob_client.create_order(order)
        resp = _bot._clob_client.post_order(signed, OrderType.GTC)
    except Exception as e:
        logger.error(f"[TRADING REAL] passive SELL post fail: {e}")
        return None
    if resp.get("success") or resp.get("status") in ("live", "matched"):
        oid = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        _tlog("INFO",
            f"[TRADING REAL] PASSIVE SELL posted {size:.2f}@{price:.3f} oid={str(oid)[:12]} | {slug[:30]}"
        )
        return oid
    _tlog("WARN", f"[TRADING REAL] passive SELL rechazada: {resp.get('errorMsg', resp)}")
    return None


async def _cancel_order(order_id: str) -> bool:
    import bot as _bot
    if not order_id or getattr(_bot, "_clob_client", None) is None:
        return False
    try:
        _bot._clob_client.cancel(order_id)
        return True
    except Exception as e:
        logger.warning(f"[TRADING REAL] cancel fail {order_id[:12]}: {e}")
        return False


async def _order_is_filled(order_id: str) -> bool:
    """True si la orden ya no está live (asumimos fill). Best-effort."""
    import bot as _bot
    if not order_id or getattr(_bot, "_clob_client", None) is None:
        return False
    try:
        o = _bot._clob_client.get_order(order_id)
        if not o:
            return True
        status = (o.get("status") or "").lower()
        if status in ("filled", "matched", "complete", "done"):
            return True
        size_matched = float(o.get("size_matched") or o.get("sizeMatched") or 0)
        size_orig    = float(o.get("original_size") or o.get("originalSize") or o.get("size") or 0)
        if size_orig > 0 and size_matched >= size_orig - 0.01:
            return True
        return False
    except Exception:
        return False


async def _execute_real_sell(token_id: str, size: float, slug: str):
    """
    Ejecuta venta real vía CLOB. Retorna (ok, fill_price, proceeds) o (False, None, None).
    Si no hay liquidez (bid ≈ 0), deja posición OPEN — stale_resolution la cerrará.
    """
    import bot as _bot
    if getattr(_bot, "_clob_client", None) is None:
        logger.warning("[TRADING REAL] No hay cliente CLOB — sell abortado")
        return False, None, None

    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL
        import httpx
    except Exception as e:
        logger.error(f"[TRADING REAL] imports fail: {e}")
        return False, None, None

    bid_price = 0.0
    try:
        r = httpx.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=8,
        )
        if r.status_code == 200:
            bids = r.json().get("bids", [])
            if bids:
                bid_price = float(bids[0].get("price", 0))
    except Exception:
        pass

    if bid_price <= 0.01:
        logger.warning(
            f"[TRADING REAL] Sin liquidez para vender {token_id[:12]} — dejando abierta"
        )
        return False, None, None

    size = max(float(size), _REAL_MIN_SHARES)

    try:
        order = OrderArgs(
            token_id=token_id,
            price=round(bid_price, 4),
            size=round(size, 2),
            side=SELL,
            fee_rate_bps=1000,
        )
        signed = _bot._clob_client.create_order(order)
        resp = _bot._clob_client.post_order(signed, OrderType.GTC)
    except Exception as e:
        logger.error(f"[TRADING REAL] SELL post fail: {e}")
        return False, None, None

    if resp.get("success") or resp.get("status") in ("live", "matched"):
        proceeds = round(size * bid_price, 4)
        _bot.state.balance_usdc += proceeds
        try:
            _bot._save_state()
        except Exception:
            pass
        logger.info(
            f"[TRADING REAL] SELL FILLED {size:.2f}@{bid_price:.3f}=${proceeds:.2f} | {slug[:30]}"
        )
        return True, bid_price, proceeds

    logger.warning(f"[TRADING REAL] SELL rechazado: {resp.get('errorMsg', resp)}")
    return False, None, None


def _check_real_safety(bot_params, prospective_stake: float):
    """
    Valida que sea seguro abrir una posición REAL nueva.
    Retorna (ok, reason). reason describe por qué se rechaza.
    Activa kill-switch si se cruza max_consec_losses (efecto secundario).
    """
    if bool(getattr(bot_params, "trading_real_killed", False)):
        return False, "kill_switch activo (reset manual en UI)"

    # Punto 19B — paper-to-live preflight gate (phantom debe probar primero)
    if not bool(getattr(bot_params, "trading_paper_gate_override", False)):
        gate = tp.phantom_gate_status(
            required_days=float(getattr(bot_params, "trading_paper_required_days", 7.0)),
            required_trades=int(getattr(bot_params, "trading_paper_required_trades", 200)),
            required_wr=float(getattr(bot_params, "trading_paper_required_wr", 0.75)),
        )
        if not gate["ok"]:
            return False, f"paper-gate: {', '.join(gate['reasons'])}"

    # Punto 19A — drawdown kill switch (-X% desde ATH cumulative real PnL)
    dd_halt = float(getattr(bot_params, "trading_real_drawdown_halt_pct", 0.40))
    if dd_halt > 0:
        dd = tp.real_equity_drawdown()
        if dd["peak"] > 0 and dd["drawdown_pct"] >= dd_halt:
            try:
                bot_params.trading_real_killed = True
                bot_params.save()
            except Exception:
                pass
            logger.warning(
                f"[TRADING REAL] DRAWDOWN KILL-SWITCH: peak={dd['peak']:.2f} "
                f"current={dd['current']:.2f} dd={dd['drawdown_pct']*100:.1f}%"
            )
            return False, f"drawdown {dd['drawdown_pct']*100:.1f}% >= {dd_halt*100:.0f}%"

    # 1. Pérdidas consecutivas — auto-kill si exceden
    max_consec = int(getattr(bot_params, "trading_real_max_consec_losses", 3))
    consec = tp.real_consecutive_losses()
    if consec >= max_consec:
        try:
            bot_params.trading_real_killed = True
            bot_params.save()
        except Exception:
            pass
        logger.warning(
            f"[TRADING REAL] KILL-SWITCH activado: {consec} pérdidas consecutivas (cap {max_consec})"
        )
        return False, f"kill-switch ({consec} pérdidas seguidas)"

    # 2. Daily loss cap (0 o negativo = deshabilitado)
    daily_limit = float(getattr(bot_params, "trading_real_daily_loss_limit_usdc", 5.0))
    if daily_limit > 0:
        pnl_today = tp.real_pnl_today_usdc()
        if pnl_today <= -abs(daily_limit):
            return False, f"daily loss {pnl_today:.2f} <= -{daily_limit:.2f}"

    # 3. Exposure cap (0 o negativo = deshabilitado)
    max_exposure = float(getattr(bot_params, "trading_real_max_exposure_usdc", 20.0))
    if max_exposure > 0:
        current_exposure = tp.real_exposure_usdc()
        if current_exposure + prospective_stake > max_exposure:
            return False, (
                f"exposure {current_exposure:.2f}+{prospective_stake:.2f} > cap {max_exposure:.2f}"
            )

    return True, ""


def params_from_config(bot_params) -> TradingParams:
    return TradingParams(
        entry_threshold        = float(getattr(bot_params, "trading_entry_threshold", 0.35)),
        min_entry_price        = float(getattr(bot_params, "trading_min_entry_price", 0.10)),
        max_entry_price        = float(getattr(bot_params, "trading_max_entry_price", 0.30)),
        trend_prefer_winning   = bool(getattr(bot_params, "trading_trend_prefer_winning", True)),
        profit_offset          = float(getattr(bot_params, "trading_profit_offset", 0.30)),
        exit_deadline_min      = float(getattr(bot_params, "trading_exit_deadline_min", 3.0)),
        min_entry_minutes_left = float(getattr(bot_params, "trading_min_entry_minutes_left", 6.0)),
        max_entries_per_market = int(getattr(bot_params, "trading_max_entries_per_market", 3)),
        max_open_per_side      = int(getattr(bot_params, "trading_max_open_per_side", 2)),
        stake_usdc             = float(getattr(bot_params, "trading_stake_usdc", 5.0)),
        one_open_at_a_time     = bool(getattr(bot_params, "trading_one_open_at_a_time", True)),
        sl_enabled               = bool(getattr(bot_params, "trading_sl_enabled", True)),
        sl_trigger_drop          = float(getattr(bot_params, "trading_sl_trigger_drop", 0.50)),
        sl_wait_min              = float(getattr(bot_params, "trading_sl_wait_min", 3.0)),
        sl_min_recover_factor    = float(getattr(bot_params, "trading_sl_min_recover_factor", 0.50)),
        panic_trigger_drop       = float(getattr(bot_params, "trading_panic_trigger_drop", 0.80)),
        panic_min_recover_factor = float(getattr(bot_params, "trading_panic_min_recover_factor", 0.33)),
        buy_probable             = bool(getattr(bot_params, "trading_buy_probable", True)),
        probable_min_price       = float(getattr(bot_params, "trading_probable_min_price", 0.45)),
        probable_max_price       = float(getattr(bot_params, "trading_probable_max_price", 0.85)),
        probable_profit_offset   = float(getattr(bot_params, "trading_probable_profit_offset", 0.45)),
        min_elapsed_for_entry    = float(getattr(bot_params, "trading_min_elapsed_for_entry", 8.0)),
        stake_tier_60            = float(getattr(bot_params, "trading_stake_tier_60", 5.0)),
        stake_tier_70            = float(getattr(bot_params, "trading_stake_tier_70", 10.0)),
        stake_tier_80            = float(getattr(bot_params, "trading_stake_tier_80", 15.0)),
        stake_tier_90            = float(getattr(bot_params, "trading_stake_tier_90", 20.0)),
    )


async def _get_token_price(token_id: str) -> Optional[float]:
    """Obtiene precio live del token desde CLOB."""
    try:
        from markets import get_live_price
        return await get_live_price(token_id)
    except Exception as e:
        logger.warning(f"[TRADING] Error obteniendo precio token {token_id[:12]}: {e}")
        return None


async def _get_book(token_id: str) -> dict:
    """Snapshot completo del order book para decisiones."""
    try:
        from markets import get_full_order_book
        return await get_full_order_book(token_id)
    except Exception as e:
        logger.warning(f"[TRADING] book fail {token_id[:12]}: {e}")
        return {"best_bid": None, "best_ask": None, "bid_total_shares": 0, "ask_total_shares": 0, "pressure": 1.0}


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
        book = await _get_book(token_id)
        best_bid = book.get("best_bid")
        best_ask = book.get("best_ask")
        target = float(pos.get("target_price", 1.0))

        # Real con passive sell: chequear si la orden ya se llenó
        if is_real and pos.get("sell_order_id"):
            if await _order_is_filled(pos["sell_order_id"]):
                # Fill passive al target
                exit_price = target
                pnl = calc_pnl(pos, exit_price)
                updated = tp.close_position(
                    slug=slug, position_id=pos["id"],
                    exit_price=exit_price, pnl_usdc=pnl,
                    exit_reason="TARGET_HIT", is_real=True,
                )
                if updated:
                    _tlog("INFO",
                        f"[TRADING REAL] PASSIVE FILL | {pos['side']} entry={pos['entry_price']:.3f} "
                        f"exit={exit_price:.3f} pnl={pnl:+.2f} | {slug[:30]}"
                    )
                    closed.append(updated)
                continue

        # Si no hay best_bid (mercado ilíquido), fallback a mid/live
        current = best_bid if best_bid is not None else await _get_token_price(token_id)
        if current is None:
            continue

        # Phantom: simular fill passive si best_bid cruzó target (alguien compraría nuestra orden)
        fill_passive_phantom = (not is_real) and best_bid is not None and best_bid >= target

        _sl_before = pos.get("sl_armed_ts")
        reason = should_exit_position(pos, current, minutes_to_close, params)
        # Persistir sl_armed_ts si recién se armó (punto 12)
        if pos.get("sl_armed_ts") and pos.get("sl_armed_ts") != _sl_before:
            try:
                tp.patch_position(slug, pos["id"], {"sl_armed_ts": pos["sl_armed_ts"]}, is_real=is_real)
            except Exception as _e:
                logger.warning(f"[TRADING] patch sl_armed_ts fail: {_e}")
        if fill_passive_phantom and not reason:
            reason = "TARGET_HIT"
        if not reason:
            continue

        if is_real:
            # deadline forzado — cancelar passive sell si existe y salir al bid
            if pos.get("sell_order_id"):
                await _cancel_order(pos["sell_order_id"])
            entry = float(pos.get("entry_price", 0) or 0)
            stake = float(pos.get("stake_usdc", 0) or 0)
            shares_held = (stake / entry) if entry > 0 else 0.0
            ok, fill_price, _ = await _execute_real_sell(pos["token_id"], shares_held, slug)
            if not ok:
                continue
            exit_price = float(fill_price)
        else:
            # Phantom: fill al target si passive cruzó; si no, al bid actual
            exit_price = target if fill_passive_phantom else current

        pnl = calc_pnl(pos, exit_price)

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
            _tlog("INFO",
                f"[TRADING {tag}] CLOSE {reason} | {pos['side']} entry={pos['entry_price']:.3f} "
                f"exit={exit_price:.3f} pnl={pnl:+.2f} | {slug[:30]}"
            )
            closed.append(updated)

    return closed


async def evaluate_and_open(market: dict, is_real: bool, params: TradingParams, bot_params=None) -> Optional[dict]:
    """
    Evalúa si abrir nueva posición en el mercado.
    Retorna posición abierta o None.
    """
    slug = market["slug"]
    interval = int(market.get("interval_minutes", 15))
    end_ts = int(market.get("end_ts", 0))

    # Punto 19C — rechazar datos de precio obsoletos antes de evaluar
    if bot_params is not None:
        max_age = float(getattr(bot_params, "trading_max_price_age_sec", 10.0))
        if max_age > 0:
            import time as _t
            px_ts = market.get("price_ts") or market.get("scanned_ts") or market.get("fetched_ts")
            if px_ts:
                age = _t.time() - float(px_ts)
                if age > max_age:
                    tag0 = "REAL" if is_real else "PHANTOM"
                    _tlog("INFO",
                        f"[TRADING {tag0}] NO-ENTRY | {slug[:30]} | precio obsoleto {age:.1f}s > {max_age:.0f}s"
                    )
                    return None

    all_positions = tp.get_positions(slug, is_real=is_real)

    signal, reason = evaluate_entry_verbose(market, all_positions, params)
    tag = "REAL" if is_real else "PHANTOM"
    if not signal:
        _tlog("INFO",
            f"[TRADING {tag}] NO-ENTRY | {slug[:30]} "
            f"mk={market.get('minutes_to_close',0):.1f}m up={market.get('up_price',0):.3f} "
            f"down={market.get('down_price',0):.3f} | {reason}"
        )
        return None
    _tlog("INFO", f"[TRADING {tag}] ENTRY-CANDIDATE | {slug[:30]} | {reason}")

    # Pre-entry: consultar order book para asegurar liquidez de salida
    book = await _get_book(signal.token_id)
    bid_shares = float(book.get("bid_total_shares") or 0)
    best_ask = book.get("best_ask")
    best_bid = book.get("best_bid")
    pressure = float(book.get("pressure") or 1.0)
    if bid_shares < _MIN_BID_DEPTH_SHARES:
        _tlog("INFO",
            f"[TRADING {tag}] NO-ENTRY | {slug[:30]} | bids depth {bid_shares:.1f} < {_MIN_BID_DEPTH_SHARES} "
            f"(sin compradores para salir)"
        )
        return None
    _tlog("INFO",
        f"[TRADING {tag}] BOOK | ask={best_ask} bid={best_bid} bid_depth={bid_shares:.1f} "
        f"ask_depth={book.get('ask_total_shares'):.1f} pressure={pressure:.2f}"
    )

    # Real: gate de safety + ejecutar BUY CLOB + passive SELL al target
    if is_real:
        if bot_params is not None:
            ok, reason = _check_real_safety(bot_params, signal.stake_usdc)
            if not ok:
                _tlog("WARN", f"[TRADING REAL] BLOQUEADO por safety: {reason}")
                return None
        ok, fill_price, fill_shares, cost = await _execute_real_buy(
            token_id=signal.token_id,
            stake_usdc=signal.stake_usdc,
            side_label=signal.side,
            slug=slug,
        )
        if not ok:
            return None
        real_entry = float(fill_price)
        real_stake = float(cost)
        real_target = round(min(real_entry + params.profit_offset, 0.99), 4)

        # Passive SELL GTC al target — se queda en el libro hasta fill
        sell_oid = await _post_passive_sell_gtc(
            token_id=signal.token_id,
            size=float(fill_shares),
            price=real_target,
            slug=slug,
        )

        pos = tp.open_position(
            slug=slug,
            interval=interval,
            end_ts=end_ts,
            side=signal.side,
            token_id=signal.token_id,
            entry_price=real_entry,
            target_price=real_target,
            stake_usdc=real_stake,
            is_real=True,
            extra={
                "shares": round(float(fill_shares), 4),
                "condition_id": market.get("condition_id", ""),
                "sell_order_id": sell_oid,
            },
        )
        _tlog("INFO",
            f"[TRADING REAL] OPEN {signal.side} @ {real_entry:.3f} target={real_target:.3f} "
            f"stake=${real_stake:.2f} shares={fill_shares:.2f} passive_sell={bool(sell_oid)} | {slug[:30]}"
        )
        return pos

    # Phantom — simula EXACTAMENTE el path real: aplica slippage + cap + min shares (igualdad 1.25)
    raw_ask = float(best_ask) if best_ask is not None else float(signal.entry_price)
    if raw_ask <= 0:
        _tlog("INFO", f"[TRADING PHANTOM] NO-ENTRY | {slug[:30]} | ask invalido ({raw_ask})")
        return None
    if raw_ask >= _REAL_MAX_ENTRY_PRICE:
        _tlog("INFO",
            f"[TRADING PHANTOM] NO-ENTRY | {slug[:30]} | ask {raw_ask:.3f} >= cap {_REAL_MAX_ENTRY_PRICE} (igual a real)"
        )
        return None
    phantom_entry = round(min(raw_ask + _REAL_SLIPPAGE, 0.99), 4)
    phantom_target = round(min(phantom_entry + params.profit_offset, 0.99), 4)
    phantom_stake = signal.stake_usdc
    phantom_shares = round(phantom_stake / phantom_entry, 2)
    if phantom_shares < _REAL_MIN_SHARES:
        phantom_stake = round(_REAL_MIN_SHARES * phantom_entry + 0.01, 2)
        phantom_shares = round(phantom_stake / phantom_entry, 2)

    pos = tp.open_position(
        slug=slug,
        interval=interval,
        end_ts=end_ts,
        side=signal.side,
        token_id=signal.token_id,
        entry_price=phantom_entry,
        target_price=phantom_target,
        stake_usdc=phantom_stake,
        is_real=False,
        extra={"shares": phantom_shares},
    )
    _tlog("INFO",
        f"[TRADING PHANTOM] OPEN {signal.side} @ {phantom_entry:.3f} target={phantom_target:.3f} "
        f"stake=${phantom_stake:.2f} shares={phantom_shares:.2f} | passive sell simulado | {slug[:30]}"
    )
    return pos


async def enrich_open_positions(is_real: bool = False) -> list:
    """
    Devuelve cada posición OPEN con campos extra para UI:
      • current_price: bid actual del token
      • unrealized_pnl: shares * (current - entry)
      • minutes_left: si > 0 mercado vivo; si negativo, segundos desde cierre
      • market_status: 'live' | 'closed_awaiting' | 'closed_winning' | 'closed_losing'
    """
    from markets import get_live_price
    out = []
    now = int(time.time())
    for pos in tp.all_open_positions(is_real=is_real):
        token_id = pos.get("token_id")
        end_ts = int(pos.get("end_ts") or 0)
        try:
            cur = await get_live_price(token_id) if token_id else None
        except Exception:
            cur = None
        cur = float(cur) if cur is not None else None

        entry = float(pos.get("entry_price") or 0)
        stake = float(pos.get("stake_usdc") or 0)
        shares = (stake / entry) if entry > 0 else 0.0
        unreal = round(shares * (cur - entry), 4) if cur is not None else None

        secs_to_close = end_ts - now
        if secs_to_close > 0:
            mstatus = "live"
        else:
            secs_past = -secs_to_close
            if cur is None:
                mstatus = "closed_awaiting"
            elif cur >= 0.95:
                mstatus = "closed_winning"
            elif cur <= 0.05:
                mstatus = "closed_losing"
            else:
                mstatus = "closed_awaiting" if secs_past < 600 else "closed_stale"

        enriched = dict(pos)
        enriched["current_price"]    = cur
        enriched["unrealized_pnl"]   = unreal
        enriched["seconds_to_close"] = secs_to_close
        enriched["market_status"]    = mstatus
        enriched["shares"]           = round(shares, 4)
        out.append(enriched)
    return out


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

        seconds_past_close = now - end_ts
        force_late = seconds_past_close > 600  # 10 min => force-close para no dejar fantasma

        if price is None:
            if force_late:
                # Sin precio y mercado cerrado hace 10 min — asumir LOSS (peor caso)
                price = 0.0
            else:
                continue

        # Decidir outcome binario
        if price >= 0.95:
            status = "RESOLVED_WIN"
            won = True
        elif price <= 0.05:
            status = "RESOLVED_LOSS"
            won = False
        elif force_late:
            # Mercado cerrado hace 10+ min y CLOB ambiguo — cerrar al precio actual como FORCED_EXIT_LATE
            from strategy_trading import calc_pnl as _cp
            exit_price = price
            pnl = _cp(pos, exit_price)
            updated = tp.close_position(
                slug=pos["slug"],
                position_id=pos["id"],
                exit_price=exit_price,
                pnl_usdc=pnl,
                exit_reason="FORCED_EXIT",
                is_real=is_real,
            )
            if updated:
                tag = "REAL" if is_real else "PHANTOM"
                _tlog("WARN",
                    f"[TRADING {tag}] FORCED_EXIT (late {seconds_past_close}s) | "
                    f"{pos['side']} entry={pos['entry_price']:.3f} exit={exit_price:.3f} "
                    f"pnl={pnl:+.2f} | {pos['slug'][:30]}"
                )
                resolved.append(updated)
            continue
        else:
            # Mercado recién cerrado, CLOB aún no resuelve — esperar próximo ciclo
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
            _tlog("INFO",
                f"[TRADING {tag}] {status} | {pos['side']} entry={pos['entry_price']:.3f} "
                f"exit={exit_price:.2f} pnl={pnl:+.2f} | {pos['slug'][:30]}"
            )
            resolved.append(updated)

    # Auto-kill-switch: si es real y tras resolver hay N pérdidas seguidas, activar flag
    if is_real and resolved:
        try:
            from config import bot_params as _bp
            if not bool(getattr(_bp, "trading_real_killed", False)):
                cap = int(getattr(_bp, "trading_real_max_consec_losses", 3))
                consec = tp.real_consecutive_losses()
                if consec >= cap:
                    _bp.trading_real_killed = True
                    _bp.save()
                    _tlog("WARN",
                        f"[TRADING REAL] KILL-SWITCH auto-activado tras resolve: "
                        f"{consec} pérdidas seguidas (cap {cap}) — reset manual en UI"
                    )
        except Exception as _ks_err:
            logger.warning(f"[TRADING REAL] kill-switch auto-check falló: {_ks_err}")

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
    slug = market.get("slug", "?")
    _tlog("INFO",
        f"[TRADING CYCLE] {slug[:30]} | mk_close={market.get('minutes_to_close',0):.1f}m "
        f"up={market.get('up_price',0):.3f} down={market.get('down_price',0):.3f} | "
        f"real_on={real_on} | params: thr={params.entry_threshold:.2f} floor={params.min_entry_price:.2f} "
        f"offset={params.profit_offset:.2f} exit_tmin={params.exit_deadline_min:.1f} "
        f"min_entry={params.min_entry_minutes_left:.1f} one_open={params.one_open_at_a_time}"
    )

    result = {
        "phantom": {"closed": [], "opened": None},
        "real":    {"closed": [], "opened": None},
    }

    # 1. Phantom (siempre activo si trading_mode_enabled)
    result["phantom"]["closed"] = await monitor_and_close(market, is_real=False, params=params)
    result["phantom"]["opened"] = await evaluate_and_open(market, is_real=False, params=params, bot_params=bot_params)

    # 2. Real (solo si habilitado)
    if real_on:
        result["real"]["closed"] = await monitor_and_close(market, is_real=True, params=params)
        result["real"]["opened"] = await evaluate_and_open(market, is_real=True, params=params, bot_params=bot_params)

    return result
