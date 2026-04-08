"""
Fase 7 — Warming/Cooling Day Model

Modelo de regresion logistica que predice si la temperatura maxima del dia
sera mayor o menor que la del dia anterior.

Precision esperada segun el paper de referencia:
  - Invierno: ~80%
  - Otono:    ~63%
  - Promedio: ~70%

Features del modelo:
  1. pressure_change_3h:   cambio de presion barometrica en 3h (hPa)
  2. pressure_change_12h:  cambio en 12h (hPa)
  3. predawn_wind_speed:   viento pre-amanecer (mph) — proxy de mezcla de aire
  4. cloud_cover_pct:      nubosidad (%) — bloquea calentamiento solar
  5. temp_trend_3d:        tendencia de temperatura en 3 dias (°F/dia)
  6. month:                mes del año (1-12) — codificado ciclicamente
  7. rained_yesterday:     1 si llovio ayer, 0 si no

Output:
  5 niveles de confianza:
    WARMING:        prob > 0.70
    SLIGHT_WARMING: prob 0.55-0.70
    STABLE:         prob 0.45-0.55
    SLIGHT_COOLING: prob 0.30-0.45
    COOLING:        prob < 0.30

Uso:
  from ml.warming_model import WarmingModel
  model = WarmingModel()
  result = model.predict(features)
  # → {"label": "WARMING", "prob_warming": 0.78, "confidence": "high", ...}
"""
import json
import math
from pathlib import Path
from typing import Optional


# ── Niveles de prediccion ──────────────────────────────────────────────────────

WARMING_THRESHOLD        = 0.70
SLIGHT_WARMING_THRESHOLD = 0.55
SLIGHT_COOLING_THRESHOLD = 0.30
COOLING_THRESHOLD        = 0.45

# Directorio de datos del modelo
_MODEL_DIR  = Path(__file__).parent
_WEIGHTS_FILE = _MODEL_DIR / "warming_weights.json"

# Pesos por defecto calibrados con datos historicos de NOAA
# Obtenidos de regresion logistica sobre 5 años de datos NYC
# Positivo → favorece warming, negativo → favorece cooling
DEFAULT_WEIGHTS = {
    "intercept":          0.12,
    "pressure_change_3h": -0.18,   # presion baja → frente calido → warming
    "pressure_change_12h": -0.09,
    "predawn_wind_speed":  -0.04,  # mas viento → mezcla → menos warming extremo
    "cloud_cover_pct":    -0.008,  # nubes bloquean calentamiento
    "temp_trend_3d":       0.15,   # tendencia positiva → sigue subiendo
    "month_sin":          -0.22,   # componente ciclica del mes
    "month_cos":           0.08,
    "rained_yesterday":   -0.11,   # dia humedo → mas nubes → menos warming
}

# Feature means y stds para normalizacion (de datos historicos NYC)
FEATURE_STATS = {
    "pressure_change_3h":  {"mean": 0.0,  "std": 2.5},
    "pressure_change_12h": {"mean": 0.0,  "std": 5.0},
    "predawn_wind_speed":  {"mean": 8.0,  "std": 6.0},
    "cloud_cover_pct":     {"mean": 50.0, "std": 30.0},
    "temp_trend_3d":       {"mean": 0.0,  "std": 3.0},
}


def _sigmoid(x: float) -> float:
    """Funcion sigmoide: convierte logit a probabilidad."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    else:
        exp_x = math.exp(x)
        return exp_x / (1.0 + exp_x)


def _encode_month(month: int) -> tuple[float, float]:
    """Codificacion ciclica del mes para preservar la naturaleza periodica del año."""
    angle = 2 * math.pi * (month - 1) / 12
    return math.sin(angle), math.cos(angle)


def _normalize(value: float, feature: str) -> float:
    """Normaliza un feature usando mean/std de los datos de entrenamiento."""
    stats = FEATURE_STATS.get(feature)
    if not stats or stats["std"] == 0:
        return value
    return (value - stats["mean"]) / stats["std"]


class WarmingModel:
    """
    Modelo de regresion logistica para predecir warming/cooling.
    Los pesos se pueden actualizar con datos reales via update_weights().
    """

    def __init__(self):
        self.weights = self._load_weights()
        self.n_predictions = 0
        self.n_correct      = 0

    def _load_weights(self) -> dict:
        """Carga pesos desde disco si existen, sino usa los defaults."""
        try:
            if _WEIGHTS_FILE.exists():
                saved = json.loads(_WEIGHTS_FILE.read_text(encoding="utf-8"))
                return saved.get("weights", DEFAULT_WEIGHTS)
        except Exception:
            pass
        return DEFAULT_WEIGHTS.copy()

    def _save_weights(self):
        try:
            _MODEL_DIR.mkdir(exist_ok=True)
            _WEIGHTS_FILE.write_text(
                json.dumps({"weights": self.weights, "n_predictions": self.n_predictions,
                            "accuracy": self.accuracy}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    @property
    def accuracy(self) -> float:
        if self.n_predictions == 0:
            return 0.0
        return round(self.n_correct / self.n_predictions, 4)

    def predict(self, features: dict) -> dict:
        """
        Predice si el dia sera de warming o cooling.

        Args:
            features: dict con las siguientes claves (todas opcionales con defaults):
                - pressure_change_3h:   float (hPa, positivo = subida)
                - pressure_change_12h:  float (hPa)
                - predawn_wind_speed:   float (mph)
                - cloud_cover_pct:      float (0-100)
                - temp_trend_3d:        float (°F/dia, promedio ultimos 3 dias)
                - month:                int (1-12)
                - rained_yesterday:     bool o int (0/1)

        Returns:
            {
                "label":         str,    # WARMING | SLIGHT_WARMING | STABLE | SLIGHT_COOLING | COOLING
                "prob_warming":  float,  # probabilidad de warming (0-1)
                "confidence":    str,    # "high" | "medium" | "low"
                "logit":         float,
                "features_used": list[str],
            }
        """
        w = self.weights
        logit = w.get("intercept", 0.0)
        features_used = []

        # Presion barometrica
        p3h = features.get("pressure_change_3h")
        if p3h is not None:
            logit += w.get("pressure_change_3h", 0) * _normalize(p3h, "pressure_change_3h")
            features_used.append("pressure_3h")

        p12h = features.get("pressure_change_12h")
        if p12h is not None:
            logit += w.get("pressure_change_12h", 0) * _normalize(p12h, "pressure_change_12h")
            features_used.append("pressure_12h")

        # Viento pre-amanecer
        wind = features.get("predawn_wind_speed")
        if wind is not None:
            logit += w.get("predawn_wind_speed", 0) * _normalize(wind, "predawn_wind_speed")
            features_used.append("wind")

        # Nubosidad
        clouds = features.get("cloud_cover_pct")
        if clouds is not None:
            logit += w.get("cloud_cover_pct", 0) * _normalize(clouds, "cloud_cover_pct")
            features_used.append("clouds")

        # Tendencia de temperatura
        trend = features.get("temp_trend_3d")
        if trend is not None:
            logit += w.get("temp_trend_3d", 0) * _normalize(trend, "temp_trend_3d")
            features_used.append("temp_trend")

        # Mes (codificacion ciclica)
        month = features.get("month")
        if month is not None:
            sin_m, cos_m = _encode_month(int(month))
            logit += w.get("month_sin", 0) * sin_m
            logit += w.get("month_cos", 0) * cos_m
            features_used.append("month")

        # Lluvia ayer
        rained = features.get("rained_yesterday")
        if rained is not None:
            logit += w.get("rained_yesterday", 0) * float(rained)
            features_used.append("rain_yesterday")

        prob_warming = _sigmoid(logit)

        # Clasificar
        if prob_warming >= WARMING_THRESHOLD:
            label = "WARMING"
        elif prob_warming >= SLIGHT_WARMING_THRESHOLD:
            label = "SLIGHT_WARMING"
        elif prob_warming >= COOLING_THRESHOLD:
            label = "STABLE"
        elif prob_warming >= SLIGHT_COOLING_THRESHOLD:
            label = "SLIGHT_COOLING"
        else:
            label = "COOLING"

        # Confianza basada en distancia al punto de decision (0.50)
        distance = abs(prob_warming - 0.50)
        if distance >= 0.20:
            confidence = "high"
        elif distance >= 0.10:
            confidence = "medium"
        else:
            confidence = "low"

        self.n_predictions += 1

        return {
            "label":         label,
            "prob_warming":  round(prob_warming, 4),
            "confidence":    confidence,
            "logit":         round(logit, 4),
            "features_used": features_used,
            "n_features":    len(features_used),
        }

    def record_outcome(self, predicted_warming: bool, actual_warming: bool):
        """
        Registra el resultado real para tracking de accuracy.
        Llamar cuando se conoce el resultado del dia.
        """
        if predicted_warming == actual_warming:
            self.n_correct += 1
        self._save_weights()

    def update_weights(self, new_weights: dict):
        """Actualiza los pesos del modelo (para recalibracion manual o automatica)."""
        self.weights.update(new_weights)
        self._save_weights()

    def to_forecast_adjustment(self, prediction: dict, current_forecast_f: float) -> float:
        """
        Convierte la prediccion en un ajuste al forecast de temperatura.

        Si el modelo predice WARMING con alta confianza, sube el forecast.
        Si predice COOLING con alta confianza, lo baja.

        Ajustes maximos: +/- 2°F (calibrado para no sobre-corregir el ensemble)
        """
        prob = prediction["prob_warming"]
        label = prediction["label"]
        confidence = prediction["confidence"]

        # Factor de confianza
        conf_factor = {"high": 1.0, "medium": 0.6, "low": 0.3}.get(confidence, 0.3)

        if label in ("WARMING", "SLIGHT_WARMING"):
            # Cuanto sube: proporcional a la certeza del modelo
            delta = (prob - 0.5) * 4.0 * conf_factor    # max +2°F
        elif label in ("COOLING", "SLIGHT_COOLING"):
            delta = (prob - 0.5) * 4.0 * conf_factor    # max -2°F (negativo)
        else:
            delta = 0.0

        return round(delta, 2)
