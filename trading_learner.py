"""
Trading Mode learner — sugiere params adaptativos por interval (5/15/1440)
basado en stats reales de trading_positions (phantom + real combinados).

Reglas:
  - recent_wr < 40 → más selectivo: bajar entry_threshold, subir min_entry_price
  - recent_wr > 60 → más agresivo: subir entry_threshold
  - imbalance side: si UP wr < DOWN wr - 15pp → preferir DOWN (y viceversa)
  - alta racha de losses (≥3) → reducir stake sugerido a la mitad
"""
from typing import Optional

import trading_positions as tp


def _wr_pct(stats: dict) -> Optional[float]:
    return stats.get("recent_wr") if stats.get("recent_wr") is not None else stats.get("win_rate")


def get_adaptive_params(interval: int, base_params: Optional[dict] = None) -> dict:
    base = dict(base_params or {})
    base.setdefault("entry_threshold", 0.55)
    base.setdefault("min_entry_price", 0.10)
    base.setdefault("profit_offset", 0.12)
    base.setdefault("stake_usdc", 5.0)
    base.setdefault("preferred_side", None)

    ph = tp.stats_by_interval(is_real=False, interval=interval)
    rl = tp.stats_by_interval(is_real=True, interval=interval)

    closed_total = (ph.get("closed", 0) or 0) + (rl.get("closed", 0) or 0)
    if closed_total < 5:
        return {
            **base,
            "reason": "muestra insuficiente (<5 trades cerrados) — usar defaults",
            "recent_wr": _wr_pct(ph),
            "samples_phantom": ph.get("closed", 0),
            "samples_real": rl.get("closed", 0),
        }

    wr = _wr_pct(ph)
    suggested = dict(base)
    notes = []

    if wr is not None:
        if wr < 40:
            suggested["entry_threshold"] = round(max(0.20, base["entry_threshold"] - 0.05), 2)
            suggested["min_entry_price"] = round(min(0.30, base["min_entry_price"] + 0.03), 2)
            notes.append(f"WR {wr:.1f}% bajo → más selectivo (threshold↓, floor↑)")
        elif wr > 60:
            suggested["entry_threshold"] = round(min(0.75, base["entry_threshold"] + 0.03), 2)
            notes.append(f"WR {wr:.1f}% alto → más agresivo (threshold↑)")

    by_side = ph.get("by_side", {}) or {}
    up = by_side.get("UP", {}) or {}
    dn = by_side.get("DOWN", {}) or {}
    up_wr = (up.get("w", 0) / up["t"] * 100) if up.get("t") else None
    dn_wr = (dn.get("w", 0) / dn["t"] * 100) if dn.get("t") else None
    if up_wr is not None and dn_wr is not None and abs(up_wr - dn_wr) >= 15 and (up.get("t", 0) + dn.get("t", 0)) >= 8:
        if up_wr > dn_wr:
            suggested["preferred_side"] = "UP"
            notes.append(f"UP {up_wr:.0f}% vs DOWN {dn_wr:.0f}% → preferir UP")
        else:
            suggested["preferred_side"] = "DOWN"
            notes.append(f"DOWN {dn_wr:.0f}% vs UP {up_wr:.0f}% → preferir DOWN")

    recent = (ph.get("recent") or [])[-6:]
    cur_streak = 0
    for r in reversed(recent):
        if r == "LOSS":
            cur_streak += 1
        else:
            break
    if cur_streak >= 3:
        suggested["stake_usdc"] = round(max(1.0, base["stake_usdc"] * 0.5), 2)
        notes.append(f"{cur_streak} losses seguidos → stake ↓50%")

    suggested["reason"] = " | ".join(notes) if notes else "params estables"
    suggested["recent_wr"] = wr
    suggested["samples_phantom"] = ph.get("closed", 0)
    suggested["samples_real"] = rl.get("closed", 0)
    suggested["streak_losses"] = cur_streak
    return suggested


def get_summary(interval: int) -> dict:
    """Datos para mostrar en cerebro de bot Trading Mode {interval}m."""
    ph = tp.stats_by_interval(is_real=False, interval=interval)
    rl = tp.stats_by_interval(is_real=True, interval=interval)
    return {
        "interval": interval,
        "phantom": ph,
        "real":    rl,
        "adaptive": get_adaptive_params(interval),
    }
