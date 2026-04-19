"""
Mercados BTC Up/Down de corto plazo en Polymarket.

Resuelven: "UP" si precio BTC al final del ventana >= precio al inicio, "DOWN" si menor.
Fuente de resolución: Chainlink BTC/USD data stream.

Slugs observados en producción:
  btc-updown-5m-{start_ts}       (formato más común)
  btc-up-or-down-5m-{start_ts}
  btc-up-down-5m-{start_ts}
  Los timestamps pueden usar start O end de la ventana según el ciclo.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger("weatherbot")

GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS     = {"User-Agent": "WeatherbotPolymarket/1.0"}

_REQUEST_TIMEOUT = 20.0   # subido de 10 → 20s para evitar timeouts en Gamma

# Cache de mercado por intervalo: evita repetir búsqueda de slug dentro de la misma ventana.
# Estructura: { interval_minutes: {"event": dict, "end_ts": int, "result": dict} }
_market_cache: dict = {}


def _get_cached_market(interval_minutes: int, now_ts: int) -> Optional[dict]:
    """Retorna el resultado cacheado si la ventana sigue activa."""
    entry = _market_cache.get(interval_minutes)
    if not entry:
        return None
    # La ventana sigue activa si still has > 1min to close
    if entry["end_ts"] - now_ts > 60:
        return entry["result"]
    return None


def _set_market_cache(interval_minutes: int, result: Optional[dict]) -> None:
    """Guarda el resultado de la búsqueda para la ventana actual."""
    if result and result.get("end_ts"):
        _market_cache[interval_minutes] = {
            "end_ts": result["end_ts"],
            "result": result,
        }
    else:
        # Limpiar cache si no se encontró mercado
        _market_cache.pop(interval_minutes, None)


async def _get_events_by_slug(client: httpx.AsyncClient, slug: str) -> list:
    """Intenta obtener evento por slug exacto. Retorna lista (puede ser vacía)."""
    try:
        import json as _json
        async with client.stream(
            "GET",
            f"{GAMMA_BASE}/events",
            params={"slug": slug},
            headers=HEADERS,
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return []
            chunks = []
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                chunks.append(chunk)
            data = _json.loads(b"".join(chunks))
        if not data:
            return []
        return data if isinstance(data, list) else [data]
    except Exception as e:
        logger.warning(f"UpDown | Error fetching {slug}: {e}")
        return []


async def _search_updown_by_markets(client: httpx.AsyncClient, interval_minutes: int) -> list:
    """
    Fallback robusto: busca en el endpoint /markets (más estable que /events).
    Filtra por título que contenga "up" y "down" y el intervalo.
    Luego agrupa por eventId para reconstruir el evento.
    """
    label = f"{interval_minutes}m"
    try:
        import json as _json
        async with client.stream(
            "GET",
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": 100,
                "order": "volume24hr",
            },
            headers=HEADERS,
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return []
            chunks = []
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                chunks.append(chunk)
            markets = _json.loads(b"".join(chunks))
        if not isinstance(markets, list):
            return []

        # Filtrar mercados que parecen UpDown del intervalo correcto
        candidates = []
        for m in markets:
            title = (m.get("question") or m.get("title") or "").lower()
            slug  = (m.get("slug") or "").lower()
            combined = title + " " + slug
            if (
                ("up" in combined and "down" in combined)
                and label in combined
                and ("btc" in combined or "bitcoin" in combined)
            ):
                candidates.append(m)

        if not candidates:
            return []

        # Intentar reconstruir eventos desde los mercados encontrados
        # Buscar el evento padre via eventId o groupItemTitle
        events_found = []
        for m in candidates:
            event_id = m.get("eventId") or m.get("event_id")
            if not event_id:
                continue
            try:
                import json as _json
                async with client.stream(
                    "GET",
                    f"{GAMMA_BASE}/events/{event_id}",
                    headers=HEADERS,
                    timeout=_REQUEST_TIMEOUT,
                ) as ev_resp:
                    if ev_resp.status_code == 200:
                        ev_chunks = []
                        async for chunk in ev_resp.aiter_bytes(chunk_size=8192):
                            ev_chunks.append(chunk)
                        ev = _json.loads(b"".join(ev_chunks))
                        if ev and isinstance(ev, dict):
                            events_found.append(ev)
            except Exception:
                pass

        logger.info(
            f"UpDown {interval_minutes}m | Market-fallback: "
            f"{len(candidates)} markets candidatos → {len(events_found)} eventos"
        )
        return events_found

    except Exception as e:
        logger.warning(f"UpDown {interval_minutes}m | Market-fallback error: {e}")
        return []


async def _search_updown_events_keyword(client: httpx.AsyncClient, interval_minutes: int) -> list:
    """Búsqueda por keyword en endpoint /events (más amplia, 500 eventos)."""
    label = f"{interval_minutes}m"
    try:
        import json as _json
        async with client.stream(
            "GET",
            f"{GAMMA_BASE}/events",
            params={"active": "true", "closed": "false", "limit": 100},
            headers=HEADERS,
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                return []
            chunks = []
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                chunks.append(chunk)
            events = _json.loads(b"".join(chunks))
        if not isinstance(events, list):
            return []
        results = []
        for ev in events:
            title = (ev.get("title") or "").lower()
            slug  = (ev.get("slug")  or "").lower()
            combined = title + " " + slug
            if (
                ("up" in combined and "down" in combined)
                and label in combined
                and ("btc" in combined or "bitcoin" in combined)
            ):
                results.append(ev)
        logger.info(
            f"UpDown {interval_minutes}m | Keyword search: "
            f"{len(results)} eventos encontrados de {len(events)}"
        )
        return results
    except Exception as e:
        logger.warning(f"UpDown {interval_minutes}m | Keyword search error: {e}")
        return []


async def fetch_updown_market(interval_minutes: int) -> Optional[dict]:
    """
    Busca el mercado UP/DOWN activo para el intervalo dado (5 o 15 min).

    Estrategia de búsqueda (en orden):
      1. Slug exacto con START timestamp (forma más común)
      2. Slug exacto con END timestamp (formato alternativo observado)
      3. Keyword search en /events (hasta 500 eventos)
      4. Fallback via /markets endpoint (más robusto)
    """
    interval_seconds = interval_minutes * 60
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())

    # Chequear cache: si ya conocemos el mercado de esta ventana, devolver directo
    cached = _get_cached_market(interval_minutes, now_ts)
    if cached:
        # Actualizar minutes_to_close y elapsed_minutes con valores actuales
        cached = dict(cached)
        end_ts_cached = cached["end_ts"]
        cached["minutes_to_close"] = (end_ts_cached - now_ts) / 60
        cached["elapsed_minutes"]  = (now_ts - cached["window_start_ts"]) / 60
        return cached

    # Timestamps candidatos para el slug
    current_start = (now_ts // interval_seconds) * interval_seconds
    current_end   = current_start + interval_seconds
    prev_start    = current_start - interval_seconds   # ventana anterior (puede seguir activa)
    prev_end      = current_start

    # Todas las variantes de slug a probar (start y end, 3 formatos de nombre)
    slug_prefixes = ["btc-updown", "btc-up-or-down", "btc-up-down"]
    slug_timestamps = [current_start, current_end, prev_start, prev_end]

    async with httpx.AsyncClient() as client:
        # ── Fase 1: slug exacto ──────────────────────────────────────────────
        candidate_events: list = []
        for ts in slug_timestamps:
            if candidate_events:
                break
            for prefix in slug_prefixes:
                slug = f"{prefix}-{interval_minutes}m-{ts}"
                events = await _get_events_by_slug(client, slug)
                if events:
                    logger.info(f"UpDown {interval_minutes}m | Slug encontrado: {slug}")
                    candidate_events = events
                    break

        # ── Fase 2: keyword en /events ───────────────────────────────────────
        if not candidate_events:
            logger.info(f"UpDown {interval_minutes}m | Slug exacto no encontrado, keyword search...")
            candidate_events = await _search_updown_events_keyword(client, interval_minutes)

        # ── Fase 3: fallback via /markets ────────────────────────────────────
        if not candidate_events:
            logger.info(f"UpDown {interval_minutes}m | Keyword vacío, buscando via /markets...")
            candidate_events = await _search_updown_by_markets(client, interval_minutes)

        if not candidate_events:
            logger.warning(f"UpDown {interval_minutes}m | No se encontró ningún mercado activo")
            return None

        # ── Seleccionar el evento activo correcto ────────────────────────────
        for event in candidate_events:
            end_str = event.get("endDate", "")
            if not end_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                continue

            end_ts_ev       = int(end_dt.timestamp())
            window_start_ts = end_ts_ev - interval_seconds
            elapsed_minutes  = (now_ts - window_start_ts) / 60
            minutes_to_close = (end_dt - now).total_seconds() / 60

            # Mercado ya cerrado
            if minutes_to_close <= 0:
                continue

            # La ventana debe haber comenzado (precio BTC inicial existe)
            if elapsed_minutes < 0.25:
                continue

            # No operar en el último minuto
            if minutes_to_close < 1.0:
                continue

            # Validar que es la ventana ACTIVA (no futura)
            total_window = elapsed_minutes + minutes_to_close
            if total_window > interval_minutes + 2.0:   # +2 min de tolerancia
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
            up_idx   = 0
            if outcomes and outcomes[0].lower() != "up":
                up_idx = 1
            down_idx = 1 - up_idx

            up_price   = float(outcome_prices[up_idx])
            down_price = float(outcome_prices[down_idx])
            up_token   = clob_token_ids[up_idx]
            down_token = clob_token_ids[down_idx]

            best_bid = float(m.get("bestBid") or 0)
            best_ask = float(m.get("bestAsk") or 1)
            spread   = (best_ask - best_bid) / best_ask if best_ask > 0 else 1.0

            event_slug = event.get("slug", "")
            poly_url   = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

            logger.info(
                f"UpDown {interval_minutes}m | Mercado activo: {event_slug} | "
                f"elapsed={elapsed_minutes:.1f}min remaining={minutes_to_close:.1f}min"
            )

            # Precio referencia Chainlink al inicio de la ventana (si disponible)
            event_meta    = event.get("eventMetadata") or {}
            price_to_beat = event_meta.get("priceToBeat")

            result = {
                "slug":              event_slug,
                "poly_url":          poly_url,
                "title":             event.get("title", ""),
                "condition_id":      m.get("conditionId", ""),
                "asset":             "BTC_UPDOWN",
                "interval_minutes":  interval_minutes,
                "end_dt":            end_dt.isoformat(),
                "end_ts":            end_ts_ev,
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
                # Precio referencia Chainlink (precio a superar, según Polymarket)
                "btc_price_to_beat": round(float(price_to_beat), 2) if price_to_beat else None,
                # Calidad
                "liquidity":   float(event.get("liquidity") or m.get("liquidityNum") or 0),
                "volume_24h":  float(event.get("volume24hr") or 0),
                "accepting_orders": True,
            }
            _set_market_cache(interval_minutes, result)
            return result

    logger.warning(f"UpDown {interval_minutes}m | Ningún evento pasó los filtros de ventana")
    return None
