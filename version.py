"""
Versioning de WeatherBot Polymarket.

Convención: MAJOR.MINOR.PATCH
  MAJOR = Número de Fase
  MINOR = Mejoras/features dentro de la fase
  PATCH = Bugfixes

Historial de fases:
  Fase 1  (v1.0.0) — Bot base: mercados de clima, integración Polymarket CLOB
  Fase 2  (v2.0.0) — Patrones avanzados: category tracker, contrarian, exit monitor, P2 win rate decay
  Fase 3  (v3.0.0) — Backtesting + Screener: backtest engine 4 estrategias, value/momentum screener, Bayesian updating, capital velocity
  Fase 4  (v4.0.0) — Lawyer's Edge: parser ICAO, boundary zones, reglas de resolución, prompt Claude actualizado
  Fase 5  (v5.0.0) — Near-Zero + Wallet Tracker: entradas near-zero (<8c), señales smart wallets
  Fase 6  (v6.0.0) — Risk Manager: circuit breaker, cash buffer, auto-sizing por rachas, heatmap de riesgo
  Fase 7  (v7.0.0) — ML Models: Warming/Cooling Model, Ensemble Calibrator, pesos adaptativos NOAA/OpenMeteo
  Fase 8  (v8.0.0) — UpDown Markets + Performance Monitor: BTC 5m/15m up/down, learner adaptativo, resource dashboard
  Fase 9  (v9.0.0) — Phantom Bets + Claude Advisor Total: apuestas fantasma para aprendizaje, Claude con acceso completo de lectura + tool update_params, análisis proactivo, win rate fixes
"""

MAJOR = 9
MINOR = 3
PATCH = 66

VERSION = f"{MAJOR}.{MINOR}.{PATCH}"
PHASE   = f"Fase {MAJOR}"
PHASE_NAME = "Capital Buckets + VPS + Phantom Analysis"

FULL_LABEL  = f"v{VERSION} — {PHASE}: {PHASE_NAME}"
SHORT_LABEL = f"v{VERSION}"

PHASES = {
    1: "Bot base — clima + Polymarket CLOB",
    2: "Patrones avanzados — category tracker, contrarian, exit monitor",
    3: "Backtesting + Screener — backtest 4 estrategias, capital velocity",
    4: "Lawyer's Edge — ICAO, boundary zones, reglas de resolución",
    5: "Near-Zero + Wallet Tracker — entradas <8c, señales smart wallets",
    6: "Risk Manager — circuit breaker, cash buffer, auto-sizing",
    7: "ML Models — Warming Model, Ensemble Calibrator adaptativo",
    8: "UpDown Markets — BTC 5m/15m, learner adaptativo, performance monitor",
    9: "Phantom Bets + Claude Advisor + Telonex — on-chain OFI, smart wallet flow, UI panel",
    10: "Capital Buckets — pool manual, buckets por mercado, stake return on WIN, cleanup main.py",
}
