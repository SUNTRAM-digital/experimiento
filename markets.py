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
    async with httpx.AsyncClient() as client:
        # Paginar mercados activos para encontrar todos los de temperatura
        raw_markets = []
        offset = 0
        while offset < 4000:
            try:
                resp = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": 500,
                        "offset": offset,
                        "order": "volume",
                    },
                    headers=HEADERS,
                    timeout=20,
                )
                resp.raise_for_status()
                batch = resp.json()
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
                offset += 500
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

            markets.append({
                # Identificacion
                "condition_id": m.get("conditionId", ""),
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
                return float(data.get("price", 0))
    except Exception:
        pass
    return None


DATA_API_BASE = "https://data-api.polymarket.com"

# Cache conditionId → slug para no hacer N llamadas por refresh
_slug_cache: dict[str, str] = {}


async def _fetch_slug_for_condition(client: httpx.AsyncClient, condition_id: str) -> str:
    """
    Devuelve el EVENT slug para construir URLs de Polymarket.
    Prioridad: campo eventSlug/event del mercado → events[0].slug → slug del mercado.
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
                # 1) campo directo eventSlug o event.slug
                event_slug = (
                    market.get("eventSlug")
                    or market.get("event_slug")
                    or (market.get("event") or {}).get("slug")
                )
                # 2) events array  [{"slug": "..."}]
                if not event_slug:
                    events_list = market.get("events") or []
                    if events_list and isinstance(events_list, list):
                        event_slug = events_list[0].get("slug", "")
                # 3) fallback: slug del mercado (suele coincidir con el evento para binarios)
                if not event_slug:
                    event_slug = market.get("slug", "")
                if event_slug:
                    _slug_cache[condition_id] = event_slug
                return event_slug or ""
    except Exception:
        pass
    return ""


async def get_polymarket_positions(wallet_address: str) -> list[dict]:
    """
    Obtiene las posiciones reales desde data-api.polymarket.com/positions.
    """
    if not wallet_address:
        return []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{DATA_API_BASE}/positions",
                params={"user": wallet_address},
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code != 200:
                return []

            raw = resp.json()
            if not isinstance(raw, list):
                return []

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

                # Construir URL de Polymarket usando slug real (Gamma API si es necesario)
                condition_id = p.get("conditionId", "")
                event_slug = (
                    p.get("slug") or p.get("eventSlug") or p.get("event_slug")
                    or p.get("marketSlug") or ""
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
        return []


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
