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
    # Calcular disponibles distribuyendo el cash entre categorías con espacio libre
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
        # Tipos de trade habilitados
        "weather_enabled":              bot_params.weather_enabled,
        "btc_enabled":                  bot_params.btc_enabled,
        # UpDown — control
        "updown_5m_enabled":            bot_params.updown_5m_enabled,
        "updown_15m_enabled":           bot_params.updown_15m_enabled,
        "updown_5m_stopped":            bot.state.updown_5m_stopped,
        "updown_15m_stopped":           bot.state.updown_15m_stopped,
        "updown_5m_consecutive_losses": bot.state.updown_5m_consecutive_losses,
        "updown_15m_consecutive_losses":bot.state.updown_15m_consecutive_losses,
        "updown_max_usdc":              bot_params.updown_max_usdc,
        "updown_max_consecutive_losses":bot_params.updown_max_consecutive_losses,
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


# ── endpoints REST ─────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


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

    result = await analyze_portfolio(bot.state.poly_positions, bot.state.balance_usdc)
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
    stats = {
        "WEATHER":    {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "total_cost": 0.0},
        "BTC":        {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "total_cost": 0.0},
        "BTC_UPDOWN": {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "total_cost": 0.0},
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
    # Win rates
    for s in stats.values():
        resolved = s["wins"] + s["losses"]
        s["win_rate"] = round(s["wins"] / resolved, 3) if resolved > 0 else None
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

    opp = evaluate_updown_market(
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
            "reason":      "Señal insuficiente" if opp is None else f"Señal {opp['side']} con {opp['confidence']}% de confianza",
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

def _build_chat_context() -> str:
    """Construye el contexto actual del bot para el chat."""
    s = bot.state
    lines = [
        f"Fecha/hora actual: {bot.state.last_scan or 'N/A'} UTC",
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
        f"Capital asignado — Clima: {bp['alloc_weather_pct']*100:.0f}% | BTC: {bp['alloc_btc_pct']*100:.0f}% | UpDown: {bp['alloc_updown_pct']*100:.0f}%",
        f"EV mínimo clima: {bp['min_ev_threshold']*100:.0f}% | Kelly fraction: {bp['kelly_fraction']}",
        f"Posición máx clima: ${bp['max_position_usdc']} | mín: ${bp['min_position_usdc']}",
        f"Liquidez mín: ${bp['min_liquidity_usdc']} | Spread máx: {bp['max_spread_pct']*100:.0f}%",
        f"UpDown habilitado: 5m={'Sí' if bp['updown_5m_enabled'] else 'No'} | 15m={'Sí' if bp['updown_15m_enabled'] else 'No'} | Máx por trade: ${bp['updown_max_usdc']}",
        f"UpDown máx pérdidas consecutivas: {bp['updown_max_consecutive_losses']}",
        f"BTC habilitado: {'Sí' if bp['btc_enabled'] else 'No'} | BTC max posición: ${bp['btc_max_position_usdc']}",
    ]

    # === CAPITAL ===
    lines += [
        "",
        "=== ASIGNACIÓN DE CAPITAL ===",
        f"Clima   — Presupuesto: ${getattr(s,'budget_weather',0):.2f} | Deployed: ${getattr(s,'deployed_weather',0):.2f} | Disponible: ${getattr(s,'available_weather',0):.2f}",
        f"BTC     — Presupuesto: ${getattr(s,'budget_btc',0):.2f} | Deployed: ${getattr(s,'deployed_btc',0):.2f} | Disponible: ${getattr(s,'available_btc',0):.2f}",
        f"UpDown  — Presupuesto: ${getattr(s,'budget_updown',0):.2f} | Deployed: ${getattr(s,'deployed_updown',0):.2f} | Disponible: ${getattr(s,'available_updown',0):.2f}",
    ]

    # === META DE GANANCIA ===
    if bp.get("profit_goal_usdc", 0) > 0:
        from datetime import datetime, timezone as _tz
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
                elapsed_h = (datetime.now(_tz.utc) - start_dt).total_seconds() / 3600
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

    return "\n".join(lines)


CHAT_SYSTEM = """Eres el operador de trading de Weatherbot. Tienes control TOTAL sobre el bot: puedes vender posiciones, comprar oportunidades de clima, comprar oportunidades de Bitcoin, disparar escaneos UpDown y controlar el auto-trading.

IMPORTANTE: NUNCA respondas que "no puedes realizar operaciones" — las herramientas SÍ ejecutan trades reales en Polymarket.

CONTEXTO ACTUAL DEL BOT:
{context}

INSTRUCCIONES OPERATIVAS:

Clima:
- Si el usuario pide vender una posición → llama sell_position con token_id y size exactos
- Si el usuario pide comprar una oportunidad de clima → llama buy_opportunity con el condition_id

Bitcoin (mercados de precio):
- Si el usuario pide comprar una oportunidad de BTC → llama buy_btc_opportunity con el condition_id de la lista BTC
- Si el usuario pide activar auto-trading / cada N minutos → llama set_btc_auto_mode(enabled=true, interval_minutes=N)
- Si el usuario pide detener auto-trading → llama set_btc_auto_mode(enabled=false)
- Para comprar BTC: evalúa el EV, el RSI, la dirección del mercado y el momentum de TradingView antes de decidir

UpDown (5m/15m BTC up-or-down):
- Si el usuario pide un escaneo UpDown o quieres ejecutar un trade UpDown → llama trigger_updown_scan con interval_minutes=5 o 15
- Evalúa si el UpDown está detenido (pérdidas consecutivas) antes de dispararlo
- Para recomendar UpDown: mira la señal TA de BTC (RSI, EMA, recomendación TradingView), momentum y tendencia CMC

MODO AUTÓNOMO (cuando auto_trade_mode = ACTIVO y hay meta de ganancia activa):
- Actúa sin pedir confirmación — ejecuta las operaciones directamente
- Prioriza las acciones que acerquen la cuenta a la meta de ganancia
- Si hay oportunidades de clima con EV > umbral → compra directamente
- Si hay señal BTC clara (RSI, TA, CMC alineados) → compra la oportunidad BTC
- Si UpDown no está detenido y hay señal → dispara escaneo UpDown
- Reporta en 2-3 líneas qué hiciste y por qué; no pidas aprobación
- Si la meta ya se alcanzó → informa al usuario y para de tradear activamente

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
]


async def _execute_chat_tool(name: str, inputs: dict) -> str:
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

    return f"Herramienta desconocida: {name}"


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
    model = _os.getenv("CLAUDE_CHAT_MODEL", _os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6"))
    system = CHAT_SYSTEM.format(context=_build_chat_context())

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
                    result = await _execute_chat_tool(tb.name, tb.input)
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


# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)

    # Estado completo inicial
    await ws.send_json({"type": "status",       "data": _build_status()})
    await ws.send_json({"type": "positions",     "data": bot.state.poly_positions})
    await ws.send_json({"type": "open_orders",   "data": bot.state.open_orders})
    await ws.send_json({"type": "opportunities", "data": bot.state.opportunities})

    try:
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
