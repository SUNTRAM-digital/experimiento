"""
Loop principal del bot: escanea mercados, evalua oportunidades, ejecuta trades.
"""
import asyncio
import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, Callable
from config import settings, bot_params
from markets import fetch_weather_markets, get_live_price, get_order_book_depth, get_polymarket_positions
from weather import get_forecast_high
from strategy import evaluate_market
from claude_analyst import analyze_opportunity, analyze_portfolio

logger = logging.getLogger("weatherbot")

_MONTHS = ["january","february","march","april","may","june",
           "july","august","september","october","november","december"]

def _date_in_title(target_date: date, title: str) -> bool:
    """Devuelve True si el titulo del mercado menciona el mes+dia de target_date."""
    tl = title.lower()
    return _MONTHS[target_date.month - 1] in tl and str(target_date.day) in tl

STATE_FILE = Path(__file__).parent / "data" / "state.json"
LOGS_FILE  = Path(__file__).parent / "data" / "logs.json"
MAX_LOG_HISTORY = 500


class BotState:
    def __init__(self):
        self.running = False
        self.balance_usdc = 0.0
        self.daily_start_balance = 0.0
        self.daily_loss_usdc = 0.0
        self.daily_date = None
        self.total_trades = 0
        self.total_pnl = 0.0
        self.active_positions: list[dict] = []
        self.trade_history: list[dict] = []
        self.opportunities: list[dict] = []
        self.poly_positions: list[dict] = []         # posiciones reales en Polymarket
        self.open_orders: list[dict] = []            # ordenes pendientes de ejecutar
        self.portfolio_analysis: str = ""            # ultimo analisis de portafolio de Claude
        self.portfolio_recommendations: list[dict] = []  # recomendaciones por posicion
        self.last_portfolio_analysis: Optional[datetime] = None  # timestamp ultimo analisis
        self.last_scan: Optional[str] = None
        self.error_count = 0


state = BotState()
_log_callbacks: list[Callable] = []
_log_history: list[dict] = []
_clob_client = None


def add_log_callback(cb: Callable):
    _log_callbacks.append(cb)


def remove_log_callback(cb: Callable):
    if cb in _log_callbacks:
        _log_callbacks.remove(cb)


def get_log_history() -> list[dict]:
    return list(_log_history)


def _log(level: str, msg: str):
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    entry = {"time": timestamp, "level": level, "msg": msg}
    # Guardar en historial en memoria
    _log_history.append(entry)
    if len(_log_history) > MAX_LOG_HISTORY:
        _log_history.pop(0)
    if level == "ERROR":
        logger.error(msg)
    elif level == "WARN":
        logger.warning(msg)
    else:
        logger.info(msg)
    for cb in list(_log_callbacks):
        try:
            cb(entry)
        except Exception:
            pass
    # Persistir logs en segundo plano (no bloqueante)
    _save_logs_sync()


def _save_logs_sync():
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        LOGS_FILE.write_text(json.dumps(_log_history, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_logs():
    global _log_history
    try:
        if LOGS_FILE.exists():
            data = json.loads(LOGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                _log_history = data[-MAX_LOG_HISTORY:]
    except Exception:
        pass


def _save_state():
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        payload = {
            "balance_usdc": state.balance_usdc,
            "daily_start_balance": state.daily_start_balance,
            "daily_loss_usdc": state.daily_loss_usdc,
            "daily_date": state.daily_date.isoformat() if state.daily_date else None,
            "total_trades": state.total_trades,
            "total_pnl": state.total_pnl,
            "active_positions": state.active_positions,
            "trade_history": state.trade_history,
            "last_scan": state.last_scan,
            "error_count": state.error_count,
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"No se pudo guardar estado: {e}")


def _load_state():
    try:
        if not STATE_FILE.exists():
            return
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.balance_usdc        = float(payload.get("balance_usdc", 0))
        state.daily_start_balance = float(payload.get("daily_start_balance", 0))
        state.daily_loss_usdc     = float(payload.get("daily_loss_usdc", 0))
        state.total_trades        = int(payload.get("total_trades", 0))
        state.total_pnl           = float(payload.get("total_pnl", 0))
        state.active_positions    = payload.get("active_positions", [])
        state.trade_history       = payload.get("trade_history", [])
        state.last_scan           = payload.get("last_scan")
        state.error_count         = int(payload.get("error_count", 0))
        raw_date = payload.get("daily_date")
        if raw_date:
            state.daily_date = date.fromisoformat(raw_date)
        logger.info(f"Estado restaurado: {len(state.active_positions)} posiciones, {len(state.trade_history)} trades")
    except Exception as e:
        logger.warning(f"No se pudo cargar estado previo: {e}")


# Cargar estado e historial de logs al importar el modulo
_load_state()
_load_logs()


def _init_clob_client():
    global _clob_client
    if not settings.poly_private_key or settings.poly_private_key == "0x_tu_private_key_aqui":
        _log("ERROR", "POLY_PRIVATE_KEY no configurada en .env")
        return False
    try:
        from py_clob_client.client import ClobClient
        _clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=settings.poly_private_key,
            chain_id=137,
            signature_type=settings.poly_signature_type,
            funder=settings.poly_wallet_address if settings.poly_wallet_address else None,
        )
        creds = _clob_client.create_or_derive_api_creds()
        _clob_client.set_api_creds(creds)
        _log("INFO", "Polymarket client inicializado correctamente")
        return True
    except Exception as e:
        _log("ERROR", f"Error inicializando cliente Polymarket: {e}")
        return False


def _get_balance() -> float:
    if _clob_client is None:
        return 0.0
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        data = _clob_client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=settings.poly_signature_type)
        )
        return int(data.get("balance", 0)) / 1_000_000
    except Exception as e:
        _log("WARN", f"No se pudo obtener balance: {e}")
        return state.balance_usdc


def _fetch_open_orders() -> list[dict]:
    """Obtiene ordenes abiertas usando el cliente CLOB autenticado."""
    if _clob_client is None:
        return []
    try:
        from py_clob_client.clob_types import OpenOrderParams
        raw = _clob_client.get_orders(OpenOrderParams()) or []
        orders = []
        for o in raw:
            size_matched  = float(o.get("size_matched", 0))
            size_original = float(o.get("original_size", 0))
            size_remaining = size_original - size_matched
            if size_remaining <= 0:
                continue
            price = float(o.get("price", 0))
            orders.append({
                "id":             o.get("id", ""),
                "market":         o.get("market", ""),
                "token_id":       o.get("asset_id", ""),
                "outcome":        o.get("outcome", ""),
                "side":           o.get("side", "BUY"),
                "price":          round(price, 4),
                "size_orig":      round(size_original, 2),
                "size_filled":    round(size_matched, 2),
                "size_remaining": round(size_remaining, 2),
                "cost_usdc":      round(price * size_remaining, 2),
                "created_at":     o.get("created_at", ""),
                "status":         o.get("status", "LIVE"),
            })
        return orders
    except Exception as e:
        _log("WARN", f"No se pudieron obtener ordenes abiertas: {e}")
        return []


def _check_daily_reset():
    today = datetime.now(timezone.utc).date()
    if state.daily_date != today:
        state.daily_date = today
        state.daily_start_balance = state.balance_usdc
        state.daily_loss_usdc = 0.0
        _log("INFO", f"Nuevo dia. Balance inicial: ${state.balance_usdc:.2f}")


def _daily_loss_limit_reached() -> bool:
    if state.daily_start_balance <= 0:
        return False
    # Perdida real = caida de balance EN EFECTIVO (no cuenta posiciones abiertas como perdida)
    # El balance baja cuando compramos, pero ese dinero esta en posiciones, no perdido
    # Solo contamos como perdida lo que cayo por debajo de (inicio - valor_en_posiciones)
    capital_en_posiciones = sum(
        p["cost_usdc"] for p in state.active_positions if p["status"] == "open"
    )
    balance_efectivo_inicial = state.daily_start_balance - capital_en_posiciones
    realized_loss = max(0.0, balance_efectivo_inicial - state.balance_usdc)
    loss_pct = realized_loss / state.daily_start_balance
    state.daily_loss_usdc = realized_loss  # sincronizar para la UI
    return loss_pct >= bot_params.max_daily_loss_pct


def _redeem_position(token_id: str, condition_id: str, size: float, market_title: str) -> bool:
    """
    Redime una posición resuelta. Para posiciones perdidas (precio ≈ 0) las quema a $0
    para limpiarlas del portafolio. Para posiciones ganadas (precio ≈ 1) reclama el USDC.
    """
    if _clob_client is None:
        _log("ERROR", "No hay cliente CLOB — no se puede redimir")
        return False

    # Intentar via py_clob_client si el método existe
    try:
        if hasattr(_clob_client, "redeem_positions"):
            result = _clob_client.redeem_positions(condition_id)
            _log("INFO", f"REDIMIR OK | {market_title[:50]} | {result}")
            return True
    except Exception as e:
        _log("WARN", f"redeem_positions falló ({e}), intentando método alternativo")

    # Intentar via API directa con credenciales del cliente
    try:
        import httpx as _httpx
        headers = {}
        for attr in ("api_creds", "_creds", "creds"):
            creds = getattr(_clob_client, attr, None)
            if creds and hasattr(creds, "api_key"):
                headers = {
                    "POLY_API_KEY":         creds.api_key,
                    "POLY_API_SECRET":      getattr(creds, "api_secret", ""),
                    "POLY_API_PASSPHRASE":  getattr(creds, "api_passphrase", ""),
                }
                break

        if headers:
            resp = _httpx.post(
                "https://clob.polymarket.com/settlement",
                json={"condition_id": condition_id, "asset_id": token_id},
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                _log("INFO", f"REDIMIR OK (API) | {market_title[:50]}")
                return True
            _log("WARN", f"Settlement API respondió {resp.status_code}: {resp.text[:80]}")
    except Exception as e:
        _log("WARN", f"API de redención falló: {e}")

    _log(
        "WARN",
        f"POSICIÓN RESUELTA NO REDIMIDA | {market_title[:50]} | "
        f"Usa polymarket.com/portfolio para reclamar manualmente",
    )
    return False


def _sell_position(token_id: str, size: float, market_title: str, reason: str = "",
                   condition_id: str = "") -> bool:
    """
    Vende shares de una posicion existente en Polymarket al mejor precio disponible.
    Si el precio es ≈ 0 (posición resuelta/perdida), intenta redimir en lugar de vender.
    """
    if _clob_client is None:
        _log("ERROR", "No hay cliente CLOB — no se puede vender")
        return False
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        # Obtener mejor bid actual para venta a mercado
        import httpx
        bid_price = 0.0
        try:
            resp = httpx.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
                timeout=8,
            )
            if resp.status_code == 200:
                bids = resp.json().get("bids", [])
                if bids:
                    bid_price = float(bids[0].get("price", 0))
        except Exception:
            pass

        # Si no hay liquidez → posición resuelta → intentar redimir
        if bid_price <= 0.01:
            _log("WARN", f"Sin liquidez para vender {market_title[:40]} (bid={bid_price}) — intentando redención")
            # Buscar condition_id en poly_positions si no fue pasado
            _cid = condition_id
            if not _cid:
                pos = next((p for p in state.poly_positions if p.get("token_id") == token_id), None)
                if pos:
                    _cid = pos.get("condition_id", "")
            return _redeem_position(token_id, _cid, size, market_title)

        # Minimo 5 shares por orden
        size = max(size, 5.0)

        order = OrderArgs(
            token_id=token_id,
            price=round(bid_price, 4),
            size=round(size, 2),
            side=SELL,
            fee_rate_bps=1000,
        )
        signed = _clob_client.create_order(order)
        response = _clob_client.post_order(signed, OrderType.GTC)

        if response.get("success") or response.get("status") in ("live", "matched"):
            proceeds = size * bid_price
            state.balance_usdc += proceeds
            _log(
                "INFO",
                f"VENTA EJECUTADA | {market_title[:50]} | "
                f"{size:.1f} shares @ {bid_price:.3f} = ${proceeds:.2f} USDC"
                + (f" | Razon: {reason}" if reason else ""),
            )
            _save_state()
            return True
        else:
            _log("WARN", f"Venta rechazada: {response.get('errorMsg', response)}")
            return False

    except Exception as e:
        _log("ERROR", f"Error ejecutando venta: {e}")
        return False


def _execute_trade(opportunity: dict) -> bool:
    if _clob_client is None:
        return False
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        shares = opportunity["shares"]
        price = opportunity["entry_price"]

        # Minimo 5 shares por orden (requisito de Polymarket)
        shares = max(shares, 5.0)

        order = OrderArgs(
            token_id=opportunity["token_id"],
            price=round(price, 4),
            size=round(shares, 2),
            side=BUY,
            fee_rate_bps=1000,  # 10% maker fee en mercados de clima
        )
        signed = _clob_client.create_order(order)
        response = _clob_client.post_order(signed, OrderType.GTC)

        if response.get("success") or response.get("status") in ("live", "matched"):
            cost = shares * price
            state.balance_usdc -= cost
            # No sumamos cost a daily_loss — la perdida real se calcula en _daily_loss_limit_reached
            state.total_trades += 1

            trade = {
                "id": response.get("orderID", ""),
                "time": datetime.now(timezone.utc).isoformat(),
                "market": opportunity["market_title"],
                "side": opportunity["side"],
                "price": price,
                "shares": shares,
                "cost_usdc": round(cost, 2),
                "ev_pct": opportunity["ev_pct"],
                "claude_approved": not opportunity.get("claude_rejected", False),
                "status": "open",
            }
            state.active_positions.append(trade)
            state.trade_history.insert(0, trade)
            if len(state.trade_history) > 100:
                state.trade_history = state.trade_history[:100]

            _log(
                "INFO",
                f"TRADE EJECUTADO | {opportunity['side']} {opportunity['market_title'][:50]} | "
                f"${cost:.2f} @ {price:.3f} | EV: {opportunity['ev_pct']}%",
            )
            _save_state()
            return True
        else:
            _log("WARN", f"Trade rechazado: {response.get('errorMsg', response)}")
            return False

    except Exception as e:
        _log("ERROR", f"Error ejecutando trade: {e}")
        return False


async def _scan_cycle():
    _log("INFO", "Iniciando escaneo de mercados...")
    state.last_scan = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    # Actualizar balance
    balance = await asyncio.get_event_loop().run_in_executor(None, _get_balance)
    if balance > 0:
        state.balance_usdc = balance

    _check_daily_reset()

    # Actualizar posiciones reales y ordenes abiertas desde Polymarket
    if settings.poly_wallet_address:
        positions = await get_polymarket_positions(settings.poly_wallet_address)
        if positions:
            state.poly_positions = positions
            total_value = sum(p["cur_value_usdc"] for p in positions)
            total_pnl   = sum(p["pnl_usdc"] for p in positions)
            _log("INFO", f"Posiciones Polymarket: {len(positions)} activas | Valor: ${total_value:.2f} | P&L: ${total_pnl:+.2f}")
            # Alertar posiciones proximas a cerrar y auto-limpiar resueltas
            for pos in positions:
                if pos["hours_to_close"] is not None and 0 < pos["hours_to_close"] < 6:
                    _log("WARN", f"CIERRE PROXIMO ({pos['hours_to_close']:.1f}h): {pos['market_title'][:55]} | {pos['outcome']} {pos['size']} shares @ ${pos['cur_price']:.3f}")
                # Auto-redimir posiciones resueltas
                if pos.get("redeemable"):
                    cur_price = pos.get("cur_price", 0)
                    if cur_price > 0.95:
                        # Ganamos — redimir para cobrar
                        _log("INFO", f"POSICIÓN GANADA PENDIENTE DE COBRO | {pos['market_title'][:50]} — ${pos['cur_value_usdc']:.2f} USDC")
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda p=pos: _redeem_position(p["token_id"], p.get("condition_id",""), p["size"], p["market_title"]),
                        )
                    elif cur_price < 0.05:
                        # Perdimos — redimir a $0 para limpiar el portafolio
                        _log("WARN", f"POSICIÓN PERDIDA (${pos['pnl_usdc']:.2f}) | {pos['market_title'][:50]} — limpiando portafolio")
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda p=pos: _redeem_position(p["token_id"], p.get("condition_id",""), p["size"], p["market_title"]),
                        )

        open_orders = await asyncio.get_event_loop().run_in_executor(None, _fetch_open_orders)
        state.open_orders = open_orders
        if open_orders:
            _log("INFO", f"Ordenes abiertas pendientes: {len(open_orders)}")

        # Analisis de portafolio con Claude (max una vez cada 12 horas)
        _portfolio_analysis_interval_h = 12
        now_utc = datetime.now(timezone.utc)
        _hours_since_analysis = (
            (now_utc - state.last_portfolio_analysis).total_seconds() / 3600
            if state.last_portfolio_analysis else 999
        )
        if state.poly_positions and _hours_since_analysis >= _portfolio_analysis_interval_h:
            _log("INFO", f"Claude analizando portafolio ({len(state.poly_positions)} posiciones)... "
                 f"(proximo en {_portfolio_analysis_interval_h}h)")
            port_analysis = await analyze_portfolio(state.poly_positions, state.balance_usdc)
            if not port_analysis["skipped"]:
                state.portfolio_analysis = port_analysis["analysis"]
                state.portfolio_recommendations = port_analysis.get("recommendations", [])
                state.last_portfolio_analysis = datetime.now(timezone.utc)
                for line in port_analysis["analysis"].splitlines():
                    if line.strip():
                        _log("INFO", f"[PORTAFOLIO] {line}")

                # Ejecutar ventas recomendadas por Claude
                for rec in port_analysis.get("recommendations", []):
                    if rec["action"] == "SALIR":
                        _log(
                            "WARN",
                            f"Claude recomienda SALIR de: {rec['title'][:50]} | {rec['reason']}",
                        )
                        success = await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda r=rec: _sell_position(
                                r["token_id"], r["size"], r["title"], r["reason"]
                            ),
                        )
                        if success:
                            await asyncio.sleep(1)
            else:
                if port_analysis["summary"]:
                    _log("INFO", f"[PORTAFOLIO] {port_analysis['summary']}")

    if _daily_loss_limit_reached():
        _log("WARN", f"Limite de perdida diaria alcanzado ({bot_params.max_daily_loss_pct*100:.0f}%). Bot pausado.")
        return

    # Obtener mercados activos
    markets = await fetch_weather_markets()
    _log("INFO", f"Mercados encontrados: {len(markets)}")

    opportunities = []
    for market in markets:
        if not market.get("target_date"):
            continue

        # Obtener forecast
        forecast = await get_forecast_high(market["station"], market["target_date"])
        if not forecast:
            _log("WARN", f"Sin forecast para {market['station']} en {market['target_date']}")
            continue

        # Precio en vivo (mas preciso que el de Gamma)
        live_price = await get_live_price(market["yes_token_id"])
        if live_price:
            market["yes_price"] = live_price

        # Evaluar oportunidad
        opportunity = evaluate_market(market, forecast, state.balance_usdc)
        if opportunity:
            opportunities.append(opportunity)
            _log(
                "INFO",
                f"OPORTUNIDAD | {opportunity['side']} {market['city'].title()} "
                f"[{market['temp_low']}-{market['temp_high']}°F] | "
                f"Forecast: {forecast['high_f']:.1f}°F | "
                f"Mercado: {market['yes_price']:.2f} | "
                f"Nuestra prob: {opportunity['our_prob']:.2%} | "
                f"EV: {opportunity['ev_pct']}%",
            )

    # Ordenar por EV descendente
    opportunities.sort(key=lambda x: x["ev_pct"], reverse=True)
    state.opportunities = opportunities

    if not opportunities:
        _log("INFO", "Sin oportunidades con edge suficiente en este ciclo.")
        return

    # Ejecutar el mejor trade si hay balance suficiente
    for opp in opportunities:
        if state.balance_usdc < bot_params.min_position_usdc:
            _log("WARN", "Balance insuficiente para abrir nuevas posiciones.")
            break

        if _daily_loss_limit_reached():
            _log("WARN", "Limite de perdida diaria alcanzado. Deteniendo trades.")
            break

        # Verificar que no tengamos ya posicion en este mercado exacto (condition_id)
        if any(p.get("condition_id") == opp.get("condition_id") for p in state.poly_positions):
            _log("INFO", f"Ya tenemos posicion en este mercado: {opp['market_title'][:50]}")
            continue

        # Verificar que no tengamos NINGUNA apuesta para la misma ciudad+fecha
        # (aunque sea un bucket distinto — son mutuamente excluyentes, apostamos dos veces a perder)
        if opp.get("target_date") and any(
            opp["city"] in p.get("market_title", "").lower()
            and _date_in_title(opp["target_date"], p.get("market_title", ""))
            for p in state.poly_positions
        ):
            _log(
                "WARN",
                f"Ya tenemos una apuesta en {opp['city'].title()} para esa fecha "
                f"— omitiendo para no abrir posiciones conflictivas en el mismo mercado.",
            )
            continue

        # Obtener profundidad del order book (dato adicional para Claude)
        book_side = "ask" if opp["side"] == "YES" else "bid"
        book_depth = await get_order_book_depth(
            opp["token_id"], book_side, opp["entry_price"]
        )
        opp["book_depth"] = book_depth

        # Verificar profundidad minima
        if book_depth["depth_usdc"] < bot_params.min_book_depth_usdc:
            _log(
                "WARN",
                f"Profundidad insuficiente en libro: ${book_depth['depth_usdc']:.1f} USDC "
                f"(minimo ${bot_params.min_book_depth_usdc}) — descartando.",
            )
            continue

        # Consultar a Claude antes de ejecutar
        _log("INFO", f"Consultando a Claude sobre: {opp['market_title'][:55]}...")
        analysis = await analyze_opportunity(opp, state.balance_usdc, state.poly_positions or [])

        if analysis["skipped"] or not analysis["approved"]:
            if analysis["skipped"]:
                _log("WARN", "Claude no configurado — trade BLOQUEADO. Configura ANTHROPIC_API_KEY en .env")
            else:
                _log(
                    "WARN",
                    f"Claude RECHAZA [{analysis['confidence']}]: {analysis['reason']}",
                )
            opp["claude_rejected"] = True
            opp["claude_reason"] = analysis["reason"]
            continue

        _log(
            "INFO",
            f"Claude APRUEBA [{analysis['confidence']}] "
            f"[Riesgo ejecucion: {analysis.get('execution_risk','N/A')}]: "
            f"{analysis['reason']}",
        )

        success = await asyncio.get_event_loop().run_in_executor(None, _execute_trade, opp)
        if success:
            await asyncio.sleep(2)  # Pausa entre trades

    # Guardar estado al final de cada ciclo
    _save_state()


def _sync_account_from_polymarket():
    """
    Consulta Polymarket para sincronizar posiciones abiertas e historial de trades.
    Complementa el estado local con datos reales de la cuenta.
    """
    if _clob_client is None:
        return
    try:
        from py_clob_client.clob_types import OpenOrderParams, TradeParams

        # --- Ordenes abiertas (posiciones vigentes) ---
        try:
            open_orders = _clob_client.get_orders(OpenOrderParams())
            if open_orders:
                existing_ids = {p.get("id") for p in state.active_positions}
                added = 0
                for o in open_orders:
                    order_id = o.get("id") or o.get("orderID", "")
                    if order_id in existing_ids:
                        continue
                    trade = {
                        "id": order_id,
                        "time": o.get("createdAt", datetime.now(timezone.utc).isoformat()),
                        "market": o.get("asset_id", o.get("tokenID", "")),
                        "side": o.get("side", "?"),
                        "price": float(o.get("price", 0)),
                        "shares": float(o.get("originalSize", o.get("size", 0))),
                        "cost_usdc": round(float(o.get("price", 0)) * float(o.get("originalSize", o.get("size", 0))), 2),
                        "ev_pct": 0,
                        "claude_approved": True,
                        "status": "open",
                        "source": "polymarket_sync",
                    }
                    state.active_positions.append(trade)
                    added += 1
                if added:
                    _log("INFO", f"Sincronizadas {added} posiciones abiertas desde Polymarket.")
        except Exception as e:
            _log("WARN", f"No se pudieron obtener ordenes abiertas: {e}")

        # --- Historial de trades ---
        try:
            trades_resp = _clob_client.get_trades(TradeParams(maker_address=settings.poly_wallet_address))
            if trades_resp:
                existing_ids = {t.get("id") for t in state.trade_history}
                added = 0
                for t in trades_resp[:100]:
                    trade_id = t.get("id", "")
                    if trade_id in existing_ids:
                        continue
                    size = float(t.get("size", 0))
                    price = float(t.get("price", 0))
                    trade = {
                        "id": trade_id,
                        "time": t.get("createdAt", ""),
                        "market": t.get("market", t.get("asset_id", "")),
                        "side": t.get("side", "?"),
                        "price": price,
                        "shares": size,
                        "cost_usdc": round(price * size, 2),
                        "ev_pct": 0,
                        "claude_approved": True,
                        "status": "filled",
                        "source": "polymarket_sync",
                    }
                    state.trade_history.append(trade)
                    added += 1
                # Ordenar por tiempo descendente
                state.trade_history.sort(key=lambda x: x.get("time", ""), reverse=True)
                state.trade_history = state.trade_history[:100]
                if added:
                    _log("INFO", f"Sincronizados {added} trades historicos desde Polymarket.")
        except Exception as e:
            _log("WARN", f"No se pudo obtener historial de trades: {e}")

        _save_state()

    except Exception as e:
        _log("WARN", f"Error en sincronizacion con Polymarket: {e}")


async def run_bot():
    """Loop principal del bot."""
    _log("INFO", "Bot iniciado. Inicializando cliente Polymarket...")

    ok = await asyncio.get_event_loop().run_in_executor(None, _init_clob_client)
    if not ok:
        state.running = False
        return

    balance = await asyncio.get_event_loop().run_in_executor(None, _get_balance)
    state.balance_usdc = balance
    state.daily_start_balance = balance
    _log("INFO", f"Balance inicial: ${balance:.2f} USDC")

    # Sincronizar posiciones y trades existentes desde Polymarket
    _log("INFO", "Sincronizando cuenta con Polymarket...")
    await asyncio.get_event_loop().run_in_executor(None, _sync_account_from_polymarket)

    while state.running:
        try:
            await _scan_cycle()
        except Exception as e:
            state.error_count += 1
            _log("ERROR", f"Error en ciclo de escaneo: {e}")

        if not state.running:
            break

        interval = bot_params.scan_interval_minutes * 60
        _log("INFO", f"Proximo escaneo en {bot_params.scan_interval_minutes} minutos.")

        # Esperar con capacidad de interrumpir
        for _ in range(interval):
            if not state.running:
                break
            await asyncio.sleep(1)

    _log("INFO", "Bot detenido.")


def start():
    if state.running:
        return
    state.running = True
    asyncio.create_task(run_bot())


def stop():
    state.running = False
    _log("INFO", "Senalizando detencion del bot...")
