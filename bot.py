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
from weather_ensemble import get_ensemble_high
from strategy import evaluate_market
from claude_analyst import analyze_opportunity, analyze_portfolio, analyze_updown_opportunity
from price_feed import (
    get_btc_price, get_btc_volatility, get_btc_ta,
    get_btc_market_data_cmc, vol_interval_for_horizon, tv_interval_for_horizon,
)
from markets_btc import fetch_btc_markets
from strategy_btc import evaluate_btc_market
from markets_updown import fetch_updown_market
from strategy_updown import evaluate_updown_market
from exit_manager import evaluate_exit_batch
from rules_parser import parse_market_rules, detect_boundary_zone, format_rules_for_analyst
from category_tracker import get_category_status, record_trade_result, get_all_stats
from strategy_nearzero import evaluate_nearzero, scan_nearzero_opportunities
from wallet_tracker import wallet_tracker
from risk_manager import risk_manager
from performance_monitor import perf
from telonex_data import telonex_data

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
        # Bitcoin
        self.btc_price: Optional[float] = None
        self.btc_opportunities: list[dict] = []
        self.btc_ta: dict = {}
        self.btc_cmc: dict = {}
        # Auto-trading BTC
        self.btc_auto_mode: bool = False
        self.btc_scan_interval_minutes: int = 5
        self.btc_next_scan_in: int = 0   # segundos hasta siguiente escaneo
        # Auto-trade global (omite aprobación de Claude)
        self.auto_trade_mode: bool = False
        # Presupuestos por tipo (calculados al inicio de cada ciclo)
        self.budget_weather: float = 0.0
        self.budget_btc: float = 0.0
        self.budget_updown: float = 0.0
        # Capital desplegado en posiciones abiertas por tipo
        self.deployed_weather: float = 0.0
        self.deployed_btc: float = 0.0
        self.deployed_updown: float = 0.0
        # Capital disponible por tipo (= budget - deployed)
        self.available_weather: float = 0.0
        self.available_btc: float = 0.0
        self.available_updown: float = 0.0
        # Proporciones de asignación — viven en bot_params (con persistencia)
        # BTC Up/Down (5m y 15m)
        self.updown_5m_consecutive_losses: int = 0
        self.updown_15m_consecutive_losses: int = 0
        self.updown_5m_stopped: bool = False
        self.updown_15m_stopped: bool = False
        self.updown_last_market_5m: Optional[dict] = None   # último mercado visto
        self.updown_last_market_15m: Optional[dict] = None
        self.updown_last_opp_5m: Optional[dict] = None      # última oportunidad evaluada
        self.updown_last_opp_15m: Optional[dict] = None
        self.updown_last_trade_5m: Optional[dict] = None    # último trade ejecutado
        self.updown_last_trade_15m: Optional[dict] = None
        self.updown_ta_5m: dict = {}                        # último TA usado
        self.updown_ta_15m: dict = {}
        self.updown_recent_trades: list[dict] = []          # historial reciente (últimos 30)
        # Capital Velocity (Fase 3): total_volume_traded / avg_capital_deployed
        # Target: >20x (indica uso eficiente del capital via posiciones de corta duracion)
        self.capital_velocity: float = 0.0
        self.total_volume_traded: float = 0.0             # USDC total comprado (acumulado)
        self.avg_capital_deployed: float = 0.0            # promedio movil del capital desplegado
        # Sistema de buckets (Fase 12)
        self.cash_free: float = 0.0                       # balance - suma de buckets asignados


state = BotState()
_log_callbacks: list[Callable] = []
_log_history: list[dict] = []
_clob_client = None


# ── Helpers de buckets de capital (Fase 12) ───────────────────────────────────

def _bucket_attr(bucket_id: str) -> str:
    """Retorna el nombre del atributo en bot_params para el bucket dado."""
    return {
        "weather":    "bucket_weather_usdc",
        "btc":        "bucket_btc_usdc",
        "updown_5m":  "bucket_updown_5m_usdc",
        "updown_15m": "bucket_updown_15m_usdc",
    }.get(bucket_id, "")


def _deduct_from_bucket(bucket_id: str, amount: float):
    """Resta amount del bucket; persiste. No-op si sistema de buckets inactivo."""
    if not bot_params.betting_pool_usdc or not bucket_id:
        return
    attr = _bucket_attr(bucket_id)
    if not attr:
        return
    current = getattr(bot_params, attr, 0.0)
    setattr(bot_params, attr, round(max(0.0, current - amount), 4))
    bot_params.save()


def _return_stake_to_bucket(bucket_id: str, amount: float):
    """Devuelve stake al bucket tras ganar una apuesta; persiste."""
    if not bot_params.betting_pool_usdc or not bucket_id or not amount:
        return
    attr = _bucket_attr(bucket_id)
    if not attr:
        return
    current = getattr(bot_params, attr, 0.0)
    setattr(bot_params, attr, round(current + amount, 4))
    bot_params.save()
    _log("INFO", f"Capital | Stake ${amount:.2f} devuelto a bucket '{bucket_id}' → saldo ${getattr(bot_params, attr):.2f}")


def _bucket_id_from_opportunity(opp: dict) -> str:
    """Determina el bucket_id a partir del diccionario de oportunidad."""
    asset = opp.get("asset", "WEATHER")
    interval = opp.get("interval_minutes")
    if asset == "BTC_UPDOWN":
        return "updown_5m" if interval == 5 else "updown_15m"
    if asset == "BTC":
        return "btc"
    return "weather"


def _build_capital_context(bucket_id: str = "") -> dict:
    """
    Construye el dict de capital para pasarle a Claude en sus análisis.
    Incluye estado del pool, buckets y cash libre.
    """
    bp = bot_params
    bucket_sum = (bp.bucket_weather_usdc + bp.bucket_btc_usdc +
                  bp.bucket_updown_5m_usdc + bp.bucket_updown_15m_usdc)
    cash_free = round(max(0.0, state.balance_usdc - bucket_sum), 2)
    bucket_usdc_map = {
        "weather":    bp.bucket_weather_usdc,
        "btc":        bp.bucket_btc_usdc,
        "updown_5m":  bp.bucket_updown_5m_usdc,
        "updown_15m": bp.bucket_updown_15m_usdc,
    }
    return {
        "pool_usdc":       bp.betting_pool_usdc,
        "cash_free":       cash_free,
        "bucket_id":       bucket_id,
        "bucket_usdc":     bucket_usdc_map.get(bucket_id, 0.0),
        "bucket_weather":  bp.bucket_weather_usdc,
        "bucket_btc":      bp.bucket_btc_usdc,
        "bucket_updown_5m":  bp.bucket_updown_5m_usdc,
        "bucket_updown_15m": bp.bucket_updown_15m_usdc,
    }


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
            "updown_recent_trades": state.updown_recent_trades,
            "last_scan": state.last_scan,
            "error_count": state.error_count,
            "total_volume_traded": state.total_volume_traded,
            "avg_capital_deployed": state.avg_capital_deployed,
            "capital_velocity": state.capital_velocity,
            # Racha UpDown — persistir para no perder el conteo entre reinicios
            "updown_5m_consecutive_losses":  state.updown_5m_consecutive_losses,
            "updown_15m_consecutive_losses": state.updown_15m_consecutive_losses,
            "updown_5m_stopped":             state.updown_5m_stopped,
            "updown_15m_stopped":            state.updown_15m_stopped,
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
        state.updown_recent_trades = payload.get("updown_recent_trades", [])
        state.last_scan           = payload.get("last_scan")
        state.error_count         = int(payload.get("error_count", 0))
        state.total_volume_traded  = float(payload.get("total_volume_traded", 0))
        state.avg_capital_deployed = float(payload.get("avg_capital_deployed", 0))
        state.capital_velocity     = float(payload.get("capital_velocity", 0))
        state.updown_5m_consecutive_losses  = int(payload.get("updown_5m_consecutive_losses", 0))
        state.updown_15m_consecutive_losses = int(payload.get("updown_15m_consecutive_losses", 0))
        state.updown_5m_stopped  = bool(payload.get("updown_5m_stopped", False))
        state.updown_15m_stopped = bool(payload.get("updown_15m_stopped", False))
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
    Redimir posiciones en Polymarket requiere una llamada on-chain al contrato CTF
    (Conditional Token Framework), que no está soportada por py_clob_client.
    Polymarket generalmente auto-redime en ~24h. Si no, el usuario puede hacerlo
    manualmente en polymarket.com/portfolio.
    """
    _log(
        "INFO",
        f"POSICIÓN RESUELTA | {market_title[:55]} | "
        f"Polymarket auto-redimirá en ~24h. Si no, ve a polymarket.com/portfolio",
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
        price  = opportunity["entry_price"]
        market_title = opportunity.get("market_title") or opportunity.get("title", "Unknown")

        # Seguridad: el costo real (shares × price) nunca debe superar el max configurado
        is_updown = opportunity.get("asset") == "BTC_UPDOWN"
        if is_updown:
            max_usdc = max(1.0, round(float(bot_params.updown_max_usdc), 2))
            actual_cost = round(shares * price, 2)
            if actual_cost > max_usdc + 0.05:
                shares = round(max_usdc / price, 2)
                _log("WARN", f"UpDown | Shares recortados para respetar max ${max_usdc} (costo era ${actual_cost:.2f} → ahora ${round(shares*price,2):.2f})")

        order = OrderArgs(
            token_id=opportunity["token_id"],
            price=round(price, 4),
            size=round(shares, 2),
            side=BUY,
            fee_rate_bps=1000,
        )
        signed = _clob_client.create_order(order)
        # Todos los trades usan GTC. Para UpDown, las órdenes abiertas se cancelan
        # automáticamente al inicio del siguiente ciclo (_cancel_stale_updown_orders).
        response = _clob_client.post_order(signed, OrderType.GTC)

        if response.get("success") or response.get("status") in ("live", "matched"):
            cost = shares * price
            state.balance_usdc -= cost
            # No sumamos cost a daily_loss — la perdida real se calcula en _daily_loss_limit_reached
            state.total_trades += 1
            # Capital Velocity: acumular volumen total tradeado
            state.total_volume_traded += cost

            bucket_id = _bucket_id_from_opportunity(opportunity)
            # Descontar del bucket correspondiente
            _deduct_from_bucket(bucket_id, cost)

            trade = {
                "id": response.get("orderID", ""),
                "time": datetime.now(timezone.utc).isoformat(),
                "market": market_title,
                "side": opportunity["side"],
                "price": price,
                "shares": shares,
                "cost_usdc": round(cost, 2),
                "ev_pct": opportunity["ev_pct"],
                "claude_approved": not opportunity.get("claude_rejected", False),
                "status": "open",
                # Campos extra para UpDown tracking
                "asset": opportunity.get("asset", "WEATHER"),
                "token_id": opportunity.get("token_id", ""),
                "interval_minutes": opportunity.get("interval_minutes"),
                # URL de Polymarket para el botón "Ver en Polymarket"
                "poly_url": opportunity.get("poly_url", ""),
                # Bucket de capital (Fase 12)
                "bucket_id": bucket_id,
            }
            state.active_positions.append(trade)
            state.trade_history.insert(0, trade)
            if len(state.trade_history) > 100:
                state.trade_history = state.trade_history[:100]

            _log(
                "INFO",
                f"TRADE EJECUTADO | {opportunity['side']} {market_title[:50]} | "
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
    _cycle_t0 = __import__("time").perf_counter()
    _log("INFO", "Iniciando escaneo de mercados...")
    state.last_scan = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    # Actualizar balance
    balance = await asyncio.get_event_loop().run_in_executor(None, _get_balance)
    if balance > 0:
        state.balance_usdc = balance

    # ── Asignación de capital ─────────────────────────────────────────────────
    _dep = _calc_deployed_by_type()
    state.deployed_weather = round(_dep.get("WEATHER",    0.0), 2)
    state.deployed_btc     = round(_dep.get("BTC",        0.0), 2)
    state.deployed_updown  = round(_dep.get("BTC_UPDOWN", 0.0), 2)
    _total_deployed = state.deployed_weather + state.deployed_btc + state.deployed_updown

    if bot_params.betting_pool_usdc > 0:
        # ── Sistema de buckets (Fase 12) ──────────────────────────────────────
        # Cada mercado opera con su bucket independiente; presupuesto = saldo del bucket
        _bucket_sum = (bot_params.bucket_weather_usdc + bot_params.bucket_btc_usdc +
                       bot_params.bucket_updown_5m_usdc + bot_params.bucket_updown_15m_usdc)
        state.cash_free = round(max(0.0, state.balance_usdc - _bucket_sum), 2)
        state.budget_weather  = round(bot_params.bucket_weather_usdc, 2)
        state.budget_btc      = round(bot_params.bucket_btc_usdc, 2)
        state.budget_updown   = round(bot_params.bucket_updown_5m_usdc + bot_params.bucket_updown_15m_usdc, 2)
        state.available_weather = state.budget_weather
        state.available_btc     = state.budget_btc
        state.available_updown  = state.budget_updown
    else:
        # ── Sistema legacy: asignación por % del total de la cuenta ──────────
        state.cash_free = 0.0
        _total = state.balance_usdc + _total_deployed
        state.budget_weather = round(_total * bot_params.alloc_weather_pct, 2)
        state.budget_btc     = round(_total * bot_params.alloc_btc_pct,     2)
        state.budget_updown  = round(_total * bot_params.alloc_updown_pct,  2)
        _cash = state.balance_usdc
        _hw = max(0.0, state.budget_weather - state.deployed_weather)
        _hb = max(0.0, state.budget_btc     - state.deployed_btc)
        _hu = max(0.0, state.budget_updown  - state.deployed_updown)
        _total_h = _hw + _hb + _hu
        _ratio = min(1.0, _cash / _total_h) if _total_h > 0 else 0.0
        state.available_weather = round(_hw * _ratio, 2)
        state.available_btc     = round(_hb * _ratio, 2)
        state.available_updown  = round(_hu * _ratio, 2)

    # ── Capital Velocity (Fase 3) ─────────────────────────────────────────────
    # Calcula cuantas veces hemos rotado el capital (target: >20x)
    # avg_capital_deployed se actualiza como promedio movil exponencial (alpha=0.1)
    current_deployed = _total_deployed
    if state.avg_capital_deployed == 0.0 and current_deployed > 0:
        state.avg_capital_deployed = current_deployed
    elif current_deployed > 0:
        state.avg_capital_deployed = 0.9 * state.avg_capital_deployed + 0.1 * current_deployed
    if state.avg_capital_deployed > 0:
        state.capital_velocity = round(state.total_volume_traded / state.avg_capital_deployed, 2)

    if bot_params.betting_pool_usdc > 0:
        _log(
            "INFO",
            f"Capital (buckets) — Weather: ${state.budget_weather:.2f} | "
            f"BTC: ${state.budget_btc:.2f} | "
            f"UpDown: ${state.budget_updown:.2f} (5m:${bot_params.bucket_updown_5m_usdc:.2f} 15m:${bot_params.bucket_updown_15m_usdc:.2f}) | "
            f"CashLibre: ${state.cash_free:.2f} | Velocity: {state.capital_velocity:.1f}x",
        )
    else:
        _log(
            "INFO",
            f"Capital — Weather: ${state.budget_weather:.2f} (dep ${state.deployed_weather:.2f} / avail ${state.available_weather:.2f}) | "
            f"BTC: ${state.budget_btc:.2f} (dep ${state.deployed_btc:.2f} / avail ${state.available_btc:.2f}) | "
            f"UpDown: ${state.budget_updown:.2f} (dep ${state.deployed_updown:.2f} / avail ${state.available_updown:.2f}) | "
            f"Velocity: {state.capital_velocity:.1f}x (target >20x)",
        )

    _check_daily_reset()

    # ── Fase 6: Risk Manager update ───────────────────────────────────────────
    _total_account = state.balance_usdc + _total_deployed
    risk_manager.update(_total_account, state.poly_positions)
    _log("INFO", risk_manager.status_summary(_total_account))
    if risk_manager.circuit_breaker_active:
        _log("WARN", f"RISK | {risk_manager.circuit_breaker_reason}")
        _log("WARN", "RISK | Bot pausado por circuit breaker — no se ejecutaran trades.")
        _save_state()
        return

    # Actualizar posiciones reales y ordenes abiertas desde Polymarket
    if settings.poly_wallet_address:
        positions = await get_polymarket_positions(settings.poly_wallet_address)
        if positions is not None:
            state.poly_positions = positions
            if positions:
                total_value = sum(p["cur_value_usdc"] for p in positions)
                total_pnl   = sum(p["pnl_usdc"] for p in positions)
                _log("INFO", f"Posiciones Polymarket: {len(positions)} activas | Valor: ${total_value:.2f} | P&L: ${total_pnl:+.2f}")
            else:
                _log("INFO", "Posiciones Polymarket: sin posiciones activas")
            # Alertar posiciones proximas a cerrar y auto-limpiar resueltas
            for pos in positions:
                if pos["hours_to_close"] is not None and 0 < pos["hours_to_close"] < 6:
                    _log("WARN", f"CIERRE PROXIMO ({pos['hours_to_close']:.1f}h): {pos['market_title'][:55]} | {pos['outcome']} {pos['size']} shares @ ${pos['cur_price']:.3f}")
                # Auto-redimir posiciones resueltas
                if pos.get("redeemable"):
                    cur_price = pos.get("cur_price", 0)
                    token_id  = pos.get("token_id", "")
                    # Actualizar racha UpDown si aplica (via precio posición redeemable)
                    # Solo si aún no fue resuelto por _resolve_pending_updown_outcomes
                    _ud_interval = None
                    if token_id in _updown_pending_outcomes:
                        # Pendiente en memoria → resolver aquí y sacar del dict
                        pending = _updown_pending_outcomes.pop(token_id)
                        _ud_interval = pending["interval"] if isinstance(pending, dict) else pending
                    else:
                        # Posible restart: buscar en trade_history por token_id
                        # Solo actuar si el trade aún no tiene resultado (evita doble-conteo)
                        for _th in state.trade_history:
                            if _th.get("token_id") == token_id:
                                if _th.get("result") is None:
                                    _ud_interval = _th.get("interval_minutes")
                                # Si ya tiene resultado, _resolve_pending ya lo procesó → no tocar
                                break
                    if cur_price > 0.95:
                        # Ganamos — redimir para cobrar
                        _log("INFO", f"POSICIÓN GANADA PENDIENTE DE COBRO | {pos['market_title'][:50]} — ${pos['cur_value_usdc']:.2f} USDC")
                        # Patron 2: registrar resultado para win rate tracking
                        _mtitle = pos.get("market_title","").lower(); _cat = "updown" if any(k in _mtitle for k in ("updown","up or down","up/down","btc")) or "btc" in pos.get("market_type","") else "weather"
                        record_trade_result(_cat, won=True, pnl_usdc=pos.get("pnl_usdc", 0))
                        risk_manager.record_trade_result(won=True)   # Fase 6: auto-sizing streak
                        # Actualizar trade_history con resultado WIN + devolver stake al bucket
                        # IMPORTANTE: hacer esto ANTES de llamar _update_updown_loss_streak,
                        # que también puede marcar trade_history y dejaría result != None
                        _tk = pos.get("token_id", "")
                        for _th in state.trade_history:
                            if _th.get("token_id") == _tk and _th.get("result") is None:
                                _th["result"] = "WIN"
                                # Devolver stake original al bucket (la ganancia queda en balance)
                                _return_stake_to_bucket(_th.get("bucket_id", ""), _th.get("cost_usdc", 0.0))
                                break
                        if _ud_interval:
                            _update_updown_loss_streak(_ud_interval, True, None)
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda p=pos: _redeem_position(p["token_id"], p.get("condition_id",""), p["size"], p["market_title"]),
                        )
                    elif cur_price < 0.05:
                        # Perdimos — redimir a $0 para limpiar el portafolio
                        _log("WARN", f"POSICIÓN PERDIDA (${pos['pnl_usdc']:.2f}) | {pos['market_title'][:50]} — limpiando portafolio")
                        # Patron 2: registrar resultado para win rate tracking
                        _mtitle = pos.get("market_title","").lower(); _cat = "updown" if any(k in _mtitle for k in ("updown","up or down","up/down","btc")) or "btc" in pos.get("market_type","") else "weather"
                        record_trade_result(_cat, won=False, pnl_usdc=pos.get("pnl_usdc", 0))
                        risk_manager.record_trade_result(won=False)  # Fase 6: resetear streak
                        # Actualizar trade_history con resultado LOSS antes del streak
                        _tk = pos.get("token_id", "")
                        for _th in state.trade_history:
                            if _th.get("token_id") == _tk and _th.get("result") is None:
                                _th["result"] = "LOSS"
                                break
                        if _ud_interval:
                            _update_updown_loss_streak(_ud_interval, False, None)
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda p=pos: _redeem_position(p["token_id"], p.get("condition_id",""), p["size"], p["market_title"]),
                        )

        open_orders = await asyncio.get_event_loop().run_in_executor(None, _fetch_open_orders)
        state.open_orders = open_orders
        if open_orders:
            _log("INFO", f"Ordenes abiertas pendientes: {len(open_orders)}")

        # ── Patrones 3+5: Exit Manager (Disposition Coefficient + Swing) ────────
        # Revisar en cada ciclo si alguna posicion debe cerrarse por edge agotado
        # o stop loss, sin esperar al settlement.
        if state.poly_positions:
            # Obtener precios actuales de cada token
            current_prices = {}
            for pos in state.poly_positions:
                tid = pos.get("token_id")
                if tid:
                    lp = await get_live_price(tid)
                    if lp:
                        current_prices[tid] = lp

            # Usar el precio actual como proxy de la prob estimada (sin forecast disponible aqui)
            # Para una estimacion mas precisa, el exit_manager usaria el ensemble,
            # pero para el monitoreo continuo el precio de mercado es suficiente
            estimated_probs = {
                pos.get("condition_id", ""): pos.get("cur_price", pos.get("avg_price", 0.5))
                for pos in state.poly_positions
            }

            exits = evaluate_exit_batch(state.poly_positions, current_prices, estimated_probs)
            for exit_pos in exits:
                urgency = exit_pos.get("exit_urgency", "low")
                reason  = exit_pos.get("exit_reason", "")
                details = exit_pos.get("exit_details", "")
                pnl_pct = exit_pos.get("exit_pnl_pct", 0)

                log_level = "WARN" if urgency == "high" else "INFO"
                _log(log_level,
                     f"[EXIT P{3 if 'stop' in reason else 5}] "
                     f"{exit_pos.get('market_title','')[:50]} | "
                     f"Razon: {reason} | P&L: {pnl_pct:+.1%} | {details}"
                )

                # Solo ejecutar salida automatica si auto_trade_mode activo
                # En modo normal, la salida la aprueba Claude via analisis de portafolio
                if state.auto_trade_mode and urgency in ("high", "medium"):
                    _log("WARN", f"[EXIT AUTO] Cerrando posicion: {exit_pos.get('market_title','')[:50]}")
                    tid = exit_pos.get("token_id")
                    size = exit_pos.get("size", 0)
                    if tid and size:
                        await asyncio.get_event_loop().run_in_executor(
                            None,
                            lambda e=exit_pos: _sell_position(
                                e["token_id"], e["size"],
                                e.get("market_title",""), details
                            ),
                        )

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
            port_analysis = await analyze_portfolio(state.poly_positions, state.balance_usdc, _build_capital_context())
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

    # ── Patron 2: Verificar estado de categorias (Win Rate Decay) ────────────
    cat_stats = get_all_stats()
    for cat_name, cat_info in cat_stats.items():
        if cat_info.get("total_trades", 0) > 0:
            if not cat_info["allowed"]:
                _log("WARN", f"[P2] {cat_info['message']}")
            elif cat_info.get("warning"):
                _log("INFO", f"[P2] {cat_info['message']}")

    # ── Patron 2: Bloquear weather si categoria sin edge ─────────────────────
    weather_cat = cat_stats.get("weather", {})
    if not weather_cat.get("allowed", True):
        _log("WARN", "[P2] Weather BLOQUEADO por win rate bajo — saltando scan de clima")
        # No return — continua con BTC si esta habilitado

    # ── Ciclo Weather ─────────────────────────────────────────────────────────
    if not bot_params.weather_enabled:
        _log("INFO", "Weather DESACTIVADO — saltando scan de mercados de clima")
        # Saltar al resto del ciclo (BTC sigue adelante abajo)
        markets = []
    else:
        with perf.timer("fetch_weather_markets"):
            markets = await fetch_weather_markets()
        _log("INFO", f"Mercados encontrados: {len(markets)}")

    opportunities = []
    for market in markets:
        if not market.get("target_date"):
            continue

        # Obtener forecast (ensemble: NOAA + OpenMeteo + observacion actual)
        with perf.timer("ensemble_forecast"):
            forecast = await get_ensemble_high(market["station"], market["target_date"])
        if not forecast:
            _log("WARN", f"Sin forecast para {market['station']} en {market['target_date']}")
            continue
        sources_str = "+".join(forecast.get("sources_used", ["?"]))
        _log(
            "INFO",
            f"Forecast {market['station']} | "
            f"NOAA:{forecast.get('noaa_high_f','?')}°F "
            f"OpenMeteo:{forecast.get('openmeteo_high_f','?')}°F "
            f"Obs:{forecast.get('current_obs_f','?')}°F → "
            f"Ensemble:{forecast['high_f']}°F ±{forecast['std_dev']}°F "
            f"({forecast['confidence'].upper()}) [{sources_str}]"
        )

        # Precio en vivo (mas preciso que el de Gamma)
        with perf.timer("live_price"):
            live_price = await get_live_price(market["yes_token_id"])
        if live_price:
            market["yes_price"] = live_price

        # Fase 4: Analizar reglas de resolucion del mercado (Lawyer's Edge)
        rules    = parse_market_rules(market.get("title") or market.get("market_title", ""), market.get("condition_id", ""))
        boundary = detect_boundary_zone(
            forecast_high_f=forecast["high_f"],
            std_dev=forecast["std_dev"],
            bucket={"type": market.get("bucket_type","range"),
                    "low":  market.get("temp_low", -999.0),
                    "high": market.get("temp_high",  999.0)},
        )
        if rules.get("warnings"):
            for w in rules["warnings"]:
                _log("WARN", f"[Lawyer] {w}")
        if boundary.get("in_boundary_zone"):
            _log("INFO", f"[Lawyer] {boundary['message']}")

        # Evaluar oportunidad (usa budget_weather para sizing)
        opportunity = evaluate_market(market, forecast, state.budget_weather)
        if opportunity:
            opportunity["rules"]    = rules
            opportunity["boundary"] = boundary
            opportunity["rules_summary"] = format_rules_for_analyst(rules, boundary)
            opportunities.append(opportunity)
            # Patron 1: mostrar bonus temporal y retorno anualizado estimado
            time_tag = f" [+{opportunity['time_bonus']:.2f} time bonus]" if opportunity.get("time_bonus", 0) > 0 else ""
            ann_tag  = f" ~{opportunity.get('annualized_return', 0):.0f}% anual" if opportunity.get("annualized_return") else ""
            # Patron 4: señal contrarian
            contra_tag = ""
            if opportunity.get("is_contrarian") and opportunity.get("contrarian_signal"):
                cs = opportunity["contrarian_signal"]
                contra_tag = f" ⚡CONTRARIAN({cs['signal']})"
            _log(
                "INFO",
                f"OPORTUNIDAD | {opportunity['side']} {market['city'].title()} "
                f"[{market['temp_low']}-{market['temp_high']}°F] | "
                f"Forecast: {forecast['high_f']:.1f}°F | "
                f"Mercado: {market['yes_price']:.2f} | "
                f"Prob: {opportunity['our_prob']:.2%} | "
                f"EV: {opportunity['ev_pct']}%{time_tag}{ann_tag}{contra_tag}",
            )

    # Patron 1 (72-hour rule): ordenar por priority_score (EV + bonus temporal)
    # Mercados que resuelven pronto suben en el ranking aunque tengan EV similar
    opportunities.sort(key=lambda x: x.get("priority_score", x["ev_pct"] / 100), reverse=True)
    state.opportunities = opportunities

    if not opportunities:
        _log("INFO", "Sin oportunidades con edge suficiente en este ciclo.")
        return

    # Ejecutar el mejor trade respetando available_weather
    weather_spent = 0.0
    for opp in opportunities:
        if state.balance_usdc < bot_params.min_position_usdc:
            _log("WARN", "Balance insuficiente para abrir nuevas posiciones.")
            break
        if weather_spent + opp["size_usdc"] > state.available_weather:
            _log("INFO", f"Weather | Disponible agotado (gastado ${weather_spent:.2f} / disponible ${state.available_weather:.2f} / presupuesto ${state.budget_weather:.2f})")
            break

        if _daily_loss_limit_reached():
            _log("WARN", "Limite de perdida diaria alcanzado. Deteniendo trades.")
            break

        # ── Fase 6: Risk Manager check ────────────────────────────────────────
        _total_acc = state.balance_usdc + state.deployed_weather + state.deployed_btc + state.deployed_updown
        risk_check = risk_manager.check_trade(
            size_usdc           = opp["size_usdc"],
            total_account_value = _total_acc,
            cash_available      = state.balance_usdc,
            open_positions      = state.poly_positions,
            city                = opp.get("city", ""),
            hours_to_close      = opp.get("hours_to_close", 48.0),
        )
        if not risk_check["allowed"]:
            _log("WARN", f"RISK | Trade bloqueado: {risk_check['reason']}")
            continue
        for w in risk_check.get("warnings", []):
            _log("INFO", f"RISK | {w}")
        if risk_check["adjusted_size"] != opp["size_usdc"]:
            _log("INFO", f"RISK | Tamaño ajustado: ${opp['size_usdc']:.2f} → ${risk_check['adjusted_size']:.2f}")
            opp["size_usdc"] = risk_check["adjusted_size"]
            opp["shares"]    = round(opp["size_usdc"] / opp["entry_price"], 1)

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

        # Consultar a Claude antes de ejecutar (saltar si auto_trade_mode activo)
        if state.auto_trade_mode:
            _log("INFO", f"AUTO-TRADE | Ejecutando sin aprobación: {opp['market_title'][:55]}...")
        else:
            _log("INFO", f"Consultando a Claude sobre: {opp['market_title'][:55]}...")
            with perf.timer("claude_analysis"):
                analysis = await analyze_opportunity(opp, state.balance_usdc, state.poly_positions or [],
                                                     _build_capital_context("weather"))

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
            weather_spent += opp["size_usdc"]
            await asyncio.sleep(2)  # Pausa entre trades

    # ── Fase 5: Near-Zero scan ─────────────────────────────────────────────
    # Escanea todos los mercados weather por oportunidades <8c con EV alto
    if bot_params.weather_enabled and state.available_weather > 0:
        await _scan_nearzero(markets)

    # ── Ciclo BTC (mercados de precio) ─────────────────────────────────────
    if bot_params.btc_enabled:
        await _scan_btc_markets()

    # Guardar estado al final de cada ciclo
    _save_state()

    # Registrar tiempo total del ciclo
    _cycle_ms = (__import__("time").perf_counter() - _cycle_t0) * 1000
    perf.record_scan(
        markets_analyzed=len(markets),
        opps_found=len(state.opportunities),
        trades_evaluated=len(state.opportunities),
        scan_ms=_cycle_ms,
    )
    perf.record_time("scan_cycle_total", _cycle_ms)


async def _scan_nearzero(markets: list[dict]):
    """
    Fase 5: Escanea mercados weather buscando entradas near-zero (<8c).
    Consulta señales de smart wallets para cada candidato antes de decidir.
    """
    from strategy import calc_bucket_probability

    _log("INFO", "Near-Zero | Escaneando mercados con precio <8c...")

    def _prob_estimator(market: dict) -> float | None:
        """Estima prob usando el ultimo forecast disponible (sin llamada extra a NOAA)."""
        # Usamos el forecast ya calculado en state si lo tenemos, o retornamos None
        # para no duplicar llamadas a la API en este ciclo
        opp_match = next(
            (o for o in state.opportunities if o.get("condition_id") == market.get("condition_id")),
            None,
        )
        if opp_match:
            return opp_match.get("our_prob")
        # Si el mercado tiene yes_price muy bajo y no tenemos forecast, usar heuristica
        # basada en el precio con descuento conservador del 30%
        yes_price = market.get("yes_price", 1.0)
        if yes_price <= 0.08:
            return min(yes_price * 3.0, 0.25)   # heuristica muy conservadora
        return None

    # Señales de smart wallets para mercados near-zero
    nearzero_candidates = [m for m in markets if m.get("yes_price", 1.0) <= 0.08]
    wallet_signals_map: dict[str, list] = {}

    if nearzero_candidates:
        _log("INFO", f"Near-Zero | {len(nearzero_candidates)} candidatos — consultando smart wallets...")
        signal_tasks = [
            wallet_tracker.get_signals_for_market(m.get("condition_id", ""))
            for m in nearzero_candidates
        ]
        all_signals = await asyncio.gather(*signal_tasks)
        for market, signals in zip(nearzero_candidates, all_signals):
            cid = market.get("condition_id", "")
            if signals:
                wallet_signals_map[cid] = signals
                _log("INFO", f"Near-Zero | Señal wallet en {market.get('market_title','')[:50]}: "
                     f"{[s['wallet_name'] for s in signals]}")

    opps = scan_nearzero_opportunities(
        markets=markets,
        prob_estimator=_prob_estimator,
        balance_usdc=state.balance_usdc,
        wallet_signals_by_cid=wallet_signals_map,
    )

    if not opps:
        _log("INFO", "Near-Zero | Sin oportunidades near-zero en este ciclo.")
        return

    _log("INFO", f"Near-Zero | {len(opps)} oportunidades encontradas:")
    for opp in opps[:5]:
        wallet_tag = f" [wallets: {opp['wallet_count']}]" if opp["wallet_count"] > 0 else ""
        _log(
            "INFO",
            f"Near-Zero [{opp['quality']}] | {opp['market_title'][:50]} | "
            f"Precio: {opp['entry_price']:.3f} | EV: +{opp['ev_pct']}% | "
            f"Size: ${opp['size_usdc']} | Payout: {opp['payout_ratio']}:1{wallet_tag}",
        )

    # Ejecutar solo las A+ y A con confirmacion de wallets (las mas seguras)
    for opp in opps:
        if opp["quality"] not in ("A+", "A"):
            continue
        if state.balance_usdc < opp["size_usdc"]:
            break
        # No entrar si ya tenemos posicion en este mercado
        if any(p.get("condition_id") == opp["condition_id"] for p in state.poly_positions):
            continue

        _log("INFO", f"Near-Zero | AUTO-EJECUTANDO [{opp['quality']}]: {opp['market_title'][:50]}")
        success = await asyncio.get_event_loop().run_in_executor(None, _execute_trade, opp)
        if success:
            await asyncio.sleep(1)


async def _scan_btc_markets():
    """
    Escanea mercados de precio de BTC en Polymarket.
    Usa TradingView TA y CoinMarketCap para enriquecer el análisis.
    Ajusta el intervalo de volatilidad según el horizonte máximo configurado.
    """
    if not bot_params.btc_enabled:
        _log("INFO", "BTC | Desactivado en panel — scan omitido")
        return
    _log("INFO", "BTC | Obteniendo precio y datos de mercado...")

    # ── Precio ──────────────────────────────────────────────────────────────
    btc_price = await get_btc_price()
    if not btc_price:
        _log("WARN", "BTC | No se pudo obtener precio de BTC — saltando ciclo BTC")
        return
    state.btc_price = btc_price

    # ── CoinMarketCap ────────────────────────────────────────────────────────
    from config import settings as _settings
    cmc_data = await get_btc_market_data_cmc(_settings.cmc_api_key)
    if cmc_data:
        state.btc_cmc = cmc_data
        _log(
            "INFO",
            f"BTC | CMC: ${cmc_data['price']:,.2f} | "
            f"1h: {cmc_data['percent_change_1h']:+.2f}% | "
            f"24h: {cmc_data['percent_change_24h']:+.2f}%",
        )
    else:
        _log("INFO", f"BTC | Precio Binance: ${btc_price:,.2f} (CMC no configurado)")

    # ── Volatilidad adaptativa ───────────────────────────────────────────────
    # Usar intervalo corto si el horizonte máximo es corto (ej. mercados de 5 min)
    vol_interval, vol_candles = vol_interval_for_horizon(bot_params.btc_max_hours_to_resolution)
    vol_per_candle = await get_btc_volatility(interval=vol_interval, candles=vol_candles)
    # Convertir sigma por vela → sigma por minuto (para la fórmula log-normal del estratega)
    _CANDLE_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    candle_mins = _CANDLE_MINUTES.get(vol_interval, 15)
    import math as _math
    vol_per_minute = vol_per_candle / _math.sqrt(candle_mins)
    _log(
        "INFO",
        f"BTC | Vol ({vol_interval} × {vol_candles}): {vol_per_candle * 100:.4f}% por vela → "
        f"{vol_per_minute * 100:.4f}% por minuto",
    )

    # ── TradingView TA ───────────────────────────────────────────────────────
    tv_interval = tv_interval_for_horizon(bot_params.btc_max_hours_to_resolution)
    ta_data = await get_btc_ta(interval=tv_interval)
    state.btc_ta = ta_data
    if ta_data.get("available"):
        rsi_str = f" | RSI: {ta_data['rsi']:.1f}" if ta_data.get("rsi") else ""
        _log(
            "INFO",
            f"BTC | TradingView ({tv_interval}): {ta_data['recommendation']} "
            f"[↑{ta_data['buy']} ↔{ta_data['neutral']} ↓{ta_data['sell']}]{rsi_str}",
        )
    else:
        _log("WARN", f"BTC | TradingView TA no disponible: {ta_data.get('error', '')}")

    # ── Mercados Polymarket ──────────────────────────────────────────────────
    markets = await fetch_btc_markets()
    _log("INFO", f"BTC | Mercados encontrados: {len(markets)}")

    btc_opps = []
    for market in markets:
        live_price = await get_live_price(market["yes_token_id"])
        if live_price:
            market["yes_price"] = live_price

        opp = evaluate_btc_market(
            market, btc_price, vol_per_minute, state.budget_btc,
            ta_data=ta_data,
        )
        if opp:
            btc_opps.append(opp)
            ta_str = f" | TV: {opp['ta_recommendation']}" if opp.get("ta_recommendation") else ""
            _log(
                "INFO",
                f"BTC OPP | {opp['side']} {'>' if opp['direction']=='above' else '<'}"
                f"${opp['threshold']:,.0f} | "
                f"${btc_price:,.0f} ({opp['pct_from_threshold']:+.2f}%) | "
                f"P: {opp['our_prob']:.1%} vs {opp['market_prob']:.1%} | "
                f"EV: {opp['ev_pct']}% | {opp['minutes_to_close']:.0f}m{ta_str}",
            )

    btc_opps.sort(key=lambda x: x["ev_pct"], reverse=True)
    state.btc_opportunities = btc_opps

    if not btc_opps:
        _log("INFO", "BTC | Sin oportunidades con edge suficiente.")
        return

    # ── Ejecutar trades (respeta budget_btc) ───────────────────────────────
    btc_spent = 0.0
    for opp in btc_opps:
        if state.balance_usdc < bot_params.min_position_usdc:
            break
        if btc_spent + opp["size_usdc"] > state.available_btc:
            _log("INFO", f"BTC | Disponible agotado (gastado ${btc_spent:.2f} / disponible ${state.available_btc:.2f} / presupuesto ${state.budget_btc:.2f})")
            break

        if _daily_loss_limit_reached():
            break

        if any(p.get("condition_id") == opp.get("condition_id") for p in state.poly_positions):
            continue

        if state.auto_trade_mode:
            _log("INFO", f"BTC AUTO-TRADE | Ejecutando sin aprobación: {opp['market_title'][:55]}...")
        else:
            _log("INFO", f"BTC | Consultando Claude sobre: {opp['market_title'][:55]}...")
            analysis = await analyze_opportunity(opp, state.balance_usdc, state.poly_positions or [],
                                                 _build_capital_context("btc"))

            if analysis["skipped"] or not analysis["approved"]:
                reason = analysis.get("reason", "")
                _log("WARN", f"BTC | Claude {'NO CONFIGURADO' if analysis['skipped'] else 'RECHAZA'}: {reason}")
                opp["claude_rejected"] = True
                continue

            _log("INFO", f"BTC | Claude APRUEBA [{analysis['confidence']}]: {analysis['reason']}")

        success = await asyncio.get_event_loop().run_in_executor(None, _execute_trade, opp)
        if success:
            btc_spent += opp["size_usdc"]
            await asyncio.sleep(2)


def _calc_deployed_by_type() -> dict[str, float]:
    """
    Calcula el capital actualmente desplegado en posiciones abiertas, separado por tipo.
    Fuente de verdad: state.poly_positions (datos frescos de Polymarket).
    Usa cost_usdc (costo original) de cada posición activa.
    """
    deployed = {"WEATHER": 0.0, "BTC": 0.0, "BTC_UPDOWN": 0.0}
    for pos in state.poly_positions:
        cost  = float(pos.get("cost_usdc", pos.get("cur_value_usdc", 0.0)))
        title = pos.get("market_title", "").lower()
        if "updown" in title or "up/down" in title or "up or down" in title or "up down" in title:
            asset = "BTC_UPDOWN"
        elif "btc" in title or "bitcoin" in title or "$" in title:
            asset = "BTC"
        else:
            asset = "WEATHER"
        deployed[asset] = deployed.get(asset, 0.0) + cost
    return deployed


# ── BTC Up/Down markets (5m y 15m) ────────────────────────────────────────

# Rastrea qué slugs ya operamos en esta sesión para no duplicar
_updown_traded_slugs: set[str] = set()

# Mapeo token_id → detalles del trade pendiente de resolución
# {"interval": int, "side": str, "btc_start": float, "end_ts": int, "trade_idx": int}
_updown_pending_outcomes: dict[str, dict] = {}

# Rastrea qué slugs ya tienen apuesta fantasma registrada
_updown_phantom_slugs: set[str] = set()

# Mapeo slug → detalles de apuesta fantasma pendiente de resolución
_updown_phantom_pending: dict[str, dict] = {}


async def _scan_updown(interval_minutes: int):
    """
    Escanea el mercado UP/DOWN del intervalo dado y ejecuta si hay edge.
    Respeta el límite de pérdidas consecutivas.
    """
    is_5m = interval_minutes == 5

    if is_5m:
        losses  = state.updown_5m_consecutive_losses
        stopped = state.updown_5m_stopped
    else:
        losses  = state.updown_15m_consecutive_losses
        stopped = state.updown_15m_stopped

    if stopped:
        _log("WARN", f"UpDown {interval_minutes}m | DETENIDO por {losses} pérdidas consecutivas")
        return

    market = await fetch_updown_market(interval_minutes)
    if not market:
        _log("INFO", f"UpDown {interval_minutes}m | Sin mercado activo ahora mismo")
        return

    slug = market["slug"]
    _log(
        "INFO",
        f"UpDown {interval_minutes}m | {market['title']} | "
        f"{market['minutes_to_close']:.1f}min restantes | "
        f"UP:{market['up_price']:.3f} DOWN:{market['down_price']:.3f}",
    )

    # Guardar mercado actual en estado
    if is_5m:
        state.updown_last_market_5m = market
    else:
        state.updown_last_market_15m = market

    # No operar dos veces en el mismo ciclo de mercado
    if slug in _updown_traded_slugs:
        _log("INFO", f"UpDown {interval_minutes}m | Ya operado en este ciclo — esperando el siguiente")
        return

    # No operar si ya hay posición abierta en este mercado (protección post-restart)
    _live_tokens = {p.get("token_id") for p in (state.poly_positions or [])}
    if market["up_token"] in _live_tokens or market["down_token"] in _live_tokens:
        _updown_traded_slugs.add(slug)  # marcar para no reintentar
        _log("INFO", f"UpDown {interval_minutes}m | Posición ya abierta en {slug} — omitiendo trade duplicado")
        return

    # Obtener TA para el intervalo adecuado
    ta_interval = "1m" if interval_minutes == 5 else "5m"
    ta_data = await get_btc_ta(interval=ta_interval)

    if is_5m:
        state.updown_ta_5m = ta_data
    else:
        state.updown_ta_15m = ta_data

    # Precio BTC al inicio de la ventana (de Binance klines)
    btc_price_now   = state.btc_price or await get_btc_price()
    btc_price_start = await _get_btc_price_at_ts(market["window_start_ts"])

    # Sin precio base no podemos analizar dirección — no operar
    if not btc_price_start or btc_price_start <= 0:
        _log("WARN", f"UpDown {interval_minutes}m | Sin precio BTC al inicio de ventana — cancelando")
        return

    # CMC data para señal de tendencia macro 1h
    cmc_data = state.btc_cmc if state.btc_cmc else None

    # Telonex on-chain signals (OFI real + smart wallet bias)
    telonex_signals = None
    if bot_params.telonex_enabled:
        try:
            telonex_signals = await telonex_data.get_updown_signals(market, btc_price_start)
        except Exception as _tx_err:
            _log("WARN", f"UpDown {interval_minutes}m | Telonex error: {_tx_err}")

    opp, skip_reason = evaluate_updown_market(
        market=market,
        ta_data=ta_data,
        btc_price=btc_price_now or 0,
        btc_price_window_start=btc_price_start,
        cmc_data=cmc_data,
        telonex_signals=telonex_signals,
    )

    # Guardar oportunidad evaluada (aunque no se opere)
    scan_snapshot = {
        "scanned_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "slug":       slug,
        "up_price":   market["up_price"],
        "down_price": market["down_price"],
        "elapsed_minutes": round(market.get("elapsed_minutes", 0), 1),
        "minutes_to_close": round(market["minutes_to_close"], 1),
        "ta_rec":     ta_data.get("recommendation", "—"),
        "ta_signal":  round(ta_data.get("signal", 0), 2) if ta_data.get("signal") is not None else 0,
        "ta_rsi":     round(ta_data["rsi"], 1) if ta_data.get("rsi") else None,
        "btc_price":  btc_price_now,
        "btc_start":  btc_price_start,
        "opp":        opp,  # None si no hay señal suficiente
    }
    if is_5m:
        state.updown_last_opp_5m = scan_snapshot
    else:
        state.updown_last_opp_15m = scan_snapshot

    # Log del análisis completo (siempre, haya o no señal)
    from strategy_updown import build_btc_direction_signal
    _sig = build_btc_direction_signal(
        ta_data=ta_data,
        btc_price=btc_price_now or 0,
        btc_price_window_start=btc_price_start,
        cmc_data=cmc_data,
        market=market,
        telonex_signals=telonex_signals,
    )
    _log("INFO",
         f"UpDown {interval_minutes}m | Mercado: UP={market['up_price']:.2f} DOWN={market['down_price']:.2f} "
         f"| BTC inicio=${btc_price_start:.0f} ahora=${btc_price_now:.0f} ({_sig['window_pct']:+.3f}%) "
         f"| cierra en {market['minutes_to_close']:.1f}min")
    _tx_str = (
        f" RealOFI:{_sig['real_ofi']:+.3f} SmartBias:{_sig['smart_bias']:+.3f}"
        if _sig.get("telonex_available") else " [Telonex:off]"
    )
    _log("INFO",
         f"UpDown {interval_minutes}m | Señales — "
         f"TA:{ta_data.get('recommendation','?')}({_sig['ta_raw']:+.3f}) "
         f"RSI:{ta_data.get('rsi','?')} MACD:{_sig['macd_sig']:+.3f} "
         f"EMA:{_sig['ema_sig']:+.3f} Momentum:{_sig['momentum']:+.3f} "
         f"OFI:{_sig.get('ofi',0):+.3f} Mercado:{_sig['market_sig']:+.3f} Macro:{_sig['macro']:+.3f}"
         f"{_tx_str} → COMBINADA:{_sig['combined']:+.4f} ({_sig['direction']})")

    # ── Apuesta fantasma (siempre, haya o no señal real) ────────────────────
    # Se registra para CADA mercado escaneado — si hubo trade real la
    # skip_reason será "traded_real", si no "no_signal" u otro filtro.
    # Así acumulamos datos continuos para el learner.
    if slug not in _updown_phantom_slugs:
        if interval_minutes <= 5:
            _ph_combined = (
                _sig["ta"]            * 0.55
                + (-_sig["momentum"]) * 0.25   # mean-reversion en 5m
                + _sig["market_sig"]  * 0.10
                + _sig["macro"]       * 0.10
            )
            _ph_combined = max(-1.0, min(1.0, _ph_combined))
            phantom_dir  = "UP" if _ph_combined > 0 else ("DOWN" if _ph_combined < 0 else "NEUTRAL")
            phantom_conf = round(abs(_ph_combined) * 100, 1)
        else:
            phantom_dir  = _sig["direction"]
            phantom_conf = _sig["confidence"]

        if phantom_dir != "NEUTRAL":
            _ph_reason = skip_reason or ("traded_real" if opp else "no_signal")
            end_ts = int(market["window_start_ts"]) + interval_minutes * 60
            _updown_phantom_pending[slug] = {
                "interval":        interval_minutes,
                "side":            phantom_dir,
                "btc_start":       btc_price_start or 0,
                "end_ts":          end_ts,
                "slug":            slug,
                "skip_reason":     _ph_reason,
                "confidence":      phantom_conf,
                "combined_signal": _sig["combined"],
                "ta_signal":       _sig["ta_raw"],
                "ta_rsi":          _sig.get("rsi"),
                "window_momentum": _sig["momentum"],
                "elapsed_minutes": market.get("elapsed_minutes", 0),
            }
            _updown_phantom_slugs.add(slug)
            _log(
                "INFO",
                f"UpDown {interval_minutes}m | [PHANTOM] ✦ {phantom_dir} registrado — "
                f"confianza {phantom_conf:.0f}% | combined={_sig['combined']:+.3f} | "
                f"motivo_skip={_ph_reason}",
            )
            # ── Experimento VPS-Confianza (solo phantom, sin dinero real) ────
            try:
                from vps_experiment import record_phantom_vps as _vps_rec
                _vps_rec(
                    slug=slug,
                    interval=interval_minutes,
                    side=phantom_dir,
                    confidence_pct=phantom_conf,
                    btc_start=btc_price_start or 0,
                    end_ts=end_ts,
                    ta_scores={
                        "combined":  _sig.get("combined", 0),
                        "ta":        _sig.get("ta_raw", 0),
                        "rsi":       _sig.get("rsi"),
                        "macd":      _sig.get("macd_sig", 0),
                        "ema":       _sig.get("ema_sig", 0),
                        "momentum":  _sig.get("momentum", 0),
                        "ofi":       _sig.get("ofi", 0),
                        "market":    _sig.get("market_sig", 0),
                        "macro":     _sig.get("macro", 0),
                    },
                    entry_price=opp["entry_price"] if opp else 0.50,
                )
            except Exception as _vps_err:
                _log("WARN", f"[VPS] Error registrando phantom: {_vps_err}")
        else:
            # Señal neutral — phantom no se registra porque no hay dirección clara
            _updown_phantom_slugs.add(slug)  # evitar spam en ciclos siguientes del mismo slug
            _log(
                "INFO",
                f"UpDown {interval_minutes}m | [PHANTOM] ✗ NEUTRAL — señal sin dirección clara "
                f"(combined={_sig['combined']:+.3f} | TA:{_sig['ta_raw']:+.3f} "
                f"RSI:{_sig.get('rsi','?')} Momentum:{_sig['momentum']:+.3f})",
            )
    else:
        _log(
            "INFO",
            f"UpDown {interval_minutes}m | [PHANTOM] ⟳ ya registrado para este slug — esperando resolución",
        )

    if not opp:
        _log("INFO", f"UpDown {interval_minutes}m | [REAL] ✗ Sin entrada — {skip_reason}")
        return

    _log(
        "INFO",
        f"UpDown {interval_minutes}m | SEÑAL {opp['side']} | "
        f"Confianza:{opp['confidence']}% | Precio entrada:{opp['entry_price']:.3f} | "
        f"RR ratio:{opp.get('rr_ratio',0):.3f} | BTC movió {opp.get('window_pct',0):+.3f}% en ventana",
    )

    # Calcular disponible para este intervalo
    if bot_params.betting_pool_usdc > 0:
        # Sistema de buckets: cada intervalo tiene su propio bucket
        _avail_now = round(bot_params.bucket_updown_5m_usdc if is_5m else bot_params.bucket_updown_15m_usdc, 2)
        _bucket_label = "updown_5m" if is_5m else "updown_15m"
    else:
        # Sistema legacy
        _updown_headroom = max(0.0, state.budget_updown - state.deployed_updown)
        _avail_now = round(min(_updown_headroom, state.balance_usdc), 2)
        _bucket_label = "UpDown"

    if state.balance_usdc < opp["size_usdc"]:
        _log("WARN", (
            f"UpDown {interval_minutes}m | Cash insuficiente: "
            f"tienes ${state.balance_usdc:.2f} pero el trade necesita ${opp['size_usdc']} — "
            f"sube el cash o baja updown_max_usdc"
        ))
        return
    if opp["size_usdc"] > _avail_now:
        _log("WARN", (
            f"UpDown {interval_minutes}m | Bucket [{_bucket_label}] agotado: "
            f"quieres gastar ${opp['size_usdc']} pero solo hay ${_avail_now:.2f} disponible"
        ))
        return
    if _daily_loss_limit_reached():
        return

    # ── Revisión de Claude antes de ejecutar ────────────────────────────────
    if not state.auto_trade_mode:
        _log("INFO", f"UpDown {interval_minutes}m | Consultando a Claude...")
        claude = await analyze_updown_opportunity(
            opportunity=opp,
            ta_data=ta_data,
            btc_price_now=btc_price_now or 0,
            btc_price_start=btc_price_start or 0,
            cmc_data=cmc_data,
        )
        opp["claude_reason"]     = claude["reason"]
        opp["claude_confidence"] = claude["confidence"]
        opp["claude_raw"]        = claude.get("raw", "")

        if not claude["approved"]:
            _log("WARN", f"UpDown {interval_minutes}m | Claude RECHAZA [{claude['confidence']}]: {claude['reason']}")
            opp["claude_rejected"] = True
            return

        # Claude puede cambiar la dirección si ve señales más fuertes en el otro lado
        if claude["direction_changed"]:
            new_dir = claude["direction"]
            _log("INFO",
                 f"UpDown {interval_minutes}m | Claude CAMBIA dirección "
                 f"{opp['side']} → {new_dir}: {claude['reason']}")
            opp["side"] = new_dir
            if new_dir == "UP":
                opp["token_id"]    = market["up_token"]
                opp["entry_price"] = market["up_price"]
            else:
                opp["token_id"]    = market["down_token"]
                opp["entry_price"] = market["down_price"]
            opp["shares"] = round(opp["size_usdc"] / opp["entry_price"], 2)

        _log("INFO", f"UpDown {interval_minutes}m | Claude APRUEBA [{claude['confidence']}]: {claude['reason']}")
    else:
        _log("INFO", f"UpDown {interval_minutes}m | AUTO-TRADE — ejecutando sin revisión de Claude")

    success = await asyncio.get_event_loop().run_in_executor(None, _execute_trade, opp)
    if success:
        _updown_traded_slugs.add(slug)
        # Evitar crecimiento ilimitado: mantener solo los 200 slugs más recientes
        if len(_updown_traded_slugs) > 200:
            # Los slugs son strings con timestamp — eliminar arbitrariamente los más viejos
            excess = len(_updown_traded_slugs) - 200
            for old_slug in list(_updown_traded_slugs)[:excess]:
                _updown_traded_slugs.discard(old_slug)

        # Calcular cuándo cierra la ventana para poder resolver el resultado vía BTC price
        end_ts = int(market["window_start_ts"]) + interval_minutes * 60

        trade_record = {
            "time":            datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
            "interval":        interval_minutes,
            "slug":            slug,
            "side":            opp["side"],
            "entry_price":     opp["entry_price"],
            "size_usdc":       opp["size_usdc"],
            "confidence":      opp.get("confidence", 0),
            "combined_signal": opp.get("combined_signal", 0),
            "ta_rec":          opp.get("ta_recommendation", "—"),
            "ta_rsi":          opp.get("ta_rsi"),
            "ta_signal":       opp.get("ta_signal", 0),
            "window_momentum": opp.get("window_momentum", 0),
            "elapsed_minutes": opp.get("elapsed_minutes", 0),
            "btc_price":       btc_price_now,
            "btc_start":       btc_price_start,
            "end_ts":          end_ts,
            # Claude review
            "claude_reason":     opp.get("claude_reason", "AUTO-TRADE"),
            "claude_confidence": opp.get("claude_confidence", "N/A"),
            "result":          None,  # se actualiza cuando resuelve
            # campos legacy para compatibilidad con la UI
            "ev_pct":          opp.get("ev_pct", 0),
            "our_prob":        opp.get("our_prob", 0),
        }
        if is_5m:
            state.updown_last_trade_5m = trade_record
        else:
            state.updown_last_trade_15m = trade_record
        # Historial reciente (últimos 30)
        state.updown_recent_trades.insert(0, trade_record)
        if len(state.updown_recent_trades) > 30:
            state.updown_recent_trades = state.updown_recent_trades[:30]

        # Guardar en pending para resolver el resultado por precio BTC al cierre
        _updown_pending_outcomes[opp["token_id"]] = {
            "interval": interval_minutes,
            "side":     opp["side"],
            "btc_start": btc_price_start or 0,
            "end_ts":   end_ts,
            "slug":     slug,
        }

        # Descontar del bucket (ya hecho en _execute_trade via _deduct_from_bucket)
        # Actualizar estado en memoria para coherencia
        state.available_updown = round(max(0.0, state.available_updown - opp["size_usdc"]), 2)
        state.deployed_updown  = round(state.deployed_updown + opp["size_usdc"], 2)
        if bot_params.betting_pool_usdc > 0:
            _bkt_val = bot_params.bucket_updown_5m_usdc if is_5m else bot_params.bucket_updown_15m_usdc
            _log("INFO", f"UpDown {interval_minutes}m | TRADE {opp['side']} ${opp['size_usdc']} @ {opp['entry_price']:.3f} | EV {opp['ev_pct']}% | bucket restante ${_bkt_val:.2f}")
        else:
            _log("INFO", f"UpDown {interval_minutes}m | TRADE {opp['side']} ${opp['size_usdc']} @ {opp['entry_price']:.3f} | EV {opp['ev_pct']}% | avail restante ${state.available_updown:.2f}")
    else:
        _log("WARN", f"UpDown {interval_minutes}m | Fallo al ejecutar trade")


async def _get_btc_price_at_ts(unix_ts: int) -> Optional[float]:
    """
    Obtiene el precio OPEN de BTC al timestamp dado consultando múltiples fuentes.
    Incluye Chainlink on-chain como fuente primaria (es la fuente de resolución de
    los mercados UpDown de Polymarket).

    Fuentes (en paralelo):
      0. Chainlink on-chain — precio actual si unix_ts es reciente (< 5 min atrás)
      1. Binance  — klines 1m, precio OPEN
      2. Kraken   — OHLC 1m, precio OPEN
      3. Coinbase — candles 1m, precio OPEN
    """
    import httpx as _httpx

    async def _from_binance(client: _httpx.AsyncClient) -> Optional[float]:
        try:
            r = await client.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m",
                        "startTime": unix_ts * 1000, "endTime": (unix_ts + 120) * 1000, "limit": 2},
                timeout=6,
            )
            if r.status_code == 200:
                d = r.json()
                if d:
                    return float(d[0][1])  # open
        except Exception:
            pass
        return None

    async def _from_kraken(client: _httpx.AsyncClient) -> Optional[float]:
        try:
            r = await client.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": "XBTUSD", "interval": 1, "since": unix_ts - 60},
                timeout=6,
            )
            if r.status_code == 200:
                data = r.json()
                ohlc = data.get("result", {}).get("XXBTZUSD") or data.get("result", {}).get("XBTUSD", [])
                # Encontrar la vela más cercana al timestamp
                best = None
                for candle in ohlc:
                    candle_ts = int(candle[0])
                    if abs(candle_ts - unix_ts) <= 60:
                        best = float(candle[1])  # open
                        break
                return best
        except Exception:
            pass
        return None

    async def _from_coinbase(client: _httpx.AsyncClient) -> Optional[float]:
        try:
            from datetime import timezone as _tz
            start = datetime.fromtimestamp(unix_ts - 60, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            end   = datetime.fromtimestamp(unix_ts + 120, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            r = await client.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={"granularity": 60, "start": start, "end": end},
                headers={"User-Agent": "WeatherbotPolymarket/1.0"},
                timeout=6,
            )
            if r.status_code == 200:
                candles = r.json()  # [timestamp, low, high, open, close, volume]
                if candles:
                    # Ordenar y tomar la vela más cercana
                    candles.sort(key=lambda c: abs(int(c[0]) - unix_ts))
                    return float(candles[0][3])  # open
        except Exception:
            pass
        return None

    # Chainlink: si el timestamp es reciente (< 5 min), podemos usar el precio live
    # Para timestamps históricos, Chainlink on-chain requiere buscar rondas — usamos spot
    now_ts_local = int(datetime.now(timezone.utc).timestamp())
    chainlink_price: Optional[float] = None
    if abs(now_ts_local - unix_ts) < 300:  # timestamp reciente → precio Chainlink actual válido
        try:
            from price_feed import get_btc_price_chainlink as _cl_price
            chainlink_price = await _cl_price()
            if chainlink_price:
                _log("INFO", f"UpDown | Precio Chainlink live: ${chainlink_price:,.2f} (fuente resolución Polymarket)")
        except Exception:
            pass

    async with _httpx.AsyncClient(headers={"User-Agent": "WeatherbotPolymarket/1.0"}) as client:
        results = await asyncio.gather(
            _from_binance(client),
            _from_kraken(client),
            _from_coinbase(client),
            return_exceptions=True,
        )

    prices = [p for p in results if isinstance(p, float) and p and p > 0]

    # Añadir Chainlink al pool si está disponible
    if chainlink_price:
        prices.append(chainlink_price)

    if not prices:
        # Sin ninguna fuente disponible — fallback al cache si es reciente
        if abs(now_ts_local - unix_ts) < 600 and state.btc_price and state.btc_price > 0:
            _log("WARN", f"UpDown | Todas las fuentes fallaron, usando cache ${state.btc_price:,.0f}")
            return state.btc_price
        return None

    # Con múltiples precios: descartar outliers (> 0.5% de la mediana) y usar mediana
    prices.sort()
    median = prices[len(prices) // 2]
    valid  = [p for p in prices if abs(p - median) / median < 0.005]
    final  = sum(valid) / len(valid) if valid else median

    spot_sources = ["Binance", "Kraken", "Coinbase"]
    labels  = [spot_sources[i] for i, p in enumerate(results) if isinstance(p, float) and p and p > 0]
    if chainlink_price:
        labels.insert(0, "Chainlink")
    _log("INFO", f"UpDown | Precio inicio ventana ${final:,.2f} ({'/'.join(labels)}, {len(prices)} fuentes)")
    return round(final, 2)


def _update_updown_loss_streak(interval_minutes: int, won: bool, trade_record: Optional[dict]):
    """
    Actualiza el contador de pérdidas consecutivas, registra resultado en learner,
    y actualiza el campo 'result' en el historial reciente.
    """
    from config import bot_params as _bp

    # ── Actualizar historial reciente ────────────────────────────────────────
    result_str = "WIN" if won else "LOSS"
    # Buscar el trade más reciente sin resultado para este intervalo y marcarlo
    for tr in state.updown_recent_trades:
        if tr.get("interval") == interval_minutes and tr.get("result") is None:
            tr["result"] = result_str
            break

    # Actualizar trade_history con el resultado (para que el win rate global sea correcto)
    for th in state.trade_history:
        if (
            th.get("asset") == "BTC_UPDOWN"
            and th.get("interval_minutes") == interval_minutes
            and th.get("result") is None
        ):
            th["result"] = result_str
            break

    # ── Learner: registrar resultado ─────────────────────────────────────────
    try:
        from updown_learner import record_result as _lr
        effective_trade = trade_record or {}
        # Si no tenemos el trade record explícito, usar el del historial
        if not effective_trade:
            for tr in state.updown_recent_trades:
                if tr.get("interval") == interval_minutes and tr.get("result") == result_str:
                    effective_trade = tr
                    break
        _lr(interval_minutes, effective_trade, won)

        # Loguear parámetros adaptativos actualizados
        from updown_learner import get_adaptive_params as _gap
        ap = _gap(interval_minutes)
        _log("INFO",
             f"UpDown {interval_minutes}m | Learner: {ap['reason']} | "
             f"min_signal={ap['min_signal']:.2f} invert={ap['invert_signal']}")
    except Exception as e:
        _log("WARN", f"UpDown learner error: {e}")

    # ── Racha de pérdidas ────────────────────────────────────────────────────
    if interval_minutes == 5:
        if won:
            state.updown_5m_consecutive_losses = 0
            _log("INFO", f"UpDown 5m | WIN — racha de pérdidas reiniciada")
        else:
            state.updown_5m_consecutive_losses += 1
            _log("WARN", f"UpDown 5m | LOSS — pérdidas consecutivas: {state.updown_5m_consecutive_losses}")
            if state.updown_5m_consecutive_losses >= _bp.updown_max_consecutive_losses:
                state.updown_5m_stopped = True
                _log(
                    "WARN",
                    f"UpDown 5m | DETENIDO — {state.updown_5m_consecutive_losses} pérdidas consecutivas. "
                    "Reactiva manualmente desde la UI.",
                )
    else:
        if won:
            state.updown_15m_consecutive_losses = 0
            _log("INFO", f"UpDown 15m | WIN — racha de pérdidas reiniciada")
        else:
            state.updown_15m_consecutive_losses += 1
            _log("WARN", f"UpDown 15m | LOSS — pérdidas consecutivas: {state.updown_15m_consecutive_losses}")
            if state.updown_15m_consecutive_losses >= _bp.updown_max_consecutive_losses:
                state.updown_15m_stopped = True
                _log(
                    "WARN",
                    f"UpDown 15m | DETENIDO — {state.updown_15m_consecutive_losses} pérdidas consecutivas. "
                    "Reactiva manualmente desde la UI.",
                )


async def _cancel_stale_updown_orders():
    """
    Cancela órdenes GTC de UpDown que llevan más de 90 segundos abiertas sin llenarse.
    Usa el mismo patrón de _fetch_open_orders (OpenOrderParams).
    """
    if _clob_client is None:
        return
    try:
        # Reusar _fetch_open_orders — ya parsea correctamente con OpenOrderParams
        open_orders = await asyncio.get_event_loop().run_in_executor(None, _fetch_open_orders)
        if not open_orders:
            return

        # Tokens UpDown conocidos (los que el bot está siguiendo)
        updown_tokens = {
            p.get("token_id", "") if isinstance(p, dict) else ""
            for p in _updown_pending_outcomes.values()
        }

        now_ts    = datetime.now(timezone.utc).timestamp()
        cancelled = 0

        for order in open_orders:
            token_id   = order.get("token_id", "")
            order_id   = order.get("id", "")
            created_at = order.get("created_at", "")

            # Solo cancelar si es una orden de UpDown
            if updown_tokens and token_id not in updown_tokens:
                continue

            # Parsear edad de la orden
            try:
                created_ts = datetime.fromisoformat(
                    str(created_at).replace("Z", "+00:00")
                ).timestamp()
                age_s = now_ts - created_ts
            except Exception:
                age_s = 999  # no se puede parsear → cancelar igual

            if age_s > 90 and order_id:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda oid=order_id: _clob_client.cancel(oid)
                    )
                    cancelled += 1
                    _log("INFO", f"UpDown | Orden GTC cancelada ({age_s:.0f}s sin llenar): {order_id[:20]}…")
                except Exception as ce:
                    _log("WARN", f"UpDown | Error cancelando {order_id[:20]}: {ce}")

        if cancelled:
            _log("INFO", f"UpDown | {cancelled} orden(es) stale canceladas")

    except Exception as e:
        _log("WARN", f"UpDown | Error al cancelar órdenes stale: {e}")


async def _resolve_pending_updown_outcomes():
    """
    Revisa si algún trade UpDown pendiente ya cerró su ventana.
    En ese caso, obtiene el precio BTC al cierre y determina WIN/LOSS.
    Mucho más confiable que esperar el flag 'redeemable' de Polymarket,
    que tarda minutos para mercados de 5m.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())

    # ── Resolver orphans del historial (trades sin resultado tras restart) ────
    # updown_recent_trades persiste en state.json pero _updown_pending_outcomes
    # es in-memory: se pierde al reiniciar. Buscamos trades con result=None
    # cuya ventana ya cerró y los resolvemos retroactivamente.
    for tr in state.updown_recent_trades:
        if tr.get("result") is not None:
            continue
        end_ts_tr = tr.get("end_ts", 0)
        if not end_ts_tr or now_ts < end_ts_tr + 30:
            continue  # ventana aún abierta o sin datos
        interval_tr = tr.get("interval", 5)
        side_tr     = tr.get("side", "UP")
        btc_start_tr = tr.get("btc_start") or tr.get("btc_price", 0)
        if not btc_start_tr:
            continue
        btc_end_tr = await _get_btc_price_at_ts(end_ts_tr)
        if not btc_end_tr:
            if now_ts > end_ts_tr + 60:
                btc_end_tr = state.btc_price or await get_btc_price()
            if not btc_end_tr:
                continue
        btc_went_up_tr = btc_end_tr >= btc_start_tr
        won_tr = (side_tr == "UP") == btc_went_up_tr
        _log(
            "INFO" if won_tr else "WARN",
            f"UpDown {interval_tr}m | Resolviendo trade pendiente (orphan) → "
            f"{'WIN ✓' if won_tr else 'LOSS ✗'} | BTC ${btc_start_tr:.0f}→${btc_end_tr:.0f} | apostamos {side_tr}",
        )
        _update_updown_loss_streak(interval_tr, won_tr, tr)

    if not _updown_pending_outcomes and not _updown_phantom_pending:
        return

    resolved_tokens = []

    for token_id, pending in list(_updown_pending_outcomes.items()):
        if not isinstance(pending, dict):
            # formato viejo (int), limpiar
            resolved_tokens.append(token_id)
            continue

        end_ts = pending.get("end_ts", 0)
        if now_ts < end_ts + 15:   # esperar 15s de buffer tras el cierre
            continue

        side      = pending.get("side", "UP")
        btc_start = pending.get("btc_start", 0.0)
        interval  = pending.get("interval", 5)
        slug      = pending.get("slug", "")

        # Obtener precio BTC al cierre de la ventana
        btc_end = await _get_btc_price_at_ts(end_ts)
        if not btc_end:
            # Fallback: usar precio live si han pasado más de 30s desde el cierre
            if now_ts > end_ts + 30:
                btc_end = state.btc_price or await get_btc_price()
                if btc_end:
                    _log("INFO", f"UpDown | Usando precio live ${btc_end:.0f} para resolver {slug} (kline no disponible)")
                else:
                    _log("WARN", f"UpDown | Sin precio BTC para resolver {slug}")
                    if now_ts > end_ts + interval * 60 * 2:
                        resolved_tokens.append(token_id)  # descartar después de 2 ventanas
                    continue
            else:
                continue  # esperar más tiempo

        if not (btc_start and btc_start > 0):
            # Sin precio de inicio no podemos determinar WIN/LOSS — contar como LOSS conservadoramente
            _log("WARN", f"UpDown | Sin btc_start para {slug} — contando como LOSS")
            _update_updown_loss_streak(interval, False, None)
            resolved_tokens.append(token_id)
            continue

        btc_went_up = btc_end >= btc_start
        won = (side == "UP") == btc_went_up
        direction = "SUBIO" if btc_went_up else "BAJO"
        _log(
            "INFO" if won else "WARN",
            f"UpDown {interval}m | {'WIN ✓' if won else 'LOSS ✗'} — BTC {direction} "
            f"${btc_start:.0f}→${btc_end:.0f} | apostamos {side}",
        )

        _update_updown_loss_streak(interval, won, None)
        resolved_tokens.append(token_id)

    for token_id in resolved_tokens:
        _updown_pending_outcomes.pop(token_id, None)

    # ── Resolver apuestas fantasma ───────────────────────────────────────────
    if not _updown_phantom_pending:
        return

    resolved_phantoms = []
    for ph_slug, pending in list(_updown_phantom_pending.items()):
        end_ts = pending.get("end_ts", 0)
        if now_ts < end_ts + 15:
            continue

        side      = pending.get("side", "UP")
        btc_start = pending.get("btc_start", 0.0)
        interval  = pending.get("interval", 5)

        btc_end = await _get_btc_price_at_ts(end_ts)
        if not btc_end:
            if now_ts > end_ts + 30:
                btc_end = state.btc_price or await get_btc_price()
                if not btc_end:
                    if now_ts > end_ts + interval * 60 * 2:
                        resolved_phantoms.append(ph_slug)
                    continue
            else:
                continue

        if not (btc_start and btc_start > 0):
            resolved_phantoms.append(ph_slug)
            continue

        btc_went_up = btc_end >= btc_start
        ph_won      = (side == "UP") == btc_went_up
        direction   = "SUBIO" if btc_went_up else "BAJO"

        _log(
            "INFO" if ph_won else "WARN",
            f"UpDown {interval}m | [PHANTOM] {'✓ HUBIERA GANADO' if ph_won else '✗ HUBIERA PERDIDO'} — "
            f"apostaba {side} | BTC {direction} ${btc_start:.0f}→${btc_end:.0f} "
            f"({(btc_end-btc_start)/btc_start*100:+.3f}%) | "
            f"conf={pending.get('confidence',0):.0f}% | skip={pending.get('skip_reason','?')[:40]}",
        )

        try:
            from updown_learner import record_phantom_result as _rph
            _rph(interval, pending, ph_won)
        except Exception as e:
            _log("WARN", f"UpDown phantom learner error: {e}")

        # ── Experimento VPS-Confianza: resolver trade ────────────────────────
        try:
            from vps_experiment import resolve_phantom_vps as _vps_res
            _vps_res(slug=ph_slug, btc_end=btc_end, won=ph_won)
        except Exception as _vps_err:
            _log("WARN", f"[VPS] Error resolviendo phantom: {_vps_err}")

        resolved_phantoms.append(ph_slug)

    for ph_slug in resolved_phantoms:
        _updown_phantom_pending.pop(ph_slug, None)


# ── BTC Auto-trading loop ──────────────────────────────────────────────────

_btc_auto_running: bool = False
_btc_auto_task: Optional[asyncio.Task] = None


async def _btc_auto_loop(interval_minutes: int):
    """Loop independiente: ejecuta _scan_btc_markets() cada interval_minutes."""
    global _btc_auto_running
    _log("INFO", f"BTC AUTO | Iniciado — intervalo: {interval_minutes} min")
    state.btc_auto_mode = True
    _btc_auto_running = True
    state.btc_scan_interval_minutes = interval_minutes

    # Escaneo inmediato al activar
    try:
        if bot_params.btc_enabled:
            await _scan_btc_markets()
        else:
            _log("INFO", "BTC AUTO | btc_enabled=False — scan omitido")
    except Exception as e:
        _log("ERROR", f"BTC AUTO | Error en primer escaneo: {e}")

    while _btc_auto_running:
        # Cuenta regresiva
        for remaining in range(interval_minutes * 60, 0, -1):
            if not _btc_auto_running:
                break
            state.btc_next_scan_in = remaining
            await asyncio.sleep(1)

        if not _btc_auto_running:
            break

        try:
            if not bot_params.btc_enabled:
                _log("INFO", "BTC AUTO | btc_enabled=False — ciclo omitido")
            else:
                state.btc_next_scan_in = 0
                await _scan_btc_markets()
        except Exception as e:
            _log("ERROR", f"BTC AUTO | Error en ciclo: {e}")

    state.btc_auto_mode = False
    state.btc_next_scan_in = 0
    _log("INFO", "BTC AUTO | Detenido")


def enable_btc_auto(interval_minutes: int = 5):
    """Activa el auto-trading de BTC con el intervalo dado."""
    global _btc_auto_running, _btc_auto_task
    _btc_auto_running = True
    if _btc_auto_task is None or _btc_auto_task.done():
        try:
            loop = asyncio.get_event_loop()
            _btc_auto_task = loop.create_task(_btc_auto_loop(interval_minutes))
        except RuntimeError:
            asyncio.ensure_future(_btc_auto_loop(interval_minutes))
    _log("INFO", f"BTC AUTO | enable_btc_auto({interval_minutes} min)")


def disable_btc_auto():
    """Detiene el auto-trading de BTC."""
    global _btc_auto_running
    _btc_auto_running = False
    state.btc_auto_mode = False
    state.btc_next_scan_in = 0
    _log("INFO", "BTC AUTO | disable_btc_auto()")


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
        except OSError as e:
            state.error_count += 1
            if e.errno == 34:  # Result too large — respuesta HTTP demasiado grande
                _log("WARN", "Ciclo de escaneo: respuesta demasiado grande (OSError 34) — reintentando en el próximo ciclo")
            else:
                _log("ERROR", f"Error en ciclo de escaneo: {e}")
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


async def _run_updown_loop():
    """
    Loop dedicado para UpDown: escanea cada 60s independientemente del ciclo principal.
    Necesario porque los mercados 5m/15m abren y cierran mucho más rápido que scan_interval.
    """
    await asyncio.sleep(15)  # esperar a que run_bot inicialice balance
    _updown_balance_tick = 0
    _updown_last_balance_refresh = 0.0  # timestamp de último refresh de balance
    _telonex_last_wallet_update = 0.0   # timestamp de último update_top_wallets
    while state.running:
        try:
            # Refrescar balance real desde Polymarket cada 3 min (no cada 60s).
            # Reduce API calls al CLOB de 60/hr → 20/hr sin impacto en capital tracking.
            import time as _time
            _now_mono = _time.monotonic()
            if _now_mono - _updown_last_balance_refresh >= 180:
                fresh = await asyncio.get_event_loop().run_in_executor(None, _get_balance)
                if fresh > 0:
                    state.balance_usdc = fresh
                _updown_last_balance_refresh = _now_mono

            # Calcular budget si el ciclo principal aún no lo ha hecho
            if state.balance_usdc > 0 and state.budget_updown == 0:
                if bot_params.betting_pool_usdc > 0:
                    state.budget_updown    = round(bot_params.bucket_updown_5m_usdc + bot_params.bucket_updown_15m_usdc, 2)
                    state.available_updown = state.budget_updown
                    _log("INFO", f"UpDown | Buckets: 5m=${bot_params.bucket_updown_5m_usdc:.2f} 15m=${bot_params.bucket_updown_15m_usdc:.2f}")
                else:
                    _dep = _calc_deployed_by_type()
                    state.deployed_weather = round(_dep.get("WEATHER",    0.0), 2)
                    state.deployed_btc     = round(_dep.get("BTC",        0.0), 2)
                    state.deployed_updown  = round(_dep.get("BTC_UPDOWN", 0.0), 2)
                    _total_dep = state.deployed_weather + state.deployed_btc + state.deployed_updown
                    total = state.balance_usdc + _total_dep
                    state.budget_updown    = round(total * bot_params.alloc_updown_pct, 2)
                    _ud_headroom           = max(0.0, state.budget_updown - state.deployed_updown)
                    state.available_updown = round(min(_ud_headroom, state.balance_usdc), 2)
                    _log("INFO", f"UpDown | Budget calculado: ${state.budget_updown:.2f} / headroom ${_ud_headroom:.2f} / disponible ${state.available_updown:.2f}")

            # Actualizar smart wallet ranking (Telonex) cada 2h
            if bot_params.telonex_enabled and _now_mono - _telonex_last_wallet_update >= 7200:
                try:
                    await telonex_data.update_top_wallets()
                    _telonex_last_wallet_update = _now_mono
                except Exception as _tw_err:
                    _log("WARN", f"Telonex wallet update error: {_tw_err}")

            # Cancelar órdenes GTC de UpDown que no se llenaron (>90s abiertas)
            await _cancel_stale_updown_orders()

            # Resolver trades pasados cuya ventana ya cerró
            await _resolve_pending_updown_outcomes()

            if bot_params.updown_5m_enabled:
                await _scan_updown(5)
            if bot_params.updown_15m_enabled:
                await _scan_updown(15)
        except Exception as e:
            _log("ERROR", f"UpDown loop error: {e}")
        await asyncio.sleep(60)


def start():
    if state.running:
        return
    state.running = True
    asyncio.create_task(run_bot())
    asyncio.create_task(_run_updown_loop())


def stop():
    state.running = False
    _log("INFO", "Senalizando detencion del bot...")
