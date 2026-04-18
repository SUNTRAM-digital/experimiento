"""
Fase 6 — Risk Manager Profesional

La mayoria de bots explotan no por mal edge sino por over-sizing y falta
de controles de riesgo. Un trade mal sizedo puede arrasar el bankroll.

Controles implementados:
  1. Max risk per trade:    nunca mas del MAX_RISK_PER_TRADE_PCT del bankroll
  2. Cash buffer:           mantener siempre >= MIN_CASH_BUFFER_PCT en efectivo
  3. Portfolio heatmap:     concentracion por ciudad, categoria y horizonte
  4. Auto-sizing escalonado: $1→$5→$10→$50→$100 solo despues de N consecutivos rentables
  5. Circuit breaker:        pausar bot si drawdown semanal > MAX_WEEKLY_DRAWDOWN_PCT
  6. Alertas de seguridad:   transacciones grandes, drawdown critico

Uso:
  from risk_manager import RiskManager
  rm = RiskManager()

  # Antes de cada trade
  check = rm.check_trade(opportunity, state)
  if not check["allowed"]:
      log(check["reason"])
      return

  # Al inicio de cada ciclo
  rm.update(state)
  if rm.circuit_breaker_active:
      log("Circuit breaker activado")
      return
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ── Parametros de riesgo ───────────────────────────────────────────────────────

MAX_RISK_PER_TRADE_PCT   = 0.05   # 5% del bankroll por trade (maximo absoluto)
MIN_CASH_BUFFER_PCT      = 0.30   # 30% del total debe estar en cash siempre
MAX_WEEKLY_DRAWDOWN_PCT  = 0.15   # 15% de caida semanal activa el circuit breaker
MAX_CITY_CONCENTRATION   = 0.40   # max 40% del capital expuesto en una sola ciudad
MAX_HORIZON_CONCENTRATION = 0.60  # max 60% del capital en posiciones de mismo horizonte

# Auto-sizing escalonado: niveles de capital por trade segun racha ganadora
AUTOSIZING_LEVELS = [
    {"min_streak": 0,  "max_usdc": 1.0},    # sin historial: maximo $1
    {"min_streak": 3,  "max_usdc": 5.0},    # 3 ganancias consecutivas: hasta $5
    {"min_streak": 7,  "max_usdc": 10.0},   # 7 consecutivas: hasta $10
    {"min_streak": 15, "max_usdc": 50.0},   # 15 consecutivas: hasta $50
    {"min_streak": 25, "max_usdc": 100.0},  # 25 consecutivas: hasta $100
]

_RISK_STATE_FILE = Path(__file__).parent / "data" / "risk_state.json"


class RiskManager:
    """
    Gestor de riesgo centralizado. Una instancia por sesion del bot.
    Persiste el estado semanal en disco para sobrevivir reinicios.
    """

    def __init__(self):
        self.circuit_breaker_active: bool = False
        self.circuit_breaker_reason: str  = ""
        self.weekly_start_value: float    = 0.0
        self.weekly_start_date: Optional[datetime] = None
        self.current_streak: int          = 0   # racha de trades ganadores consecutivos
        self.total_trades_session: int    = 0
        self._load_state()

    # ── Persistencia ──────────────────────────────────────────────────────────

    def _load_state(self):
        try:
            if _RISK_STATE_FILE.exists():
                data = json.loads(_RISK_STATE_FILE.read_text(encoding="utf-8"))
                self.weekly_start_value = float(data.get("weekly_start_value", 0))
                self.current_streak     = int(data.get("current_streak", 0))
                raw_date = data.get("weekly_start_date")
                if raw_date:
                    self.weekly_start_date = datetime.fromisoformat(raw_date)
                # Reactivar circuit breaker si corresponde
                if data.get("circuit_breaker_active"):
                    self.circuit_breaker_active = True
                    self.circuit_breaker_reason = data.get("circuit_breaker_reason", "")
        except Exception:
            pass

    def _save_state(self):
        try:
            _RISK_STATE_FILE.parent.mkdir(exist_ok=True)
            _RISK_STATE_FILE.write_text(
                json.dumps({
                    "weekly_start_value":    self.weekly_start_value,
                    "weekly_start_date":     self.weekly_start_date.isoformat() if self.weekly_start_date else None,
                    "current_streak":        self.current_streak,
                    "circuit_breaker_active": self.circuit_breaker_active,
                    "circuit_breaker_reason": self.circuit_breaker_reason,
                }, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── Actualizacion de estado ────────────────────────────────────────────────

    def update(self, total_account_value: float, open_positions: list[dict]):
        """
        Llamar al inicio de cada ciclo.
        Actualiza el valor semanal de referencia y verifica el circuit breaker.
        """
        now = datetime.now(timezone.utc)

        # Inicializar semana si es la primera vez o paso una semana
        if self.weekly_start_date is None:
            self.weekly_start_value = total_account_value
            self.weekly_start_date  = now
        elif (now - self.weekly_start_date).days >= 7:
            # Nueva semana — resetear referencia
            self.weekly_start_value = total_account_value
            self.weekly_start_date  = now
            # Desactivar circuit breaker al inicio de nueva semana
            if self.circuit_breaker_active:
                self.circuit_breaker_active = False
                self.circuit_breaker_reason = ""

        # Verificar circuit breaker (solo si está habilitado en config)
        try:
            from config import bot_params as _bp
            _cb_enabled = _bp.circuit_breaker_enabled
        except Exception:
            _cb_enabled = True  # por seguridad, si no hay config → activo
        if self.weekly_start_value > 0 and _cb_enabled:
            weekly_drawdown = (self.weekly_start_value - total_account_value) / self.weekly_start_value
            if weekly_drawdown >= MAX_WEEKLY_DRAWDOWN_PCT and not self.circuit_breaker_active:
                self.circuit_breaker_active = True
                self.circuit_breaker_reason = (
                    f"Drawdown semanal {weekly_drawdown:.1%} >= {MAX_WEEKLY_DRAWDOWN_PCT:.0%} — "
                    f"bot pausado hasta nueva semana"
                )
        elif not _cb_enabled and self.circuit_breaker_active:
            # Si el usuario desactiva el CB estando activo, limpiarlo
            self.circuit_breaker_active = False
            self.circuit_breaker_reason = ""

        self._save_state()

    def record_trade_result(self, won: bool):
        """Registrar resultado de un trade para el auto-sizing escalonado."""
        self.total_trades_session += 1
        if won:
            self.current_streak += 1
        else:
            self.current_streak = 0
        self._save_state()

    def reset_circuit_breaker(self):
        """Permite al usuario desactivar manualmente el circuit breaker."""
        self.circuit_breaker_active = False
        self.circuit_breaker_reason = ""
        self._save_state()

    # ── Limite de tamaño escalonado ────────────────────────────────────────────

    def get_autosizing_max(self) -> float:
        """
        Retorna el maximo permitido por trade segun la racha actual.
        El tamaño escala SOLO despues de rachas ganadoras probadas.
        """
        max_usdc = AUTOSIZING_LEVELS[0]["max_usdc"]
        for level in AUTOSIZING_LEVELS:
            if self.current_streak >= level["min_streak"]:
                max_usdc = level["max_usdc"]
        return max_usdc

    # ── Validacion de trade ────────────────────────────────────────────────────

    def check_trade(
        self,
        size_usdc: float,
        total_account_value: float,
        cash_available: float,
        open_positions: list[dict],
        city: str = "",
        hours_to_close: float = 48.0,
    ) -> dict:
        """
        Valida si un trade cumple todos los controles de riesgo.

        Args:
            size_usdc:           tamaño del trade en USDC
            total_account_value: valor total de la cuenta (cash + posiciones)
            cash_available:      cash libre en USDC
            open_positions:      posiciones abiertas actuales
            city:                ciudad del mercado (para concentracion)
            hours_to_close:      horas al cierre (para concentracion por horizonte)

        Returns:
            {"allowed": bool, "reason": str, "warnings": list[str], "adjusted_size": float}
        """
        warnings = []
        adjusted_size = size_usdc

        # 1. Circuit breaker
        if self.circuit_breaker_active:
            return {
                "allowed": False,
                "reason": f"CIRCUIT BREAKER: {self.circuit_breaker_reason}",
                "warnings": [],
                "adjusted_size": 0.0,
            }

        # 2. Max risk per trade
        if total_account_value > 0:
            risk_pct = size_usdc / total_account_value
            max_allowed = total_account_value * MAX_RISK_PER_TRADE_PCT
            if risk_pct > MAX_RISK_PER_TRADE_PCT:
                adjusted_size = round(max_allowed, 2)
                warnings.append(
                    f"Tamaño reducido de ${size_usdc:.2f} a ${adjusted_size:.2f} "
                    f"(max {MAX_RISK_PER_TRADE_PCT:.0%} del bankroll)"
                )

        # 3. Cash buffer obligatorio
        cash_after_trade = cash_available - adjusted_size
        min_cash_required = total_account_value * MIN_CASH_BUFFER_PCT
        if cash_after_trade < min_cash_required:
            return {
                "allowed": False,
                "reason": (
                    f"Cash buffer insuficiente: quedan ${cash_after_trade:.2f} "
                    f"despues del trade (minimo ${min_cash_required:.2f} = "
                    f"{MIN_CASH_BUFFER_PCT:.0%} del total)"
                ),
                "warnings": warnings,
                "adjusted_size": 0.0,
            }

        # 4. Auto-sizing: no superar el nivel actual
        autosizing_max = self.get_autosizing_max()
        if adjusted_size > autosizing_max:
            adjusted_size = round(autosizing_max, 2)
            warnings.append(
                f"Auto-sizing: reducido a ${adjusted_size:.2f} "
                f"(racha actual: {self.current_streak} wins consecutivos)"
            )

        # 5. Concentracion por ciudad
        if city and open_positions:
            total_deployed = sum(p.get("cost_usdc", 0) for p in open_positions)
            city_deployed  = sum(
                p.get("cost_usdc", 0) for p in open_positions
                if city.lower() in (p.get("market_title", "") or p.get("city", "")).lower()
            )
            city_after = city_deployed + adjusted_size
            total_after = total_deployed + adjusted_size
            if total_after > 0:
                city_pct = city_after / total_after
                if city_pct > MAX_CITY_CONCENTRATION:
                    warnings.append(
                        f"Concentracion alta en {city.title()}: "
                        f"{city_pct:.0%} del capital desplegado "
                        f"(max recomendado {MAX_CITY_CONCENTRATION:.0%})"
                    )

        # 6. Concentracion por horizonte temporal
        if open_positions and hours_to_close > 0:
            total_deployed = sum(p.get("cost_usdc", 0) for p in open_positions)
            # Mismo horizonte = posiciones con cierre en ventana similar (±12h)
            same_horizon = sum(
                p.get("cost_usdc", 0) for p in open_positions
                if abs((p.get("hours_to_close") or 48) - hours_to_close) <= 12
            )
            horizon_after = same_horizon + adjusted_size
            total_after   = total_deployed + adjusted_size
            if total_after > 0 and (horizon_after / total_after) > MAX_HORIZON_CONCENTRATION:
                warnings.append(
                    f"Concentracion alta en horizonte ~{hours_to_close:.0f}h: "
                    f"{horizon_after/total_after:.0%} del capital"
                )

        return {
            "allowed":       True,
            "reason":        "OK",
            "warnings":      warnings,
            "adjusted_size": adjusted_size,
        }

    # ── Portfolio Heatmap ──────────────────────────────────────────────────────

    def portfolio_heatmap(self, open_positions: list[dict]) -> dict:
        """
        Calcula la concentracion del portafolio por ciudad, categoria y horizonte.

        Returns:
            {
                "by_city":     {ciudad: {"usdc": float, "pct": float}},
                "by_category": {categoria: {"usdc": float, "pct": float}},
                "by_horizon":  {"<24h": ..., "24-72h": ..., ">72h": ...},
                "total_deployed": float,
                "alerts":      list[str],   # concentraciones > umbral
            }
        """
        if not open_positions:
            return {
                "by_city": {}, "by_category": {}, "by_horizon": {},
                "total_deployed": 0.0, "alerts": [],
            }

        total = sum(p.get("cost_usdc", 0) for p in open_positions)
        if total == 0:
            return {
                "by_city": {}, "by_category": {}, "by_horizon": {},
                "total_deployed": 0.0, "alerts": [],
            }

        # Por ciudad
        by_city: dict[str, float] = {}
        for p in open_positions:
            title = (p.get("market_title") or "").lower()
            city  = p.get("city") or "unknown"
            by_city[city] = by_city.get(city, 0) + p.get("cost_usdc", 0)

        # Por categoria
        by_category: dict[str, float] = {}
        for p in open_positions:
            cat = p.get("asset", "WEATHER")
            by_category[cat] = by_category.get(cat, 0) + p.get("cost_usdc", 0)

        # Por horizonte temporal
        by_horizon: dict[str, float] = {"<24h": 0, "24-72h": 0, ">72h": 0}
        for p in open_positions:
            h = p.get("hours_to_close") or 48
            if h < 24:
                by_horizon["<24h"] += p.get("cost_usdc", 0)
            elif h <= 72:
                by_horizon["24-72h"] += p.get("cost_usdc", 0)
            else:
                by_horizon[">72h"] += p.get("cost_usdc", 0)

        def to_pct(d: dict) -> dict:
            return {k: {"usdc": round(v, 2), "pct": round(v / total, 4)} for k, v in d.items()}

        # Generar alertas
        alerts = []
        for city, usdc in by_city.items():
            if usdc / total > MAX_CITY_CONCENTRATION:
                alerts.append(
                    f"Concentracion critica en {city}: {usdc/total:.0%} del portafolio"
                )
        for h_key, usdc in by_horizon.items():
            if usdc / total > MAX_HORIZON_CONCENTRATION:
                alerts.append(
                    f"Concentracion critica en horizonte {h_key}: {usdc/total:.0%}"
                )

        return {
            "by_city":        to_pct(by_city),
            "by_category":    to_pct(by_category),
            "by_horizon":     to_pct(by_horizon),
            "total_deployed": round(total, 2),
            "alerts":         alerts,
        }

    # ── Resumen de estado ──────────────────────────────────────────────────────

    def status_summary(self, total_account_value: float) -> str:
        """Resumen compacto del estado del risk manager para logs."""
        autosizing_max = self.get_autosizing_max()
        weekly_dd = 0.0
        if self.weekly_start_value > 0:
            weekly_dd = (self.weekly_start_value - total_account_value) / self.weekly_start_value

        cb_tag = " | CIRCUIT BREAKER ACTIVO" if self.circuit_breaker_active else ""
        return (
            f"RiskMgr | Racha: {self.current_streak} wins | "
            f"Max/trade: ${autosizing_max:.0f} | "
            f"DD semanal: {weekly_dd:.1%}{cb_tag}"
        )


# ── Instancia global ───────────────────────────────────────────────────────────
risk_manager = RiskManager()
