"""
Descubre y parsea mercados de precio de Bitcoin en Polymarket.
Los mercados de BTC en Polymarket son binarios del tipo:
  "Will Bitcoin be above $X at [time] on [date]?"
  "Bitcoin above $X at 3:15 PM ET on April 2?"
  "BTC above $X?"
"""
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx
from config import bot_params

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"
HEADERS    = {"User-Agent": "WeatherbotPolymarket/1.0"}

# Variantes de título que indican mercados de precio BTC
BTC_KEYWORDS = ["bitcoin", "btc"]
PRICE_KEYWORDS = ["above", "below", "over", "under", "exceed", "price"]


def _parse_btc_market(title: str, end_date_str: str) -> Optional[dict]:
    """
    Parsea el título de un mercado BTC y extrae:
    - threshold: precio umbral en USD
    - side: "above" o "below"
    - resolution_time: cuándo resuelve (datetime UTC)

    Ejemplos:
    - "Will Bitcoin be above $65,000 at 3:15 PM ET on April 2?"
    - "Bitcoin above $65,500?"
    - "BTC below $64,000 at close?"
    """
    tl = title.lower()

    # Verificar que es mercado BTC
    if not any(kw in tl for kw in BTC_KEYWORDS):
        return None

    # Excluir mercados "between X and Y" — no modelables con log-normal simple
    if "between" in tl:
        return None

    # Detectar si es mercado de precio (no de otros eventos sobre BTC)
    if not any(kw in tl for kw in PRICE_KEYWORDS):
        return None

    # Detectar threshold de precio: $65,000 o $65500 o 65000
    price_match = re.search(r"\$?([\d,]+(?:\.\d+)?)\s*(?:k\b)?", tl)
    if not price_match:
        return None

    raw_price = price_match.group(1).replace(",", "")
    try:
        threshold = float(raw_price)
        # Si el número es pequeño (ej: "65k"), multiplicar
        if "k" in tl[price_match.start():price_match.end() + 2]:
            threshold *= 1000
        # Sanity check: BTC debería estar entre $1,000 y $1,000,000
        if not (1_000 < threshold < 1_000_000):
            return None
    except ValueError:
        return None

    # Detectar dirección
    if any(w in tl for w in ["above", "over", "exceed", "higher", "more than"]):
        side = "above"
    elif any(w in tl for w in ["below", "under", "lower", "less than"]):
        side = "below"
    else:
        # Si no hay dirección explícita, asumir "above" (más común)
        side = "above"

    # Parsear tiempo de resolución desde end_date_str
    resolution_dt = None
    if end_date_str:
        try:
            resolution_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            pass

    return {
        "threshold": threshold,
        "side": side,
        "resolution_dt": resolution_dt,
    }


async def fetch_btc_markets() -> list[dict]:
    """
    Busca mercados activos de precio de BTC en Polymarket.
    Filtra por mercados que cierran dentro de la ventana configurada.
    """
    markets = []
    # Limite por pagina reducido a 100 para evitar OSError(34, 'Result too large')
    # en Windows cuando la respuesta supera ~3MB en un solo read del socket.
    _PAGE_SIZE = 100
    async with httpx.AsyncClient() as client:
        raw_markets = []
        offset = 0
        while offset < 2000:
            try:
                resp = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params={
                        "active":  "true",
                        "closed":  "false",
                        "limit":   _PAGE_SIZE,
                        "offset":  offset,
                        "order":   "volume",
                        "tag":     "crypto",        # filtrar por categoría
                    },
                    headers=HEADERS,
                    timeout=20,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break

                raw_markets.extend(batch)

                # Parar si encontramos suficientes mercados BTC
                btc_count = sum(
                    1 for m in raw_markets
                    if any(kw in (m.get("question") or "").lower() for kw in BTC_KEYWORDS)
                )
                if btc_count >= 40:
                    break
                offset += _PAGE_SIZE
            except Exception:
                break

        if not raw_markets:
            return []

        now = datetime.now(timezone.utc)
        max_hours = bot_params.btc_max_hours_to_resolution
        min_liq   = bot_params.min_liquidity_usdc

        for m in raw_markets:
            title = m.get("question") or m.get("title") or ""
            if not title:
                continue

            # Solo mercados BTC de precio
            tl = title.lower()
            if not any(kw in tl for kw in BTC_KEYWORDS):
                continue
            if not any(kw in tl for kw in PRICE_KEYWORDS):
                continue

            end_date_str = m.get("endDate") or m.get("endDateIso") or ""
            if not end_date_str:
                continue

            try:
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                hours_to_close = (end_dt - now).total_seconds() / 3600
                if hours_to_close < 0 or hours_to_close > max_hours:
                    continue
            except Exception:
                continue

            if not m.get("acceptingOrders", True):
                continue

            liquidity = float(m.get("liquidityNum") or m.get("liquidity") or 0)
            if liquidity < min_liq:
                continue

            best_bid = float(m.get("bestBid") or 0)
            best_ask = float(m.get("bestAsk") or 1)
            spread_pct = (best_ask - best_bid) / best_ask if best_ask > 0 else 1.0
            if spread_pct > bot_params.max_spread_pct:
                continue

            volume_24h = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
            if volume_24h < bot_params.min_volume_24h_usdc:
                continue

            # Filtro: profundidad mínima del libro
            if bot_params.min_book_depth_usdc > 0 and liquidity < bot_params.min_book_depth_usdc * 2:
                continue

            # Parsear mercado
            parsed = _parse_btc_market(title, end_date_str)
            if not parsed:
                continue

            # Token IDs y precios
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

            if not clob_token_ids or not outcome_prices:
                continue

            try:
                yes_price    = float(outcome_prices[0])
                yes_token_id = clob_token_ids[0]
                no_token_id  = clob_token_ids[1] if len(clob_token_ids) > 1 else None
            except Exception:
                continue

            # Slug y URL para Polymarket
            _m_event_slug = (
                m.get("eventSlug") or m.get("event_slug")
                or (m.get("event") or {}).get("slug")
                or m.get("slug", "")
            )
            # Strip outcome suffix (-yes/-no) if market slug was returned
            if _m_event_slug:
                for _sfx in ("-yes", "-no", "-up", "-down"):
                    if _m_event_slug.lower().endswith(_sfx):
                        _m_event_slug = _m_event_slug[:-len(_sfx)]
                        break
            _m_poly_url = f"https://polymarket.com/event/{_m_event_slug}" if _m_event_slug else ""

            markets.append({
                # Identificación
                "condition_id":    m.get("conditionId", ""),
                "slug":            _m_event_slug,
                "poly_url":        _m_poly_url,
                "title":           title,
                "asset":           "BTC",
                "threshold":       parsed["threshold"],
                "side":            parsed["side"],          # "above" / "below"
                "resolution_dt":   parsed["resolution_dt"],
                # Precios
                "yes_price":       yes_price,
                "yes_token_id":    yes_token_id,
                "no_token_id":     no_token_id,
                "best_bid":        best_bid,
                "best_ask":        best_ask,
                "last_trade_price": float(m.get("lastTradePrice") or yes_price),
                # Calidad de mercado
                "liquidity":        liquidity,
                "volume_total":     float(m.get("volumeClob") or m.get("volume") or 0),
                "volume_24h":       volume_24h,
                "volume_7d":        float(m.get("volume1wkClob") or m.get("volume1wk") or 0),
                "spread_pct":       round(spread_pct, 4),
                "competitive_score": float(m.get("competitive") or 0),
                "accepting_orders": bool(m.get("acceptingOrders", True)),
                "min_order_size":   float(m.get("orderMinSize") or 5),
                "tick_size":        float(m.get("orderPriceMinTickSize") or 0.01),
                # Tiempo
                "hours_to_close":   hours_to_close,
            })

    return markets
