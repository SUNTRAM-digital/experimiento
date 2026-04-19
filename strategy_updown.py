"""
Estrategia para mercados BTC Up/Down (5m y 15m) — Motor profesional v2.

FLUJO DE DECISIÓN:
  1. FILTROS DE MERCADO
  2. DETECCIÓN DE RÉGIMEN (trending vs ranging, volatilidad)
  3. CONSTRUCCIÓN DE SEÑAL MULTI-FUENTE
     a) Multi-timeframe TA (1m + 5m/15m + 1h) — alineación de marcos temporales
     b) RSI + Stochastic — osciladores sobrecompra/sobreventa
     c) Bollinger Bands — precio relativo a volatilidad histórica
     d) MACD + AO (Awesome Oscillator) — momentum de tendencia
     e) EMA stack (9/20/50/100/200) — estructura de tendencia multi-EMA
     f) ADX — fuerza de tendencia (trending vs ranging)
     g) Funding rate Binance — contrarian: longs sobrecargados → presión bajista
     h) Momentum intra-ventana — BTC ahora vs inicio de la ventana
     i) Macro CMC 1h — inercia de mercado
     j) OFI / Consenso Polymarket
     k) Telonex on-chain (cuando disponible)
  4. PESOS DINÁMICOS según régimen
  5. GATES Y DECISIÓN FINAL
"""
from typing import Optional
from config import bot_params

# ── Umbrales ──────────────────────────────────────────────────────────────────
_MIN_CONFIDENCE  = 0.20
_MAX_ENTRY_PRICE = 0.89
_MIN_ELAPSED_5M  = 1.5
_MIN_ELAPSED_15M = 5.0


# ══════════════════════════════════════════════════════════════════════════════
# SEÑALES INDIVIDUALES
# ══════════════════════════════════════════════════════════════════════════════

def _ema_signal(ema20: Optional[float], ema50: Optional[float]) -> float:
    if not ema20 or not ema50:
        return 0.0
    diff_pct = (ema20 - ema50) / ema50
    return max(-1.0, min(1.0, diff_pct / 0.003))


def _ema_stack_signal(ta: dict) -> float:
    """
    Stack de EMAs: cuántas están alineadas (9>20>50>100>200 = alcista total).
    Retorna [-1, +1] proporcional al alineamiento.
    """
    emas = [
        ta.get("ema9"), ta.get("ema20") or ta.get("ema21"),
        ta.get("ema50"), ta.get("ema100"), ta.get("ema200"),
    ]
    close = ta.get("close")
    if not close or close <= 0:
        return 0.0
    # Contar cuántas EMAs están por debajo del precio (alcista)
    valid = [(e, e is not None and e > 0) for e in emas]
    below = sum(1 for e, ok in valid if ok and close > e)
    above = sum(1 for e, ok in valid if ok and close < e)
    total = below + above
    if total == 0:
        return 0.0
    return max(-1.0, min(1.0, (below - above) / total))


def _rsi_signal(rsi: Optional[float]) -> float:
    if rsi is None:
        return 0.0
    if rsi <= 25:
        return min(1.0, (25 - rsi) / 25 * 1.5)   # zona extrema de sobreventa
    if rsi <= 35:
        return (35 - rsi) / 35
    if rsi >= 75:
        return max(-1.0, (75 - rsi) / 25 * 1.5)  # zona extrema de sobrecompra
    if rsi >= 65:
        return (65 - rsi) / 35
    return (rsi - 50) / 50 * 0.20   # zona neutral: señal muy suave


def _stoch_signal(k: Optional[float], d: Optional[float]) -> float:
    """
    Stochastic %K/%D: señal de sobrecompra/sobreventa con divergencia.
    K<20 + cruzando hacia arriba: UP fuerte
    K>80 + cruzando hacia abajo: DOWN fuerte
    """
    if k is None:
        return 0.0
    sig = 0.0
    if k <= 20:
        sig = (20 - k) / 20      # 0.0 a 1.0
    elif k >= 80:
        sig = (80 - k) / 20      # -1.0 a 0.0
    # Añadir señal de cruce K/D
    if d is not None:
        cross = (k - d) / 20.0   # K > D = momentum alcista
        sig = sig * 0.7 + cross * 0.3
    return max(-1.0, min(1.0, sig))


def _bb_signal(close: Optional[float], bb_upper: Optional[float],
               bb_lower: Optional[float], bb_basis: Optional[float]) -> tuple[float, float]:
    """
    Bollinger Bands: posición del precio dentro de las bandas.
    Retorna (señal, ancho_normalizado):
      señal: +1 en banda inferior (UP), -1 en banda superior (DOWN)
      ancho: BB width / basis → 0 = muy comprimido, 1 = muy expandido
    """
    if not close or not bb_upper or not bb_lower or not bb_basis or bb_basis <= 0:
        return 0.0, 0.0
    width = (bb_upper - bb_lower) / bb_basis
    # Posición del precio: 0=banda inferior, 0.5=basis, 1=banda superior
    band_range = bb_upper - bb_lower
    if band_range <= 0:
        return 0.0, width
    position = (close - bb_lower) / band_range   # 0..1
    # Señal: mean reversion. Precio en zona alta → DOWN, en zona baja → UP
    # Amplificar en extremos (>0.9 o <0.1)
    signal = -(position - 0.5) * 2.0  # -1 en bb_upper, +1 en bb_lower
    if position > 0.90:
        signal *= 1.3   # señal bajista amplificada sobre la banda
    elif position < 0.10:
        signal *= 1.3   # señal alcista amplificada bajo la banda
    return max(-1.0, min(1.0, signal)), round(width, 4)


def _macd_signal(macd: Optional[float], macd_signal_line: Optional[float] = None) -> float:
    """
    MACD vs signal line: cruce alcista/bajista + magnitud.
    """
    if macd is None:
        return 0.0
    base = max(-1.0, min(1.0, macd / 50.0))
    if macd_signal_line is not None:
        # Histograma: MACD - signal. Positivo = aceleración alcista
        hist = macd - macd_signal_line
        hist_sig = max(-1.0, min(1.0, hist / 30.0))
        return base * 0.6 + hist_sig * 0.4
    return base


def _ao_signal(ao: Optional[float]) -> float:
    """Awesome Oscillator: positivo=momentum alcista, negativo=bajista."""
    if ao is None:
        return 0.0
    return max(-1.0, min(1.0, ao / 500.0))


def _funding_rate_signal(funding: Optional[dict]) -> float:
    """
    Funding rate perpetuos Binance (contrarian):
      Positivo (longs pagan) → mercado sobre-comprado → señal bajista (DOWN)
      Negativo (shorts pagan) → mercado sobre-vendido → señal alcista (UP)
      También considera premium: mark >> index → exceso de longs → bajista

    Rango típico funding: -0.05% a +0.05% cada 8h.
    ±0.03% diario = señal máxima.
    """
    if not funding or not funding.get("available"):
        return 0.0
    rate = float(funding.get("funding_rate", 0))
    # Convertir a señal contrarian (invertir signo)
    # ±0.0003 = ±0.03% cada 8h → señal ~±1.0
    rate_sig = max(-1.0, min(1.0, -rate / 0.0003))
    # Añadir prima mark/index (premium_pct positivo = exceso longs → bajista)
    premium = float(funding.get("premium_pct", 0))
    prem_sig = max(-1.0, min(1.0, -premium / 0.05))
    return rate_sig * 0.7 + prem_sig * 0.3


def _adx_regime(adx: Optional[float], adx_pos: Optional[float],
                adx_neg: Optional[float]) -> tuple[str, float]:
    """
    Detecta régimen de mercado usando ADX.
    Retorna (régimen, fuerza):
      "trending_up"   → ADX>25 y DI+ > DI-
      "trending_down" → ADX>25 y DI- > DI+
      "ranging"       → ADX<20
      "neutral"       → entre 20-25
    """
    if adx is None:
        return "neutral", 0.5
    if adx >= 25:
        if adx_pos is not None and adx_neg is not None:
            regime = "trending_up" if adx_pos > adx_neg else "trending_down"
        else:
            regime = "trending"
        strength = min(1.0, (adx - 25) / 25)
    elif adx <= 20:
        regime = "ranging"
        strength = (20 - adx) / 20
    else:
        regime = "neutral"
        strength = 0.5
    return regime, round(strength, 3)


def _multi_tf_alignment(ta_map: dict) -> tuple[float, int]:
    """
    Alineación multi-timeframe: cuenta cuántos timeframes apuntan la misma dirección.
    ta_map: {"1m": ta_data, "15m": ta_data, "1h": ta_data}
    Retorna (señal_combinada, n_alineados):
      señal: promedio ponderado de señales × bonus de alineación
      n_alineados: 0-3 (todos los TF disponibles apuntando mismo lado)
    """
    if not ta_map:
        return 0.0, 0
    # Peso mayor al timeframe más alto (más confiable para dirección)
    weights = {"1m": 0.20, "3m": 0.20, "5m": 0.30, "15m": 0.30, "1h": 0.50}
    signals = []
    total_w = 0.0
    for iv, ta in ta_map.items():
        if not ta or not ta.get("available"):
            continue
        w = weights.get(iv, 0.25)
        s = float(ta.get("signal", 0.0) or 0.0)
        signals.append((s, w))
        total_w += w
    if not signals or total_w == 0:
        return 0.0, 0
    combined = sum(s * w for s, w in signals) / total_w
    # Bonus de alineación: si todos apuntan el mismo lado, amplificar
    signs = [1 if s > 0.05 else (-1 if s < -0.05 else 0) for s, _ in signals]
    non_neutral = [x for x in signs if x != 0]
    if non_neutral:
        aligned = sum(1 for x in non_neutral if x == non_neutral[0]) / len(non_neutral)
        if aligned >= 0.8:   # ≥80% alineados = bonus
            combined *= 1.25
    n_aligned = sum(1 for x in non_neutral if x == (non_neutral[0] if non_neutral else 0))
    return max(-1.0, min(1.0, combined)), n_aligned


def _window_momentum(btc_now: float, btc_start: Optional[float]) -> float:
    if not btc_start or btc_start <= 0 or not btc_now:
        return 0.0
    pct = (btc_now - btc_start) / btc_start
    return max(-1.0, min(1.0, pct * 500))


def _market_consensus_signal(up_price: float, down_price: float) -> float:
    if up_price <= 0 or down_price <= 0:
        return 0.0
    lean = up_price - 0.50
    return max(-1.0, min(1.0, lean / 0.35))


def _macro_signal(cmc_data: Optional[dict]) -> float:
    if not cmc_data:
        return 0.0
    change_1h = cmc_data.get("percent_change_1h", 0.0) or 0.0
    return max(-1.0, min(1.0, change_1h / 2.0))


def _order_book_imbalance(market: Optional[dict]) -> float:
    if not market:
        return 0.0
    up_price = float(market.get("up_price", 0.5) or 0.5)
    lean = (up_price - 0.5) * 2.0
    return max(-1.0, min(1.0, lean))


# ══════════════════════════════════════════════════════════════════════════════
# SEÑAL COMBINADA — MOTOR PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

def build_btc_direction_signal(
    ta_data: dict,
    btc_price: float,
    btc_price_window_start: Optional[float] = None,
    cmc_data: Optional[dict] = None,
    market: Optional[dict] = None,
    telonex_signals: Optional[dict] = None,
    ta_multi: Optional[dict] = None,   # {"1m": ta, "15m": ta, "1h": ta}
    funding_data: Optional[dict] = None,
) -> dict:
    """
    Motor de señal profesional v2 con régimen detection y multi-timeframe.
    """
    # ── Extraer indicadores del TA principal ─────────────────────────────────
    ta_signal    = float(ta_data.get("signal", 0.0) or 0.0)
    rsi_val      = ta_data.get("rsi")
    stoch_k      = ta_data.get("stoch_k")
    stoch_d      = ta_data.get("stoch_d")
    ema20        = ta_data.get("ema20")
    ema50        = ta_data.get("ema50")
    macd         = ta_data.get("macd")
    macd_sig_ln  = ta_data.get("macd_signal")
    ao           = ta_data.get("ao")
    bb_upper     = ta_data.get("bb_upper")
    bb_lower     = ta_data.get("bb_lower")
    bb_basis     = ta_data.get("bb_basis")
    close_price  = ta_data.get("close") or btc_price
    adx          = ta_data.get("adx")
    adx_pos      = ta_data.get("adx_pos")
    adx_neg      = ta_data.get("adx_neg")

    # ── Calcular cada señal ───────────────────────────────────────────────────
    rsi_sig      = _rsi_signal(rsi_val)
    stoch_sig    = _stoch_signal(stoch_k, stoch_d)
    ema_sig      = _ema_signal(ema20, ema50)
    ema_stack    = _ema_stack_signal(ta_data)
    macd_sig     = _macd_signal(macd, macd_sig_ln)
    ao_sig       = _ao_signal(ao)
    bb_sig, bb_width = _bb_signal(close_price, bb_upper, bb_lower, bb_basis)
    mom_sig      = _window_momentum(btc_price, btc_price_window_start)
    macro_sig    = _macro_signal(cmc_data)
    funding_sig  = _funding_rate_signal(funding_data)

    # ── Detección de régimen (ADX) ────────────────────────────────────────────
    regime, regime_strength = _adx_regime(adx, adx_pos, adx_neg)
    is_trending = regime in ("trending_up", "trending_down", "trending")
    is_ranging  = regime == "ranging"

    # ── Multi-timeframe alignment ─────────────────────────────────────────────
    mtf_sig, n_aligned = _multi_tf_alignment(ta_multi or {})

    # ── Composite TA: ponderar osciladores según régimen ─────────────────────
    if is_trending:
        # En tendencia: EMA stack y MACD pesan más; BB como señal de continuación
        ta_composite = (
            ta_signal   * 0.35
            + ema_stack * 0.20
            + macd_sig  * 0.20
            + ao_sig    * 0.10
            + rsi_sig   * 0.10
            + ema_sig   * 0.05
        )
        # BB en tendencia = señal de entrada (precio en banda inferior = pull-back en uptrend)
        bb_contribution = bb_sig * 0.08   # más suave en tendencia
    elif is_ranging:
        # En rango: RSI, Stochastic y BB pesan más (mean reversion dominante)
        ta_composite = (
            ta_signal    * 0.20
            + rsi_sig    * 0.25
            + stoch_sig  * 0.20
            + bb_sig     * 0.20
            + macd_sig   * 0.10
            + ema_sig    * 0.05
        )
        bb_contribution = bb_sig * 0.15   # BB importa más en rango
    else:
        # Neutral: balance entre osciladores y tendencia
        ta_composite = (
            ta_signal   * 0.30
            + rsi_sig   * 0.20
            + ema_stack * 0.15
            + macd_sig  * 0.15
            + stoch_sig * 0.10
            + ao_sig    * 0.05
            + ema_sig   * 0.05
        )
        bb_contribution = bb_sig * 0.12
    ta_composite = max(-1.0, min(1.0, ta_composite))

    # ── Mercado / OFI ─────────────────────────────────────────────────────────
    mkt_sig = 0.0
    ofi_sig = 0.0
    if market:
        mkt_sig = _market_consensus_signal(
            market.get("up_price", 0.5),
            market.get("down_price", 0.5),
        )
        ofi_sig = _order_book_imbalance(market)

    # ── Telonex on-chain ──────────────────────────────────────────────────────
    use_telonex = bool(telonex_signals and telonex_signals.get("available"))
    real_ofi    = float(telonex_signals.get("real_ofi", 0.0))   if use_telonex else 0.0
    smart_bias  = float(telonex_signals.get("smart_bias", 0.0)) if use_telonex else 0.0

    # ── CONSTRUCCIÓN FINAL ponderada ─────────────────────────────────────────
    # Pesos base — ajustados según disponibilidad de datos
    has_mtf     = bool(ta_multi and any(v.get("available") for v in ta_multi.values()))
    has_funding = bool(funding_data and funding_data.get("available"))

    if use_telonex:
        combined = (
            ta_composite  * 0.28
            + mtf_sig     * (0.12 if has_mtf else 0.0)
            + bb_contribution
            + mom_sig     * 0.15
            + real_ofi    * 0.12
            + smart_bias  * 0.10
            + funding_sig * (0.07 if has_funding else 0.0)
            + macro_sig   * 0.06
            + ofi_sig     * 0.05
            + mkt_sig     * 0.03
        )
    elif has_mtf and has_funding:
        combined = (
            ta_composite  * 0.28
            + mtf_sig     * 0.15
            + bb_contribution
            + mom_sig     * 0.18
            + funding_sig * 0.10
            + macro_sig   * 0.08
            + ofi_sig     * 0.07
            + mkt_sig     * 0.04
        )
    elif has_mtf:
        combined = (
            ta_composite  * 0.32
            + mtf_sig     * 0.18
            + bb_contribution
            + mom_sig     * 0.20
            + macro_sig   * 0.10
            + ofi_sig     * 0.08
            + mkt_sig     * 0.04
        )
    elif has_funding:
        combined = (
            ta_composite  * 0.35
            + bb_contribution
            + mom_sig     * 0.22
            + funding_sig * 0.12
            + macro_sig   * 0.10
            + ofi_sig     * 0.09
            + mkt_sig     * 0.04
        )
    else:
        # Modo clásico (sin MTF ni funding): igual que v1 pero con más indicadores en ta_composite
        combined = (
            ta_composite  * 0.40
            + bb_contribution
            + mom_sig     * 0.25
            + macro_sig   * 0.12
            + ofi_sig     * 0.10
            + mkt_sig     * 0.05
        )

    # Escalar por alineación multi-TF (bonus máx +20% si todos alineados)
    if has_mtf and n_aligned >= 2:
        scale = 1.0 + (n_aligned - 1) * 0.08
        combined *= scale

    combined = max(-1.0, min(1.0, combined))

    # Movimiento BTC en la ventana
    window_pct = 0.0
    if btc_price_window_start and btc_price_window_start > 0 and btc_price:
        window_pct = round((btc_price - btc_price_window_start) / btc_price_window_start * 100, 4)

    # Calidad de señal TA: qué fracción de indicadores son direccionales (no neutrales)
    # buy=10, sell=3, neutral=14 → consensus = 13/27 = 0.48 (moderado)
    # buy=2,  sell=1, neutral=24 → consensus = 3/27  = 0.11 (muy bajo, señal ruidosa)
    ta_buy     = int(ta_data.get("buy",     0) or 0)
    ta_sell    = int(ta_data.get("sell",    0) or 0)
    ta_neutral = int(ta_data.get("neutral", 0) or 0)
    ta_total   = ta_buy + ta_sell + ta_neutral
    ta_consensus = round((ta_buy + ta_sell) / ta_total, 3) if ta_total > 0 else 0.5

    # Penalizar confianza cuando TA es muy ruidosa (pocas señales direccionales)
    # Solo aplica cuando NO hay desplazamiento claro (displacement < lo_threshold)
    window_abs = abs(window_pct)
    _lo = 0.10  # umbral bajo de desplazamiento
    if ta_consensus < 0.25 and window_abs < _lo:
        # TA casi toda neutral y BTC sin movimiento claro → señal puro ruido
        combined *= 0.50   # reducir señal al 50%
        combined = max(-1.0, min(1.0, combined))
    elif ta_consensus < 0.35 and window_abs < _lo:
        combined *= 0.75
        combined = max(-1.0, min(1.0, combined))

    return {
        "combined":          round(combined, 4),
        "ta":                round(ta_composite, 4),
        "ta_raw":            round(ta_signal, 4),
        "rsi_sig":           round(rsi_sig, 4),
        "stoch_sig":         round(stoch_sig, 4),
        "ema_sig":           round(ema_sig, 4),
        "ema_stack":         round(ema_stack, 4),
        "macd_sig":          round(macd_sig, 4),
        "ao_sig":            round(ao_sig, 4),
        "bb_sig":            round(bb_sig, 4),
        "bb_width":          round(bb_width, 4),
        "bb_contribution":   round(bb_contribution, 4),
        "funding_sig":       round(funding_sig, 4),
        "mtf_sig":           round(mtf_sig, 4),
        "n_aligned":         n_aligned,
        "regime":            regime,
        "regime_strength":   regime_strength,
        "momentum":          round(mom_sig, 4),
        "market_sig":        round(mkt_sig, 4),
        "macro":             round(macro_sig, 4),
        "ofi":               round(ofi_sig, 4),
        "real_ofi":          round(real_ofi, 4),
        "smart_bias":        round(smart_bias, 4),
        "telonex_available": use_telonex,
        "rsi":               rsi_val,
        "stoch_k":           stoch_k,
        "stoch_d":           stoch_d,
        "adx":               adx,
        "bb_upper":          bb_upper,
        "bb_lower":          bb_lower,
        "macd":              macd,
        "window_pct":        window_pct,
        "ta_consensus":      ta_consensus,   # fracción de indicadores direccionales
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
    ta_multi: Optional[dict] = None,
    funding_data: Optional[dict] = None,
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

    # Floor de confianza mínima — puede ser overrideado por bot_params via adaptive_params
    _min_conf_floor = adaptive_params.get("min_signal_floor", _MIN_CONFIDENCE)
    min_confidence  = max(adaptive_params.get("min_signal", _min_conf_floor), _min_conf_floor)
    invert_signal   = adaptive_params.get("invert_signal", False)
    max_elapsed     = adaptive_params.get("max_elapsed_min")

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
        ta_multi=ta_multi,
        funding_data=funding_data,
    )

    combined = sig["combined"]

    # ── Señal de desplazamiento: precio actual vs inicio de ventana ──────────
    # Cuando BTC ya está significativamente por encima/abajo del precio de
    # apertura, el mercado PROBABLEMENTE ya cerró en esa dirección.
    # Este es el predictor más fuerte para entradas tardías.
    window_pct_val = sig.get("window_pct", 0.0)      # % movimiento en la ventana
    displacement   = abs(window_pct_val)              # magnitud del movimiento
    # Umbrales configurables (defaults en config o params del bot)
    try:
        _disp_hi = float(getattr(bot_params, "updown_displacement_hi_pct", 0.20))
        _disp_lo = float(getattr(bot_params, "updown_displacement_lo_pct", 0.10))
    except Exception:
        _disp_hi, _disp_lo = 0.20, 0.10

    # Señal de desplazamiento: signo del movimiento en la ventana
    displacement_sig = 1.0 if window_pct_val > 0 else (-1.0 if window_pct_val < 0 else 0.0)

    # ── Mean reversion / trend-follow adaptativo en 5m ───────────────────────
    # Lógica: movimiento GRANDE = seguir tendencia (precio ya se movió).
    #         movimiento pequeño en rango = mean-reversion puede aplicar.
    if interval_min <= 5:
        mom_raw      = sig["momentum"]
        ta_composite = sig["ta"]
        bb_sig_val   = sig.get("bb_sig", 0.0)
        regime       = sig.get("regime", "neutral")

        if displacement >= _disp_hi:
            # Movimiento grande (>0.20%): BTC ya se desplazó fuertemente.
            # Seguir la dirección del movimiento — no invertir.
            mom_inversion = 1.0
            mode_label    = f"trend_follow (disp={displacement:.3f}%>={_disp_hi:.2f}%)"
        elif displacement >= _disp_lo:
            # Movimiento medio (0.10-0.20%): neutral, dejar que TA decida.
            mom_inversion = 0.0
            mode_label    = f"neutral (disp={displacement:.3f}% in [{_disp_lo:.2f},{_disp_hi:.2f}]%)"
        elif regime in ("trending_up", "trending_down"):
            # Tendencia fuerte + movimiento pequeño: semi mean-reversion
            mom_inversion = -0.5
            mode_label    = f"semi_mean_rev (regime={regime}, disp={displacement:.3f}%)"
        else:
            # Rango + movimiento pequeño: mean-reversion completa
            mom_inversion = -1.0
            mode_label    = f"mean_rev (regime={regime}, disp={displacement:.3f}%)"

        combined = max(-1.0, min(1.0,
            ta_composite              * 0.35
            + (mom_raw * mom_inversion) * 0.30
            + displacement_sig        * (0.20 if displacement >= _disp_lo else 0.0)
            + bb_sig_val              * 0.10
            + sig["market_sig"]       * 0.05
        ))
        sig["combined"]       = round(combined, 4)
        sig["direction"]      = "UP" if combined > 0 else ("DOWN" if combined < 0 else "NEUTRAL")
        sig["confidence"]     = round(abs(combined) * 100, 1)
        sig["5m_mode"]        = mode_label
        sig["displacement"]   = round(displacement, 4)
        sig["mom_inversion"]  = mom_inversion

    elif interval_min >= 15:
        # ── Recalculación displacement-aware para 15m ────────────────────────
        # Problema detectado: los indicadores lagging (EMA stack, MACD, macro 1h)
        # dominaban la señal y producían sesgo hacia la tendencia general.
        # Solución: el desplazamiento intra-ventana y el momentum de la ventana
        # son los predictores más fuertes para ESTA ventana de 15m específica.
        # Los indicadores de tendencia bajan a rol de contexto (peso reducido).
        mom_raw     = sig["momentum"]
        ta_composite = sig["ta"]
        bb_sig_val  = sig.get("bb_sig", 0.0)
        mtf_15m     = sig.get("mtf_sig", 0.0)
        funding_15m = sig.get("funding_sig", 0.0)

        # Umbrales calibrados para ventanas de 15m.
        # BTC se mueve menos en % por minuto que en 5m, así que los umbrales
        # son menores: 0.15% en 5-6 min ya es un movimiento decisivo en 15m.
        _disp_hi_15 = 0.15   # movimiento decisivo → seguir la dirección
        _disp_lo_15 = 0.07   # movimiento moderado → momentum + TA equilibrado

        if displacement >= _disp_hi_15:
            # BTC ya se desplazó fuertemente en la ventana actual.
            # La dirección de ese movimiento es la señal primaria.
            # TA y contexto son de apoyo, no determinantes.
            combined = max(-1.0, min(1.0,
                displacement_sig * 0.30
                + mom_raw        * 0.25
                + ta_composite   * 0.22
                + mtf_15m        * 0.10
                + bb_sig_val     * 0.07
                + funding_15m    * 0.06
            ))
            mode_15m = f"displacement_follow (disp={displacement:.3f}%>={_disp_hi_15:.2f}%)"

        elif displacement >= _disp_lo_15:
            # Desplazamiento moderado: momentum y displacement guían,
            # TA aporta contexto de tendencia sin dominar.
            combined = max(-1.0, min(1.0,
                mom_raw          * 0.28
                + displacement_sig * 0.22
                + ta_composite   * 0.24
                + mtf_15m        * 0.10
                + bb_sig_val     * 0.10
                + funding_15m    * 0.06
            ))
            mode_15m = f"momentum_guided (disp={displacement:.3f}% in [{_disp_lo_15:.2f},{_disp_hi_15:.2f}]%)"

        else:
            # Desplazamiento mínimo: sin señal intra-ventana clara.
            # TA y MTF dominan, pero se penaliza la confianza final
            # porque la falta de displacement indica ventana incierta.
            combined = max(-1.0, min(1.0,
                ta_composite       * 0.32
                + mom_raw          * 0.28
                + mtf_15m          * 0.15
                + bb_sig_val       * 0.15
                + displacement_sig * 0.05
                + funding_15m      * 0.05
            ))
            combined *= 0.75   # penalizar confianza sin dirección intra-ventana
            combined = max(-1.0, min(1.0, combined))
            mode_15m = f"ta_guided_penalized (disp={displacement:.3f}%<{_disp_lo_15:.2f}%)"

        # Reaplicar penalización por TA ruidosa (pocas señales direccionales)
        # solo cuando además no hay displacement que compense.
        ta_cons = sig.get("ta_consensus", 0.5)
        if ta_cons < 0.25 and displacement < _disp_lo_15:
            combined = max(-1.0, min(1.0, combined * 0.65))

        # ── Penalización por momentum extremo (data-driven, 652 trades) ─────
        # WR por rango de |momentum|:
        #   <0.05  → WR=55.3% (mejor zona: mercado sin dirección → señal limpia)
        #   0.05-0.10 → WR=48.2%
        #   0.10-0.15 → WR=44.2% (zona conflictiva)
        #   0.15-0.20 → WR=36.4% (peor zona: movimiento a medio camino)
        #   >0.20  → WR=50.3% (breakeven: movimiento ya consumado, precio ajustado)
        # La zona 0.10-0.20 es la más peligrosa: BTC en movimiento pero sin definir.
        mom_magnitude = abs(sig.get("momentum", 0.0))
        if 0.15 <= mom_magnitude <= 0.20:
            combined = max(-1.0, min(1.0, combined * 0.50))   # -50%: peor zona (WR=36%)
        elif 0.10 <= mom_magnitude < 0.15:
            combined = max(-1.0, min(1.0, combined * 0.70))   # -30%: zona mala (WR=44%)

        sig["combined"]     = round(combined, 4)
        sig["direction"]    = "UP" if combined > 0 else ("DOWN" if combined < 0 else "NEUTRAL")
        sig["confidence"]   = round(abs(combined) * 100, 1)
        sig["15m_mode"]     = mode_15m
        sig["displacement"] = round(displacement, 4)

    if invert_signal:
        combined = -combined
        sig["combined"]  = combined
        sig["direction"] = "UP" if combined > 0 else ("DOWN" if combined < 0 else "NEUTRAL")

    # ── PASO 3a: Filtro horario Asia ─────────────────────────────────────────
    # Data histórica (652 trades): horas Asia (00-07h UTC) WR=43.2% vs
    # Americas (13-22h UTC) WR=55.8%. Los indicadores TA son menos confiables
    # con baja liquidez. Solo bloquear si la señal no es genuinamente fuerte.
    if interval_min >= 15:
        from datetime import datetime as _dt, timezone as _tz
        _hour_utc = _dt.now(_tz.utc).hour
        if 0 <= _hour_utc <= 6:
            if abs(combined) < 0.45:
                return None, (
                    f"Filtro Asia: hora {_hour_utc:02d}h UTC — WR histórico 43% en baja liquidez "
                    f"(señal {abs(combined)*100:.1f}% < 45% umbral Asia)"
                )

    # ── PASO 3b: Gate de momentum ─────────────────────────────────────────────
    # En 5m usamos mean-reversion por lo que el gate de momentum estándar no
    # aplica (ya invertimos el momentum en el combined). Solo aplicamos gate en 15m.
    #
    # RELAJACIÓN (recomendación asesor): si TA y momentum están en conflicto
    # pero la confianza combinada es >25%, permitimos la entrada. El gate solo
    # bloquea cuando la señal es débil Y el momentum va fuerte en contra.
    momentum      = sig["momentum"]

    if interval_min > 5:
        base_threshold = adaptive_params.get("momentum_gate_base", 0.20)
        mom_threshold  = adaptive_params.get("momentum_gate_threshold", base_threshold)
        # Modo estricto: el learner lo activa cuando conflicto TA/momentum pierde ≥15pp
        # En modo estricto se elimina el bypass — cualquier conflicto bloquea el trade.
        gate_strict = adaptive_params.get("momentum_gate_strict", False)
        # Umbral de confianza para bypassear el gate cuando hay conflicto TA/momentum
        # (solo aplica cuando gate_strict=False)
        # Umbral subido de 0.25 → 0.40: el gate solo se bypasea con señal
        # genuinamente fuerte. Antes, cualquier señal >25% lo bypaseaba,
        # lo que en la práctica inutilizaba el gate.
        gate_bypass_confidence = 0.40

        if combined > 0 and momentum < -mom_threshold:
            if not gate_strict and abs(combined) >= gate_bypass_confidence:
                pass  # confianza ≥40% y modo normal — continúa sin bloquear
            else:
                strict_note = " [gate estricto activo]" if gate_strict else f", conf={abs(combined)*100:.1f}%<40%"
                return None, (
                    f"Gate momentum: señal UP pero BTC bajó {sig['window_pct']:+.3f}% en la ventana "
                    f"(momentum={momentum:+.3f} < -{mom_threshold:.2f}{strict_note}) "
                    f"— inercia bajista en contra"
                )
        if combined < 0 and momentum > mom_threshold:
            if not gate_strict and abs(combined) >= gate_bypass_confidence:
                pass  # confianza ≥40% y modo normal — permitir entrada
            else:
                strict_note = " [gate estricto activo]" if gate_strict else f", conf={abs(combined)*100:.1f}%<40%"
                return None, (
                    f"Gate momentum: señal DOWN pero BTC subió {sig['window_pct']:+.3f}% en la ventana "
                    f"(momentum={momentum:+.3f} > +{mom_threshold:.2f}{strict_note}) "
                    f"— inercia alcista en contra"
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

    # ── Gate de sesgo DOWN (data-driven: 652 trades) ─────────────────────────
    # Histórico: UP=53% WR vs DOWN=43% WR en 15m (diferencia de 10pp).
    # Los indicadores TA tienen sesgo alcista estructural; las señales DOWN
    # son intrínsecamente más débiles. Requerir mayor convicción para DOWN.
    if side == "DOWN" and interval_min >= 15:
        _down_threshold = min_confidence * 1.35  # 35% más exigente que UP
        if confidence < _down_threshold:
            return None, (
                f"Gate DOWN: confianza {confidence*100:.1f}% < umbral DOWN "
                f"{_down_threshold*100:.1f}% (sesgo bajista históricamente -10pp vs UP)"
            )

    # ── Gate de entrada tardía con precio extremo ────────────────────────────
    # Si el mercado tiene <2min restantes Y el precio del lado elegido ya está
    # en zona extrema (>0.75), el mercado ya descontó el movimiento. El ratio
    # riesgo/recompensa es malo y la probabilidad de reversión aumenta.
    # Además, un precio extremo con poco tiempo sugiere que el precio BTC ya
    # se movió fuertemente; entrar aquí es perseguir el mercado.
    minutes_to_close = market.get("minutes_to_close", 999)
    if minutes_to_close < 2.0 and entry_price > 0.75:
        return None, (
            f"Entrada tardía con precio extremo: {side} cuesta ${entry_price:.3f} "
            f"con solo {minutes_to_close:.1f}min restantes — mercado ya descontó el movimiento. "
            f"Ratio riesgo/recompensa inaceptable en la recta final."
        )

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
