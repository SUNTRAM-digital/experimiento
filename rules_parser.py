"""
Fase 4 — The Lawyer's Edge: Parser de reglas de resolucion.

Los pros "tradean la formulacion", no el evento.
"Will the HIGH temperature exceed 90°F at KLGA on July 4th?" y
"Will it be warmer than 90°F in New York on July 4th?" son trades DISTINTOS.

Este modulo:
  1. Extrae la estacion ICAO exacta del titulo del mercado
  2. Determina el tipo de dato (intraday high, settlement, etc.)
  3. Detecta la unidad (°F vs °C)
  4. Detecta si el forecast esta en zona critica: ±1.5°F del limite del bucket
     → maxima oportunidad de edge cuando el mercado no sabe si cruza el umbral

Uso:
  from rules_parser import parse_market_rules, detect_boundary_zone

  rules = parse_market_rules(market_title, condition_id)
  boundary = detect_boundary_zone(forecast_high_f, bucket_low, bucket_high)
"""
import re
from typing import Optional


# ── Mapa completo ICAO ─────────────────────────────────────────────────────────
# Fuente: Polymarket usa estas estaciones para el settlement de temperatura.
# CRITICO: ciudad ≠ aeropuerto principal. Dallas usa KDAL (Love Field), no KDFW.
# NYC usa KLGA (LaGuardia), no KJFK ni KEWR.

ICAO_MAP: dict[str, str] = {
    # USA — Este
    "new york":     "KLGA",
    "nyc":          "KLGA",
    "laguardia":    "KLGA",
    "boston":       "KBOS",
    "miami":        "KMIA",
    "atlanta":      "KATL",
    "washington":   "KDCA",
    "dc":           "KDCA",
    "philadelphia": "KPHL",
    "charlotte":    "KCLT",
    "baltimore":    "KBWI",
    "orlando":      "KMCO",
    "tampa":        "KTPA",
    "raleigh":      "KRDU",
    "pittsburgh":   "KPIT",
    "cleveland":    "KCLE",
    "detroit":      "KDTW",
    "buffalo":      "KBUF",
    "jacksonville": "KJAX",
    # USA — Centro
    "chicago":      "KORD",
    "dallas":       "KDAL",   # Love Field, NO es DFW
    "houston":      "KHOU",   # Hobby, NO es KIAH
    "minneapolis":  "KMSP",
    "kansas city":  "KMCI",
    "st louis":     "KSTL",
    "indianapolis": "KIND",
    "nashville":    "KBNA",
    "memphis":      "KMEM",
    "oklahoma city":"KOKC",
    "austin":       "KAUS",
    "san antonio":  "KSAT",
    "omaha":        "KOMA",
    "milwaukee":    "KMKE",
    # USA — Oeste
    "los angeles":  "KLAX",
    "san francisco":"KSFO",
    "seattle":      "KSEA",
    "portland":     "KPDX",
    "denver":       "KDEN",
    "phoenix":      "KPHX",
    "las vegas":    "KLAS",
    "salt lake":    "KSLC",
    "san diego":    "KSAN",
    "sacramento":   "KSMF",
    "tucson":       "KTUS",
    "albuquerque":  "KABQ",
    "anchorage":    "PANC",
    "honolulu":     "PHNL",
    # Internacional
    "london":       "EGLL",
    "paris":        "LFPG",
    "berlin":       "EDDB",
    "amsterdam":    "EHAM",
    "madrid":       "LEMD",
    "rome":         "LIRF",
    "tokyo":        "RJTT",
    "sydney":       "YSSY",
    "toronto":      "CYYZ",
    "montreal":     "CYUL",
    "mexico city":  "MMMX",
}

# Aliases tipograficos que aparecen en titulos de Polymarket
_TITLE_ALIASES: dict[str, str] = {
    "new york city": "new york",
    "n.y.c":         "new york",
    "ny":            "new york",
    "l.a.":          "los angeles",
    "la":            "los angeles",
    "s.f.":          "san francisco",
    "sf":            "san francisco",
    "d.c.":          "dc",
    "chi":           "chicago",
    "phx":           "phoenix",
    "pdx":           "portland",
    "sea":           "seattle",
}

# ── Tipos de dato de settlement ────────────────────────────────────────────────

DATA_TYPE_PATTERNS: list[tuple[str, str]] = [
    (r"\bhigh\b",           "intraday_high"),   # "high temperature"
    (r"\bmax\b",            "intraday_high"),
    (r"\bmaximum\b",        "intraday_high"),
    (r"\bsettlement\b",     "settlement"),
    (r"\bclosing\b",        "settlement"),
    (r"\baverage\b",        "daily_average"),
    (r"\bmean\b",           "daily_average"),
    (r"\blow\b",            "intraday_low"),
    (r"\bminimum\b",        "intraday_low"),
]

# ── Detectores de unidad ───────────────────────────────────────────────────────

_UNIT_F = re.compile(r"°?\s*f(?:ahrenheit)?\b", re.IGNORECASE)
_UNIT_C = re.compile(r"°?\s*c(?:elsius|elcius)?\b", re.IGNORECASE)

# ── Detector de temperatura en el titulo ──────────────────────────────────────

_TEMP_PATTERN = re.compile(
    r"(\d{1,3}(?:\.\d)?)\s*°?\s*([FCfc])\b"
)

# ── Funciones principales ──────────────────────────────────────────────────────

def extract_icao_from_title(title: str) -> Optional[str]:
    """
    Intenta identificar la estacion ICAO a partir del titulo del mercado.

    Busca patrones como:
      - "KLGA" explicito en el titulo
      - Ciudad conocida: "New York high temperature..."

    Returns: codigo ICAO (ej. "KLGA") o None si no se puede determinar.
    """
    title_lower = title.lower()

    # 1. ICAO explicito en el titulo (ej. "KLGA temperature")
    icao_match = re.search(r"\b([A-Z]{4})\b", title)
    if icao_match:
        candidate = icao_match.group(1)
        # Verificar que sea un codigo ICAO conocido
        all_icao = set(ICAO_MAP.values())
        if candidate in all_icao:
            return candidate

    # 2. Alias primero (mas especificos)
    for alias, canonical in _TITLE_ALIASES.items():
        if alias in title_lower:
            icao = ICAO_MAP.get(canonical)
            if icao:
                return icao

    # 3. Nombre de ciudad directo
    for city, icao in ICAO_MAP.items():
        if city in title_lower:
            return icao

    return None


def extract_data_type(title: str) -> str:
    """
    Determina el tipo de dato de settlement desde el titulo.
    Returns: "intraday_high" | "intraday_low" | "daily_average" | "settlement" | "unknown"
    """
    title_lower = title.lower()
    for pattern, dtype in DATA_TYPE_PATTERNS:
        if re.search(pattern, title_lower):
            return dtype
    return "unknown"


def extract_unit(title: str) -> str:
    """
    Determina la unidad de temperatura del titulo.
    Returns: "F" | "C" | "unknown"
    """
    if _UNIT_F.search(title):
        return "F"
    if _UNIT_C.search(title):
        return "C"
    # Heuristica: si hay un numero seguido de F/C sin simbolo
    match = _TEMP_PATTERN.search(title)
    if match:
        unit = match.group(2).upper()
        return unit if unit in ("F", "C") else "unknown"
    return "unknown"


def extract_bucket_thresholds(title: str) -> Optional[dict]:
    """
    Extrae los limites del bucket de temperatura desde el titulo.

    Maneja patrones comunes de Polymarket:
      "Will the high temp exceed 90°F?"        → above 90
      "Will it be at least 85°F?"              → above_or_eq 85
      "Will the high be below 70°F?"           → below 70
      "Will the high be between 80-85°F?"      → range 80-85
      "High temperature of 85-90°F?"           → range 85-90

    Returns:
        {"type": "above"|"below"|"range", "low": float, "high": float}
        or None if not detected.
    """
    title_lower = title.lower()
    temps = _TEMP_PATTERN.findall(title)
    values = [float(t[0]) for t in temps]

    if not values:
        # Buscar numeros simples sin unidad
        nums = re.findall(r"\b(\d{2,3})\b", title)
        # Filtrar a rangos razonables de temperatura
        values = [float(n) for n in nums if 20 <= float(n) <= 130]

    if not values:
        return None

    # Detectar tipo
    if any(kw in title_lower for kw in ["exceed", "above", "over", "more than", "at least", "or higher"]):
        return {"type": "above", "low": values[0], "high": 999.0}

    if any(kw in title_lower for kw in ["below", "under", "less than", "or lower", "at most"]):
        return {"type": "below", "low": -999.0, "high": values[0]}

    if len(values) >= 2 or any(kw in title_lower for kw in ["between", "-", "to", "and"]):
        vals = sorted(values[:2])
        return {"type": "range", "low": vals[0], "high": vals[1]}

    # Solo un numero sin contexto de dirección — asumir above
    return {"type": "above", "low": values[0], "high": 999.0}


def parse_market_rules(title: str, condition_id: str = "") -> dict:
    """
    Analiza completamente las reglas de un mercado de temperatura.

    Returns:
        {
            "icao":          str | None,   # estacion ICAO detectada
            "city":          str | None,   # ciudad detectada
            "data_type":     str,          # intraday_high | settlement | etc.
            "unit":          str,          # F | C | unknown
            "bucket":        dict | None,  # {type, low, high}
            "warnings":      list[str],    # alertas sobre reglas ambiguas
            "confidence":    str,          # "high" | "medium" | "low"
        }
    """
    icao      = extract_icao_from_title(title)
    data_type = extract_data_type(title)
    unit      = extract_unit(title)
    bucket    = extract_bucket_thresholds(title)

    # Identificar ciudad desde el ICAO
    city = None
    if icao:
        reverse_map = {v: k for k, v in ICAO_MAP.items()}
        city = reverse_map.get(icao)

    # Alertas de reglas ambiguas
    warnings = []

    if icao is None:
        warnings.append("No se pudo identificar la estacion ICAO — verificar manualmente")

    if data_type == "unknown":
        warnings.append("Tipo de dato de settlement no claro (high vs settlement vs average)")

    if unit == "unknown":
        warnings.append("Unidad de temperatura no detectada (F vs C)")

    if bucket is None:
        warnings.append("No se pudo extraer el umbral de temperatura del titulo")

    # Advertencias criticas de estaciones confundidas
    title_lower = title.lower()
    if "new york" in title_lower and icao != "KLGA":
        warnings.append("ALERTA: NYC debe usar KLGA (LaGuardia), no JFK/EWR")
    if "dallas" in title_lower and icao not in ("KDAL", None):
        warnings.append("ALERTA: Dallas usa KDAL (Love Field), no KDFW")
    if "houston" in title_lower and icao not in ("KHOU", None):
        warnings.append("ALERTA: Houston usa KHOU (Hobby), no KIAH")

    # Nivel de confianza del parser
    fields_ok = sum([icao is not None, data_type != "unknown", unit != "unknown", bucket is not None])
    if fields_ok == 4:
        confidence = "high"
    elif fields_ok >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "icao":       icao,
        "city":       city,
        "data_type":  data_type,
        "unit":       unit,
        "bucket":     bucket,
        "warnings":   warnings,
        "confidence": confidence,
    }


# ── Detector de zona critica (boundary zone) ──────────────────────────────────

BOUNDARY_ZONE_F = 1.5   # Distancia en °F al limite que define "zona critica"


def detect_boundary_zone(
    forecast_high_f: float,
    std_dev: float,
    bucket: dict,
) -> dict:
    """
    Detecta si el forecast esta en la zona critica cerca de un limite de bucket.

    Zona critica = forecast dentro de BOUNDARY_ZONE_F del limite.
    En esta zona el mercado tiene maxima incertidumbre → mayor edge potencial.

    Args:
        forecast_high_f: temperatura forecast en °F
        std_dev:         incertidumbre del forecast en °F
        bucket:          {"type": str, "low": float, "high": float}

    Returns:
        {
            "in_boundary_zone":  bool,
            "distance_to_limit": float,    # °F al limite mas cercano
            "limit_f":           float,    # cual es el limite critico
            "direction":         str,      # "above_limit" | "below_limit"
            "edge_quality":      str,      # "maximum" | "high" | "normal"
            "message":           str,
        }
    """
    if not bucket:
        return {
            "in_boundary_zone": False,
            "distance_to_limit": 999.0,
            "limit_f": 0.0,
            "direction": "unknown",
            "edge_quality": "normal",
            "message": "Bucket no disponible",
        }

    bucket_type = bucket.get("type", "unknown")
    low  = bucket.get("low",  -999.0)
    high = bucket.get("high",  999.0)

    # Calcular distancia al limite relevante
    if bucket_type == "above":
        limit = low
        distance = abs(forecast_high_f - limit)
        direction = "above_limit" if forecast_high_f >= limit else "below_limit"
    elif bucket_type == "below":
        limit = high
        distance = abs(forecast_high_f - limit)
        direction = "above_limit" if forecast_high_f >= limit else "below_limit"
    elif bucket_type == "range":
        # Limite mas cercano
        dist_low  = abs(forecast_high_f - low)
        dist_high = abs(forecast_high_f - high)
        if dist_low <= dist_high:
            limit    = low
            distance = dist_low
            direction = "above_limit" if forecast_high_f >= low else "below_limit"
        else:
            limit    = high
            distance = dist_high
            direction = "above_limit" if forecast_high_f >= high else "below_limit"
    else:
        return {
            "in_boundary_zone": False,
            "distance_to_limit": 999.0,
            "limit_f": 0.0,
            "direction": "unknown",
            "edge_quality": "normal",
            "message": "Tipo de bucket desconocido",
        }

    in_zone = distance <= BOUNDARY_ZONE_F

    # Calidad del edge segun distancia y std_dev
    if distance < 0.5:
        edge_quality = "maximum"   # Justo en el limite → maximo edge
    elif distance < BOUNDARY_ZONE_F:
        edge_quality = "high"
    else:
        edge_quality = "normal"

    # El std_dev es importante: si el error del forecast es mayor que la distancia
    # al limite, la incertidumbre es real y el edge existe de ambos lados
    std_covers_limit = std_dev >= distance

    if in_zone:
        msg = (
            f"ZONA CRITICA: forecast {forecast_high_f:.1f}°F esta a {distance:.1f}°F "
            f"del limite {limit:.0f}°F ({direction.replace('_', ' ')}). "
            f"{'El error del modelo cubre el limite — edge bilateral real.' if std_covers_limit else 'Edge claro hacia un lado.'}"
        )
    else:
        msg = (
            f"Zona normal: {distance:.1f}°F del limite {limit:.0f}°F — "
            f"forecast {direction.replace('_', ' ')} del umbral con margen."
        )

    return {
        "in_boundary_zone":  in_zone,
        "distance_to_limit": round(distance, 2),
        "limit_f":           limit,
        "direction":         direction,
        "edge_quality":      edge_quality,
        "std_covers_limit":  std_covers_limit,
        "message":           msg,
    }


# ── Resumen para Claude Analyst ───────────────────────────────────────────────

def format_rules_for_analyst(rules: dict, boundary: dict) -> str:
    """
    Formatea el analisis de reglas para incluir en el prompt de Claude.
    """
    lines = ["═══ REGLAS DE RESOLUCION (Lawyer's Edge) ═══"]

    icao = rules.get("icao") or "NO DETECTADO"
    city = rules.get("city") or "?"
    lines.append(f"Estacion ICAO de settlement: {icao} ({city})")
    lines.append(f"Tipo de dato:  {rules.get('data_type', '?')}")
    lines.append(f"Unidad:        {rules.get('unit', '?')}")
    lines.append(f"Parser confianza: {rules.get('confidence', '?').upper()}")

    bucket = rules.get("bucket")
    if bucket:
        btype = bucket["type"]
        if btype == "range":
            lines.append(f"Bucket:        {bucket['low']:.0f}°F – {bucket['high']:.0f}°F")
        elif btype == "above":
            lines.append(f"Bucket:        >{bucket['low']:.0f}°F")
        elif btype == "below":
            lines.append(f"Bucket:        <{bucket['high']:.0f}°F")

    if boundary.get("in_boundary_zone"):
        lines.append(f"*** {boundary['message']} ***")
        lines.append(f"Calidad del edge: {boundary['edge_quality'].upper()}")
    else:
        lines.append(f"Posicion vs limite: {boundary['message']}")

    for w in rules.get("warnings", []):
        lines.append(f"ADVERTENCIA: {w}")

    return "\n".join(lines)
