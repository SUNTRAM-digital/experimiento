"""
Position Manager para la estrategia de trading de volatilidad.

Persistencia en data/trading_positions.json:
  {
    "phantom": {
      "slug_A": [ {position}, {position}, ... ],
      "slug_B": [ {position}, ... ]
    },
    "real": {
      "slug_A": [ ... ]
    },
    "meta": {
      "phantom_balance": 50.0,
      "realized_pnl_phantom": 0.0,
      "realized_pnl_real": 0.0
    }
  }

Estados de posición:
  OPEN         — comprada, esperando target o deadline
  TARGET_HIT   — cerrada al target (profit)
  FORCED_EXIT  — cerrada a T-deadline (puede ser profit o loss)
  RESOLVED_WIN — no se cerró, mercado resolvió a favor (exit=1.00)
  RESOLVED_LOSS— no se cerró, mercado resolvió en contra (exit=0.00)
"""
import json
import os
import time
from threading import RLock
from typing import Optional
from uuid import uuid4

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "trading_positions.json")
_LOCK = RLock()


def _default_state() -> dict:
    return {
        "phantom": {},
        "real": {},
        "meta": {
            "phantom_balance": 50.0,
            "realized_pnl_phantom": 0.0,
            "realized_pnl_real": 0.0,
        },
    }


def _load() -> dict:
    if not os.path.exists(_DATA_PATH):
        return _default_state()
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        # Asegurar llaves mínimas
        for k in ("phantom", "real"):
            if k not in state:
                state[k] = {}
        if "meta" not in state:
            state["meta"] = _default_state()["meta"]
        for mk, mv in _default_state()["meta"].items():
            state["meta"].setdefault(mk, mv)
        return state
    except Exception:
        return _default_state()


def _save(state: dict) -> None:
    os.makedirs(os.path.dirname(_DATA_PATH), exist_ok=True)
    tmp = _DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _DATA_PATH)


def get_positions(slug: str, is_real: bool = False) -> list:
    """Devuelve todas las posiciones (abiertas y cerradas) para un mercado."""
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        return list(state[bucket].get(slug, []))


def get_open_positions(slug: str, is_real: bool = False) -> list:
    return [p for p in get_positions(slug, is_real) if p.get("status") == "OPEN"]


def open_position(
    slug: str,
    interval: int,
    end_ts: int,
    side: str,
    token_id: str,
    entry_price: float,
    target_price: float,
    stake_usdc: float,
    is_real: bool = False,
    extra: Optional[dict] = None,
) -> dict:
    """Abre nueva posición y retorna el dict."""
    pos = {
        "id": uuid4().hex[:12],
        "slug": slug,
        "interval": interval,
        "end_ts": end_ts,
        "side": side,
        "token_id": token_id,
        "entry_price": round(float(entry_price), 4),
        "target_price": round(float(target_price), 4),
        "stake_usdc": round(float(stake_usdc), 2),
        "entry_ts": int(time.time()),
        "status": "OPEN",
        "exit_price": None,
        "exit_ts": None,
        "exit_reason": None,
        "pnl_usdc": None,
        "is_real": bool(is_real),
    }
    if extra:
        pos.update({k: v for k, v in extra.items() if k not in pos})
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        state[bucket].setdefault(slug, []).append(pos)
        # Reservar balance phantom (lo sumamos de nuevo en close)
        if not is_real:
            state["meta"]["phantom_balance"] = round(
                state["meta"].get("phantom_balance", 50.0) - pos["stake_usdc"], 2
            )
        _save(state)
    return pos


def close_position(
    slug: str,
    position_id: str,
    exit_price: float,
    pnl_usdc: float,
    exit_reason: str,
    is_real: bool = False,
) -> Optional[dict]:
    """
    Cierra una posición. Mapea el exit_reason a status:
      TARGET_HIT    -> TARGET_HIT
      FORCED_EXIT   -> FORCED_EXIT
      RESOLVED_WIN  -> RESOLVED_WIN
      RESOLVED_LOSS -> RESOLVED_LOSS
    """
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        positions = state[bucket].get(slug, [])
        target = None
        for p in positions:
            if p.get("id") == position_id:
                target = p
                break
        if target is None or target.get("status") != "OPEN":
            return None

        target["status"]      = exit_reason
        target["exit_price"]  = round(float(exit_price), 4)
        target["exit_ts"]     = int(time.time())
        target["exit_reason"] = exit_reason
        target["pnl_usdc"]    = round(float(pnl_usdc), 4)

        # Actualizar balance / realized pnl
        if not is_real:
            # Devolver stake + pnl al balance phantom
            stake = float(target.get("stake_usdc", 0))
            state["meta"]["phantom_balance"] = round(
                state["meta"].get("phantom_balance", 50.0) + stake + pnl_usdc, 2
            )
            state["meta"]["realized_pnl_phantom"] = round(
                state["meta"].get("realized_pnl_phantom", 0.0) + pnl_usdc, 4
            )
        else:
            state["meta"]["realized_pnl_real"] = round(
                state["meta"].get("realized_pnl_real", 0.0) + pnl_usdc, 4
            )

        _save(state)
        return dict(target)


def all_open_positions(is_real: bool = False) -> list:
    """Devuelve todas las posiciones abiertas en todos los mercados."""
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        out = []
        for slug, plist in state[bucket].items():
            for p in plist:
                if p.get("status") == "OPEN":
                    out.append(p)
        return out


def get_meta() -> dict:
    with _LOCK:
        return dict(_load()["meta"])


def reset_phantom(new_balance: float = 50.0) -> None:
    """Reinicia estado phantom — borra posiciones y reinicia balance."""
    with _LOCK:
        state = _load()
        state["phantom"] = {}
        state["meta"]["phantom_balance"] = float(new_balance)
        state["meta"]["realized_pnl_phantom"] = 0.0
        _save(state)


def get_all_positions_flat(is_real: bool = False, limit: Optional[int] = None) -> list:
    """Todas las posiciones (abiertas+cerradas) de todos los mercados, ordenadas por entry_ts desc."""
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        out = []
        for slug, plist in state[bucket].items():
            out.extend(plist)
        out.sort(key=lambda p: p.get("entry_ts", 0), reverse=True)
        if limit:
            return out[:limit]
        return out


def stats_summary(is_real: bool = False) -> dict:
    """Resumen de wins/losses/pnl para UI."""
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        total = 0
        open_ct = 0
        target_hits = 0
        forced_profit = 0
        forced_loss = 0
        resolved_wins = 0
        resolved_losses = 0
        total_pnl = 0.0

        for plist in state[bucket].values():
            for p in plist:
                total += 1
                status = p.get("status", "OPEN")
                pnl = float(p.get("pnl_usdc", 0) or 0)
                if status == "OPEN":
                    open_ct += 1
                    continue
                total_pnl += pnl
                if status == "TARGET_HIT":
                    target_hits += 1
                elif status == "FORCED_EXIT":
                    if pnl >= 0:
                        forced_profit += 1
                    else:
                        forced_loss += 1
                elif status == "RESOLVED_WIN":
                    resolved_wins += 1
                elif status == "RESOLVED_LOSS":
                    resolved_losses += 1

        closed = total - open_ct
        wins = target_hits + forced_profit + resolved_wins
        return {
            "total_positions": total,
            "open": open_ct,
            "closed": closed,
            "target_hits": target_hits,
            "forced_profit": forced_profit,
            "forced_loss": forced_loss,
            "resolved_wins": resolved_wins,
            "resolved_losses": resolved_losses,
            "wins": wins,
            "losses": closed - wins,
            "win_rate": round(wins / closed * 100, 2) if closed else 0,
            "realized_pnl": round(total_pnl, 4),
            "phantom_balance": state["meta"].get("phantom_balance", 50.0),
        }
