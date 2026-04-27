"""
Feed de precios y análisis técnico en tiempo real para BTC.
Fuentes:
  - Precio: Binance (primario) → Coinbase (fallback) → CoinMarketCap (fallback)
  - Volatilidad: Binance klines (log-retornos históricos)
  - TA: TradingView vía tradingview-ta (RSI, MACD, EMA, recomendación)
  - Market data: CoinMarketCap API (cambios %, volumen, market cap)
  - Microestructura: Binance klines 1m — Taker OFI + Volume z-score
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
_TA_CACHE_TTL     = 120   # segundos — no re-fetchar si el dato tiene menos de 2min
_TA_CACHE_TTL_429 = 600   # 10min de backoff después de un 429


def _get_btc_ta_sync(interval: str = "1m") -> dict:
    """
    Obtiene análisis técnico de TradingView (síncrono).
    Usa caché de 90s para no exceder el rate-limit de TradingView.
    """
    import time
    cached, fetched_at = _ta_cache.get(interval, ({}, 0.0))
    ttl = _TA_CACHE_TTL_429 if (cached and not cached.get("available", True)) else _TA_CACHE_TTL
    if cached and (time.time() - fetched_at) < ttl:
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
        ind = analysis.indicators
        result = {
            "recommendation": rec,
            "signal":   continuous_signal,
            "signal_discrete": _REC_SIGNAL.get(rec, 0.0),
            "buy":      buy,
            "neutral":  neutral,
            "sell":     sell,
            # ── Osciladores básicos ─────────────────────────────────────────
            "rsi":       ind.get("RSI"),
            "stoch_k":   ind.get("Stoch.K"),
            "stoch_d":   ind.get("Stoch.D"),
            "macd":      ind.get("MACD.macd"),
            "macd_signal": ind.get("MACD.signal"),
            "cci":       ind.get("CCI20"),
            "ao":        ind.get("AO"),       # Awesome Oscillator
            "mom":       ind.get("Mom"),      # Momentum
            # ── Tendencia ───────────────────────────────────────────────────
            "ema9":      ind.get("EMA9"),
            "ema20":     ind.get("EMA20"),
            "ema21":     ind.get("EMA21"),
            "ema50":     ind.get("EMA50"),
            "ema100":    ind.get("EMA100"),
            "ema200":    ind.get("EMA200"),
            "adx":       ind.get("ADX"),
            "adx_pos":   ind.get("ADX+DI"),   # DI+ (comprador)
            "adx_neg":   ind.get("ADX-DI"),   # DI- (vendedor)
            "psar":      ind.get("P.SAR"),     # Parabolic SAR
            # ── Volatilidad / Bandas ─────────────────────────────────────
            "bb_upper":  ind.get("BB.upper"),
            "bb_lower":  ind.get("BB.lower"),
            "bb_basis":  ind.get("BB.basis"),
            "atr":       ind.get("ATR"),
            # ── Precio / Volumen ─────────────────────────────────────────
            "open":      ind.get("open"),
            "high":      ind.get("high"),
            "low":       ind.get("low"),
            "close":     ind.get("close"),
            "volume":    ind.get("volume"),
            "vwma":      ind.get("VWMA"),
            "interval":  interval,
            "available": True,
        }
        _ta_cache[interval] = (result, time.time())
        return result
    except Exception as e:
        logger.warning(f"TradingView TA error ({interval}): {e}")
        err = {"recommendation": "NEUTRAL", "signal": 0.0, "available": False, "error": str(e)}
        if "429" in str(e):
            _ta_cache[interval] = (err, time.time())
        return err


async def get_btc_ta(interval: str = "1m") -> dict:
    """Análisis técnico TradingView de forma asíncrona."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_btc_ta_sync, interval)


async def get_btc_ta_multi(intervals: list[str]) -> dict[str, dict]:
    """
    Obtiene TA de múltiples timeframes secuencialmente con pequeño delay.
    Paralelo causaba 429 al golpear TradingView 3-4 veces simultáneas.
    """
    import asyncio as _aio
    results = []
    for iv in intervals:
        results.append(await get_btc_ta(iv))
        await _aio.sleep(0.4)
    return {iv: r for iv, r in zip(intervals, results)}


# ── Binance Perpetuals: Funding Rate ──────────────────────────────────────────

_FUNDING_CACHE: tuple[dict, float] = ({}, 0.0)
_FUNDING_CACHE_TTL = 120  # el funding rate cambia cada 8h, 2min de caché es suficiente


async def get_btc_funding_rate() -> dict:
    """
    Tasa de financiamiento del perpetuo BTCUSDT en Binance (gratis, sin API key).
    Funding rate positivo (+) → longs pagan shorts → mercado sobre-comprado → señal bajista.
    Funding rate negativo (-) → shorts pagan longs → mercado sobre-vendido → señal alcista.
    Actualiza cada 8h (00:00, 08:00, 16:00 UTC); caché de 2min.
    """
    import time
    global _FUNDING_CACHE
    cached, fetched_at = _FUNDING_CACHE
    if cached and (time.time() - fetched_at) < _FUNDING_CACHE_TTL:
        return cached
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=6) as client:
            resp = await client.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": "BTCUSDT"},
            )
            if resp.status_code != 200:
                return {"available": False}
            data = resp.json()
            rate = float(data.get("lastFundingRate", 0))
            mark = float(data.get("markPrice", 0))
            index = float(data.get("indexPrice", 0))
            premium_pct = round((mark - index) / index * 100, 4) if index > 0 else 0.0
            result = {
                "funding_rate":  round(rate, 6),
                "rate_pct":      round(rate * 100, 4),
                "mark_price":    mark,
                "index_price":   index,
                "premium_pct":   premium_pct,   # mark vs index spread
                "available":     True,
            }
            _FUNDING_CACHE = (result, time.time())
            return result
    except Exception as e:
        logger.debug(f"Funding rate error: {e}")
        return {"available": False}


# ── Microestructura de mercado: Taker OFI + Volume strength ──────────────────

_MICRO_CACHE: tuple[dict, float] = ({}, 0.0)
_MICRO_CACHE_TTL = 30  # 30s — vela 1m cambia cada 60s; refrescar con margen


async def get_btc_microstructure() -> dict:
    """
    Señales de microestructura desde Binance klines 1m (sin API key).

    Taker OFI [-1, +1]:
      kline[9] = takerBuyBaseAssetVolume (BTC comprado por takers = agredir asks).
      kline[5] = volumen total BTC.
      OFI = (2*buy - total) / total → +1 compradores agresivos, -1 vendedores.
      Promedio sobre las 10 velas cerradas más recientes.

    Volume z-score (kline[8] = numberOfTrades):
      z-score sobre ventana de 29 velas cerradas.
      z < -1.5 → mercado thin → señal TA poco confiable.
    """
    import time
    global _MICRO_CACHE
    cached, fetched_at = _MICRO_CACHE
    if cached and (time.time() - fetched_at) < _MICRO_CACHE_TTL:
        return cached
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=8) as client:
            resp = await client.get(
                f"{BINANCE_BASE}/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": 30},
            )
            if resp.status_code != 200:
                return {"available": False}
            klines = resp.json()
            if len(klines) < 12:
                return {"available": False}

        closed = klines[:-1]  # excluir vela en curso

        # Taker OFI: últimas 10 velas cerradas
        ofi_vals = []
        for k in closed[-10:]:
            total = float(k[5])
            buy   = float(k[9])
            if total > 0:
                ofi_vals.append((2 * buy - total) / total)
        taker_ofi = round(sum(ofi_vals) / len(ofi_vals), 4) if ofi_vals else 0.0

        # Volume z-score: todas las velas cerradas disponibles
        n_trades = [float(k[8]) for k in closed]
        mean_n   = sum(n_trades) / len(n_trades)
        std_n    = math.sqrt(
            sum((x - mean_n) ** 2 for x in n_trades) / len(n_trades)
        ) if len(n_trades) > 1 else 0.0
        vol_z = round((n_trades[-1] - mean_n) / std_n, 3) if std_n > 0 else 0.0

        result = {"taker_ofi": taker_ofi, "volume_zscore": vol_z, "available": True}
        _MICRO_CACHE = (result, time.time())
        logger.debug(f"Microstructure: OFI={taker_ofi:+.4f} VolZ={vol_z:.2f}")
        return result
    except Exception as e:
        logger.debug(f"Microstructure error: {e}")
        return {"available": False}


# ── Vela de la ventana actual (OHLC del período del mercado) ─────────────────

async def get_btc_window_ohlc(window_start_ts: int, interval_minutes: int) -> dict:
    """
    Fetch del OHLC de la vela BTC que corresponde al período actual del mercado.

    window_start_ts: Unix timestamp (segundos) de apertura del mercado.
    interval_minutes: 5 o 15.

    Retorna:
      open/high/low/close  — precios del período
      body_ratio           — |close-open| / (high-low)  [0..1]
                             0 = doji puro (sin movimiento neto)
                             1 = marubozu (sin mechas, todo cuerpo)
      body_pct             — |close-open| / open * 100 (magnitud del movimiento)
      upper_wick_ratio     — mecha superior / rango total
      lower_wick_ratio     — mecha inferior / rango total
      candle_direction     — "UP" / "DOWN" según close vs open
      available            — False si el fetch falló
    """
    interval_str = "5m" if interval_minutes <= 5 else "15m"
    start_ms     = int(window_start_ts) * 1000
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=8) as client:
            resp = await client.get(
                f"{BINANCE_BASE}/klines",
                params={
                    "symbol":    "BTCUSDT",
                    "interval":  interval_str,
                    "startTime": start_ms,
                    "limit":     1,
                },
            )
            if resp.status_code != 200:
                return {"available": False}
            klines = resp.json()
            if not klines:
                return {"available": False}

        k = klines[0]
        o = float(k[1])   # open
        h = float(k[2])   # high
        lo = float(k[3])  # low
        c  = float(k[4])  # close

        total_range = h - lo
        body        = abs(c - o)
        body_ratio  = round(body / total_range, 3) if total_range > 0 else 0.0
        body_pct    = round(body / o * 100, 4)     if o > 0 else 0.0

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - lo
        uw_ratio   = round(upper_wick / total_range, 3) if total_range > 0 else 0.0
        lw_ratio   = round(lower_wick / total_range, 3) if total_range > 0 else 0.0

        logger.debug(
            f"Window OHLC {interval_str}: O={o:.0f} H={h:.0f} L={lo:.0f} C={c:.0f} "
            f"body={body_ratio:.2f} range={total_range:.0f}"
        )
        return {
            "open":              o,
            "high":              h,
            "low":               lo,
            "close":             c,
            "body_ratio":        body_ratio,
            "body_pct":          body_pct,
            "upper_wick_ratio":  uw_ratio,
            "lower_wick_ratio":  lw_ratio,
            "candle_direction":  "UP" if c >= o else "DOWN",
            "range_usdc":        round(total_range, 2),
            "available":         True,
        }
    except Exception as e:
        logger.debug(f"Window OHLC error: {e}")
        return {"available": False}


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
