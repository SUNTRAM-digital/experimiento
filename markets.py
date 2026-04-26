"""
Cliente de Polymarket: descubre mercados de temperatura US y obtiene precios.
Usa Gamma API (publica) + CLOB API (autenticada para trades).
"""
import re
import asyncio
from datetime import datetime, timezone, date
from typing import Optional
import httpx
from config import CITY_STATIONS, settings, bot_params

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

HEADERS = {"User-Agent": "WeatherbotPolymarket/1.0"}


def _parse_temp_market(title: str) -> Optional[dict]:
    """
    Parsea el titulo de un mercado para extraer ciudad, rango de temperatura y fecha.

    Ejemplos de titulos:
    - "Will the highest temperature in Chicago be between 62-63°F on March 21?"
    - "Highest temp in NYC 34-35°F on April 2?"
    - "Will Chicago high temp be 29°F or below on March 15?"
    """
    title_lower = title.lower()

    # Detectar ciudad
    city_key = None
    station = None
    for city, st in CITY_STATIONS.items():
        if city in title_lower:
            city_key = city
            station = st
            break

    if not station:
        return None

    # Detectar rango de temperatura (ej: 62-63, 62–63, 29 or below, 80 or above)
    range_match = re.search(r"(\d+(?:\.\d+)?)[–\-](\d+(?:\.\d+)?)°?f", title_lower)
    below_match = re.search(r"(\d+(?:\.\d+)?)°?f?\s+or\s+(?:below|lower)", title_lower)
    above_match = re.search(r"(\d+(?:\.\d+)?)°?f?\s+or\s+(?:above|higher)", title_lower)

    if range_match:
        temp_low = float(range_match.group(1))
        temp_high = float(range_match.group(2))
        bucket_type = "range"
    elif below_match:
        temp_low = -999.0
        temp_high = float(below_match.group(1))
        bucket_type = "below"
    elif above_match:
        temp_low = float(above_match.group(1))
        temp_high = 999.0
        bucket_type = "above"
    else:
        return None

    # Detectar fecha
    date_match = re.search(
        r"on\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d+)",
        title_lower,
    )
    target_date = None
    if date_match:
        month_str = date_match.group(1)
        day = int(date_match.group(2))
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        month = months[month_str]
        year = datetime.now(timezone.utc).year
        try:
            target_date = date(year, month, day)
            # Si la fecha ya paso este año, asumir el siguiente
            if target_date < datetime.now(timezone.utc).date():
                target_date = date(year + 1, month, day)
        except ValueError:
            return None

    return {
        "city": city_key,
        "station": station,
        "temp_low": temp_low,
        "temp_high": temp_high,
        "bucket_type": bucket_type,
        "target_date": target_date,
    }


async def fetch_weather_markets() -> list[dict]:
    """
    Busca todos los mercados activos de temperatura US en Polymarket.
    Retorna lista de mercados parseados y listos para analizar.
    """
    markets = []
    # Limite por pagina reducido a 100 para evitar OSError(34, 'Result too large')
    # en Windows cuando la respuesta supera ~3MB en un solo read del socket.
    _PAGE_SIZE = 100
    import json as _json
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0)) as client:
        # Paginar mercados activos para encontrar todos los de temperatura
        raw_markets = []
        offset = 0
        while offset < 4000:
            try:
                async with client.stream(
                    "GET",
                    f"{GAMMA_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": _PAGE_SIZE,
                        "offset": offset,
                        "order": "volume",
                    },
                    headers=HEADERS,
                ) as resp:
                    resp.raise_for_status()
                    chunks = []
                    async for chunk in resp.aiter_bytes(chunk_size=8192):
                        chunks.append(chunk)
                    batch = _json.loads(b"".join(chunks))
                if not batch:
                    break
                raw_markets.extend(batch)
                # Contar cuantos en Fahrenheit (US) llevamos
                fahr_count = sum(
                    1 for m in raw_markets
                    if "\u00b0f" in (m.get("question") or "").lower()
                )
                if fahr_count >= 60:
                    break
                offset += _PAGE_SIZE
            except Exception:
                break

        if not raw_markets:
            return []

        now = datetime.now(timezone.utc)
        max_hours = bot_params.max_hours_to_resolution
        min_liq = bot_params.min_liquidity_usdc

        for m in raw_markets:
            title = m.get("question") or m.get("title") or ""
            if not title:
                continue

            # Solo mercados de temperatura
            title_lower = title.lower()
            if not any(kw in title_lower for kw in ["temperature", "temp", "°f", "degrees"]):
                continue

            # Parsear ciudad y rango
            parsed = _parse_temp_market(title)
            if not parsed:
                continue

            # Verificar ventana de tiempo
            end_date_str = m.get("endDate") or m.get("endDateIso") or ""
            hours_to_close = None
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    hours_to_close = (end_dt - now).total_seconds() / 3600
                    if hours_to_close < 0 or hours_to_close > max_hours:
                        continue
                except Exception:
                    continue
            else:
                continue

            # Filtro: mercado debe estar aceptando ordenes
            if not m.get("acceptingOrders", True):
                continue

            # Verificar liquidez
            liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            if liquidity < min_liq:
                continue

            # Filtro: spread maximo
            best_bid = float(m.get("bestBid") or 0)
            best_ask = float(m.get("bestAsk") or 1)
            spread_pct = (best_ask - best_bid) / best_ask if best_ask > 0 else 1.0
            if spread_pct > bot_params.max_spread_pct:
                continue

            # Filtro: volumen 24h minimo
            volume_24h = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
            if volume_24h < bot_params.min_volume_24h_usdc:
                continue

            # Filtro: profundidad mínima del libro ($200 según knowledge base — sin
            # suficiente profundidad no se puede salir del trade en modo swing)
            if bot_params.min_book_depth_usdc > 0:
                # Proxy: usamos liquidez total como estimación de profundidad agregada
                # Si la liquidez total < 2× min_book_depth, es muy poco profundo
                if liquidity < bot_params.min_book_depth_usdc * 2:
                    continue

            # Obtener token IDs y precios
            clob_token_ids = m.get("clobTokenIds") or []
            if isinstance(clob_token_ids, str):
                try:
                    import json
                    clob_token_ids = json.loads(clob_token_ids)
                except Exception:
                    clob_token_ids = []

            outcome_prices = m.get("outcomePrices") or []
            if isinstance(outcome_prices, str):
                try:
                    import json
                    outcome_prices = json.loads(outcome_prices)
                except Exception:
                    outcome_prices = []

            yes_price = None
            yes_token_id = None
            if clob_token_ids and outcome_prices:
                try:
                    yes_price = float(outcome_prices[0])
                    yes_token_id = clob_token_ids[0]
                except Exception:
                    pass

            if yes_price is None or yes_token_id is None:
                continue

            # Slug del evento para URL de Polymarket
            m_event_slug = (
                m.get("eventSlug") or m.get("event_slug")
                or (m.get("event") or {}).get("slug")
                or _clean_market_slug(m.get("slug", ""))
            )
            m_poly_url = f"https://polymarket.com/event/{m_event_slug}" if m_event_slug else ""

            markets.append({
                # Identificacion
                "condition_id": m.get("conditionId", ""),
                "slug":         m_event_slug,
                "poly_url":     m_poly_url,
                "title": title,
                "city": parsed["city"],
                "station": parsed["station"],
                "temp_low": parsed["temp_low"],
                "temp_high": parsed["temp_high"],
                "bucket_type": parsed["bucket_type"],
                "target_date": parsed["target_date"],
                # Precios
                "yes_price": yes_price,
                "yes_token_id": yes_token_id,
                "no_token_id": clob_token_ids[1] if len(clob_token_ids) > 1 else None,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "last_trade_price": float(m.get("lastTradePrice") or yes_price),
                # Calidad de mercado
                "liquidity": liquidity,
                "volume_total": float(m.get("volumeClob") or m.get("volume") or 0),
                "volume_24h": volume_24h,
                "volume_7d": float(m.get("volume1wkClob") or m.get("volume1wk") or 0),
                "spread_pct": round(spread_pct, 4),
                "competitive_score": float(m.get("competitive") or 0),
                "accepting_orders": bool(m.get("acceptingOrders", True)),
                "min_order_size": float(m.get("orderMinSize") or 5),
                "tick_size": float(m.get("orderPriceMinTickSize") or 0.01),
                # Tiempo
                "hours_to_close": hours_to_close,
            })

    return markets


async def get_order_book_depth(token_id: str, side: str, up_to_price: float) -> dict:
    """
    Obtiene la profundidad del order book en el lado que nos interesa.
    side = 'ask' (si vamos a comprar YES) o 'bid' (si vamos a comprar NO)
    Retorna cuantos shares y USDC estan disponibles hasta up_to_price.
    """
    try:
        # /book es un endpoint pequeño (un solo mercado) — client.get() simple es seguro.
        # El streaming se usa solo en endpoints paginados de Gamma que pueden superar 3MB.
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code != 200:
                return {"depth_shares": 0, "depth_usdc": 0, "levels": 0}
            book = resp.json()
            levels_key = "asks" if side == "ask" else "bids"
            levels = book.get(levels_key, [])

            total_shares = 0.0
            total_usdc = 0.0
            count = 0
            for level in levels:
                price = float(level.get("price", 0))
                size = float(level.get("size", 0))
                # Para asks: incluir si price <= up_to_price
                # Para bids: incluir si price >= up_to_price
                if side == "ask" and price <= up_to_price:
                    total_shares += size
                    total_usdc += size * price
                    count += 1
                elif side == "bid" and price >= up_to_price:
                    total_shares += size
                    total_usdc += size * price
                    count += 1

            return {
                "depth_shares": round(total_shares, 2),
                "depth_usdc": round(total_usdc, 2),
                "levels": count,
            }
    except Exception:
        return {"depth_shares": 0, "depth_usdc": 0, "levels": 0}


async def get_full_order_book(token_id: str) -> dict:
    """
    Devuelve snapshot completo del order book en tiempo real.
    Retorna:
      {
        best_bid, best_ask, mid, spread,
        bids: [(price, size), ...] top-5,
        asks: [(price, size), ...] top-5,
        bid_total_shares, ask_total_shares,
        bid_usdc_depth, ask_usdc_depth,
        pressure  (>1 = buy pressure, <1 = sell pressure),
      }
    """
    default = {
        "best_bid": None, "best_ask": None, "mid": None, "spread": None,
        "bids": [], "asks": [],
        "bid_total_shares": 0.0, "ask_total_shares": 0.0,
        "bid_usdc_depth": 0.0, "ask_usdc_depth": 0.0,
        "pressure": 1.0,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code != 200:
                return default
            book = resp.json()
        raw_bids = book.get("bids", []) or []
        raw_asks = book.get("asks", []) or []
        # Polymarket: bids ASC price (peor primero), asks DESC. Normalizar.
        bids = sorted(
            [(float(l.get("price", 0)), float(l.get("size", 0))) for l in raw_bids],
            key=lambda x: -x[0],
        )
        asks = sorted(
            [(float(l.get("price", 0)), float(l.get("size", 0))) for l in raw_asks],
            key=lambda x: x[0],
        )
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        mid = None
        spread = None
        if best_bid is not None and best_ask is not None:
            mid = round((best_bid + best_ask) / 2, 4)
            spread = round(best_ask - best_bid, 4)
        bid_shares = sum(s for _, s in bids[:10])
        ask_shares = sum(s for _, s in asks[:10])
        bid_usdc = sum(p * s for p, s in bids[:10])
        ask_usdc = sum(p * s for p, s in asks[:10])
        pressure = (bid_shares / ask_shares) if ask_shares > 0 else 1.0
        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread": spread,
            "bids": bids[:5],
            "asks": asks[:5],
            "bid_total_shares": round(bid_shares, 2),
            "ask_total_shares": round(ask_shares, 2),
            "bid_usdc_depth": round(bid_usdc, 2),
            "ask_usdc_depth": round(ask_usdc, 2),
            "pressure": round(pressure, 3),
        }
    except Exception:
        return default


async def get_clob_trades(token_id: str, limit: int = 100) -> list:
    """Fills recientes del CLOB para un token."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_BASE}/trades",
                params={"token_id": token_id, "limit": limit},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data if isinstance(data, list) else (data.get("data") or [])
    except Exception:
        return []


async def get_clob_flow(up_token_id: str, down_token_id: str, window_seconds: int = 300) -> dict:
    """
    Calcula flujo de volumen CLOB últimos N segundos.
    Retorna dirección donde va el dinero real de participantes.
    """
    import time as _t
    cutoff = _t.time() - window_seconds

    _neutral = {"available": False, "up_vol": 0.0, "down_vol": 0.0,
                "direction": "NEUTRAL", "strength": 0.0, "flow_ratio": 0.5}
    if not up_token_id or not down_token_id:
        return _neutral

    up_raw, down_raw = await asyncio.gather(
        get_clob_trades(up_token_id),
        get_clob_trades(down_token_id),
        return_exceptions=True,
    )
    if isinstance(up_raw, Exception):   up_raw = []
    if isinstance(down_raw, Exception): down_raw = []

    def _sum_vol(trades):
        v = 0.0
        for t in trades:
            try:
                ts = float(t.get("match_time") or t.get("timestamp") or t.get("created_at") or 0)
                if ts >= cutoff:
                    v += float(t.get("size") or t.get("trade_size") or t.get("matched_amount") or 0)
            except Exception:
                pass
        return v

    up_vol   = _sum_vol(up_raw)
    down_vol = _sum_vol(down_raw)
    total    = up_vol + down_vol

    if total < 5.0:
        return _neutral

    ratio = up_vol / total
    if ratio > 0.60:
        direction, strength = "UP",   round((ratio - 0.50) / 0.50, 3)
    elif ratio < 0.40:
        direction, strength = "DOWN", round((0.50 - ratio) / 0.50, 3)
    else:
        direction, strength = "NEUTRAL", 0.0

    return {
        "available":  True,
        "up_vol":     round(up_vol, 1),
        "down_vol":   round(down_vol, 1),
        "flow_ratio": round(ratio, 3),
        "direction":  direction,
        "strength":   strength,
    }


async def get_live_price(token_id: str) -> Optional[float]:
    """Obtiene el precio actual de un token desde el CLOB."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_BASE}/price",
                params={"token_id": token_id, "side": "BUY"},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                price = float(data.get("price", 0))
                if price > 0:
                    return price
    except Exception:
        pass

    # Fallback: last-trade-price (funciona para mercados ya resueltos)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_BASE}/last-trade-price",
                params={"token_id": token_id},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                price = float(data.get("price", 0))
                if price > 0:
                    return price
    except Exception:
        pass
    return None


async def get_official_outcome(up_token: str, slug: str) -> Optional[str]:
    """
    Obtiene el resultado oficial de un mercado UP/DOWN desde Polymarket.
    Retorna "UP_WON", "DOWN_WON", o None si aún no está resuelto.

    Método 1: last-trade-price del UP token (fuente más directa).
    Método 2: outcomePrices del evento en Gamma API.
    """
    # Método 1: precio final del UP token
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CLOB_BASE}/last-trade-price",
                params={"token_id": up_token},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                price = float(resp.json().get("price", 0))
                if price >= 0.90:
                    return "UP_WON"
                if price <= 0.10:
                    return "DOWN_WON"
    except Exception:
        pass

    # Método 2: Gamma API outcomePrices
    try:
        event_slug = slug.split("btc-updown-")[1] if "btc-updown-" in slug else slug
        # El slug del evento es el slug completo del mercado
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{GAMMA_BASE}/events",
                params={"slug": slug},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                events = resp.json()
                for evt in events:
                    for mkt in evt.get("markets", []):
                        op = mkt.get("outcomePrices")
                        if isinstance(op, str):
                            try:
                                op = __import__("json").loads(op)
                            except Exception:
                                continue
                        if isinstance(op, list) and len(op) >= 2:
                            up_price   = float(op[0])
                            down_price = float(op[1])
                            if up_price >= 0.90:
                                return "UP_WON"
                            if down_price >= 0.90:
                                return "DOWN_WON"
    except Exception:
        pass

    return None


DATA_API_BASE = "https://data-api.polymarket.com"

# Cache conditionId → slug para no hacer N llamadas por refresh
_slug_cache: dict[str, str] = {}


def _clean_market_slug(slug: str) -> str:
    """
    Convierte un market slug en event slug eliminando sufijos de outcome.
    Ej: "btc-up-or-down-5m-1234-up" → "btc-up-or-down-5m-1234"
        "will-chicago-be-above-90f-yes" → "will-chicago-be-above-90f"
    """
    if not slug:
        return slug
    low = slug.lower()
    for suffix in ("-yes", "-no", "-up", "-down"):
        if low.endswith(suffix):
            return slug[: -len(suffix)]
    return slug


async def _fetch_slug_for_condition(client: httpx.AsyncClient, condition_id: str) -> str:
    """
    Devuelve el EVENT slug para construir URLs de Polymarket.
    Prioridad:
      1) eventSlug / event_slug del mercado
      2) event.slug o events[0].slug
      3) Llamada al endpoint /events con el eventId del mercado
      4) market.slug limpiado de sufijos de outcome (-yes/-no/-up/-down)
    """
    if not condition_id:
        return ""
    if condition_id in _slug_cache:
        return _slug_cache[condition_id]
    try:
        resp = await client.get(
            f"{GAMMA_BASE}/markets",
            params={"conditionId": condition_id},
            headers=HEADERS,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            market = None
            if isinstance(data, list) and data:
                market = data[0]
            elif isinstance(data, dict) and data:
                market = data

            if market:
                # 1) campo directo eventSlug / event_slug
                event_slug = (
                    market.get("eventSlug")
                    or market.get("event_slug")
                    or (market.get("event") or {}).get("slug")
                )
                # 2) events array [{"slug": "..."}]
                if not event_slug:
                    events_list = market.get("events") or []
                    if events_list and isinstance(events_list, list):
                        event_slug = events_list[0].get("slug", "")

                # 3) Llamar /events con el eventId del mercado
                if not event_slug:
                    event_id = market.get("eventId") or market.get("event_id")
                    if event_id:
                        try:
                            ev_resp = await client.get(
                                f"{GAMMA_BASE}/events/{event_id}",
                                headers=HEADERS,
                                timeout=8,
                            )
                            if ev_resp.status_code == 200:
                                ev_data = ev_resp.json()
                                ev_obj = ev_data if isinstance(ev_data, dict) else (ev_data[0] if ev_data else {})
                                event_slug = ev_obj.get("slug", "")
                        except Exception:
                            pass

                # 4) Fallback: slug del mercado limpiando sufijo de outcome
                if not event_slug:
                    raw_slug = market.get("slug", "")
                    event_slug = _clean_market_slug(raw_slug)

                if event_slug:
                    _slug_cache[condition_id] = event_slug
                return event_slug or ""
    except Exception:
        pass
    return ""


async def get_polymarket_positions(wallet_address: str) -> list[dict] | None:
    """
    Obtiene las posiciones reales desde data-api.polymarket.com/positions.
    Retorna [] si no hay posiciones activas, None si hubo error de red/API.
    """
    if not wallet_address:
        return []
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "GET",
                f"{DATA_API_BASE}/positions",
                params={"user": wallet_address},
                headers=HEADERS,
                timeout=15,
            ) as resp:
                if resp.status_code != 200:
                    return None  # Error de API — conservar datos anteriores
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    chunks.append(chunk)
            raw = json.loads(b"".join(chunks))
            if not isinstance(raw, list):
                return None  # Respuesta inesperada — conservar datos anteriores

            now = datetime.now(timezone.utc)
            positions = []
            for p in raw:
                size = float(p.get("size", 0))
                if size <= 0:
                    continue

                avg_price = float(p.get("avgPrice", 0))
                cur_price = float(p.get("curPrice", avg_price))
                cost      = float(p.get("initialValue", avg_price * size))
                cur_value = float(p.get("currentValue", cur_price * size))
                pnl       = float(p.get("cashPnl", cur_value - cost))
                pnl_pct   = float(p.get("percentPnl", (pnl / cost * 100) if cost > 0 else 0))

                hours_to_close = None
                end_date_iso = None

                # Intentar obtener fecha de cierre del campo endDate
                for date_field in ("endDate", "end_date", "expirationDate", "resolveDate"):
                    end_date_str = p.get(date_field, "")
                    if end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            hours_to_close = round((end_dt - now).total_seconds() / 3600, 1)
                            end_date_iso = end_dt.isoformat()
                            break
                        except Exception:
                            pass

                # Fallback: parsear la fecha del título ("on March 21")
                if end_date_iso is None:
                    title = p.get("title", "")
                    _months_map = {
                        "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
                        "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
                    }
                    dm = re.search(
                        r"on\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d+)",
                        title.lower(),
                    )
                    if dm:
                        month = _months_map[dm.group(1)]
                        day   = int(dm.group(2))
                        year  = now.year
                        try:
                            # Los mercados de temperatura en Polymarket cierran ~23:00 UTC
                            end_dt = datetime(year, month, day, 23, 0, tzinfo=timezone.utc)
                            if end_dt < now:
                                end_dt = datetime(year + 1, month, day, 23, 0, tzinfo=timezone.utc)
                            hours_to_close = round((end_dt - now).total_seconds() / 3600, 1)
                            end_date_iso = end_dt.isoformat()
                        except Exception:
                            pass

                # Construir URL de Polymarket usando event slug
                # Prioridad: eventSlug directo → slug limpiado de sufijos → Gamma API lookup
                condition_id = p.get("conditionId", "")
                event_slug = (
                    p.get("eventSlug") or p.get("event_slug")
                    or _clean_market_slug(p.get("slug") or p.get("marketSlug") or "")
                )
                if not event_slug and condition_id:
                    event_slug = await _fetch_slug_for_condition(client, condition_id)
                poly_url = f"https://polymarket.com/event/{event_slug}" if event_slug else ""

                positions.append({
                    "token_id":       p.get("asset", ""),
                    "condition_id":   condition_id,
                    "market_title":   p.get("title", ""),
                    "outcome":        p.get("outcome", "YES"),
                    "size":           round(size, 2),
                    "avg_price":      round(avg_price, 4),
                    "cur_price":      round(cur_price, 4),
                    "cost_usdc":      round(cost, 2),
                    "cur_value_usdc": round(cur_value, 2),
                    "pnl_usdc":       round(pnl, 2),
                    "pnl_pct":        round(pnl_pct, 1),
                    "hours_to_close": hours_to_close,
                    "end_date":       end_date_iso,
                    "redeemable":     bool(p.get("redeemable", False)),
                    "mergeable":      bool(p.get("mergeable", False)),
                    "poly_url":       poly_url,
                })
            return positions

    except Exception:
        return None  # Error de red — conservar datos anteriores


async def get_open_orders(clob_client) -> list[dict]:
    """
    Obtiene las ordenes abiertas usando el cliente autenticado de py-clob-client.
    clob_client debe ser el _clob_client ya inicializado en bot.py
    """
    if clob_client is None:
        return []
    try:
        from py_clob_client.clob_types import OpenOrderParams
        raw = clob_client.get_orders(OpenOrderParams())
        if not raw:
            return []

        orders = []
        for o in raw:
            size_matched  = float(o.get("size_matched", 0))
            size_original = float(o.get("original_size", 0))
            size_remaining = size_original - size_matched
            if size_remaining <= 0:
                continue
            price = float(o.get("price", 0))
            orders.append({
                "id":              o.get("id", ""),
                "market":          o.get("market", ""),
                "token_id":        o.get("asset_id", ""),
                "outcome":         o.get("outcome", ""),
                "side":            o.get("side", "BUY"),
                "price":           round(price, 4),
                "size_orig":       round(size_original, 2),
                "size_filled":     round(size_matched, 2),
                "size_remaining":  round(size_remaining, 2),
                "cost_usdc":       round(price * size_remaining, 2),
                "created_at":      o.get("created_at", ""),
                "status":          o.get("status", "LIVE"),
            })
        return orders
    except Exception:
        return []
