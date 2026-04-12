"""
Aprendizaje adaptativo para mercados BTC UpDown (5m y 15m).

Registra resultados por buckets de características (señal TA, RSI, timing, lado)
y ajusta dinámicamente los parámetros de la estrategia según el rendimiento histórico.

Persiste estadísticas en data/updown_stats.json para sobrevivir reinicios.
"""
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("weatherbot")

STATS_FILE = Path(__file__).parent / "data" / "updown_stats.json"

_RECENT_WINDOW = 30   # trades recientes para win-rate dinámico
_MIN_SAMPLES   = 5    # mínimo de muestras para ajustar un parámetro


# ── Estructura de estadísticas ────────────────────────────────────────────────

def _bucket():
    return {"w": 0, "l": 0}


def _default_phantom_stats() -> dict:
    """Stats para apuestas fantasma (no ejecutadas) — rastrear oportunidades perdidas."""
    return {
        "total": 0,
        "wins": 0,
        "recent": [],
        "by_signal": {"weak": _bucket(), "med": _bucket(), "strong": _bucket()},
        "by_side":   {"UP": _bucket(), "DOWN": _bucket()},
        "by_elapsed": {"early": _bucket(), "mid": _bucket(), "late": _bucket()},
        # Agrupado por motivo de no-entrada
        "by_skip_reason": {},
    }


def _default_stats() -> dict:
    return {
        "total": 0,
        "wins": 0,
        # Últimos _RECENT_WINDOW resultados (1=win, 0=loss)
        "recent": [],
        # Por fuerza de señal combinada
        "by_signal": {
            "weak":   _bucket(),   # 0.10 – 0.25
            "med":    _bucket(),   # 0.25 – 0.50
            "strong": _bucket(),   # 0.50+
        },
        # Por RSI al momento de entrada
        "by_rsi": {
            "low":  _bucket(),   # RSI < 40
            "mid":  _bucket(),   # 40 – 60
            "high": _bucket(),   # > 60
        },
        # Por minuto transcurrido en la ventana al entrar
        "by_elapsed": {
            "early": _bucket(),   # < 1.5 min
            "mid":   _bucket(),   # 1.5 – 3 min
            "late":  _bucket(),   # > 3 min
        },
        # Por lado operado
        "by_side": {
            "UP":   _bucket(),
            "DOWN": _bucket(),
        },
        # TA vs Momentum: ¿estaban de acuerdo o en conflicto?
        "by_ta_mom": {
            "agree":    _bucket(),  # TA y momentum apuntan al mismo lado
            "conflict": _bucket(),  # TA y momentum apuntan en sentidos opuestos
        },
        # Por fuerza del momentum intra-ventana
        "by_momentum": {
            "strong_agree":    _bucket(),  # momentum fuerte a favor (>0.15)
            "weak":            _bucket(),  # momentum débil (abs <= 0.15)
            "strong_conflict": _bucket(),  # momentum fuerte en contra (>0.15)
        },
        # Calibración TA: ¿cuántas veces la dirección TA fue correcta?
        "dir_correct": 0,
        "dir_total":   0,
        # Apuestas fantasma (no ejecutadas)
        "phantom": _default_phantom_stats(),
    }


# ── Estado interno ────────────────────────────────────────────────────────────

_stats: dict = {}   # "5" → stats_dict, "15" → stats_dict


def _load():
    global _stats
    try:
        if STATS_FILE.exists():
            _stats = json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        _stats = {}
    for key in ("5", "15"):
        if key not in _stats:
            _stats[key] = _default_stats()


def _save():
    try:
        STATS_FILE.parent.mkdir(exist_ok=True)
        STATS_FILE.write_text(
            json.dumps(_stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"UpDown learner: no se pudo guardar stats: {e}")


_load()


# ── API pública ───────────────────────────────────────────────────────────────

def record_result(interval_minutes: int, trade: dict, won: bool):
    """
    Registra el resultado de un trade UpDown completado.

    trade: dict guardado en updown_recent_trades (side, ta_signal, ta_rsi,
           combined_signal, elapsed_minutes, btc_price_window_start, btc_price).
    won:   True si BTC se movió en la dirección que apostamos.
    """
    key = str(interval_minutes)
    if key not in _stats:
        _stats[key] = _default_stats()
    s = _stats[key]
    w = 1 if won else 0

    s["total"] += w + (1 - w)   # siempre +1
    s["wins"]  += w

    # Reciente (ventana deslizante)
    s["recent"].append(w)
    if len(s["recent"]) > _RECENT_WINDOW:
        s["recent"] = s["recent"][-_RECENT_WINDOW:]

    # ── Por fuerza de señal ──────────────────────────────────────────────────
    # combined_signal en rango -1..+1; confidence es abs(combined)*100
    confidence_pct = trade.get("confidence")  # 0-100 (nuevo campo)
    if confidence_pct is not None:
        sig_abs = confidence_pct / 100.0
    else:
        sig_abs = abs(
            trade.get("combined_signal")
            or trade.get("ta_signal")
            or 0.0
        )
    sig_key = "weak" if sig_abs < 0.25 else ("med" if sig_abs < 0.50 else "strong")
    s["by_signal"][sig_key]["w" if won else "l"] += 1

    # ── Por RSI ──────────────────────────────────────────────────────────────
    rsi = trade.get("ta_rsi")
    if rsi is not None:
        rsi_key = "low" if rsi < 40 else ("high" if rsi > 60 else "mid")
        s["by_rsi"][rsi_key]["w" if won else "l"] += 1

    # ── Por timing ───────────────────────────────────────────────────────────
    elapsed = trade.get("elapsed_minutes") or 0.0
    el_key = "early" if elapsed < 1.5 else ("mid" if elapsed < 3.0 else "late")
    s["by_elapsed"][el_key]["w" if won else "l"] += 1

    # ── Por lado ─────────────────────────────────────────────────────────────
    side = trade.get("side", "UP")
    if side in s["by_side"]:
        s["by_side"][side]["w" if won else "l"] += 1

    # ── TA vs Momentum ────────────────────────────────────────────────────────
    momentum      = trade.get("window_momentum") or 0.0
    combined_sig  = trade.get("combined_signal") or 0.0
    ta_dir_up     = combined_sig > 0  # dirección que eligió la señal
    mom_dir_up    = momentum > 0

    # Acuerdo: TA y momentum van en el mismo sentido
    if "by_ta_mom" not in s:
        s["by_ta_mom"] = {"agree": _bucket(), "conflict": _bucket()}
    ta_mom_key = "agree" if (ta_dir_up == mom_dir_up) else "conflict"
    s["by_ta_mom"][ta_mom_key]["w" if won else "l"] += 1

    # Fuerza del momentum vs dirección elegida
    if "by_momentum" not in s:
        s["by_momentum"] = {"strong_agree": _bucket(), "weak": _bucket(), "strong_conflict": _bucket()}
    mom_strong = abs(momentum) > 0.15
    if not mom_strong:
        mom_key = "weak"
    elif ta_dir_up == mom_dir_up:
        mom_key = "strong_agree"
    else:
        mom_key = "strong_conflict"
    s["by_momentum"][mom_key]["w" if won else "l"] += 1

    # ── Calibración dirección TA ─────────────────────────────────────────────
    ta_signal_raw = trade.get("ta_signal") or 0.0
    predicted_up  = ta_signal_raw > 0
    actual_up     = won if side == "UP" else not won
    s["dir_total"] += 1
    if predicted_up == actual_up:
        s["dir_correct"] += 1

    _save()

    recent = s["recent"]
    wr = sum(recent) / len(recent) if recent else 0
    logger.info(
        f"UpDown {interval_minutes}m | {'WIN' if won else 'LOSS'} registrado | "
        f"Racha reciente {sum(recent)}/{len(recent)} ({wr:.0%}) | "
        f"Total {s['wins']}/{s['total']}"
    )


def record_phantom_result(interval_minutes: int, trade: dict, won: bool):
    """
    Registra el resultado de una apuesta fantasma (señal detectada pero no ejecutada).

    trade: dict con side, confidence, combined_signal, elapsed_minutes, skip_reason, etc.
    won:   True si BTC se movió en la dirección que habríamos apostado.

    Estos datos alimentan el aprendizaje para ajustar los filtros del bot:
    - Phantoms que ganan mucho → estamos siendo demasiado conservadores (subir umbral vacío)
    - Phantoms que pierden → los filtros son correctos
    """
    key = str(interval_minutes)
    if key not in _stats:
        _stats[key] = _default_stats()
    s = _stats[key]
    if "phantom" not in s:
        s["phantom"] = _default_phantom_stats()
    p = s["phantom"]
    w = 1 if won else 0

    p["total"] += 1
    p["wins"]  += w

    p["recent"].append(w)
    if len(p["recent"]) > _RECENT_WINDOW:
        p["recent"] = p["recent"][-_RECENT_WINDOW:]

    # ── Por fuerza de señal ──────────────────────────────────────────────────
    confidence_pct = trade.get("confidence")
    sig_abs = (confidence_pct / 100.0) if confidence_pct is not None else abs(trade.get("combined_signal") or 0.0)
    sig_key = "weak" if sig_abs < 0.25 else ("med" if sig_abs < 0.50 else "strong")
    p["by_signal"][sig_key]["w" if won else "l"] += 1

    # ── Por lado ─────────────────────────────────────────────────────────────
    side = trade.get("side", "UP")
    if side in p["by_side"]:
        p["by_side"][side]["w" if won else "l"] += 1

    # ── Por timing ───────────────────────────────────────────────────────────
    elapsed = trade.get("elapsed_minutes") or 0.0
    el_key = "early" if elapsed < 1.5 else ("mid" if elapsed < 3.0 else "late")
    p["by_elapsed"][el_key]["w" if won else "l"] += 1

    # ── Por motivo de no-entrada ─────────────────────────────────────────────
    skip_reason = trade.get("skip_reason", "unknown")
    skip_lower  = skip_reason.lower()
    if skip_lower == "traded_real":
        srk = "traded_real"
    elif "débil" in skip_lower or "señal" in skip_lower or "confianza" in skip_lower:
        srk = "weak_signal"
    elif "gate" in skip_lower or ("momentum" in skip_lower and "gate" in skip_lower):
        srk = "momentum_gate"
    elif "timing" in skip_lower or "temprana" in skip_lower or "avanzada" in skip_lower:
        srk = "timing"
    elif "precio" in skip_lower or "caro" in skip_lower:
        srk = "price_expensive"
    elif "presupuesto" in skip_lower or "cash" in skip_lower:
        srk = "budget"
    elif "bloqueado" in skip_lower:
        srk = "blocked_side"
    elif skip_lower in ("no_signal", ""):
        srk = "no_signal"
    else:
        srk = "other"

    if srk not in p["by_skip_reason"]:
        p["by_skip_reason"][srk] = _bucket()
    p["by_skip_reason"][srk]["w" if won else "l"] += 1

    _save()

    recent = p["recent"]
    wr = sum(recent) / len(recent) if recent else 0
    logger.info(
        f"UpDown {interval_minutes}m | PHANTOM {'WIN' if won else 'LOSS'} "
        f"[{srk}] | WR phantom {sum(recent)}/{len(recent)} ({wr:.0%}) | "
        f"Total phantom {p['wins']}/{p['total']}"
    )


def _wr(bucket: dict) -> Optional[float]:
    """Win rate de un bucket si tiene suficientes muestras."""
    total = bucket["w"] + bucket["l"]
    return bucket["w"] / total if total >= _MIN_SAMPLES else None


def get_adaptive_params(interval_minutes: int) -> dict:
    """
    Devuelve parámetros de estrategia adaptados al rendimiento histórico.

    Returns dict:
        min_signal      – umbral mínimo |señal TA| (default 0.10)
        momentum_weight – cuánto desplaza la señal la prob base (default 0.15)
        min_ev          – EV mínimo para entrar (default 0.03)
        invert_signal   – si True, apostar CONTRA la dirección TA
        max_elapsed_min – entrar solo si elapsed < este valor (None = sin límite)
        reason          – texto para logging/UI
    """
    defaults = {
        "min_signal":      0.20,   # subido 0.10→0.20: sin historia suficiente, ser conservador
        "momentum_weight": 0.15,
        "min_ev":          0.05,   # subido 0.03→0.05: EV mínimo sin datos históricos
        "invert_signal":   False,
        "max_elapsed_min": None,
        "reason":          "defaults (historia insuficiente)",
    }

    key = str(interval_minutes)
    if key not in _stats or _stats[key]["total"] < _MIN_SAMPLES:
        return defaults

    s = _stats[key]
    p = dict(defaults)
    reasons = []

    # ── Win rate reciente ────────────────────────────────────────────────────
    recent = s["recent"]
    if len(recent) >= _MIN_SAMPLES:
        wr_r = sum(recent) / len(recent)
        if wr_r < 0.35:
            p["min_signal"]      = 0.35
            p["momentum_weight"] = 0.10
            p["min_ev"]          = 0.06
            reasons.append(f"WR reciente muy bajo {wr_r:.0%} → mínimo estricto")
        elif wr_r < 0.42:
            p["min_signal"] = 0.22
            p["min_ev"]     = 0.045
            reasons.append(f"WR reciente bajo {wr_r:.0%} → señal mínima media")
        elif wr_r > 0.60:
            p["min_signal"] = 0.08
            reasons.append(f"WR reciente alto {wr_r:.0%} → más permisivo")

    # ── Calibración dirección TA ─────────────────────────────────────────────
    if s["dir_total"] >= _MIN_SAMPLES:
        dir_acc = s["dir_correct"] / s["dir_total"]
        if dir_acc < 0.35:
            p["invert_signal"] = True
            reasons.append(f"TA apunta al lado incorrecto {dir_acc:.0%} → invertir")
        elif dir_acc < 0.42:
            p["momentum_weight"] = max(0.06, p["momentum_weight"] - 0.06)
            reasons.append(f"TA poco fiable {dir_acc:.0%} → reducir peso TA")

    # ── Timing: entradas tempranas pierden más ──────────────────────────────
    # min_elapsed proporcional al intervalo para no entrar en el primer 20% de la ventana
    wr_early = _wr(s["by_elapsed"]["early"])
    if wr_early is not None and wr_early < 0.40:
        # 5m → esperar 2.0min (40%), 15m → esperar 5.0min (33%)
        _min_el = 5.0 if interval_minutes >= 15 else 2.0
        p["min_elapsed_min"] = _min_el
        reasons.append(f"entradas tempranas WR {wr_early:.0%} → esperar >{_min_el}min")

    # ── Timing: entradas tardías pierden más ─────────────────────────────────
    wr_late  = _wr(s["by_elapsed"]["late"])
    wr_mid   = _wr(s["by_elapsed"]["mid"])
    if wr_late is not None and wr_late < 0.38:
        cutoff = 3.0
        if wr_mid is not None and wr_mid < 0.42:
            cutoff = 1.5
        p["max_elapsed_min"] = cutoff
        reasons.append(f"entradas tardías WR {wr_late:.0%} → limitar a {cutoff}min")

    # ── Señal débil consistentemente pierde ─────────────────────────────────
    wr_weak = _wr(s["by_signal"]["weak"])
    if wr_weak is not None and wr_weak < 0.38:
        p["min_signal"] = max(p["min_signal"], 0.25)
        reasons.append(f"señal débil WR {wr_weak:.0%} → umbral min 0.25")

    # ── Lado DOWN/UP consistentemente pierde ─────────────────────────────────
    # Requiere al menos 15 trades por lado antes de poder bloquearlo.
    # Con 5-10 trades la varianza es demasiado alta — ciclo vicioso:
    #   pocos DOWN trades → baja WR → block_down=True → nunca más DOWN trades
    _MIN_SIDE_SAMPLES = 15
    down_b = s["by_side"].get("DOWN", {})
    up_b   = s["by_side"].get("UP",   {})
    down_n = down_b.get("w", 0) + down_b.get("l", 0)
    up_n   = up_b.get("w", 0)   + up_b.get("l", 0)
    wr_down = (down_b["w"] / down_n) if down_n >= _MIN_SIDE_SAMPLES else None
    wr_up   = (up_b["w"]   / up_n)   if up_n   >= _MIN_SIDE_SAMPLES else None
    if wr_down is not None and wr_down < 0.30:
        if wr_up is None or wr_up >= wr_down:
            p["block_down"] = True
            reasons.append(f"DOWN pierde {wr_down:.0%} (n={down_n}) → bloquear lado DOWN")
    if wr_up is not None and wr_up < 0.30:
        p["block_up"] = True
        reasons.append(f"UP pierde {wr_up:.0%} (n={up_n}) → bloquear lado UP")

    # ── TA vs Momentum: aprender si el conflicto es tóxico ───────────────────
    by_ta_mom   = s.get("by_ta_mom", {})
    wr_agree    = _wr(by_ta_mom.get("agree",    {}))
    wr_conflict = _wr(by_ta_mom.get("conflict", {}))
    if wr_conflict is not None and wr_agree is not None:
        if wr_conflict < 0.38 or wr_conflict < wr_agree - 0.15:
            p["momentum_gate_strict"] = True
            reasons.append(
                f"conflicto TA/momentum pierde {wr_conflict:.0%} vs acuerdo {wr_agree:.0%} → gate estricto"
            )

    # ── Momentum fuerte en contra siempre pierde → bajar threshold del gate ──
    by_mom             = s.get("by_momentum", {})
    wr_strong_conflict = _wr(by_mom.get("strong_conflict", {}))
    wr_strong_agree    = _wr(by_mom.get("strong_agree",    {}))
    if wr_strong_conflict is not None and wr_strong_conflict < 0.35:
        p["momentum_gate_threshold"] = 0.08
        reasons.append(f"momentum fuerte en contra pierde {wr_strong_conflict:.0%} → gate threshold 0.08")
    if wr_strong_agree is not None and wr_strong_agree > 0.60:
        p["prefer_momentum_align"] = True
        reasons.append(f"momentum alineado gana {wr_strong_agree:.0%} → priorizar acuerdo TA+momentum")

    p["reason"] = "; ".join(reasons) if reasons else "sin ajustes (historia OK)"
    return p


def get_summary(interval_minutes: int) -> dict:
    """Resumen de estadísticas para la UI y API."""
    key = str(interval_minutes)
    s   = _stats.get(key, {})
    if not s or s.get("total", 0) == 0:
        return {
            "total": 0, "wins": 0,
            "win_rate": None, "recent_wr": None,
            "adaptive": get_adaptive_params(interval_minutes),
        }
    recent = s.get("recent", [])

    def _wr_pct(b):
        r = _wr(b)
        return round(r, 3) if r is not None else None

    dir_total = s.get("dir_total", 0)

    # Phantom summary
    ph = s.get("phantom", {})
    ph_total  = ph.get("total", 0)
    ph_wins   = ph.get("wins", 0)
    ph_recent = ph.get("recent", [])
    phantom_summary = None
    if ph_total > 0:
        phantom_summary = {
            "total":     ph_total,
            "wins":      ph_wins,
            "win_rate":  round(ph_wins / ph_total, 3),
            "recent_wr": round(sum(ph_recent) / len(ph_recent), 3) if ph_recent else None,
            "by_signal": {k: _wr_pct(v) for k, v in ph.get("by_signal", {}).items()},
            "by_side":   {k: _wr_pct(v) for k, v in ph.get("by_side",   {}).items()},
            "by_elapsed":{k: _wr_pct(v) for k, v in ph.get("by_elapsed",{}).items()},
            "by_skip_reason": {
                k: {"wr": _wr_pct(v), "total": v["w"] + v["l"]}
                for k, v in ph.get("by_skip_reason", {}).items()
            },
        }

    return {
        "total":     s["total"],
        "wins":      s["wins"],
        "win_rate":  round(s["wins"] / s["total"], 3),
        "recent_wr": round(sum(recent) / len(recent), 3) if recent else None,
        "by_signal": {k: _wr_pct(v) for k, v in s.get("by_signal", {}).items()},
        "by_side":   {k: _wr_pct(v) for k, v in s.get("by_side",   {}).items()},
        "by_elapsed":{k: _wr_pct(v) for k, v in s.get("by_elapsed",{}).items()},
        "ta_accuracy": round(s["dir_correct"] / dir_total, 3) if dir_total >= _MIN_SAMPLES else None,
        "adaptive":  get_adaptive_params(interval_minutes),
        "phantom":   phantom_summary,
    }
