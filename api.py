"""
API FastAPI: controla el bot y sirve la interfaz web.
"""
import asyncio
import json
import os
import signal
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import bot
from config import bot_params, settings
from performance_monitor import perf
from telonex_data import telonex_data

# ── Chat history storage ───────────────────────────────────────────────────
CHATS_FILE = Path(__file__).parent / "data" / "chats.json"

def _load_chats() -> dict:
    if CHATS_FILE.exists():
        try:
            return json.loads(CHATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_chats(chats: dict):
    CHATS_FILE.parent.mkdir(exist_ok=True)
    CHATS_FILE.write_text(json.dumps(chats, ensure_ascii=False, indent=2), encoding="utf-8")

app = FastAPI(title="Weatherbot Polymarket")

# Servir archivos estaticos
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# WebSocket connections activas
_ws_clients: list[WebSocket] = []


# ── helpers de broadcast ────────────────────────────────────────────────────

async def _broadcast(msg: dict):
    dead = []
    for ws in list(_ws_clients):
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


def _calc_goal_progress(total_value: float) -> dict:
    """Calcula el progreso hacia la meta de ganancia activa."""
    g = bot_params
    if not g.profit_goal_usdc or not g.profit_goal_start_iso:
        return {"active": False}
    try:
        start_dt = datetime.fromisoformat(g.profit_goal_start_iso)
        now = datetime.now(timezone.utc)
        elapsed_hours = (now - start_dt).total_seconds() / 3600
        hours_remaining = max(0.0, g.profit_goal_hours - elapsed_hours)
        earned = round(total_value - g.profit_goal_start_value, 2)
        pct = round(min(100.0, earned / g.profit_goal_usdc * 100), 1) if g.profit_goal_usdc > 0 else 0
        expired = elapsed_hours >= g.profit_goal_hours
        return {
            "active": True,
            "goal_usdc": g.profit_goal_usdc,
            "goal_hours": g.profit_goal_hours,
            "earned": earned,
            "pct": pct,
            "hours_remaining": round(hours_remaining, 2),
            "elapsed_hours": round(elapsed_hours, 2),
            "expired": expired,
            "reached": earned >= g.profit_goal_usdc,
            "start_iso": g.profit_goal_start_iso,
        }
    except Exception:
        return {"active": False}


def _build_status() -> dict:
    portfolio_value = round(sum(p["cur_value_usdc"] for p in bot.state.poly_positions), 2)
    portfolio_pnl   = round(sum(p["pnl_usdc"]       for p in bot.state.poly_positions), 2)
    total_value     = round(bot.state.balance_usdc + portfolio_value, 2)
    cash            = bot.state.balance_usdc
    # Deployed siempre fresco
    _dep = bot._calc_deployed_by_type()
    dep_w = round(_dep.get("WEATHER",    0.0), 2)
    dep_b = round(_dep.get("BTC",        0.0), 2)
    dep_u = round(_dep.get("BTC_UPDOWN", 0.0), 2)
    bot.state.deployed_weather = dep_w
    bot.state.deployed_btc     = dep_b
    bot.state.deployed_updown  = dep_u
    # Calcular disponibles
    if bot_params.betting_pool_usdc > 0:
        # Sistema de buckets
        bud_w  = round(bot_params.bucket_weather_usdc, 2)
        bud_b  = round(bot_params.bucket_btc_usdc, 2)
        bud_u  = round(bot_params.bucket_updown_5m_usdc + bot_params.bucket_updown_15m_usdc, 2)
        avail_w = bud_w
        avail_b = bud_b
        avail_u = bud_u
    else:
        bud_w = round(total_value * bot_params.alloc_weather_pct, 2)
        bud_b = round(total_value * bot_params.alloc_btc_pct,     2)
        bud_u = round(total_value * bot_params.alloc_updown_pct,  2)
        hw = max(0.0, bud_w - dep_w)
        hb = max(0.0, bud_b - dep_b)
        hu = max(0.0, bud_u - dep_u)
        total_h = hw + hb + hu
        ratio = min(1.0, cash / total_h) if total_h > 0 else 0.0
        avail_w = round(hw * ratio, 2)
        avail_b = round(hb * ratio, 2)
        avail_u = round(hu * ratio, 2)
    return {
        "running":             bot.state.running,
        "balance_usdc":        round(bot.state.balance_usdc, 2),
        "portfolio_value":     portfolio_value,
        "portfolio_pnl":       portfolio_pnl,
        "total_value":         total_value,
        "goal":                _calc_goal_progress(total_value),
        "daily_start_balance": round(bot.state.daily_start_balance, 2),
        "daily_loss_usdc":     round(bot.state.daily_loss_usdc, 2),
        "total_trades":        bot.state.total_trades,
        "total_pnl":           round(bot.state.total_pnl, 2),
        "last_scan":           bot.state.last_scan,
        "error_count":         bot.state.error_count,
        "opportunities_count": len(bot.state.opportunities),
        "btc_price":               bot.state.btc_price,
        "btc_opportunities_count": len(bot.state.btc_opportunities),
        "btc_ta":                  bot.state.btc_ta,
        "btc_cmc":                 bot.state.btc_cmc,
        "btc_auto_mode":           bot.state.btc_auto_mode,
        "btc_scan_interval_minutes": bot.state.btc_scan_interval_minutes,
        "btc_next_scan_in":        bot.state.btc_next_scan_in,
        "auto_trade_mode":         bot.state.auto_trade_mode,
        # Capital allocation — siempre calculado en vivo desde el balance actual
        "alloc_weather_pct":  bot_params.alloc_weather_pct,
        "alloc_btc_pct":      bot_params.alloc_btc_pct,
        "alloc_updown_pct":   bot_params.alloc_updown_pct,
        "budget_weather":     bud_w,
        "budget_btc":         bud_b,
        "budget_updown":      bud_u,
        "deployed_weather":   dep_w,
        "deployed_btc":       dep_b,
        "deployed_updown":    dep_u,
        "available_weather":  avail_w,
        "available_btc":      avail_b,
        "available_updown":   avail_u,
        # Sistema de buckets (Fase 12)
        "capital_buckets_active": bot_params.betting_pool_usdc > 0,
        "cash_free":          bot.state.cash_free,
        "betting_pool_usdc":  round(bot_params.betting_pool_usdc, 2),
        "bucket_weather_usdc":    round(bot_params.bucket_weather_usdc, 2),
        "bucket_btc_usdc":        round(bot_params.bucket_btc_usdc, 2),
        "bucket_updown_5m_usdc":  round(bot_params.bucket_updown_5m_usdc, 2),
        "bucket_updown_15m_usdc": round(bot_params.bucket_updown_15m_usdc, 2),
        # Tipos de trade habilitados
        "weather_enabled":              bot_params.weather_enabled,
        "btc_enabled":                  bot_params.btc_enabled,
        # UpDown — control
        "updown_5m_enabled":            bot_params.updown_5m_enabled,
        "updown_15m_enabled":           bot_params.updown_15m_enabled,
        "updown_1d_enabled":            getattr(bot_params, "updown_1d_enabled", False),
        "updown_5m_stopped":            bot.state.updown_5m_stopped,
        "updown_15m_stopped":           bot.state.updown_15m_stopped,
        "updown_5m_consecutive_losses": bot.state.updown_5m_consecutive_losses,
        "updown_15m_consecutive_losses":bot.state.updown_15m_consecutive_losses,
        "updown_max_usdc":              bot_params.updown_max_usdc,
        "updown_max_consecutive_losses":bot_params.updown_max_consecutive_losses,
        "updown_stake_min_usdc":        bot_params.updown_stake_min_usdc,
        "updown_stake_max_usdc":        bot_params.updown_stake_max_usdc,
        "updown_stake_conf_min_pct":    bot_params.updown_stake_conf_min_pct,
        "updown_stake_conf_max_pct":    bot_params.updown_stake_conf_max_pct,
        "updown_displacement_hi_pct":   bot_params.updown_displacement_hi_pct,
        "updown_displacement_lo_pct":   bot_params.updown_displacement_lo_pct,
        # UpDown — live data
        "updown_last_market_5m":  bot.state.updown_last_market_5m,
        "updown_last_market_15m": bot.state.updown_last_market_15m,
        "updown_last_opp_5m":     bot.state.updown_last_opp_5m,
        "updown_last_opp_15m":    bot.state.updown_last_opp_15m,
        "updown_last_trade_5m":   bot.state.updown_last_trade_5m,
        "updown_last_trade_15m":  bot.state.updown_last_trade_15m,
        "updown_recent_trades":   bot.state.updown_recent_trades,
        # Todos los parámetros del bot (para la sección Estrategias)
        "bot_params":             bot_params.to_dict(),
        # Versión del proyecto
        "version":                __import__("version").SHORT_LABEL,
        "version_full":           __import__("version").FULL_LABEL,
    }


def _sync_log_cb(entry: dict):
    """Callback sincrono que schedula el broadcast de logs async."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_broadcast({"type": "log", "data": entry}))
    except Exception:
        pass


bot.add_log_callback(_sync_log_cb)


# ── tarea de fondo: push de datos en tiempo real ───────────────────────────

async def _realtime_broadcaster():
    """
    Empuja actualizaciones a todos los clientes WebSocket conectados:
    - Status (balance, portfolio, P&L): cada 5s
    - Posiciones Polymarket: cada 15s
    - Ordenes abiertas: cada 15s
    - Oportunidades: cada 30s
    """
    from markets import get_polymarket_positions

    tick = 0
    while True:
        await asyncio.sleep(5)
        tick += 1

        if not _ws_clients:
            continue

        # Status siempre (cada 5s)
        await _broadcast({"type": "status", "data": _build_status()})

        # Posiciones, ordenes y balance real (cada 10s = cada 2 ticks)
        if tick % 2 == 0 and settings.poly_wallet_address:
            positions = await get_polymarket_positions(settings.poly_wallet_address)
            if positions:
                bot.state.poly_positions = positions
            await _broadcast({"type": "positions", "data": bot.state.poly_positions})

            orders = await asyncio.get_event_loop().run_in_executor(None, bot._fetch_open_orders)
            bot.state.open_orders = orders
            await _broadcast({"type": "open_orders", "data": bot.state.open_orders})

            # Refrescar balance USDC real desde Polymarket (no usar valor en cache)
            fresh_balance = await asyncio.get_event_loop().run_in_executor(None, bot._get_balance)
            if fresh_balance > 0:
                bot.state.balance_usdc = fresh_balance

        # Oportunidades clima (cada 30s = cada 6 ticks)
        if tick % 6 == 0:
            await _broadcast({"type": "opportunities",     "data": bot.state.opportunities})
            await _broadcast({"type": "btc_opportunities", "data": bot.state.btc_opportunities})


# ── startup ────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    from markets import get_polymarket_positions

    if settings.poly_wallet_address:
        positions = await get_polymarket_positions(settings.poly_wallet_address)
        if positions:
            bot.state.poly_positions = positions

        ok = await asyncio.get_event_loop().run_in_executor(None, bot._init_clob_client)
        if ok:
            orders = await asyncio.get_event_loop().run_in_executor(None, bot._fetch_open_orders)
            bot.state.open_orders = orders

    # Arrancar broadcaster en background
    asyncio.create_task(_realtime_broadcaster())
    # Arrancar análisis proactivo de Claude
    asyncio.create_task(_proactive_advisor_loop())


# ── Análisis proactivo de Claude ──────────────────────────────────────────

_PROACTIVE_INTERVAL_H = 0.5    # horas entre análisis proactivos (30 min)
_proactive_last_run: datetime | None = None

_PROACTIVE_SYSTEM = """Eres el asesor proactivo de WeatherBot. Analiza el estado completo del sistema y detecta:

1. RIESGO: pérdidas consecutivas excesivas, concentración en pocas posiciones, capital ocioso
2. CONFIGURACIÓN: parámetros que parecen subóptimos dado el historial (EV mínimo, Kelly, allocations)
3. APRENDIZAJE: si el updown learner muestra patrones claros (phantom bets que hubieran ganado, señales que fallan, etc.)
4. OPORTUNIDADES: si hay algo que el bot debería hacer diferente

RESPONDE en una de estas dos formas:
- Si no hay nada importante: responde EXACTAMENTE "SIN_ALERTAS"
- Si hay algo que el usuario debería saber: da un análisis conciso (máx 5 líneas) con tu hallazgo principal.

Si sugieres cambiar parámetros, incluye AL FINAL tu sugerencia en este formato exacto (en la misma respuesta):
CAMBIO_SUGERIDO: [descripción breve de qué cambiar y por qué]
PARAMS_JSON: {"key": value}

No incluyas PARAMS_JSON si no tienes una sugerencia concreta de parámetros.
Responde en español."""


async def _run_proactive_analysis() -> str | None:
    """
    Pide a Claude que analice el sistema proactivamente.
    Retorna el texto de la notificación, o None si no hay alertas.
    """
    from claude_analyst import get_client as _get_client
    import os as _os, json as _json

    client = _get_client()
    if client is None:
        return None

    model = _os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    context = _build_chat_context(include_logs=True, include_learner=True)

    try:
        resp = await client.messages.create(
            model=model,
            max_tokens=600,
            system=_PROACTIVE_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Analiza el estado actual del sistema:\n\n{context}",
            }],
        )
        text = resp.content[0].text.strip()
        if text == "SIN_ALERTAS" or "SIN_ALERTAS" in text.upper():
            return None
        return text
    except Exception:
        return None


async def _proactive_advisor_loop():
    """Loop de fondo: análisis proactivo cada _PROACTIVE_INTERVAL_H horas."""
    global _proactive_last_run
    # Esperar 3 minutos antes del primer análisis para que el bot se estabilice
    await asyncio.sleep(180)
    while True:
        try:
            _proactive_last_run = datetime.now(timezone.utc)
            msg = await _run_proactive_analysis()
            if msg:
                _store_advisor_notification(msg)
                await _broadcast({
                    "type": "advisor_notification",
                    "data": {
                        "text": msg,
                        "time": _proactive_last_run.strftime("%Y-%m-%d %H:%M UTC"),
                    },
                })
        except Exception:
            pass
        await asyncio.sleep(_PROACTIVE_INTERVAL_H * 3600)


_ADVISOR_NOTIF_FILE = Path(__file__).parent / "data" / "advisor_notifications.json"


def _store_advisor_notification(text: str):
    """Guarda la notificación proactiva en disco para que persista entre recargas."""
    try:
        notifs = []
        if _ADVISOR_NOTIF_FILE.exists():
            notifs = json.loads(_ADVISOR_NOTIF_FILE.read_text(encoding="utf-8"))
        notifs.insert(0, {
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "text": text,
            "read": False,
        })
        notifs = notifs[:20]  # mantener últimas 20
        _ADVISOR_NOTIF_FILE.parent.mkdir(exist_ok=True)
        _ADVISOR_NOTIF_FILE.write_text(json.dumps(notifs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/api/advisor/notifications")
async def get_advisor_notifications():
    """Devuelve las notificaciones proactivas de Claude."""
    try:
        if _ADVISOR_NOTIF_FILE.exists():
            data = json.loads(_ADVISOR_NOTIF_FILE.read_text(encoding="utf-8"))
            return {"notifications": data, "unread": sum(1 for n in data if not n.get("read"))}
    except Exception:
        pass
    return {"notifications": [], "unread": 0}


@app.post("/api/advisor/notifications/read")
async def mark_notifications_read():
    """Marca todas las notificaciones como leídas."""
    try:
        if _ADVISOR_NOTIF_FILE.exists():
            data = json.loads(_ADVISOR_NOTIF_FILE.read_text(encoding="utf-8"))
            for n in data:
                n["read"] = True
            _ADVISOR_NOTIF_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/advisor/analyze-now")
async def advisor_analyze_now():
    """Fuerza un análisis proactivo inmediato."""
    msg = await _run_proactive_analysis()
    if msg:
        _store_advisor_notification(msg)
        _proactive_last_run_val = datetime.now(timezone.utc)
        await _broadcast({
            "type": "advisor_notification",
            "data": {"text": msg, "time": _proactive_last_run_val.strftime("%Y-%m-%d %H:%M UTC")},
        })
        return {"ok": True, "notification": msg}
    return {"ok": True, "notification": None, "msg": "Sin alertas en este momento"}


# ── endpoints REST ─────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/version")
async def get_version():
    from version import VERSION, PHASE, PHASE_NAME, FULL_LABEL, SHORT_LABEL, PHASES
    return {
        "version":     VERSION,
        "phase":       PHASE,
        "phase_name":  PHASE_NAME,
        "full_label":  FULL_LABEL,
        "short_label": SHORT_LABEL,
        "phases":      {str(k): v for k, v in PHASES.items()},
    }


@app.get("/api/status")
async def get_status():
    return _build_status()


@app.post("/api/start")
async def start_bot():
    if bot.state.running:
        return {"ok": False, "msg": "Bot ya esta corriendo"}
    bot.start()
    return {"ok": True, "msg": "Bot iniciado"}


@app.post("/api/stop")
async def stop_bot():
    bot.stop()
    return {"ok": True, "msg": "Bot detenido"}


@app.post("/api/shutdown")
async def shutdown_server():
    bot.stop()
    asyncio.get_event_loop().call_later(1, lambda: os.kill(os.getpid(), signal.SIGTERM))
    return {"ok": True, "msg": "Servidor apagandose..."}


@app.get("/api/debug/updown")
async def debug_updown():
    """Diagnóstico completo — raw event data + trace de filtros."""
    from datetime import timezone as _tz
    import httpx as _httpx
    GAMMA = "https://gamma-api.polymarket.com"
    HDRS  = {"User-Agent": "WeatherbotPolymarket/1.0"}
    results = {}
    for interval in [5, 15]:
        interval_seconds = interval * 60
        now = datetime.now(_tz.utc)
        now_ts = int(now.timestamp())
        def next_b(ts): return ((ts // interval_seconds) + 1) * interval_seconds
        report = {"now_utc": now.isoformat(), "now_ts": now_ts, "events": []}
        async with _httpx.AsyncClient() as client:
            for offset in range(4):
                boundary = next_b(now_ts) + offset * interval_seconds
                slug = f"btc-updown-{interval}m-{boundary}"
                try:
                    r = await client.get(f"{GAMMA}/events", params={"slug": slug}, headers=HDRS, timeout=10)
                    data = r.json()
                    if not data:
                        report["events"].append({"slug": slug, "found": False})
                        continue
                    ev = data[0] if isinstance(data, list) else data
                    end_str = ev.get("endDate", "")
                    markets  = ev.get("markets", [])
                    m = markets[0] if markets else {}

                    # Calcular minutes_to_close
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        mtc = round((end_dt - now).total_seconds() / 60, 2)
                    except Exception:
                        mtc = None

                    # Raw market fields
                    raw_m = {k: m.get(k) for k in ["conditionId","acceptingOrders","outcomePrices","clobTokenIds","outcomes","bestBid","bestAsk","liquidityNum"]}

                    # Filter trace
                    reject = None
                    if not end_str:           reject = "NO endDate"
                    elif mtc is None:         reject = "endDate parse failed"
                    elif mtc < 0.5:           reject = f"too_close: {mtc}m < 0.5"
                    elif mtc > interval*2+3:  reject = f"too_far: {mtc}m > {interval*2+3}"
                    elif not markets:         reject = "no markets array"
                    elif not m.get("acceptingOrders"): reject = f"acceptingOrders={m.get('acceptingOrders')}"

                    report["events"].append({
                        "slug": slug, "offset": offset, "found": True,
                        "endDate": end_str, "minutes_to_close": mtc,
                        "reject_reason": reject,
                        "PASS": reject is None,
                        "market_raw": raw_m,
                    })
                except Exception as ex:
                    report["events"].append({"slug": slug, "error": str(ex)})
        results[f"{interval}m"] = report
    return results


@app.get("/api/params")
async def get_params():
    return bot_params.to_dict()


@app.post("/api/params")
async def update_params(data: dict):
    try:
        bot_params.update(data)
        return {"ok": True, "params": bot_params.to_dict()}
    except Exception as e:
        return JSONResponse(status_code=400, content={"ok": False, "msg": str(e)})


@app.get("/api/logs")
async def get_logs():
    return bot.get_log_history()


@app.post("/api/sell")
async def sell_position(data: dict):
    """Venta manual de una posicion desde la UI."""
    token_id = data.get("token_id", "")
    size     = float(data.get("size", 0))
    title    = data.get("title", "")
    if not token_id or size <= 0:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "token_id y size requeridos"})
    success = await asyncio.get_event_loop().run_in_executor(
        None, lambda: bot._sell_position(token_id, size, title, "Venta manual desde UI")
    )
    return {"ok": success, "msg": "Venta ejecutada" if success else "Venta fallida — revisa los logs"}


@app.get("/api/poly-positions")
async def get_poly_positions():
    return bot.state.poly_positions


@app.get("/api/price-history/{token_id:path}")
async def get_price_history(token_id: str):
    """Proxy del historial de precios de Polymarket CLOB (evita CORS en el browser)."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://clob.polymarket.com/prices-history",
                params={"market": token_id, "interval": "all", "fidelity": "60"},
                headers={"User-Agent": "WeatherbotPolymarket/1.0"},
                timeout=12,
            )
            if resp.status_code != 200:
                return JSONResponse(status_code=resp.status_code, content={"history": []})
            return resp.json()
    except Exception as e:
        return JSONResponse(status_code=500, content={"history": [], "error": str(e)})


@app.get("/api/portfolio-analysis")
async def get_portfolio_analysis():
    last = bot.state.last_portfolio_analysis
    if last:
        from datetime import datetime, timezone
        hours_since = (datetime.now(timezone.utc) - last).total_seconds() / 3600
        next_in_h = max(0, 12 - hours_since)
        last_str = last.strftime("%d/%m %H:%M UTC")
    else:
        next_in_h = 0
        last_str = None
    return {
        "analysis":         bot.state.portfolio_analysis,
        "recommendations":  bot.state.portfolio_recommendations,
        "last_analysis_at": last_str,
        "next_in_hours":    round(next_in_h, 1),
    }


@app.post("/api/portfolio-analysis/refresh")
async def refresh_portfolio_analysis():
    """Solicita un análisis de portafolio inmediato sin esperar el ciclo de 12h."""
    from claude_analyst import analyze_portfolio
    from datetime import datetime, timezone

    if not bot.state.poly_positions:
        return {"ok": False, "msg": "No hay posiciones abiertas para analizar"}

    result = await analyze_portfolio(bot.state.poly_positions, bot.state.balance_usdc,
                                     bot._build_capital_context())
    if result["skipped"]:
        return {"ok": False, "msg": "Claude API no configurada (falta ANTHROPIC_API_KEY)"}

    bot.state.portfolio_analysis = result["analysis"]
    bot.state.portfolio_recommendations = result.get("recommendations", [])
    bot.state.last_portfolio_analysis = datetime.now(timezone.utc)

    last_str = bot.state.last_portfolio_analysis.strftime("%d/%m %H:%M UTC")
    return {
        "ok":              True,
        "analysis":        result["analysis"],
        "recommendations": result.get("recommendations", []),
        "last_analysis_at": last_str,
        "next_in_hours":   12.0,
    }


@app.get("/api/open-orders")
async def get_open_orders():
    return bot.state.open_orders


@app.get("/api/opportunities")
async def get_opportunities():
    return bot.state.opportunities


@app.get("/api/btc-opportunities")
async def get_btc_opportunities():
    return bot.state.btc_opportunities


@app.get("/api/btc-price")
async def get_btc_price_endpoint():
    return {"price": bot.state.btc_price}


@app.post("/api/btc/auto")
async def set_btc_auto(data: dict):
    """Activa o desactiva el auto-trading de BTC."""
    enabled  = data.get("enabled", False)
    interval = max(1, min(60, int(data.get("interval_minutes", 5))))
    if enabled:
        bot.enable_btc_auto(interval)
        return {"ok": True, "msg": f"Auto-trading BTC activado — cada {interval} min"}
    else:
        bot.disable_btc_auto()
        return {"ok": True, "msg": "Auto-trading BTC desactivado"}


@app.post("/api/auto-trade")
async def set_auto_trade(data: dict):
    """Activa o desactiva el modo auto-trade (omite aprobación de Claude)."""
    enabled = bool(data.get("enabled", False))
    bot.state.auto_trade_mode = enabled
    status = "ACTIVADO" if enabled else "DESACTIVADO"
    bot._log("WARN" if enabled else "INFO",
             f"Modo AUTO-TRADE {status} {'⚡ — trades se ejecutan sin confirmación' if enabled else ''}")
    return {"ok": True, "auto_trade_mode": enabled}


@app.post("/api/btc/scan")
async def btc_manual_scan():
    """Dispara un escaneo BTC inmediato (sin esperar al ciclo)."""
    asyncio.create_task(bot._scan_btc_markets())
    return {"ok": True, "msg": "Escaneo BTC iniciado"}


@app.post("/api/toggle-trade-type")
async def toggle_trade_type(data: dict):
    """Activa o desactiva un tipo de trade: weather, btc, updown_5m, updown_15m."""
    trade_type = data.get("type", "")
    enabled    = bool(data.get("enabled", True))

    key_map = {
        "weather":   "weather_enabled",
        "btc":       "btc_enabled",
        "updown_5m": "updown_5m_enabled",
        "updown_15m":"updown_15m_enabled",
        "updown_1d": "updown_1d_enabled",
    }
    key = key_map.get(trade_type)
    if not key:
        return {"ok": False, "msg": f"Tipo desconocido: {trade_type}"}

    bot_params.update({key: enabled})
    label = {
        "weather":   "⛅ Weather",
        "btc":       "₿ BTC above/below",
        "updown_5m": "⚡ UpDown 5m",
        "updown_15m":"⚡ UpDown 15m",
        "updown_1d": "⚡ UpDown 1d",
    }[trade_type]
    estado = "ACTIVADO" if enabled else "DESACTIVADO"
    bot._log("INFO", f"Tipo de trade {label} → {estado}")
    return {"ok": True, "enabled": enabled, "msg": f"{label} {estado}"}


@app.post("/api/updown/reset")
async def reset_updown(data: dict):
    """Reactiva un mercado UpDown detenido por pérdidas consecutivas."""
    interval = int(data.get("interval_minutes", 5))
    if interval == 5:
        bot.state.updown_5m_stopped = False
        bot.state.updown_5m_consecutive_losses = 0
        bot._log("INFO", "UpDown 5m | Pérdidas reiniciadas — mercado reactivado")
        return {"ok": True, "msg": "UpDown 5m reactivado"}
    else:
        bot.state.updown_15m_stopped = False
        bot.state.updown_15m_consecutive_losses = 0
        bot._log("INFO", "UpDown 15m | Pérdidas reiniciadas — mercado reactivado")
        return {"ok": True, "msg": "UpDown 15m reactivado"}


@app.post("/api/updown/params")
async def set_updown_params(data: dict):
    """Actualiza parámetros del UpDown (max_usdc, enabled)."""
    clean = {}
    if "updown_max_usdc" in data:
        clean["updown_max_usdc"] = max(0.1, float(data["updown_max_usdc"]))
    if "updown_5m_enabled" in data:
        clean["updown_5m_enabled"] = bool(data["updown_5m_enabled"])
    if "updown_15m_enabled" in data:
        clean["updown_15m_enabled"] = bool(data["updown_15m_enabled"])
    if "updown_max_consecutive_losses" in data:
        clean["updown_max_consecutive_losses"] = max(1, int(data["updown_max_consecutive_losses"]))
    if "updown_stake_min_usdc" in data:
        clean["updown_stake_min_usdc"] = max(0.5, float(data["updown_stake_min_usdc"]))
    if "updown_stake_max_usdc" in data:
        clean["updown_stake_max_usdc"] = max(0.5, float(data["updown_stake_max_usdc"]))
    if "updown_stake_conf_min_pct" in data:
        clean["updown_stake_conf_min_pct"] = max(0.0, min(99.0, float(data["updown_stake_conf_min_pct"])))
    if "updown_stake_conf_max_pct" in data:
        clean["updown_stake_conf_max_pct"] = max(1.0, min(100.0, float(data["updown_stake_conf_max_pct"])))
    if "updown_displacement_hi_pct" in data:
        clean["updown_displacement_hi_pct"] = max(0.01, min(5.0, float(data["updown_displacement_hi_pct"])))
    if "updown_displacement_lo_pct" in data:
        clean["updown_displacement_lo_pct"] = max(0.01, min(5.0, float(data["updown_displacement_lo_pct"])))
    if clean:
        bot_params.update(clean)  # update() llama a save() internamente
    return {"ok": True}


@app.get("/api/updown/learn")
async def get_updown_learn():
    """Estadísticas de aprendizaje adaptativo por intervalo."""
    try:
        from updown_learner import get_summary
        return {
            "5m":  get_summary(5),
            "15m": get_summary(15),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/risk/status")
async def get_risk_status():
    """Estado actual del risk manager: circuit breaker activo, drawdown semanal."""
    try:
        from risk_manager import risk_manager as _rm, MAX_WEEKLY_DRAWDOWN_PCT
        from config import bot_params
        return {
            "circuit_breaker_enabled": bot_params.circuit_breaker_enabled,
            "circuit_breaker_active":  _rm.circuit_breaker_active,
            "circuit_breaker_reason":  _rm.circuit_breaker_reason,
            "weekly_start_value":      getattr(_rm, "weekly_start_value", None),
            "weekly_drawdown_limit":   MAX_WEEKLY_DRAWDOWN_PCT,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/risk/reset-circuit-breaker")
async def reset_risk_circuit_breaker():
    """Desactiva manualmente el circuit breaker del risk manager."""
    try:
        from risk_manager import risk_manager as _rm
        _rm.reset_circuit_breaker()
        return {"ok": True, "message": "Circuit breaker desactivado — el bot puede volver a operar."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/risk/toggle-circuit-breaker")
async def toggle_circuit_breaker(body: dict):
    """Activa o desactiva el circuit breaker (feature flag)."""
    try:
        from config import bot_params
        from risk_manager import risk_manager as _rm
        enable = bool(body.get("enabled", False))
        bot_params.circuit_breaker_enabled = enable
        bot_params.save()
        # Si se desactiva, limpiar cualquier CB activo
        if not enable and _rm.circuit_breaker_active:
            _rm.reset_circuit_breaker()
        state = "ACTIVADO" if enable else "DESACTIVADO"
        return {"ok": True, "circuit_breaker_enabled": enable, "message": f"Circuit breaker {state}."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/bots/param-history/{bot_id}")
async def get_bot_param_history(bot_id: str, limit: int = 50):
    """Historial de cambios de parámetros aplicados a un bot desde el chat."""
    history = _load_param_history()
    entries = history.get(bot_id, [])
    return {"bot_id": bot_id, "entries": entries[:limit]}


@app.get("/api/bots/optimizer-status")
async def get_optimizer_status():
    """Estado del optimizador autónomo para los bots phantom (5m y 15m)."""
    try:
        from phantom_optimizer import get_status as _opt_status
        return {
            "ph5m":  _opt_status(5),
            "ph15m": _opt_status(15),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/bots/stats")
async def get_bots_stats():
    """
    Fuente única de verdad para WR de los 4 bots.
    - Real bots (ud5m, ud15m): updown_learner (datos reales completos)
    - Phantom bots (ph5m, ph15m): updown_learner .phantom (historial completo)
      phantom_learner solo para adaptive params (strategy_hint, insights, tier)
    """
    try:
        from updown_learner import get_summary as _ud_sum
        ud5  = _ud_sum(5)
        ud15 = _ud_sum(15)

        # Phantom: updown_learner.phantom tiene el historial COMPLETO
        # phantom_learner solo se usa para adaptive params / insights
        def _ph_stats(ud_summary: dict, interval: int) -> dict:
            ph = ud_summary.get("phantom") or {}
            total = ph.get("total", 0)
            wins  = ph.get("wins",  0)
            # recent WR desde phantom sub-stats
            ph_recent = ph.get("recent_wr")
            # adaptive params: preferir phantom_learner (tiene strategy_hint, insights)
            ap = {}
            try:
                from phantom_learner import get_adaptive_params as _pl_ap
                ap = _pl_ap(interval)
            except Exception:
                pass
            # fallback: adaptive del learner updown
            if not ap:
                ap = ud_summary.get("adaptive", {})
            return {
                "total":     total,
                "wins":      wins,
                "win_rate":  round(wins / total, 3) if total > 0 else None,
                "recent_wr": ph_recent,
                "by_side":   ph.get("by_side", {}),
                "by_signal": ph.get("by_signal", {}),
                "by_elapsed": ph.get("by_elapsed", {}),
                "adaptive":  ap,
            }

        ph5  = _ph_stats(ud5,  5)
        ph15 = _ph_stats(ud15, 15)

        # Trading Mode bots (phantom + real combined into per-interval bot)
        try:
            import trading_positions as _tp
            tm5_ph  = _tp.stats_by_interval(is_real=False, interval=5)
            tm15_ph = _tp.stats_by_interval(is_real=False, interval=15)
            tm1d_ph = _tp.stats_by_interval(is_real=False, interval=1440)
            tm5_rl  = _tp.stats_by_interval(is_real=True,  interval=5)
            tm15_rl = _tp.stats_by_interval(is_real=True,  interval=15)
            tm1d_rl = _tp.stats_by_interval(is_real=True,  interval=1440)

            try:
                import trading_learner as _tl
            except Exception:
                _tl = None

            def _tm_card(ph: dict, rl: dict, interval: int) -> dict:
                ap = {}
                if _tl:
                    try:
                        ap = _tl.get_adaptive_params(interval)
                    except Exception:
                        ap = {}
                return {
                    "phantom": ph,
                    "real":    rl,
                    "total":     (ph.get("total")  or 0) + (rl.get("total")  or 0),
                    "wins":      (ph.get("wins")   or 0) + (rl.get("wins")   or 0),
                    "losses":    (ph.get("losses") or 0) + (rl.get("losses") or 0),
                    "win_rate":  ph.get("win_rate"),
                    "recent_wr": ph.get("recent_wr"),
                    "by_side":   ph.get("by_side", {}),
                    "realized_pnl_phantom": ph.get("realized_pnl", 0),
                    "realized_pnl_real":    rl.get("realized_pnl", 0),
                    "adaptive":  ap,
                }
            tm5  = _tm_card(tm5_ph,  tm5_rl,  5)
            tm15 = _tm_card(tm15_ph, tm15_rl, 15)
            tm1d = _tm_card(tm1d_ph, tm1d_rl, 1440)
        except Exception:
            tm5 = tm15 = tm1d = {}

        return {
            "ud5m":  {"total": ud5.get("total",0), "wins": ud5.get("wins",0),
                      "win_rate": ud5.get("win_rate"), "recent_wr": ud5.get("recent_wr"),
                      "by_side": ud5.get("by_side",{}), "by_signal": ud5.get("by_signal",{}),
                      "by_elapsed": ud5.get("by_elapsed",{}), "adaptive": ud5.get("adaptive",{})},
            "ud15m": {"total": ud15.get("total",0), "wins": ud15.get("wins",0),
                      "win_rate": ud15.get("win_rate"), "recent_wr": ud15.get("recent_wr"),
                      "by_side": ud15.get("by_side",{}), "by_signal": ud15.get("by_signal",{}),
                      "by_elapsed": ud15.get("by_elapsed",{}), "adaptive": ud15.get("adaptive",{})},
            "ph5m":  ph5,
            "ph15m": ph15,
            "tm5m":  tm5,
            "tm15m": tm15,
            "tm1d":  tm1d,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/trading/stats-by-interval")
async def get_trading_stats_by_interval():
    """Stats Trading Mode segregados por interval (5/15/1440) y por modo (phantom/real)."""
    try:
        import trading_positions as _tp
        return {
            "phantom": {
                "5m":  _tp.stats_by_interval(is_real=False, interval=5),
                "15m": _tp.stats_by_interval(is_real=False, interval=15),
                "1d":  _tp.stats_by_interval(is_real=False, interval=1440),
                "all": _tp.stats_by_interval(is_real=False, interval=None),
            },
            "real": {
                "5m":  _tp.stats_by_interval(is_real=True, interval=5),
                "15m": _tp.stats_by_interval(is_real=True, interval=15),
                "1d":  _tp.stats_by_interval(is_real=True, interval=1440),
                "all": _tp.stats_by_interval(is_real=True, interval=None),
            },
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/trading/dashboard")
async def get_trading_dashboard():
    """Punto 9 — KPIs agregados Trading Mode (phantom + real, por TF, hoy + total)."""
    try:
        import datetime as _dt
        import trading_positions as _tp

        midnight_ts = int(_dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

        def _today_stats(is_real: bool):
            wins = losses = trades = 0
            pnl = 0.0
            with _tp._LOCK:
                state = _tp._load()
                bucket = "real" if is_real else "phantom"
                for plist in state[bucket].values():
                    for p in plist:
                        if p.get("status") == "OPEN":
                            continue
                        ets = int(p.get("exit_ts") or 0)
                        if ets < midnight_ts:
                            continue
                        trades += 1
                        pn = float(p.get("pnl_usdc") or 0)
                        pnl += pn
                        st = p.get("status")
                        is_win = st in ("TARGET_HIT", "RESOLVED_WIN") or (st == "FORCED_EXIT" and pn >= 0)
                        if is_win:
                            wins += 1
                        else:
                            losses += 1
            wr = round(wins / (wins + losses) * 100, 2) if (wins + losses) else None
            return {"trades": trades, "wins": wins, "losses": losses, "win_rate": wr, "pnl_usdc": round(pnl, 4)}

        ph_today = _today_stats(False)
        rl_today = _today_stats(True)
        ph_all   = _tp.stats_by_interval(is_real=False, interval=None)
        rl_all   = _tp.stats_by_interval(is_real=True,  interval=None)
        meta     = _tp.get_meta()

        # mejor/peor interval por WR (phantom)
        per_tf = {iv: _tp.stats_by_interval(is_real=False, interval=iv) for iv in (5, 15, 1440)}
        ranked = [(k, v.get("win_rate"), v.get("realized_pnl", 0.0), v.get("closed", 0))
                  for k, v in per_tf.items() if v.get("closed", 0) > 0]
        ranked_wr = sorted([r for r in ranked if r[1] is not None], key=lambda x: x[1], reverse=True)
        best = ranked_wr[0] if ranked_wr else None
        worst = ranked_wr[-1] if ranked_wr else None
        ranked_pnl = sorted(ranked, key=lambda x: x[2], reverse=True)
        most_profitable = ranked_pnl[0] if ranked_pnl else None

        return {
            "phantom": {
                "balance":        meta.get("phantom_balance", 0.0),
                "today":          ph_today,
                "all_time":       {"trades": ph_all["closed"], "wr": ph_all["win_rate"], "pnl": ph_all["realized_pnl"]},
                "open":           ph_all["open"],
                "by_tf":          {("1d" if iv == 1440 else f"{iv}m"): {"wr": v["win_rate"], "pnl": v["realized_pnl"], "trades": v["closed"]} for iv, v in per_tf.items()},
                "best_tf":        ({"tf": ("1d" if best[0] == 1440 else f"{best[0]}m"), "wr": best[1], "pnl": best[2], "trades": best[3]} if best else None),
                "worst_tf":       ({"tf": ("1d" if worst[0] == 1440 else f"{worst[0]}m"), "wr": worst[1], "pnl": worst[2], "trades": worst[3]} if worst else None),
                "most_profit_tf": ({"tf": ("1d" if most_profitable[0] == 1440 else f"{most_profitable[0]}m"), "wr": most_profitable[1], "pnl": most_profitable[2], "trades": most_profitable[3]} if most_profitable else None),
            },
            "real": {
                "exposure":   _tp.real_exposure_usdc(),
                "today":      rl_today,
                "all_time":   {"trades": rl_all["closed"], "wr": rl_all["win_rate"], "pnl": rl_all["realized_pnl"]},
                "open":       rl_all["open"],
                "consec_losses": _tp.real_consecutive_losses(),
                "pending_redeem": _tp.real_pending_redemption_usdc(48),
            },
            "params": {
                "trading_mode_enabled": bool(getattr(bot_params, "trading_mode_enabled", False)),
                "trading_real_enabled": bool(getattr(bot_params, "trading_real_enabled", False)),
                "killed":               bool(getattr(bot_params, "trading_real_killed", False)),
            },
        }
    except Exception as e:
        return {"error": str(e)}


# ── Estrategias: rendimiento + notas ──────────────────────────────────────────

_NOTES_FILE = Path(__file__).parent / "data" / "strategy_notes.json"


def _load_notes() -> dict:
    try:
        if _NOTES_FILE.exists():
            return json.loads(_NOTES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"weather": "", "btc": "", "updown": "", "general": ""}


def _save_notes(notes: dict):
    try:
        _NOTES_FILE.parent.mkdir(exist_ok=True)
        _NOTES_FILE.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


@app.get("/api/strategy/performance")
async def get_strategy_performance():
    """Estadísticas de rendimiento por tipo de estrategia desde el historial de trades."""
    history = bot.state.trade_history or []

    def _empty():
        return {
            "trades": 0, "wins": 0, "losses": 0, "pending": 0, "total_cost": 0.0,
            "by_side": {"YES": {"t": 0, "w": 0, "l": 0}, "NO": {"t": 0, "w": 0, "l": 0}},
            "by_ev":   {"low": {"t": 0, "w": 0, "l": 0}, "med": {"t": 0, "w": 0, "l": 0}, "high": {"t": 0, "w": 0, "l": 0}},
            "recent":  [],  # last 10 resolved results ("WIN"/"LOSS")
        }

    stats = {
        "WEATHER":    _empty(),
        "BTC":        _empty(),
        "BTC_UPDOWN": _empty(),
    }

    for t in history:
        asset = t.get("asset", "WEATHER")
        if asset not in stats:
            asset = "WEATHER"
        s = stats[asset]
        s["trades"] += 1
        s["total_cost"] = round(s["total_cost"] + float(t.get("cost_usdc", 0)), 2)
        result = t.get("result", "")
        if result == "WIN":
            s["wins"] += 1
        elif result == "LOSS":
            s["losses"] += 1
        else:
            s["pending"] += 1

        # By side
        side = (t.get("side") or "").upper()
        if side in ("YES", "NO"):
            bs = s["by_side"][side]
            bs["t"] += 1
            if result == "WIN":   bs["w"] += 1
            elif result == "LOSS": bs["l"] += 1

        # By EV bucket
        ev = float(t.get("ev_pct") or 0)
        bucket = "high" if ev >= 30 else ("med" if ev >= 15 else "low")
        be = s["by_ev"][bucket]
        be["t"] += 1
        if result == "WIN":   be["w"] += 1
        elif result == "LOSS": be["l"] += 1

        # Recent resolved
        if result in ("WIN", "LOSS") and len(s["recent"]) < 10:
            s["recent"].append(result)

    def _wr(w, l):
        return round(w / (w + l), 3) if (w + l) > 0 else None

    # Win rates + recent WR
    for s in stats.values():
        resolved = s["wins"] + s["losses"]
        s["win_rate"] = _wr(s["wins"], s["losses"])
        rec = s["recent"]
        s["recent_wr"] = _wr(sum(1 for r in rec if r == "WIN"), sum(1 for r in rec if r == "LOSS"))
        # Flatten by_side / by_ev with win rates
        for bkey in ("YES", "NO"):
            b = s["by_side"][bkey]
            b["wr"] = _wr(b["w"], b["l"])
        for bkey in ("low", "med", "high"):
            b = s["by_ev"][bkey]
            b["wr"] = _wr(b["w"], b["l"])

    # Learner data for UpDown
    try:
        from updown_learner import get_summary
        stats["BTC_UPDOWN"]["learner_5m"]  = get_summary(5)
        stats["BTC_UPDOWN"]["learner_15m"] = get_summary(15)
    except Exception:
        pass
    return stats


@app.get("/api/strategy/notes")
async def get_strategy_notes():
    return _load_notes()


@app.post("/api/strategy/notes")
async def save_strategy_notes(data: dict):
    notes = _load_notes()
    for key in ("weather", "btc", "updown", "general"):
        if key in data:
            notes[key] = str(data[key])[:2000]  # max 2000 chars por sección
    _save_notes(notes)
    return {"ok": True}


@app.get("/api/updown/pending")
async def get_updown_pending():
    """Muestra los trades UpDown pendientes de resolución (para diagnóstico)."""
    from datetime import datetime, timezone
    now_ts = int(datetime.now(timezone.utc).timestamp())
    result = []
    for token_id, pending in bot._updown_pending_outcomes.items():
        if not isinstance(pending, dict):
            result.append({"token_id": token_id, "format": "viejo (int)", "value": pending})
            continue
        end_ts  = pending.get("end_ts", 0)
        elapsed_since_close = now_ts - end_ts
        result.append({
            "token_id":     token_id[:16] + "...",
            "slug":         pending.get("slug", ""),
            "interval":     pending.get("interval"),
            "side":         pending.get("side"),
            "btc_start":    pending.get("btc_start"),
            "end_ts":       end_ts,
            "closed_ago_s": elapsed_since_close,
            "status":       "PENDIENTE" if elapsed_since_close < 15 else "LISTO PARA RESOLVER",
        })
    return {"pending_count": len(result), "trades": result, "now_ts": now_ts}


@app.post("/api/updown/dry-run")
async def updown_dry_run(data: dict = {}):
    """
    Ejecuta el scan de UpDown completo (fetch + TA + estrategia) sin colocar ningún trade.
    Útil para verificar la lógica antes de arriesgar capital.
    """
    from markets_updown import fetch_updown_market
    from strategy_updown import evaluate_updown_market, build_btc_direction_signal
    from price_feed import get_btc_price, get_btc_ta
    from datetime import datetime, timezone

    interval = int(data.get("interval_minutes", 5))
    ta_interval = "1m" if interval == 5 else "5m"

    market = await fetch_updown_market(interval)
    if not market:
        return {"ok": False, "reason": "Sin mercado activo ahora mismo"}

    ta_data = await get_btc_ta(interval=ta_interval)
    btc_now = bot.state.btc_price or await get_btc_price()
    btc_start = await bot._get_btc_price_at_ts(market["window_start_ts"])
    cmc_data = bot.state.btc_cmc or {}

    if not btc_start:
        return {
            "ok": False,
            "reason": "Sin precio BTC al inicio de la ventana (Binance no responde o ventana muy reciente)",
            "market": {
                "slug": market["slug"],
                "elapsed_minutes": round(market["elapsed_minutes"], 2),
                "minutes_to_close": round(market["minutes_to_close"], 2),
            }
        }

    sig = build_btc_direction_signal(
        ta_data=ta_data,
        btc_price=btc_now or 0,
        btc_price_window_start=btc_start,
        cmc_data=cmc_data,
    )

    opp, _opp_reason = evaluate_updown_market(
        market=market,
        ta_data=ta_data,
        btc_price=btc_now or 0,
        btc_price_window_start=btc_start,
        cmc_data=cmc_data,
    )

    return {
        "ok": True,
        "dry_run": True,
        "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "market": {
            "slug":             market["slug"],
            "elapsed_minutes":  round(market["elapsed_minutes"], 2),
            "minutes_to_close": round(market["minutes_to_close"], 2),
            "up_price":         market["up_price"],
            "down_price":       market["down_price"],
        },
        "btc": {
            "price_now":   btc_now,
            "price_start": btc_start,
            "move_pct":    round((btc_now - btc_start) / btc_start * 100, 4) if btc_start else None,
        },
        "signal": sig,
        "ta": {
            "recommendation": ta_data.get("recommendation"),
            "signal":         round(ta_data.get("signal", 0), 4),
            "rsi":            ta_data.get("rsi"),
            "buy":            ta_data.get("buy"),
            "sell":           ta_data.get("sell"),
            "neutral":        ta_data.get("neutral"),
        },
        "decision": {
            "would_trade": opp is not None,
            "reason":      (_opp_reason or "Señal insuficiente") if opp is None else f"Señal {opp['side']} con {opp['confidence']}% de confianza",
            "side":        opp["side"] if opp else None,
            "entry_price": opp["entry_price"] if opp else None,
            "size_usdc":   opp["size_usdc"] if opp else None,
            "confidence":  opp["confidence"] if opp else None,
        }
    }


@app.get("/api/positions")
async def get_positions():
    return bot.state.active_positions


@app.get("/api/trades")
async def get_trades():
    return bot.state.trade_history


@app.get("/api/stats")
async def get_stats():
    """Datos agregados para el dashboard: P&L, trades por tipo, historial acumulado."""
    trades = bot.state.trade_history

    # Breakdown por tipo de asset
    by_type: dict[str, dict] = {
        "WEATHER":    {"count": 0, "cost": 0.0},
        "BTC":        {"count": 0, "cost": 0.0},
        "BTC_UPDOWN": {"count": 0, "cost": 0.0},
    }
    for t in trades:
        asset = t.get("asset") or ""
        # Inferir tipo si el campo asset no está o es vacío
        if not asset:
            mkt = (t.get("market") or "").lower()
            if "updown" in mkt or "up/down" in mkt or "up or down" in mkt:
                asset = "BTC_UPDOWN"
            elif "btc" in mkt or "bitcoin" in mkt or "$" in mkt:
                asset = "BTC"
            else:
                asset = "WEATHER"
        if asset not in by_type:
            by_type[asset] = {"count": 0, "cost": 0.0}
        by_type[asset]["count"] += 1
        by_type[asset]["cost"]  += float(t.get("cost_usdc", 0))

    # ── P&L acumulado por trade ───────────────────────────────────────────────
    # Para UpDown resueltos: calculamos ganancia/pérdida real
    # Para posiciones abiertas en poly_positions: usamos pnl_usdc actual
    # Construimos una línea de tiempo uniendo ambas fuentes

    # Mapa token_id → pnl_usdc actual (de Polymarket)
    poly_pnl_map = {p.get("token_id", ""): float(p.get("pnl_usdc", 0)) for p in bot.state.poly_positions}

    pnl_history = []
    cumulative_pnl = 0.0
    for t in reversed(trades):  # reversed = cronológico
        result = t.get("result", "")
        cost   = float(t.get("cost_usdc", 0))
        price  = float(t.get("price", t.get("entry_price", 0.5)) or 0.5)

        if result == "WIN":
            # Ganancia en una opción binaria: shares*(1 - entry_price)
            pnl_t = cost * (1.0 - price) / price if price > 0 else 0
        elif result == "LOSS":
            pnl_t = -cost
        else:
            # Posición abierta: usar P&L actual de Polymarket si está disponible
            token_id = t.get("token_id", "")
            pnl_t = poly_pnl_map.get(token_id, 0.0)

        cumulative_pnl += pnl_t
        pnl_history.append({
            "time": t.get("time", ""),
            "pnl":  round(cumulative_pnl, 4),
            "label": (t.get("market") or t.get("title") or "")[:30],
        })

    # Posiciones con P&L por tipo desde poly_positions
    poly_by_type: dict[str, float] = {"WEATHER": 0.0, "BTC": 0.0, "BTC_UPDOWN": 0.0}
    for p in bot.state.poly_positions:
        title = p.get("market_title", "").lower()
        if "updown" in title or "up/down" in title:
            poly_by_type["BTC_UPDOWN"] += float(p.get("pnl_usdc", 0))
        elif "btc" in title or "bitcoin" in title or "$" in title:
            poly_by_type["BTC"] += float(p.get("pnl_usdc", 0))
        else:
            poly_by_type["WEATHER"] += float(p.get("pnl_usdc", 0))

    total_value   = round(bot.state.balance_usdc + sum(p["cur_value_usdc"] for p in bot.state.poly_positions), 2)
    portfolio_pnl = round(sum(p["pnl_usdc"] for p in bot.state.poly_positions), 2)

    # Deployed calculado FRESCO desde active_positions (no usar state.deployed_X que puede ser stale)
    dep = bot._calc_deployed_by_type()
    dep_w = round(dep.get("WEATHER",    0.0), 2)
    dep_b = round(dep.get("BTC",        0.0), 2)
    dep_u = round(dep.get("BTC_UPDOWN", 0.0), 2)
    # Actualizar state también para que el loop de updown tenga el valor correcto
    bot.state.deployed_weather = dep_w
    bot.state.deployed_btc     = dep_b
    bot.state.deployed_updown  = dep_u

    cash = bot.state.balance_usdc

    def _headroom(pct, deployed):
        budget = round(total_value * pct, 2)
        return max(0.0, budget - deployed), budget

    hw, bud_w = _headroom(bot_params.alloc_weather_pct, dep_w)
    hb, bud_b = _headroom(bot_params.alloc_btc_pct,     dep_b)
    hu, bud_u = _headroom(bot_params.alloc_updown_pct,  dep_u)
    total_headroom = hw + hb + hu

    # Distribuir el cash disponible proporcionalmente al espacio libre de cada categoría
    # Si total_headroom <= cash → cada una puede usar todo su espacio
    # Si total_headroom > cash  → se escala down proporcionalmente
    if total_headroom > 0:
        ratio = min(1.0, cash / total_headroom)
        avail_w = round(hw * ratio, 2)
        avail_b = round(hb * ratio, 2)
        avail_u = round(hu * ratio, 2)
    else:
        avail_w = avail_b = avail_u = 0.0

    def _alloc(pct, deployed, budget, available):
        used_pct    = min(100.0, round(deployed / budget * 100, 1)) if budget > 0 else 0
        over_budget = budget > 0 and deployed > budget
        return {"pct": pct, "budget": budget, "deployed": deployed, "available": available, "used_pct": used_pct, "over_budget": over_budget}

    return {
        "total_trades":    bot.state.total_trades,
        "total_pnl":       round(bot.state.total_pnl, 2),
        "balance_usdc":    round(bot.state.balance_usdc, 2),
        "total_value":     total_value,
        "portfolio_pnl":   portfolio_pnl,
        "daily_loss_usdc": round(bot.state.daily_loss_usdc, 2),
        "daily_start_balance": round(bot.state.daily_start_balance, 2),
        "by_type": by_type,
        "poly_pnl_by_type": poly_by_type,
        "pnl_history": pnl_history[-200:],
        "allocation": {
            "weather": _alloc(bot_params.alloc_weather_pct, dep_w, bud_w, avail_w),
            "btc":     _alloc(bot_params.alloc_btc_pct,     dep_b, bud_b, avail_b),
            "updown":  _alloc(bot_params.alloc_updown_pct,  dep_u, bud_u, avail_u),
        },
    }


@app.get("/api/performance")
async def get_performance():
    """Metricas de recursos del sistema y tiempos de ejecucion por componente."""
    return perf.get_stats()


@app.get("/api/performance/log")
async def get_performance_log(limit: int = 200):
    """Resource log: llamadas a APIs, spikes de CPU/RAM, resúmenes de ciclos."""
    return perf.get_resource_log(limit=limit)


@app.post("/api/allocation")
async def set_allocation(data: dict):
    """
    Actualiza las proporciones de asignación de capital.
    Recibe {weather_pct, btc_pct, updown_pct} como decimales (ej. 0.60, 0.20, 0.20).
    La suma debe ser 1.0.
    """
    w = float(data.get("weather_pct", bot_params.alloc_weather_pct))
    b = float(data.get("btc_pct",     bot_params.alloc_btc_pct))
    u = float(data.get("updown_pct",  bot_params.alloc_updown_pct))
    total = w + b + u
    if abs(total - 1.0) > 0.01:
        return JSONResponse(status_code=400, content={
            "ok": False, "msg": f"La suma debe ser 1.0 (actual: {total:.2f})"
        })
    bot_params.update({
        "alloc_weather_pct": round(w, 2),
        "alloc_btc_pct":     round(b, 2),
        "alloc_updown_pct":  round(u, 2),
    })
    bot._log("INFO", f"Asignación actualizada: Weather {w*100:.0f}% / BTC {b*100:.0f}% / UpDown {u*100:.0f}%")
    return {"ok": True, "weather_pct": w, "btc_pct": b, "updown_pct": u}


@app.post("/api/goal")
async def set_goal(data: dict):
    """
    Establece o actualiza la meta de ganancia.
    Body: { "usdc": 30.0, "hours": 24.0 }
    Si usdc == 0, cancela la meta activa.
    """
    usdc  = float(data.get("usdc", 0))
    hours = float(data.get("hours", 24))
    if usdc < 0 or hours <= 0:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "Valores inválidos"})

    if usdc == 0:
        bot_params.update({
            "profit_goal_usdc": 0.0,
            "profit_goal_start_iso": "",
            "profit_goal_start_value": 0.0,
        })
        bot._log("INFO", "Meta de ganancia cancelada.")
        return {"ok": True, "active": False}

    portfolio_value = sum(p["cur_value_usdc"] for p in bot.state.poly_positions)
    total_value = round(bot.state.balance_usdc + portfolio_value, 2)
    bot_params.update({
        "profit_goal_usdc":        round(usdc, 2),
        "profit_goal_hours":       round(hours, 2),
        "profit_goal_start_iso":   datetime.now(timezone.utc).isoformat(),
        "profit_goal_start_value": total_value,
    })
    bot._log("INFO", f"Meta activada: +${usdc:.2f} en {hours:.1f}h (valor base: ${total_value:.2f})")
    return {"ok": True, "active": True, "goal_usdc": usdc, "goal_hours": hours, "start_value": total_value}


# ── Chat con Claude ────────────────────────────────────────────────────────

def _build_chat_context(include_logs: bool = True, include_learner: bool = True) -> str:
    """Construye el contexto actual del bot para el chat."""
    s = bot.state
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"Fecha/hora actual: {now_str}",
        f"Bot corriendo: {'Sí' if s.running else 'No'}",
        f"",
        f"=== BALANCE ===",
        f"Cash libre: ${s.balance_usdc:.2f} USDC",
        f"Portfolio (posiciones abiertas): ${sum(p.get('cur_value_usdc',0) for p in s.poly_positions):.2f} USDC",
        f"P&L no realizado: ${sum(p.get('pnl_usdc',0) for p in s.poly_positions):.2f} USDC",
        f"Pérdida del día: ${s.daily_loss_usdc:.2f} USDC",
        f"Trades totales: {s.total_trades}",
    ]
    if s.poly_positions:
        lines += ["", f"=== POSICIONES ABIERTAS ({len(s.poly_positions)}) ==="]
        for p in s.poly_positions:
            cur_price = p.get("cur_price", 0)
            status = ""
            if p.get("redeemable") and cur_price < 0.05:
                status = " [PERDIDA/RESUELTA - necesita redeem]"
            elif p.get("redeemable") and cur_price > 0.95:
                status = " [GANADA - pendiente de cobro]"
            elif cur_price < 0.05:
                status = " [PRECIO ≈ 0 - posiblemente resuelta]"
            lines.append(
                f"• [token_id:{p['token_id']}] {p['market_title'][:60]} | {p['outcome']} | "
                f"{p['size']} shares @ {p['avg_price']:.3f} → {p['cur_price']:.3f} | "
                f"P&L: ${p['pnl_usdc']:+.2f} ({p['pnl_pct']:+.1f}%) | "
                f"Cierra en: {p.get('hours_to_close','?')}h{status}"
            )
    if s.opportunities:
        lines += ["", f"=== OPORTUNIDADES DETECTADAS ({len(s.opportunities)}) ==="]
        for o in s.opportunities[:8]:
            lines.append(
                f"• [condition_id:{o.get('condition_id','')}] {o.get('market_title','')[:50]} | "
                f"Lado: {o.get('side')} | EV: {o.get('ev_pct')}% | "
                f"Nuestra P: {o.get('our_prob',0)*100:.1f}% | "
                f"Mercado P: {o.get('market_prob',0)*100:.1f}% | "
                f"Cierra en: {o.get('hours_to_close',0):.1f}h"
            )
    if s.open_orders:
        lines += ["", f"=== ÓRDENES ABIERTAS ({len(s.open_orders)}) ==="]
        for o in s.open_orders:
            lines.append(
                f"• {o.get('market','')} | {o.get('side')} @ {o.get('price'):.3f} | "
                f"Pendientes: {o.get('size_remaining')} shares (${o.get('cost_usdc'):.2f})"
            )
    if s.btc_price:
        lines += ["", "=== BITCOIN ==="]
        lines.append(f"Precio Binance: ${s.btc_price:,.2f} USD")

        if s.btc_cmc:
            c = s.btc_cmc
            lines.append(
                f"CoinMarketCap: ${c.get('price',0):,.2f} | "
                f"1h: {c.get('percent_change_1h',0):+.2f}% | "
                f"24h: {c.get('percent_change_24h',0):+.2f}% | "
                f"7d: {c.get('percent_change_7d',0):+.2f}%"
            )

        if s.btc_ta:
            ta = s.btc_ta
            rsi_str = f" | RSI: {ta['rsi']:.1f}" if ta.get('rsi') else ""
            ema_str = f" | EMA20: {ta['ema20']:,.0f}" if ta.get('ema20') else ""
            lines.append(
                f"TradingView ({ta.get('interval','?')}): {ta.get('recommendation','NEUTRAL')} "
                f"[↑{ta.get('buy',0)} ↔{ta.get('neutral',0)} ↓{ta.get('sell',0)}]{rsi_str}{ema_str}"
            )

        auto_str = (f"ACTIVO — cada {s.btc_scan_interval_minutes} min, "
                    f"próximo en {s.btc_next_scan_in}s") if s.btc_auto_mode else "INACTIVO"
        lines.append(f"Auto-trading BTC: {auto_str}")

        if s.btc_opportunities:
            lines += [f"Oportunidades BTC detectadas: {len(s.btc_opportunities)}"]
            for o in s.btc_opportunities[:8]:
                tv = f" | TV:{o.get('ta_recommendation','?')}" if o.get('ta_recommendation') else ""
                lines.append(
                    f"• [condition_id:{o.get('condition_id','')}] "
                    f"{o.get('side')} {'>' if o.get('direction')=='above' else '<'}"
                    f"${o.get('threshold',0):,.0f} | "
                    f"BTC: ${o.get('btc_price_at_eval',0):,.0f} ({o.get('pct_from_threshold',0):+.2f}%) | "
                    f"P: {o.get('our_prob',0)*100:.1f}% vs {o.get('market_prob',0)*100:.1f}% | "
                    f"EV: {o.get('ev_pct')}% | Cierra en: {o.get('minutes_to_close',0):.0f}m{tv}"
                )
        else:
            lines.append("Sin oportunidades BTC detectadas (ejecuta un escaneo o activa el auto-trading)")
    if s.portfolio_analysis:
        lines += ["", "=== ÚLTIMO ANÁLISIS DE PORTAFOLIO (Claude) ===", s.portfolio_analysis[:800]]

    # === PARÁMETROS DEL BOT ===
    from config import bot_params as _bp
    bp = _bp.to_dict()
    lines += [
        "",
        "=== PARÁMETROS DEL BOT ===",
        f"Auto-trade global: {'ACTIVO' if s.auto_trade_mode else 'INACTIVO'}",
        f"Intervalo de escaneo: {bp['scan_interval_minutes']} min",
        f"EV mínimo clima: {bp['min_ev_threshold']*100:.0f}% | Kelly fraction: {bp['kelly_fraction']}",
        f"Posición máx clima: ${bp['max_position_usdc']} | mín: ${bp['min_position_usdc']}",
        f"Liquidez mín: ${bp['min_liquidity_usdc']} | Spread máx: {bp['max_spread_pct']*100:.0f}%",
        f"UpDown habilitado: 5m={'Sí' if bp['updown_5m_enabled'] else 'No'} | 15m={'Sí' if bp['updown_15m_enabled'] else 'No'} | Máx por trade: ${bp['updown_max_usdc']}",
        f"UpDown máx pérdidas consecutivas: {bp['updown_max_consecutive_losses']}",
        f"BTC habilitado: {'Sí' if bp['btc_enabled'] else 'No'} | BTC max posición: ${bp['btc_max_position_usdc']}",
    ]

    # === CAPITAL (sistema de buckets Fase 12) ===
    _pool = bp.get("betting_pool_usdc", 0.0)
    if _pool > 0:
        _bkt_sum = (bp.get("bucket_weather_usdc", 0) + bp.get("bucket_btc_usdc", 0) +
                    bp.get("bucket_updown_5m_usdc", 0) + bp.get("bucket_updown_15m_usdc", 0))
        _cash_free = round(max(0.0, s.balance_usdc - _bkt_sum), 2)
        lines += [
            "",
            "=== CAPITAL (sistema de buckets activo) ===",
            f"Pool de apuestas asignado: ${_pool:.2f} USDC",
            f"Cash libre (no asignado): ${_cash_free:.2f} USDC",
            f"Balance total Polymarket: ${s.balance_usdc:.2f} USDC",
            f"Bucket Weather   — disponible: ${bp.get('bucket_weather_usdc',0):.2f} | % asignado: {bp.get('bucket_weather_pct',0)*100:.0f}%",
            f"Bucket BTC       — disponible: ${bp.get('bucket_btc_usdc',0):.2f} | % asignado: {bp.get('bucket_btc_pct',0)*100:.0f}%",
            f"Bucket UpDown 5m — disponible: ${bp.get('bucket_updown_5m_usdc',0):.2f} | % asignado: {bp.get('bucket_updown_5m_pct',0)*100:.0f}%",
            f"Bucket UpDown 15m— disponible: ${bp.get('bucket_updown_15m_usdc',0):.2f} | % asignado: {bp.get('bucket_updown_15m_pct',0)*100:.0f}%",
            f"Deployed Weather: ${getattr(s,'deployed_weather',0):.2f} | BTC: ${getattr(s,'deployed_btc',0):.2f} | UpDown: ${getattr(s,'deployed_updown',0):.2f}",
        ]
    else:
        lines += [
            "",
            "=== CAPITAL (sistema legacy por %) ===",
            f"Balance Polymarket: ${s.balance_usdc:.2f} USDC",
            f"Asignación: Clima {bp.get('alloc_weather_pct',0.6)*100:.0f}% | BTC {bp.get('alloc_btc_pct',0.2)*100:.0f}% | UpDown {bp.get('alloc_updown_pct',0.2)*100:.0f}%",
            f"Clima   — Presupuesto: ${getattr(s,'budget_weather',0):.2f} | Deployed: ${getattr(s,'deployed_weather',0):.2f} | Disponible: ${getattr(s,'available_weather',0):.2f}",
            f"BTC     — Presupuesto: ${getattr(s,'budget_btc',0):.2f} | Deployed: ${getattr(s,'deployed_btc',0):.2f} | Disponible: ${getattr(s,'available_btc',0):.2f}",
            f"UpDown  — Presupuesto: ${getattr(s,'budget_updown',0):.2f} | Deployed: ${getattr(s,'deployed_updown',0):.2f} | Disponible: ${getattr(s,'available_updown',0):.2f}",
            f"NOTA: Para activar el sistema de buckets, asigna un pool en /api/capital/assign",
        ]

    # === META DE GANANCIA ===
    if bp.get("profit_goal_usdc", 0) > 0:
        goal_usdc = bp["profit_goal_usdc"]
        goal_hours = bp["profit_goal_hours"]
        start_iso = bp.get("profit_goal_start_iso", "")
        start_val = bp.get("profit_goal_start_value", 0.0)
        total_now = s.balance_usdc + sum(p.get("cur_value_usdc", 0) for p in s.poly_positions)
        earned = total_now - start_val if start_val > 0 else 0.0
        progress_pct = (earned / goal_usdc * 100) if goal_usdc > 0 else 0.0
        time_left_str = "?"
        if start_iso:
            try:
                start_dt = datetime.fromisoformat(start_iso)
                elapsed_h = (datetime.now(timezone.utc) - start_dt).total_seconds() / 3600
                remaining_h = max(0, goal_hours - elapsed_h)
                time_left_str = f"{remaining_h:.1f}h restantes"
            except Exception:
                pass
        lines += [
            "",
            "=== META DE GANANCIA ===",
            f"Meta: +${goal_usdc:.2f} USDC en {goal_hours:.0f}h",
            f"Progreso: +${earned:.2f} / ${goal_usdc:.2f} ({progress_pct:.1f}%) — {time_left_str}",
            f"Valor total cuenta ahora: ${total_now:.2f} | Inicio: ${start_val:.2f}",
        ]

    # === ESTADO UPDOWN ===
    lines += [
        "",
        "=== ESTADO UPDOWN ===",
        f"5m — Pérdidas consecutivas: {s.updown_5m_consecutive_losses} | Detenido: {'SÍ' if s.updown_5m_stopped else 'No'}",
        f"15m — Pérdidas consecutivas: {s.updown_15m_consecutive_losses} | Detenido: {'SÍ' if s.updown_15m_stopped else 'No'}",
    ]
    if s.updown_last_opp_5m:
        o = s.updown_last_opp_5m
        lines.append(f"Último scan 5m: {o.get('slug','')} | Señal: {o.get('signal','?')} | Confianza: {o.get('confidence','?')}% | Decisión: {o.get('decision','?')}")
    if s.updown_last_opp_15m:
        o = s.updown_last_opp_15m
        lines.append(f"Último scan 15m: {o.get('slug','')} | Señal: {o.get('signal','?')} | Confianza: {o.get('confidence','?')}% | Decisión: {o.get('decision','?')}")
    if s.updown_recent_trades:
        lines.append(f"Últimos trades UpDown ({len(s.updown_recent_trades)}):")
        for t in s.updown_recent_trades[:5]:
            result = t.get("result", "PENDIENTE")
            lines.append(
                f"  • {t.get('interval','?')}m {t.get('side','?')} @ {t.get('entry_price',0):.3f} | "
                f"${t.get('size_usdc',0):.2f} | {result} | Confianza: {t.get('confidence',0):.0f}%"
            )

    # === NOTAS DEL USUARIO SOBRE ESTRATEGIAS ===
    notes = _load_notes()
    has_notes = any(v.strip() for v in notes.values())
    if has_notes:
        lines += ["", "=== NOTAS DEL USUARIO SOBRE ESTRATEGIAS ==="]
        if notes.get("general", "").strip():
            lines.append(f"General: {notes['general']}")
        if notes.get("weather", "").strip():
            lines.append(f"Clima: {notes['weather']}")
        if notes.get("btc", "").strip():
            lines.append(f"BTC predicción: {notes['btc']}")
        if notes.get("updown", "").strip():
            lines.append(f"UpDown: {notes['updown']}")

    # === HISTORIAL RECIENTE DE TRADES ===
    if s.trade_history:
        recent_trades = s.trade_history[-15:]
        lines += ["", f"=== ÚLTIMOS {len(recent_trades)} TRADES ==="]
        for t in recent_trades:
            result = t.get("result", "PENDIENTE")
            asset  = t.get("asset", "?")
            lines.append(
                f"• {t.get('time','?')} | {asset} | {t.get('market','')[:40]} | "
                f"${t.get('cost_usdc',0):.2f} @ {t.get('price',0):.3f} | {result}"
            )

    # === BOTS ARENA — COMPARATIVA DETALLADA ===
    if include_learner:
        try:
            from updown_learner import get_summary as _ud_summary
            from config import bot_params as _bp2
            bp2 = _bp2.to_dict()
            lines += ["", "=== BOTS ARENA — COMPARATIVA DETALLADA ==="]
            for iv in [5, 15]:
                ud = _ud_summary(iv)
                if not ud:
                    continue
                ap = ud.get("adaptive", {})
                ph = ud.get("phantom") or {}

                def _pct(v):
                    return f"{v*100:.1f}%" if v is not None else "—"

                # Real bot
                wr_str   = _pct(ud.get("win_rate"))
                rwr_str  = _pct(ud.get("recent_wr"))
                up_wr    = _pct((ud.get("by_side") or {}).get("UP"))
                dn_wr    = _pct((ud.get("by_side") or {}).get("DOWN"))
                sig_w    = _pct((ud.get("by_signal") or {}).get("weak"))
                sig_m    = _pct((ud.get("by_signal") or {}).get("med"))
                sig_s    = _pct((ud.get("by_signal") or {}).get("strong"))
                el_e     = _pct((ud.get("by_elapsed") or {}).get("early"))
                el_m     = _pct((ud.get("by_elapsed") or {}).get("mid"))
                el_l     = _pct((ud.get("by_elapsed") or {}).get("late"))
                conf_min = bp2.get(f"updown_{iv}m_min_confidence", 0.20)
                mom_gate = bp2.get(f"updown_{iv}m_momentum_gate", 0.20)
                gate_str = "ESTRICTO" if ap.get("momentum_gate_strict") else "normal"
                inv_str  = " [SEÑAL INVERTIDA]" if ap.get("invert_signal") else ""
                lines += [
                    f"",
                    f"--- Bot UpDown {iv}m (Real) ---",
                    f"  Trades: {ud.get('total',0)} | Wins: {ud.get('wins',0)} | WR total: {wr_str} | WR reciente: {rwr_str}",
                    f"  Por lado: UP={up_wr} | DOWN={dn_wr}",
                    f"  Por señal: débil={sig_w} | media={sig_m} | fuerte={sig_s}",
                    f"  Por timing: temprano={el_e} | medio={el_m} | tardío={el_l}",
                    f"  Params adaptativos: confianza_min={conf_min*100:.0f}% | gate_momentum={mom_gate*100:.0f}% | modo_gate={gate_str}{inv_str}",
                    f"  Min elapsed aprendido: {ap.get('min_elapsed_min','?')}min | Motivo ajuste: {ap.get('reason','?')}",
                ]
                # Phantom bot
                if ph.get("total", 0) > 0:
                    ph_wr  = _pct(ph.get("win_rate"))
                    ph_rwr = _pct(ph.get("recent_wr"))
                    ph_up  = _pct((ph.get("by_side") or {}).get("UP"))
                    ph_dn  = _pct((ph.get("by_side") or {}).get("DOWN"))
                    lines += [
                        f"--- Bot Phantom {iv}m ---",
                        f"  Trades: {ph.get('total',0)} | WR total: {ph_wr} | WR reciente: {ph_rwr}",
                        f"  Por lado: UP={ph_up} | DOWN={ph_dn}",
                    ]
                else:
                    lines.append(f"--- Bot Phantom {iv}m — sin datos aún ---")
        except Exception as _e:
            lines.append(f"(Error al cargar comparativa bots: {_e})")

    # === TRADING MODE (v9.4 — punto 13) ===
    try:
        import trading_positions as _tp
        from config import bot_params as _bp3
        bp3 = _bp3.to_dict()
        ph_stats = _tp.stats_summary(is_real=False)
        rl_stats = _tp.stats_summary(is_real=True) if bp3.get("trading_real_enabled") else None
        lines += [
            "",
            "=== TRADING MODE ===",
            f"enabled: {bp3.get('trading_mode_enabled')} | real_enabled: {bp3.get('trading_real_enabled')}",
            f"Params: entry≤{bp3.get('trading_entry_threshold')} | max_entry≤{bp3.get('trading_max_entry_price')} | "
            f"offset={bp3.get('trading_profit_offset')} | stake=${bp3.get('trading_stake_usdc')} | "
            f"one_open={bp3.get('trading_one_open_at_a_time')}",
            f"Stop-loss: enabled={bp3.get('trading_sl_enabled')} | trigger={bp3.get('trading_sl_trigger_drop')} "
            f"| wait={bp3.get('trading_sl_wait_min')}min | recover×={bp3.get('trading_sl_min_recover_factor')} "
            f"| panic={bp3.get('trading_panic_trigger_drop')}",
            f"Safety real: max_exp=${bp3.get('trading_real_max_exposure_usdc')} | daily_loss=${bp3.get('trading_real_daily_loss_limit_usdc')} "
            f"| max_consec={bp3.get('trading_real_max_consec_losses')} | killed={bp3.get('trading_real_killed')}",
            f"Phantom: balance=${ph_stats.get('phantom_balance', 0):.2f} | trades={ph_stats.get('total_positions',0)} "
            f"| wins={ph_stats.get('wins',0)}/{ph_stats.get('closed',0)} WR={ph_stats.get('win_rate',0)}% "
            f"| pnl_realizado=${ph_stats.get('realized_pnl',0):.2f} | open={ph_stats.get('open',0)}",
        ]
        if rl_stats:
            lines.append(
                f"Real: trades={rl_stats.get('total_positions',0)} | wins={rl_stats.get('wins',0)}/{rl_stats.get('closed',0)} "
                f"WR={rl_stats.get('win_rate',0)}% | pnl=${rl_stats.get('realized_pnl',0):.2f} | open={rl_stats.get('open',0)} "
                f"| exposure=${_tp.real_exposure_usdc():.2f}"
            )
        # Últimas 5 posiciones (phantom+real mezcladas por ts)
        recent = _tp.get_all_positions_flat(is_real=False, limit=5)
        if recent:
            lines.append("Últimos 5 trades phantom:")
            for p in recent:
                tf = "1d" if p.get("interval") == 1440 else f"{p.get('interval','?')}m"
                lines.append(
                    f"  • {p.get('entry_iso','?')} | TF={tf} | {p.get('side')} "
                    f"entry={p.get('entry_price',0):.3f}→exit={p.get('exit_price') if p.get('exit_price') is not None else '—'} "
                    f"| status={p.get('status')} | pnl=${p.get('pnl_usdc',0):.2f}"
                )
    except Exception as _e:
        lines.append(f"(Trading Mode context err: {_e})")

    # === LOGS RECIENTES (últimos 30) ===
    if include_logs:
        recent_logs = bot.get_log_history()[-30:]
        if recent_logs:
            lines += ["", "=== LOGS RECIENTES (últimos 30) ==="]
            for entry in recent_logs:
                lines.append(f"[{entry.get('time','?')}] [{entry.get('level','INFO')}] {entry.get('msg','')}")

    return "\n".join(lines)


CHAT_SYSTEM = """Eres el asesor experto y operador de Weatherbot en Polymarket. Tienes acceso TOTAL de lectura a todo el sistema: logs, parámetros, posiciones, estadísticas, historial de trades y datos de mercado.

IMPORTANTE: NUNCA respondas que "no puedes realizar operaciones" — las herramientas SÍ ejecutan trades reales en Polymarket.

CONTEXTO ACTUAL DEL BOT:
{context}

═══ REGLAS DE ACCESO ═══

LECTURA (siempre disponible): puedes analizar y comentar sobre cualquier dato del sistema.

EJECUCIÓN DE TRADES (cuando el usuario lo pide): vender posiciones, comprar oportunidades, disparar escaneos.

MODIFICACIÓN DE PARÁMETROS (SOLO cuando el usuario lo pide explícitamente): llama update_params únicamente si el usuario dice "cambia", "ajusta", "modifica", "aplica eso", "sube X", "baja Y" u otra orden explícita de cambio. NUNCA modifiques parámetros como sugerencia proactiva — solo sugiérelos y espera confirmación.

ANÁLISIS PROACTIVO: Cuando el usuario pregunta sobre el estado general, analiza en profundidad: patrones de pérdidas, configuraciones subóptimas, oportunidades perdidas (phantom bets), concentración de riesgo, etc. Ofrece sugerencias concretas con los valores exactos recomendados, pero NO las apliques sin orden explícita.

═══ INSTRUCCIONES OPERATIVAS ═══

Clima:
- Si el usuario pide vender una posición → llama sell_position con token_id y size exactos
- Si el usuario pide comprar una oportunidad de clima → llama buy_opportunity con el condition_id

Bitcoin (mercados de precio):
- Si el usuario pide comprar una oportunidad de BTC → llama buy_btc_opportunity con el condition_id de la lista BTC
- Si el usuario pide activar auto-trading / cada N minutos → llama set_btc_auto_mode(enabled=true, interval_minutes=N)
- Si el usuario pide detener auto-trading → llama set_btc_auto_mode(enabled=false)

UpDown (5m/15m BTC up-or-down):
- Si el usuario pide un escaneo UpDown → llama trigger_updown_scan con interval_minutes=5 o 15
- Si el usuario pide pausar/activar UpDown → llama update_params con {"updown_enabled": false/true}
- Si el usuario pide resetear el bloqueo / circuit breaker de 5m o 15m → llama reset_updown_circuit_breaker
- Si el usuario pregunta "¿qué ve el bot ahora?", "analiza el 5m/15m", "señal actual", "¿debería entrar?" → llama analyze_bot con el interval correspondiente
- Para comparar bots o recomendar cambios con datos frescos → llama analyze_bot para cada intervalo de interés

Trading Mode (v9.4 — mercados UP/DOWN Polymarket, compra barato y vende target):
- Opera en UpDown 5m/15m/1d de BTC con bot phantom (entrenamiento) y opcionalmente real.
- Parámetros clave: trading_entry_threshold, trading_max_entry_price (ceiling R:R), trading_profit_offset, trading_stake_usdc.
- Stop-loss escalonado (punto 12): trading_sl_enabled, trading_sl_trigger_drop (0.50=50% drop arma), trading_sl_wait_min, trading_sl_min_recover_factor, trading_panic_trigger_drop (0.80=80% drop → panic salvage).
- Safety REAL (NO afecta phantom): trading_real_max_exposure_usdc, trading_real_daily_loss_limit_usdc, trading_real_max_consec_losses, trading_real_killed.
- Si el usuario pide "ajusta R:R", "cambia stop-loss", "sube stake trading", "reset exposure", "activa trading real" → llama update_params con claves trading_*.
- NUNCA modifiques params proactivamente — solo sugiere y espera orden explícita.

Phantom (trades de aprendizaje):
- Si el usuario pide activar dinero real en phantom → llama toggle_phantom_real con enabled=true
- Si el usuario pide desactivar / quitar dinero real del phantom → llama toggle_phantom_real con enabled=false
- Si el usuario pide cambiar el capital phantom (cash libre, pool, %) → llama set_phantom_capital con los valores indicados

Parámetros:
- Si el usuario dice "aplica esa sugerencia", "cambia X a Y", "sube/baja el parámetro Z" → llama update_params
- Incluye siempre un "reason" claro de por qué se hace el cambio
- Después de aplicar, confirma qué cambió y el valor anterior vs nuevo

MODO AUTÓNOMO (cuando auto_trade_mode = ACTIVO y hay meta de ganancia activa):
- Actúa sin pedir confirmación — ejecuta las operaciones directamente
- Prioriza las acciones que acerquen la cuenta a la meta de ganancia
- Reporta en 2-3 líneas qué hiciste y por qué; no pidas aprobación

General:
- Antes de cada operación di en 1 línea qué harás y por qué
- Después confirma el resultado
- Responde en español, conciso y directo"""


# ── Herramientas del chat ──────────────────────────────────────────────────

CHAT_TOOLS = [
    {
        "name": "sell_position",
        "description": "Vende shares de una posición existente en Polymarket. Usa el token_id exacto de la lista de posiciones abiertas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "token_id": {
                    "type": "string",
                    "description": "El token_id de la posición (aparece como [token_id:...] en el contexto)"
                },
                "size": {
                    "type": "number",
                    "description": "Cantidad de shares a vender (usa el valor 'size' de la posición para vender todo)"
                },
                "title": {"type": "string", "description": "Título del mercado para los logs"},
                "reason": {"type": "string", "description": "Razón de la venta en 1 frase"},
            },
            "required": ["token_id", "size", "title", "reason"],
        },
    },
    {
        "name": "buy_opportunity",
        "description": "Ejecuta la compra de una oportunidad de clima detectada por el bot.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition_id": {
                    "type": "string",
                    "description": "El condition_id de la oportunidad de clima (aparece como [condition_id:...] en el contexto)"
                },
                "reason": {"type": "string", "description": "Razón de la compra en 1 frase"},
            },
            "required": ["condition_id", "reason"],
        },
    },
    {
        "name": "buy_btc_opportunity",
        "description": "Ejecuta la compra de una oportunidad de precio de Bitcoin en Polymarket. Usa el condition_id de la sección BITCOIN del contexto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition_id": {
                    "type": "string",
                    "description": "El condition_id de la oportunidad BTC"
                },
                "reason": {"type": "string", "description": "Razón de la compra incluyendo análisis de TA/EV"},
            },
            "required": ["condition_id", "reason"],
        },
    },
    {
        "name": "set_btc_auto_mode",
        "description": "Activa o desactiva el auto-trading de BTC. Cuando está activo, el bot escanea mercados BTC, evalúa oportunidades con TA + modelo log-normal, y ejecuta trades automáticamente cada N minutos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "true para activar, false para desactivar"
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Intervalo entre escaneos en minutos (1-60). Por defecto 5.",
                    "minimum": 1,
                    "maximum": 60,
                },
            },
            "required": ["enabled"],
        },
    },
    {
        "name": "trigger_updown_scan",
        "description": "Dispara un escaneo del mercado UpDown BTC y ejecuta un trade si hay señal. Usa esto cuando quieres operar en los mercados de BTC sube/baja de 5 o 15 minutos.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interval_minutes": {
                    "type": "integer",
                    "description": "Intervalo del mercado UpDown: 5 o 15 minutos",
                    "enum": [5, 15],
                },
                "reason": {"type": "string", "description": "Por qué se dispara este escaneo"},
            },
            "required": ["interval_minutes"],
        },
    },
    {
        "name": "update_params",
        "description": "Actualiza uno o más parámetros del bot. SOLO llamar si el usuario explícitamente pide cambiar parámetros (dice 'cambia', 'ajusta', 'aplica', 'sube', 'baja', etc.). NUNCA usar proactivamente sin orden explícita del usuario.",
        "input_schema": {
            "type": "object",
            "properties": {
                "params": {
                    "type": "object",
                    "description": (
                        "Diccionario con los parámetros a cambiar. Claves válidas: "
                        "max_position_usdc, min_position_usdc, kelly_fraction, "
                        "min_ev_threshold, max_daily_loss_pct, max_hours_to_resolution, "
                        "min_liquidity_usdc, max_spread_pct, min_volume_24h_usdc, "
                        "scan_interval_minutes, weather_enabled, btc_enabled, "
                        "btc_max_position_usdc, updown_enabled, "
                        "updown_5m_enabled, updown_15m_enabled, "
                        "updown_max_usdc, updown_max_consecutive_losses, "
                        "alloc_weather_pct, alloc_btc_pct, alloc_updown_pct, "
                        "updown_15m_min_confidence, updown_5m_min_confidence, "
                        "updown_15m_momentum_gate, updown_5m_momentum_gate, "
                        "updown_stake_min_usdc, updown_stake_max_usdc, "
                        "updown_stake_conf_min_pct, updown_stake_conf_max_pct, "
                        "updown_displacement_hi_pct, updown_displacement_lo_pct, "
                        "trading_mode_enabled, trading_real_enabled, "
                        "trading_entry_threshold, trading_min_entry_price, trading_max_entry_price, "
                        "trading_profit_offset, trading_exit_deadline_min, "
                        "trading_min_entry_minutes_left, trading_max_entries_per_market, "
                        "trading_max_open_per_side, trading_stake_usdc, trading_one_open_at_a_time, "
                        "trading_real_max_exposure_usdc, trading_real_daily_loss_limit_usdc, "
                        "trading_real_max_consec_losses, trading_real_killed, "
                        "trading_sl_enabled, trading_sl_trigger_drop, trading_sl_wait_min, "
                        "trading_sl_min_recover_factor, trading_panic_trigger_drop, "
                        "trading_buy_probable, trading_probable_min_price, "
                        "trading_probable_max_price, trading_probable_profit_offset, "
                        "trading_real_drawdown_halt_pct, "
                        "trading_paper_required_days, trading_paper_required_trades, "
                        "trading_paper_required_wr, trading_paper_gate_override, "
                        "trading_max_price_age_sec"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Razón del cambio — por qué se hace esta modificación"
                },
            },
            "required": ["params", "reason"],
        },
    },
    {
        "name": "toggle_phantom_real",
        "description": "Activa o desactiva el uso de dinero real en trades phantom UpDown. Cuando está activo, el bot usa USDC reales del pool phantom para ejecutar trades junto con los ficticiios. Cuando está desactivado, solo opera en modo ficticio (aprende sin gastar). Úsalo cuando el usuario diga 'activa phantom real', 'desactiva phantom real', 'pon phantom en modo real', 'quita dinero real del phantom', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "enabled": {
                    "type": "boolean",
                    "description": "true para activar dinero real en phantom, false para solo ficticio"
                },
                "reason": {"type": "string", "description": "Razón del cambio"},
            },
            "required": ["enabled", "reason"],
        },
    },
    {
        "name": "set_phantom_capital",
        "description": "Configura el capital del sistema phantom: cuánto cash libre tiene y cuánto hay en el pool de apuestas activas. También permite ajustar el porcentaje de split entre 5m y 15m. Úsalo cuando el usuario quiera cambiar el capital phantom.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cash_libre_usdc": {
                    "type": "number",
                    "description": "Reserva libre del phantom en USDC. No se toca al perder; crece con ganancias."
                },
                "pool_usdc": {
                    "type": "number",
                    "description": "Capital activo para apostar en USDC. Se reduce al perder; se recupera al ganar."
                },
                "pct_5m": {
                    "type": "number",
                    "description": "Porcentaje del pool para trades de 5m (0-100). Por defecto 30."
                },
                "pct_15m": {
                    "type": "number",
                    "description": "Porcentaje del pool para trades de 15m (0-100). Por defecto 70."
                },
                "reason": {"type": "string", "description": "Razón del cambio"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "reset_updown_circuit_breaker",
        "description": "Resetea el contador de pérdidas consecutivas de un intervalo UpDown para reactivarlo después de que lo detuvo el circuit breaker. Úsalo cuando el usuario diga 'reactiva el 15m', 'resetea las pérdidas del 5m', 'quita el bloqueo UpDown', etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "interval_minutes": {
                    "type": "integer",
                    "description": "Intervalo a resetear: 5 o 15",
                    "enum": [5, 15],
                },
                "reason": {"type": "string", "description": "Razón del reset"},
            },
            "required": ["interval_minutes", "reason"],
        },
    },
    {
        "name": "analyze_bot",
        "description": (
            "Ejecuta un análisis en tiempo real de un bot UpDown específico: obtiene señal BTC actual, "
            "indicadores técnicos, mercado activo, y la decisión que tomaría el bot ahora mismo. "
            "Úsalo cuando el usuario pregunte '¿qué ve el bot 5m ahora?', 'analiza el bot 15m', "
            "'¿cuál es la señal actual?', 'compara 5m y 15m', o cuando necesites datos fresh para recomendar cambios."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "interval_minutes": {
                    "type": "integer",
                    "description": "Intervalo del bot a analizar: 5 o 15",
                    "enum": [5, 15],
                },
                "reason": {"type": "string", "description": "Por qué se hace el análisis"},
            },
            "required": ["interval_minutes"],
        },
    },
]


_PARAM_HISTORY_FILE = Path(__file__).parent / "data" / "bot_param_history.json"
_MAX_HISTORY_PER_BOT = 100  # entradas más recientes a conservar


def _load_param_history() -> dict:
    try:
        if _PARAM_HISTORY_FILE.exists():
            return json.loads(_PARAM_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_param_history_entry(bot_id: str | None, tool: str, inputs: dict,
                               old_values: dict, result_ok: bool, reason: str):
    """Persiste un cambio de parámetros en el historial por bot."""
    try:
        from datetime import datetime, timezone
        history = _load_param_history()
        key = bot_id or "global"
        if key not in history:
            history[key] = []
        entry = {
            "ts":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tool":      tool,
            "params":    inputs.get("params") or {k: v for k, v in inputs.items()
                                                   if k not in ("reason",)},
            "old":       old_values,
            "reason":    reason,
            "ok":        result_ok,
        }
        history[key].insert(0, entry)          # más recientes primero
        history[key] = history[key][:_MAX_HISTORY_PER_BOT]
        _PARAM_HISTORY_FILE.parent.mkdir(exist_ok=True)
        _PARAM_HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    except Exception:
        pass


async def _execute_chat_tool(name: str, inputs: dict, bot_id: str | None = None) -> str:
    """Ejecuta una herramienta del chat y retorna el resultado como string."""
    if name == "sell_position":
        token_id = inputs.get("token_id", "")
        size     = float(inputs.get("size", 0))
        title    = inputs.get("title", "")
        reason   = inputs.get("reason", "Instrucción via chat")

        pos = next((p for p in bot.state.poly_positions if p.get("token_id") == token_id), None)
        if not pos:
            return f"Error: no se encontró una posición con token_id '{token_id[:30]}...' en el portafolio actual."

        # Detectar posición resuelta (precio ≈ 0)
        cur_price = pos.get("cur_price", 1.0)
        if cur_price < 0.05:
            # Posición perdida/resuelta — intentar redimir directamente
            condition_id = pos.get("condition_id", "")
            success = await asyncio.get_event_loop().run_in_executor(
                None, lambda: bot._redeem_position(token_id, condition_id, size, title)
            )
            if success:
                return f"Posición perdida redimida y limpiada del portafolio: '{title[:50]}'"
            else:
                return (
                    f"La posición '{title[:50]}' está resuelta y perdida (precio actual: {cur_price:.3f}). "
                    f"No se puede vender en el mercado porque no hay liquidez. "
                    f"Para limpiarla del portafolio, ve a polymarket.com/portfolio y haz clic en 'Redeem'."
                )

        success = await asyncio.get_event_loop().run_in_executor(
            None, lambda: bot._sell_position(token_id, size, title, f"Chat: {reason}",
                                              pos.get("condition_id", ""))
        )
        if success:
            return f"Venta ejecutada: {size} shares de '{title[:50]}'. Motivo: {reason}"
        else:
            return "Venta fallida. El cliente CLOB puede no estar inicializado o no hay liquidez. Revisa los logs del bot."

    elif name == "buy_opportunity":
        condition_id = inputs.get("condition_id", "")
        reason       = inputs.get("reason", "Instrucción via chat")

        opp = next((o for o in bot.state.opportunities if o.get("condition_id") == condition_id), None)
        if not opp:
            return f"Error: oportunidad '{condition_id[:30]}' no encontrada. Las oportunidades se actualizan cada escaneo; puede que ya no esté disponible."

        if bot.state.balance_usdc < opp.get("size_usdc", 0):
            return f"Balance insuficiente: ${bot.state.balance_usdc:.2f} disponibles, se necesitan ${opp.get('size_usdc',0):.2f}."

        success = await asyncio.get_event_loop().run_in_executor(
            None, lambda: bot._execute_trade(opp)
        )
        if success:
            return (
                f"Compra ejecutada: {opp.get('market_title','')[:50]} | "
                f"${opp.get('size_usdc',0):.2f} USDC @ {opp.get('entry_price',0):.3f} | "
                f"Motivo: {reason}"
            )
        else:
            return "Compra fallida. El cliente CLOB puede no estar inicializado o la orden fue rechazada. Revisa los logs."

    elif name == "buy_btc_opportunity":
        condition_id = inputs.get("condition_id", "")
        reason       = inputs.get("reason", "Instrucción via chat")

        opp = next((o for o in bot.state.btc_opportunities if o.get("condition_id") == condition_id), None)
        if not opp:
            return (
                f"Error: oportunidad BTC '{condition_id[:30]}' no encontrada. "
                "Ejecuta un escaneo BTC primero o verifica el condition_id."
            )

        if bot.state.balance_usdc < opp.get("size_usdc", 0):
            return f"Balance insuficiente: ${bot.state.balance_usdc:.2f} disponibles, se necesitan ${opp.get('size_usdc',0):.2f}."

        success = await asyncio.get_event_loop().run_in_executor(
            None, lambda: bot._execute_trade(opp)
        )
        if success:
            return (
                f"Compra BTC ejecutada: {opp.get('market_title','')[:50]} | "
                f"{opp.get('side')} {'>' if opp.get('direction')=='above' else '<'} ${opp.get('threshold',0):,.0f} | "
                f"${opp.get('size_usdc',0):.2f} USDC @ {opp.get('entry_price',0):.3f} | "
                f"EV: {opp.get('ev_pct')}% | Motivo: {reason}"
            )
        else:
            return "Compra BTC fallida. El cliente CLOB puede no estar inicializado o la orden fue rechazada. Revisa los logs."

    elif name == "set_btc_auto_mode":
        enabled  = inputs.get("enabled", False)
        interval = max(1, min(60, int(inputs.get("interval_minutes", 5))))
        if enabled:
            bot.enable_btc_auto(interval)
            return f"Auto-trading BTC ACTIVADO — escaneo cada {interval} minutos. El bot analizará mercados BTC automáticamente y ejecutará trades si hay edge."
        else:
            bot.disable_btc_auto()
            return "Auto-trading BTC DESACTIVADO."

    elif name == "trigger_updown_scan":
        interval_minutes = int(inputs.get("interval_minutes", 5))
        reason = inputs.get("reason", "Instrucción via chat")
        s = bot.state
        if interval_minutes == 5:
            if s.updown_5m_stopped:
                return (f"UpDown 5m está DETENIDO por pérdidas consecutivas ({s.updown_5m_consecutive_losses}). "
                        f"Resetea el contador desde la UI antes de operar.")
            if not bot_params.updown_5m_enabled:
                return "UpDown 5m está deshabilitado en los parámetros."
        else:
            if s.updown_15m_stopped:
                return (f"UpDown 15m está DETENIDO por pérdidas consecutivas ({s.updown_15m_consecutive_losses}). "
                        f"Resetea el contador desde la UI antes de operar.")
            if not bot_params.updown_15m_enabled:
                return "UpDown 15m está deshabilitado en los parámetros."

        # Ejecutar el scan en el loop del bot
        try:
            await bot._scan_updown(interval_minutes)  # type: ignore  # module-level async fn
            # Leer el resultado del último trade/opp
            if interval_minutes == 5:
                last_trade = s.updown_last_trade_5m
                last_opp   = s.updown_last_opp_5m
            else:
                last_trade = s.updown_last_trade_15m
                last_opp   = s.updown_last_opp_15m

            if last_trade and last_trade.get("slug"):
                return (
                    f"UpDown {interval_minutes}m — Trade ejecutado: {last_trade.get('side')} "
                    f"@ {last_trade.get('entry_price',0):.3f} | ${last_trade.get('size_usdc',0):.2f} USDC | "
                    f"Confianza: {last_trade.get('confidence',0):.0f}% | Motivo scan: {reason}"
                )
            elif last_opp:
                return (
                    f"UpDown {interval_minutes}m — Scan completado, sin trade. "
                    f"Decisión: {last_opp.get('decision','?')} | "
                    f"Señal: {last_opp.get('signal','?')} | Confianza: {last_opp.get('confidence','?')}%"
                )
            else:
                return f"UpDown {interval_minutes}m — Scan ejecutado, no se encontró mercado activo."
        except Exception as e:
            return f"Error al ejecutar scan UpDown {interval_minutes}m: {e}"

    elif name == "update_params":
        params = inputs.get("params", {})
        reason = inputs.get("reason", "Cambio via chat")
        if not params:
            return "Error: no se proporcionaron parámetros a cambiar."
        # Guardar valores anteriores para confirmación
        old_values = {}
        valid_keys = {
            "max_position_usdc", "min_position_usdc", "kelly_fraction",
            "min_ev_threshold", "max_daily_loss_pct", "max_hours_to_resolution",
            "min_liquidity_usdc", "max_spread_pct", "min_volume_24h_usdc",
            "scan_interval_minutes", "weather_enabled", "btc_enabled",
            "btc_max_position_usdc", "updown_enabled",
            "updown_5m_enabled", "updown_15m_enabled",
            "updown_max_usdc", "updown_max_consecutive_losses",
            "alloc_weather_pct", "alloc_btc_pct", "alloc_updown_pct",
            "updown_15m_min_confidence", "updown_5m_min_confidence",
            "updown_15m_momentum_gate",  "updown_5m_momentum_gate",
            "updown_stake_min_usdc", "updown_stake_max_usdc",
            "updown_stake_conf_min_pct", "updown_stake_conf_max_pct",
            "updown_displacement_hi_pct", "updown_displacement_lo_pct",
            # Punto 17 — Trading Mode (Claude puede modificar en chat)
            "trading_mode_enabled", "trading_real_enabled",
            "trading_5m_enabled", "trading_15m_enabled", "trading_1d_enabled",
            "trading_entry_threshold", "trading_min_entry_price", "trading_max_entry_price",
            "trading_trend_prefer_winning", "trading_profit_offset",
            "trading_exit_deadline_min", "trading_min_entry_minutes_left",
            "trading_max_entries_per_market", "trading_max_open_per_side",
            "trading_stake_usdc", "trading_one_open_at_a_time",
            "trading_real_max_exposure_usdc", "trading_real_daily_loss_limit_usdc",
            "trading_real_max_consec_losses", "trading_real_killed",
            "trading_sl_enabled", "trading_sl_trigger_drop", "trading_sl_wait_min",
            "trading_sl_min_recover_factor",
            "trading_panic_trigger_drop", "trading_panic_min_recover_factor",
            "trading_buy_probable", "trading_probable_min_price",
            "trading_probable_max_price", "trading_probable_profit_offset",
            # Punto 19 — preflight gates + stale check
            "trading_real_drawdown_halt_pct",
            "trading_paper_required_days", "trading_paper_required_trades",
            "trading_paper_required_wr", "trading_paper_gate_override",
            "trading_max_price_age_sec",
            # Punto 2 v9.5.1 — phantom per-interval toggles
            "phantom_5m_enabled", "phantom_15m_enabled", "phantom_1d_enabled",
            "phantom_deadzone_enabled", "phantom_deadzone_min_conf", "phantom_deadzone_max_conf",
            # v9.5.6 — filtros de calidad phantom
            "phantom_min_conf_pct", "phantom_ta_mom_gate", "phantom_min_elapsed_15m",
        }
        invalid = [k for k in params if k not in valid_keys]
        if invalid:
            return f"Error: clave(s) de parámetro desconocida(s): {', '.join(invalid)}. Claves válidas: {', '.join(sorted(valid_keys))}"
        current = bot_params.to_dict()
        for k in params:
            old_values[k] = current.get(k, "N/A")
        try:
            bot_params.update(params)
            changes = "\n".join(f"  • {k}: {old_values[k]} → {params[k]}" for k in params)
            bot._log("INFO", f"[Chat] Parámetros actualizados por Claude: {', '.join(f'{k}={v}' for k,v in params.items())} | Motivo: {reason}")
            _save_param_history_entry(bot_id, "update_params", {"params": params}, old_values, True, reason)
            return f"Parámetros actualizados:\n{changes}\nMotivo: {reason}"
        except Exception as e:
            _save_param_history_entry(bot_id, "update_params", {"params": params}, old_values, False, reason)
            return f"Error al actualizar parámetros: {e}"

    elif name == "toggle_phantom_real":
        enabled = bool(inputs.get("enabled", False))
        reason  = inputs.get("reason", "Instrucción via chat")
        try:
            old_val = bot_params.phantom_real_enabled
            bot_params.phantom_real_enabled = enabled
            bot_params.save()
            mode = "REAL + FICTICIO" if enabled else "SOLO FICTICIO"
            bot._log("INFO", f"[Chat] Phantom real {'activado' if enabled else 'desactivado'} por Claude | Motivo: {reason}")
            _save_param_history_entry(bot_id, "toggle_phantom_real",
                                      {"enabled": enabled}, {"phantom_real_enabled": old_val}, True, reason)
            return f"Phantom cambiado a modo {mode}. Motivo: {reason}"
        except Exception as e:
            return f"Error al cambiar modo phantom: {e}"

    elif name == "set_phantom_capital":
        reason = inputs.get("reason", "Instrucción via chat")
        try:
            cash_libre = inputs.get("cash_libre_usdc")
            pool       = inputs.get("pool_usdc")
            pct_5m     = inputs.get("pct_5m")
            pct_15m    = inputs.get("pct_15m")

            if cash_libre is not None:
                bot_params.phantom_cash_libre_usdc = round(float(cash_libre), 4)
            if pool is not None:
                bot_params.phantom_pool_usdc = round(float(pool), 4)
            if pct_5m is not None:
                bot_params.phantom_bucket_5m_pct = round(float(pct_5m) / 100, 4)
            if pct_15m is not None:
                bot_params.phantom_bucket_15m_pct = round(float(pct_15m) / 100, 4)

            # Recalcular buckets
            _pool = bot_params.phantom_pool_usdc
            bot_params.phantom_bucket_5m_usdc  = round(_pool * bot_params.phantom_bucket_5m_pct, 4)
            bot_params.phantom_bucket_15m_usdc = round(_pool * bot_params.phantom_bucket_15m_pct, 4)
            bot_params.save()

            bot._log("INFO", f"[Chat] Capital phantom actualizado por Claude | Motivo: {reason}")
            _save_param_history_entry(bot_id, "set_phantom_capital",
                {"cash_libre_usdc": cash_libre, "pool_usdc": pool,
                 "pct_5m": pct_5m, "pct_15m": pct_15m}, {}, True, reason)
            return (
                f"Capital phantom actualizado:\n"
                f"  • Cash libre: ${bot_params.phantom_cash_libre_usdc:.2f}\n"
                f"  • Pool activo: ${bot_params.phantom_pool_usdc:.2f}\n"
                f"  • Bucket 5m: ${bot_params.phantom_bucket_5m_usdc:.2f} ({bot_params.phantom_bucket_5m_pct*100:.0f}%)\n"
                f"  • Bucket 15m: ${bot_params.phantom_bucket_15m_usdc:.2f} ({bot_params.phantom_bucket_15m_pct*100:.0f}%)\n"
                f"Motivo: {reason}"
            )
        except Exception as e:
            return f"Error al configurar capital phantom: {e}"

    elif name == "reset_updown_circuit_breaker":
        interval = int(inputs.get("interval_minutes", 15))
        reason   = inputs.get("reason", "Instrucción via chat")
        try:
            if interval == 5:
                bot.state.updown_5m_consecutive_losses = 0
                bot.state.updown_5m_stopped = False
            else:
                bot.state.updown_15m_consecutive_losses = 0
                bot.state.updown_15m_stopped = False
            bot._log("INFO", f"[Chat] Circuit breaker UpDown {interval}m reseteado por Claude | Motivo: {reason}")
            return f"Circuit breaker UpDown {interval}m reseteado. El bot puede volver a operar en {interval}m. Motivo: {reason}"
        except Exception as e:
            return f"Error al resetear circuit breaker: {e}"

    elif name == "analyze_bot":
        interval = int(inputs.get("interval_minutes", 5))
        try:
            from markets_updown import fetch_updown_market
            from strategy_updown import evaluate_updown_market, build_btc_direction_signal
            from price_feed import get_btc_price, get_btc_ta
            ta_interval = "1m" if interval == 5 else "5m"
            market = await fetch_updown_market(interval)
            if not market:
                return f"Sin mercado UpDown {interval}m activo en este momento."
            ta_data = await get_btc_ta(interval=ta_interval)
            btc_now = bot.state.btc_price or await get_btc_price()
            btc_start = await bot._get_btc_price_at_ts(market["window_start_ts"])
            if not btc_start:
                return f"No se pudo obtener precio BTC al inicio de ventana para {interval}m."
            sig = build_btc_direction_signal(
                ta_data=ta_data, btc_price=btc_now or 0,
                btc_price_window_start=btc_start, cmc_data=bot.state.btc_cmc or {},
            )
            opp, reason_skip = evaluate_updown_market(
                market=market, ta_data=ta_data,
                btc_price=btc_now or 0, btc_price_window_start=btc_start,
                cmc_data=bot.state.btc_cmc or {},
            )
            move_pct = round((btc_now - btc_start) / btc_start * 100, 4) if btc_start else None
            result = [
                f"=== ANÁLISIS BOT UPDOWN {interval}m ===",
                f"BTC ahora: ${btc_now:,.0f} | Inicio ventana: ${btc_start:,.0f} | Movimiento: {move_pct:+.3f}%" if move_pct is not None else f"BTC: ${btc_now:,.0f}",
                f"Mercado: {market.get('slug','')} | Elapsed: {market.get('elapsed_minutes',0):.1f}m | Cierra en: {market.get('minutes_to_close',0):.1f}m",
                f"UP/DOWN precios: {market.get('up_price',0)*100:.1f}¢ / {market.get('down_price',0)*100:.1f}¢",
                f"TA: {ta_data.get('recommendation','?')} [↑{ta_data.get('buy',0)} ↔{ta_data.get('neutral',0)} ↓{ta_data.get('sell',0)}] RSI={ta_data.get('rsi',0):.1f}",
                f"Señal combinada: {sig.get('direction','?')} | Confianza: {sig.get('confidence',0):.1f}% | Combined: {sig.get('combined',0):.4f}",
            ]
            if sig.get("5m_mode"):
                result.append(f"Modo 5m: {sig['5m_mode']}")
            if opp:
                result.append(f"DECISIÓN: TRADE {opp['side']} @ {opp['entry_price']*100:.1f}¢ | ${opp['size_usdc']:.2f} | {opp['confidence']:.1f}% confianza")
            else:
                result.append(f"DECISIÓN: SKIP — {reason_skip or 'señal insuficiente'}")
            return "\n".join(result)
        except Exception as e:
            return f"Error al analizar bot {interval}m: {e}"

    return f"Herramienta desconocida: {name}"


@app.get("/api/chat/models")
async def list_chat_models():
    """Punto 16 — devuelve la lista de modelos Claude disponibles para el chat."""
    import os as _os
    default = _os.getenv("CLAUDE_CHAT_MODEL", _os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
    return {
        "default": default,
        "models": [
            {"id": "claude-opus-4-7",           "name": "Opus 4.7",           "tier": "premium"},
            {"id": "claude-opus-4-6",           "name": "Opus 4.6",           "tier": "premium"},
            {"id": "claude-sonnet-4-6",         "name": "Sonnet 4.6",         "tier": "balanced"},
            {"id": "claude-sonnet-4-5",         "name": "Sonnet 4.5",         "tier": "balanced"},
            {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5",          "tier": "fast"},
        ],
    }


@app.post("/api/chat")
async def chat_endpoint(data: dict):
    from claude_analyst import get_client
    import json as _json

    messages = data.get("messages", [])
    if not messages:
        return JSONResponse(status_code=400, content={"error": "Sin mensajes"})

    client = get_client()
    if client is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Claude no configurado. Agrega ANTHROPIC_API_KEY en .env y reinicia con python main.py"},
        )

    import os as _os
    # Chat usa sonnet para mejor razonamiento con herramientas; CLAUDE_CHAT_MODEL lo sobreescribe
    _default_model = _os.getenv("CLAUDE_CHAT_MODEL", _os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
    # Punto 16 — selector de modelo desde UI (whitelist para evitar modelos inválidos)
    _ALLOWED_MODELS = {
        "claude-opus-4-7", "claude-opus-4-6",
        "claude-sonnet-4-6", "claude-sonnet-4-5",
        "claude-haiku-4-5-20251001",
    }
    _req_model = (data.get("model") or "").strip()
    model = _req_model if _req_model in _ALLOWED_MODELS else _default_model
    bot_id = data.get("bot_id")  # presente solo en brain chat
    try:
        if bot_id:
            # Brain chat: sistema enfocado solo en el bot solicitado
            _bot_names = {
                "ud5m": "UpDown 5m (Real)", "ud15m": "UpDown 15m (Real)",
                "ph5m": "Phantom 5m",        "ph15m": "Phantom 15m",
            }
            _bot_name = _bot_names.get(bot_id, bot_id)
            _interval = "15" if "15m" in bot_id else "5"
            _ctx = _build_chat_context()
            system = (
                f"Eres el asistente especializado del bot '{_bot_name}' (intervalo {_interval}m). "
                f"Tu rol es EXCLUSIVAMENTE ayudar con este bot: analizar su rendimiento, "
                f"modificar sus parámetros mediante herramientas, y responder preguntas sobre él. "
                f"NO hagas cambios a otros bots. NO hables de otros bots a menos que el usuario lo pida explícitamente para comparar. "
                f"Cuando el usuario diga 'el bot' o 'este bot', siempre se refiere a '{_bot_name}'. "
                f"Usa update_params con interval_minutes={_interval} cuando modifiques parámetros.\n\n"
                + CHAT_SYSTEM.replace("{context}", _ctx)
            )
        else:
            system = CHAT_SYSTEM.replace("{context}", _build_chat_context())
    except Exception as _ctx_err:
        import traceback as _tb
        return JSONResponse(status_code=500, content={"error": f"Error building context: {_ctx_err}\n{_tb.format_exc()}"})

    async def stream():
        try:
            loop_messages = [{"role": m["role"], "content": m["content"]} for m in messages]

            for _ in range(6):  # máximo 6 iteraciones (3 tool calls + respuestas)
                response = await client.messages.create(
                    model=model,
                    max_tokens=1024,
                    system=system,
                    messages=loop_messages,
                    tools=CHAT_TOOLS,
                    tool_choice={"type": "auto"},
                )

                # Enviar texto de esta iteración al frontend
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        yield f"data: {_json.dumps({'text': block.text})}\n\n"

                # Si no hay tool_use, terminamos
                if response.stop_reason != "tool_use":
                    break

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                if not tool_use_blocks:
                    break

                # Ejecutar herramientas
                tool_results = []
                for tb in tool_use_blocks:
                    yield f"data: {_json.dumps({'tool_call': {'name': tb.name, 'input': tb.input}})}\n\n"
                    result = await _execute_chat_tool(tb.name, tb.input, bot_id=bot_id)
                    yield f"data: {_json.dumps({'tool_result': result})}\n\n"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tb.id,
                        "content": result,
                    })

                # Añadir turno del asistente y resultados de herramientas
                assistant_content = []
                for block in response.content:
                    if block.type == "text":
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                loop_messages.append({"role": "assistant", "content": assistant_content})
                loop_messages.append({"role": "user", "content": tool_results})

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Chat history endpoints ────────────────────────────────────────────────

@app.get("/api/chats")
async def list_chats():
    """Lista todas las conversaciones (sin messages completos)."""
    chats = _load_chats()
    result = []
    for c in chats.values():
        result.append({
            "id":         c["id"],
            "title":      c["title"],
            "created_at": c["created_at"],
            "updated_at": c["updated_at"],
            "msg_count":  len(c["messages"]),
        })
    result.sort(key=lambda x: x["updated_at"], reverse=True)
    return result


@app.get("/api/chats/{chat_id}")
async def get_chat(chat_id: str):
    chats = _load_chats()
    if chat_id not in chats:
        return JSONResponse(status_code=404, content={"error": "No encontrado"})
    return chats[chat_id]


@app.post("/api/chats")
async def save_chat(data: dict):
    """Crea o actualiza una conversación. Si no hay id, genera uno nuevo."""
    chats = _load_chats()
    chat_id = data.get("id") or str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    messages = data.get("messages", [])
    # Titulo = primer mensaje del usuario, truncado
    title = next((m["content"][:60] for m in messages if m["role"] == "user"), "Conversación")
    chats[chat_id] = {
        "id":         chat_id,
        "title":      title,
        "created_at": chats.get(chat_id, {}).get("created_at", now),
        "updated_at": now,
        "messages":   messages,
    }
    _save_chats(chats)
    return {"id": chat_id, "title": title}


@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    chats = _load_chats()
    if chat_id in chats:
        del chats[chat_id]
        _save_chats(chats)
    return {"ok": True}


# ── Telonex endpoints ─────────────────────────────────────────────────────

@app.get("/api/telonex/status")
async def get_telonex_status():
    """Estado del módulo Telonex: última actualización, wallets top, OFI disponible."""
    return telonex_data.get_status()


@app.get("/api/telonex/top-wallets")
async def get_telonex_top_wallets():
    """Lista de top smart wallets con PnL y posición de sesgo."""
    status = telonex_data.get_status()
    wallets = status.get("top_wallets", [])
    return {"wallets": wallets, "count": len(wallets)}


@app.post("/api/telonex/update-wallets")
async def refresh_telonex_wallets():
    """Fuerza actualización del ranking de smart wallets (puede tardar ~10s)."""
    try:
        await telonex_data.update_top_wallets(force=True)
        return {"ok": True, "message": "Wallets actualizados"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── Capital Buckets (Fase 12) ──────────────────────────────────────────────

@app.get("/api/capital")
async def get_capital():
    """Estado actual del sistema de buckets de capital."""
    bp = bot_params
    balance = round(bot.state.balance_usdc, 2)
    bucket_sum = round(
        bp.bucket_weather_usdc + bp.bucket_btc_usdc +
        bp.bucket_updown_5m_usdc + bp.bucket_updown_15m_usdc, 2
    )
    cash_free = round(max(0.0, balance - bucket_sum), 2)
    return {
        "active": bp.betting_pool_usdc > 0,
        "betting_pool_usdc": round(bp.betting_pool_usdc, 2),
        "cash_free": cash_free,
        "balance_usdc": balance,
        "buckets": {
            "weather":    {"usdc": round(bp.bucket_weather_usdc, 2),    "pct": round(bp.bucket_weather_pct, 2)},
            "btc":        {"usdc": round(bp.bucket_btc_usdc, 2),        "pct": round(bp.bucket_btc_pct, 2)},
            "updown_5m":  {"usdc": round(bp.bucket_updown_5m_usdc, 2),  "pct": round(bp.bucket_updown_5m_pct, 2)},
            "updown_15m": {"usdc": round(bp.bucket_updown_15m_usdc, 2), "pct": round(bp.bucket_updown_15m_pct, 2)},
        },
    }


@app.post("/api/capital/assign")
async def assign_capital(data: dict):
    """
    Asigna capital al pool de apuestas y define los buckets.

    Body (todos opcionales):
      pool_usdc:         float  — nuevo total del pool de apuestas
      weather_pct:       float  — % para weather (0-1)
      btc_pct:           float  — % para BTC
      updown_5m_pct:     float  — % para UpDown 5m
      updown_15m_pct:    float  — % para UpDown 15m
      fill_buckets:      bool   — si True, rellena los buckets según los % (default True)

    Si fill_buckets=True los buckets se recalculan: bucket_X = pool_usdc * pct_X.
    Si fill_buckets=False solo se guardan los parámetros sin tocar los saldos actuales.
    """
    bp = bot_params

    pool = float(data.get("pool_usdc", bp.betting_pool_usdc))
    w_pct  = float(data.get("weather_pct",    bp.bucket_weather_pct))
    b_pct  = float(data.get("btc_pct",        bp.bucket_btc_pct))
    u5_pct = float(data.get("updown_5m_pct",  bp.bucket_updown_5m_pct))
    u15_pct= float(data.get("updown_15m_pct", bp.bucket_updown_15m_pct))
    fill   = bool(data.get("fill_buckets", True))

    if pool < 0:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "pool_usdc no puede ser negativo"})
    total_pct = w_pct + b_pct + u5_pct + u15_pct
    if total_pct > 1.001:
        return JSONResponse(status_code=400, content={"ok": False, "msg": f"La suma de porcentajes es {total_pct:.2f} > 1.0"})

    update = {
        "betting_pool_usdc":   round(pool, 2),
        "bucket_weather_pct":   round(w_pct, 4),
        "bucket_btc_pct":       round(b_pct, 4),
        "bucket_updown_5m_pct": round(u5_pct, 4),
        "bucket_updown_15m_pct":round(u15_pct, 4),
    }
    if fill:
        update["bucket_weather_usdc"]    = round(pool * w_pct, 4)
        update["bucket_btc_usdc"]        = round(pool * b_pct, 4)
        update["bucket_updown_5m_usdc"]  = round(pool * u5_pct, 4)
        update["bucket_updown_15m_usdc"] = round(pool * u15_pct, 4)

    bot_params.update(update)
    bot._log(
        "INFO",
        f"Capital asignado — Pool: ${pool:.2f} | "
        f"Weather {w_pct*100:.0f}% (${pool*w_pct:.2f}) | "
        f"BTC {b_pct*100:.0f}% (${pool*b_pct:.2f}) | "
        f"UpDown5m {u5_pct*100:.0f}% (${pool*u5_pct:.2f}) | "
        f"UpDown15m {u15_pct*100:.0f}% (${pool*u15_pct:.2f})",
    )
    return {"ok": True, **update}


@app.post("/api/capital/reload/{market}")
async def reload_bucket(market: str):
    """
    Recarga el bucket de un mercado hasta su asignación configurada
    (pool_usdc * bucket_pct). Solo opera si el sistema de buckets está activo.

    market: 'weather' | 'btc' | 'updown_5m' | 'updown_15m'
    """
    bp = bot_params
    if bp.betting_pool_usdc <= 0:
        return JSONResponse(status_code=400, content={"ok": False, "msg": "Sistema de buckets no activo. Asigna un pool primero."})

    markets_map = {
        "weather":    ("bucket_weather_usdc",    "bucket_weather_pct"),
        "btc":        ("bucket_btc_usdc",        "bucket_btc_pct"),
        "updown_5m":  ("bucket_updown_5m_usdc",  "bucket_updown_5m_pct"),
        "updown_15m": ("bucket_updown_15m_usdc", "bucket_updown_15m_pct"),
    }
    if market not in markets_map:
        return JSONResponse(status_code=400, content={"ok": False, "msg": f"Mercado desconocido: {market}"})

    usdc_attr, pct_attr = markets_map[market]
    target = round(bp.betting_pool_usdc * getattr(bp, pct_attr), 2)
    bot_params.update({usdc_attr: target})
    bot._log("INFO", f"Capital | Bucket '{market}' recargado → ${target:.2f}")
    return {"ok": True, "market": market, "bucket_usdc": target}


# ── Phantom Tab: status, toggle, capital ──────────────────────────────────

@app.get("/api/phantom/status")
async def phantom_status():
    """Estado completo del tab Phantom: capital, buckets, métricas, learner."""
    try:
        from bot import bot_params
        import json as _json
        from updown_learner import get_summary as _ud_summary

        # Capital phantom
        cash_libre  = bot_params.phantom_cash_libre_usdc   # reserva libre (no en juego)
        b5m         = bot_params.phantom_bucket_5m_usdc    # pool activo 5m
        b15m        = bot_params.phantom_bucket_15m_usdc   # pool activo 15m
        pool        = round(b5m + b15m, 4)                 # pool total activo
        in_bets     = round(bot_params.phantom_pool_usdc - pool, 4)  # en apuestas abiertas

        # Métricas del experimento VPS
        vps_file = os.path.join("data", "vps_phantom_experiment.json")
        total_trades = wins = 0
        total_pnl_vps = total_pnl_real = 0.0
        if os.path.exists(vps_file):
            with open(vps_file, "r", encoding="utf-8") as f:
                vps_data = _json.load(f)
            resolved = [t for t in vps_data.get("trades", []) if t.get("result") in ("WIN", "LOSS")]
            total_trades = len(resolved)
            wins         = sum(1 for t in resolved if t.get("result") == "WIN")
            total_pnl_vps  = round(sum(t.get("pnl_vps",   0) or 0 for t in resolved), 2)
            total_pnl_real = round(sum(
                (t.get("pnl_vps", 0) or 0)
                for t in resolved if t.get("used_real_money")
            ), 2)

        win_rate = round(wins / total_trades * 100, 1) if total_trades else 0.0

        # ── Win rates por intervalo (phantom_learner = fuente única de verdad) ─
        try:
            from phantom_learner import get_total_win_rate, _AUTORULE_MIN_SAMPLES, _stats as _pl_st
            _wr5  = get_total_win_rate(5)
            _wr15 = get_total_win_rate(15)
            _wr5_pct  = round(_wr5  * 100, 1) if _wr5  is not None else None
            _wr15_pct = round(_wr15 * 100, 1) if _wr15 is not None else None

            # Totales por intervalo desde phantom_learner (fuente única, no VPS combinado)
            _s5  = _pl_st.get("5",  {})
            _s15 = _pl_st.get("15", {})
            _total5  = _s5.get("total", 0)
            _total15 = _s15.get("total", 0)
            _wins5   = _s5.get("wins", 0)
            _wins15  = _s15.get("wins", 0)

            if _wr5 is None and _wr15 is None:
                _autorule_status = "waiting"   # esperando datos
            elif (_wr5 is not None and _wr5 < 0.50) or (_wr15 is not None and _wr15 < 0.50):
                _autorule_status = "disabled"  # alguno < 50% → desactiva
            elif (_wr5 is None or _wr5 > 0.70) and (_wr15 is None or _wr15 > 0.70):
                _autorule_status = "enabled"   # ambos > 70% → activa
            else:
                _autorule_status = "neutral"   # zona neutral, sin cambio
        except Exception:
            _wr5_pct = _wr15_pct = None
            _autorule_status = "waiting"
            _total5 = _total15 = _wins5 = _wins15 = 0

        return {
            "phantom_real_enabled":    bot_params.phantom_real_enabled,
            "phantom_real_always":     getattr(bot_params, "phantom_real_always", False),
            "phantom_cash_libre_usdc": cash_libre,
            "phantom_pool_usdc":       pool,
            "phantom_pool_max_usdc":   bot_params.phantom_pool_usdc,
            "phantom_bucket_5m_pct":   bot_params.phantom_bucket_5m_pct,
            "phantom_bucket_15m_pct":  bot_params.phantom_bucket_15m_pct,
            "phantom_bucket_5m_usdc":  b5m,
            "phantom_bucket_15m_usdc": b15m,
            "phantom_5m_enabled":      bool(getattr(bot_params, "phantom_5m_enabled", True)),
            "phantom_15m_enabled":     bool(getattr(bot_params, "phantom_15m_enabled", True)),
            "phantom_1d_enabled":      bool(getattr(bot_params, "phantom_1d_enabled", False)),
            "phantom_deadzone_enabled":  bool(getattr(bot_params, "phantom_deadzone_enabled", True)),
            "phantom_deadzone_min_conf": float(getattr(bot_params, "phantom_deadzone_min_conf", 20.0)),
            "phantom_deadzone_max_conf": float(getattr(bot_params, "phantom_deadzone_max_conf", 34.0)),
            "phantom_min_conf_pct":     float(getattr(bot_params, "phantom_min_conf_pct",    35.0)),
            "phantom_ta_mom_gate":      bool(getattr(bot_params,  "phantom_ta_mom_gate",     True)),
            "phantom_min_elapsed_15m":  float(getattr(bot_params, "phantom_min_elapsed_15m", 8.0)),
            "in_bets":     max(0.0, in_bets),
            "total_trades":  total_trades,
            "wins":          wins,
            "win_rate_pct":  win_rate,
            "wins_5m":       _wins5,
            "total_5m":      _total5,
            "win_rate_5m_pct": _wr5_pct,
            "wins_15m":      _wins15,
            "total_15m":     _total15,
            "win_rate_15m_pct": _wr15_pct,
            "total_pnl_vps": total_pnl_vps,
            "total_pnl_real": total_pnl_real,
            "learner_5m":    _ud_summary(5),
            "learner_15m":   _ud_summary(15),
            "autorule_wr5_pct":   _wr5_pct,
            "autorule_wr15_pct":  _wr15_pct,
            "autorule_status":    _autorule_status,
            "autorule_min_trades": _AUTORULE_MIN_SAMPLES,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/phantom/toggle")
async def phantom_toggle(body: dict):
    """Activa o desactiva el modo phantom real. body: {enabled: bool}"""
    try:
        from bot import bot_params
        enabled = bool(body.get("enabled", False))
        bot_params.phantom_real_enabled = enabled
        bot_params.save()
        mode = "REAL+FICTICIO" if enabled else "SOLO FICTICIO"
        return {"ok": True, "phantom_real_enabled": enabled, "mode": mode}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/phantom/toggle_always")
async def phantom_toggle_always(body: dict):
    """Activa o desactiva el modo 'phantom real siempre'. body: {enabled: bool}"""
    try:
        from bot import bot_params
        enabled = bool(body.get("enabled", False))
        bot_params.phantom_real_always = enabled
        bot_params.save()
        return {"ok": True, "phantom_real_always": enabled}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/phantom/interval_toggle")
async def phantom_interval_toggle(body: dict):
    """Toggle phantom por intervalo (5m/15m/1d). body: {interval: '5m'|'15m'|'1d', enabled: bool}"""
    try:
        from bot import bot_params
        interval = str(body.get("interval", "")).lower()
        enabled  = bool(body.get("enabled", False))
        attr_map = {"5m": "phantom_5m_enabled", "15m": "phantom_15m_enabled", "1d": "phantom_1d_enabled"}
        attr = attr_map.get(interval)
        if not attr:
            return {"ok": False, "error": f"interval inválido: {interval}"}
        setattr(bot_params, attr, enabled)
        bot_params.save()
        return {"ok": True, "interval": interval, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/phantom/capital")
async def phantom_capital(body: dict):
    """
    Configura capital phantom.
    body: {cash_libre_usdc, pool_usdc, pct_5m, pct_15m}
      cash_libre_usdc = reserva libre phantom (lo que queda fuera del pool activo)
      pool_usdc       = pool activo de apuestas
    Ambos se guardan directamente — el total es solo la suma de los dos.
    """
    try:
        from bot import bot_params
        cash_libre = float(body.get("cash_libre_usdc", bot_params.phantom_cash_libre_usdc))
        pool       = float(body.get("pool_usdc",       bot_params.phantom_pool_usdc))
        p5m        = float(body.get("pct_5m",  bot_params.phantom_bucket_5m_pct))
        p15m       = float(body.get("pct_15m", bot_params.phantom_bucket_15m_pct))
        if p5m + p15m > 1.01:
            return {"ok": False, "error": "pct_5m + pct_15m no puede superar 100%"}
        bot_params.phantom_cash_libre_usdc = round(cash_libre, 4)
        bot_params.phantom_pool_usdc       = round(pool, 4)
        bot_params.phantom_bucket_5m_pct   = round(p5m,  4)
        bot_params.phantom_bucket_15m_pct  = round(p15m, 4)
        bot_params.phantom_bucket_5m_usdc  = round(pool * p5m,  4)
        bot_params.phantom_bucket_15m_usdc = round(pool * p15m, 4)
        bot_params.save()
        return {
            "ok": True,
            "cash_libre": cash_libre,
            "pool_usdc":  pool,
            "pct_5m": p5m, "pct_15m": p15m,
            "bucket_5m": bot_params.phantom_bucket_5m_usdc,
            "bucket_15m": bot_params.phantom_bucket_15m_usdc,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/phantom/reload-buckets")
async def phantom_reload_buckets():
    """Recarga los buckets phantom a partir del pool asignado y los porcentajes."""
    try:
        from bot import bot_params
        pool = bot_params.phantom_pool_usdc
        bot_params.phantom_bucket_5m_usdc  = round(pool * bot_params.phantom_bucket_5m_pct,  4)
        bot_params.phantom_bucket_15m_usdc = round(pool * bot_params.phantom_bucket_15m_pct, 4)
        bot_params.save()
        return {
            "ok": True,
            "phantom_bucket_5m_usdc":  bot_params.phantom_bucket_5m_usdc,
            "phantom_bucket_15m_usdc": bot_params.phantom_bucket_15m_usdc,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Experimento VPS-Confianza ──────────────────────────────────────────────

@app.get("/api/vps-experiment")
async def get_vps_experiment():
    """Estado actual del experimento VPS-Confianza (phantom, sin dinero real)."""
    try:
        from vps_experiment import get_status as _vps_status
        return _vps_status()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/vps-experiment/trades")
async def get_vps_trades(page: int = 0, limit: int = 50):
    """
    Historial paginado de trades phantom.
    Devuelve los trades más recientes primero.
    page=0 → primeros 50, page=1 → siguientes 50, etc.
    """
    try:
        import json as _json
        vps_file = os.path.join("data", "vps_phantom_experiment.json")
        if not os.path.exists(vps_file):
            return {"trades": [], "total": 0, "page": page, "limit": limit, "has_more": False}
        with open(vps_file, "r", encoding="utf-8") as f:
            data = _json.load(f)
        all_trades = list(reversed(data.get("trades", [])))  # más reciente primero
        total = len(all_trades)
        offset = page * limit
        page_trades = all_trades[offset: offset + limit]
        return {
            "trades":   page_trades,
            "total":    total,
            "page":     page,
            "limit":    limit,
            "has_more": (offset + limit) < total,
        }
    except Exception as e:
        return {"trades": [], "total": 0, "error": str(e)}


@app.post("/api/vps-experiment/daily-summary")
async def vps_daily_summary():
    """Fuerza generación del resumen del día actual del experimento VPS."""
    try:
        from vps_experiment import force_daily_summary as _vps_sum
        return _vps_sum()
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/phantom-analysis")
async def get_phantom_analysis(interval: int = None):
    """Analiza patrones en trades phantom para detectar por qué alta confianza pierde."""
    try:
        from phantom_analysis import analyze_phantom_trades
        result = analyze_phantom_trades(interval=interval)
        return result
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _fetch_anthropic_credits(api_key: str) -> dict | None:
    """
    Intenta obtener el saldo de créditos de la cuenta Anthropic.
    Prueba el endpoint de usage de la Admin API.
    Retorna dict con 'credits_usd' si tiene éxito, None si no está disponible.
    """
    import httpx
    endpoints = [
        "https://api.anthropic.com/v1/organizations/billing/credits",
        "https://api.anthropic.com/v1/usage/credits",
    ]
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    for url in endpoints:
        try:
            r = httpx.get(url, headers=headers, timeout=5.0)
            if r.status_code == 200:
                data = r.json()
                # Distintos posibles nombres del campo de créditos
                for key in ("credit_balance", "credits", "balance", "available_credits"):
                    if key in data:
                        return {"credits_usd": float(data[key])}
                return {"credits_usd": None, "raw": data}
        except Exception:
            continue
    return None


@app.get("/api/claude-status")
async def get_claude_status():
    """
    Verifica el estado de la API de Claude (Anthropic) y obtiene saldo de créditos.
    El saldo de créditos se consulta vía Admin API; si la clave no tiene permisos
    de Admin, devuelve credits_usd=null y se muestra link a la consola.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "ok": False,
            "status": "NO_KEY",
            "msg": "ANTHROPIC_API_KEY no configurada",
            "credits_usd": None,
            "last_call": None,
            "console_url": "https://console.anthropic.com/settings/billing",
        }

    # Intentar obtener saldo de créditos (puede fallar si no es admin key)
    credits_info = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _fetch_anthropic_credits(api_key)
    )
    credits_usd = credits_info.get("credits_usd") if credits_info else None

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        # Mini-llamada para verificar validez de la clave
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
        )
        return {
            "ok": True,
            "status": "OK",
            "msg": f"API activa — modelo {resp.model}",
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
            "credits_usd": credits_usd,
            "last_call": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "console_url": "https://console.anthropic.com/settings/billing",
        }
    except Exception as e:
        err_str = str(e)
        status = "AUTH_ERROR" if "authentication" in err_str.lower() or "api_key" in err_str.lower() else "ERROR"
        return {
            "ok": False,
            "status": status,
            "msg": err_str[:120],
            "credits_usd": credits_usd,
            "last_call": None,
            "console_url": "https://console.anthropic.com/settings/billing",
        }


@app.post("/api/vps-experiment/reset")
async def reset_vps_experiment():
    """Reinicia el experimento VPS eliminando los datos y empezando de nuevo."""
    import os
    try:
        data_file = os.path.join("data", "vps_phantom_experiment.json")
        if os.path.exists(data_file):
            os.remove(data_file)
        from vps_experiment import _load as _vps_load
        _vps_load()  # crea estructura inicial
        return {"ok": True, "msg": "Experimento reiniciado"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/vps-experiment/set-balance")
async def set_vps_balance(body: dict):
    """Reset experimento VPS con saldo virtual custom. Borra trades, daily_summaries
    y reinicia balances + stats (todo arranca de cero con el balance pedido).
    body: {balance: float}"""
    try:
        bal = float(body.get("balance", 0))
        if bal < 0:
            return {"ok": False, "error": "balance debe ser >= 0"}
        from vps_experiment import reset_with_balance
        reset_with_balance(bal)
        return {"ok": True, "msg": f"Experimento reiniciado con saldo ${bal:.2f}", "balance": bal}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Trading Mode (v9.4) ────────────────────────────────────────────────────

async def _safe_enrich(is_real: bool):
    try:
        from trading_runner import enrich_open_positions
        return await enrich_open_positions(is_real=is_real)
    except Exception:
        import trading_positions as _tp
        return _tp.all_open_positions(is_real=is_real)


@app.get("/api/trading/state")
async def get_trading_state():
    """
    Estado completo del trading mode: params, stats, posiciones abiertas,
    historial reciente. Usado por el panel UI.
    """
    try:
        import trading_positions as tp
        is_real_enabled = bool(getattr(bot_params, "trading_real_enabled", False))
        max_consec = int(getattr(bot_params, "trading_real_max_consec_losses", 3))
        max_exposure = float(getattr(bot_params, "trading_real_max_exposure_usdc", 20.0))
        daily_limit  = float(getattr(bot_params, "trading_real_daily_loss_limit_usdc", 5.0))
        exposure_now = tp.real_exposure_usdc()
        pnl_today    = tp.real_pnl_today_usdc()
        consec       = tp.real_consecutive_losses()
        pending_red  = tp.real_pending_redemption_usdc(48)
        return {
            "enabled":      bool(getattr(bot_params, "trading_mode_enabled", False)),
            "real_enabled": is_real_enabled,
            "intervals": {
                "5m":  bool(getattr(bot_params, "trading_5m_enabled",  False)),
                "15m": bool(getattr(bot_params, "trading_15m_enabled", True)),
                "1d":  bool(getattr(bot_params, "trading_1d_enabled",  False)),
            },
            "params": {
                "entry_threshold":         getattr(bot_params, "trading_entry_threshold", 0.35),
                "min_entry_price":         getattr(bot_params, "trading_min_entry_price", 0.10),
                "max_entry_price":         getattr(bot_params, "trading_max_entry_price", 0.30),
                "profit_offset":           getattr(bot_params, "trading_profit_offset", 0.30),
                "exit_deadline_min":       getattr(bot_params, "trading_exit_deadline_min", 3.0),
                "min_entry_minutes_left":  getattr(bot_params, "trading_min_entry_minutes_left", 6.0),
                "max_entries_per_market":  getattr(bot_params, "trading_max_entries_per_market", 3),
                "max_open_per_side":       getattr(bot_params, "trading_max_open_per_side", 2),
                "stake_usdc":              getattr(bot_params, "trading_stake_usdc", 5.0),
                "one_open_at_a_time":      bool(getattr(bot_params, "trading_one_open_at_a_time", True)),
                "real_max_exposure_usdc":      max_exposure,
                "real_daily_loss_limit_usdc":  daily_limit,
                "real_max_consec_losses":      max_consec,
                "sl_enabled":                  bool(getattr(bot_params, "trading_sl_enabled", True)),
                "sl_trigger_drop":             getattr(bot_params, "trading_sl_trigger_drop", 0.50),
                "sl_wait_min":                 getattr(bot_params, "trading_sl_wait_min", 3.0),
                "sl_min_recover_factor":       getattr(bot_params, "trading_sl_min_recover_factor", 0.50),
                "panic_trigger_drop":          getattr(bot_params, "trading_panic_trigger_drop", 0.80),
                "panic_min_recover_factor":    getattr(bot_params, "trading_panic_min_recover_factor", 0.33),
                "buy_probable":                bool(getattr(bot_params, "trading_buy_probable", True)),
                "probable_min_price":          getattr(bot_params, "trading_probable_min_price", 0.55),
                "probable_max_price":          getattr(bot_params, "trading_probable_max_price", 0.85),
                "probable_profit_offset":      getattr(bot_params, "trading_probable_profit_offset", 0.08),
                "real_drawdown_halt_pct":      getattr(bot_params, "trading_real_drawdown_halt_pct", 0.40),
                "paper_required_days":         getattr(bot_params, "trading_paper_required_days", 7.0),
                "paper_required_trades":       getattr(bot_params, "trading_paper_required_trades", 200),
                "paper_required_wr":           getattr(bot_params, "trading_paper_required_wr", 0.75),
                "paper_gate_override":         bool(getattr(bot_params, "trading_paper_gate_override", False)),
                "max_price_age_sec":           getattr(bot_params, "trading_max_price_age_sec", 10.0),
            },
            "real_safety": {
                "killed":          bool(getattr(bot_params, "trading_real_killed", False)),
                "exposure_usdc":   exposure_now,
                "exposure_cap":    max_exposure,
                "pnl_today_usdc":  pnl_today,
                "daily_loss_cap":  daily_limit,
                "consec_losses":   consec,
                "consec_cap":      max_consec,
                "pending_redeem_usdc": pending_red["total_usdc"],
                "pending_redeem_count": pending_red["count"],
                "drawdown":        tp.real_equity_drawdown(),
                "paper_gate":      tp.phantom_gate_status(
                    required_days=float(getattr(bot_params, "trading_paper_required_days", 7.0)),
                    required_trades=int(getattr(bot_params, "trading_paper_required_trades", 200)),
                    required_wr=float(getattr(bot_params, "trading_paper_required_wr", 0.75)),
                ),
            },
            "phantom": {
                "stats": tp.stats_summary(is_real=False),
                "open":  await _safe_enrich(False),
                "history": tp.get_all_positions_flat(is_real=False, limit=100),
            },
            "real": {
                "stats": tp.stats_summary(is_real=True) if is_real_enabled else None,
                "open":  (await _safe_enrich(True)) if is_real_enabled else [],
                "history": tp.get_all_positions_flat(is_real=True, limit=100) if is_real_enabled else [],
            },
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/trading/params")
async def set_trading_params(body: dict):
    """Actualiza parámetros de trading en caliente."""
    try:
        mapping = {
            "trading_mode_enabled":           bool,
            "trading_real_enabled":           bool,
            "trading_5m_enabled":             bool,
            "trading_15m_enabled":            bool,
            "trading_1d_enabled":             bool,
            "trading_entry_threshold":        float,
            "trading_min_entry_price":        float,
            "trading_max_entry_price":        float,
            "trading_trend_prefer_winning":   bool,
            "trading_profit_offset":          float,
            "trading_exit_deadline_min":      float,
            "trading_min_entry_minutes_left": float,
            "trading_max_entries_per_market": int,
            "trading_max_open_per_side":      int,
            "trading_stake_usdc":             float,
            "trading_one_open_at_a_time":     bool,
            "trading_real_max_exposure_usdc":     float,
            "trading_real_daily_loss_limit_usdc": float,
            "trading_real_max_consec_losses":     int,
            "trading_real_killed":                bool,
            "trading_sl_enabled":                 bool,
            "trading_sl_trigger_drop":            float,
            "trading_sl_wait_min":                float,
            "trading_sl_min_recover_factor":      float,
            "trading_panic_trigger_drop":         float,
            "trading_panic_min_recover_factor":   float,
            "trading_buy_probable":               bool,
            "trading_probable_min_price":         float,
            "trading_probable_max_price":         float,
            "trading_probable_profit_offset":     float,
            "trading_real_drawdown_halt_pct":     float,
            "trading_paper_required_days":        float,
            "trading_paper_required_trades":      int,
            "trading_paper_required_wr":          float,
            "trading_paper_gate_override":        bool,
            "trading_max_price_age_sec":          float,
        }
        changed = {}
        for key, caster in mapping.items():
            if key in body:
                try:
                    val = caster(body[key])
                    setattr(bot_params, key, val)
                    changed[key] = val
                except Exception:
                    pass
        try:
            bot_params.save()
        except Exception:
            pass
        return {"ok": True, "changed": changed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/interval_toggle")
async def trading_interval_toggle(body: dict):
    """Toggle trading mode por intervalo (5m/15m/1d). body: {interval, enabled}."""
    try:
        interval = str(body.get("interval", "")).lower()
        enabled  = bool(body.get("enabled", False))
        attr_map = {"5m": "trading_5m_enabled", "15m": "trading_15m_enabled", "1d": "trading_1d_enabled"}
        attr = attr_map.get(interval)
        if not attr:
            return {"ok": False, "error": f"interval inválido: {interval}"}
        setattr(bot_params, attr, enabled)
        bot_params.save()
        return {"ok": True, "interval": interval, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/force-resolve")
async def force_resolve_trading(body: dict = None):
    """
    Fuerza la resolución de TODAS las posiciones OPEN cuyo mercado ya cerró.
    Útil cuando quedaron fantasmas porque el CLOB no devuelve outcome claro.
    """
    try:
        from trading_runner import resolve_stale_positions
        scope = (body or {}).get("scope", "both")
        out = {"phantom": 0, "real": 0}
        if scope in ("both", "phantom"):
            r = await resolve_stale_positions(is_real=False)
            out["phantom"] = len(r)
        if scope in ("both", "real"):
            r = await resolve_stale_positions(is_real=True)
            out["real"] = len(r)
        return {"ok": True, "resolved": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/reset-kill-switch")
async def reset_trading_kill_switch():
    """Desactiva el kill-switch de trading real. Reanuda apertura de posiciones."""
    try:
        bot_params.trading_real_killed = False
        try:
            bot_params.save()
        except Exception:
            pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/trading/logs")
async def get_trading_logs(limit: int = 100):
    """Retorna últimas N entradas de log que contengan [TRADING."""
    try:
        import bot as _bot
        hist = _bot.get_log_history()
        filtered = [e for e in hist if "[TRADING" in str(e.get("msg", ""))]
        return {"ok": True, "count": len(filtered), "logs": filtered[-limit:]}
    except Exception as e:
        return {"ok": False, "error": str(e), "logs": []}


@app.get("/api/trading/diagnostics")
async def get_trading_diagnostics():
    """Por qué el bot no opera. Devuelve flags de runtime."""
    try:
        import bot as _bot
        import trading_positions as tp
        flags = {
            "bot_running":           bool(getattr(_bot.state, "running", False)),
            "trading_mode_enabled":  bool(getattr(bot_params, "trading_mode_enabled", False)),
            "trading_real_enabled":  bool(getattr(bot_params, "trading_real_enabled", False)),
            "updown_enabled":        bool(getattr(bot_params, "updown_enabled", False)),
            "updown_5m_enabled":     bool(getattr(bot_params, "updown_5m_enabled", False)),
            "updown_15m_enabled":    bool(getattr(bot_params, "updown_15m_enabled", False)),
            "trading_real_killed":   bool(getattr(bot_params, "trading_real_killed", False)),
            "clob_client_ready":     getattr(_bot, "_clob_client", None) is not None,
            "phantom_open":          len(tp.all_open_positions(is_real=False)),
            "real_open":             len(tp.all_open_positions(is_real=True)),
            "balance_usdc":          round(float(getattr(_bot.state, "balance_usdc", 0) or 0), 2),
        }
        # Chequeos de problemas comunes
        issues = []
        if not flags["bot_running"]:
            issues.append("Bot detenido — presiona 'Iniciar bot' en el dashboard")
        if not flags["trading_mode_enabled"]:
            issues.append("trading_mode_enabled=False — activa Trading Mode")
        if not flags["updown_enabled"]:
            issues.append("updown_enabled=False — activa UpDown")
        if not flags["updown_5m_enabled"] and not flags["updown_15m_enabled"]:
            issues.append("Ninguna ventana UpDown (5m/15m) habilitada")
        if flags["trading_real_enabled"] and not flags["clob_client_ready"]:
            issues.append("Real activo pero CLOB no inicializado (revisa POLY_PRIVATE_KEY)")
        if flags["trading_real_enabled"] and flags["trading_real_killed"]:
            issues.append("Kill-switch activo — resetea en UI para reanudar real")
        stake = float(getattr(bot_params, "trading_stake_usdc", 5.0))
        if flags["trading_real_enabled"] and flags["balance_usdc"] < stake:
            issues.append(
                f"Balance real ${flags['balance_usdc']:.2f} < stake ${stake:.2f} — real no podrá comprar"
            )
        return {"ok": True, "flags": flags, "issues": issues}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/reset-consec-losses")
async def reset_trading_consec_losses():
    """Resetea contador de pérdidas consecutivas marcando la posición más
    reciente con streak_reset=True. También desactiva kill-switch."""
    try:
        import trading_positions as tp
        n = tp.reset_real_streak()
        bot_params.trading_real_killed = False
        try:
            bot_params.save()
        except Exception:
            pass
        return {"ok": True, "marked": n, "consec_losses_after": tp.real_consecutive_losses()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/reset-phantom")
async def reset_trading_phantom(body: dict = None):
    """Reinicia el balance y posiciones phantom."""
    try:
        import trading_positions as tp
        new_balance = 50.0
        if body and "balance" in body:
            new_balance = float(body["balance"])
        tp.reset_phantom(new_balance=new_balance)
        return {"ok": True, "balance": new_balance}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/set-phantom-balance")
async def set_phantom_balance(body: dict):
    """Punto 6 — modifica el balance virtual phantom manualmente sin borrar posiciones."""
    try:
        import trading_positions as tp
        if "balance" not in body:
            return {"ok": False, "error": "falta 'balance' en body"}
        new_bal = float(body["balance"])
        if new_bal < 0:
            return {"ok": False, "error": "balance debe ser >= 0"}
        result = tp.set_phantom_balance(new_bal)
        return {"ok": True, "balance": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/trading/reset-exposure")
async def reset_trading_exposure():
    """Punto 7 — libera exposure REAL (marca OPEN como RELEASED, no toca on-chain)."""
    try:
        import trading_positions as tp
        out = tp.reset_real_exposure()
        return {"ok": True, **out, "exposure_after": tp.real_exposure_usdc()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)

    try:
        # Estado completo inicial
        await ws.send_json({"type": "status",       "data": _build_status()})
        await ws.send_json({"type": "positions",     "data": bot.state.poly_positions})
        await ws.send_json({"type": "open_orders",   "data": bot.state.open_orders})
        await ws.send_json({"type": "opportunities", "data": bot.state.opportunities})

        while True:
            data = await asyncio.wait_for(ws.receive_text(), timeout=30)
            if data == "ping":
                await ws.send_json({"type": "pong"})
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except Exception:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
