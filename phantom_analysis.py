"""
Análisis de trades phantom UpDown — busca patrones que expliquen
por qué trades de alta confianza pierden y trades de baja confianza ganan.

Módulo de solo lectura: no modifica trades, parámetros ni estrategias.
Lee de updown_learner_data.json y vps_phantom_experiment.json.
"""
import json
import math
import os
from datetime import datetime, timezone
from typing import Optional

LEARNER_FILE = os.path.join("data", "updown_learner_data.json")
VPS_FILE     = os.path.join("data", "vps_phantom_experiment.json")

# ── Carga de datos ─────────────────────────────────────────────────────────────

def _load_phantom_trades() -> list[dict]:
    """Carga todos los trades phantom resueltos de las dos fuentes."""
    trades = []

    # Fuente 1: VPS experiment (tiene confianza, tier, señales TA, resultado)
    if os.path.exists(VPS_FILE):
        try:
            with open(VPS_FILE, "r", encoding="utf-8") as f:
                vps_data = json.load(f)
            for t in vps_data.get("trades", []):
                if t.get("result") in ("WIN", "LOSS"):
                    trades.append({
                        "source":      "vps",
                        "slug":        t.get("slug", ""),
                        "market":      t.get("market", ""),
                        "side":        t.get("signal", ""),
                        "confidence":  t.get("confidence_pct", 0),
                        "tier":        t.get("confidence_tier", ""),
                        "result":      t.get("result"),
                        "pnl_vps":     t.get("pnl_vps", 0),
                        "pnl_fixed":   t.get("pnl_fixed", 0),
                        "entry_price": t.get("entry_price", 0.5),
                        "btc_start":   t.get("btc_start_price", 0),
                        "btc_end":     t.get("btc_end_price", 0),
                        "ta_combined": (t.get("ta_scores") or {}).get("combined", 0),
                        "ta_rsi":      (t.get("ta_scores") or {}).get("rsi"),
                        "ta_momentum": (t.get("ta_scores") or {}).get("momentum", 0),
                        "ta_ofi":      (t.get("ta_scores") or {}).get("ofi", 0),
                        "timestamp":   t.get("timestamp", ""),
                    })
        except Exception:
            pass

    return trades


# ── Análisis estadístico ───────────────────────────────────────────────────────

def _pearson(xs: list, ys: list) -> float:
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = math.sqrt(sum((x - mx)**2 for x in xs) * sum((y - my)**2 for y in ys))
    return round(num / den, 3) if den > 0 else 0.0


def _bucket_analysis(trades: list[dict], key: str, n_buckets: int = 5) -> list[dict]:
    """Divide trades en N cuartiles por la variable `key` y calcula WR por bucket."""
    values = [t.get(key) for t in trades if t.get(key) is not None]
    if not values or len(values) < n_buckets:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return []
    step = (hi - lo) / n_buckets
    buckets = []
    for i in range(n_buckets):
        blo = lo + i * step
        bhi = blo + step if i < n_buckets - 1 else hi + 0.001
        bucket_trades = [t for t in trades if t.get(key) is not None and blo <= t[key] < bhi]
        if not bucket_trades:
            continue
        wins = [t for t in bucket_trades if t["result"] == "WIN"]
        buckets.append({
            "range_lo":  round(blo, 2),
            "range_hi":  round(bhi, 2),
            "n":         len(bucket_trades),
            "wins":      len(wins),
            "wr_pct":    round(len(wins) / len(bucket_trades) * 100, 1),
        })
    return buckets


def analyze_phantom_trades(interval: Optional[int] = None) -> dict:
    """
    Analiza todos los trades phantom resueltos buscando:
    - Correlación entre confianza y resultado
    - WR por rango de confianza (buckets)
    - WR por RSI, momentum, OFI
    - Insights sobre por qué alta confianza pierde
    """
    trades = _load_phantom_trades()

    if interval:
        key = f"updown_{interval}m"
        trades = [t for t in trades if t.get("market") == key]

    if not trades:
        return {"ok": False, "msg": "Sin trades phantom resueltos aún", "trades_analyzed": 0}

    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    wr     = round(len(wins) / len(trades) * 100, 1)

    # Correlaciones
    confs   = [t["confidence"] for t in trades]
    results = [1 if t["result"] == "WIN" else 0 for t in trades]
    corr_conf = _pearson(confs, results)

    moms   = [t["ta_momentum"] for t in trades if t["ta_momentum"] is not None]
    mom_res = [1 if t["result"] == "WIN" else 0 for t in trades if t["ta_momentum"] is not None]
    corr_mom = _pearson(moms, mom_res)

    ta_cs  = [t["ta_combined"] for t in trades if t["ta_combined"] is not None]
    ta_res = [1 if t["result"] == "WIN" else 0 for t in trades if t["ta_combined"] is not None]
    corr_ta = _pearson(ta_cs, ta_res)

    # Buckets por confianza
    conf_buckets = _bucket_analysis(trades, "confidence", n_buckets=5)

    # Alta vs baja confianza
    median_conf = sorted(confs)[len(confs) // 2] if confs else 50
    high_conf = [t for t in trades if t["confidence"] >= median_conf]
    low_conf  = [t for t in trades if t["confidence"] <  median_conf]
    wr_high = round(sum(1 for t in high_conf if t["result"]=="WIN") / len(high_conf) * 100, 1) if high_conf else 0
    wr_low  = round(sum(1 for t in low_conf  if t["result"]=="WIN") / len(low_conf)  * 100, 1) if low_conf  else 0

    # Side analysis (UP vs DOWN)
    up_trades   = [t for t in trades if t["side"] == "UP"]
    down_trades = [t for t in trades if t["side"] == "DOWN"]
    wr_up   = round(sum(1 for t in up_trades   if t["result"]=="WIN") / len(up_trades)   * 100, 1) if up_trades   else 0
    wr_down = round(sum(1 for t in down_trades if t["result"]=="WIN") / len(down_trades) * 100, 1) if down_trades else 0

    # Market split
    mkt_5m  = [t for t in trades if t.get("market") == "updown_5m"]
    mkt_15m = [t for t in trades if t.get("market") == "updown_15m"]
    wr_5m  = round(sum(1 for t in mkt_5m  if t["result"]=="WIN") / len(mkt_5m)  * 100, 1) if mkt_5m  else 0
    wr_15m = round(sum(1 for t in mkt_15m if t["result"]=="WIN") / len(mkt_15m) * 100, 1) if mkt_15m else 0

    # Generar insights
    insights = []

    if corr_conf < -0.05:
        insights.append({
            "severity": "WARN",
            "finding":  f"Correlación confianza→resultado NEGATIVA ({corr_conf:+.3f}): "
                        f"alta confianza correlaciona con PÉRDIDA. El modelo de confianza puede estar sobreajustado o invertido.",
        })
    elif corr_conf < 0.10:
        insights.append({
            "severity": "INFO",
            "finding":  f"Correlación confianza→resultado débil ({corr_conf:+.3f}): "
                        f"la confianza actual NO predice bien el resultado.",
        })
    else:
        insights.append({
            "severity": "OK",
            "finding":  f"Correlación confianza→resultado positiva ({corr_conf:+.3f}): "
                        f"alta confianza correlaciona con GANANCIA.",
        })

    if wr_high < wr_low - 5:
        insights.append({
            "severity": "WARN",
            "finding":  f"Alta confianza gana {wr_high}% vs baja confianza {wr_low}% "
                        f"(umbral mediana={median_conf:.1f}%). "
                        f"Los trades 'seguros' pierden más. Posible sobreconfianza en señales débiles amplificadas.",
        })

    if abs(wr_up - wr_down) > 10 and min(len(up_trades), len(down_trades)) >= 5:
        dominant = "UP" if wr_up > wr_down else "DOWN"
        insights.append({
            "severity": "INFO",
            "finding":  f"Asimetría UP/DOWN: UP={wr_up}% ({len(up_trades)} trades) vs DOWN={wr_down}% ({len(down_trades)} trades). "
                        f"El lado {dominant} está funcionando mejor — considerar sesgo estructural.",
        })

    if corr_mom and abs(corr_mom) > 0.15:
        dir_word = "ALINEADO" if corr_mom > 0 else "CONTRARIO"
        insights.append({
            "severity": "INFO",
            "finding":  f"Momentum correlaciona {dir_word} con victorias ({corr_mom:+.3f}). "
                        f"{'Momentum a favor mejora resultados.' if corr_mom > 0 else 'Momentum en contra mejora resultados (mean-reversion).'}",
        })

    return {
        "ok":              True,
        "trades_analyzed": len(trades),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate_pct":    wr,
        "median_confidence": round(median_conf, 1),
        "correlations": {
            "confidence_vs_result": corr_conf,
            "momentum_vs_result":   corr_mom,
            "ta_combined_vs_result": corr_ta,
        },
        "confidence_buckets": conf_buckets,
        "wr_by_side": {"UP": wr_up, "DOWN": wr_down, "n_up": len(up_trades), "n_down": len(down_trades)},
        "wr_by_market": {"updown_5m": wr_5m, "updown_15m": wr_15m, "n_5m": len(mkt_5m), "n_15m": len(mkt_15m)},
        "high_conf_wr": wr_high,
        "low_conf_wr":  wr_low,
        "insights":     insights,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    }
