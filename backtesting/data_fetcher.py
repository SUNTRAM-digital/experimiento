"""
Fetcher de datos historicos para backtesting.

Fuentes:
  1. Polymarket Gamma API — historial de precios de contratos resueltos
  2. NOAA CDO API       — datos historicos de temperatura por estacion ICAO

Uso principal:
  - get_resolved_weather_markets()  → mercados de clima ya cerrados con precio final
  - get_price_history()             → serie de precios de un contrato durante su vida
  - get_historical_temp()           → temperaturas maximas historicas de una estacion ICAO
"""
import asyncio
from datetime import date, datetime, timezone, timedelta
from typing import Optional
import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "WeatherbotPolymarket/1.0", "Accept": "application/json"}

# Cache local para evitar re-descargar datos
_price_history_cache: dict[str, list[dict]] = {}
_resolved_markets_cache: list[dict] = []


async def get_resolved_weather_markets(
    limit: int = 200,
    city_filter: Optional[str] = None,
) -> list[dict]:
    """
    Obtiene mercados de temperatura US ya resueltos desde Gamma API.

    Cada mercado resuelto tiene:
      - question:        titulo del mercado
      - outcomePrices:   precios finales YES/NO al cierre
      - volume:          volumen total negociado
      - condition_id:    identificador unico

    Returns lista de dicts normalizados para el backtest engine.
    """
    global _resolved_markets_cache
    if _resolved_markets_cache:
        return _resolved_markets_cache

    markets = []
    async with httpx.AsyncClient() as client:
        offset = 0
        while len(markets) < limit:
            try:
                params = {
                    "active": "false",
                    "closed": "true",
                    "limit": 100,
                    "offset": offset,
                    "order": "volume",
                }
                resp = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params=params,
                    headers=HEADERS,
                    timeout=20,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break

                for m in batch:
                    title = m.get("question") or m.get("title") or ""
                    title_lower = title.lower()

                    # Solo mercados de temperatura °F
                    if not any(kw in title_lower for kw in ["temperature", "temp", "°f", "degrees"]):
                        continue
                    if city_filter and city_filter.lower() not in title_lower:
                        continue

                    # Extraer precio de resolucion YES (1.0 = YES resolvio, 0.0 = NO)
                    outcome_prices = m.get("outcomePrices") or []
                    if isinstance(outcome_prices, str):
                        import json
                        try:
                            outcome_prices = json.loads(outcome_prices)
                        except Exception:
                            outcome_prices = []

                    resolved_yes = None
                    if outcome_prices and len(outcome_prices) >= 1:
                        try:
                            resolved_yes = float(outcome_prices[0])
                        except (ValueError, TypeError):
                            pass

                    markets.append({
                        "title":        title,
                        "condition_id": m.get("conditionId") or m.get("id") or "",
                        "volume":       float(m.get("volume") or 0),
                        "resolved_yes": resolved_yes,
                        "end_date":     m.get("endDate") or "",
                        "start_date":   m.get("startDate") or "",
                        "raw":          m,
                    })

                    if len(markets) >= limit:
                        break

                offset += 100
                if offset >= 1000:
                    break
            except Exception:
                break

    _resolved_markets_cache = markets
    return markets


async def get_price_history(
    condition_id: str,
    days_back: int = 30,
) -> list[dict]:
    """
    Obtiene el historial de precios de un contrato especifico.

    La Gamma API expone /prices-history?market={condition_id}&interval=1d
    Retorna lista de {timestamp, price} ordenada cronologicamente.
    """
    cache_key = f"{condition_id}:{days_back}"
    if cache_key in _price_history_cache:
        return _price_history_cache[cache_key]

    result = []
    async with httpx.AsyncClient() as client:
        try:
            params = {
                "market":    condition_id,
                "interval":  "1d",
                "fidelity":  "1440",  # 1 punto por dia
            }
            resp = await client.get(
                f"{GAMMA_BASE}/prices-history",
                params=params,
                headers=HEADERS,
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                history = data.get("history") or data if isinstance(data, list) else []
                for point in history:
                    ts  = point.get("t") or point.get("timestamp") or 0
                    prc = point.get("p") or point.get("price") or point.get("y") or 0
                    if ts and prc:
                        result.append({
                            "timestamp": int(ts),
                            "price":     float(prc),
                            "date":      datetime.fromtimestamp(int(ts), tz=timezone.utc).date(),
                        })
                result.sort(key=lambda x: x["timestamp"])
        except Exception:
            pass

    _price_history_cache[cache_key] = result
    return result


async def get_historical_temp(
    station: str,
    target_date: date,
) -> Optional[float]:
    """
    Obtiene la temperatura maxima observada historicamente para una estacion ICAO y fecha.
    Usa NOAA observations API (misma que weather.py usa para datos actuales).

    Returns temperatura en Fahrenheit o None si no disponible.
    """
    url = f"https://api.weather.gov/stations/{station}/observations"
    params = {
        "start": datetime.combine(target_date, datetime.min.time())
                 .replace(tzinfo=timezone.utc).isoformat(),
        "end":   datetime.combine(target_date + timedelta(days=1), datetime.min.time())
                 .replace(tzinfo=timezone.utc).isoformat(),
        "limit": 24,
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=15,
                                    headers={"User-Agent": "WeatherbotPolymarket/1.0",
                                             "Accept": "application/geo+json"})
            if resp.status_code != 200:
                return None
            features = resp.json().get("features", [])
            temps_f = []
            for f in features:
                temp_c = f.get("properties", {}).get("temperature", {}).get("value")
                if temp_c is not None:
                    temps_f.append(temp_c * 9 / 5 + 32)
            return max(temps_f) if temps_f else None
    except Exception:
        return None


def clear_cache():
    """Limpia los caches en memoria (para tests o recargas)."""
    global _price_history_cache, _resolved_markets_cache
    _price_history_cache = {}
    _resolved_markets_cache = []
