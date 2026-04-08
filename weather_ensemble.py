"""
Weather Ensemble - motor central de prediccion meteorologica.

Combina tres fuentes de datos en un forecast unificado:
  1. NOAA/NWS  (weather.py)  - dato oficial US, alta precision local
  2. Open-Meteo multi-modelo  - consenso GFS + ECMWF + ensemble
  3. Observacion actual       - temperatura medida en la estacion ICAO ahora mismo

Fase 7: los pesos NOAA/OpenMeteo son dinamicos (EnsembleCalibrator por estacion+temporada)
         y el forecast final se ajusta segun el modelo WarmingModel (+/-2 F).

Flujo:
  get_ensemble_high()
    └── NOAA forecast  ──┐
    └── OpenMeteo multi  ├──► blend calibrado ──► Kalman ──► warming adjust ──► resultado
    └── current obs    ──┘

El resultado incluye:
  - high_f:        mejor estimacion de la maxima del dia
  - std_dev:       incertidumbre total (menor si modelos coinciden + obs disponible)
  - confidence:    "high" / "medium" / "low"
  - sources:       detalle de cada fuente para debugging / Telegram
"""
import asyncio
from datetime import date, datetime, timezone
from typing import Optional

from weather import get_forecast_high, get_current_temp
from weather_openmeteo import get_ensemble_forecast
from weather_kalman import apply_kalman_correction
from ml.ensemble_calibrator import ensemble_calibrator
from ml.warming_model import WarmingModel

_warming_model = WarmingModel()

# Pesos base de cada fuente — se usan solo si EnsembleCalibrator no tiene historial
WEIGHT_NOAA       = 0.45
WEIGHT_OPENMETEO  = 0.45
WEIGHT_OBS_BONUS  = 0.10


async def get_ensemble_high(
    station: str,
    target_date: date,
    ml_features: Optional[dict] = None,
) -> Optional[dict]:
    """
    Obtiene la mejor estimacion posible de la temperatura maxima del dia.

    Args:
        station:     codigo ICAO (ej. "KLGA")
        target_date: fecha objetivo
        ml_features: features opcionales para el WarmingModel (Fase 7):
            {
                "pressure_change_3h":  float,  # hPa, positivo = subida
                "pressure_change_12h": float,
                "predawn_wind_speed":  float,  # mph
                "cloud_cover_pct":     float,  # 0-100
                "temp_trend_3d":       float,  # F/dia promedio 3 dias
                "rained_yesterday":    bool | int,
            }
            El mes se deriva automaticamente de target_date.

    Returns None si no hay suficientes datos (todas las fuentes fallaron).

    Returns dict:
    {
        "high_f":          float,   # estimacion final en Fahrenheit
        "std_dev":         float,   # incertidumbre en grados F
        "confidence":      str,     # "high" | "medium" | "low"
        "confidence_boost":float,   # 0.0-0.3 bonus por consenso multi-modelo

        # Desglose por fuente
        "noaa_high_f":     float | None,
        "openmeteo_high_f":float | None,
        "current_obs_f":   float | None,
        "kalman_weight":   float,    # peso dado a la obs actual
        "peak_locked":     bool,

        # Detalles Open-Meteo
        "model_highs":     dict,     # {"gfs_seamless": X, "ecmwf_ifs025": Y, ...}
        "models_available":int,
        "consensus_std":   float,    # desacuerdo entre modelos (°F)

        # Descuentos meteorologicos aplicados
        "cloud_discount_f":float,
        "wind_discount_f": float,

        "sources_used":    list[str],

        # Fase 7 — ML
        "ensemble_weights":     dict,   # {"noaa": float, "openmeteo": float, "calibrated": bool}
        "warming_prediction":   dict | None,   # resultado WarmingModel
        "warming_adjustment_f": float,         # ajuste aplicado al forecast (F)
    }
    """
    now_utc = datetime.now(timezone.utc)
    current_hour_local = now_utc.hour  # aprox, sin ajuste de timezone

    # Lanzar todas las consultas en paralelo
    noaa_task      = get_forecast_high(station, target_date)
    openmeteo_task = get_ensemble_forecast(station, target_date)
    obs_task       = get_current_temp(station)

    noaa_result, openmeteo_result, current_obs = await asyncio.gather(
        noaa_task, openmeteo_task, obs_task
    )

    # --- Extraer valores individuales ---
    noaa_high      = noaa_result["high_f"] if noaa_result else None
    noaa_std       = noaa_result["std_dev"] if noaa_result else None
    openmeteo_high = openmeteo_result["high_f"] if openmeteo_result else None
    consensus_std  = openmeteo_result["consensus_std"] if openmeteo_result else 4.0
    confidence_boost = openmeteo_result["confidence_boost"] if openmeteo_result else 0.0
    model_highs    = openmeteo_result["model_highs"] if openmeteo_result else {}
    models_avail   = openmeteo_result["models_available"] if openmeteo_result else 0

    # Fase 7: obtener pesos calibrados por estacion + temporada
    cal_weights = ensemble_calibrator.get_weights(station, target_date.month)
    w_noaa      = cal_weights["noaa"]
    w_om        = cal_weights["openmeteo"]

    sources_used = []
    forecast_values = []
    forecast_weights = []

    if noaa_high is not None:
        sources_used.append("NOAA")
        forecast_values.append(noaa_high)
        forecast_weights.append(w_noaa)

    if openmeteo_high is not None:
        sources_used.append("OpenMeteo")
        forecast_values.append(openmeteo_high)
        forecast_weights.append(w_om)

    if not forecast_values:
        return None  # Sin datos no hay nada que hacer

    # Blend ponderado de fuentes disponibles (normalizar pesos)
    total_w = sum(forecast_weights)
    blended_forecast = sum(v * w for v, w in zip(forecast_values, forecast_weights)) / total_w

    # std_dev base: tomar la de NOAA si existe, sino usar consenso_std de openmeteo
    if noaa_std is not None:
        base_std = noaa_std
    else:
        base_std = max(consensus_std, 2.0)  # minimo 2°F de incertidumbre

    # Reducir std_dev si los modelos coinciden
    if consensus_std < 1.0:
        base_std = max(base_std - 1.0, 1.5)
    elif consensus_std < 2.0:
        base_std = max(base_std - 0.5, 2.0)

    # Aplicar corrección Kalman con la observacion actual
    kalman = apply_kalman_correction(
        forecast_high_f=blended_forecast,
        current_obs_f=current_obs,
        current_hour_local=current_hour_local,
    )

    final_high = kalman["corrected_high_f"]
    final_std  = kalman["std_dev_adjusted"]

    if current_obs is not None:
        sources_used.append("Obs")

    # Fase 7: aplicar ajuste del WarmingModel si hay features disponibles
    warming_prediction   = None
    warming_adjustment_f = 0.0
    if ml_features is not None:
        features_with_month = {**ml_features, "month": target_date.month}
        warming_prediction = _warming_model.predict(features_with_month)
        warming_adjustment_f = _warming_model.to_forecast_adjustment(
            warming_prediction, final_high
        )
        final_high = final_high + warming_adjustment_f

    # Determinar nivel de confianza general
    n_sources = len(sources_used)
    if n_sources >= 3 and consensus_std < 1.5:
        confidence = "high"
    elif n_sources >= 2 or consensus_std < 2.5:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "high_f":           round(final_high, 1),
        "std_dev":          round(final_std, 2),
        "confidence":       confidence,
        "confidence_boost": confidence_boost,

        "noaa_high_f":      round(noaa_high, 1) if noaa_high is not None else None,
        "openmeteo_high_f": round(openmeteo_high, 1) if openmeteo_high is not None else None,
        "current_obs_f":    round(current_obs, 1) if current_obs is not None else None,
        "kalman_weight":    kalman["kalman_weight_obs"],
        "peak_locked":      kalman["peak_locked"],

        "model_highs":      model_highs,
        "models_available": models_avail,
        "consensus_std":    consensus_std,

        "cloud_discount_f": kalman["cloud_discount_f"],
        "wind_discount_f":  kalman["wind_discount_f"],

        "sources_used":     sources_used,

        # Fase 7 — ML
        "ensemble_weights":     cal_weights,
        "warming_prediction":   warming_prediction,
        "warming_adjustment_f": round(warming_adjustment_f, 2),
    }
