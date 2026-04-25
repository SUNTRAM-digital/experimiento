"""
Experimento VPS-Confianza (Variable Position Sizing basado en Confianza)
========================================================================
Módulo AISLADO — NO ejecuta dinero real, NO modifica estrategias del bot.
Registra trades phantom paralelos y compara VPS vs Fixed $3.0 durante 7 días.

Integración: bot.py llama record_phantom_vps() al registrar phantom y
resolve_phantom_vps() cuando el mercado cierra. Nada más cambia.
"""

import json
import math
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import phantom_learner as _pl

logger = logging.getLogger("vps_experiment")

DATA_FILE = os.path.join("data", "vps_phantom_experiment.json")
FIXED_BASELINE = 3.0
PAYOUT_WIN  =  0.98   # ganancia = size * 0.98 (Polymarket estándar)
PAYOUT_LOSS = -1.00   # pérdida  = size * -1.0
EXPERIMENT_DAYS = 7

# Mapeo confianza → (size_usdc, tier_name)
# Umbrales ajustados a la distribución real de confianza del bot (típicamente 5-30%).
# El mínimo es $3 (igual al fijo) — el experimento mide cuánto MÁS
# genera apostar más en trades de alta confianza, nunca menos que la base.
_TIERS = [
    (65, 10.0, "aggressive"),   # ≥65%  → $10
    (50,  8.0, "high"),         # 50-64% → $8
    (35,  6.0, "moderate"),     # 35-49% → $6
    (20,  4.0, "low_moderate"), # 20-34% → $4
    ( 0,  3.0, "minimal"),      # <20%   → $3 (igual al fijo)
]

VIRTUAL_BALANCE_INITIAL = 50.0   # Saldo ficticio inicial para simular el experimento

# Reconstruir stats del phantom learner si no existen
try:
    if not os.path.exists(_pl.STATS_FILE):
        _pl.rebuild_from_vps_file(DATA_FILE)
except Exception:
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def calculate_vps_size(confidence_pct: float) -> tuple[float, str]:
    """Retorna (size_usdc, tier_name) según confianza."""
    for threshold, size, tier in _TIERS:
        if confidence_pct >= threshold:
            return size, tier
    return 0.5, "minimal"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load() -> dict:
    """Carga el archivo JSON del experimento. Crea estructura inicial si no existe."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Migrar datos existentes sin campo de saldo virtual
            meta = data.setdefault("meta", {})
            if "virtual_balance_vps" not in meta:
                # Recalcular desde trades resueltos
                resolved = [t for t in data.get("trades", []) if t.get("result") in ("WIN", "LOSS")]
                vps_sum   = sum(t.get("pnl_vps",   0) or 0 for t in resolved)
                fixed_sum = sum(t.get("pnl_fixed",  0) or 0 for t in resolved)
                meta["virtual_balance_vps"]   = round(VIRTUAL_BALANCE_INITIAL + vps_sum,   4)
                meta["virtual_balance_fixed"] = round(VIRTUAL_BALANCE_INITIAL + fixed_sum, 4)
                _save(data)
            return data
        except Exception as e:
            logger.warning(f"[VPS] Error leyendo {DATA_FILE}: {e}")

    # Estructura inicial
    now = _now_utc()
    end_dt = (datetime.now(timezone.utc) + timedelta(days=EXPERIMENT_DAYS))
    end_str = end_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    data = {
        "meta": {
            "experiment_name": "VPS-Confianza Phantom UpDown",
            "version": "1.1",
            "started": now,
            "end_target": end_str,
            "duration_hours": EXPERIMENT_DAYS * 24,
            "status": "RUNNING",
            "virtual_balance_vps":   VIRTUAL_BALANCE_INITIAL,
            "virtual_balance_fixed": VIRTUAL_BALANCE_INITIAL,
        },
        "config": {
            "scope": ["updown_15m", "updown_5m"],
            "mode": "phantom_only",
            "money_real": False,
            "max_position_usdc": 10.0,
            "baseline_fixed_usdc": FIXED_BASELINE,
            "virtual_balance_initial": VIRTUAL_BALANCE_INITIAL,
            "confidence_tiers": {
                "aggressive":   {"min": 65, "max": 100, "size": 10.0},
                "high":         {"min": 50, "max":  64, "size":  8.0},
                "moderate":     {"min": 35, "max":  49, "size":  6.0},
                "low_moderate": {"min": 20, "max":  34, "size":  4.0},
                "minimal":      {"min":  0, "max":  19, "size":  3.0},
            },
        },
        "trades": [],
        "daily_summaries": [],
        "final_analysis": None,
    }
    _save(data)
    return data


def _save(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[VPS] Error guardando {DATA_FILE}: {e}")


def reset_with_balance(new_balance: float) -> dict:
    """Reset total: trades=[], daily_summaries=[], saldos virtuales = new_balance,
    started=ahora. Se conserva la estructura de config (tiers, etc.).
    Devuelve el dict resultante."""
    if new_balance is None or new_balance < 0:
        raise ValueError("new_balance debe ser >= 0")
    bal = round(float(new_balance), 4)
    if os.path.exists(DATA_FILE):
        try:
            os.remove(DATA_FILE)
        except Exception as e:
            logger.warning(f"[VPS] No se pudo borrar {DATA_FILE}: {e}")
    data = _load()  # crea estructura fresca con VIRTUAL_BALANCE_INITIAL
    data["meta"]["virtual_balance_vps"]   = bal
    data["meta"]["virtual_balance_fixed"] = bal
    data["meta"]["virtual_balance_initial_custom"] = bal
    data["config"]["virtual_balance_initial"] = bal
    _save(data)
    return data


# ── API pública ───────────────────────────────────────────────────────────────

def record_phantom_vps(
    slug: str,
    interval: int,
    side: str,
    confidence_pct: float,
    btc_start: float,
    end_ts: int,
    ta_scores: Optional[dict] = None,
    entry_price: float = 0.50,
    used_real_money: bool = False,
    up_token: Optional[str] = None,
    down_token: Optional[str] = None,
    btc_price_to_beat: Optional[float] = None,
) -> None:
    """
    Registra un trade phantom en el experimento VPS.
    Llamar desde bot.py justo cuando se registra el phantom.
    No toca nada del trading real.
    """
    data = _load()

    # Evitar duplicados por slug
    if any(t.get("slug") == slug for t in data["trades"]):
        return

    # Verificar si el experimento sigue activo
    meta = data.get("meta", {})
    if meta.get("status") != "RUNNING":
        return
    try:
        end_target = datetime.fromisoformat(meta["end_target"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > end_target:
            meta["status"] = "COMPLETED"
            _save(data)
            return
    except Exception:
        pass

    size_vps, tier = calculate_vps_size(confidence_pct)
    trade_id = len(data["trades"]) + 1
    market_key = f"updown_{interval}m"

    trade = {
        "trade_id":            trade_id,
        "slug":                slug,
        "timestamp":           _now_utc(),
        "market":              market_key,
        "signal":              side,
        "confidence_pct":      round(confidence_pct, 2),
        "confidence_tier":     tier,
        "entry_price":         round(entry_price, 4),
        "position_size_vps":   size_vps,
        "position_size_fixed": FIXED_BASELINE,
        "ta_scores":           ta_scores or {},
        "btc_start_price":     round(btc_start, 2),
        "end_ts":              end_ts,
        "result":              "PENDING",
        "result_timestamp":    None,
        "btc_end_price":       None,
        "pnl_vps":             None,
        "pnl_fixed":           None,
        "pnl_difference":      None,
        "used_real_money":     used_real_money,
        "up_token":            up_token,
        "down_token":          down_token,
        # Precios Chainlink (Polymarket oracle) — más precisos que Binance
        "btc_price_to_beat":   round(btc_price_to_beat, 2) if btc_price_to_beat else None,
        "btc_final_price":     None,  # se completa al resolver
    }

    data["trades"].append(trade)
    _save(data)

    logger.info(
        f"[VPS] Trade #{trade_id} registrado — {market_key} {side} "
        f"confianza={confidence_pct:.1f}% tier={tier} size_vps=${size_vps} fixed=${FIXED_BASELINE}"
    )


def get_pending_for_restore() -> dict:
    """
    Retorna los trades PENDING del JSON en el formato de _updown_phantom_pending.
    Llamar al arrancar bot.py para restaurar trades que sobrevivieron un reinicio.
    Solo incluye trades cuyo end_ts es reciente (< 2 horas en el pasado);
    los más viejos los maneja sweep_stale_pending().
    """
    data = _load()
    now_ts = datetime.now(timezone.utc).timestamp()
    result = {}
    for t in data.get("trades", []):
        if t.get("result") != "PENDING":
            continue
        slug = t.get("slug")
        end_ts = t.get("end_ts", 0)
        if not slug or not end_ts:
            continue
        # Solo restaurar trades recientes (end_ts < 2h atrás)
        # Los muy viejos los resuelve sweep_stale_pending() con precio actual
        if now_ts > end_ts + 7200:
            continue
        interval = 5 if t.get("market") == "updown_5m" else 15
        result[slug] = {
            "interval":        interval,
            "side":            t.get("signal", "UP"),
            "btc_start":       t.get("btc_start_price", 0.0),
            "end_ts":          end_ts,
            "slug":            slug,
            "skip_reason":     t.get("ta_scores", {}).get("skip_reason", "restored_after_restart"),
            "confidence":      t.get("confidence_pct", 0.0),
            "combined_signal": 0.0,
            "ta_signal":       0.0,
            "ta_rsi":          None,
            "window_momentum": 0.0,
            "elapsed_minutes": 0,
            "up_token":        t.get("up_token"),
            "down_token":      t.get("down_token"),
        }
    return result


def get_stale_pending() -> list[dict]:
    """
    Retorna trades PENDING cuyo end_ts fue hace más de 2 horas.
    Llamar desde bot.py para resolverlos con precio actual como fallback.
    """
    data = _load()
    now_ts = datetime.now(timezone.utc).timestamp()
    return [
        t for t in data.get("trades", [])
        if t.get("result") == "PENDING"
        and t.get("end_ts", 0)
        and now_ts > t["end_ts"] + 7200
    ]


def resolve_phantom_vps(slug: str, btc_end: float, won: bool, btc_final_price: Optional[float] = None, btc_price_to_beat: Optional[float] = None) -> None:
    """
    Resuelve un trade phantom con el resultado real.
    Llamar desde bot.py cuando el mercado phantom cierra.
    """
    data = _load()

    trade = next((t for t in data["trades"] if t.get("slug") == slug), None)
    if trade is None or trade.get("result") != "PENDING":
        return

    result_str = "WIN" if won else "LOSS"
    payout = PAYOUT_WIN if won else PAYOUT_LOSS

    pnl_vps   = round(trade["position_size_vps"]   * payout, 4)
    pnl_fixed = round(trade["position_size_fixed"]  * payout, 4)

    trade["result"]           = result_str
    trade["result_timestamp"] = _now_utc()
    trade["btc_end_price"]    = round(btc_end, 2)
    if btc_final_price is not None:
        trade["btc_final_price"]   = round(btc_final_price,   2)
    if btc_price_to_beat is not None:
        trade["btc_price_to_beat"] = round(btc_price_to_beat, 2)
    trade["pnl_vps"]          = pnl_vps
    trade["pnl_fixed"]        = pnl_fixed
    trade["pnl_difference"]   = round(pnl_vps - pnl_fixed, 4)

    # Actualizar saldo virtual
    meta = data.setdefault("meta", {})
    meta["virtual_balance_vps"]   = round(
        meta.get("virtual_balance_vps",   VIRTUAL_BALANCE_INITIAL) + pnl_vps,   4
    )
    meta["virtual_balance_fixed"] = round(
        meta.get("virtual_balance_fixed", VIRTUAL_BALANCE_INITIAL) + pnl_fixed, 4
    )

    _save(data)

    logger.info(
        f"[VPS] Trade resuelto — slug={slug[:30]} {result_str} "
        f"pnl_vps={pnl_vps:+.2f} pnl_fixed={pnl_fixed:+.2f} Δ={trade['pnl_difference']:+.2f}"
    )

    # Registrar en phantom learner para aprendizaje adaptativo
    try:
        interval = 5 if trade.get("market", "") == "updown_5m" else 15
        _pl.record_result(interval, trade, won)
    except Exception as e:
        logger.warning(f"[VPS] phantom_learner.record_result error: {e}")

    # Intentar generar resumen del día automáticamente
    try:
        _maybe_generate_daily_summary(data)
    except Exception as e:
        logger.warning(f"[VPS] Error generando daily summary: {e}")


def get_status() -> dict:
    """Retorna el estado actual del experimento con métricas calculadas en vivo."""
    data = _load()
    trades = data.get("trades", [])
    meta   = data.get("meta", {})

    resolved = [t for t in trades if t.get("result") in ("WIN", "LOSS")]
    pending  = [t for t in trades if t.get("result") == "PENDING"]

    stats = _calc_stats(resolved)
    tier_breakdown = _calc_tier_breakdown(resolved)

    # Días transcurridos
    days_elapsed = 0
    try:
        started = datetime.fromisoformat(meta["started"].replace("Z", "+00:00"))
        days_elapsed = (datetime.now(timezone.utc) - started).days
    except Exception:
        pass

    # Desglose por mercado (5m vs 15m)
    market_breakdown = {}
    for mkt_key in ("updown_5m", "updown_15m"):
        mkt_resolved = [t for t in resolved if t.get("market") == mkt_key]
        mkt_pending  = [t for t in pending  if t.get("market") == mkt_key]
        mkt_stats    = _calc_stats(mkt_resolved)
        market_breakdown[mkt_key] = {
            "resolved": len(mkt_resolved),
            "pending":  len(mkt_pending),
            "vps":      mkt_stats["vps"],
            "fixed":    mkt_stats["fixed"],
        }

    # Parámetros adaptativos del phantom learner
    try:
        phantom_learn_5m  = _pl.get_adaptive_params(5)
        phantom_learn_15m = _pl.get_adaptive_params(15)
    except Exception:
        phantom_learn_5m  = {}
        phantom_learn_15m = {}

    return {
        "status":        meta.get("status", "UNKNOWN"),
        "started":       meta.get("started"),
        "end_target":    meta.get("end_target"),
        "days_elapsed":  days_elapsed,
        "days_total":    EXPERIMENT_DAYS,
        "total_trades":  len(trades),
        "resolved":      len(resolved),
        "pending":       len(pending),
        "virtual_balance_vps":   round(meta.get("virtual_balance_vps",   VIRTUAL_BALANCE_INITIAL), 2),
        "virtual_balance_fixed": round(meta.get("virtual_balance_fixed", VIRTUAL_BALANCE_INITIAL), 2),
        "virtual_balance_initial": round(
            meta.get("virtual_balance_initial_custom",
                     data.get("config", {}).get("virtual_balance_initial", VIRTUAL_BALANCE_INITIAL)),
            2,
        ),
        "vps":           stats["vps"],
        "fixed":         stats["fixed"],
        "comparison":    stats["comparison"],
        "tier_breakdown":   tier_breakdown,
        "market_breakdown": market_breakdown,
        "recent_trades": trades[-10:][::-1],  # últimos 10, más reciente primero
        "daily_summaries": data.get("daily_summaries", []),
        "phantom_learn_5m":  phantom_learn_5m,
        "phantom_learn_15m": phantom_learn_15m,
    }


# ── Cálculos estadísticos ─────────────────────────────────────────────────────

def _calc_stats(resolved: list[dict]) -> dict:
    """Calcula métricas completas para VPS y Fixed a partir de trades resueltos."""
    if not resolved:
        empty = {"total_trades":0,"wins":0,"losses":0,"win_rate_pct":0,
                 "total_risked":0,"net_pnl":0,"roi_pct":0,
                 "avg_position_size":0,"sharpe_ratio":0,"profit_factor":0,"expectancy":0}
        return {"vps": empty, "fixed": empty, "comparison": {"vps_superior": None, "pnl_difference": 0}}

    wins   = [t for t in resolved if t["result"] == "WIN"]
    losses = [t for t in resolved if t["result"] == "LOSS"]
    wr     = len(wins) / len(resolved)

    def _metrics(pnl_key: str, size_key: str) -> dict:
        pnls         = [t[pnl_key] for t in resolved if t.get(pnl_key) is not None]
        sizes        = [t[size_key] for t in resolved]
        win_pnls     = [t[pnl_key] for t in wins  if t.get(pnl_key) is not None]
        loss_pnls    = [t[pnl_key] for t in losses if t.get(pnl_key) is not None]
        total_risked = sum(sizes)
        net_pnl      = sum(pnls)
        avg_win      = (sum(win_pnls)  / len(win_pnls))  if win_pnls  else 0
        avg_loss     = abs(sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0
        # Sharpe (simplificado: mean/std)
        if len(pnls) >= 2:
            mean_p = net_pnl / len(pnls)
            std_p  = math.sqrt(sum((p - mean_p)**2 for p in pnls) / len(pnls))
            sharpe = round(mean_p / std_p, 3) if std_p > 0 else 0
        else:
            sharpe = 0
        profit_factor = round(abs(sum(win_pnls)) / abs(sum(loss_pnls)), 3) if loss_pnls and sum(loss_pnls) != 0 else 0
        expectancy    = round(wr * avg_win - (1 - wr) * avg_loss, 4)
        return {
            "total_trades":     len(resolved),
            "wins":             len(wins),
            "losses":           len(losses),
            "win_rate_pct":     round(wr * 100, 1),
            "total_risked":     round(total_risked, 2),
            "net_pnl":          round(net_pnl, 2),
            "roi_pct":          round(net_pnl / total_risked * 100, 1) if total_risked else 0,
            "avg_position_size": round(sum(sizes) / len(sizes), 2),
            "sharpe_ratio":     sharpe,
            "profit_factor":    profit_factor,
            "expectancy":       expectancy,
        }

    vps_m   = _metrics("pnl_vps",   "position_size_vps")
    fixed_m = _metrics("pnl_fixed", "position_size_fixed")
    diff    = round(vps_m["net_pnl"] - fixed_m["net_pnl"], 2)
    diff_pct = round(diff / abs(fixed_m["net_pnl"]) * 100, 1) if fixed_m["net_pnl"] != 0 else 0

    # Correlación Pearson: confidence_pct vs resultado (1=WIN, 0=LOSS)
    confs   = [t["confidence_pct"] for t in resolved]
    results = [1 if t["result"] == "WIN" else 0 for t in resolved]
    corr    = _pearson(confs, results)

    return {
        "vps":   vps_m,
        "fixed": fixed_m,
        "comparison": {
            "pnl_difference":     diff,
            "pnl_difference_pct": diff_pct,
            "roi_difference":     round(vps_m["roi_pct"] - fixed_m["roi_pct"], 1),
            "sharpe_difference":  round(vps_m["sharpe_ratio"] - fixed_m["sharpe_ratio"], 3),
            "vps_superior":       vps_m["net_pnl"] > fixed_m["net_pnl"],
            "correlation_conf_result": corr,
        },
    }


def _calc_tier_breakdown(resolved: list[dict]) -> dict:
    tiers = ["aggressive","high","moderate","low_moderate","minimal"]
    breakdown = {}
    for tier in tiers:
        t_trades = [t for t in resolved if t.get("confidence_tier") == tier]
        if not t_trades:
            breakdown[tier] = {"trades": 0, "wins": 0, "wr_pct": 0, "pnl_vps": 0, "pnl_fixed": 0}
            continue
        wins = [t for t in t_trades if t["result"] == "WIN"]
        breakdown[tier] = {
            "trades":    len(t_trades),
            "wins":      len(wins),
            "wr_pct":    round(len(wins) / len(t_trades) * 100, 1),
            "pnl_vps":   round(sum(t.get("pnl_vps",   0) or 0 for t in t_trades), 2),
            "pnl_fixed": round(sum(t.get("pnl_fixed",  0) or 0 for t in t_trades), 2),
            "avg_size_vps": t_trades[0]["position_size_vps"] if t_trades else 0,
        }
    return breakdown


def _pearson(xs: list, ys: list) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num  = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den  = math.sqrt(sum((x - mx)**2 for x in xs) * sum((y - my)**2 for y in ys))
    return round(num / den, 3) if den > 0 else 0.0


def _maybe_generate_daily_summary(data: dict) -> None:
    """Genera resumen diario si hoy aún no tiene uno."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summaries = data.get("daily_summaries", [])
    if any(s.get("summary_date") == today for s in summaries):
        return

    # Solo generar si hay al menos 5 trades resueltos hoy
    today_resolved = [
        t for t in data["trades"]
        if t.get("result") in ("WIN","LOSS")
        and (t.get("result_timestamp") or "").startswith(today)
    ]
    if len(today_resolved) < 5:
        return

    try:
        started = datetime.fromisoformat(data["meta"]["started"].replace("Z", "+00:00"))
        day_num = (datetime.now(timezone.utc) - started).days + 1
    except Exception:
        day_num = len(summaries) + 1

    stats = _calc_stats(today_resolved)
    summary = {
        "summary_date": today,
        "day_number":   day_num,
        "trades_today": len(today_resolved),
        "vps_results":   stats["vps"],
        "fixed_results": stats["fixed"],
        "comparison":    stats["comparison"],
        "tier_breakdown": _calc_tier_breakdown(today_resolved),
    }
    data["daily_summaries"].append(summary)
    _save(data)
    logger.info(f"[VPS] Resumen día {day_num} generado — {len(today_resolved)} trades")


def force_daily_summary() -> dict:
    """Fuerza generación de resumen del día actual (para reportes manuales)."""
    data = _load()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Remover resumen del día si ya existe para regenerar
    data["daily_summaries"] = [s for s in data.get("daily_summaries", []) if s.get("summary_date") != today]

    today_resolved = [
        t for t in data["trades"]
        if t.get("result") in ("WIN","LOSS")
        and (t.get("result_timestamp") or "").startswith(today)
    ]
    if not today_resolved:
        return {"ok": False, "msg": "Sin trades resueltos hoy"}

    try:
        started = datetime.fromisoformat(data["meta"]["started"].replace("Z", "+00:00"))
        day_num = (datetime.now(timezone.utc) - started).days + 1
    except Exception:
        day_num = len(data.get("daily_summaries", [])) + 1

    stats = _calc_stats(today_resolved)
    summary = {
        "summary_date": today,
        "day_number":   day_num,
        "trades_today": len(today_resolved),
        "vps_results":   stats["vps"],
        "fixed_results": stats["fixed"],
        "comparison":    stats["comparison"],
        "tier_breakdown": _calc_tier_breakdown(today_resolved),
    }
    data["daily_summaries"].append(summary)
    _save(data)
    return {"ok": True, "summary": summary}
