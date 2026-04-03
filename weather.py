"""
Cliente para weather.gov (NOAA) - completamente gratis, sin API key.
Obtiene forecasts de temperatura para estaciones ICAO específicas.
"""
import asyncio
from datetime import datetime, timezone, date
from typing import Optional
import httpx
from config import STATION_COORDS


HEADERS = {
    "User-Agent": "WeatherbotPolymarket/1.0 (trading bot, contact: user@example.com)",
    "Accept": "application/geo+json",
}

# Cache de gridpoints para no consultar repetidamente
_grid_cache: dict[str, dict] = {}


async def _get_gridpoint(station: str, client: httpx.AsyncClient) -> Optional[dict]:
    """Obtiene el gridpoint de NWS para una estacion ICAO."""
    if station in _grid_cache:
        return _grid_cache[station]

    coords = STATION_COORDS.get(station)
    if not coords:
        return None

    lat, lon = coords
    url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
    try:
        resp = await client.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        grid = {
            "office": data["properties"]["gridId"],
            "x": data["properties"]["gridX"],
            "y": data["properties"]["gridY"],
            "forecast_hourly": data["properties"]["forecastHourly"],
        }
        _grid_cache[station] = grid
        return grid
    except Exception:
        return None


async def get_forecast_high(station: str, target_date: date) -> Optional[dict]:
    """
    Obtiene la temperatura maxima pronosticada para una estacion y fecha.

    Returns:
        {
            "high_f": float,        # temperatura maxima en F
            "low_f": float,         # temperatura minima en F
            "std_dev": float,       # incertidumbre estimada en grados F
            "hours_sampled": int,   # cuantas horas de forecast usamos
        }
    """
    async with httpx.AsyncClient() as client:
        grid = await _get_gridpoint(station, client)
        if not grid:
            return None

        try:
            url = grid["forecast_hourly"]
            resp = await client.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            periods = resp.json()["properties"]["periods"]
        except Exception:
            return None

        # Filtrar periodos del dia objetivo
        temps = []
        for period in periods:
            start_str = period.get("startTime", "")
            if not start_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str)
                if start_dt.date() == target_date:
                    temp_f = period.get("temperature")
                    unit = period.get("temperatureUnit", "F")
                    if temp_f is not None:
                        if unit == "C":
                            temp_f = temp_f * 9 / 5 + 32
                        temps.append(float(temp_f))
            except Exception:
                continue

        if not temps:
            return None

        high_f = max(temps)
        low_f = min(temps)

        # Estimacion de incertidumbre basada en horas hasta resolucion
        now = datetime.now(timezone.utc)
        target_dt = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        hours_ahead = max(0, (target_dt - now).total_seconds() / 3600)

        # Incertidumbre empirica: ~1.5F same-day, ~2.5F next-day, ~4F 2+ days
        if hours_ahead < 12:
            std_dev = 1.5
        elif hours_ahead < 30:
            std_dev = 2.5
        else:
            std_dev = 4.0

        return {
            "high_f": high_f,
            "low_f": low_f,
            "std_dev": std_dev,
            "hours_sampled": len(temps),
        }


async def get_current_temp(station: str) -> Optional[float]:
    """Temperatura actual observada en la estacion (en F)."""
    url = f"https://api.weather.gov/stations/{station}/observations/latest"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()
            temp_c = resp.json()["properties"]["temperature"]["value"]
            if temp_c is None:
                return None
            return temp_c * 9 / 5 + 32
    except Exception:
        return None
