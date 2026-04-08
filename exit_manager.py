"""
Exit Manager — Patron 3: Disposition Coefficient + Patron 5: Swing Trading

Monitorea posiciones abiertas y decide cuando salir SIN esperar al settlement.

Reglas basadas en el analisis de los top 30 wallets (112,000 wallets estudiadas):
  - Top wallets salen de ganadores al 91% del valor maximo posible
  - Top wallets cortan perdedores en -12% promedio de drawdown
  - Algunos de los mejores wallets tienen CERO posiciones resueltas al settlement

Formulas:
  EXIT (ganancia):    (estimated_prob - current_price) < 0.05   → edge agotado
  STOP (perdida):     current_prob < entry_price - 0.12         → tesis rota
  SWING:              unrealized_pnl_pct > 0 AND remaining_edge < 0.05 → tomar ganancia
"""
from datetime import datetime, timezone
from typing import Optional


def evaluate_exit(position: dict, current_price: float, estimated_prob: float) -> dict:
    """
    Evalua si una posicion abierta debe cerrarse ahora.

    Args:
        position:       dict de la posicion abierta (debe tener entry_price, size, side)
        current_price:  precio actual del token en el mercado (0.0 - 1.0)
        estimated_prob: nuestra estimacion actual de la probabilidad real (0.0 - 1.0)

    Returns:
        {
            "should_exit": bool,
            "reason": str,           # "edge_exhausted" | "stop_loss" | "swing_capture" | "hold"
            "urgency": str,          # "high" | "medium" | "low"
            "details": str,          # descripcion para el log
            "unrealized_pnl_pct": float,
            "remaining_edge": float,
        }
    """
    entry_price  = position.get("entry_price", current_price)
    side         = position.get("side", "YES")

    # Si es posicion NO, invertir la logica de precios
    if side == "NO":
        # Para NO: entry_price es el precio del NO token (= 1 - yes_price_al_entrar)
        # current_price es el precio actual del NO token
        # estimated_prob es nuestra prob de que el outcome sea NO
        pass  # La logica ya viene en terminos del token comprado

    # ── P&L no realizado ──────────────────────────────────────────────────────
    if entry_price > 0:
        unrealized_pnl_pct = (current_price - entry_price) / entry_price
    else:
        unrealized_pnl_pct = 0.0

    # ── Edge restante ─────────────────────────────────────────────────────────
    # Cuanto puede subir mas el precio hasta llegar a nuestra prob estimada
    remaining_edge = estimated_prob - current_price

    # ── Regla 1: STOP LOSS (Disposition Coefficient) ─────────────────────────
    # Si la prob estimada cayo por debajo del precio de entrada - 12%,
    # la tesis original ya no vale. Cortar la perdida.
    stop_threshold = entry_price - 0.12
    if estimated_prob < stop_threshold and unrealized_pnl_pct < -0.05:
        return {
            "should_exit": True,
            "reason": "stop_loss",
            "urgency": "high",
            "details": (
                f"Tesis rota: prob estimada {estimated_prob:.2%} < "
                f"umbral de stop {stop_threshold:.2%} "
                f"(entrada {entry_price:.2%}) | P&L: {unrealized_pnl_pct:+.1%}"
            ),
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "remaining_edge": remaining_edge,
        }

    # ── Regla 2: EDGE AGOTADO (Swing Trading) ────────────────────────────────
    # Si el precio ya refleja casi toda nuestra estimacion (edge < 5%),
    # no hay razon para seguir esperando al settlement.
    if remaining_edge < 0.05 and unrealized_pnl_pct > 0.05:
        return {
            "should_exit": True,
            "reason": "edge_exhausted",
            "urgency": "medium",
            "details": (
                f"Edge agotado: precio {current_price:.2%} ≈ prob estimada {estimated_prob:.2%} "
                f"(edge restante: {remaining_edge:+.2%}) | "
                f"Ganancia capturada: {unrealized_pnl_pct:+.1%}"
            ),
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "remaining_edge": remaining_edge,
        }

    # ── Regla 3: CAPTURA DE GANANCIA ALTA (91% del maximo) ───────────────────
    # Si ya capturamos >85% del movimiento posible desde entrada hasta prob estimada,
    # salir para no arriesgar la ganancia.
    max_possible_gain = estimated_prob - entry_price
    if max_possible_gain > 0.05:  # Solo aplica si habia ganancia potencial real
        captured_pct = (current_price - entry_price) / max_possible_gain
        if captured_pct > 0.85:
            return {
                "should_exit": True,
                "reason": "swing_capture",
                "urgency": "low",
                "details": (
                    f"Capturado {captured_pct:.0%} del movimiento maximo posible "
                    f"({entry_price:.2%} → {current_price:.2%} / objetivo {estimated_prob:.2%}) | "
                    f"P&L: {unrealized_pnl_pct:+.1%}"
                ),
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "remaining_edge": remaining_edge,
            }

    # ── MANTENER ──────────────────────────────────────────────────────────────
    return {
        "should_exit": False,
        "reason": "hold",
        "urgency": "low",
        "details": (
            f"Edge activo: {remaining_edge:+.2%} restante | "
            f"P&L: {unrealized_pnl_pct:+.1%} | "
            f"Precio: {current_price:.2%} vs prob: {estimated_prob:.2%}"
        ),
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "remaining_edge": remaining_edge,
    }


def evaluate_exit_batch(
    positions: list[dict],
    current_prices: dict[str, float],
    estimated_probs: dict[str, float],
) -> list[dict]:
    """
    Evalua todas las posiciones abiertas en batch.

    Args:
        positions:       lista de posiciones activas del estado del bot
        current_prices:  {token_id: precio_actual}
        estimated_probs: {condition_id: prob_estimada_actual}

    Returns:
        lista de dicts con las posiciones que deben cerrarse
    """
    exits = []
    for pos in positions:
        token_id     = pos.get("token_id") or pos.get("yes_token_id")
        condition_id = pos.get("condition_id", "")

        current_price  = current_prices.get(token_id)
        estimated_prob = estimated_probs.get(condition_id)

        if current_price is None or estimated_prob is None:
            continue

        result = evaluate_exit(pos, current_price, estimated_prob)
        if result["should_exit"]:
            exits.append({
                **pos,
                "exit_reason":   result["reason"],
                "exit_urgency":  result["urgency"],
                "exit_details":  result["details"],
                "exit_pnl_pct":  result["unrealized_pnl_pct"],
                "remaining_edge": result["remaining_edge"],
            })

    # Ordenar: primero los urgentes (stop_loss), luego edge_exhausted, luego swing
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    exits.sort(key=lambda x: urgency_order.get(x["exit_urgency"], 3))
    return exits
