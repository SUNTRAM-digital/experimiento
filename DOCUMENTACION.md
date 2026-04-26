# WeatherBot Polymarket — Documentación Completa
**Versión:** v9.6.3 | **Fase:** 9 — Phantom lead signal (conf 65-95%)

---

## Índice
1. [¿Qué es WeatherBot?](#1-qué-es-weatherbot)
2. [Cómo iniciar el bot](#2-cómo-iniciar-el-bot)
3. [Arquitectura general](#3-arquitectura-general)
4. [Estrategias de trading](#4-estrategias-de-trading)
5. [Sistema Phantom](#5-sistema-phantom)
6. [Lead Signal (señal principal)](#6-lead-signal-señal-principal)
7. [Parámetros completos](#7-parámetros-completos)
8. [Archivos del proyecto](#8-archivos-del-proyecto)
9. [Panel de control (UI)](#9-panel-de-control-ui)
10. [Aprendizaje adaptativo](#10-aprendizaje-adaptativo)
11. [Risk Manager](#11-risk-manager)
12. [Historial de versiones](#12-historial-de-versiones)

---

## 1. ¿Qué es WeatherBot?

Bot de trading para [Polymarket](https://polymarket.com) especializado en **mercados binarios BTC UP/DOWN** de corto plazo (5 minutos y 15 minutos). Originalmente empezó con mercados de clima (temperatura US), pero evolucionó a mercados de precio de Bitcoin.

El bot opera en tres modos:
- **Phantom (simulado):** aprende sin gastar dinero real. Registra predicciones y verifica resultados.
- **Phantom Real:** usa dinero real pero con la lógica del sistema phantom (señal lead ≥55% a T≥8min).
- **Trading Mode:** estrategia de entrada/salida activa dentro de la ventana del mercado.

**Mercados objetivo:** BTC UP/DOWN 15m (principal) y BTC UP/DOWN 5m (secundario).

Cada mercado binario funciona así:
- Al abrir la ventana se establece un `price_to_beat` (precio de referencia BTC vía Chainlink).
- Al cerrar la ventana se compara BTC actual vs `price_to_beat`.
- Si BTC subió → token UP vale $1, token DOWN vale $0.
- Si BTC bajó → token DOWN vale $1, token UP vale $0.

---

## 2. Cómo iniciar el bot

```bash
# Instalar dependencias (primera vez)
pip install -r requirements.txt

# Iniciar bot + UI
python main.py
```

El bot arranca en `http://localhost:8000`. La UI es accesible desde el navegador.

**Variables de entorno requeridas (`.env`):**
```
POLY_PRIVATE_KEY=...       # Clave privada Polymarket (para ejecutar trades reales)
ANTHROPIC_API_KEY=...      # API de Claude (para el advisor)
CMC_API_KEY=...            # CoinMarketCap (datos macro BTC)
TELONEX_API_KEY=...        # Datos on-chain Polymarket (opcional)
```

---

## 3. Arquitectura general

```
main.py                    ← Punto de entrada (FastAPI + Uvicorn puerto 8000)
    ↓
bot.py                     ← Loop principal: escanea mercados cada N minutos
    ↓ llama a
    ├── markets_updown.py  ← Descubre mercados BTC UP/DOWN activos en Polymarket
    ├── price_feed.py      ← Precio BTC en tiempo real + TradingView TA (RSI, MACD, EMA)
    ├── strategy_updown.py ← Evalúa señal TA + lead signal Browniano
    ├── trading_runner.py  ← Estrategia trading (entry/exit intra-ventana)
    └── vps_experiment.py  ← Registro phantom + cálculo de tamaño VPS
        ↓
api.py                     ← API REST: controla parámetros, expone stats a la UI
config.py                  ← BotParams: todos los parámetros del bot (~150)
data/params.json           ← Persistencia de parámetros (se lee en cada reinicio)
```

**Flujo de un scan completo:**
1. Bot descubre mercados 15m activos en Polymarket (`markets_updown.py`)
2. Obtiene precio BTC actual y TA multi-timeframe (`price_feed.py`)
3. Calcula `lead_confidence` (señal Browniana) si han pasado ≥8 min (`strategy_updown.py`)
4. Evalúa señal combinada (lead + TA + momentum + on-chain)
5. Si hay señal válida y phantom está habilitado → registra apuesta ficticia
6. Si `phantom_real_enabled=True` → ejecuta trade real en Polymarket
7. Al cierre de ventana → verifica resultado y actualiza learner

---

## 4. Estrategias de trading

### 4.1 Phantom / Lead Signal (estrategia principal activa)

La estrategia más rentable. Basada en matemática Browniana aplicada al movimiento de BTC.

**Principio:** A T=0, el mercado es exactamente 50/50 (no hay ventaja). A T=8min con BTC moviéndose en una dirección, se puede calcular la probabilidad de que esa dirección se mantenga hasta el cierre.

**Fórmula:**
```
lead_pct = (btc_ahora - price_to_beat) / price_to_beat × 100
sigma    = lead_pct / (vol_por_min × √minutos_restantes)
confianza = Φ(sigma)   ← distribución normal acumulada
```

**Ejemplo práctico:**
- `price_to_beat = $50,000` (BTC al abrir ventana)
- A T=8min: `btc_ahora = $50,300` → `lead_pct = +0.6%`
- `minutos_restantes = 7` → `sigma ≈ 2.27`
- `confianza = Φ(2.27) ≈ 88%`

**Tiers de confianza y stakes:**
| Confianza | Tier | Stake real |
|-----------|------|------------|
| ≥65% | aggressive | $10 |
| 50–64% | high | $8 |
| 35–49% | moderate | $6 |
| <35% | minimal | $3 |

**Activación:** Solo activa cuando `elapsed_minutes ≥ 8.0` y `lead_pct > 0.05%`.

### 4.2 Trading Mode (entrada/salida intra-ventana)

Estrategia de volatilidad: compra el token que más se ha movido y vende cuando alcanza el target.

**Parámetros clave:**
- `trading_buy_probable = true` → compra en rango 45-85¢ (mercados 50/50)
- `trading_probable_min_price = 0.45` → precio mínimo de entrada
- `trading_probable_profit_offset = 0.15` → target = entrada + 0.15 (toma ganancia rápida)
- `trading_sl_trigger_drop = 0.45` → stop-loss cuando precio cae 45%
- `trading_stake_usdc = 3.0` → stake base

**Stop-Loss (v9.6.2):**
- Ciclo 1: precio cae ≥45% → arma el SL (`sl_armed_ts`)
- Ciclo 2 (~15 segundos después): dispara `STOP_LOSS` sin condiciones → recupera ~55% del stake

**Estados de salida:**
- `TARGET_HIT` — precio alcanzó el target → ganancia
- `STOP_LOSS` — precio cayó ≥45% y se armó en ciclo anterior → pérdida parcial (~45%)
- `PANIC_EXIT` — precio cayó ≥80% → salida de emergencia
- `FORCED_EXIT` — mercado a punto de cerrar (≤2 min) → salida al precio actual

### 4.3 UpDown TA (señal técnica de respaldo)

Señal combinada de múltiples indicadores técnicos vía TradingView:
- RSI, MACD, Stochastics, Bollinger Bands, EMA stack
- Momentum de precio BTC en la ventana
- On-chain: OFI real (Order Flow Imbalance) + smart wallet bias (Telonex)
- Funding rate de Binance perpetuals

**Pesos de la señal combinada:**
```
combined = ta_raw × 0.40 + momentum × 0.25 + regime × 0.15 + telonex × 0.20
```

**Thresholds para operar:**
- `phantom_min_conf_pct = 35%` — confianza mínima phantom
- `phantom_ta_mom_gate = true` — requiere que TA y momentum apunten en la misma dirección
- `phantom_min_elapsed_15m = 8.0` — no operar antes de T=8min

---

## 5. Sistema Phantom

El sistema phantom es el **motor de aprendizaje** del bot. Registra predicciones, verifica resultados y aprende patrones.

### 5.1 Flujo de un trade phantom

```
Mercado activo detectado
    ↓
Lead signal calculado (T≥8min)
    ↓
¿Hay dirección clara (UP/DOWN) con confianza ≥35%?
    ↓ Sí
Registrar en _updown_phantom_pending[slug]
    ↓
¿phantom_real_enabled=True?
    ├── No → Solo registrar como ficticio
    └── Sí → ¿phantom_real_always=True o bot principal no opera?
                 ↓ Sí
         Ejecutar trade real en Polymarket (stake VPS)
         ↓
Al cierre de ventana: verificar resultado
    ↓
Actualizar updown_learner + phantom_learner + vps_experiment
```

### 5.2 Controles del sistema phantom (UI)

**Botón "FICTICIO / REAL+FICTICIO":**
- `FICTICIO` → `phantom_real_enabled = false`. Solo aprende, no gasta dinero.
- `REAL+FICTICIO` → `phantom_real_enabled = true`. Puede ejecutar trades reales.

**Botón "SIEMPRE: OFF / ON":**
- `OFF` (seguro) → phantom real solo opera cuando el bot principal NO tiene señal.
- `ON` (agresivo) → phantom real opera en CADA señal válida (lead ≥55%, T≥8min), aunque el bot principal también opere. **Este modo captura el 95%+ de WR.**

**Toggles de intervalo:**
- `Phantom 5m: ON/OFF` → habilita/deshabilita phantom en mercados 5 minutos
- `Phantom 15m: ON/OFF` → habilita/deshabilita phantom en mercados 15 minutos

### 5.3 Gates de calidad (filtros antes de registrar)

Los gates protegen de entradas de baja calidad. Si un gate falla, el bot reintenta en el siguiente scan (no bloquea el slug).

| Gate | Condición | Acción si falla |
|------|-----------|-----------------|
| `low-conf` | confianza < 35% | retry next scan |
| `TA/mom` | TA y momentum no coinciden | retry next scan (omitido si usa lead signal) |
| `too-early` | elapsed < 8min | retry next scan (omitido si usa lead signal) |
| `deadzone` | confianza en [20-34%] | skip (bloquea slug si T≥8min) |
| `NEUTRAL` | señal sin dirección | skip (bloquea slug si T≥8min) |

### 5.4 Columna "motivo_skip" en la tabla

Explica el contexto cuando se registró el phantom:
- `traded_real` → el bot principal ya hizo un trade real en este mercado
- `no_signal` → el bot principal no tenía señal suficiente

Este campo **no** indica si se usó dinero real — solo describe el contexto.

---

## 6. Lead Signal (señal principal)

La señal más confiable del bot. Basada en movimiento de BTC medido contra `price_to_beat`.

**Código:** `strategy_updown.py → calc_lead_confidence()`

**Parámetros:**
- `btc_now` — precio BTC actual
- `price_to_beat` — precio BTC al abrir la ventana (vía Chainlink/Polymarket)
- `elapsed_minutes` — minutos transcurridos desde que abrió el mercado
- `minutes_remaining` — minutos hasta que cierra
- `btc_vol_per_min = 0.10` — volatilidad BTC en % por minuto (calibrada empíricamente)

**Retorna:**
```python
{
  "direction":   "UP" | "DOWN" | "NEUTRAL",
  "confidence":  float,      # 0-99%
  "lead_pct":    float,      # % de ventaja BTC sobre price_to_beat
  "sigma":       float       # número de desviaciones estándar
}
```

**Cuándo es NEUTRAL:**
- `elapsed < 8 min` → demasiado pronto, mercado todavía 50/50
- `|lead_pct| < 0.05%` → BTC casi no se ha movido
- `minutes_remaining ≤ 0.5` → demasiado tarde para operar
- `price_to_beat = 0` → dato no disponible

---

## 7. Parámetros completos

Todos los parámetros son ajustables en tiempo real desde la UI sin reiniciar el bot.
Se persisten en `data/params.json`.

### 7.1 Phantom

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `phantom_real_enabled` | `false` | Activa uso de dinero real en phantom |
| `phantom_real_always` | `false` | Opera con real SIEMPRE (no solo cuando bot principal falla) |
| `phantom_5m_enabled` | `true` | Habilita phantom en mercados 5m |
| `phantom_15m_enabled` | `true` | Habilita phantom en mercados 15m |
| `phantom_1d_enabled` | `false` | Habilita phantom en mercados 1d |
| `phantom_pool_usdc` | `20.0` | Capital total asignado al sistema phantom |
| `phantom_bucket_15m_usdc` | `20.0` | Saldo disponible para trades 15m |
| `phantom_bucket_5m_usdc` | `0.0` | Saldo disponible para trades 5m |
| `phantom_min_conf_pct` | `35.0` | Confianza mínima para registrar phantom |
| `phantom_ta_mom_gate` | `true` | Requiere alineación TA + momentum |
| `phantom_min_elapsed_15m` | `8.0` | Mínimo de minutos transcurridos para operar 15m |
| `phantom_deadzone_enabled` | `false` | Activa zona muerta de confianza |
| `phantom_deadzone_min_conf` | `20.0` | Inicio zona muerta (%) |
| `phantom_deadzone_max_conf` | `34.0` | Fin zona muerta (%) |

### 7.2 Trading Mode

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `trading_mode_enabled` | `true` | Activa estrategia trading |
| `trading_real_enabled` | `false` | Permite trades reales en trading mode |
| `trading_15m_enabled` | `true` | Opera en mercados 15m |
| `trading_5m_enabled` | `false` | Opera en mercados 5m |
| `trading_stake_usdc` | `3.0` | Stake base por trade |
| `trading_buy_probable` | `true` | Modo probable (acepta precios 45-85¢) |
| `trading_probable_min_price` | `0.45` | Precio mínimo de entrada en modo probable |
| `trading_probable_max_price` | `0.85` | Precio máximo de entrada en modo probable |
| `trading_probable_profit_offset` | `0.15` | Target = entrada + offset |
| `trading_min_entry_minutes_left` | `1.0` | Minutos mínimos restantes para entrar |
| `trading_min_elapsed_for_entry` | `8.0` | Minutos mínimos transcurridos para entrar |
| `trading_exit_deadline_min` | `2.0` | Salida forzada N minutos antes del cierre |
| `trading_sl_enabled` | `true` | Activa stop-loss |
| `trading_sl_trigger_drop` | `0.45` | % de caída para armar SL (45%) |
| `trading_sl_wait_min` | `0.0` | Minutos de espera tras armar SL (0 = inmediato) |
| `trading_panic_trigger_drop` | `0.80` | % de caída para PANIC_EXIT (80%) |
| `trading_max_entries_per_market` | `3` | Máximo de entradas por mercado |
| `trading_max_open_per_side` | `2` | Máximo de posiciones abiertas por lado |
| `trading_one_open_at_a_time` | `true` | Solo una posición abierta simultánea |
| `trading_stake_tier_60` | `5.0` | Stake cuando confianza ≥60% |
| `trading_stake_tier_70` | `10.0` | Stake cuando confianza ≥70% |
| `trading_stake_tier_80` | `15.0` | Stake cuando confianza ≥80% |
| `trading_stake_tier_90` | `20.0` | Stake cuando confianza ≥90% |

### 7.3 UpDown General

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `updown_enabled` | `true` | Activa estrategia UpDown |
| `updown_5m_enabled` | `true` | Habilita mercados 5m |
| `updown_15m_enabled` | `true` | Habilita mercados 15m |
| `updown_max_usdc` | `3.1` | Stake máximo por trade UpDown |
| `updown_max_consecutive_losses` | `3` | Detiene operaciones tras N pérdidas seguidas |
| `updown_15m_min_confidence` | `0.20` | Confianza mínima señal 15m |
| `updown_5m_min_confidence` | `0.20` | Confianza mínima señal 5m |
| `updown_stake_min_usdc` | `1.5` | Stake mínimo dinámico |
| `updown_stake_max_usdc` | `5.0` | Stake máximo dinámico |
| `updown_stake_conf_min_pct` | `35.0` | Confianza mínima para stake dinámico |
| `updown_stake_conf_max_pct` | `100.0` | Confianza máxima para stake dinámico |

### 7.4 Risk Manager

| Parámetro | Default | Descripción |
|-----------|---------|-------------|
| `max_daily_loss_pct` | `0.20` | Pérdida máxima diaria (20% del capital) |
| `circuit_breaker_enabled` | `false` | Activa circuit breaker automático |
| `trading_real_max_exposure_usdc` | `100.0` | Exposición máxima real simultánea |
| `trading_real_daily_loss_limit_usdc` | `999999` | Límite de pérdida diaria en trading real |
| `trading_real_max_consec_losses` | `5` | Máx. pérdidas consecutivas antes de detener |
| `trading_real_drawdown_halt_pct` | `0.40` | Detiene trading si drawdown supera 40% |
| `trading_paper_required_days` | `7` | Días de paper trading requeridos antes de real |
| `trading_paper_required_trades` | `100` | Trades paper requeridos antes de real |
| `trading_paper_required_wr` | `0.75` | Win rate mínimo en paper para activar real |
| `trading_paper_gate_override` | `true` | Salta el paper gate (override manual) |

---

## 8. Archivos del proyecto

### Código principal

| Archivo | Propósito |
|---------|-----------|
| `main.py` | Punto de entrada. Inicia FastAPI + Uvicorn en puerto 8000 |
| `bot.py` | Loop principal. Orquesta todos los scans y estrategias |
| `api.py` | API REST. Controla el bot desde la UI y expone estadísticas |
| `config.py` | Clase `BotParams` con todos los parámetros del bot |
| `version.py` | Versión actual y historial de fases |

### Estrategias

| Archivo | Propósito |
|---------|-----------|
| `strategy_updown.py` | Señal TA + `calc_lead_confidence()` para mercados BTC UP/DOWN |
| `strategy_trading.py` | Lógica de entrada/salida de Trading Mode (compra cheap/vende target) |
| `trading_runner.py` | Ejecuta ciclos de Trading Mode: abre posiciones, monitorea, cierra |
| `strategy.py` | Estrategia base para mercados de clima (EV, Kelly, patrones wallets) |
| `strategy_btc.py` | Estrategia para mercados de precio BTC directional |
| `strategy_nearzero.py` | Estrategia Near-Zero: entradas <8¢ en mercados con edge claro |

### Mercados y datos

| Archivo | Propósito |
|---------|-----------|
| `markets_updown.py` | Descubre mercados BTC UP/DOWN activos en Polymarket |
| `markets.py` | Descubre mercados de temperatura US + cliente CLOB para precios |
| `markets_btc.py` | Descubre y parsea mercados de precio BTC directional |
| `price_feed.py` | Precio BTC (Binance→Coinbase→CMC) + TradingView TA multi-timeframe |
| `telonex_data.py` | Datos on-chain: OFI real, smart wallet flow, fills Polymarket |

### Aprendizaje

| Archivo | Propósito |
|---------|-----------|
| `updown_learner.py` | Aprende de trades UpDown reales: buckets por señal/RSI/timing/lado |
| `phantom_learner.py` | Aprende exclusivamente de trades phantom VPS |
| `trading_learner.py` | Aprende de Trading Mode: sugiere parámetros adaptativos |
| `vps_experiment.py` | Experimento Variable Position Sizing: compara stake fijo $3 vs dinámico $3-$10 |
| `phantom_optimizer.py` | Optimizador autónomo: ajusta parámetros phantom automáticamente |
| `phantom_analysis.py` | Análisis de patrones en historial phantom |

### Risk y gestión

| Archivo | Propósito |
|---------|-----------|
| `risk_manager.py` | Circuit breaker, cash buffer, auto-sizing por rachas, heatmap de riesgo |
| `trading_positions.py` | Gestión de posiciones abiertas en Trading Mode |
| `exit_manager.py` | Disposition Coefficient + Swing Trading (detecta cuándo salir) |
| `performance_monitor.py` | Monitor de recursos del sistema (CPU, memoria, tiempos) |

### Análisis inteligente

| Archivo | Propósito |
|---------|-----------|
| `claude_analyst.py` | Analista Claude: evalúa oportunidades con acceso total de lectura |
| `screener.py` | Screener: identifica mercados con edge real antes de operar |
| `category_tracker.py` | Rastreo de win rate por categoría de mercados |
| `wallet_tracker.py` | Smart Wallet Tracker: detecta actividad de wallets expertos |

### Data persistida (`data/`)

| Archivo | Contenido |
|---------|-----------|
| `params.json` | Parámetros actuales del bot (se carga en cada reinicio) |
| `bot_param_history.json` | Historial de cambios de parámetros |
| `updown_stats.json` | Estadísticas UpDown por buckets (señal, RSI, timing, lado) |
| `phantom_learner_stats.json` | Win rates phantom por tier de confianza |
| `vps_phantom_experiment.json` | Todos los trades phantom con resultado y PnL |
| `trading_positions.json` | Posiciones de trading abiertas y cerradas |
| `phantom_optimizer_state.json` | Estado del optimizador phantom |
| `advisor_notifications.json` | Notificaciones del advisor Claude |
| `logs.json` | Últimas 500 líneas de log del bot |

---

## 9. Panel de control (UI)

Accesible en `http://localhost:8000` después de iniciar `python main.py`.

### Sección UpDown

**Controles phantom:**
- `FICTICIO / REAL+FICTICIO` — toggle principal de dinero real
- `SIEMPRE: OFF/ON` — modo agresivo (opera aunque el bot principal también opere)
- `Phantom 5m / 15m / 1d` — toggles por intervalo

**Tabla "Registros de Operaciones":**
Muestra todos los trades phantom con:
- Hora, Mercado, Lado (UP/DOWN), Confianza %, Tier
- BTC inicio y BTC cierre (con Δ%)
- Size VPS (stake hipotético), Fixed $3 (baseline)
- Resultado (WIN/LOSS/PENDING)
- P&L VPS, P&L Fixed
- **Retorno** (stake + ganancia total devuelta)
- Δ (diferencia VPS vs Fixed)

**Cómo leer el Retorno:**
- WIN con $3 → `$5.94` (recuperas $3 + $2.94 de ganancia)
- LOSS con $3 → `$0.00` (pierdes el stake completo)
- PENDING → `—` (mercado aún no resuelto)

### Sección Trading

Controles de Trading Mode con posiciones abiertas en tiempo real, historial de trades y PnL acumulado.

---

## 10. Aprendizaje adaptativo

### VPS Experiment (principal)

El experimento VPS compara dos estrategias en paralelo durante 7 días:
- **VPS (Variable Position Sizing):** stake variable según confianza ($3-$10)
- **Fixed:** stake siempre $3

Resultado actual (28 trades): VPS genera ~2.5× más ganancia que Fixed en señales de alta confianza.

**Tiers VPS:**
```
≥65% conf → $10  (tier: aggressive)
50-64%    → $8   (tier: high)      ← 18 trades, 100% WR
35-49%    → $6   (tier: moderate)  ← 5 trades, 100% WR
20-34%    → $4   (tier: low_moderate)
<20%      → $3   (tier: minimal)
```

### Phantom Learner

Aprende exclusivamente de trades phantom. Rastrea:
- Win rate por tier de confianza
- Win rate por lado (UP vs DOWN)
- Win rate por timing (temprano/medio/tarde en la ventana)
- Correlación momentum → resultado

### UpDown Learner

Aprende de todos los trades (reales + phantom). Puede:
- Sugerir invertir la señal si win rate < 30% (señal invertida sistemáticamente)
- Ajustar gates de entrada si ciertos patrones pierden sistemáticamente
- Detectar preferencia por UP o DOWN en cada intervalo

---

## 11. Risk Manager

Protecciones automáticas contra pérdidas excesivas.

**Circuit Breaker** (`circuit_breaker_enabled`):
- Se activa si pérdida diaria supera `max_daily_loss_pct`
- Detiene todos los trades nuevos hasta reset manual

**Kill Switch de Trading Real** (`trading_real_killed`):
- Se activa automáticamente si:
  - Drawdown supera `trading_real_drawdown_halt_pct` (40%)
  - Pérdidas consecutivas superan `trading_real_max_consec_losses` (5)
  - Pérdida diaria supera `trading_real_daily_loss_limit_usdc`
- Reset manual desde la UI

**Paper Gate** (`trading_paper_gate_override`):
- Bloquea trading real hasta cumplir:
  - 7 días de paper trading
  - 100 trades paper
  - Win rate ≥75% en paper
- Override manual disponible desde UI

---

## 12. Historial de versiones

| Versión | Cambio principal |
|---------|-----------------|
| v9.6.3 | Phantom usa lead signal Browniano (65-95% conf) en lugar de TA (30-50%) |
| v9.6.2 | SL inmediato (dispara en 2do ciclo ~15s), profit offset 0.15, stake base $3 |
| v9.6.1 | Log phantom deshabilitado vs ya registrado (mensajes separados) |
| v9.6.0 | Late-entry T≥8min + CLOB flow + stakes dinámicos $3-$20 |
| v9.5.8 | Trading mode guiado por señal (signal_direction inyectado desde opp) |
| v9.5.7 | Phantom gates no bloquean retry del mismo mercado |
| v9.5.6 | Phantom 3 filtros de calidad (min_conf, TA+mom, elapsed) |
| v9.5.5 | Trading mode toggles 5m/15m/1d + R/R ajustado |
| v9.5.4 | Phantom filtro zona muerta de confianza (skip 20-34%) |
| v9.0.0 | Phantom Bets + Claude Advisor: apuestas fantasma, Claude con acceso total |
| v8.0.0 | UpDown Markets + Performance Monitor: BTC 5m/15m, learner adaptativo |
| v7.0.0 | ML Models: Warming Model, Ensemble Calibrator, pesos adaptativos |
| v6.0.0 | Risk Manager: circuit breaker, cash buffer, auto-sizing por rachas |
| v5.0.0 | Near-Zero + Wallet Tracker: entradas <8¢, señales smart wallets |
| v4.0.0 | Lawyer's Edge: parser ICAO, boundary zones, reglas de resolución |
| v3.0.0 | Backtesting + Screener: 4 estrategias, capital velocity |
| v2.0.0 | Patrones avanzados: category tracker, contrarian, exit monitor |
| v1.0.0 | Bot base: mercados de clima, integración Polymarket CLOB |

---

*Documentación generada para WeatherBot v9.6.3 — 2026-04-26*
