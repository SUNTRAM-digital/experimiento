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

    bucket_desc = (
        f"{opportunity['temp_low']}-{opportunity['temp_high']}°F"
        if opportunity["temp_low"] > -900 and opportunity["temp_high"] < 900
        else (
            f"{opportunity['temp_high']}°F o menos"
            if opportunity["temp_low"] <= -900
            else f"{opportunity['temp_low']}°F o mas"
        )
    )

    spread_pct = opportunity.get("spread_pct", 0)
    vol_24h = opportunity.get("volume_24h", 0)
    vol_7d = opportunity.get("volume_7d", 0)
    vol_total = opportunity.get("volume_total", 0)
    comp_score = opportunity.get("competitive_score", 0)
    best_bid = opportunity.get("best_bid", 0)
    best_ask = opportunity.get("best_ask", 0)
    last_trade = opportunity.get("last_trade_price", opportunity["entry_price"])
    book_depth = opportunity.get("book_depth", {})

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


PORTFOLIO_SYSTEM_PROMPT = """Eres un gestor de riesgo especializado en mercados de predicción de clima en Polymarket. Gestionas dinero real en USDC.

Tu rol es revisar el portafolio y decidir qué posiciones MANTENER, SALIR (vender ahora para recuperar lo que quede), o AGREGAR MAS.

CRITERIOS PARA SALIR (venta de mercado):
- El precio cayó >50% desde la entrada y quedan >12h → el mercado se movió en contra, salir limita pérdidas
- Quedan <3h y el precio está por debajo del 30% de la entrada → improbable recuperación
- P&L negativo >60% del capital invertido en esa posición

CRITERIOS PARA MANTENER:
- P&L positivo o negativo pequeño (<30%)
- El precio se está recuperando
- Quedan pocas horas y la posición está ganando

CRITERIOS PARA AGREGAR MAS:
- P&L positivo y tendencia favorable
- El mercado ofrece mejor precio que la entrada original

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
        import re as _re
        recommendations = []
        for line in raw.splitlines():
            m = _re.match(r"POS_(\d+):\s*(MANTENER|SALIR|AGREGAR MAS)\s*\|\s*(.+)", line.strip(), _re.IGNORECASE)
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
