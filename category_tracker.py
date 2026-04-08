"""
Category Tracker — Patron 2: Especializacion (Win Rate Decay)

Fundamento: cada categoria adicional que se opera cuesta ~6.3% de win rate.
  WR(n) = WR_base * e^(-0.065 * n)

  1 categoria:  88.2% WR
  2 categorias: 82.8% WR
  3 categorias: 77.7% WR
  5 categorias: 68.5% WR
  8 categorias: 56.2% WR

El bot trackea el win rate por categoria y bloquea automaticamente las que
caen por debajo del umbral minimo despues de suficientes trades.

Categorias del bot:
  - weather:  mercados de temperatura Polymarket (categoria dominante)
  - btc:      mercados de precio BTC
  - updown:   mercados BTC up/down 5m/15m
"""
import json
import math
from pathlib import Path
from typing import Optional

STATS_FILE = Path(__file__).parent / "data" / "category_stats.json"

# Umbrales por categoria
MIN_TRADES_TO_EVALUATE  = 20   # minimo de trades antes de poder bloquear
MIN_WIN_RATE_THRESHOLD  = 0.45  # por debajo de esto, bloquear la categoria
WARN_WIN_RATE_THRESHOLD = 0.52  # advertencia temprana


def _load_stats() -> dict:
    try:
        if STATS_FILE.exists():
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_stats(stats: dict):
    try:
        STATS_FILE.parent.mkdir(exist_ok=True)
        STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def record_trade_result(category: str, won: bool, pnl_usdc: float = 0.0):
    """
    Registra el resultado de un trade para el tracking de win rate.

    Args:
        category:  "weather", "btc", o "updown"
        won:       True si el trade fue ganador
        pnl_usdc:  ganancia/perdida en USDC
    """
    stats = _load_stats()
    if category not in stats:
        stats[category] = {"wins": 0, "losses": 0, "total_pnl": 0.0, "blocked": False}

    if won:
        stats[category]["wins"] += 1
    else:
        stats[category]["losses"] += 1
    stats[category]["total_pnl"] = round(stats[category].get("total_pnl", 0.0) + pnl_usdc, 4)

    # Evaluar si bloquear la categoria
    total = stats[category]["wins"] + stats[category]["losses"]
    if total >= MIN_TRADES_TO_EVALUATE:
        wr = stats[category]["wins"] / total
        stats[category]["win_rate"] = round(wr, 4)
        if wr < MIN_WIN_RATE_THRESHOLD:
            stats[category]["blocked"] = True
        elif wr >= MIN_WIN_RATE_THRESHOLD + 0.05:
            # Desbloquear si se recupera con margen
            stats[category]["blocked"] = False
    _save_stats(stats)


def get_category_status(category: str) -> dict:
    """
    Devuelve el estado actual de una categoria.

    Returns:
        {
            "allowed":    bool,    # False si esta bloqueada
            "win_rate":   float,   # win rate actual (0-1)
            "total_trades": int,
            "warning":    bool,    # True si win rate esta cayendo
            "message":    str,
        }
    """
    stats = _load_stats()
    cat = stats.get(category, {})

    wins   = cat.get("wins", 0)
    losses = cat.get("losses", 0)
    total  = wins + losses
    blocked = cat.get("blocked", False)

    if total == 0:
        return {
            "allowed": True,
            "win_rate": None,
            "total_trades": 0,
            "warning": False,
            "message": f"{category}: sin historial aun",
        }

    wr = wins / total
    warning = total >= MIN_TRADES_TO_EVALUATE and wr < WARN_WIN_RATE_THRESHOLD

    if blocked:
        msg = (
            f"{category} BLOQUEADA: win rate {wr:.1%} < {MIN_WIN_RATE_THRESHOLD:.0%} "
            f"en {total} trades — categoria sin edge demostrado"
        )
    elif warning:
        msg = (
            f"{category}: ADVERTENCIA — win rate {wr:.1%} cayendo "
            f"({total} trades)"
        )
    else:
        msg = f"{category}: OK — win rate {wr:.1%} en {total} trades"

    return {
        "allowed":      not blocked,
        "win_rate":     round(wr, 4),
        "total_trades": total,
        "warning":      warning,
        "message":      msg,
    }


def get_all_stats() -> dict:
    """Devuelve el estado completo de todas las categorias."""
    stats = _load_stats()
    result = {}
    for category in ["weather", "btc", "updown"]:
        result[category] = get_category_status(category)
    return result


def win_rate_decay_model(base_wr: float, n_categories: int) -> float:
    """
    Modelo de decaimiento exponencial de win rate segun numero de categorias.
    WR(n) = WR_base * e^(-0.065 * n)

    Util para proyectar el impacto de agregar categorias.
    """
    return base_wr * math.exp(-0.065 * n_categories)
