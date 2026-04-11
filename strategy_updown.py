"""
Estrategia para mercados BTC Up/Down (5m y 15m).

FLUJO DE DECISIÓN:
  1. FILTROS DE MERCADO (condiciones mínimas para operar)
     - Precio de compra < 0.90  (si cuesta ≥ $0.90, el riesgo/recompensa es pésimo)
     - Ambos lados tienen precio entre 0.05 y 0.95 (mercado no resuelto)

  2. CONSTRUCCIÓN DE SEÑAL DIRECCIONAL
     Combina TODAS las fuentes disponibles, ponderadas por relevancia para ventanas cortas:

     a) TradingView (intervalo 1m para 5m / 5m para 15m)
        - Señal continua: (indicadores_alcistas - bajistas) / total → [-1, +1]
        - RSI: <30 sobreventa→UP, >70 sobrecompra→DOWN
        - MACD: positivo→UP, negativo→DOWN
        - EMA20 vs EMA50: cruce dorado→UP, cruce de muerte→DOWN

     b) Momentum de ventana (BTC ahora vs precio al inicio de la ventana)
        - El movimiento YA ocurrido en esta ventana es la señal más directa
        - 0.1% de movimiento = señal 0.5

     c) Consenso de Polymarket (precio UP vs 0.50)
        - Si mercado ya asignó 70% prob a UP, eso es información
        - Peso bajo: confirma pero no decide solo

     d) Tendencia macro CMC 1h
        - Contexto amplio: si BTC lleva 1h subiendo, la inercia importa

  3. DECISIÓN FINAL
     - Si confianza < umbral → no operar
     - Comprar la dirección indicada por la señal combinada
"""
from typing import Optional
from config import bot_params

# ── Umbrales ──────────────────────────────────────────────────────────────────
# Confianza mínima: subido 0.10→0.20 para evitar trades con señal débil.
# 0.20 ≈ 60% de indicadores alineados (más conservador que el 55% previo).
_MIN_CONFIDENCE  = 0.20

# No entrar si el share de la dirección elegida cuesta >= este valor
# $0.90 → ganas $0.10 arriesgando $0.90 → ratio 1:9 → inaceptable
_MAX_ENTRY_PRICE = 0.89

# ── Tiempo mínimo de entrada (floor fijo, independiente del learner) ──────────
# Para que el precio BTC muestre dirección antes de apostar.
# 5m: esperar 1.5min (30% de la ventana)
# 15m: esperar 3.5min (23% de la ventana)
# El learner puede subir este umbral pero nunca lo reduce debajo del floor.
_MIN_ELAPSED_5M  = 1.5   # minutos
_MIN_ELAPSED_15M = 3.5   # minutos

# ── Pesos de cada componente ──────────────────────────────────────────────────
# Basado en knowledge base (v83 bot externo + IR = IC × √N):
#   TA + RSI/EMA/MACD = señal técnica compuesta (más informativa)
#   Momentum intra-ventana = señal más directa para 5m/15m
#   OFI real (Telonex on-chain): buy/sell pressure desde fills reales → reemplaza proxy
#   OFI proxy (up_price - 0.5): fallback cuando Telonex no disponible
#   Smart wallet bias (Telonex): dirección de wallets históricamente rentables
#   Macro (CMC 1h) = contexto amplio
#   Consenso de mercado = confirmación secundaria
#
# Con Telonex disponible: TA=0.43 + MOMENTUM=0.22 + OFI_REAL=0.12 + SMART=0.10 + MACRO=0.08 + MKT=0.05
# Sin Telonex (proxy):    TA=0.50 + MOMENTUM=0.25 + OFI_PROXY=0.10 + MACRO=0.10 + MKT=0.05
_W_TA           = 0.50   # sin Telonex
_W_MOMENTUM     = 0.25
_W_OFI          = 0.10   # proxy sin Telonex
_W_MACRO        = 0.10
_W_MARKET       = 0.05

# Con Telonex: pesos ajustados (suman 1.0)
_W_TA_TX        = 0.43   # TA con Telonex
_W_MOMENTUM_TX  = 0.22
_W_OFI_REAL_TX  = 0.12   # OFI real on-chain
_W_SMART_TX     = 0.10   # Smart wallet bias
_W_MACRO_TX     = 0.08
_W_MARKET_TX    = 0.05


# ── Señales individuales ──────────────────────────────────────────────────────

def _ema_signal(ema20: Optional[float], ema50: Optional[float]) -> float:
    """EMA20 > EMA50 = uptrend (+1); EMA20 < EMA50 = downtrend (-1)."""
    if not ema20 or not ema50:
        return 0.0
    diff_pct = (ema20 - ema50) / ema50
    # 0.3% de diferencia = señal máxima para timeframes cortos
    return max(-1.0, min(1.0, diff_pct / 0.003))


def _rsi_signal(rsi: Optional[float]) -> float:
    """
    RSI < 30 → sobreventa → señal UP fuerte
    RSI > 70 → sobrecompra → señal DOWN fuerte
    RSI 45-55 → zona neutral (señal ≈ 0)
    """
    if rsi is None:
        return 0.0
    if rsi <= 30:
        return (30 - rsi) / 30          # +1.0 en RSI=0, +0 en RSI=30
    if rsi >= 70:
        return (70 - rsi) / 30          # -1.0 en RSI=100, -0 en RSI=70
    # Zona media: señal suave proporcional a distancia del 50
    return (rsi - 50) / 50 * 0.25


def _macd_signal(macd: Optional[float]) -> float:
    """
    MACD positivo → momentum alcista → UP
    MACD negativo → momentum bajista → DOWN
    Normalizado: ±50 USDC de MACD = señal máxima en BTC.
    """
    if macd is None:
        return 0.0
    return max(-1.0, min(1.0, macd / 50.0))


def _window_momentum(btc_now: float, btc_start: Optional[float]) -> float:
    """
    Momentum intra-ventana: cuánto se movió BTC desde el inicio de esta ventana.
    +0.1% → señal UP de +0.5  |  -0.1% → señal DOWN de -0.5
    Es la señal más directa para predecir si el cierre supera el inicio.
    """
    if not btc_start or btc_start <= 0 or not btc_now:
        return 0.0
    pct = (btc_now - btc_start) / btc_start
    return max(-1.0, min(1.0, pct * 500))


def _market_consensus_signal(up_price: float, down_price: float) -> float:
    """
    El precio de UP/DOWN en Polymarket refleja la probabilidad implícita del mercado.
    up_price = 0.70 → mercado cree 70% que BTC sube → señal UP moderada.
    up_price = 0.50 → mercado neutral → señal 0.

    Peso bajo: no queremos seguir al mercado ciegamente, pero sí considerarlo.
    """
    if up_price <= 0 or down_price <= 0:
        return 0.0
    # Lean del mercado: [−0.50, +0.50] → normalizar a [−1, +1]
    lean = up_price - 0.50
    return max(-1.0, min(1.0, lean / 0.35))


def _macro_signal(cmc_data: Optional[dict]) -> float:
    """Tendencia BTC en la última 1h desde CoinMarketCap."""
    if not cmc_data:
        return 0.0
    change_1h = cmc_data.get("percent_change_1h", 0.0) or 0.0
    # ±2% en 1h = señal máxima
    return max(-1.0, min(1.0, change_1h / 2.0))


def _order_book_imbalance(market: Optional[dict]) -> float:
    """
    Order Book Imbalance (OFI proxy) desde los precios bid/ask del mercado UpDown.
    Si best_bid está muy cerca de best_ask en el lado UP, hay presión compradora.

    Fórmula simplificada:
      imbalance = (up_price - 0.5) × 2    → [-1, +1]
      Si UP cuesta 0.65 → mercado presionado al alza → +0.30 señal UP
      Si UP cuesta 0.35 → mercado presionado a la baja → -0.30 señal DOWN

    El knowledge base (v83) usa CVD completo, pero con los datos disponibles
    (solo precio de mercado, no full order book) este proxy es el mejor aproximado.
    """
    if not market:
        return 0.0
    up_price = float(market.get("up_price", 0.5) or 0.5)
    # Neutralizar en 0.50, amplificar desviaciones
    lean = (up_price - 0.5) * 2.0
    return max(-1.0, min(1.0, lean))


# ── Señal combinada ───────────────────────────────────────────────────────────

def build_btc_direction_signal(
    ta_data: dict,
    btc_price: float,
    btc_price_window_start: Optional[float] = None,
    cmc_data: Optional[dict] = None,
    market: Optional[dict] = None,
    telonex_signals: Optional[dict] = None,
) -> dict:
    """
    Construye la señal de dirección BTC combinando todas las fuentes.

    telonex_signals (opcional): dict de telonex_data.get_updown_signals()
        real_ofi    – OFI on-chain de la ventana actual  [-1, +1]
        smart_bias  – sesgo de smart wallets             [-1, +1]
        available   – bool; si False se ignora

    Returns dict con:
        combined   – señal combinada [−1, +1]; positivo=UP, negativo=DOWN
        confidence – |combined| × 100  (0–100%)
        direction  – "UP" / "DOWN" / "NEUTRAL"
        components – desglose para logging
    """
    ta_signal = float(ta_data.get("signal", 0.0) or 0.0)
    rsi_val   = ta_data.get("rsi")
    ema20     = ta_data.get("ema20")
    ema50     = ta_data.get("ema50")
    macd      = ta_data.get("macd")

    rsi_sig   = _rsi_signal(rsi_val)
    ema_sig   = _ema_signal(ema20, ema50)
    macd_sig  = _macd_signal(macd)
    mom_sig   = _window_momentum(btc_price, btc_price_window_start)
    macro_sig = _macro_signal(cmc_data)

    # Componente TA: señal TV agregada + refuerzo RSI + EMA + MACD
    ta_composite = (
        ta_signal * 0.55
        + rsi_sig  * 0.20
        + ema_sig  * 0.15
        + macd_sig * 0.10
    )
    ta_composite = max(-1.0, min(1.0, ta_composite))

    # Señal de consenso de mercado (Polymarket) + OFI proxy
    mkt_sig = 0.0
    ofi_sig = 0.0
    if market:
        mkt_sig = _market_consensus_signal(
            market.get("up_price", 0.5),
            market.get("down_price", 0.5),
        )
        ofi_sig = _order_book_imbalance(market)

    # ── Telonex on-chain signals ──────────────────────────────────────────────
    use_telonex = bool(telonex_signals and telonex_signals.get("available"))
    real_ofi   = float(telonex_signals.get("real_ofi", 0.0))   if use_telonex else 0.0
    smart_bias = float(telonex_signals.get("smart_bias", 0.0)) if use_telonex else 0.0

    if use_telonex:
        # Pesos con Telonex: TA=0.43, MOM=0.22, OFI_REAL=0.12, SMART=0.10, MACRO=0.08, MKT=0.05
        combined = (
            ta_composite * _W_TA_TX
            + mom_sig    * _W_MOMENTUM_TX
            + real_ofi   * _W_OFI_REAL_TX
            + smart_bias * _W_SMART_TX
            + macro_sig  * _W_MACRO_TX
            + mkt_sig    * _W_MARKET_TX
        )
    else:
        # Pesos sin Telonex (originales): TA=0.50, MOM=0.25, OFI_PROXY=0.10, MACRO=0.10, MKT=0.05
        combined = (
            ta_composite * _W_TA
            + mom_sig    * _W_MOMENTUM
            + ofi_sig    * _W_OFI
            + macro_sig  * _W_MACRO
            + mkt_sig    * _W_MARKET
        )
    combined = max(-1.0, min(1.0, combined))

    # Movimiento BTC en la ventana (para log)
    window_pct = 0.0
    if btc_price_window_start and btc_price_window_start > 0 and btc_price:
        window_pct = round((btc_price - btc_price_window_start) / btc_price_window_start * 100, 4)

    return {
        "combined":          round(combined, 4),
        "ta":                round(ta_composite, 4),
        "ta_raw":            round(ta_signal, 4),
        "rsi_sig":           round(rsi_sig, 4),
        "ema_sig":           round(ema_sig, 4),
        "macd_sig":          round(macd_sig, 4),
        "momentum":          round(mom_sig, 4),
        "market_sig":        round(mkt_sig, 4),
        "macro":             round(macro_sig, 4),
        "ofi":               round(ofi_sig, 4),
        "real_ofi":          round(real_ofi, 4),
        "smart_bias":        round(smart_bias, 4),
        "telonex_available": use_telonex,
        "rsi":               rsi_val,
        "macd":              macd,
        "window_pct":        window_pct,
        "confidence":        round(abs(combined) * 100, 1),
        "direction":         "UP" if combined > 0 else ("DOWN" if combined < 0 else "NEUTRAL"),
    }


# ── Evaluación del mercado ────────────────────────────────────────────────────

def evaluate_updown_market(
    market: dict,
    ta_data: dict,
    btc_price: float,
    btc_price_window_start: Optional[float] = None,
    cmc_data: Optional[dict] = None,
    adaptive_params: Optional[dict] = None,
    telonex_signals: Optional[dict] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """
    Evalúa si hay condiciones para operar el mercado UP/DOWN.
    Retorna (oportunidad, None) si hay señal, o (None, motivo_str) si no hay entrada.
    """
    # ── PASO 1: Filtros de mercado ────────────────────────────────────────────

    up_price   = market.get("up_price", 0.5)
    down_price = market.get("down_price", 0.5)

    if up_price <= 0.02 or down_price <= 0.02:
        return None, f"Mercado degenerado: UP={up_price:.3f} DOWN={down_price:.3f} (sin liquidez)"
    if up_price >= 0.98 or down_price >= 0.98:
        return None, f"Mercado ya resuelto: UP={up_price:.3f} DOWN={down_price:.3f}"

    # Parámetros adaptativos del learner
    if adaptive_params is None:
        try:
            from updown_learner import get_adaptive_params
            adaptive_params = get_adaptive_params(market["interval_minutes"])
        except Exception:
            adaptive_params = {}

    min_confidence = max(adaptive_params.get("min_signal", _MIN_CONFIDENCE), _MIN_CONFIDENCE)
    invert_signal  = adaptive_params.get("invert_signal", False)
    max_elapsed    = adaptive_params.get("max_elapsed_min")

    elapsed          = market.get("elapsed_minutes", 0.0)
    interval_minutes = market.get("interval_minutes", 15)

    # Floor fijo por intervalo (learner solo puede SUBIR, nunca bajar del floor)
    _floor = _MIN_ELAPSED_15M if interval_minutes >= 15 else _MIN_ELAPSED_5M
    learner_min = adaptive_params.get("min_elapsed_min")
    min_elapsed = max(_floor, learner_min) if learner_min is not None else _floor

    if elapsed < min_elapsed:
        return None, (
            f"Timing: ventana muy temprana "
            f"({elapsed:.1f}min < mín {min_elapsed:.1f}min — "
            f"floor {_floor}min, learner {learner_min}min)"
        )
    if max_elapsed is not None and elapsed > max_elapsed:
        return None, f"Timing: ventana muy avanzada ({elapsed:.1f}min > máx {max_elapsed:.1f}min)"

    # ── PASO 2: Señal de dirección ────────────────────────────────────────────

    interval_min = market.get("interval_minutes", 15)

    sig = build_btc_direction_signal(
        ta_data=ta_data,
        btc_price=btc_price,
        btc_price_window_start=btc_price_window_start,
        cmc_data=cmc_data,
        market=market,
        telonex_signals=telonex_signals,
    )

    combined = sig["combined"]

    # ── Mean reversion en 5m ──────────────────────────────────────────────────
    # En ventanas de 5 minutos el bot entra cuando ya transcurrieron ~30-90 seg.
    # El movimiento inicial de BTC en esos primeros 90s tiende a REVERTIR antes
    # de que cierre la ventana (el token ya tiene ese movimiento descontado en el
    # precio y el mercado se equilibra). Por eso invertimos el componente de
    # momentum para 5m: si BTC subió → apostamos DOWN (reversión inminente).
    # El TA de tendencia macro se mantiene porque da contexto más amplio.
    if interval_min <= 5:
        mom_raw       = sig["momentum"]
        ta_composite  = sig["ta"]
        # Recalcular combined con momentum invertido (mean-reversion)
        combined = (
            ta_composite   * _W_TA
            + (-mom_raw)   * _W_MOMENTUM   # <-- invertido
            + sig["market_sig"] * _W_MARKET
            + sig["macro"]      * _W_MACRO
        )
        combined = max(-1.0, min(1.0, combined))
        sig["combined"]   = round(combined, 4)
        sig["direction"]  = "UP" if combined > 0 else ("DOWN" if combined < 0 else "NEUTRAL")
        sig["5m_mode"]    = "mean_reversion (momentum invertido)"

    if invert_signal:
        combined = -combined
        sig["combined"]  = combined
        sig["direction"] = "UP" if combined > 0 else ("DOWN" if combined < 0 else "NEUTRAL")

    # ── PASO 3: Gate de momentum ──────────────────────────────────────────────
    # En 5m usamos mean-reversion por lo que el gate de momentum estándar no
    # aplica (ya invertimos el momentum en el combined). Solo aplicamos gate en 15m.
    momentum      = sig["momentum"]

    if interval_min > 5:
        base_threshold = 0.20
        mom_threshold  = adaptive_params.get("momentum_gate_threshold", base_threshold)

        if combined > 0 and momentum < -mom_threshold:
            return None, (
                f"Gate momentum: señal UP pero BTC bajó {sig['window_pct']:+.3f}% en la ventana "
                f"(momentum={momentum:+.3f} < -{mom_threshold:.2f}) — inercia bajista en contra"
            )
        if combined < 0 and momentum > mom_threshold:
            return None, (
                f"Gate momentum: señal DOWN pero BTC subió {sig['window_pct']:+.3f}% en la ventana "
                f"(momentum={momentum:+.3f} > +{mom_threshold:.2f}) — inercia alcista en contra"
            )

    # ── PASO 4: Confianza y ratio riesgo/recompensa ───────────────────────────

    confidence = abs(combined)
    if confidence < min_confidence:
        dominant = max(
            [("TA", abs(sig["ta"])), ("Momentum", abs(sig["momentum"])),
             ("Mercado", abs(sig["market_sig"])), ("Macro", abs(sig["macro"]))],
            key=lambda x: x[1]
        )
        return None, (
            f"Señal débil: confianza {confidence*100:.1f}% < mínimo {min_confidence*100:.1f}% | "
            f"Dirección tentativa: {sig['direction']} | "
            f"Componente más fuerte: {dominant[0]} ({dominant[1]:.3f}) | "
            f"TA:{sig['ta_raw']:+.3f} RSI:{sig['rsi']} Momentum:{sig['momentum']:+.3f} Macro:{sig['macro']:+.3f}"
        )

    # Dirección y precio de entrada
    if combined > 0:
        side        = "UP"
        token_id    = market["up_token"]
        entry_price = up_price
    else:
        side        = "DOWN"
        token_id    = market["down_token"]
        entry_price = down_price

    # ── Gate por lado (si el historial muestra que ese lado pierde) ──────────
    if side == "DOWN" and adaptive_params.get("block_down"):
        return None, "Learner: lado DOWN bloqueado por bajo win rate histórico"
    if side == "UP" and adaptive_params.get("block_up"):
        return None, "Learner: lado UP bloqueado por bajo win rate histórico"

    if entry_price >= _MAX_ENTRY_PRICE:
        rr = round((1 - entry_price) / entry_price, 3)
        return None, (
            f"Precio caro: {side} cuesta ${entry_price:.3f} (≥ límite ${_MAX_ENTRY_PRICE}) | "
            f"Ganarías ${1-entry_price:.3f} arriesgando ${entry_price:.3f} → ratio {rr:.2f}:1 inaceptable"
        )

    # ── PASO 4: Sizing ────────────────────────────────────────────────────────

    import math as _math
    size_usdc  = max(1.0, round(float(bot_params.updown_max_usdc), 2))
    MIN_SHARES = 5
    min_cost   = round(MIN_SHARES * entry_price, 2)

    # Verificar presupuesto antes de calcular shares
    if min_cost > size_usdc:
        return None, (
            f"Presupuesto insuficiente para mínimo 5 shares: "
            f"necesitas ${min_cost} (5 × {entry_price:.3f}) pero updown_max_usdc=${size_usdc} — "
            f"sube updown_max_usdc a al menos ${min_cost}"
        )

    # Shares = máximo entero que cabe en el presupuesto, mínimo 5
    shares = max(MIN_SHARES, _math.floor(size_usdc / entry_price))

    # Reward/risk explícito para logging
    reward_per_share = round(1.0 - entry_price, 4)
    rr_ratio         = round(reward_per_share / entry_price, 3) if entry_price > 0 else 0

    return {
        # Identificación
        "slug":             market["slug"],
        "poly_url":         market.get("poly_url", ""),
        "title":            market["title"],
        "condition_id":     market["condition_id"],
        "asset":            "BTC_UPDOWN",
        "interval_minutes": market["interval_minutes"],
        # Trade
        "side":         side,
        "token_id":     token_id,
        "entry_price":  entry_price,
        "size_usdc":    size_usdc,
        "shares":       shares,
        # Señal completa
        "confidence":        round(confidence * 100, 1),
        "combined_signal":   round(combined, 4),
        "ta_signal":         sig["ta_raw"],
        "ta_recommendation": ta_data.get("recommendation", "NEUTRAL"),
        "ta_rsi":            sig["rsi"],
        "ta_macd":           sig["macd"],
        "window_momentum":   sig["momentum"],
        "window_pct":        sig["window_pct"],
        "ema_signal":        sig["ema_sig"],
        "macd_signal":       sig["macd_sig"],
        "market_signal":     sig["market_sig"],
        "macro_signal":      sig["macro"],
        "signal_breakdown":  sig,
        # Contexto del mercado
        "up_price":          up_price,
        "down_price":        down_price,
        "btc_price_start":   btc_price_window_start,
        "btc_price_now":     btc_price,
        # Ratio riesgo/recompensa
        "reward_per_share":  reward_per_share,
        "rr_ratio":          rr_ratio,
        # Para el learner
        "our_prob":     round(0.5 + confidence * 0.15, 4),
        "market_prob":  entry_price,
        "ev_pct":       round((0.5 + confidence * 0.15 - entry_price) / entry_price * 100, 1),
        # Tiempo / calidad
        "minutes_to_close": market["minutes_to_close"],
        "elapsed_minutes":  elapsed,
        "liquidity":        market["liquidity"],
        "spread_pct":       market["spread_pct"],
        "adaptive_params":  adaptive_params,
    }, None
