"""
Motor de backtesting en tiempo real — Fase 10.

Ejecuta estrategias en modo simulado en paralelo con trades reales.
Sin capital real: todas las operaciones son virtuales sobre $100 USDC ficticios.

Flujo:
  1. Cada vez que el bot evalúa un mercado (updown o clima), también lo evalúa en simulado.
  2. Los trades simulados se resuelven con los mismos precios/resultados que los reales.
  3. Todo queda registrado en data/backtest_trades.csv + data/backtest_state.json.
  4. La API expone /api/backtest/* para la UI.

Diferencia con phantom bets (Fase 9):
  - Phantom: registra mercados SKIPPED para el learner (1 bit: WIN/LOSS)
  - Backtest: capital ficticio, P&L en USDC, todos los campos TA, comparación vs real
"""
import csv
import json
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("weatherbot")

_DATA_DIR   = Path(__file__).parent / "data"
_CSV_FILE   = _DATA_DIR / "backtest_trades.csv"
_STATE_FILE = _DATA_DIR / "backtest_state.json"

# Capital simulado inicial (USDC ficticios)
SIM_CAPITAL_INITIAL = 100.0

# Asignación del capital simulado
_ALLOC_WEATHER = 0.60
_ALLOC_BTC     = 0.01
_ALLOC_UPDOWN  = 0.39

# Thresholds relajados vs real para capturar más señales y datos
SIM_MIN_CONFIDENCE = 0.10   # real = 0.20
SIM_MIN_EV_PCT     = 1.0    # real = 7%
SIM_KELLY_FRACTION = 0.25
SIM_MAX_POS_UPDOWN  = 5.0   # USDC máx por trade simulado updown
SIM_MAX_POS_WEATHER = 8.0   # USDC máx por trade simulado clima

_CSV_FIELDS = [
    "trade_id", "timestamp", "asset", "interval_min",
    "market_title", "side", "entry_price",
    "size_sim", "cost_sim",
    "confidence", "ev_pct",
    "ta_rec", "rsi", "ema20", "ofi",
    "elapsed_min", "min_to_close",
    "window_start_ts", "btc_start_price", "condition_id",
    "resolved", "result", "exit_price",
    "pnl_usdc", "pnl_pct",
    "real_trade_placed",   # True si el bot REAL también operó en este mercado
    "skip_reason",
]

# Retención de historial en memoria (evita RAM ilimitada)
_MAX_TRADES_MEMORY = 500


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ts_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


# ─────────────────────────────────────────────────────────────────────────────
#  MOTOR DE BACKTESTING
# ─────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Motor de backtesting en tiempo real.
    Singleton; se integra en el loop async del bot (no usa threads).
    """

    def __init__(self):
        _DATA_DIR.mkdir(exist_ok=True)
        self.sim_balance: float      = SIM_CAPITAL_INITIAL
        self.sim_initial: float      = SIM_CAPITAL_INITIAL
        # {trade_id: trade_dict} — posiciones abiertas (no resueltas)
        self.sim_positions: dict[str, dict] = {}
        # Trades completados (resueltos)
        self.sim_trades: list[dict]  = []
        self._load()
        self._ensure_csv()

    # ── Persistencia ─────────────────────────────────────────────────────────

    def _load(self):
        try:
            if _STATE_FILE.exists():
                data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
                self.sim_balance   = float(data.get("sim_balance",  SIM_CAPITAL_INITIAL))
                self.sim_initial   = float(data.get("sim_initial",  SIM_CAPITAL_INITIAL))
                self.sim_positions = data.get("sim_positions", {})
                self.sim_trades    = data.get("sim_trades",    [])[-_MAX_TRADES_MEMORY:]
        except Exception:
            pass

    def _save(self):
        try:
            _STATE_FILE.write_text(
                json.dumps({
                    "sim_balance":   round(self.sim_balance, 6),
                    "sim_initial":   self.sim_initial,
                    "sim_positions": self.sim_positions,
                    "sim_trades":    self.sim_trades[-_MAX_TRADES_MEMORY:],
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[BACKTEST] Save error: {e}")

    def _ensure_csv(self):
        if not _CSV_FILE.exists():
            with open(_CSV_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
                writer.writeheader()

    def _append_csv(self, trade: dict):
        try:
            with open(_CSV_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
                writer.writerow({k: trade.get(k, "") for k in _CSV_FIELDS})
        except Exception as e:
            logger.warning(f"[BACKTEST] CSV append error: {e}")

    # ── Entrada simulada: UpDown ─────────────────────────────────────────────

    def record_updown(
        self,
        market: dict,
        opp: Optional[dict],
        signal: dict,
        btc_start_price: float,
        skip_reason: str = "",
        real_trade_placed: bool = False,
    ) -> Optional[dict]:
        """
        Registra un trade UpDown simulado.

        - opp: oportunidad evaluada (puede ser None si no hubo señal suficiente)
        - signal: señal cruda de build_btc_direction_signal (siempre disponible)
        - btc_start_price: precio BTC al inicio de la ventana (para resolución)
        - real_trade_placed: True si el bot real ejecutó en este mercado
        """
        # Usar señal combinada para decidir dirección en simulado
        combined = signal.get("combined", 0.0)
        confidence = abs(combined)

        if confidence < SIM_MIN_CONFIDENCE:
            return None  # señal demasiado débil — no simular

        side = "UP" if combined > 0 else "DOWN"
        interval_min = market.get("interval_minutes", 15)
        entry_price  = market["up_price"] if side == "UP" else market["down_price"]

        if entry_price >= 0.95 or entry_price <= 0.05:
            return None  # mercado degenerado

        # Tamaño simulado: Kelly fraction sobre presupuesto updown
        budget_updown = self.sim_balance * _ALLOC_UPDOWN
        size_usdc = min(
            budget_updown * SIM_KELLY_FRACTION * confidence,
            SIM_MAX_POS_UPDOWN,
            self.sim_balance,
        )
        if size_usdc < 0.20:
            return None  # posición demasiado pequeña

        shares = round(size_usdc / entry_price, 4)
        cost   = round(shares * entry_price, 6)

        trade_id = uuid.uuid4().hex[:10]
        end_ts   = int(market.get("window_start_ts", 0)) + interval_min * 60

        trade = {
            "trade_id":         trade_id,
            "timestamp":        _now_str(),
            "asset":            f"BTC_UPDOWN_{interval_min}M",
            "interval_min":     interval_min,
            "market_title":     market.get("title", ""),
            "side":             side,
            "entry_price":      round(entry_price, 6),
            "size_sim":         shares,
            "cost_sim":         cost,
            "confidence":       round(confidence, 4),
            "ev_pct":           (opp or {}).get("ev_pct", round(confidence * 15, 2)),
            # Señales TA
            "ta_rec":           signal.get("ta_recommendation", signal.get("ta_dir", "")),
            "rsi":              signal.get("rsi", ""),
            "ema20":            signal.get("ema20", ""),
            "ofi":              round(signal.get("ofi", 0.0), 4),
            # Timing
            "elapsed_min":      round(market.get("elapsed_minutes", 0), 2),
            "min_to_close":     round(market.get("minutes_to_close", 0), 2),
            "window_start_ts":  market.get("window_start_ts", 0),
            "btc_start_price":  round(btc_start_price, 2),
            "condition_id":     market.get("condition_id", ""),
            # Para resolución
            "_end_ts":          end_ts,
            # Resultado (pendiente)
            "resolved":         False,
            "result":           "PENDING",
            "exit_price":       "",
            "pnl_usdc":         "",
            "pnl_pct":          "",
            # Metadata
            "real_trade_placed": real_trade_placed,
            "skip_reason":       skip_reason or (
                "real_trade" if real_trade_placed else
                ("no_real_signal" if opp is None else "below_real_threshold")
            ),
        }

        self.sim_balance   = round(self.sim_balance - cost, 6)
        self.sim_positions[trade_id] = trade

        logger.info(
            f"[BACKTEST] UpDown {interval_min}m SIM {side} "
            f"{shares:.4f}sh @ {entry_price:.3f} (${cost:.2f}) "
            f"conf={confidence:.3f} | real={'YES' if real_trade_placed else 'NO'}"
        )
        self._save()
        return trade

    # ── Entrada simulada: Clima ──────────────────────────────────────────────

    def record_weather(
        self,
        opp: dict,
        real_trade_placed: bool = False,
    ) -> Optional[dict]:
        """
        Registra un trade de clima simulado.
        opp: oportunidad de clima (de strategy.py evaluate_market).
        """
        ev_pct = float(opp.get("ev_pct") or 0)
        if ev_pct < SIM_MIN_EV_PCT:
            return None

        side        = opp.get("side", "YES")
        entry_price = float(opp.get("market_prob", 0.5))
        if entry_price <= 0.02 or entry_price >= 0.98:
            return None

        confidence = min(ev_pct / 20, 1.0)  # normalizar EV → confianza 0-1
        budget_wx  = self.sim_balance * _ALLOC_WEATHER
        size_usdc  = min(
            budget_wx * SIM_KELLY_FRACTION * confidence,
            SIM_MAX_POS_WEATHER,
            self.sim_balance,
        )
        if size_usdc < 0.20:
            return None

        shares = round(size_usdc / entry_price, 4)
        cost   = round(shares * entry_price, 6)

        trade_id = uuid.uuid4().hex[:10]
        trade = {
            "trade_id":        trade_id,
            "timestamp":       _now_str(),
            "asset":           "WEATHER",
            "interval_min":    "",
            "market_title":    opp.get("market_title", ""),
            "side":            side,
            "entry_price":     round(entry_price, 6),
            "size_sim":        shares,
            "cost_sim":        cost,
            "confidence":      round(confidence, 4),
            "ev_pct":          round(ev_pct, 2),
            "ta_rec":          "",
            "rsi":             "",
            "ema20":           "",
            "ofi":             "",
            "elapsed_min":     "",
            "min_to_close":    round(float(opp.get("hours_to_close", 0)) * 60, 1),
            "window_start_ts": "",
            "btc_start_price": "",
            "condition_id":    opp.get("condition_id", ""),
            "_end_ts":         0,   # se resuelve por condition_id
            "resolved":        False,
            "result":          "PENDING",
            "exit_price":      "",
            "pnl_usdc":        "",
            "pnl_pct":         "",
            "real_trade_placed": real_trade_placed,
            "skip_reason":      "real_trade" if real_trade_placed else "below_real_threshold",
        }

        self.sim_balance   = round(self.sim_balance - cost, 6)
        self.sim_positions[trade_id] = trade

        logger.info(
            f"[BACKTEST] Weather SIM {side} {shares:.4f}sh @ {entry_price:.3f} "
            f"(${cost:.2f}) EV={ev_pct}% | real={'YES' if real_trade_placed else 'NO'}"
        )
        self._save()
        return trade

    # ── Resolución: UpDown ───────────────────────────────────────────────────

    def resolve_updown_trades(self, btc_price_now: float) -> int:
        """
        Resuelve trades UpDown cuya ventana ya cerró.
        Usa btc_price_now como precio de resolución.
        Retorna número de trades resueltos.
        """
        if not btc_price_now:
            return 0

        now_ts = _ts_now()
        resolved_ids = []

        for trade_id, trade in list(self.sim_positions.items()):
            asset = trade.get("asset", "")
            if not asset.startswith("BTC_UPDOWN"):
                continue

            end_ts = trade.get("_end_ts", 0)
            if now_ts < end_ts + 30:   # esperar 30s tras cierre para que el precio estabilice
                continue

            btc_start = float(trade.get("btc_start_price") or btc_price_now)
            side      = trade["side"]
            won = (
                (side == "UP"   and btc_price_now >= btc_start) or
                (side == "DOWN" and btc_price_now <  btc_start)
            )

            exit_price = 1.0 if won else 0.0
            pnl_usdc   = round(trade["size_sim"] * exit_price - trade["cost_sim"], 6)
            pnl_pct    = round(pnl_usdc / trade["cost_sim"] * 100, 2) if trade["cost_sim"] > 0 else 0.0

            trade.update({
                "resolved":   True,
                "result":     "WIN" if won else "LOSS",
                "exit_price": exit_price,
                "pnl_usdc":   pnl_usdc,
                "pnl_pct":    pnl_pct,
            })

            self.sim_balance = round(self.sim_balance + trade["size_sim"] * exit_price, 6)
            resolved_ids.append(trade_id)
            self.sim_trades.append({k: v for k, v in trade.items() if not k.startswith("_")})
            self._append_csv(trade)

            logger.info(
                f"[BACKTEST] UpDown {trade['interval_min']}m RESOLVED "
                f"{side} {'WIN' if won else 'LOSS'} "
                f"btc_start={btc_start:.0f} btc_now={btc_price_now:.0f} "
                f"P&L=${pnl_usdc:+.4f} | SimBal=${self.sim_balance:.2f}"
            )

        for tid in resolved_ids:
            self.sim_positions.pop(tid, None)

        if resolved_ids:
            # Mantener historial acotado
            if len(self.sim_trades) > _MAX_TRADES_MEMORY:
                self.sim_trades = self.sim_trades[-_MAX_TRADES_MEMORY:]
            self._save()

        return len(resolved_ids)

    # ── Resolución: Clima ────────────────────────────────────────────────────

    def resolve_weather_trade(self, condition_id: str, won: bool) -> bool:
        """
        Resuelve un trade de clima simulado por condition_id.
        Llamado cuando el mercado real resuelve (posición se vuelve redeemable).
        Retorna True si se encontró y resolvió el trade.
        """
        for trade_id, trade in list(self.sim_positions.items()):
            if trade.get("condition_id") != condition_id:
                continue
            if trade.get("asset") != "WEATHER":
                continue

            exit_price = 1.0 if won else 0.0
            pnl_usdc   = round(trade["size_sim"] * exit_price - trade["cost_sim"], 6)
            pnl_pct    = round(pnl_usdc / trade["cost_sim"] * 100, 2) if trade["cost_sim"] > 0 else 0.0

            trade.update({
                "resolved":   True,
                "result":     "WIN" if won else "LOSS",
                "exit_price": exit_price,
                "pnl_usdc":   pnl_usdc,
                "pnl_pct":    pnl_pct,
            })

            self.sim_balance = round(self.sim_balance + trade["size_sim"] * exit_price, 6)
            self.sim_positions.pop(trade_id, None)
            self.sim_trades.append({k: v for k, v in trade.items() if not k.startswith("_")})
            self._append_csv(trade)

            if len(self.sim_trades) > _MAX_TRADES_MEMORY:
                self.sim_trades = self.sim_trades[-_MAX_TRADES_MEMORY:]
            self._save()

            logger.info(
                f"[BACKTEST] Weather RESOLVED {condition_id[:12]}… "
                f"{'WIN' if won else 'LOSS'} P&L=${pnl_usdc:+.4f} | SimBal=${self.sim_balance:.2f}"
            )
            return True

        return False

    # ── Reset ─────────────────────────────────────────────────────────────────

    def reset(self):
        """Reinicia el capital simulado y borra el historial."""
        self.sim_balance   = SIM_CAPITAL_INITIAL
        self.sim_initial   = SIM_CAPITAL_INITIAL
        self.sim_positions = {}
        self.sim_trades    = []
        self._save()
        # Reescribir CSV con solo el header
        with open(_CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            writer.writeheader()
        logger.info("[BACKTEST] Motor reseteado — capital simulado restablecido a $100")

    # ── Estadísticas ─────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Retorna estadísticas completas del portfolio simulado."""
        trades   = self.sim_trades
        resolved = [t for t in trades if t.get("resolved")]
        pending  = list(self.sim_positions.values())

        if not resolved:
            return {
                "total":           0,
                "resolved":        0,
                "wins":            0,
                "losses":          0,
                "win_rate":        None,
                "total_pnl":       0.0,
                "sim_balance":     round(self.sim_balance, 2),
                "sim_initial":     self.sim_initial,
                "sim_return_pct":  round((self.sim_balance - self.sim_initial) / self.sim_initial * 100, 2),
                "open_positions":  len(pending),
                "open_cost":       round(sum(float(p.get("cost_sim", 0)) for p in pending), 2),
                "by_asset":        {},
                "by_confidence":   {},
                "by_real":         {},
                "recent":          [],
            }

        wins   = [t for t in resolved if t.get("result") == "WIN"]
        losses = [t for t in resolved if t.get("result") == "LOSS"]
        total_pnl = sum(
            float(t["pnl_usdc"]) for t in resolved
            if isinstance(t.get("pnl_usdc"), (int, float))
        )

        def _wr(lst): return round(len([x for x in lst if x.get("result")=="WIN"]) / len(lst), 3) if lst else None

        # Por asset
        by_asset: dict = {}
        for t in resolved:
            a = t.get("asset", "?")
            if a not in by_asset:
                by_asset[a] = {"trades": [], "pnl": 0.0}
            by_asset[a]["trades"].append(t)
            if isinstance(t.get("pnl_usdc"), (int, float)):
                by_asset[a]["pnl"] = round(by_asset[a]["pnl"] + float(t["pnl_usdc"]), 4)
        by_asset_out = {}
        for a, d in by_asset.items():
            tl = d["trades"]
            by_asset_out[a] = {
                "total":    len(tl),
                "wins":     len([x for x in tl if x.get("result") == "WIN"]),
                "win_rate": _wr(tl),
                "pnl":      round(d["pnl"], 4),
            }

        # Por rango de confianza
        def _bucket_conf(conf):
            c = float(conf or 0)
            if c < 0.20: return "low(<20%)"
            if c < 0.40: return "mid(20-40%)"
            return "high(>40%)"
        by_conf: dict = {}
        for t in resolved:
            b = _bucket_conf(t.get("confidence", 0))
            if b not in by_conf:
                by_conf[b] = []
            by_conf[b].append(t)
        by_conf_out = {b: {"total": len(l), "win_rate": _wr(l)} for b, l in by_conf.items()}

        # Simulado vs Real: ¿las señales que el bot real ejecutó tienen mejor WR en simulado?
        real_trades = [t for t in resolved if t.get("real_trade_placed")]
        sim_only    = [t for t in resolved if not t.get("real_trade_placed")]
        by_real_out = {
            "when_real_traded":  {"total": len(real_trades), "win_rate": _wr(real_trades)},
            "when_real_skipped": {"total": len(sim_only),    "win_rate": _wr(sim_only)},
        }

        return {
            "total":           len(trades),
            "resolved":        len(resolved),
            "wins":            len(wins),
            "losses":          len(losses),
            "win_rate":        round(len(wins) / len(resolved), 3) if resolved else None,
            "total_pnl":       round(total_pnl, 4),
            "sim_balance":     round(self.sim_balance, 2),
            "sim_initial":     self.sim_initial,
            "sim_return_pct":  round((self.sim_balance - self.sim_initial) / self.sim_initial * 100, 2),
            "open_positions":  len(pending),
            "open_cost":       round(sum(float(p.get("cost_sim", 0)) for p in pending), 2),
            "by_asset":        by_asset_out,
            "by_confidence":   by_conf_out,
            "by_real":         by_real_out,
            "recent":          list(reversed(trades[-15:])),
        }

    def get_open_positions(self) -> list[dict]:
        return [
            {k: v for k, v in t.items() if not k.startswith("_")}
            for t in self.sim_positions.values()
        ]

    def get_recent_trades(self, n: int = 50) -> list[dict]:
        return list(reversed(self.sim_trades[-n:]))


# ── Singleton global ──────────────────────────────────────────────────────────
backtest_engine = BacktestEngine()
