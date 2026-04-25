import json
import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


_PARAMS_FILE = Path(__file__).parent / "data" / "params.json"


class BotParams:
    """Parametros ajustables en runtime (desde la UI). Se persisten en data/params.json."""

    def __init__(self):
        # --- Posicion ---
        self.max_position_usdc: float = float(os.getenv("MAX_POSITION_USDC", 5.0))
        self.min_position_usdc: float = float(os.getenv("MIN_POSITION_USDC", 1.0))
        self.kelly_fraction: float = float(os.getenv("KELLY_FRACTION", 0.25))
        # --- Edge ---
        # 7% es el umbral óptimo según análisis de 400M trades (5%=61% hit rate, 7%=74%)
        self.min_ev_threshold: float = float(os.getenv("MIN_EV_THRESHOLD", 0.07))
        # --- Riesgo ---
        self.max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", 0.20))
        # 72-hour rule: mercados <72h tienen mayor capital velocity (annualized return)
        self.max_hours_to_resolution: int = int(os.getenv("MAX_HOURS_TO_RESOLUTION", 72))
        # --- Calidad de mercado ---
        self.min_liquidity_usdc: float = float(os.getenv("MIN_LIQUIDITY_USDC", 50.0))
        self.max_spread_pct: float = float(os.getenv("MAX_SPREAD_PCT", 0.60))
        self.min_volume_24h_usdc: float = float(os.getenv("MIN_VOLUME_24H_USDC", 5.0))
        # $200 depth mínimo: sin profundidad suficiente no se puede salir (scanner article)
        self.min_book_depth_usdc: float = float(os.getenv("MIN_BOOK_DEPTH_USDC", 200.0))
        self.min_competitive_score: float = float(os.getenv("MIN_COMPETITIVE_SCORE", 0.0))
        # --- Operacion ---
        self.scan_interval_minutes: int = int(os.getenv("SCAN_INTERVAL_MINUTES", 30))
        # --- Tipos de trade habilitados ---
        self.weather_enabled: bool = os.getenv("WEATHER_ENABLED", "true").lower() == "true"
        # --- Bitcoin ---
        self.btc_enabled: bool = os.getenv("BTC_ENABLED", "true").lower() == "true"
        self.btc_max_position_usdc: float = float(os.getenv("BTC_MAX_POSITION_USDC", 3.0))
        self.btc_max_hours_to_resolution: float = float(os.getenv("BTC_MAX_HOURS_TO_RESOLUTION", 96.0))
        self.btc_vol_candles: int = int(os.getenv("BTC_VOL_CANDLES", 96))
        self.btc_momentum_weight: float = float(os.getenv("BTC_MOMENTUM_WEIGHT", 0.03))
        # --- BTC Up/Down (5m y 15m) ---
        self.updown_enabled: bool = os.getenv("UPDOWN_ENABLED", "true").lower() == "true"  # kill switch global
        self.updown_5m_enabled: bool = os.getenv("UPDOWN_5M_ENABLED", "true").lower() == "true"
        self.updown_15m_enabled: bool = os.getenv("UPDOWN_15M_ENABLED", "true").lower() == "true"
        self.updown_1d_enabled: bool = os.getenv("UPDOWN_1D_ENABLED", "false").lower() == "true"  # mercado diario BTC
        self.updown_max_usdc: float = float(os.getenv("UPDOWN_MAX_USDC", 1.0))
        self.updown_max_consecutive_losses: int = int(os.getenv("UPDOWN_MAX_CONSECUTIVE_LOSSES", 5))
        # Umbrales configurables por el asesor (0 = usar el valor adaptativo del learner)
        self.updown_15m_min_confidence: float = 0.20   # confianza mínima para entrar en 15m
        self.updown_5m_min_confidence:  float = 0.20   # confianza mínima para entrar en 5m
        self.updown_15m_momentum_gate:  float = 0.20   # umbral momentum gate para 15m
        self.updown_5m_momentum_gate:   float = 0.20   # umbral momentum gate para 5m
        # Umbrales de desplazamiento para 5m (% de movimiento BTC en la ventana)
        # >= hi → seguir tendencia | [lo, hi) → neutro | < lo → mean-reversion
        self.updown_displacement_hi_pct: float = 0.20  # >0.20% → follow trend
        self.updown_displacement_lo_pct: float = 0.10  # >0.10% → neutral
        # --- Telonex (Fase 11) ---
        self.telonex_enabled: bool = os.getenv("TELONEX_ENABLED", "true").lower() == "true"
        self.telonex_smart_wallet_weight: float = float(os.getenv("TELONEX_SMART_WALLET_WEIGHT", 0.10))
        self.telonex_real_ofi_weight: float = float(os.getenv("TELONEX_REAL_OFI_WEIGHT", 0.12))
        # --- Asignación de capital ---
        self.alloc_weather_pct: float = 0.60
        self.alloc_btc_pct: float = 0.20
        self.alloc_updown_pct: float = 0.20
        # --- Meta de ganancia ---
        self.profit_goal_usdc: float = 0.0        # 0 = sin meta activa
        self.profit_goal_hours: float = 24.0
        self.profit_goal_start_iso: str = ""       # ISO timestamp al activar meta
        self.profit_goal_start_value: float = 0.0  # valor total de cuenta al activar meta
        # --- Sistema de buckets de capital (Fase 12) ---
        # betting_pool_usdc > 0 activa el sistema de buckets (0 = sistema legacy por %)
        self.betting_pool_usdc: float = 0.0
        # Porcentajes de referencia para recarga (suman ≤ 1.0)
        self.bucket_weather_pct: float = 0.20
        self.bucket_btc_pct: float = 0.20
        self.bucket_updown_5m_pct: float = 0.15
        self.bucket_updown_15m_pct: float = 0.45
        # Saldo actual de cada bucket (decrece al apostar, sube al ganar)
        self.bucket_weather_usdc: float = 0.0
        self.bucket_btc_usdc: float = 0.0
        self.bucket_updown_5m_usdc: float = 0.0
        self.bucket_updown_15m_usdc: float = 0.0
        # --- Circuit breaker (Risk Manager) ---
        # False = desactivado (útil en desarrollo); True = activo, para cuando no estés presente
        self.circuit_breaker_enabled: bool = False
        # --- Sistema de capital Phantom (Fase 10) ---
        # Cuando phantom_real_enabled=True el bot puede usar dinero real en trades phantom
        self.phantom_real_enabled: bool = False
        self.phantom_cash_libre_usdc: float = 0.0    # reserva libre phantom (total_asignado - pool; crece con ganancias)
        self.phantom_pool_usdc: float = 0.0          # pool activo de apuestas (se reduce al perder)
        self.phantom_bucket_5m_pct: float = 0.30     # % del pool para trades 5m
        self.phantom_bucket_15m_pct: float = 0.70    # % del pool para trades 15m
        self.phantom_bucket_5m_usdc: float = 0.0     # saldo actual bucket phantom 5m
        self.phantom_bucket_15m_usdc: float = 0.0    # saldo actual bucket phantom 15m
        # Toggles por intervalo — registrar/operar phantom independiente del bot real (punto 2)
        self.phantom_5m_enabled: bool = True
        self.phantom_15m_enabled: bool = True
        self.phantom_1d_enabled: bool = False
        # Zona muerta de confianza — saltar señales con conf en [min, max]
        # (en sample 54 trades, tier "low_moderate" 20-34% conf perdió 67% y -$12.24 PnL)
        self.phantom_deadzone_enabled:  bool  = True
        self.phantom_deadzone_min_conf: float = 20.0
        self.phantom_deadzone_max_conf: float = 34.0
        # Stake dinámico UpDown por nivel de confianza (item 29)
        # Stake = min_stake + (max_stake - min_stake) * (conf - conf_min) / (conf_max - conf_min)
        # clipped to [min_stake, max_stake]
        self.updown_stake_min_usdc: float = 3.0      # stake mínimo (en conf ≤ conf_min_pct)
        self.updown_stake_max_usdc: float = 15.0     # stake máximo (en conf ≥ conf_max_pct)
        self.updown_stake_conf_min_pct: float = 20.0 # % confianza que mapea al stake mínimo
        self.updown_stake_conf_max_pct: float = 65.0 # % confianza que mapea al stake máximo
        # --- Trading Mode (v9.4) — compra barato, vende target ---
        self.trading_mode_enabled: bool = True       # usar trading mode en vez de prediction
        self.trading_real_enabled: bool = False      # False = solo phantom; True = phantom + real
        self.trading_entry_threshold: float = 0.55   # comprar si token <= este precio
        self.trading_min_entry_price: float = 0.10   # floor: no comprar si token < este (mercado muerto)
        self.trading_max_entry_price: float = 0.30   # ceiling (punto 10): solo comprar barato → R:R favorable
        self.trading_trend_prefer_winning: bool = True  # preferir lado trending vs cheapest
        self.trading_profit_offset: float = 0.30     # vender en entry + offset (punto 10: 0.12→0.30 para R:R≥2:1)
        self.trading_exit_deadline_min: float = 1.0  # forzar salida a T-X min del cierre
        self.trading_min_entry_minutes_left: float = 1.0  # entrar desde el inicio
        self.trading_max_entries_per_market: int = 8 # con one-open-at-a-time, permite muchos ciclos
        self.trading_max_open_per_side: int = 1      # con one-open, solo 1 a la vez
        self.trading_stake_usdc: float = 5.0         # USDC por entrada
        self.trading_one_open_at_a_time: bool = True # vender antes de volver a comprar
        # --- Safety caps para trading real (v9.4.4) ---
        self.trading_real_max_exposure_usdc: float = 20.0   # exposure máximo vivo en real (suma stakes OPEN)
        self.trading_real_daily_loss_limit_usdc: float = 5.0  # stop diario (|pnl negativo| día hoy)
        self.trading_real_max_consec_losses: int = 3        # kill-switch tras N pérdidas seguidas
        self.trading_real_killed: bool = False              # flag activado por kill-switch; reset manual
        # --- Modo "comprar el más probable" (punto 14) ---
        self.trading_buy_probable: bool = True
        self.trading_probable_min_price: float = 0.55
        self.trading_probable_max_price: float = 0.85
        self.trading_probable_profit_offset: float = 0.08
        # --- Stop-loss escalonado (punto 12, enfoque A) ---
        self.trading_sl_enabled: bool = True
        self.trading_sl_trigger_drop: float = 0.50           # caída 50% vs entry arma SL
        self.trading_sl_wait_min: float = 3.0                # esperar N min tras trigger
        self.trading_sl_min_recover_factor: float = 0.50     # vender si bid >= entry/2
        self.trading_panic_trigger_drop: float = 0.80        # caída 80% → panic
        self.trading_panic_min_recover_factor: float = 0.33  # vender si bid >= entry/3
        # --- Punto 19A — drawdown kill switch (-40% desde ATH de cumulative real PnL) ---
        self.trading_real_drawdown_halt_pct: float = 0.40
        # --- Punto 19B — paper-to-live gate ---
        self.trading_paper_required_days: float = 7.0
        self.trading_paper_required_trades: int = 200
        self.trading_paper_required_wr: float = 0.75
        self.trading_paper_gate_override: bool = False
        # --- Punto 19C — stale price check ---
        self.trading_max_price_age_sec: float = 10.0
        # Cargar valores guardados previamente (sobreescriben los defaults)
        self._load()

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
            "weather_enabled": self.weather_enabled,
            "btc_enabled": self.btc_enabled,
            "btc_max_position_usdc": self.btc_max_position_usdc,
            "btc_max_hours_to_resolution": self.btc_max_hours_to_resolution,
            "btc_vol_candles": self.btc_vol_candles,
            "btc_momentum_weight": self.btc_momentum_weight,
            "updown_enabled": self.updown_enabled,
            "updown_5m_enabled": self.updown_5m_enabled,
            "updown_15m_enabled": self.updown_15m_enabled,
            "updown_1d_enabled": self.updown_1d_enabled,
            "updown_max_usdc": self.updown_max_usdc,
            "updown_max_consecutive_losses": self.updown_max_consecutive_losses,
            "updown_15m_min_confidence": self.updown_15m_min_confidence,
            "updown_5m_min_confidence":  self.updown_5m_min_confidence,
            "updown_15m_momentum_gate":  self.updown_15m_momentum_gate,
            "updown_5m_momentum_gate":   self.updown_5m_momentum_gate,
            "updown_displacement_hi_pct": self.updown_displacement_hi_pct,
            "updown_displacement_lo_pct": self.updown_displacement_lo_pct,
            "telonex_enabled":               self.telonex_enabled,
            "telonex_smart_wallet_weight":   self.telonex_smart_wallet_weight,
            "telonex_real_ofi_weight":       self.telonex_real_ofi_weight,
            "alloc_weather_pct": self.alloc_weather_pct,
            "alloc_btc_pct": self.alloc_btc_pct,
            "alloc_updown_pct": self.alloc_updown_pct,
            "profit_goal_usdc": self.profit_goal_usdc,
            "profit_goal_hours": self.profit_goal_hours,
            "profit_goal_start_iso": self.profit_goal_start_iso,
            "profit_goal_start_value": self.profit_goal_start_value,
            "betting_pool_usdc": self.betting_pool_usdc,
            "bucket_weather_pct": self.bucket_weather_pct,
            "bucket_btc_pct": self.bucket_btc_pct,
            "bucket_updown_5m_pct": self.bucket_updown_5m_pct,
            "bucket_updown_15m_pct": self.bucket_updown_15m_pct,
            "bucket_weather_usdc": self.bucket_weather_usdc,
            "bucket_btc_usdc": self.bucket_btc_usdc,
            "bucket_updown_5m_usdc": self.bucket_updown_5m_usdc,
            "bucket_updown_15m_usdc": self.bucket_updown_15m_usdc,
            "circuit_breaker_enabled": self.circuit_breaker_enabled,
            "phantom_real_enabled": self.phantom_real_enabled,
            "phantom_cash_libre_usdc": self.phantom_cash_libre_usdc,
            "phantom_pool_usdc": self.phantom_pool_usdc,
            "phantom_bucket_5m_pct": self.phantom_bucket_5m_pct,
            "phantom_bucket_15m_pct": self.phantom_bucket_15m_pct,
            "phantom_bucket_5m_usdc": self.phantom_bucket_5m_usdc,
            "phantom_bucket_15m_usdc": self.phantom_bucket_15m_usdc,
            "phantom_5m_enabled":  self.phantom_5m_enabled,
            "phantom_15m_enabled": self.phantom_15m_enabled,
            "phantom_1d_enabled":  self.phantom_1d_enabled,
            "phantom_deadzone_enabled":  self.phantom_deadzone_enabled,
            "phantom_deadzone_min_conf": self.phantom_deadzone_min_conf,
            "phantom_deadzone_max_conf": self.phantom_deadzone_max_conf,
            "updown_stake_min_usdc":      self.updown_stake_min_usdc,
            "updown_stake_max_usdc":      self.updown_stake_max_usdc,
            "updown_stake_conf_min_pct":  self.updown_stake_conf_min_pct,
            "updown_stake_conf_max_pct":  self.updown_stake_conf_max_pct,
            "trading_mode_enabled":            self.trading_mode_enabled,
            "trading_real_enabled":            self.trading_real_enabled,
            "trading_entry_threshold":         self.trading_entry_threshold,
            "trading_min_entry_price":         self.trading_min_entry_price,
            "trading_max_entry_price":         self.trading_max_entry_price,
            "trading_trend_prefer_winning":    self.trading_trend_prefer_winning,
            "trading_profit_offset":           self.trading_profit_offset,
            "trading_exit_deadline_min":       self.trading_exit_deadline_min,
            "trading_min_entry_minutes_left":  self.trading_min_entry_minutes_left,
            "trading_max_entries_per_market":  self.trading_max_entries_per_market,
            "trading_max_open_per_side":       self.trading_max_open_per_side,
            "trading_stake_usdc":              self.trading_stake_usdc,
            "trading_one_open_at_a_time":      self.trading_one_open_at_a_time,
            "trading_real_max_exposure_usdc":      self.trading_real_max_exposure_usdc,
            "trading_real_daily_loss_limit_usdc":  self.trading_real_daily_loss_limit_usdc,
            "trading_real_max_consec_losses":      self.trading_real_max_consec_losses,
            "trading_real_killed":                 self.trading_real_killed,
            "trading_sl_enabled":                  self.trading_sl_enabled,
            "trading_sl_trigger_drop":             self.trading_sl_trigger_drop,
            "trading_sl_wait_min":                 self.trading_sl_wait_min,
            "trading_sl_min_recover_factor":       self.trading_sl_min_recover_factor,
            "trading_panic_trigger_drop":          self.trading_panic_trigger_drop,
            "trading_panic_min_recover_factor":    self.trading_panic_min_recover_factor,
            "trading_buy_probable":                self.trading_buy_probable,
            "trading_probable_min_price":          self.trading_probable_min_price,
            "trading_probable_max_price":          self.trading_probable_max_price,
            "trading_probable_profit_offset":      self.trading_probable_profit_offset,
            "trading_real_drawdown_halt_pct":      self.trading_real_drawdown_halt_pct,
            "trading_paper_required_days":         self.trading_paper_required_days,
            "trading_paper_required_trades":       self.trading_paper_required_trades,
            "trading_paper_required_wr":           self.trading_paper_required_wr,
            "trading_paper_gate_override":         self.trading_paper_gate_override,
            "trading_max_price_age_sec":           self.trading_max_price_age_sec,
        }

    def _load(self):
        """Carga params guardados desde data/params.json (si existe)."""
        try:
            if _PARAMS_FILE.exists():
                saved = json.loads(_PARAMS_FILE.read_text(encoding="utf-8"))
                for key, value in saved.items():
                    if not hasattr(self, key):
                        continue
                    current = getattr(self, key)
                    if isinstance(current, bool):
                        # JSON guarda bool como bool; str como "true"/"false" no aplica aquí
                        setattr(self, key, bool(value))
                    else:
                        setattr(self, key, type(current)(value))
        except Exception:
            pass  # Si el archivo está corrupto, usar defaults

    def save(self):
        """Persiste los parámetros actuales en data/params.json."""
        try:
            _PARAMS_FILE.parent.mkdir(exist_ok=True)
            _PARAMS_FILE.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # Campos que deben ser fracciones [0.0, 1.0].
    # Si el asesor envía un valor > 1 (ej. 38 en vez de 0.38) se divide entre 100.
    _FRACTION_FIELDS = {
        "updown_15m_min_confidence", "updown_5m_min_confidence",
        "updown_15m_momentum_gate",  "updown_5m_momentum_gate",
        "updown_displacement_hi_pct", "updown_displacement_lo_pct",
        "kelly_fraction", "min_ev_threshold", "max_daily_loss_pct",
        "max_spread_pct",
    }

    def update(self, data: dict):
        for key, value in data.items():
            if not hasattr(self, key):
                continue
            typed = type(getattr(self, key))(value)
            # Auto-corregir fracciones enviadas como porcentaje entero
            if key in self._FRACTION_FIELDS and isinstance(typed, float) and typed > 1.0:
                typed = round(typed / 100.0, 6)
            setattr(self, key, typed)
        self.save()  # Persistir tras cada cambio


class Settings(BaseSettings):
    poly_private_key: str = Field(default="", env="POLY_PRIVATE_KEY")
    poly_signature_type: int = Field(default=0, env="POLY_SIGNATURE_TYPE")
    poly_wallet_address: str = Field(default="", env="POLY_WALLET_ADDRESS")
    cmc_api_key: str = Field(default="", env="CMC_API_KEY")
    # Telonex API (Fase 11): on-chain fills, smart wallet tracking
    # Registro gratuito en https://telonex.io
    telonex_api_key: str = Field(default="", env="TELONEX_API_KEY")

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
