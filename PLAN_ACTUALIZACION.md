# Plan de Actualización — Weatherbot Polymarket
**Fecha:** 2026-04-06  
**Branch actual:** fase2  
**Estado:** Pendiente de implementación — análisis completo realizado

---

## Contexto del Proyecto

Bot de trading para mercados de clima en Polymarket. Ya implementado:
- NOAA API con coordenadas ICAO correctas
- EV calculation + Kelly Criterion (fraccionado)
- Claude como analista de riesgo (claude_analyst.py)
- Estrategia UpDown y mercados BTC
- Sistema de posiciones, logs, state persistence

Archivos clave: `main.py`, `bot.py`, `api.py`, `config.py`, `strategy.py`, `weather.py`, `claude_analyst.py`, `markets.py`, `strategy_updown.py`, `price_feed.py`

---

## Base de Conocimiento Analizada

Carpeta: `base de conocimiento/` (12 documentos)

### Hallazgos críticos

**El 87.3% de wallets en Polymarket pierde dinero.**  
Los top wallets comparten estos patrones verificados en 112,000 wallets / 400M trades:

1. **Regla de las 72 Horas** — Top wallets hold avg 1.6 días vs 11.3 días el promedio. Mismo edge, 8x más ciclos de capital → 11.4x más retorno anualizado.
2. **Especialización** — Top wallets operan 1.7 categorías avg. Cada categoría adicional cuesta ~6.3% de win rate (decaimiento exponencial). 1-2 categorías: +$4,200 PnL avg. 5+: -$2,100.
3. **Disposition Coefficient** — Top wallets: salen de ganadores al 91% del valor máximo, cortan pérdidas en -12%. Promedio: salen al 58%, cortan en -41%. Diferencia: 4.6x en calidad de exits.
4. **Entrada Contrarian** — Cuando el mercado llega a 88%+, los tops VENDEN YES. Cuando cae a 12%-, COMPRAN. Solo entran con desviación >6% entre precio y prob real.
5. **Swing Trading** — Compran a $0.40, salen a $0.65 cuando edge restante <5%. Nunca hold to settlement. Algunos wallets top tienen CERO posiciones resueltas.

**Fórmulas clave:**
```
EV = P_true × (1 - P_market) - (1 - P_true) × P_market  → solo operar si EV > 5%
Kelly(¼) = ((p × b - q) / b) × 0.25
Annualized = (1 + edge)^(365/hold_days) - 1
WR(n) = WR_base × e^(-0.065×n)  [decaimiento por categorías]
EXIT when: (estimated_prob - current_price) < 0.05
STOP when: prob < entry_price - 0.12
```

**Weather-specific:**
- Resolución en estaciones ICAO exactas: NYC=KLGA, Dallas=KDAL (no DFW), etc.
- Diferencia ciudad vs aeropuerto: 3–8°F → trades perdidos si se usa app de clima
- Multi-modelo: GFS + ECMWF + ensemble. Si 3 modelos coinciden en 80%+ → prob muy alta
- Kalman gain: 6am=20% observación real / noon=72% / 1pm+=85%
- Pico horario Shanghai: 11-13h (verano el 27.6% de los días a las 12h)
- Foco en mercados que resuelven en <24h → menor varianza, más edge

**Top wallets de referencia:**
- `gopfan2` — $2M+ weather. Compra YES <15¢, NO >45¢. Riesgo <$1/pos. NYC+London.
- `securebet` — $7→$640 (+9244%). NOAA data. NYC+Seattle. 3,077 predicciones.
- `Hans323` — $1.1M en un trade de temperatura en Londres.
- `BeefSlayer` — $49K weather. Atlanta 0.2¢ → $2,984 profit (49,744%).
- `meropi` — $30K en micro-bets $1-3 con multiplicadores hasta 500x.

**Herramientas recomendadas:**
- Open-Meteo (gratis, sin key) — GFS + ECMWF + ensemble
- Visual Crossing API — histórico calibración ICAO
- Wethr.net — multi-model para Polymarket weather traders
- Tropical Tidbits — GFS y ECMWF cada 6 horas
- poly_data (GitHub warproxxx) — 86M+ trades históricos
- py-clob-client (oficial Polymarket) — CLOB execution
- polyterm (NYTEMODEONLY) — whale tracking, insider detection, arb vs Kalshi

---

## Plan por Fases

---

### FASE 1 — Multi-Model Weather Engine
**Archivos a crear/modificar:** `weather.py`, `config.py`, nuevo `weather_ensemble.py`

**Por qué:** El bot usa solo NOAA. Los top traders cross-verifican 3+ modelos. Si 80%+ coinciden → boost de confianza masivo.

**Qué implementar:**
- [ ] Integrar **Open-Meteo API** (gratis, sin key) → GFS + ECMWF + ensemble por hora
- [ ] Integrar **Visual Crossing API** → histórico y calibración por estación ICAO
- [ ] Sistema de **consenso multi-modelo**: si ≥3 modelos coinciden en bucket → confidence boost
- [ ] **Kalman Gain** corrección tiempo real:
  - 6am: 20% observación + 80% forecast
  - Noon: 72% observación + 28% forecast  
  - 1pm+: 85% observación (peak probablemente ya ocurrió)
- [ ] **Patrón de pico horario** por ciudad/temporada (NYC, Chicago, etc.)
- [ ] **Correcciones meteorológicas**: descuento si nubes >70% o viento fuerte
- [ ] **Incertidumbre dinámica**: ajustar std_dev según consenso de modelos (si todos coinciden, reducir std_dev)

---

### FASE 2 — Motor de Decisión Avanzado (5 Patrones)
**Archivos a crear/modificar:** `strategy.py`, `bot.py`, nuevo `exit_manager.py`

**Por qué:** El bot actual entra en cualquier momento y no tiene reglas de salida activas. Los 5 patrones de los top wallets pueden 8-11x el retorno sin cambiar el edge por trade.

**Qué implementar:**

**Patrón 1 — Regla 72 Horas:**
- [ ] Score de prioridad: mercados que resuelven en <72h reciben boost
- [ ] Penalizar mercados con >5 días a resolución (peor edge/tiempo)

**Patrón 2 — Especialización (Win Rate Decay):**
- [ ] Modo "weather-only" hardcodeado como default
- [ ] Tracking de performance por categoría
- [ ] Bloquear automáticamente categorías con win rate <45% después de 20+ trades

**Patrón 3 — Disposition Coefficient (exit_manager.py):**
- [ ] Monitor de posiciones abiertas cada N minutos
- [ ] Calcular: si `(estimated_prob - current_price) < 0.05` → SELL (edge agotado)
- [ ] Stop automático si `current_prob < entry_prob - 0.12`
- [ ] Never hold to settlement si ya se capturó >85% del movimiento

**Patrón 4 — Entrada Contrarian:**
- [ ] Detectar mercados con precio >88% → evaluar SELL YES si prob real <82%
- [ ] Detectar mercados con precio <12% → evaluar BUY YES si prob real >18%
- [ ] Solo actuar con desviación >6%

**Patrón 5 — Swing Trading:**
- [ ] Al comprar, registrar precio de entrada y prob estimada
- [ ] Calcular `remaining_edge = estimated_prob - current_price`
- [ ] Si `remaining_edge < 0.05` y en ganancia → SELL (no esperar settlement)

---

### FASE 3 — Backtest Engine + Value & Momentum Screener
**Archivos a crear:** `backtesting/engine.py`, `backtesting/metrics.py`, `screener.py`

**Por qué:** Sin backtest se opera a ciegas. Sin screener se pierden oportunidades en mercados no monitoreados.

**Qué implementar:**
- [ ] **Value Screener**: comparar precio mercado vs prob estimada → reportar gaps >15%
- [ ] **Momentum Screener**: momentum 7, 14, 30 días. Si los 3 son positivos → señal de calidad
- [ ] **Backtest Engine**:
  - Input: histórico de precios de contratos (via poly_data o gamma API)
  - Estrategias: MACD, RSI mean reversion, momentum, value
  - Output: win rate, profit factor, Sharpe, max drawdown
  - Umbral mínimo rentable: WR>55%, PF>1.5, MaxDD<20%, n>100 trades
- [ ] **Capital Velocity Tracking**: total_volume / avg_capital_deployed (target: >20x)
- [ ] **Bayesian Updating**: cuando llega observación de temperatura actual, actualizar prob
  - `P_new = (P_likelihood × P_prior) / P_evidence`

---

### FASE 4 — The Lawyer's Edge + Information Triage
**Archivos a crear/modificar:** `rules_parser.py`, `claude_analyst.py`

**Por qué:** Los pros "tradean la formulación", no el evento. Un contrato de "settlement price" vs "intraday high" son trades completamente distintos.

**Qué implementar:**
- [ ] **Parser de reglas de resolución** para cada mercado:
  - Estación ICAO exacta (KLGA≠JFK, KDAL≠DFW)
  - Hora exacta de cierre (UTC vs EST)
  - Tipo de dato (settlement price vs intraday high)
  - Unidad (F vs C, METAR integers vs decimales)
- [ ] **Mapa completo de estaciones ICAO** por ciudad en Polymarket:
  - NYC=KLGA, Chicago=KORD, Dallas=KDAL, Atlanta=KATL, Miami=KMIA, Seattle=KSEA, London=EGLL, etc.
- [ ] **Detector de bucket boundary**: alertar cuando forecast está ±1°F del límite → zona de máxima oportunidad
- [ ] **Actualizar System Prompt de Claude Analyst**:
  - Hierarchy: METAR raw > NOAA API > modelos numéricos > apps de clima
  - Añadir evaluación de reglas de resolución del contrato
  - Añadir verificación de fuente de settlement

---

### FASE 5 — Near-Zero Entry Strategy + Smart Wallet Intelligence
**Archivos a crear:** `strategy_nearzero.py`, `wallet_tracker.py`

**Por qué:** Los wallets más rentables acumulan contratos a 2-8¢ semanas antes en outcomes near-certain. Patrón: $8 → $200, repetido cientos de veces.

**Qué implementar:**
- [ ] **Near-Zero Accumulation**:
  - Escanear contratos con precio <8¢
  - Si prob estimada >25% → calcular EV (puede ser enorme: 0.07¢ precio, 0.30 prob = EV +328%)
  - Entrar con posición pequeña ($1-3), horizonte largo
- [ ] **Smart Wallet Tracker**:
  - Monitorear wallets: gopfan2, securebet, Hans323, BeefSlayer (on-chain público)
  - Detectar cuando entran en weather market → señal de validación adicional
  - Filtrar por categoría dominante (no copiar todo)
- [ ] **Insider Activity Detector**:
  - Posiciones inusuales en mercados de baja liquidez (<$100 vol) antes de movimiento
  - Alertar via Telegram cuando detecte patrón

---

### FASE 6 — Risk Manager Profesional
**Archivos a crear/modificar:** `risk_manager.py`, `bot.py`

**Por qué:** La mayoría de bots explotan no por mal edge sino por over-sizing. Una regla de $50K perdida por conviction excesiva.

**Qué implementar:**
- [ ] **Max risk per trade**: 1-5% del bankroll. Nunca más. Configurable en config.py
- [ ] **Cash buffer obligatorio**: mantener siempre ≥30% en cash para oportunidades de caos
- [ ] **Portfolio heatmap**: concentración por ciudad, categoría y horizonte temporal
- [ ] **Auto-sizing escalonado**: $1 → $5 → $10 → $50 → $100 solo después de N rentables consecutivos
- [ ] **Circuit breaker**: pausar bot automáticamente si drawdown >15% en la semana
- [ ] **Security hardening**:
  - Wallet dedicada, fondos mínimos
  - Límites USDC (no unlimited approvals)
  - Alertas Telegram de transacciones >$X

---

### FASE 7 — ML + Predicción Avanzada de Temperatura
**Archivos a crear:** `ml/warming_model.py`, `ml/ensemble_calibrator.py`

**Por qué:** Con suficientes datos históricos, un modelo ML puede superar la distribución normal simple para predecir si la temperatura subirá o bajará respecto al día anterior.

**Qué implementar:**
- [ ] **Warming/Cooling Day Model** (logistic regression):
  - Features: cambio de presión 3h/12h, viento pre-amanecer, nubosidad, tendencia 3 días, mes, temporada, si llovió ayer
  - Output: 5 niveles (Warming/Slight Warming/Stable/Slight Cooling/Cooling) + confidence
  - Precisión esperada: ~80% en invierno, ~63% en otoño
- [ ] **Ensemble Calibration**:
  - Pesos dinámicos por modelo según accuracy histórico por ciudad y temporada
  - Inspirado en: WC+ECMWF con pesos ajustados por tipo de día (soleado vs nublado)
- [ ] **NLP Meteorológico**:
  - Claude analiza boletines de NWS/NOAA en texto libre
  - Extrae: alertas de frente frío, cambios de masa de aire, sistema de presión
  - Convierte a señal de trading
- [ ] **Calibración de confianza**:
  - Si el modelo dice 70%, ~7/10 de esas predicciones deben resolverse YES
  - Recalibrar mensualmente con datos reales de resoluciones

---

## Tabla de Prioridades

| Fase | Impacto Esperado | Complejidad | Orden |
|------|-----------------|-------------|-------|
| 1 — Multi-Model Weather | Alto | Media | **1°** |
| 2 — 5 Patrones Entry/Exit | Muy Alto | Media | **2°** |
| 3 — Backtest + Screener | Alto | Alta | **3°** |
| 4 — Lawyer's Edge + Reglas | Alto | Baja | **4°** |
| 5 — Near-Zero + Wallets | Medio | Media | **5°** |
| 6 — Risk Manager Pro | Alto | Baja | **6°** |
| 7 — ML Avanzado | Muy Alto | Muy Alta | **7°** |

---

## Metodología de Desarrollo con Git

### Principio fundamental
Cada fase se desarrolla en su propia branch. Cada sub-tarea que funcione se commitea inmediatamente. Si algo falla y no se puede corregir, se hace `git checkout` al último commit estable sin perder nada.

---

### Flujo de trabajo por fase

**1. Antes de empezar una fase nueva:**
```bash
# Asegurarse que main está limpio y actualizado
git checkout main
git pull

# Crear branch para la fase
git checkout -b fase3-multimodel-weather   # ejemplo para Fase 1
```

**2. Durante el desarrollo — commits atómicos:**
Commitear cada vez que una pieza funciona de forma aislada. No acumular cambios.
```bash
# Después de cada archivo que funcione:
git add weather_ensemble.py
git commit -m "fase3: add Open-Meteo API client with GFS+ECMWF data"

# Después de integrar con el sistema:
git add weather.py config.py
git commit -m "fase3: integrate multi-model consensus into weather.py"
```

**3. Convención de nombres de commits:**
```
fase{N}: {qué se hizo}

Ejemplos:
fase3: add Open-Meteo API client
fase3: implement Kalman gain real-time correction
fase3: add multi-model consensus scoring
fase3: fix std_dev calibration for ECMWF data
```

**4. Cada vez que el bot corre sin errores con la nueva feature:**
```bash
# Tag de checkpoint estable
git tag -a v{N}.{sub} -m "stable: {descripción}"
# Ejemplo:
git tag -a v3.1 -m "stable: multi-model weather working, NOAA+OpenMeteo"
```

**5. Si algo falla y no se puede corregir:**
```bash
# Ver historial para elegir punto de retorno
git log --oneline -20

# Opción A — volver al último commit estable (sin perder el branch)
git checkout {commit-hash}

# Opción B — resetear el branch al último commit que funcionaba
git reset --hard {commit-hash}

# Opción C — volver a un tag estable
git checkout v3.1
```

**6. Al terminar una fase completa:**
```bash
# Merge a main cuando la fase está 100% funcional
git checkout main
git merge fase3-multimodel-weather --no-ff -m "merge fase3: multi-model weather engine"
git push origin main

# Crear tag de versión completa
git tag -a fase3-complete -m "Fase 3 completa: multi-model weather engine"
```

---

### Estructura de branches esperada

```
main                    ← siempre funcional, solo fases completas
├── fase3-multimodel    ← Fase 1 en desarrollo
├── fase4-patterns      ← Fase 2 (empieza cuando fase3 mergea)
├── fase5-backtest      ← etc.
└── ...
```

---

### Reglas de seguridad

1. **Nunca commitear claves API** — `.env` siempre en `.gitignore`
2. **Nunca hacer `git reset --hard` en `main`** — solo en branches de desarrollo
3. **Si Claude alucina código que rompe el bot** → `git diff` para ver qué cambió → `git checkout {archivo}` para revertir solo ese archivo
4. **Antes de una sesión de desarrollo**, siempre correr el bot y verificar que funciona antes de modificar nada
5. **Revertir archivo individual sin perder otros cambios:**
   ```bash
   git checkout HEAD -- weather.py   # revierte solo weather.py al último commit
   ```

---

### Comandos de emergencia más útiles

```bash
# Ver qué cambió desde el último commit
git diff

# Ver historial con ramas
git log --oneline --graph --all

# Deshacer el último commit (mantiene los cambios en staging)
git reset --soft HEAD~1

# Deshacer el último commit (descarta los cambios)
git reset --hard HEAD~1

# Revertir un archivo específico al estado del último commit
git checkout HEAD -- {archivo.py}

# Ver todos los tags (checkpoints estables)
git tag -l

# Volver a un tag específico en una nueva branch
git checkout -b hotfix v3.1
```

---

## Notas para Retomar Contexto

- La base de conocimiento está en `base de conocimiento/` (12 archivos .txt)
- El análisis completo ya fue hecho — no hace falta re-leer todos los archivos
- Empezar siempre leyendo este archivo + el archivo de fase que se va a implementar
- Antes de cada fase, leer los archivos Python relevantes del proyecto
- Branch de trabajo: `fase2` → crear nueva branch por fase si se quiere aislar cambios
- Wallet de referencia para copy-analysis: `https://polymarket.com/@googoogaga23`
