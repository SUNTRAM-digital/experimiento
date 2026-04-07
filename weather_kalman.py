"""
Correccion Kalman Gain para temperatura en tiempo real.

Principio: cuanto mas cerca estamos de la hora pico, mas confiamos
en la temperatura observada actualmente vs el forecast del dia anterior.

Pesos calibrados empiricamente basados en el paper de Shanghai ZSPD:
  - 6am  → 20% observacion + 80% forecast
  - 9am  → 35% observacion + 65% forecast
  - Noon → 72% observacion + 28% forecast
  - 1pm+ → 85% observacion + 15% forecast (pico probable ya ocurrido)
  - 2pm+ → lock: la max del dia ya se alcanzo, usar observacion directamente

Adicionalmente aplica descuentos por condiciones adversas al calentamiento:
  - Cobertura de nubes >70%: temperatura pico sera menor
  - Viento fuerte (>15 mph): disipacion de calor
"""
from datetime import datetime, timezone
from typing import Optional


# Hora local a la que se espera el pico de temperatura (en hora UTC offset 0)
# Ajustar segun ciudad si se tiene ese dato
DEFAULT_PEAK_HOUR_UTC_OFFSET = 0  # trabajamos en hora local del forecast


def _kalman_weight(current_hour_local: int) -> float:
    """
    Devuelve el peso de la observacion actual (0.0 a 1.0).
    El resto del peso va al forecast.
    Basado en la curva de Kalman Gain del paper de Shanghai.
    """
    if current_hour_local < 6:
        return 0.10   # Muy temprano: casi todo el peso en el forecast
    elif current_hour_local < 8:
        return 0.20
    elif current_hour_local < 10:
        return 0.35
    elif current_hour_local < 11:
        return 0.50
    elif current_hour_local < 12:
        return 0.65
    elif current_hour_local < 13:
        return 0.72
    elif current_hour_local < 14:
        return 0.80
    else:
        return 0.85   # Tarde: la observacion domina, el pico ya ocurrio


def apply_kalman_correction(
    forecast_high_f: float,
    current_obs_f: Optional[float],
    current_hour_local: int,
    cloud_cover_pct: Optional[float] = None,
    wind_speed_mph: Optional[float] = None,
) -> dict:
    """
    Combina el forecast con la observacion actual usando Kalman Gain.

    Args:
        forecast_high_f:   temperatura maxima del forecast (en F)
        current_obs_f:     temperatura observada AHORA en la estacion (en F), puede ser None
        current_hour_local: hora local actual (0-23)
        cloud_cover_pct:   cobertura de nubes en % (0-100), opcional
        wind_speed_mph:    velocidad del viento en mph, opcional

    Returns:
        {
            "corrected_high_f": float,    # estimacion corregida
            "kalman_weight_obs": float,   # peso dado a la observacion
            "cloud_discount_f": float,    # descuento aplicado por nubes (en grados F)
            "wind_discount_f": float,     # descuento aplicado por viento
            "std_dev_adjusted": float,    # incertidumbre ajustada post-correccion
            "peak_locked": bool,          # True si consideramos que el pico ya ocurrio
        }
    """
    weight_obs = _kalman_weight(current_hour_local)
    peak_locked = current_hour_local >= 14

    # Si no hay observacion, usar solo el forecast
    if current_obs_f is None:
        blended = forecast_high_f
        weight_obs = 0.0
    elif peak_locked:
        # Despues de las 2pm: si la observacion actual > forecast, actualizar
        # Si la observacion < forecast, el pico probablemente ya ocurrio a un valor
        # intermedio; usar el maximo entre ambos como mejor estimacion
        blended = max(current_obs_f, forecast_high_f * 0.95)
    else:
        weight_fc = 1.0 - weight_obs
        blended = weight_fc * forecast_high_f + weight_obs * current_obs_f

    # Descuento por nubosidad (nubes bloquean calentamiento solar)
    # Calibrado: 100% nubes puede reducir el pico ~2-3°F respecto al pronostico
    cloud_discount = 0.0
    if cloud_cover_pct is not None and cloud_cover_pct > 60:
        excess = (cloud_cover_pct - 60) / 40.0  # 0 a 1 cuando pasa de 60% a 100%
        cloud_discount = excess * 2.5            # maximo descuento: 2.5°F

    # Descuento por viento (disipacion de calor)
    # >15 mph empieza a enfriar significativamente
    wind_discount = 0.0
    if wind_speed_mph is not None and wind_speed_mph > 15:
        excess = (wind_speed_mph - 15) / 20.0  # 0 a 1 entre 15 y 35 mph
        wind_discount = min(excess * 2.0, 2.0)  # maximo descuento: 2°F

    corrected = blended - cloud_discount - wind_discount

    # Incertidumbre se reduce conforme avanza el dia y tenemos mas observaciones
    if peak_locked:
        std_adjusted = 1.5  # pico ya ocurrio, alta certeza
    elif current_obs_f is not None:
        # Reducir std_dev segun cuanto peso ya tiene la observacion
        base_std = 3.0
        std_adjusted = base_std * (1.0 - weight_obs * 0.5)
    else:
        std_adjusted = 4.0

    return {
        "corrected_high_f": round(corrected, 2),
        "kalman_weight_obs": round(weight_obs, 2),
        "cloud_discount_f": round(cloud_discount, 2),
        "wind_discount_f": round(wind_discount, 2),
        "std_dev_adjusted": round(std_adjusted, 2),
        "peak_locked": peak_locked,
    }
