"""
Aprendizaje adaptativo exclusivo de trades phantom VPS.

Aprende SOLO de los resultados del experimento phantom (vps_phantom_experiment.json)
y genera recomendaciones de estrategia que NO afectan los trades reales.

Persiste en data/phantom_learner_stats.json.
"""
import json
import logging
import os
from typing import Optional

logger = logging.getLogger("weatherbot")

STATS_FILE = os.path.join("data", "phantom_learner_stats.json")

_MIN_SAMPLES      = 8    # mínimo para calcular win rate confiable
_MIN_SIDE_SAMPLES = 12   # mínimo para bloquear un lado
_RECENT_WINDOW    = 25   # ventana de trades recientes para tendencia
_MIN_CORR_TRADES  = 15   # mínimo para confiar en correlaciones de análisis


def _bkt() -> dict:
    return {"w": 0, "l": 0}


def _default_stats() -> dict:
    return {
        "5":  _default_interval_stats(),
        "15": _default_interval_stats(),
    }


def _default_interval_stats() -> dict:
    return {
        "total":   0,
        "wins":    0,
        "recent":  [],                  # últimos _RECENT_WINDOW resultados (1=win,0=loss)
        "by_tier": {                    # por tier de confianza
            "aggressive":   _bkt(),
            "high":         _bkt(),
            "moderate":     _bkt(),
            "low_moderate": _bkt(),
            "minimal":      _bkt(),
        },
        "by_side": {
            "UP":   _bkt(),
            "DOWN": _bkt(),
        },
        "by_conf_range": {              # por rango % de confianza
            "0-20":   _bkt(),
            "20-40":  _bkt(),
            "40-60":  _bkt(),
            "60-80":  _bkt(),
            "80-100": _bkt(),
        },
    }


# ── Carga / guardado ──────────────────────────────────────────────────────────

_stats: dict = {}


def _load() -> dict:
    global _stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                _stats = json.load(f)
    except Exception:
        _stats = {}
    defaults = _default_stats()
    for k in ("5", "15"):
        if k not in _stats:
            _stats[k] = defaults[k]
    return _stats


def _save() -> None:
    try:
        os.makedirs("data", exist_ok=True)
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(_stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[PhantomLearner] No se pudo guardar stats: {e}")


_load()


# ── Registro de resultado ─────────────────────────────────────────────────────

def record_result(interval_minutes: int, trade: dict, won: bool) -> None:
    """
    Registra el resultado de un trade phantom resuelto.

    trade: dict del VPS experiment con keys:
        signal, confidence_pct, confidence_tier, ta_scores
    """
    key = str(interval_minutes)
    if key not in _stats:
        _stats[key] = _default_interval_stats()
    s = _stats[key]
    w = 1 if won else 0

    s["total"] += 1
    s["wins"]  += w

    s["recent"].append(w)
    if len(s["recent"]) > _RECENT_WINDOW:
        s["recent"] = s["recent"][-_RECENT_WINDOW:]

    # Por tier
    tier = trade.get("confidence_tier", "minimal")
    if tier in s["by_tier"]:
        s["by_tier"][tier]["w" if won else "l"] += 1

    # Por lado
    side = trade.get("signal", "UP")
    if side in s["by_side"]:
        s["by_side"][side]["w" if won else "l"] += 1

    # Por rango de confianza
    conf = float(trade.get("confidence_pct") or 0)
    if conf < 20:
        rk = "0-20"
    elif conf < 40:
        rk = "20-40"
    elif conf < 60:
        rk = "40-60"
    elif conf < 80:
        rk = "60-80"
    else:
        rk = "80-100"
    s["by_conf_range"][rk]["w" if won else "l"] += 1

    _save()

    recent = s["recent"]
    wr = sum(recent) / len(recent) if recent else 0
    logger.info(
        f"[PhantomLearner] {interval_minutes}m {'WIN' if won else 'LOSS'} | "
        f"tier={tier} conf={conf:.1f}% | WR reciente {wr:.0%} ({s['wins']}/{s['total']})"
    )


# ── Análisis de patrones (correlaciones) ─────────────────────────────────────

def _get_pattern_insights(interval_minutes: int, total_trades: int) -> dict:
    """
    Llama a phantom_analysis para extraer correlaciones y convertirlas en
    recomendaciones de estrategia concretas.

    Returns dict with:
        invert_confidence – True si alta confianza correlaciona con pérdida
        strategy_hint     – "mean_reversion" | "trend_following" | "neutral"
        conf_corr         – valor de correlación confianza→resultado (float | None)
        analysis_side     – "UP" | "DOWN" | None  (desde análisis de patrones)
        pattern_insights  – lista de strings con hallazgos
    """
    result = {
        "invert_confidence": False,
        "strategy_hint":     "neutral",
        "conf_corr":         None,
        "analysis_side":     None,
        "pattern_insights":  [],
    }

    if total_trades < _MIN_CORR_TRADES:
        return result

    try:
        from phantom_analysis import analyze_phantom_trades
        analysis = analyze_phantom_trades(interval_minutes)
    except Exception as e:
        logger.debug(f"[PhantomLearner] No se pudo cargar phantom_analysis: {e}")
        return result

    if not analysis.get("ok") or analysis.get("trades_analyzed", 0) < _MIN_CORR_TRADES:
        return result

    corr_conf = analysis["correlations"].get("confidence_vs_result")
    corr_mom  = analysis["correlations"].get("momentum_vs_result")
    result["conf_corr"] = corr_conf

    # ── Correlación confianza→resultado ──────────────────────────────────────
    if corr_conf is not None:
        if corr_conf < -0.15:
            result["invert_confidence"] = True
            result["pattern_insights"].append(
                f"Confianza INVERTIDA ({corr_conf:+.3f}): alta confianza → pérdida. "
                f"Preferir señales de baja confianza mientras dure el patrón"
            )
        elif corr_conf < -0.05:
            result["pattern_insights"].append(
                f"Confianza no predictiva ({corr_conf:+.3f}): alta conf no mejora resultados"
            )
        elif corr_conf > 0.10:
            result["pattern_insights"].append(
                f"Confianza calibrada ({corr_conf:+.3f}): alta conf correlaciona con ganancia"
            )

    # ── Correlación momentum→resultado (estrategia) ─────────────────────────
    if corr_mom is not None and abs(corr_mom) > 0.12:
        if corr_mom < 0:
            result["strategy_hint"] = "mean_reversion"
            result["pattern_insights"].append(
                f"Momentum opuesto gana ({corr_mom:+.3f}) → estrategia mean-reversion es mejor"
            )
        else:
            result["strategy_hint"] = "trend_following"
            result["pattern_insights"].append(
                f"Momentum a favor gana ({corr_mom:+.3f}) → estrategia trend-following es mejor"
            )

    # ── Lado preferido desde análisis (con todos los trades del intervalo) ───
    wr_by_side = analysis.get("wr_by_side", {})
    n_up   = wr_by_side.get("n_up",   0)
    n_down = wr_by_side.get("n_down", 0)
    if n_up >= 5 and n_down >= 5:
        wr_up_pct   = wr_by_side.get("UP",   0)
        wr_down_pct = wr_by_side.get("DOWN", 0)
        if abs(wr_up_pct - wr_down_pct) >= 10:
            result["analysis_side"] = "UP" if wr_up_pct > wr_down_pct else "DOWN"
            result["pattern_insights"].append(
                f"Análisis de patrones: {result['analysis_side']} rinde mejor "
                f"(UP {wr_up_pct}% / DOWN {wr_down_pct}%)"
            )

    return result


# ── Win rate helpers ──────────────────────────────────────────────────────────

def _wr(bkt: dict, min_n: int = _MIN_SAMPLES) -> Optional[float]:
    n = bkt["w"] + bkt["l"]
    return bkt["w"] / n if n >= min_n else None


# ── Parámetros adaptativos ────────────────────────────────────────────────────

def get_adaptive_params(interval_minutes: int) -> dict:
    """
    Devuelve lo que el phantom ha aprendido para este intervalo.

    Returns:
        min_confidence_tier – tier mínimo recomendado
        min_confidence_pct  – confianza % mínima sugerida
        preferred_side      – "UP" | "DOWN" | "BOTH"
        recent_wr_pct       – win rate en los últimos _RECENT_WINDOW trades
        total_trades        – total de trades registrados
        insights            – lista de strings con hallazgos
        has_data            – True si hay suficientes datos para adaptar
    """
    key = str(interval_minutes)
    if key not in _stats:
        _load()
    s = _stats.get(key, _default_interval_stats())

    total = s["total"]
    result = {
        "interval_minutes":    interval_minutes,
        "total_trades":        total,
        "wins":                s["wins"],
        "recent_wr_pct":       0,
        "min_confidence_tier": "minimal",
        "min_confidence_pct":  0,
        "preferred_side":      "BOTH",
        "block_up":            False,
        "block_down":          False,
        "insights":            [],
        "has_data":            total >= _MIN_SAMPLES,
        # Nuevos campos de análisis de patrones
        "invert_confidence":   False,
        "strategy_hint":       "neutral",  # "mean_reversion" | "trend_following" | "neutral"
        "conf_corr":           None,
    }

    recent = s["recent"]
    if recent:
        result["recent_wr_pct"] = round(sum(recent) / len(recent) * 100, 1)

    if total < _MIN_SAMPLES:
        result["insights"].append(f"Datos insuficientes ({total}/{_MIN_SAMPLES} trades mínimos)")
        return result

    insights = []

    # ── Win rate global reciente ─────────────────────────────────────────────
    wr_recent = sum(recent) / len(recent) if recent else 0
    if wr_recent < 0.35:
        insights.append(f"WR reciente muy bajo ({wr_recent:.0%}) — estrategia actual pierde sistemáticamente")
    elif wr_recent > 0.60:
        insights.append(f"WR reciente bueno ({wr_recent:.0%}) — señales actuales funcionan bien")

    # ── Mejor tier ──────────────────────────────────────────────────────────
    best_tier = None
    best_wr   = 0.0
    tier_order = ["aggressive", "high", "moderate", "low_moderate", "minimal"]
    for tier in tier_order:
        bkt  = s["by_tier"].get(tier, _bkt())
        tier_wr = _wr(bkt)
        if tier_wr is not None:
            if tier_wr > best_wr:
                best_wr   = tier_wr
                best_tier = tier

    # Buscar el tier MÍNIMO que todavía gana (WR >= 50%)
    recommended_min_tier = "minimal"
    for tier in reversed(tier_order):   # de minimal → aggressive
        bkt = s["by_tier"].get(tier, _bkt())
        wr  = _wr(bkt)
        if wr is not None and wr < 0.48:
            # Este tier pierde — recomendar no operar bajo él
            recommended_min_tier = tier_order[max(0, tier_order.index(tier) - 1)]
            insights.append(
                f"Tier '{tier}' tiene WR {wr:.0%} — phantom recomienda mínimo '{recommended_min_tier}'"
            )
            break

    result["min_confidence_tier"] = recommended_min_tier

    # Traducir tier mínimo a % de confianza
    tier_to_pct = {"aggressive": 65, "high": 50, "moderate": 35, "low_moderate": 20, "minimal": 0}
    result["min_confidence_pct"] = tier_to_pct.get(recommended_min_tier, 0)

    # ── Lado preferido ───────────────────────────────────────────────────────
    up_bkt   = s["by_side"].get("UP",   _bkt())
    down_bkt = s["by_side"].get("DOWN", _bkt())
    wr_up    = _wr(up_bkt,   _MIN_SIDE_SAMPLES)
    wr_down  = _wr(down_bkt, _MIN_SIDE_SAMPLES)

    if wr_up is not None and wr_down is not None:
        if wr_up < 0.35 and wr_down >= 0.45:
            result["preferred_side"] = "DOWN"
            result["block_up"]       = True
            insights.append(f"UP pierde {wr_up:.0%} vs DOWN gana {wr_down:.0%} → preferir DOWN")
        elif wr_down < 0.35 and wr_up >= 0.45:
            result["preferred_side"] = "UP"
            result["block_down"]     = True
            insights.append(f"DOWN pierde {wr_down:.0%} vs UP gana {wr_up:.0%} → preferir UP")
        elif abs(wr_up - wr_down) > 0.15:
            result["preferred_side"] = "UP" if wr_up > wr_down else "DOWN"
            insights.append(
                f"Asimetría UP {wr_up:.0%} vs DOWN {wr_down:.0%} → "
                f"lado {result['preferred_side']} rinde mejor"
            )

    # ── Mejor rango de confianza ─────────────────────────────────────────────
    best_conf_range = None
    best_conf_wr    = 0.0
    for rng, bkt in s["by_conf_range"].items():
        rng_wr = _wr(bkt)
        if rng_wr is not None and rng_wr > best_conf_wr:
            best_conf_wr    = rng_wr
            best_conf_range = rng

    if best_conf_range and best_conf_wr >= 0.52:
        insights.append(
            f"Mejor rango de confianza: {best_conf_range}% → WR {best_conf_wr:.0%}"
        )

    # ── Análisis de patrones (correlaciones desde phantom_analysis) ──────────
    pattern = _get_pattern_insights(interval_minutes, total)
    result["invert_confidence"] = pattern["invert_confidence"]
    result["strategy_hint"]     = pattern["strategy_hint"]
    result["conf_corr"]         = pattern["conf_corr"]

    # Si la confianza está invertida, neutralizar el filtro por tier
    # (el filtro normal excluye baja confianza, pero aquí baja conf gana más)
    if pattern["invert_confidence"] and result["min_confidence_tier"] != "minimal":
        result["min_confidence_tier"] = "minimal"
        result["min_confidence_pct"]  = 0
        insights.append("Tier mínimo resetado a 'minimal' por confianza invertida")

    # Si el análisis de patrones sugiere un lado distinto al learner y el learner
    # no tiene suficientes datos por lado, aceptar la recomendación del análisis
    if pattern["analysis_side"] and result["preferred_side"] == "BOTH":
        result["preferred_side"] = pattern["analysis_side"]

    insights.extend(pattern["pattern_insights"])

    result["insights"] = insights
    return result


# ── Reconstruir desde historial existente ────────────────────────────────────

def rebuild_from_vps_file(vps_file: str = os.path.join("data", "vps_phantom_experiment.json")) -> int:
    """
    Reconstruye las estadísticas del learner desde el archivo VPS si el
    stats JSON no existe o está vacío. Retorna el número de trades procesados.
    """
    if not os.path.exists(vps_file):
        return 0
    try:
        with open(vps_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0

    # Reiniciar stats
    global _stats
    _stats = _default_stats()

    count = 0
    for t in data.get("trades", []):
        if t.get("result") not in ("WIN", "LOSS"):
            continue
        interval = 5 if t.get("market", "") == "updown_5m" else 15
        won      = t["result"] == "WIN"
        record_result(interval, t, won)
        count += 1

    logger.info(f"[PhantomLearner] Reconstruido desde historial VPS: {count} trades")
    return count
