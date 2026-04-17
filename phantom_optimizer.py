"""
phantom_optimizer.py — Optimizador autónomo de estrategia para bots phantom.

Lógica:
  Después de cada trade phantom resuelto:
  1. Actualiza racha consecutiva (wins/losses) para ese intervalo.
  2. Evalúa si activar/desactivar dinero real:
       - ACTIVAR:  WR >= 75% O racha >= 7 wins consecutivos
       - DESACTIVAR: WR < 50% O racha >= 3 losses consecutivos
  3. Si WR < 50% después de TRIAL_MIN trades en preset actual → prueba siguiente preset.

Fuente de verdad WR: updown_learner._stats[key]['phantom'] (completo, 212/652 trades).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Configuración ─────────────────────────────────────────────────────────────

TRIAL_MIN = 20          # trades mínimos antes de evaluar cambio de preset
WIN_RATE_ENABLE = 0.75  # WR para activar dinero real
WIN_RATE_DISABLE = 0.50 # WR para desactivar dinero real
CONSEC_WIN_ENABLE = 7   # wins consecutivos para activar
CONSEC_LOSS_DISABLE = 3 # losses consecutivos para desactivar

# Search space de presets por intervalo.
# Cada preset es (min_confidence, momentum_gate).
# El optimizador los recorre en orden circular cuando el WR es bajo.
STRATEGY_PRESETS = {
    "5": [
        {"min_confidence": 0.20, "momentum_gate": 0.20, "name": "default"},
        {"min_confidence": 0.30, "momentum_gate": 0.20, "name": "conf+"},
        {"min_confidence": 0.25, "momentum_gate": 0.30, "name": "mom+"},
        {"min_confidence": 0.35, "momentum_gate": 0.25, "name": "conf++/mom+"},
        {"min_confidence": 0.20, "momentum_gate": 0.15, "name": "mom-"},
        {"min_confidence": 0.40, "momentum_gate": 0.20, "name": "conf+++"},
    ],
    "15": [
        {"min_confidence": 0.20, "momentum_gate": 0.20, "name": "default"},
        {"min_confidence": 0.30, "momentum_gate": 0.20, "name": "conf+"},
        {"min_confidence": 0.25, "momentum_gate": 0.30, "name": "mom+"},
        {"min_confidence": 0.35, "momentum_gate": 0.25, "name": "conf++/mom+"},
        {"min_confidence": 0.20, "momentum_gate": 0.15, "name": "mom-"},
        {"min_confidence": 0.40, "momentum_gate": 0.20, "name": "conf+++"},
    ],
}

# ── Archivo de estado ─────────────────────────────────────────────────────────

_STATE_FILE = Path(__file__).parent / "data" / "phantom_optimizer_state.json"


def _default_state() -> dict:
    return {
        "5": {
            "consec_wins": 0,
            "consec_losses": 0,
            "preset_idx": 0,
            "trades_in_preset": 0,
        },
        "15": {
            "consec_wins": 0,
            "consec_losses": 0,
            "preset_idx": 0,
            "trades_in_preset": 0,
        },
    }


_state: dict = _default_state()


def _load_state() -> None:
    global _state
    try:
        if _STATE_FILE.exists():
            loaded = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            for k in ("5", "15"):
                if k in loaded:
                    _state[k].update(loaded[k])
    except Exception as e:
        logger.debug(f"[Optimizer] No se pudo cargar estado: {e}")


def _save_state() -> None:
    try:
        _STATE_FILE.parent.mkdir(exist_ok=True)
        _STATE_FILE.write_text(
            json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.debug(f"[Optimizer] No se pudo guardar estado: {e}")


_load_state()

# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_phantom_wr(interval_key: str) -> tuple[Optional[float], int, int]:
    """Retorna (win_rate, wins, total) desde updown_learner (fuente completa)."""
    try:
        from updown_learner import _stats
        ph = _stats.get(interval_key, {}).get("phantom", {})
        total = ph.get("total", 0)
        wins  = ph.get("wins", 0)
        if total == 0:
            return None, 0, 0
        return wins / total, wins, total
    except Exception:
        return None, 0, 0


def _apply_preset(interval_key: str, preset: dict, bot_id: str, reason: str) -> None:
    """Aplica un preset de estrategia y lo registra en bot_param_history."""
    try:
        from config import bot_params
        conf_key = f"updown_{interval_key}m_min_confidence"
        mom_key  = f"updown_{interval_key}m_momentum_gate"
        old_conf = getattr(bot_params, conf_key)
        old_mom  = getattr(bot_params, mom_key)
        setattr(bot_params, conf_key, preset["min_confidence"])
        setattr(bot_params, mom_key,  preset["momentum_gate"])
        bot_params.save()
        logger.info(
            f"[Optimizer/{interval_key}m] Preset '{preset['name']}': "
            f"conf {old_conf:.2f}→{preset['min_confidence']:.2f}  "
            f"mom {old_mom:.2f}→{preset['momentum_gate']:.2f}  ({reason})"
        )
        # Registrar en historial de parámetros
        try:
            import api as _api
            _api._save_param_history_entry(
                bot_id=bot_id,
                tool_name="optimizer_preset",
                inputs={"preset": preset["name"], "reason": reason},
                old_values={conf_key: old_conf, mom_key: old_mom},
                success=True,
                reason=f"[Optimizer] {reason}",
            )
        except Exception as _he:
            logger.debug(f"[Optimizer] No pudo guardar historial: {_he}")
    except Exception as e:
        logger.warning(f"[Optimizer] Error aplicando preset: {e}")


def _toggle_real_money(enable: bool, reason: str, bot_id: str) -> None:
    """Activa o desactiva phantom_real_enabled y lo registra."""
    try:
        from config import bot_params
        current = bot_params.phantom_real_enabled
        if current == enable:
            return
        bot_params.phantom_real_enabled = enable
        bot_params.save()
        action = "ACTIVADO" if enable else "DESACTIVADO"
        logger.info(f"[Optimizer] Dinero real {action} — {reason}")
        try:
            import api as _api
            _api._save_param_history_entry(
                bot_id=bot_id,
                tool_name="optimizer_real_money",
                inputs={"enable": enable, "reason": reason},
                old_values={"phantom_real_enabled": current},
                success=True,
                reason=f"[Optimizer] {reason}",
            )
        except Exception as _he:
            logger.debug(f"[Optimizer] No pudo guardar historial real_money: {_he}")
    except Exception as e:
        logger.warning(f"[Optimizer] Error toggling dinero real: {e}")


# ── Función principal ─────────────────────────────────────────────────────────


def check_and_act(interval_minutes: int, won: bool) -> None:
    """
    Llamar después de cada trade phantom resuelto.
    interval_minutes: 5 o 15
    won: True si ganó, False si perdió
    """
    key = str(interval_minutes)
    bot_id = f"ph{key}m"
    s = _state[key]

    # ── 1. Actualizar racha consecutiva ──────────────────────────────────────
    if won:
        s["consec_wins"]  += 1
        s["consec_losses"] = 0
    else:
        s["consec_losses"] += 1
        s["consec_wins"]   = 0

    s["trades_in_preset"] += 1
    _save_state()

    wr, wins, total = _get_phantom_wr(key)
    wr_str = f"{wr:.0%}" if wr is not None else "N/A"

    logger.debug(
        f"[Optimizer/{key}m] WR={wr_str} ({wins}/{total}) | "
        f"streak wins={s['consec_wins']} losses={s['consec_losses']} | "
        f"preset_trades={s['trades_in_preset']}"
    )

    # ── 2. Reglas de dinero real ──────────────────────────────────────────────
    # Activar si WR >= 75% O 7+ wins consecutivos
    if (wr is not None and wr >= WIN_RATE_ENABLE and total >= TRIAL_MIN) or \
       s["consec_wins"] >= CONSEC_WIN_ENABLE:
        reason = (
            f"WR={wr_str} >= {WIN_RATE_ENABLE:.0%}" if (wr is not None and wr >= WIN_RATE_ENABLE)
            else f"{s['consec_wins']} wins consecutivos"
        )
        _toggle_real_money(True, reason, bot_id)

    # Desactivar solo si WR < 50% (la racha de losses ya no desactiva)
    elif wr is not None and wr < WIN_RATE_DISABLE and total >= TRIAL_MIN:
        _toggle_real_money(False, f"WR={wr_str} < {WIN_RATE_DISABLE:.0%}", bot_id)

    # ── 3. Cambio de preset si WR bajo después del periodo de prueba ─────────
    if (
        wr is not None
        and wr < WIN_RATE_DISABLE
        and s["trades_in_preset"] >= TRIAL_MIN
    ):
        presets = STRATEGY_PRESETS[key]
        next_idx = (s["preset_idx"] + 1) % len(presets)
        next_preset = presets[next_idx]
        reason = f"WR={wr_str} < {WIN_RATE_DISABLE:.0%} tras {s['trades_in_preset']} trades en preset '{presets[s['preset_idx']]['name']}'"
        s["preset_idx"] = next_idx
        s["trades_in_preset"] = 0
        _save_state()
        _apply_preset(key, next_preset, bot_id, reason)


def get_status(interval_minutes: int) -> dict:
    """Retorna estado del optimizador para mostrar en el brain panel."""
    key = str(interval_minutes)
    s = _state[key]
    wr, wins, total = _get_phantom_wr(key)
    presets = STRATEGY_PRESETS[key]
    current_preset = presets[s["preset_idx"]]
    return {
        "interval": interval_minutes,
        "win_rate": wr,
        "wins": wins,
        "total": total,
        "consec_wins": s["consec_wins"],
        "consec_losses": s["consec_losses"],
        "preset_idx": s["preset_idx"],
        "preset_name": current_preset["name"],
        "preset_conf": current_preset["min_confidence"],
        "preset_mom": current_preset["momentum_gate"],
        "trades_in_preset": s["trades_in_preset"],
        "trial_min": TRIAL_MIN,
        "wr_enable": WIN_RATE_ENABLE,
        "wr_disable": WIN_RATE_DISABLE,
        "consec_win_enable": CONSEC_WIN_ENABLE,
        "consec_loss_disable": CONSEC_LOSS_DISABLE,
    }
