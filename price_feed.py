"""
Feed de precios en tiempo real para activos crypto.
Usa Binance API pública (sin autenticación).
"""
import math
import httpx
from typing import Optional

BINANCE_BASE = "https://api.binance.com/api/v3"
HEADERS = {"User-Agent": "WeatherbotPolymarket/1.0"}


async def get_btc_price() -> Optional[float]:
    """Precio actual de BTCUSDT desde Binance."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BINANCE_BASE}/ticker/price",
                params={"symbol": "BTCUSDT"},
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                return float(resp.json()["price"])
    except Exception:
        pass

    # Fallback: Coinbase
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.coinbase.com/v2/prices/BTC-USD/spot",
                headers=HEADERS,
                timeout=8,
            )
            if resp.status_code == 200:
                return float(resp.json()["data"]["amount"])
    except Exception:
        pass

    return None


async def get_btc_volatility(interval: str = "15m", candles: int = 96) -> float:
    """
    Calcula la volatilidad log-normal de BTC en el intervalo dado.
    Por defecto: 96 velas de 15 minutos = 24 horas de datos.

    Returns: desviación estándar del log-retorno por vela (como fracción, no %).
    """
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BINANCE_BASE}/klines",
                params={"symbol": "BTCUSDT", "interval": interval, "limit": candles},
                headers=HEADERS,
                timeout=12,
            )
            if resp.status_code != 200:
                return _default_volatility(interval)

            klines = resp.json()
            if len(klines) < 10:
                return _default_volatility(interval)

            # Log-retornos: ln(close_t / close_{t-1})
            closes = [float(k[4]) for k in klines]
            log_returns = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]

            if not log_returns:
                return _default_volatility(interval)

            mean = sum(log_returns) / len(log_returns)
            variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
            return math.sqrt(variance)

    except Exception:
        return _default_volatility(interval)


def _default_volatility(interval: str) -> float:
    """
    Volatilidad por defecto si falla Binance.
    Basada en volatilidad anual de BTC ≈ 70%.
    """
    minutes_per_candle = {
        "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440,
    }
    mins = minutes_per_candle.get(interval, 15)
    annual_vol = 0.70
    minutes_per_year = 365 * 24 * 60
    return annual_vol * math.sqrt(mins / minutes_per_year)
