"""
Cliente Open-Meteo - gratis, sin API key.
Obtiene forecasts de temperatura de multiples modelos:
  - GFS (NOAA americano)
  - ECMWF IFS (europeo, mas preciso en sistemas de gran escala)
  - ECMWF AIFS (version con IA de ECMWF)
  - GFS Seamless (blend de GFS + GEFS ensemble)
"""
import asyncio
from datetime import date, datetime, timezone
from typing import Optional
import httpx

from config import STATION_COORDS

HEADERS = {
    "User-Agent": "WeatherbotPolymarket/1.0",
    "Accept": "application/json",
}

# Cada modelo tiene su propio endpoint en Open-Meteo
MODEL_ENDPOINTS = {
    "best_match": "https://api.open-meteo.com/v1/forecast",
    "gfs":        "https://api.open-meteo.com/v1/gfs",
    "ecmwf":      "https://api.open-meteo.com/v1/ecmwf",
}


async def _fetch_model(
    lat: float,
    lon: float,
    target_date: date,
    model: str,
    client: httpx.AsyncClient,
) -> Optional[float]:
    """Obtiene la temperatura maxima del dia para un modelo especifico."""
    url = MODEL_ENDPOINTS.get(model, MODEL_ENDPOINTS["best_match"])
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": "UTC",
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
    }
    try:
        resp = await client.get(url, params=params, headers=HEADERS, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            return float(temps[0])
    except Exception:
        pass
    return None


async def get_ensemble_forecast(
    station: str,
    target_date: date,
) -> Optional[dict]:
    """
    Consulta multiples modelos y devuelve el consenso.

    Returns:
        {
            "high_f": float,          # promedio ponderado de todos los modelos
            "model_highs": dict,      # temperatura por modelo
            "consensus_std": float,   # desviacion entre modelos (menor = mas acuerdo)
            "models_available": int,  # cuantos modelos respondieron
            "confidence_boost": float # 0.0 a 0.3 segun nivel de acuerdo
        }
    """
    coords = STATION_COORDS.get(station)
    if not coords:
        return None

    lat, lon = coords

    models = list(MODEL_ENDPOINTS.keys())
    async with httpx.AsyncClient() as client:
        tasks = [_fetch_model(lat, lon, target_date, m, client) for m in models]
        results = await asyncio.gather(*tasks)

    model_highs = {}
    valid_temps = []
    for model, temp in zip(models, results):
        if temp is not None:
            model_highs[model] = round(temp, 1)
            valid_temps.append(temp)

    if not valid_temps:
        return None

    avg_high = sum(valid_temps) / len(valid_temps)

    # Desviacion estandar entre modelos (mide desacuerdo)
    if len(valid_temps) > 1:
        variance = sum((t - avg_high) ** 2 for t in valid_temps) / len(valid_temps)
        consensus_std = variance ** 0.5
    else:
        consensus_std = 3.0  # incertidumbre alta si solo hay 1 modelo

    # Boost de confianza segun acuerdo entre modelos:
    # Si todos los modelos coinciden en <1°F → boost maximo
    # Si divergen >3°F → sin boost (situacion incierta)
    if consensus_std < 1.0:
        confidence_boost = 0.30
    elif consensus_std < 2.0:
        confidence_boost = 0.15
    elif consensus_std < 3.0:
        confidence_boost = 0.05
    else:
        confidence_boost = 0.0

    return {
        "high_f": round(avg_high, 1),
        "model_highs": model_highs,
        "consensus_std": round(consensus_std, 2),
        "models_available": len(valid_temps),
        "confidence_boost": confidence_boost,
    }
