"""
Mercados BTC Up/Down de corto plazo en Polymarket.

Resuelven: "UP" si precio BTC al final del ventana >= precio al inicio, "DOWN" si menor.
Fuente de resolución: Chainlink BTC/USD data stream.

Slugs: btc-updown-5m-{timestamp}  y  btc-updown-15m-{timestamp}
donde timestamp = Unix epoch del fin de la ventana (frontera de 5 o 15 min).
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("weatherbot")

GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS     = {"User-Agent": "WeatherbotPolymarket/1.0"}

# Solo operar la ventana ACTIVA. No entrar en mercados futuros cuyo precio inicial
# todavía no se conoce. Si el mercado actual cerró, el siguiente ciclo (60s) lo detecta.
_MAX_LOOKAHEAD = 0


def _next_boundary(now_ts: int, interval_seconds: int) -> int:
    """Devuelve el próximo timestamp múltiplo de interval_seconds."""
    return ((now_ts // interval_seconds) + 1) * interval_seconds


async def _search_updown_events(client: httpx.AsyncClient, interval_minutes: int) -> list:
    """Busca eventos UpDown activos por keyword cuando el slug exacto no funciona."""
    label = f"{interval_minutes}m"
    try:
        resp = await client.get(
            f"{GAMMA_BASE}/events",
            params={"active": "true", "closed": "false", "limit": 100},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        events = resp.json()
        if not isinstance(events, list):
            return []
        results = []
        for ev in events:
            title = (ev.get("title") or "").lower()
            slug  = (ev.get("slug")  or "").lower()
            combined = title + " " + slug
            if ("up" in combined and "down" in combined and label in combined):
                results.append(ev)
        logger.info(f"UpDown {interval_minutes}m | Keyword search: {len(results)} eventos encontrados de {len(events)}")
        return results
    except Exception as e:
        logger.warning(f"UpDown {interval_minutes}m | Keyword search error: {e}")
        return []


async def fetch_updown_market(interval_minutes: int) -> Optional[dict]:
    """
    Busca el mercado UP/DOWN activo para el intervalo dado (5 o 15 min).

    Lógica de ventana:
      - Cada ventana empieza en múltiplo exacto de interval_seconds
      - El slug usa el timestamp del FINAL de la ventana (no del inicio)
      - Solo se acepta la ventana que ya empezó (elapsed >= 0.25 min) y
        cuyo total elapsed + remaining ≈ interval_minutes

    Returns dict con datos del mercado o None.
    """
    interval_seconds = interval_minutes * 60
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    # El slug usa el START timestamp de la ventana activa (verificado contra API real)
    # Ej: btc-updown-5m-1775394300 corresponde a ventana 13:05-13:10 UTC
    current_window_start = (now_ts // interval_seconds) * interval_seconds

    async with httpx.AsyncClient() as client:
        # ── Intento 1: slug exacto con el START timestamp de la ventana activa ────
        candidate_events = []
        slugs_to_try = [
            f"btc-updown-{interval_minutes}m-{current_window_start}",
            f"btc-up-or-down-{interval_minutes}m-{current_window_start}",
            f"btc-up-down-{interval_minutes}m-{current_window_start}",
        ]
        for slug in slugs_to_try:
            try:
                resp = await client.get(
                    f"{GAMMA_BASE}/events",
                    params={"slug": slug},
                    headers=HEADERS,
                    timeout=10,
                )
                data = resp.json()
                if data:
                    candidate_events.extend(data if isinstance(data, list) else [data])
                    logger.info(f"UpDown {interval_minutes}m | Slug encontrado: {slug}")
                    break
            except Exception as e:
                logger.warning(f"UpDown | Error fetching {slug}: {e}")

        # ── Intento 2: búsqueda por keyword solo si slug exacto falló ──────────
        if not candidate_events:
            logger.info(f"UpDown {interval_minutes}m | Slug exacto no encontrado, buscando por keyword...")
            candidate_events = await _search_updown_events(client, interval_minutes)

        for event in candidate_events:
            end_str = event.get("endDate", "")
            if not end_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                continue

            end_ts           = int(end_dt.timestamp())
            window_start_ts  = end_ts - interval_seconds
            elapsed_minutes  = (now_ts - window_start_ts) / 60
            minutes_to_close = (end_dt - now).total_seconds() / 60

            # ── Filtros de tiempo ──────────────────────────────────────────────
            # 0. Mercado ya cerrado (minutes_to_close negativo)
            if minutes_to_close <= 0:
                logger.info(f"UpDown {interval_minutes}m | Mercado ya cerrado ({minutes_to_close:.2f}min) — descartado")
                continue

            # 1. La ventana debe haber comenzado (precio inicial de BTC ya existe)
            if elapsed_minutes < 0.25:
                logger.info(
                    f"UpDown {interval_minutes}m | Ventana aún no inicia "
                    f"(elapsed={elapsed_minutes:.2f}min) — descartado"
                )
                continue

            # 2. No operar en el último minuto
            if minutes_to_close < 1.0:
                logger.info(f"UpDown {interval_minutes}m | Demasiado cerca del cierre ({minutes_to_close:.2f}min) — descartado")
                continue

            # 3. Validar que es la ventana ACTIVA (no futura)
            total_window = elapsed_minutes + minutes_to_close
            if total_window > interval_minutes + 1.0:
                logger.info(
                    f"UpDown {interval_minutes}m | Ventana futura descartada "
                    f"(elapsed={elapsed_minutes:.2f} + remaining={minutes_to_close:.2f} = {total_window:.2f}min > {interval_minutes}min)"
                )
                continue

            markets = event.get("markets", [])
            if not markets:
                continue

            m = markets[0]
            if not m.get("acceptingOrders"):
                continue

            # Parsear precios y tokens
            outcome_prices_raw = m.get("outcomePrices", "[]")
            clob_token_ids_raw = m.get("clobTokenIds", "[]")
            outcomes_raw       = m.get("outcomes", "[]")

            try:
                outcome_prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
                clob_token_ids = json.loads(clob_token_ids_raw) if isinstance(clob_token_ids_raw, str) else clob_token_ids_raw
                outcomes       = json.loads(outcomes_raw)       if isinstance(outcomes_raw, str)       else outcomes_raw
            except Exception:
                continue

            if len(outcome_prices) < 2 or len(clob_token_ids) < 2:
                continue

            # outcomes[0] = "Up", outcomes[1] = "Down" (orden canónico)
            up_idx   = 0 if outcomes[0].lower() == "up" else 1
            down_idx = 1 - up_idx

            up_price   = float(outcome_prices[up_idx])
            down_price = float(outcome_prices[down_idx])
            up_token   = clob_token_ids[up_idx]
            down_token = clob_token_ids[down_idx]

            best_bid = float(m.get("bestBid") or 0)
            best_ask = float(m.get("bestAsk") or 1)
            spread   = (best_ask - best_bid) / best_ask if best_ask > 0 else 1.0

            event_slug = event.get("slug", "")

            return {
                "slug":              event_slug,
                "title":             event.get("title", ""),
                "condition_id":      m.get("conditionId", ""),
                "asset":             "BTC_UPDOWN",
                "interval_minutes":  interval_minutes,
                "end_dt":            end_dt.isoformat(),
                "minutes_to_close":  minutes_to_close,
                "elapsed_minutes":   elapsed_minutes,
                "window_start_ts":   window_start_ts,
                # Precios
                "up_price":    up_price,
                "down_price":  down_price,
                "up_token":    up_token,
                "down_token":  down_token,
                "best_bid":    best_bid,
                "best_ask":    best_ask,
                "spread_pct":  round(spread, 4),
                # Calidad
                "liquidity":   float(event.get("liquidity") or m.get("liquidityNum") or 0),
                "volume_24h":  float(event.get("volume24hr") or 0),
                "accepting_orders": True,
            }

    return None
