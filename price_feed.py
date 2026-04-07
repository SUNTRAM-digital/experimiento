"""
Feed de precios y análisis técnico en tiempo real para BTC.
Fuentes:
  - Precio: Binance (primario) → Coinbase (fallback) → CoinMarketCap (fallback)
  - Volatilidad: Binance klines (log-retornos históricos)
  - TA: TradingView vía tradingview-ta (RSI, MACD, EMA, recomendación)
  - Market data: CoinMarketCap API (cambios %, volumen, market cap)
"""
import asyncio
import math
import logging
import httpx
from typing import Optional

logger = logging.getLogger("weatherbot")

BINANCE_BASE = "https://api.binance.com/api/v3"
CMC_BASE     = "https://pro-api.coinmarketcap.com/v1"
HEADERS      = {"User-Agent": "WeatherbotPolymarket/1.0"}

# Intentar importar tradingview-ta (opcional)
try:
    from tradingview_ta import TA_Handler, Interval as TVInterval
    _TV_AVAILABLE = True
except ImportError:
    _TV_AVAILABLE = False
    logger.warning("tradingview-ta no instalado — instala con: pip install tradingview-ta")

_TV_INTERVAL_MAP = {}
if _TV_AVAILABLE:
    _TV_INTERVAL_MAP = {
        "1m":  TVInterval.INTERVAL_1_MINUTE,
        "5m":  TVInterval.INTERVAL_5_MINUTES,
        "15m": TVInterval.INTERVAL_15_MINUTES,
        "1h":  TVInterval.INTERVAL_1_HOUR,
        "4h":  TVInterval.INTERVAL_4_HOURS,
    }

_REC_SIGNAL = {
    "STRONG_BUY": 1.0,
    "BUY":        0.5,
    "NEUTRAL":    0.0,
    "SELL":      -0.5,
    "STRONG_SELL":-1.0,
}


# ── Precio BTC ────────────────────────────────────────────────────────────────

async def get_btc_price_chainlink() -> Optional[float]:
    """
    Precio BTC desde el agregador on-chain de Chainlink (Ethereum mainnet).
    Feed BTC/USD: 0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88

    Este es el precio más cercano al que Polymarket usa para resolver los
    mercados UpDown (que usan el Chainlink BTC/USD Data Stream).
    La diferencia con el spot es normalmente < 0.3%, pero en ventanas de 5–15m
    esa diferencia puede determinar si resuelve UP o DOWN.
    """
    CHAINLINK_BTC_USD = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88"
    CALL_DATA = "0xfeaf968c"  # selector de latestRoundData()
    RPC_ENDPOINTS = [
        "https://cloudflare-eth.com",
        "https://ethereum.publicnode.com",
        "https://rpc.ankr.com/eth",
    ]
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": CHAINLINK_BTC_USD, "data": CALL_DATA}, "latest"],
        "id": 1,
    }
    async with httpx.AsyncClient() as client:
        for rpc in RPC_ENDPOINTS:
            try:
                resp = await client.post(rpc, json=payload, timeout=8)
                if resp.status_code != 200:
                    continue
                result = resp.json().get("result", "")
                if not result or result == "0x":
                    continue
                # ABI decode: (roundId[32], answer[32], startedAt[32], updatedAt[32], answeredInRound[32])
                raw = result[2:]
                if len(raw) < 128:
                    continue
                answer_hex = raw[64:128]
                answer = int(answer_hex, 16)
                if answer > 2**255:
                    answer -= 2**256  # int256 signed
                price = answer / 1e8  # Chainlink usa 8 decimales
                if 1_000 < price < 1_000_000:
                    logger.debug(f"Chainlink BTC/USD: ${price:,.2f} (via {rpc})")
                    return round(price, 2)
            except Exception as e:
                logger.debug(f"Chainlink RPC {rpc} error: {e}")
                continue
    return None


async def get_btc_price() -> Optional[float]:
    """
    Precio actual de BTC. Consulta Chainlink (fuente de resolución de Polymarket),
    Binance, Kraken y Coinbase en paralelo, usa la mediana de fuentes válidas.
    """
    async def _chainlink():
        return await get_btc_price_chainlink()

    async def _binance(client):
        resp = await client.get(
            f"{BINANCE_BASE}/ticker/price",
            params={"symbol": "BTCUSDT"}, headers=HEADERS, timeout=8,
        )
        return float(resp.json()["price"]) if resp.status_code == 200 else None

    async def _kraken(client):
        resp = await client.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": "XBTUSD"}, headers=HEADERS, timeout=8,
        )
        if resp.status_code != 200:
            return None
        d = resp.json()
        return float(d["result"]["XXBTZUSD"]["c"][0])

    async def _coinbase(client):
        resp = await client.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot",
            headers=HEADERS, timeout=8,
        )
        if resp.status_code != 200:
            return None
        return float(resp.json()["data"]["amount"])

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            _chainlink(),
            _binance(client),
            _kraken(client),
            _coinbase(client),
            return_exceptions=True,
        )

    prices = sorted([
        p for p in results
        if isinstance(p, float) and 1_000 < p < 1_000_000
    ])
    if not prices:
        return None
    if len(prices) == 1:
        return prices[0]

    median = prices[len(prices) // 2]
    valid  = [p for p in prices if abs(p - median) / median < 0.005]
    final  = sum(valid) / len(valid) if valid else median
    logger.debug(f"BTC precio multi-fuente: ${final:,.2f} (muestras: {prices})")
    return round(final, 2)


# ── Volatilidad ───────────────────────────────────────────────────────────────

async def get_btc_volatility(interval: str = "15m", candles: int = 96) -> float:
    """
    Volatilidad log-normal de BTC por vela del intervalo dado.
    Para mercados de 5 min usa interval="1m"; para 15-30 min usa "5m".
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

            closes = [float(k[4]) for k in klines]
            log_returns = [
                math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes))
                if closes[i - 1] > 0
            ]
            if not log_returns:
                return _default_volatility(interval)

            mean     = sum(log_returns) / len(log_returns)
            variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
            return math.sqrt(variance)

    except Exception:
        return _default_volatility(interval)


def _default_volatility(interval: str) -> float:
    """Volatilidad de fallback basada en vol anual BTC ≈ 70%."""
    minutes_per_candle = {
        "1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440,
    }
    mins = minutes_per_candle.get(interval, 15)
    annual_vol = 0.70
    minutes_per_year = 365 * 24 * 60
    return annual_vol * math.sqrt(mins / minutes_per_year)


def vol_interval_for_horizon(hours_to_resolution: float) -> tuple[str, int]:
    """
    Elige el intervalo de velas y cantidad óptima para la volatilidad
    según el horizonte temporal del mercado.

    Returns: (interval_str, candle_count)
    """
    if hours_to_resolution <= 0.25:    # ≤ 15 min → velas de 1m, 2h de historia
        return "1m", 120
    elif hours_to_resolution <= 1.0:   # ≤ 1h → velas de 5m, 4h de historia
        return "5m", 48
    elif hours_to_resolution <= 12.0:  # ≤ 12h → velas de 15m, 24h de historia
        return "15m", 96
    elif hours_to_resolution <= 48.0:  # ≤ 48h → velas de 1h, 7 días de historia
        return "1h", 168
    else:                              # > 48h → velas de 4h, 30 días de historia
        return "4h", 180


def tv_interval_for_horizon(hours_to_resolution: float) -> str:
    """Intervalo de TradingView TA más relevante para el horizonte."""
    if hours_to_resolution <= 0.25:
        return "1m"
    elif hours_to_resolution <= 1.0:
        return "5m"
    elif hours_to_resolution <= 12.0:
        return "15m"
    else:
        return "1h"


# ── TradingView TA ────────────────────────────────────────────────────────────

# Cache: interval → (result_dict, fetched_at_timestamp)
_ta_cache: dict[str, tuple[dict, float]] = {}
_TA_CACHE_TTL = 90  # segundos — no re-fetchar si el dato tiene menos de 90s


def _get_btc_ta_sync(interval: str = "1m") -> dict:
    """
    Obtiene análisis técnico de TradingView (síncrono).
    Usa caché de 90s para no exceder el rate-limit de TradingView.
    """
    import time
    cached, fetched_at = _ta_cache.get(interval, ({}, 0.0))
    if cached and (time.time() - fetched_at) < _TA_CACHE_TTL:
        return cached

    if not _TV_AVAILABLE:
        return {"recommendation": "NEUTRAL", "signal": 0.0, "available": False,
                "error": "tradingview-ta no instalado"}
    try:
        tv_interval = _TV_INTERVAL_MAP.get(interval, TVInterval.INTERVAL_1_MINUTE)
        handler = TA_Handler(
            symbol="BTCUSDT",
            screener="crypto",
            exchange="BINANCE",
            interval=tv_interval,
        )
        analysis = handler.get_analysis()
        rec  = analysis.summary.get("RECOMMENDATION", "NEUTRAL")
        buy  = analysis.summary.get("BUY", 0)
        neutral = analysis.summary.get("NEUTRAL", 0)
        sell = analysis.summary.get("SELL", 0)
        total = buy + neutral + sell
        # Señal continua de -1 a +1 basada en conteo de indicadores
        continuous_signal = (buy - sell) / total if total > 0 else 0.0
        result = {
            "recommendation": rec,
            "signal":   continuous_signal,
            "signal_discrete": _REC_SIGNAL.get(rec, 0.0),
            "buy":      buy,
            "neutral":  neutral,
            "sell":     sell,
            "rsi":      analysis.indicators.get("RSI"),
            "ema20":    analysis.indicators.get("EMA20"),
            "ema50":    analysis.indicators.get("EMA50"),
            "macd":     analysis.indicators.get("MACD.macd"),
            "close":    analysis.indicators.get("close"),
            "interval": interval,
            "available": True,
        }
        _ta_cache[interval] = (result, time.time())
        return result
    except Exception as e:
        logger.warning(f"TradingView TA error ({interval}): {e}")
        err = {"recommendation": "NEUTRAL", "signal": 0.0, "available": False, "error": str(e)}
        # En caso de 429, backoff más largo para no seguir spameando
        if "429" in str(e):
            _ta_cache[interval] = (err, time.time())  # no reintentar por otros 90s
        return err


async def get_btc_ta(interval: str = "1m") -> dict:
    """Análisis técnico TradingView de forma asíncrona."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_btc_ta_sync, interval)


# ── CoinMarketCap ─────────────────────────────────────────────────────────────

async def get_btc_market_data_cmc(api_key: str) -> dict:
    """
    Datos de mercado de BTC desde CoinMarketCap API (requiere API key).
    Retorna: precio, volumen 24h, cambios % a 1h/24h/7d, market cap.
    API key gratuita en: https://coinmarketcap.com/api/
    """
    if not api_key:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{CMC_BASE}/cryptocurrency/quotes/latest",
                headers={
                    "X-CMC_PRO_API_KEY": api_key,
                    "Accept": "application/json",
                },
                params={"symbol": "BTC", "convert": "USD"},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"CoinMarketCap HTTP {resp.status_code}")
                return {}
            data = resp.json()
            q = data["data"]["BTC"]["quote"]["USD"]
            return {
                "price":              round(float(q["price"]), 2),
                "volume_24h":         float(q.get("volume_24h", 0)),
                "percent_change_1h":  round(float(q.get("percent_change_1h", 0)), 3),
                "percent_change_24h": round(float(q.get("percent_change_24h", 0)), 3),
                "percent_change_7d":  round(float(q.get("percent_change_7d", 0)), 3),
                "market_cap":         float(q.get("market_cap", 0)),
                "available": True,
            }
    except Exception as e:
        logger.warning(f"CoinMarketCap error: {e}")
        return {}
