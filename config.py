import json
import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class BotParams:
    """Parametros ajustables en runtime (desde la UI)."""

    def __init__(self):
        # --- Posicion ---
        self.max_position_usdc: float = float(os.getenv("MAX_POSITION_USDC", 5.0))
        self.min_position_usdc: float = float(os.getenv("MIN_POSITION_USDC", 1.0))
        self.kelly_fraction: float = float(os.getenv("KELLY_FRACTION", 0.25))
        # --- Edge ---
        self.min_ev_threshold: float = float(os.getenv("MIN_EV_THRESHOLD", 0.10))
        # --- Riesgo ---
        self.max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", 0.20))
        self.max_hours_to_resolution: int = int(os.getenv("MAX_HOURS_TO_RESOLUTION", 48))
        # --- Calidad de mercado ---
        self.min_liquidity_usdc: float = float(os.getenv("MIN_LIQUIDITY_USDC", 50.0))
        self.max_spread_pct: float = float(os.getenv("MAX_SPREAD_PCT", 0.60))      # spread max bid-ask (60%)
        self.min_volume_24h_usdc: float = float(os.getenv("MIN_VOLUME_24H_USDC", 5.0))  # volumen minimo 24h
        self.min_book_depth_usdc: float = float(os.getenv("MIN_BOOK_DEPTH_USDC", 5.0))  # profundidad minima en el libro
        self.min_competitive_score: float = float(os.getenv("MIN_COMPETITIVE_SCORE", 0.0))  # 0=off, 0.5=moderado
        # --- Operacion ---
        self.scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", 30))

    def to_dict(self) -> dict:
        return {
            "max_position_usdc": self.max_position_usdc,
            "min_position_usdc": self.min_position_usdc,
            "kelly_fraction": self.kelly_fraction,
            "min_ev_threshold": self.min_ev_threshold,
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "max_hours_to_resolution": self.max_hours_to_resolution,
            "min_liquidity_usdc": self.min_liquidity_usdc,
            "max_spread_pct": self.max_spread_pct,
            "min_volume_24h_usdc": self.min_volume_24h_usdc,
            "min_book_depth_usdc": self.min_book_depth_usdc,
            "min_competitive_score": self.min_competitive_score,
            "scan_interval_minutes": self.scan_interval_minutes,
        }

    def update(self, data: dict):
        for key, value in data.items():
            if hasattr(self, key):
                setattr(self, key, type(getattr(self, key))(value))


class Settings(BaseSettings):
    poly_private_key: str = Field(default="", env="POLY_PRIVATE_KEY")
    poly_signature_type: int = Field(default=0, env="POLY_SIGNATURE_TYPE")
    poly_wallet_address: str = Field(default="", env="POLY_WALLET_ADDRESS")

    class Config:
        env_file = ".env"
        extra = "ignore"


# Instancias globales
settings = Settings()
bot_params = BotParams()

# ICAO stations para cada ciudad US (usadas por Polymarket)
CITY_STATIONS = {
    "new york": "KLGA",
    "nyc": "KLGA",
    "chicago": "KORD",
    "dallas": "KDAL",
    "austin": "KAUS",
    "atlanta": "KATL",
    "miami": "KMIA",
    "seattle": "KSEA",
    "los angeles": "KLAX",
    "denver": "KDEN",
    "boston": "KBOS",
    "houston": "KHOU",
    "phoenix": "KPHX",
    "minneapolis": "KMSP",
    "detroit": "KDTW",
    "las vegas": "KLAS",
    "portland": "KPDX",
    "san francisco": "KSFO",
    "washington": "KDCA",
    "dc": "KDCA",
}

# Coordenadas de cada estacion ICAO para weather.gov
STATION_COORDS = {
    "KLGA": (40.7769, -73.8740),
    "KORD": (41.9742, -87.9073),
    "KDAL": (32.8471, -96.8518),
    "KAUS": (30.1945, -97.6699),
    "KATL": (33.6407, -84.4277),
    "KMIA": (25.7959, -80.2870),
    "KSEA": (47.4502, -122.3088),
    "KLAX": (33.9425, -118.4081),
    "KDEN": (39.8561, -104.6737),
    "KBOS": (42.3656, -71.0096),
    "KHOU": (29.6454, -95.2789),
    "KPHX": (33.4373, -112.0078),
    "KMSP": (44.8848, -93.2223),
    "KDTW": (42.2124, -83.3534),
    "KLAS": (36.0840, -115.1537),
    "KPDX": (45.5898, -122.5951),
    "KSFO": (37.6213, -122.3790),
    "KDCA": (38.8521, -77.0377),
}
