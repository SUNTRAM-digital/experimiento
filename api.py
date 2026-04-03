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


def _build_status() -> dict:
    portfolio_value = round(sum(p["cur_value_usdc"] for p in bot.state.poly_positions), 2)
    portfolio_pnl   = round(sum(p["pnl_usdc"]       for p in bot.state.poly_positions), 2)
    return {
        "running":             bot.state.running,
        "balance_usdc":        round(bot.state.balance_usdc, 2),
        "portfolio_value":     portfolio_value,
        "portfolio_pnl":       portfolio_pnl,
        "total_value":         round(bot.state.balance_usdc + portfolio_value, 2),
        "daily_start_balance": round(bot.state.daily_start_balance, 2),
        "daily_loss_usdc":     round(bot.state.daily_loss_usdc, 2),
        "total_trades":        bot.state.total_trades,
        "total_pnl":           round(bot.state.total_pnl, 2),
        "last_scan":           bot.state.last_scan,
        "error_count":         bot.state.error_count,
        "opportunities_count": len(bot.state.opportunities),
        "btc_price":           bot.state.btc_price,
        "btc_opportunities_count": len(bot.state.btc_opportunities),
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

        # Posiciones y ordenes (cada 15s = cada 3 ticks)
        if tick % 3 == 0 and settings.poly_wallet_address:
            positions = await get_polymarket_positions(settings.poly_wallet_address)
            if positions:
                bot.state.poly_positions = positions
            await _broadcast({"type": "positions", "data": bot.state.poly_positions})

            orders = await asyncio.get_event_loop().run_in_executor(None, bot._fetch_open_orders)
            bot.state.open_orders = orders
            await _broadcast({"type": "open_orders", "data": bot.state.open_orders})

        # Oportunidades (cada 30s = cada 6 ticks)
        if tick % 6 == 0:
            await _broadcast({"type": "opportunities", "data": bot.state.opportunities})


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


@app.get("/api/positions")
async def get_positions():
    return bot.state.active_positions


@app.get("/api/trades")
async def get_trades():
    return bot.state.trade_history


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
        lines += ["", f"=== BITCOIN ===", f"Precio actual: ${s.btc_price:,.2f} USD"]
        if s.btc_opportunities:
            lines += [f"Oportunidades BTC detectadas: {len(s.btc_opportunities)}"]
            for o in s.btc_opportunities[:5]:
                lines.append(
                    f"• [condition_id:{o.get('condition_id','')}] "
                    f"{o.get('side')} {'>' if o.get('direction')=='above' else '<'}"
                    f"${o.get('threshold',0):,.0f} | "
                    f"EV: {o.get('ev_pct')}% | "
                    f"Nuestra P: {o.get('our_prob',0)*100:.1f}% | "
                    f"Cierra en: {o.get('minutes_to_close',0):.0f}m"
                )
    if s.portfolio_analysis:
        lines += ["", "=== ÚLTIMO ANÁLISIS DE PORTAFOLIO (Claude) ===", s.portfolio_analysis[:800]]
    return "\n".join(lines)


CHAT_SYSTEM = """Eres el operador de trading de Weatherbot. Tienes control TOTAL sobre el bot: puedes vender posiciones y comprar oportunidades usando las herramientas disponibles.

IMPORTANTE: Cuando el usuario pida vender, comprar o cualquier operación, DEBES usar la herramienta correspondiente de inmediato. NUNCA respondas diciendo que "no puedes realizar operaciones reales" — las herramientas sell_position y buy_opportunity SÍ ejecutan trades reales en Polymarket. Si ves un token_id en el contexto, puedes vender esa posición ahora mismo.

CONTEXTO ACTUAL DEL BOT:
{context}

INSTRUCCIONES OPERATIVAS:
- Si el usuario pide vender → llama sell_position con el token_id y size exactos del contexto
- Si el usuario pide comprar → llama buy_opportunity con el condition_id del contexto
- Si el usuario pide vender todo → llama sell_position para CADA posición en la lista
- Si el usuario pide vender posiciones en pérdida → identifica las que tienen pnl_usdc negativo y véndalas
- Antes de cada operación di en una línea qué harás y por qué
- Después de la operación confirma el resultado
- Responde en español, conciso y directo
- No inventes datos que no estén en el contexto"""


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
        "description": "Ejecuta la compra de una oportunidad detectada por el bot. Usa el condition_id exacto de la lista de oportunidades.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition_id": {
                    "type": "string",
                    "description": "El condition_id de la oportunidad (aparece como [condition_id:...] en el contexto)"
                },
                "reason": {"type": "string", "description": "Razón de la compra en 1 frase"},
            },
            "required": ["condition_id", "reason"],
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
