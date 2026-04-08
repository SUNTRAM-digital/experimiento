"""
Fase 7 — Ensemble Calibrator

Ajusta los pesos de NOAA vs OpenMeteo vs observacion segun el accuracy
historico de cada fuente por ciudad y temporada.

Principio: si NOAA ha sido mas preciso que OpenMeteo en Chicago en invierno,
darle mas peso en Chicago en invierno. Los pesos son dinamicos, no fijos.

Almacena el historial de errores en disco y los usa para recalibrar.
Si no hay historial suficiente, usa los pesos base del ensemble.

Uso:
  from ml.ensemble_calibrator import EnsembleCalibrator
  cal = EnsembleCalibrator()
  weights = cal.get_weights("KORD", month=1)
  # → {"noaa": 0.55, "openmeteo": 0.35, "obs": 0.10}

  # Cuando se conoce el resultado real del dia:
  cal.record_outcome("KORD", month=1, noaa_pred=88.0, om_pred=85.0, actual=87.5)
"""
import json
import math
from pathlib import Path


_CAL_FILE = Path(__file__).parent / "calibration_data.json"

# Pesos base (Fase 1) — punto de partida antes de tener historial
BASE_WEIGHTS = {
    "noaa":       0.45,
    "openmeteo":  0.45,
    "obs":        0.10,
}

# Minimo de observaciones antes de ajustar los pesos
MIN_OBS_FOR_ADJUSTMENT = 20

# Maximo ajuste respecto a los pesos base (evitar colapsar a una sola fuente)
MAX_WEIGHT_DEVIATION = 0.20

# Temporadas
def _season(month: int) -> str:
    if month in (12, 1, 2):  return "winter"
    if month in (3, 4, 5):   return "spring"
    if month in (6, 7, 8):   return "summer"
    return "fall"


class EnsembleCalibrator:
    """
    Calibrador de pesos del ensemble por estacion ICAO y temporada.
    """

    def __init__(self):
        self._data: dict = self._load()

    def _load(self) -> dict:
        try:
            if _CAL_FILE.exists():
                return json.loads(_CAL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save(self):
        try:
            _CAL_FILE.parent.mkdir(exist_ok=True)
            _CAL_FILE.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _key(self, station: str, month: int) -> str:
        return f"{station}_{_season(month)}"

    def record_outcome(
        self,
        station: str,
        month: int,
        actual_high_f: float,
        noaa_pred: float | None = None,
        openmeteo_pred: float | None = None,
    ):
        """
        Registra el error de prediccion de cada fuente para una estacion/temporada.
        Llamar al final del dia cuando se conoce la temperatura real.

        Args:
            station:        codigo ICAO (ej. "KLGA")
            month:          mes del año (1-12)
            actual_high_f:  temperatura maxima real observada en °F
            noaa_pred:      prediccion NOAA del dia (None si no disponible)
            openmeteo_pred: prediccion OpenMeteo del dia (None si no disponible)
        """
        key = self._key(station, month)
        if key not in self._data:
            self._data[key] = {
                "noaa_errors":       [],
                "openmeteo_errors":  [],
                "n_obs":             0,
            }

        entry = self._data[key]
        entry["n_obs"] += 1

        if noaa_pred is not None:
            error = abs(actual_high_f - noaa_pred)
            entry["noaa_errors"].append(round(error, 2))
            # Mantener solo los ultimos 90 dias
            entry["noaa_errors"] = entry["noaa_errors"][-90:]

        if openmeteo_pred is not None:
            error = abs(actual_high_f - openmeteo_pred)
            entry["openmeteo_errors"].append(round(error, 2))
            entry["openmeteo_errors"] = entry["openmeteo_errors"][-90:]

        self._save()

    def get_weights(self, station: str, month: int) -> dict:
        """
        Retorna los pesos calibrados para esta estacion y mes.

        Si no hay suficiente historial, retorna los pesos base.
        Los pesos siempre suman 1.0.

        Returns:
            {"noaa": float, "openmeteo": float, "obs": float,
             "calibrated": bool, "n_obs": int}
        """
        key   = self._key(station, month)
        entry = self._data.get(key, {})
        n_obs = entry.get("n_obs", 0)

        if n_obs < MIN_OBS_FOR_ADJUSTMENT:
            return {**BASE_WEIGHTS, "calibrated": False, "n_obs": n_obs}

        noaa_errors = entry.get("noaa_errors", [])
        om_errors   = entry.get("openmeteo_errors", [])

        noaa_mae = _mean(noaa_errors) if noaa_errors else None
        om_mae   = _mean(om_errors)   if om_errors   else None

        if noaa_mae is None and om_mae is None:
            return {**BASE_WEIGHTS, "calibrated": False, "n_obs": n_obs}

        # Calcular pesos proporcionales al inverso del error (menor error → mas peso)
        # Si una fuente no tiene datos, usa los pesos base
        if noaa_mae is None:
            w_noaa, w_om = BASE_WEIGHTS["noaa"], BASE_WEIGHTS["openmeteo"]
        elif om_mae is None:
            w_noaa, w_om = BASE_WEIGHTS["noaa"], BASE_WEIGHTS["openmeteo"]
        else:
            # Inverso del MAE como proxy de precision
            inv_noaa = 1.0 / max(noaa_mae, 0.1)
            inv_om   = 1.0 / max(om_mae,   0.1)
            total_inv = inv_noaa + inv_om

            # Distribuir entre noaa y openmeteo (obs siempre queda en 0.10)
            obs_weight  = BASE_WEIGHTS["obs"]
            remaining   = 1.0 - obs_weight
            raw_noaa    = (inv_noaa / total_inv) * remaining
            raw_om      = (inv_om   / total_inv) * remaining

            # Limitar desviacion maxima respecto a los pesos base
            w_noaa = _clamp(
                raw_noaa,
                BASE_WEIGHTS["noaa"] - MAX_WEIGHT_DEVIATION,
                BASE_WEIGHTS["noaa"] + MAX_WEIGHT_DEVIATION,
            )
            w_om = _clamp(
                raw_om,
                BASE_WEIGHTS["openmeteo"] - MAX_WEIGHT_DEVIATION,
                BASE_WEIGHTS["openmeteo"] + MAX_WEIGHT_DEVIATION,
            )

        # Normalizar para que sumen 1.0
        obs_weight = BASE_WEIGHTS["obs"]
        total = w_noaa + w_om + obs_weight
        w_noaa = round(w_noaa / total, 4)
        w_om   = round(w_om   / total, 4)
        w_obs  = round(1.0 - w_noaa - w_om, 4)

        return {
            "noaa":       w_noaa,
            "openmeteo":  w_om,
            "obs":        w_obs,
            "calibrated": True,
            "n_obs":      n_obs,
            "noaa_mae":   round(noaa_mae, 2) if noaa_mae else None,
            "om_mae":     round(om_mae,   2) if om_mae   else None,
        }

    def get_accuracy_report(self, station: str) -> dict:
        """
        Retorna un resumen de accuracy por temporada para una estacion.
        """
        report = {}
        for season in ("winter", "spring", "summer", "fall"):
            key = f"{station}_{season}"
            entry = self._data.get(key, {})
            noaa_errors = entry.get("noaa_errors", [])
            om_errors   = entry.get("openmeteo_errors", [])
            report[season] = {
                "n_obs":     entry.get("n_obs", 0),
                "noaa_mae":  round(_mean(noaa_errors), 2) if noaa_errors else None,
                "om_mae":    round(_mean(om_errors),   2) if om_errors   else None,
            }
        return report


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _clamp(value: float, min_val: float, max_val: float) -> float:
    return max(min_val, min(max_val, value))


# Instancia global
ensemble_calibrator = EnsembleCalibrator()
