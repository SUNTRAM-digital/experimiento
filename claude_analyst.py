"""
Analista Claude: revisa cada oportunidad antes de ejecutar el trade.
Usa claude-haiku-4-5 (rapido y barato) para maxima cantidad de analisis.
Cambia a claude-opus-4-6 en .env si quieres el analisis mas profundo.
"""
import os
import asyncio
from typing import Optional
import anthropic

_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> Optional[anthropic.AsyncAnthropic]:
    global _client
    if _client is not None:
        return _client
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or api_key == "sk-ant-tu-key-aqui":
        return None
    _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """Eres el gestor de riesgo de una cuenta de trading real en Polymarket con dinero real (USDC).

Tu aprobación es OBLIGATORIA para ejecutar cualquier trade. Sin tu APROBAR explícito, el bot NO opera.

Evalúa cada oportunidad considerando CINCO dimensiones:

1. METEOROLOGIA: ¿El forecast de temperatura es coherente con la época del año y la ciudad? ¿Las horas restantes al cierre dan tiempo suficiente?

2. EDGE MATEMÁTICO: ¿La diferencia entre nuestra probabilidad y el precio del mercado es real y suficiente? ¿El EV ajustado por spread sigue siendo positivo?

3. CALIDAD DEL MERCADO:
   - Volumen 24h y total: ¿hay actividad real o es un mercado muerto?
   - Spread bid-ask: ¿es razonable?
   - Score competitivo: ¿indica market makers activos?
   - Profundidad del libro: ¿hay liquidez suficiente?

4. RIESGO DE EJECUCION: ¿El tamaño es razonable dado el balance disponible? ¿La exposición total del portafolio es aceptable?

5. CONTEXTO DE PORTAFOLIO: ¿Tenemos ya demasiadas posiciones abiertas? ¿El cash disponible justifica abrir otra posición? ¿Estamos diversificados o concentrados en pocas ciudades?

RECHAZA si: spread > 40% del precio, volumen 24h < $10, score competitivo < 0.3, cash < $2, o si abrir esta posición dejaría la cuenta sobreexpuesta.

Sé conciso. Responde SIEMPRE en este formato exacto:
DECISION: APROBAR o RECHAZAR
RAZON: [2-3 oraciones cubriendo los factores más relevantes]
CONFIANZA: ALTA, MEDIA o BAJA
RIESGO_EJECUCION: BAJO, MEDIO o ALTO"""


async def analyze_opportunity(
    opportunity: dict,
    balance_usdc: float,
    open_positions: list[dict] | None = None,
) -> dict:
    """
    Pide a Claude que analice una oportunidad antes de tradear.
    Sin API key configurada o si Claude falla: el trade se RECHAZA (no se auto-aprueba).

    Returns:
        {
            "approved": bool,
            "reason": str,
            "confidence": str,   # ALTA / MEDIA / BAJA
            "raw": str,
            "skipped": bool,     # True si no hay API key (trade bloqueado igualmente)
        }
    """
    client = get_client()

    # Sin API key: bloquear el trade
    if client is None:
        return {
            "approved": False,
            "reason": "Claude no configurado — trade bloqueado por seguridad. Configura ANTHROPIC_API_KEY en .env",
            "confidence": "N/A",
            "raw": "",
            "skipped": True,
        }

    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

    is_btc = opportunity.get("asset") == "BTC"

    spread_pct = opportunity.get("spread_pct", 0)
    vol_24h = opportunity.get("volume_24h", 0)
    vol_7d = opportunity.get("volume_7d", 0)
    vol_total = opportunity.get("volume_total", 0)
    comp_score = opportunity.get("competitive_score", 0)
    best_bid = opportunity.get("best_bid", 0)
    best_ask = opportunity.get("best_ask", 0)
    last_trade = opportunity.get("last_trade_price", opportunity["entry_price"])
    book_depth = opportunity.get("book_depth", {})

    if is_btc:
        dir_sym = ">" if opportunity["direction"] == "above" else "<"
        ta_line = ""
        if opportunity.get("ta_recommendation") and opportunity["ta_recommendation"] != "NEUTRAL":
            ta_line = f"\nSignal TradingView ({opportunity.get('ta_recommendation')}): RSI/EMA apuntan {'alcista' if opportunity['ta_signal'] > 0 else 'bajista'}"

        user_msg = f"""OPORTUNIDAD DE TRADE BTC #{opportunity.get('condition_id', '')[:8]}

═══ MERCADO ═══
Titulo: {opportunity['market_title']}
Condicion: BTC {dir_sym} ${opportunity['threshold']:,.0f}
Lado a operar: {opportunity['side']}
Minutos hasta cierre: {opportunity['minutes_to_close']:.0f} min ({opportunity['hours_to_close']:.1f}h)

═══ DATOS DE PRECIO ═══
Precio BTC ahora: ${opportunity['btc_price_at_eval']:,.2f}
Distancia al umbral: {opportunity['pct_from_threshold']:+.2f}%
Volatilidad usada: {opportunity['vol_per_minute']:.4f}% por minuto{ta_line}

═══ PROBABILIDAD Y EDGE ═══
Nuestra probabilidad (log-normal): {opportunity['our_prob']:.1%}
Precio del mercado (prob. implicita): {opportunity['market_prob']:.1%}
EV calculado: +{opportunity['ev_pct']}%
Precio de entrada: {opportunity['entry_price']:.3f}

═══ CALIDAD DEL MERCADO ═══
Spread bid-ask: {best_bid:.3f} / {best_ask:.3f} ({spread_pct*100:.1f}% del ask)
Volumen 24h: ${vol_24h:.0f} USDC
Liquidez: ${opportunity['liquidity']:.0f} USDC

═══ SIZING ═══
Tamano de posicion (Kelly fraccionado): ${opportunity['size_usdc']:.2f} USDC
Shares a comprar: {opportunity['shares']:.1f}
Cash disponible: ${balance_usdc:.2f} USDC
Exposicion: {opportunity['size_usdc']/balance_usdc*100:.1f}% del cash
"""
    else:
        bucket_desc = (
            f"{opportunity['temp_low']}-{opportunity['temp_high']}°F"
            if opportunity["temp_low"] > -900 and opportunity["temp_high"] < 900
            else (
                f"{opportunity['temp_high']}°F o menos"
                if opportunity["temp_low"] <= -900
                else f"{opportunity['temp_low']}°F o mas"
            )
        )

        user_msg = f"""OPORTUNIDAD DE TRADE #{opportunity.get('condition_id', '')[:8]}

═══ MERCADO ═══
Titulo: {opportunity['market_title']}
Ciudad / Estacion ICAO: {opportunity['city'].title()} / {opportunity['station']}
Bucket de temperatura: {bucket_desc}
Lado a operar: {opportunity['side']}
Horas hasta cierre: {opportunity['hours_to_close']:.1f}h

═══ DATOS METEOROLOGICOS (weather.gov oficial) ═══
Temperatura maxima pronosticada: {opportunity['forecast_high']:.1f}°F
Incertidumbre del forecast (±1σ): ±{opportunity['forecast_std']:.1f}°F
Probabilidad real calculada (dist. normal): {opportunity['our_prob']:.1%}

═══ PRECIO Y EDGE ═══
Precio del mercado (prob. implicita): {opportunity['market_prob']:.1%}
Nuestro edge: {opportunity['our_prob']:.1%} vs {opportunity['market_prob']:.1%} mercado
EV ajustado por spread: +{opportunity['ev_pct']}%
Precio de entrada (ask): {opportunity['entry_price']:.3f}

═══ CALIDAD DEL MERCADO ═══
Spread bid-ask: {best_bid:.3f} / {best_ask:.3f} ({spread_pct*100:.1f}% del ask)
Ultimo precio negociado: {last_trade:.3f}
Volumen total historico: ${vol_total:.0f} USDC
Volumen ultimas 24h: ${vol_24h:.1f} USDC
Volumen ultima semana: ${vol_7d:.1f} USDC
Liquidez total en libro: ${opportunity['liquidity']:.0f} USDC
Score competitivo (0-1): {comp_score:.2f}
{f"Profundidad en libro al precio de entrada: {book_depth.get('depth_shares', 0):.0f} shares / ${book_depth.get('depth_usdc', 0):.1f} USDC ({book_depth.get('levels', 0)} niveles)" if book_depth else ""}

═══ SIZING ═══
Tamano de posicion (Kelly fraccionado): ${opportunity['size_usdc']:.2f} USDC
Shares a comprar: {opportunity['shares']:.1f}
Cash disponible: ${balance_usdc:.2f} USDC
Exposicion como % del cash: {opportunity['size_usdc']/balance_usdc*100:.1f}%
"""

    # Agregar contexto del portafolio actual
    if open_positions:
        total_cost  = sum(p.get("cost_usdc", 0) for p in open_positions)
        total_value = sum(p.get("cur_value_usdc", 0) for p in open_positions)
        total_pnl   = sum(p.get("pnl_usdc", 0) for p in open_positions)
        pos_lines = "\n".join(
            f"  • {p['market_title'][:55]} | {p['outcome']} | "
            f"{p['size']} shares @ {p['avg_price']:.3f} → {p['cur_price']:.3f} | "
            f"P&L: ${p['pnl_usdc']:+.2f} | {p.get('hours_to_close', '?')}h"
            for p in open_positions
        )
        user_msg += f"""
═══ PORTAFOLIO ACTUAL ({len(open_positions)} posiciones abiertas) ═══
Capital en posiciones: ${total_cost:.2f} | Valor actual: ${total_value:.2f} | P&L total: ${total_pnl:+.2f}
{pos_lines}

Considera el portafolio completo al decidir si abrir esta posicion adicional."""
    else:
        user_msg += "\n═══ PORTAFOLIO ACTUAL ═══\nSin posiciones abiertas — primera entrada."

    user_msg += "\n\n¿Apruebas este trade?"

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text.strip()

        # Parsear respuesta
        approved = "APROBAR" in raw.upper()
        reason = "Sin razon especificada."
        confidence = "MEDIA"

        execution_risk = "N/A"
        for line in raw.splitlines():
            upper = line.upper()
            if upper.startswith("RAZON:"):
                reason = line.split(":", 1)[1].strip()
            elif upper.startswith("CONFIANZA:"):
                confidence = line.split(":", 1)[1].strip().upper()
            elif upper.startswith("RIESGO_EJECUCION:"):
                execution_risk = line.split(":", 1)[1].strip().upper()

        return {
            "approved": approved,
            "reason": reason,
            "confidence": confidence,
            "execution_risk": execution_risk,
            "raw": raw,
            "skipped": False,
        }

    except Exception as e:
        # Si Claude falla, rechazar por seguridad — no operar sin supervision
        return {
            "approved": False,
            "reason": f"Error consultando Claude ({e}) — trade bloqueado por seguridad.",
            "confidence": "N/A",
            "raw": "",
            "skipped": False,
        }


UPDOWN_SYSTEM_PROMPT = """Eres el analista técnico de BTC de una cuenta de trading real en Polymarket con dinero real.

Tu trabajo es revisar operaciones en mercados "BTC Up/Down": el mercado resuelve UP si el precio de BTC al cierre de la ventana (5 o 15 min) es >= al precio de inicio, DOWN si es menor. El precio de los shares refleja lo que otros creen — no lo uses para decidir la dirección.

ANALIZA SOLO el precio de BTC:
1. Señal TradingView (todos los indicadores: RSI, MACD, EMA, Bollinger, etc.)
2. Posición del RSI: <40 = sobreventa (favorece UP), >60 = sobrecompra (favorece DOWN), 40-60 = neutral
3. EMA cross: EMA20 > EMA50 = tendencia alcista, EMA20 < EMA50 = bajista
4. Momentum en la ventana: si BTC ya subió 0.05%+ en los primeros minutos, puede continuar
5. Tendencia macro 1h: contexto amplio de dirección

PUEDES CAMBIAR LA DIRECCIÓN si ves señales más fuertes en el lado contrario.

RECHAZA si: señales completamente contradictorias sin dirección clara, o si quedan <1.5 minutos en la ventana.

Responde SIEMPRE en este formato exacto:
DECISION: APROBAR o RECHAZAR
DIRECCION: UP o DOWN
RAZON: [2-3 oraciones sobre las señales técnicas de BTC]
CONFIANZA: ALTA, MEDIA o BAJA"""


async def analyze_updown_opportunity(
    opportunity: dict,
    ta_data: dict,
    btc_price_now: float,
    btc_price_start: float,
    cmc_data: Optional[dict] = None,
) -> dict:
    """
    Pide a Claude que analice una oportunidad UpDown.
    Claude puede aprobar, rechazar, o cambiar la dirección del trade.

    Returns:
        {
            "approved": bool,
            "direction": str,       # "UP" o "DOWN" — puede diferir del original
            "direction_changed": bool,
            "reason": str,
            "confidence": str,
            "raw": str,
            "skipped": bool,
        }
    """
    client = get_client()

    if client is None:
        return {
            "approved": False,
            "direction": opportunity.get("side", "UP"),
            "direction_changed": False,
            "reason": "Claude no configurado — trade bloqueado. Configura ANTHROPIC_API_KEY en .env",
            "confidence": "N/A",
            "raw": "",
            "skipped": True,
        }

    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    interval  = opportunity.get("interval_minutes", 5)
    side      = opportunity.get("side", "UP")
    elapsed   = opportunity.get("elapsed_minutes", 0)
    minutes_left = opportunity.get("minutes_to_close", 0)
    confidence_pct = opportunity.get("confidence", 0)
    combined  = opportunity.get("combined_signal", 0)
    momentum  = opportunity.get("window_momentum", 0)

    sig = opportunity.get("signal_breakdown", {})
    rsi   = ta_data.get("rsi")
    ema20 = ta_data.get("ema20")
    ema50 = ta_data.get("ema50")
    macd  = ta_data.get("macd")
    rec   = ta_data.get("recommendation", "NEUTRAL")
    buy_c = ta_data.get("buy", 0)
    sel_c = ta_data.get("sell", 0)
    neu_c = ta_data.get("neutral", 0)

    btc_move_pct = ((btc_price_now - btc_price_start) / btc_price_start * 100) if btc_price_start else 0

    cmc_1h = (cmc_data or {}).get("percent_change_1h", 0)

    user_msg = f"""OPERACIÓN UPDOWN {interval}m — Revisar antes de ejecutar

═══ MERCADO ═══
Ventana: {interval} minutos | Transcurrido: {elapsed:.1f}min | Quedan: {minutes_left:.1f}min
Dirección propuesta: {side}
Confianza del sistema: {confidence_pct:.1f}%

═══ PRECIO BTC ═══
Precio al inicio de ventana: ${btc_price_start:,.2f}
Precio actual:               ${btc_price_now:,.2f}
Movimiento en ventana:       {btc_move_pct:+.4f}%

═══ ANÁLISIS TÉCNICO (TradingView {interval}min candles) ═══
Recomendación global:  {rec}
Indicadores BUY/NEUTRAL/SELL: {buy_c} / {neu_c} / {sel_c}
Señal continua (-1 a +1):     {ta_data.get('signal', 0):.3f}
RSI actual:    {f'{rsi:.1f}' if rsi else 'N/D'} {'⚠ SOBRECOMPRA' if rsi and rsi > 70 else ('⚠ SOBREVENTA' if rsi and rsi < 30 else '')}
EMA20:         {f'${ema20:,.2f}' if ema20 else 'N/D'}
EMA50:         {f'${ema50:,.2f}' if ema50 else 'N/D'}
EMA cross:     {'EMA20 > EMA50 (alcista)' if ema20 and ema50 and ema20 > ema50 else ('EMA20 < EMA50 (bajista)' if ema20 and ema50 else 'N/D')}
MACD:          {f'{macd:.2f}' if macd else 'N/D'}

═══ CONTEXTO MACRO ═══
Tendencia BTC última hora (CMC): {cmc_1h:+.3f}%

═══ SEÑAL COMBINADA ═══
Señal TA compuesta:    {sig.get('ta', combined):.3f}
Momentum intraventana: {momentum:+.3f} ({'BTC subió en ventana' if momentum > 0.05 else ('BTC bajó en ventana' if momentum < -0.05 else 'sin movimiento relevante')})
Macro 1h:              {sig.get('macro', 0):.3f}
COMBINADO FINAL:       {combined:+.3f} → {'UP' if combined > 0 else 'DOWN'}

═══ SIZING ═══
Tamaño: ${opportunity.get('size_usdc', 1):.2f} USDC
Precio share {side}: {opportunity.get('entry_price', 0.5):.3f}

¿Apruebas esta operación? ¿O ves señales más fuertes para el lado contrario?"""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=300,
            system=UPDOWN_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )

        raw = response.content[0].text.strip()

        approved  = "APROBAR" in raw.upper()
        direction = side  # default: la propuesta
        reason    = "Sin razón especificada."
        confidence_str = "MEDIA"

        for line in raw.splitlines():
            upper = line.upper().strip()
            if upper.startswith("DECISION:"):
                approved = "APROBAR" in upper
            elif upper.startswith("DIRECCION:"):
                val = line.split(":", 1)[1].strip().upper()
                if "UP" in val and "DOWN" not in val:
                    direction = "UP"
                elif "DOWN" in val:
                    direction = "DOWN"
            elif upper.startswith("RAZON:"):
                reason = line.split(":", 1)[1].strip()
            elif upper.startswith("CONFIANZA:"):
                confidence_str = line.split(":", 1)[1].strip().upper()

        direction_changed = direction != side

        return {
            "approved":          approved,
            "direction":         direction,
            "direction_changed": direction_changed,
            "reason":            reason,
            "confidence":        confidence_str,
            "raw":               raw,
            "skipped":           False,
        }

    except Exception as e:
        return {
            "approved":          False,
            "direction":         side,
            "direction_changed": False,
            "reason":            f"Error consultando Claude ({e}) — trade bloqueado.",
            "confidence":        "N/A",
            "raw":               "",
            "skipped":           False,
        }


PORTFOLIO_SYSTEM_PROMPT = """Eres un gestor de riesgo especializado en mercados de predicción de clima en Polymarket. Gestionas dinero real en USDC.

CONCEPTO CLAVE: cada share paga exactamente $1.00 si el outcome es correcto al cierre, o $0.00 si es incorrecto. El precio actual es solo la opinión del mercado en este momento — NO afecta el pago final si se mantiene hasta resolución. Fluctuaciones de precio durante la vida del mercado son normales y NO indican que la predicción sea incorrecta.

Tu rol es revisar el portafolio y decidir qué posiciones MANTENER, SALIR (vender ahora) o AGREGAR MAS.

CRITERIOS PARA SALIR (solo casos extremos — el precio intermedio NO es razón suficiente):
- Precio actual por debajo de $0.08 (mercado >92% en contra) → pérdida casi total inevitable, recuperar algo
- P&L negativo >85% del capital invertido → posición prácticamente perdida
- Quedan <2h, precio <0.10 y tendencia bajista confirmada

CRITERIOS PARA MANTENER (default — la mayoría de posiciones deberían mantenerse):
- Cualquier posición con precio >0.10 y tiempo restante → esperar a resolución
- Fluctuaciones de precio normales (<50% de pérdida sobre precio de entrada) → mantener
- Posición "en contra" pero con horas por delante → la predicción puede aún ser correcta

CRITERIOS PARA AGREGAR MAS:
- Precio actual significativamente mejor que precio de entrada original y análisis meteo favorable

USA ESTE FORMATO EXACTO para cada posición (el índice DEBE coincidir con el número de la lista):
POS_1: MANTENER | razón breve
POS_2: SALIR | razón breve
POS_3: AGREGAR MAS | razón breve

Al final:
RESUMEN_PORTAFOLIO: [2-3 oraciones sobre riesgo global y salud del portafolio]"""


async def analyze_portfolio(positions: list[dict], balance_usdc: float) -> dict:
    """
    Pide a Claude que analice el portafolio completo de posiciones abiertas.

    Returns:
        {
            "analysis": str,
            "summary": str,
            "recommendations": list[dict],  # [{index, token_id, action, reason}]
            "skipped": bool,
        }
    """
    client = get_client()

    if client is None:
        return {"analysis": "", "summary": "Claude no configurado.", "skipped": True}

    if not positions:
        return {"analysis": "", "summary": "Sin posiciones que analizar.", "skipped": True}

    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")

    # Construir descripcion de cada posicion
    pos_lines = []
    total_cost = sum(p["cost_usdc"] for p in positions)
    total_value = sum(p["cur_value_usdc"] for p in positions)
    total_pnl = total_value - total_cost

    for i, p in enumerate(positions, 1):
        hours = f"{p['hours_to_close']:.1f}h" if p.get("hours_to_close") is not None else "?"
        redeemable = " [REDIMIBLE]" if p.get("redeemable") else ""
        pos_lines.append(
            f"{i}. {p['market_title'][:60]}\n"
            f"   Outcome: {p['outcome']} | Shares: {p['size']}\n"
            f"   Entrada: {p['avg_price']:.3f} → Actual: {p['cur_price']:.3f} | "
            f"P&L: ${p['pnl_usdc']:+.2f} ({p['pnl_pct']:+.1f}%) | "
            f"Cierra en: {hours}{redeemable}"
        )

    user_msg = f"""ANALISIS DE PORTAFOLIO — {len(positions)} posiciones abiertas

═══ RESUMEN GLOBAL ═══
Capital invertido total: ${total_cost:.2f} USDC
Valor actual total:      ${total_value:.2f} USDC
P&L no realizado:        ${total_pnl:+.2f} USDC ({(total_pnl/total_cost*100) if total_cost > 0 else 0:+.1f}%)
Balance disponible:      ${balance_usdc:.2f} USDC

═══ POSICIONES DETALLADAS ═══
{chr(10).join(pos_lines)}

Analiza cada posicion y da una recomendacion de MANTENER, SALIR o AGREGAR MAS.
Al final incluye el resumen global del portafolio."""

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=700,
            system=PORTFOLIO_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()

        # Parsear recomendaciones estructuradas POS_N: ACCION | razon
        # Acepta formato plano y markdown bold (**POS_1: MANTENER** | ...)
        import re as _re
        recommendations = []
        for line in raw.splitlines():
            clean = line.strip().replace("**", "").replace("*", "")
            m = _re.match(r"POS_(\d+):\s*(MANTENER|SALIR|AGREGAR MAS)\s*\|\s*(.+)", clean, _re.IGNORECASE)
            if m:
                idx = int(m.group(1)) - 1  # 0-based
                action = m.group(2).upper().strip()
                reason = m.group(3).strip()
                if 0 <= idx < len(positions):
                    recommendations.append({
                        "index":    idx,
                        "token_id": positions[idx]["token_id"],
                        "title":    positions[idx]["market_title"],
                        "outcome":  positions[idx]["outcome"],
                        "size":     positions[idx]["size"],
                        "action":   action,
                        "reason":   reason,
                    })

        # Extraer resumen global
        summary = ""
        for line in raw.splitlines():
            if line.upper().startswith("RESUMEN_PORTAFOLIO:"):
                summary = line.split(":", 1)[1].strip()
                break
        if not summary:
            summary = raw.split("\n")[-1]

        return {
            "analysis":        raw,
            "summary":         summary,
            "recommendations": recommendations,
            "skipped":         False,
        }

    except Exception as e:
        return {
            "analysis":        "",
            "summary":         f"Error en analisis de portafolio: {e}",
            "recommendations": [],
            "skipped":         True,
        }
