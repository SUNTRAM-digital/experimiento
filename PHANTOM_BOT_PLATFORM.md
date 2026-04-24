# Phantom Bot Platform — Spec standalone

Proyecto nuevo: plataforma **solo-phantom** que hospeda cuatro bots independientes. Sin dinero real, sin CLOB live. Objetivo: entrenar, medir y comparar estrategias en paper antes de portar a producción.

Derivado de weatherbot v9.4 (abril 2026). Toda la lógica aquí descrita ya existe en el monorepo actual; esta guía la extrae en su contrato para re-implementarla aislada.

---

## 1. Inventario de bots

| Bot | Interval | Mercado | Salida |
|---|---|---|---|
| **Trading 5m** | 5 min | Bitcoin Up/Down 5-min | target fijo o SL escalonado |
| **Trading 15m** | 15 min | Bitcoin Up/Down 15-min | target fijo o SL escalonado |
| **UpDown 5m** | 5 min | Bitcoin Up/Down 5-min | predicción binaria hold-to-resolution |
| **UpDown 15m** | 15 min | Bitcoin Up/Down 15-min | predicción binaria hold-to-resolution |

Los dos bots **Trading** operan como scalper: compran token ≤ umbral de precio, venden al target (entry + offset) si llega, o ejecutan stop-loss escalonado. Los dos bots **UpDown** predicen la dirección y sostienen la posición hasta la resolución del mercado.

Todos usan phantom virtual balance (USDC simulado) — nunca firman transacciones reales.

---

## 2. Arquitectura

```
app/
 ├─ api.py              # FastAPI: dashboard, endpoints, streaming chat
 ├─ markets.py          # Polymarket CLOB read-only (books, prices, mercados)
 ├─ strategy_trading.py # Trading Mode: entry/exit dataclass + funciones puras
 ├─ strategy_updown.py  # UpDown: features ML + predictor
 ├─ trading_runner.py   # Loop trading (evaluate_and_open, monitor_and_close)
 ├─ updown_runner.py    # Loop updown (scan_and_predict)
 ├─ trading_positions.py# DAL posiciones phantom (open/close/patch, stats, drawdown)
 ├─ bot_brain.py        # "Cerebro" por bot: stats, notas, aprendizajes
 ├─ config.py           # BotParams con to_dict()/update()/save() (JSON-backed)
 └─ data/
     ├─ params.json          # parámetros del bot
     ├─ positions.json       # histórico phantom (trading + updown)
     ├─ brain_<bot_id>.json  # memoria del cerebro por bot
     └─ logs/…
```

Frontend: `static/index.html` single-page app. Tabs: Dashboard / Trading Mode / UpDown / Bots / Claude Chat.

---

## 3. Parámetros (BotParams)

Fuente: [config.py](config.py). Persistidos en `data/params.json`. Expuestos al frontend via `GET /api/params`, editables via `POST /api/params` o via tool `update_params` del chat.

### 3.1 Trading Mode (scalper)

| Key | Default | Rol |
|---|---|---|
| `trading_mode_enabled` | `true` | toggle maestro |
| `trading_entry_threshold` | `0.55` | deprecated (superado por min/max entry) |
| `trading_min_entry_price` | `0.10` | floor — no comprar si token < X (mercado muerto) |
| `trading_max_entry_price` | `0.30` | ceiling — garantiza R:R ≥ 1 |
| `trading_profit_offset` | `0.30` | vender en `entry + offset` |
| `trading_stake_usdc` | `5.0` | tamaño por entrada |
| `trading_exit_deadline_min` | `1.0` | forzar salida a T-X min del cierre |
| `trading_min_entry_minutes_left` | `1.0` | no entrar si faltan menos |
| `trading_max_entries_per_market` | `8` | ciclos por mercado |
| `trading_one_open_at_a_time` | `true` | evitar apilar posiciones |

**Stop-loss escalonado** (punto 12 del roadmap original):

| Key | Default | Rol |
|---|---|---|
| `trading_sl_enabled` | `true` | |
| `trading_sl_trigger_drop` | `0.50` | 50% caída vs entry arma timer |
| `trading_sl_wait_min` | `3.0` | esperar N min tras trigger antes de vender |
| `trading_sl_min_recover_factor` | `0.50` | vender si bid ≥ entry/2 |
| `trading_panic_trigger_drop` | `0.80` | 80% caída → panic salvage |
| `trading_panic_min_recover_factor` | `0.33` | (informativo; panic vende a cualquier bid>0) |

**Buy-probable vs cheapest** (punto 14):

| Key | Default | Rol |
|---|---|---|
| `trading_buy_probable` | `true` | `true` = comprar favorito (precio alto, offset bajo); `false` = modo cheapest legacy |
| `trading_probable_min_price` | `0.55` | floor en modo probable |
| `trading_probable_max_price` | `0.85` | ceiling en modo probable |
| `trading_probable_profit_offset` | `0.08` | offset menor (favorito ya cerca de resolver) |

**Preflight gates** (punto 19, derivado de imágenes 1-19):

| Key | Default | Rol |
|---|---|---|
| `trading_real_drawdown_halt_pct` | `0.40` | kill definitivo si cumulative PnL cae 40% desde ATH |
| `trading_paper_required_days` | `7.0` | días mínimos en phantom antes de real |
| `trading_paper_required_trades` | `200` | trades mínimos |
| `trading_paper_required_wr` | `0.75` | WR mínimo |
| `trading_paper_gate_override` | `false` | bypass explícito |
| `trading_max_price_age_sec` | `10.0` | rechaza decisión si `market['price_ts']` > N seg |

### 3.2 UpDown

| Key | Default | Rol |
|---|---|---|
| `updown_enabled` | `true` | toggle maestro |
| `updown_5m_enabled` / `updown_15m_enabled` | `true` | per-interval |
| `updown_max_usdc` | — | tope total diario |
| `updown_max_consecutive_losses` | `3` | pausa tras N pérdidas seguidas |
| `updown_15m_min_confidence` / `updown_5m_min_confidence` | `0.65` | umbral de confianza del ML |
| `updown_15m_momentum_gate` / `updown_5m_momentum_gate` | gating por momentum CEX |
| `updown_stake_min_usdc` / `updown_stake_max_usdc` | stake dinámico |
| `updown_stake_conf_min_pct` / `updown_stake_conf_max_pct` | mapeo confianza → stake |
| `updown_displacement_hi_pct` / `updown_displacement_lo_pct` | gate displacement |

---

## 4. Cerebro por bot

Cada bot tiene un "cerebro" persistido en `data/brain_<bot_id>.json`. Estructura:

```json
{
  "bot_id": "trading_5m",
  "name": "Trading 5m",
  "stats": {
    "total_trades": 142,
    "wins": 87,
    "losses": 55,
    "win_rate": 0.613,
    "realized_pnl_usdc": 18.42,
    "best_streak": 7,
    "worst_streak": 4
  },
  "notes": [
    {"ts": 1745400000, "tag": "insight",
     "text": "WR cae a 45% cuando minutes_to_close < 2. Ajustar min_entry_minutes_left a 2.5."}
  ],
  "learnings": [
    {"ts": 1745401000, "key": "optimal_offset_5m", "value": 0.32,
     "confidence": 0.72, "n_samples": 60}
  ],
  "updated_ts": 1745410000
}
```

Endpoints:
- `GET /api/brain/{bot_id}` — leer cerebro completo
- `POST /api/brain/{bot_id}/note` — añadir nota
- `POST /api/brain/{bot_id}/learning` — registrar aprendizaje (valor + muestras)
- `POST /api/brain/{bot_id}/chat` — chat contextual (Claude con el cerebro cargado)

Claude puede leer el cerebro via tool `get_bot_brain(bot_id)` y escribir via `add_brain_note(bot_id, text, tag)`.

---

## 5. Algoritmos

### 5.1 Trading — entry (strategy_trading.evaluate_entry_verbose)

Pseudo:

```
if buy_probable:
    floor, ceiling, offset = probable_min_price, probable_max_price, probable_profit_offset
else:
    floor, ceiling, offset = min_entry_price, max_entry_price, profit_offset

for side in [UP, DOWN]:
    price = market[f"{side}_price"]
    if price < floor: skip("floor")
    if price > ceiling: skip("ceiling")
    if price + offset > 1.0: skip("target > 1")  # imposible ganar
    candidates.append((side, price))

# Orden:
if buy_probable: sort desc (favorito primero)
else:            sort asc (más barato primero)

# Reversal gate (solo cheapest):
if not buy_probable and best_price < 0.35:
    if market[opposite_side_price] > 0.80:
        return None, "perdedor vs opposite"

# Gate minutes_to_close, gate one_open_at_a_time, gate max_entries_per_market
return Signal(side, token_id, entry_price=price, target=price+offset, stake)
```

### 5.2 Trading — exit (strategy_trading.should_exit_position)

```
if current >= target: return TARGET_HIT
if minutes_to_close <= exit_deadline_min: return FORCED_EXIT

if sl_enabled:
    drop_pct = 1 - current/entry
    if drop_pct >= panic_trigger_drop and current > 0:
        return PANIC_EXIT   # salvar lo que quede
    if position.sl_armed_ts is None:
        if drop_pct >= sl_trigger_drop:
            position.sl_armed_ts = now  # armar timer
    else:
        mins_armed = (now - sl_armed_ts) / 60
        if mins_armed >= sl_wait_min and current >= entry * sl_min_recover_factor:
            return STOP_LOSS
return None
```

### 5.3 UpDown — predicción

Pipeline:
1. **Features** — precio BTC spot, momentum 5m/15m, volatilidad, orderbook bid/ask imbalance, tiempo al close.
2. **Modelo** — clasificador binario (LogReg + calibración isotónica por interval). Re-entrenable con histórico phantom.
3. **Confianza** — `max(p_up, p_down)`; ventana entra si confianza ≥ umbral.
4. **Displacement gate** — si precio del token del lado favorito ya está fuera de `[displacement_lo, displacement_hi]`, la edge ya se descontó.
5. **Stake** — escalado lineal entre `stake_min_usdc` y `stake_max_usdc` según confianza vs `stake_conf_min_pct..stake_conf_max_pct`.
6. **Sostiene** hasta resolución. No hay monitor_and_close intra-ventana.

### 5.4 Aprendizaje continuo

Dos loops de mejora:

**A. Auto-tuning por bot** (background job hourly):
- Consolida trades cerrados última hora.
- Recomputa métricas: WR per-interval, WR per-hora-del-día, WR per-rango-de-precio.
- Si WR(hoy) < WR(semana) - 10pp → anotar `warning` en cerebro y ajustar `min_confidence` +5pp.
- Si WR(hoy) > 80% sobre ≥ 20 trades → relajar gate en 5pp para explorar más volumen.

**B. Claude-assisted** (on-demand):
- Usuario pregunta en chat: "¿qué mejorarías del bot trading 15m?"
- Backend construye contexto: stats del bot + últimos 20 trades + params actuales + notas del cerebro.
- Claude responde con sugerencias numéricas + justificación.
- Si el usuario dice "aplica eso", Claude llama `update_params({...})` con las claves que mencionó.

---

## 6. Dashboard

Componentes mínimos:

- **Cabecera**: balance phantom total, PnL día, PnL semana, posiciones abiertas.
- **Por bot (4 cards)**:
  - Toggle on/off
  - Balance virtual asignado (editable inline, punto 6 del roadmap original)
  - Stats: trades, wins, losses, WR, realized PnL
  - Last-20 sparkline de PnL acumulado
  - Botón "Abrir cerebro"
- **Tabla de posiciones abiertas** con filtros (`interval ∈ {5,15}`, `side ∈ {UP,DOWN}`, `bot`).
- **Tabla de histórico** con filtros + sort por cualquier columna + export CSV.
- **PnL por interval** (barras: total, 5m, 15m, 1d).
- **Panel Preflight** (banner rojo si drawdown kill-switch activo o paper-gate falla).
- **Chat Claude** (sidebar colapsable, selector de modelo — punto 16).

---

## 7. Registro de operaciones

Cada fila:

```json
{
  "id": "pos_abc123",
  "bot_id": "trading_5m",
  "slug": "bitcoin-up-or-down-2026-04-24-20-05",
  "interval": 5,
  "side": "UP",
  "token_id": "0x...",
  "entry_price": 0.28,
  "target_price": 0.58,
  "stake_usdc": 5.0,
  "entry_ts": 1745410123,
  "entry_hms": "20:08:43",
  "entry_iso": "2026-04-24T20:08:43Z",
  "exit_ts": 1745410420,
  "exit_hms": "20:13:40",
  "exit_iso": "2026-04-24T20:13:40Z",
  "exit_price": 0.58,
  "exit_reason": "TARGET_HIT",
  "pnl_usdc": 5.36,
  "status": "TARGET_HIT",
  "sl_armed_ts": null,
  "streak_reset": false
}
```

Campos claves:
- `entry_hms` / `exit_hms` (punto 11): hora legible para UI sin JS conversion.
- `sl_armed_ts`: timestamp en que se armó el SL (null si nunca).
- `exit_reason ∈ {TARGET_HIT, FORCED_EXIT, STOP_LOSS, PANIC_EXIT, RESOLVED_WIN, RESOLVED_LOSS}`.
- `streak_reset`: si `true`, corta la racha al contarla.

Stored en `data/positions.json` con estructura `{phantom: {slug: [...]}, real: {slug: [...]}, meta: {...}}`.

---

## 8. Claude chat (tool use)

Sistema prompt contiene secciones:
1. Identidad: "Eres un copiloto de Phantom Bot Platform…"
2. Estado actual: params + stats + últimos 5 trades por bot.
3. Reglas de modificación: solo cambiar params cuando el usuario lo pida explícitamente.

Tools expuestas:
- `get_bot_stats(bot_id)`
- `get_bot_brain(bot_id)`
- `add_brain_note(bot_id, text, tag)`
- `get_recent_trades(bot_id, limit)`
- `update_params(params: {key: value}, reason: str)` — whitelist de claves (ver sección 3)

Selector de modelo (`/api/chat/models` → `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`). Persistido en localStorage. Enviado en cada request como `body.model`.

---

## 9. Operación & deploy

**Pre-live checklist** (derivado de imagen 18):
- [ ] POLY_API_KEY y POLY_PRIVATE_KEY en `.env`, nunca hardcoded.
- [ ] `py-clob-client==0.9.0`, `websockets==12.0`, `aiohttp==3.9.0` pinned.
- [ ] venv activo antes de `uvicorn`.
- [ ] Min edge: `trading_min_entry_price=0.10`, `trading_max_entry_price=0.30` (R:R ≥ 1 garantizado).
- [ ] WebSocket reconnect logic + stale data check (`trading_max_price_age_sec=10`).
- [ ] Kill switch: `trading_real_drawdown_halt_pct=0.40` + consec losses cap.
- [ ] Telegram alerts (opcional) para kill-switch y errores.
- [ ] Heartbeat horario.
- [ ] Log rotation 50MB.
- [ ] Alchemy/Infura RPC (no endpoint público).
- [ ] VPS 24/7, no laptop.

**Paper-to-live** (solo cuando se agregue módulo real):
- Phantom debe correr ≥ 7 días con ≥ 200 trades y WR ≥ 75% antes de habilitar real. Enforcement en `trading_runner._check_real_safety`.

---

## 10. Tests

Cada cambio en la plataforma debe tener test propio. Tests típicos:

- `test_strategy_entry_buy_probable.py` — verifica orden descendente en modo probable.
- `test_strategy_entry_cheapest.py` — verifica orden ascendente + reversal gate.
- `test_exit_sl_timer.py` — arma SL al 50% drop, espera 3 min, vende si bid ≥ entry/2.
- `test_exit_panic.py` — drop 80% → panic exit a cualquier bid>0.
- `test_drawdown_kill_switch.py` — ATH + current → `trading_real_killed=true`.
- `test_paper_gate.py` — bloquea real si faltan días/trades/wr.
- `test_stale_price_rejected.py` — evaluate_and_open skip si `price_ts > 10s`.
- `test_hms_timestamps.py` — open_position setea entry_hms/entry_iso.
- `test_update_params_tool.py` — Claude tool acepta/rechaza claves.
- `test_chat_model_selector.py` — endpoint + whitelist + UI wiring.

Meta: `pytest tests/ --ignore=tests/test_clob_price_fix.py` debe pasar 100%.

---

## 11. Roadmap hacia real

La plataforma phantom es el **gate** hacia real. El orden es estricto:

1. Phantom corre 7+ días. Analizar drawdown, WR por interval, mejor hora del día.
2. Paper-gate pasa → habilitar `trading_paper_gate_override=false` en real.
3. Primeros trades reales con `trading_stake_usdc=1.0` y `trading_real_max_exposure_usdc=5.0`.
4. Comparar WR live vs phantom día a día. Si diverge > 10pp → halt + investigar (slippage / fees / market impact).
5. Subir stake gradualmente solo tras 50 trades live con WR(live) ≥ WR(phantom) - 5pp.

---

## 12. Qué NO incluye esta plataforma

- No ejecuta órdenes reales en Polymarket. Sólo lectura de books/precios.
- No firma transacciones Polygon.
- No hace copy-trading de wallets.
- No usa el stack `weather/`, `btc/`, `telonex/`. Sólo Trading + UpDown.
- No reemplaza weatherbot — es un gemelo de entrenamiento con superficie reducida.
