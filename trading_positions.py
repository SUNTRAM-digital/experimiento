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
    _now = int(time.time())
    import datetime as _dt
    _now_hms = _dt.datetime.fromtimestamp(_now).strftime("%H:%M:%S")
    _now_iso = _dt.datetime.fromtimestamp(_now).strftime("%Y-%m-%d %H:%M:%S")
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
        "entry_ts": _now,
        "entry_hms": _now_hms,          # punto 11: HH:MM:SS de compra
        "entry_iso": _now_iso,          # punto 11: ISO legible
        "status": "OPEN",
        "exit_price": None,
        "exit_ts": None,
        "exit_hms": None,               # punto 11: HH:MM:SS de venta (se llena al cerrar)
        "exit_iso": None,
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

        import datetime as _dt
        _now = int(time.time())
        target["status"]      = exit_reason
        target["exit_price"]  = round(float(exit_price), 4)
        target["exit_ts"]     = _now
        target["exit_hms"]    = _dt.datetime.fromtimestamp(_now).strftime("%H:%M:%S")  # punto 11
        target["exit_iso"]    = _dt.datetime.fromtimestamp(_now).strftime("%Y-%m-%d %H:%M:%S")
        target["exit_reason"] = exit_reason
        target["pnl_usdc"]    = round(float(pnl_usdc), 4)
        # duración en segundos (útil para métricas)
        try:
            target["duration_secs"] = int(_now - int(target.get("entry_ts") or _now))
        except Exception:
            pass

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


def patch_position(slug: str, position_id: str, patch: dict, is_real: bool = False) -> Optional[dict]:
    """Actualiza campos de una posición OPEN (no cambia status). Usado por SL timer (punto 12)."""
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        positions = state[bucket].get(slug, [])
        for p in positions:
            if p.get("id") == position_id and p.get("status") == "OPEN":
                for k, v in patch.items():
                    p[k] = v
                _save(state)
                return dict(p)
    return None


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


def set_phantom_balance(new_balance: float) -> float:
    """Modifica solo el balance virtual phantom sin tocar posiciones (punto 6)."""
    with _LOCK:
        state = _load()
        state["meta"]["phantom_balance"] = round(float(new_balance), 2)
        _save(state)
        return state["meta"]["phantom_balance"]


def reset_real_exposure() -> dict:
    """Punto 7 — libera exposure REAL marcando posiciones OPEN como RELEASED.
    NO toca on-chain, solo el tracking interno (úsalo cuando ya cerraste manualmente
    en Polymarket o quieres olvidar trades fantasma)."""
    import time as _t
    released = 0
    pnl_zeroed = 0.0
    now = int(_t.time())
    with _LOCK:
        state = _load()
        for slug, plist in state["real"].items():
            for p in plist:
                if p.get("status") == "OPEN":
                    p["status"] = "RELEASED"
                    p["exit_ts"] = now
                    p["pnl_usdc"] = 0.0
                    p["release_reason"] = "manual_exposure_reset"
                    released += 1
        _save(state)
    return {"released": released, "pnl_zeroed": pnl_zeroed}


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


def real_exposure_usdc() -> float:
    """Suma de stake_usdc de todas las posiciones REAL con status=OPEN."""
    total = 0.0
    for p in all_open_positions(is_real=True):
        total += float(p.get("stake_usdc", 0) or 0)
    return round(total, 4)


def real_pnl_today_usdc() -> float:
    """Suma de pnl_usdc de posiciones REAL cerradas desde medianoche local."""
    import datetime as _dt
    midnight_ts = int(_dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    total = 0.0
    with _LOCK:
        state = _load()
        for plist in state["real"].values():
            for p in plist:
                if p.get("status") == "OPEN":
                    continue
                exit_ts = int(p.get("exit_ts") or 0)
                if exit_ts >= midnight_ts:
                    total += float(p.get("pnl_usdc") or 0)
    return round(total, 4)


def real_pending_redemption_usdc(window_hours: int = 48) -> dict:
    """
    Suma de redenciones esperadas en posiciones REAL recientes.
    Una posición real con status=RESOLVED_WIN y exit_reason=RESOLVED_WIN no fue vendida
    al CLOB (resolvió binario), entonces los shares pagan 1.00 cada uno via auto-redeem
    de Polymarket (~24h). Devuelve total esperado y desglose.
    """
    import time as _t
    cutoff = int(_t.time()) - window_hours * 3600
    total = 0.0
    items = []
    with _LOCK:
        state = _load()
        for plist in state["real"].values():
            for p in plist:
                if p.get("status") != "RESOLVED_WIN":
                    continue
                if p.get("exit_reason") != "RESOLVED_WIN":
                    continue
                exit_ts = int(p.get("exit_ts") or 0)
                if exit_ts < cutoff:
                    continue
                entry = float(p.get("entry_price") or 0)
                stake = float(p.get("stake_usdc") or 0)
                if entry <= 0:
                    continue
                shares = stake / entry
                payout = shares * 1.0
                total += payout
                items.append({
                    "slug": p.get("slug"),
                    "exit_ts": exit_ts,
                    "shares": round(shares, 4),
                    "payout_usdc": round(payout, 4),
                })
    return {"total_usdc": round(total, 4), "count": len(items), "items": items}


def real_consecutive_losses() -> int:
    """Cuenta pérdidas consecutivas de las posiciones REAL cerradas más recientes.
    Se detiene al encontrar un WIN o una posición con flag streak_reset=True."""
    with _LOCK:
        state = _load()
        closed = []
        for plist in state["real"].values():
            for p in plist:
                if p.get("status") != "OPEN" and p.get("exit_ts"):
                    closed.append(p)
    closed.sort(key=lambda p: int(p.get("exit_ts") or 0), reverse=True)
    streak = 0
    for p in closed:
        if p.get("streak_reset"):
            break
        if float(p.get("pnl_usdc") or 0) < 0:
            streak += 1
        else:
            break
    return streak


def reset_real_streak() -> int:
    """Marca la posición REAL cerrada más reciente con streak_reset=True.
    Efecto: real_consecutive_losses() retorna 0 hasta próxima pérdida.
    Retorna número de posiciones afectadas (0 o 1)."""
    with _LOCK:
        state = _load()
        most_recent = None
        most_recent_ts = -1
        for plist in state["real"].values():
            for p in plist:
                if p.get("status") == "OPEN":
                    continue
                ts = int(p.get("exit_ts") or 0)
                if ts > most_recent_ts:
                    most_recent_ts = ts
                    most_recent = p
        if most_recent is None:
            return 0
        most_recent["streak_reset"] = True
        _save(state)
        return 1


def real_equity_drawdown() -> dict:
    """Punto 19A — drawdown desde el peak histórico de cumulative PnL real.
    Recorre posiciones REAL cerradas cronológicamente, calcula serie de cumulative_pnl,
    retorna {peak, current, drawdown_abs, drawdown_pct}.
    drawdown_pct = (peak - current) / peak cuando peak > 0; 0 si peak <= 0.
    """
    with _LOCK:
        state = _load()
        closed = []
        for plist in state["real"].values():
            for p in plist:
                if p.get("status") == "OPEN":
                    continue
                if p.get("exit_ts") is None:
                    continue
                closed.append(p)
    closed.sort(key=lambda p: int(p.get("exit_ts") or 0))
    cum = 0.0
    peak = 0.0
    for p in closed:
        cum += float(p.get("pnl_usdc") or 0.0)
        if cum > peak:
            peak = cum
    current = cum
    drawdown_abs = peak - current
    drawdown_pct = (drawdown_abs / peak) if peak > 0 else 0.0
    return {
        "peak": peak,
        "current": current,
        "drawdown_abs": drawdown_abs,
        "drawdown_pct": drawdown_pct,
        "samples": len(closed),
    }


def phantom_gate_status(required_days: float = 7.0,
                         required_trades: int = 200,
                         required_wr: float = 0.75) -> dict:
    """Punto 19B — preflight gate antes de habilitar Trading Real.
    Verifica que phantom haya corrido suficientes días + trades con WR mínimo.
    Retorna {ok, days_elapsed, trades, wr, required_*, reasons[]}.
    """
    import time as _t
    with _LOCK:
        state = _load()
        closed = []
        first_ts = None
        for plist in state["phantom"].values():
            for p in plist:
                if p.get("status") == "OPEN":
                    continue
                if p.get("exit_ts") is None:
                    continue
                ts = int(p.get("entry_ts") or p.get("exit_ts") or 0)
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                closed.append(p)
    trades = len(closed)
    wins = sum(1 for p in closed if float(p.get("pnl_usdc") or 0) > 0)
    wr = (wins / trades) if trades > 0 else 0.0
    now = int(_t.time())
    days_elapsed = ((now - first_ts) / 86400.0) if first_ts else 0.0
    reasons: list = []
    if days_elapsed < required_days:
        reasons.append(f"días {days_elapsed:.1f}/{required_days:.1f}")
    if trades < required_trades:
        reasons.append(f"trades {trades}/{required_trades}")
    if wr < required_wr:
        reasons.append(f"WR {wr*100:.1f}%/{required_wr*100:.1f}%")
    ok = not reasons
    return {
        "ok": ok,
        "days_elapsed": days_elapsed,
        "trades": trades,
        "wr": wr,
        "required_days": required_days,
        "required_trades": required_trades,
        "required_wr": required_wr,
        "reasons": reasons,
    }


def stats_by_interval(is_real: bool = False, interval: Optional[int] = None) -> dict:
    """Stats segregados por interval (5, 15, 1440). Si interval=None retorna agregado.
    Incluye total/wins/losses/win_rate/realized_pnl/recent_wr/by_side."""
    with _LOCK:
        state = _load()
        bucket = "real" if is_real else "phantom"
        total = wins = losses = open_ct = 0
        total_pnl = 0.0
        recent_results: list = []   # últimos 20 cerrados (orden cronológico)
        by_side = {"UP": {"t": 0, "w": 0, "l": 0, "pnl": 0.0},
                   "DOWN": {"t": 0, "w": 0, "l": 0, "pnl": 0.0}}
        all_closed: list = []
        for plist in state[bucket].values():
            for p in plist:
                if interval is not None and int(p.get("interval", 0)) != int(interval):
                    continue
                total += 1
                status = p.get("status", "OPEN")
                pnl = float(p.get("pnl_usdc", 0) or 0)
                if status == "OPEN":
                    open_ct += 1
                    continue
                is_win = status in ("TARGET_HIT", "RESOLVED_WIN") or (status == "FORCED_EXIT" and pnl >= 0)
                if is_win:
                    wins += 1
                else:
                    losses += 1
                total_pnl += pnl
                side = (p.get("side") or "").upper()
                if side in by_side:
                    by_side[side]["t"] += 1
                    by_side[side]["pnl"] += pnl
                    if is_win:
                        by_side[side]["w"] += 1
                    else:
                        by_side[side]["l"] += 1
                all_closed.append((p.get("closed_at") or p.get("opened_at") or "", "WIN" if is_win else "LOSS"))
        all_closed.sort(key=lambda x: x[0])
        recent_results = [r for _, r in all_closed[-20:]]
        recent_wr = round(sum(1 for r in recent_results if r == "WIN") / len(recent_results) * 100, 2) if recent_results else None
        for s in by_side.values():
            s["pnl"] = round(s["pnl"], 4)
        return {
            "total":      total,
            "open":       open_ct,
            "closed":     total - open_ct,
            "wins":       wins,
            "losses":     losses,
            "win_rate":   round(wins / (wins + losses) * 100, 2) if (wins + losses) else None,
            "realized_pnl": round(total_pnl, 4),
            "recent":     recent_results,
            "recent_wr":  recent_wr,
            "by_side":    by_side,
        }


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
