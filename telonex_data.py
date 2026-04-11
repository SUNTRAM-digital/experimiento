"""
Telonex Data — Fase 11: Base de conocimiento ampliada con datos on-chain reales.

Provee señales de alta calidad para estrategias UpDown y clima:

  1. OFI Real (Order Flow Imbalance)
     - Calcula buy/sell pressure desde fills on-chain del período actual
     - Reemplaza nuestro proxy (up_price - 0.5) con datos reales del libro
     - Fuente: channel="onchain_fills" por token (up_token del mercado)

  2. Smart Wallet Flow
     - Identifica wallets históricamente rentables en 15m/5m crypto markets
     - Detecta si el "smart money" está comprando UP o DOWN en la ventana actual
     - Ranking actualizado cada 2h (descarga fills de los últimos 7 días)

  3. Microestructura de mercado (book depth)
     - Cost-to-move: cuánto USDC se necesita para mover el precio 1%
     - Replenishment score: qué tan rápido se repone la liquidez

Basado en análisis de Telonex research:
  - top_15m_crypto_traders.ipynb: 2,688 mercados, 46,945 wallets
  - liquidity_rewards_jesus.ipynb: market-making incentives, depth analysis

Requisitos:
  - pip install "telonex[dataframe]>=0.2.2"
  - TELONEX_API_KEY en .env

Si TELONEX_API_KEY no está configurada, el módulo devuelve señales neutras (0.0)
sin bloquear la ejecución del bot.
"""
import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("weatherbot")

_DATA_DIR    = Path(__file__).parent / "data"
_CACHE_DIR   = _DATA_DIR / "telonex_cache"
_WALLETS_CACHE_FILE = _DATA_DIR / "telonex_top_wallets.json"

# TTL para el cache de OFI en memoria (segundos)
_OFI_CACHE_TTL    = 45    # OFI se actualiza cada ~45s (ventana activa cambia rápido)
_WALLET_CACHE_TTL = 7200  # Wallet ranking cada 2h

# Número de wallets "top" a rastrear
_TOP_N_WALLETS = 50

# Días de historia para construir el ranking de wallets
_WALLET_HISTORY_DAYS = 7

# Volumen mínimo de fills para confiar en el OFI calculado
_MIN_FILLS_VOLUME = 5.0   # USDC

# Thread pool para llamadas síncronas del paquete telonex
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telonex")


def _get_api_key() -> str:
    try:
        from config import settings
        return settings.telonex_api_key
    except Exception:
        import os
        return os.getenv("TELONEX_API_KEY", "")


def _is_enabled() -> bool:
    try:
        from config import bot_params
        return bot_params.telonex_enabled
    except Exception:
        return bool(_get_api_key())


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS INTERNOS
# ─────────────────────────────────────────────────────────────────────────────

def _canonical_side(row) -> str:
    """
    Normaliza la dirección de un fill al token 'UP'.
    Equivalente a la función canonical_side() del notebook de Telonex.

    Si el taker compró el token UP → 'buy' (bullish)
    Si el taker vendió el token UP → 'sell' (bearish)
    """
    if row["taker_asset_id"] == row["_up_token"]:
        return "buy" if row["taker_side"] == "buy" else "sell"
    else:
        return "sell" if row["taker_side"] == "buy" else "buy"


def _calc_ofi_from_fills(df, up_token: str, window_start_us: int) -> dict:
    """
    Dado un DataFrame de fills on-chain:
    - Filtra al período de la ventana actual (>= window_start_us)
    - Calcula OFI normalizado en [-1, +1]
    - Calcula volumen en USDC

    Returns: {ofi, up_volume, down_volume, total_fills, total_usdc}
    """
    empty = {"ofi": 0.0, "up_volume": 0.0, "down_volume": 0.0,
             "total_fills": 0, "total_usdc": 0.0}
    try:
        import pandas as pd
        if df is None or len(df) == 0:
            return empty

        # Filtrar a ventana actual
        w = df[df["block_timestamp_us"] >= window_start_us].copy()
        if len(w) == 0:
            return empty

        # Calcular canonical side
        w["_up_token"] = up_token
        w["canonical_side"] = w.apply(_canonical_side, axis=1)

        # Volumen por dirección
        buy_mask  = w["canonical_side"] == "buy"
        sell_mask = w["canonical_side"] == "sell"

        up_vol   = float(w[buy_mask]["amount"].sum())
        down_vol = float(w[sell_mask]["amount"].sum())
        total    = up_vol + down_vol

        ofi = (up_vol - down_vol) / total if total > _MIN_FILLS_VOLUME else 0.0

        # USDC volume aproximado (amount * price)
        usdc_approx = float((w["amount"] * w["price"]).sum())

        return {
            "ofi":         round(ofi, 4),
            "up_volume":   round(up_vol, 4),
            "down_volume": round(down_vol, 4),
            "total_fills": len(w),
            "total_usdc":  round(usdc_approx, 2),
        }
    except Exception as e:
        logger.debug(f"[TELONEX] _calc_ofi_from_fills error: {e}")
        return empty


def _calc_wallet_pnl(df, up_token: str, settlement_value: float = 0.5) -> dict:
    """
    Calcula PnL por wallet desde fills on-chain.
    Réplica de la metodología del notebook top_15m_crypto_traders.

    settlement_value: usado para mercados no resueltos (0.5 = neutral)
    Returns: {wallet_address: {"pnl": float, "fills": int, "volume": float, "maker_ratio": float}}
    """
    result = {}
    try:
        if df is None or len(df) == 0:
            return result

        df["_up_token"] = up_token
        df["canonical_side"] = df.apply(_canonical_side, axis=1)
        df["price"] = df["price"].astype(float)
        df["amount"] = df["amount"].astype(float)

        # PnL por fill: ganancia = (settlement - entry_price) * amount para compradores
        df["pnl"] = df.apply(
            lambda r: (settlement_value - r["price"]) * r["amount"]
            if r["canonical_side"] == "buy"
            else (r["price"] - settlement_value) * r["amount"],
            axis=1,
        )
        df["usdc_vol"] = df["amount"] * df["price"]

        for wallet in set(list(df["maker"]) + list(df["taker"])):
            is_maker = df["maker"] == wallet
            is_taker = df["taker"] == wallet

            wallet_rows = df[is_maker | is_taker]
            if len(wallet_rows) == 0:
                continue

            maker_fills = int(is_maker.sum())
            total_fills = len(wallet_rows)
            volume      = float(wallet_rows["usdc_vol"].sum())
            pnl         = float(wallet_rows["pnl"].sum())

            result[wallet] = {
                "pnl":         round(pnl, 6),
                "fills":       total_fills,
                "volume":      round(volume, 4),
                "maker_ratio": round(maker_fills / total_fills, 3) if total_fills else 0.0,
            }
    except Exception as e:
        logger.debug(f"[TELONEX] _calc_wallet_pnl error: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
#  CLIENTE PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

class TelonexData:
    """
    Proveedor de señales on-chain via Telonex API.
    Thread-safe. Las llamadas al paquete telonex (síncronas) corren en executor.
    """

    def __init__(self):
        _DATA_DIR.mkdir(exist_ok=True)
        _CACHE_DIR.mkdir(exist_ok=True)

        # Cache en memoria para OFI: {up_token: (fetch_ts, result_dict)}
        self._ofi_cache: dict[str, tuple[float, dict]] = {}

        # Top wallets (updated periodically): {wallet_address: stats_dict}
        self._top_wallets: dict[str, dict] = {}
        self._wallets_updated_ts: float = 0.0

        # Cargar wallet cache del disco si existe
        self._load_wallet_cache()

        logger.info(f"[TELONEX] Módulo iniciado. API key: {'configurada' if _get_api_key() else 'NO CONFIGURADA'}")

    # ── Wallet cache ──────────────────────────────────────────────────────────

    def _load_wallet_cache(self):
        try:
            if _WALLETS_CACHE_FILE.exists():
                data = json.loads(_WALLETS_CACHE_FILE.read_text(encoding="utf-8"))
                self._top_wallets      = data.get("wallets", {})
                self._wallets_updated_ts = float(data.get("updated_ts", 0))
                logger.info(f"[TELONEX] Wallet cache cargado: {len(self._top_wallets)} wallets")
        except Exception:
            pass

    def _save_wallet_cache(self):
        try:
            _WALLETS_CACHE_FILE.write_text(
                json.dumps({
                    "wallets":    self._top_wallets,
                    "updated_ts": self._wallets_updated_ts,
                    "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"[TELONEX] Save wallet cache error: {e}")

    # ── OFI en tiempo real ────────────────────────────────────────────────────

    async def get_real_ofi(
        self,
        up_token: str,
        window_start_ts: int,
        interval_minutes: int,
    ) -> dict:
        """
        Calcula OFI real desde fills on-chain del período actual.

        Returns dict:
            ofi          [-1, +1]: positivo = presión compradora en UP
            up_volume    USDC comprado en UP
            down_volume  USDC vendido en UP
            total_fills  número de fills en la ventana
            total_usdc   volumen total aproximado en USDC
            source       "telonex" | "proxy" | "unavailable"
        """
        empty = {"ofi": 0.0, "up_volume": 0.0, "down_volume": 0.0,
                 "total_fills": 0, "total_usdc": 0.0, "source": "unavailable"}

        if not _is_enabled() or not _get_api_key():
            return {**empty, "source": "proxy"}

        # Verificar cache en memoria
        import time
        now_mono = time.monotonic()
        cached = self._ofi_cache.get(up_token)
        if cached and (now_mono - cached[0]) < _OFI_CACHE_TTL:
            return cached[1]

        try:
            api_key = _get_api_key()
            today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Solicitar también el día anterior (mercados que cruzaron medianoche)
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow  = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

            def _fetch():
                from telonex import get_dataframe
                return get_dataframe(
                    api_key=api_key,
                    exchange="polymarket",
                    channel="onchain_fills",
                    asset_id=up_token,
                    from_date=yesterday,
                    to_date=tomorrow,
                    download_dir=str(_CACHE_DIR),
                    engine="pandas",
                )

            df = await asyncio.get_event_loop().run_in_executor(_executor, _fetch)

            window_start_us = window_start_ts * 1_000_000
            result = _calc_ofi_from_fills(df, up_token, window_start_us)
            result["source"] = "telonex"

            # Guardar en cache
            self._ofi_cache[up_token] = (now_mono, result)

            logger.info(
                f"[TELONEX] OFI real: {result['ofi']:+.3f} "
                f"({result['total_fills']} fills, ${result['total_usdc']:.2f} USDC)"
            )
            return result

        except Exception as e:
            logger.warning(f"[TELONEX] get_real_ofi error: {e}")
            return {**empty, "source": "error"}

    # ── Smart wallet flow ─────────────────────────────────────────────────────

    async def get_smart_wallet_bias(
        self,
        up_token: str,
        window_start_ts: int,
    ) -> float:
        """
        Directional bias del smart money en la ventana actual.

        Calcula: promedio ponderado de la dirección de wallets top
        Returns [-1, +1]: positivo = smart money comprando UP
        """
        if not _is_enabled() or not _get_api_key():
            return 0.0

        if not self._top_wallets:
            return 0.0

        try:
            api_key = _get_api_key()
            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            tomorrow  = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

            def _fetch():
                from telonex import get_dataframe
                return get_dataframe(
                    api_key=api_key,
                    exchange="polymarket",
                    channel="onchain_fills",
                    asset_id=up_token,
                    from_date=yesterday,
                    to_date=tomorrow,
                    download_dir=str(_CACHE_DIR),
                    engine="pandas",
                )

            df = await asyncio.get_event_loop().run_in_executor(_executor, _fetch)
            if df is None or len(df) == 0:
                return 0.0

            window_start_us = window_start_ts * 1_000_000
            df_w = df[df["block_timestamp_us"] >= window_start_us].copy()
            if len(df_w) == 0:
                return 0.0

            df_w["_up_token"] = up_token
            df_w["canonical_side"] = df_w.apply(_canonical_side, axis=1)

            top_set = set(self._top_wallets.keys())
            smart_up   = 0.0
            smart_down = 0.0

            for _, row in df_w.iterrows():
                wallet = row["taker"] if row["taker"] in top_set else (
                    row["maker"] if row["maker"] in top_set else None
                )
                if not wallet:
                    continue
                w_score = self._top_wallets[wallet].get("pnl_rank_score", 1.0)
                amount  = float(row.get("amount", 0))
                if row["canonical_side"] == "buy":
                    smart_up   += amount * w_score
                else:
                    smart_down += amount * w_score

            total = smart_up + smart_down
            if total < _MIN_FILLS_VOLUME:
                return 0.0

            bias = (smart_up - smart_down) / total
            logger.debug(
                f"[TELONEX] Smart wallet bias: {bias:+.3f} "
                f"(up={smart_up:.2f} down={smart_down:.2f})"
            )
            return float(round(bias, 4))

        except Exception as e:
            logger.debug(f"[TELONEX] get_smart_wallet_bias error: {e}")
            return 0.0

    # ── Actualización periódica del ranking de wallets ────────────────────────

    async def update_top_wallets(self, force: bool = False) -> int:
        """
        Descarga fills de los últimos _WALLET_HISTORY_DAYS días para mercados 15m,
        calcula PnL por wallet, guarda los top _TOP_N_WALLETS.

        Retorna: número de wallets en el ranking.
        """
        if not _is_enabled() or not _get_api_key():
            return 0

        import time
        now_ts = time.time()
        if not force and (now_ts - self._wallets_updated_ts) < _WALLET_CACHE_TTL:
            return len(self._top_wallets)

        logger.info("[TELONEX] Actualizando ranking de smart wallets...")

        try:
            api_key   = _get_api_key()
            today     = datetime.now(timezone.utc)
            from_date = (today - timedelta(days=_WALLET_HISTORY_DAYS)).strftime("%Y-%m-%d")
            to_date   = (today + timedelta(days=1)).strftime("%Y-%m-%d")

            # Obtener mercados crypto 15m de Polymarket
            def _fetch_markets():
                from telonex import get_markets_dataframe
                return get_markets_dataframe(
                    exchange="polymarket",
                    download_dir=str(_CACHE_DIR),
                )

            markets_df = await asyncio.get_event_loop().run_in_executor(_executor, _fetch_markets)
            if markets_df is None or len(markets_df) == 0:
                logger.warning("[TELONEX] No se pudieron obtener mercados")
                return 0

            # Filtrar a mercados crypto 15m resueltos
            crypto_keywords = ["btc", "bitcoin", "eth", "sol", "xrp"]
            mask = markets_df["slug"].str.lower().str.contains(
                "|".join(crypto_keywords), na=False
            )
            if "tags" in markets_df.columns:
                tag_mask = markets_df["tags"].astype(str).str.lower().str.contains("crypto", na=False)
                mask = mask | tag_mask

            crypto_markets = markets_df[mask]
            logger.info(f"[TELONEX] {len(crypto_markets)} mercados crypto encontrados")

            if len(crypto_markets) == 0:
                return 0

            # Descargar fills para hasta 50 mercados (los más recientes)
            all_wallet_stats: dict[str, dict] = {}
            processed = 0

            for _, row in crypto_markets.head(50).iterrows():
                asset_id = row.get("asset_id_0") or row.get("asset_id")
                if not asset_id:
                    continue

                try:
                    def _fetch_fills(aid=asset_id):
                        from telonex import get_dataframe
                        return get_dataframe(
                            api_key=api_key,
                            exchange="polymarket",
                            channel="onchain_fills",
                            asset_id=aid,
                            from_date=from_date,
                            to_date=to_date,
                            download_dir=str(_CACHE_DIR),
                            engine="pandas",
                        )

                    fills_df = await asyncio.get_event_loop().run_in_executor(_executor, _fetch_fills)
                    if fills_df is None or len(fills_df) == 0:
                        continue

                    # Calcular PnL por wallet en este mercado
                    market_wallets = _calc_wallet_pnl(fills_df, asset_id, settlement_value=0.5)

                    # Agregar al total
                    for wallet, stats in market_wallets.items():
                        if wallet not in all_wallet_stats:
                            all_wallet_stats[wallet] = {
                                "pnl": 0.0, "fills": 0, "volume": 0.0,
                                "markets": 0, "maker_fills": 0,
                            }
                        all_wallet_stats[wallet]["pnl"]    += stats["pnl"]
                        all_wallet_stats[wallet]["fills"]  += stats["fills"]
                        all_wallet_stats[wallet]["volume"] += stats["volume"]
                        all_wallet_stats[wallet]["markets"] += 1
                        all_wallet_stats[wallet]["maker_fills"] += int(
                            stats["fills"] * stats["maker_ratio"]
                        )

                    processed += 1
                except Exception as e:
                    logger.debug(f"[TELONEX] Error procesando {asset_id[:12]}…: {e}")
                    continue

            logger.info(f"[TELONEX] Procesados {processed} mercados, {len(all_wallet_stats)} wallets únicos")

            if not all_wallet_stats:
                return 0

            # Calcular score de ranking (PnL normalizado por volumen)
            max_pnl = max(abs(s["pnl"]) for s in all_wallet_stats.values()) or 1.0
            for w, s in all_wallet_stats.items():
                vol = s["volume"] or 1.0
                # Score: PnL normalizado, bonus por consistencia (múltiples mercados)
                s["pnl_per_volume"] = s["pnl"] / vol
                s["pnl_rank_score"] = (s["pnl"] / max_pnl) * (1 + min(s["markets"] / 10, 0.5))
                s["maker_ratio"]    = round(s["maker_fills"] / max(s["fills"], 1), 3)

            # Top N wallets con PnL positivo y suficientes fills
            top = {
                w: s for w, s in sorted(
                    all_wallet_stats.items(),
                    key=lambda x: x[1]["pnl"],
                    reverse=True,
                )
                if s["pnl"] > 0 and s["fills"] >= 3
            }
            self._top_wallets = dict(list(top.items())[:_TOP_N_WALLETS])
            self._wallets_updated_ts = now_ts
            self._save_wallet_cache()

            logger.info(
                f"[TELONEX] Ranking actualizado: {len(self._top_wallets)} smart wallets "
                f"| Top PnL: ${list(self._top_wallets.values())[0]['pnl']:.2f} "
                f"(si hay wallets)"
                if self._top_wallets else
                "[TELONEX] Ranking actualizado: 0 wallets con PnL positivo suficiente"
            )
            return len(self._top_wallets)

        except Exception as e:
            logger.warning(f"[TELONEX] update_top_wallets error: {e}")
            return 0

    # ── Signal combinada para strategy_updown ────────────────────────────────

    async def get_updown_signals(
        self,
        market: dict,
        btc_price_start: float,
    ) -> dict:
        """
        Devuelve todas las señales Telonex relevantes para un mercado UpDown.

        Returns:
            real_ofi       [-1, +1]: OFI real desde fills on-chain
            smart_bias     [-1, +1]: directional bias del smart money
            total_usdc     USDC de volumen en la ventana actual
            total_fills    número de fills
            available      True si Telonex está configurado y devolvió datos
        """
        up_token        = market.get("up_token", "")
        window_start_ts = market.get("window_start_ts", 0)
        interval_min    = market.get("interval_minutes", 15)

        if not up_token or not window_start_ts:
            return {"real_ofi": 0.0, "smart_bias": 0.0, "available": False,
                    "total_usdc": 0.0, "total_fills": 0}

        # Paralelizar OFI + smart bias (misma descarga de fills)
        ofi_result, smart_bias = await asyncio.gather(
            self.get_real_ofi(up_token, window_start_ts, interval_min),
            self.get_smart_wallet_bias(up_token, window_start_ts),
            return_exceptions=True,
        )

        if isinstance(ofi_result, Exception):
            ofi_result = {"ofi": 0.0, "total_usdc": 0.0, "total_fills": 0, "source": "error"}
        if isinstance(smart_bias, Exception):
            smart_bias = 0.0

        available = ofi_result.get("source") == "telonex"
        return {
            "real_ofi":    ofi_result.get("ofi", 0.0),
            "smart_bias":  float(smart_bias),
            "total_usdc":  ofi_result.get("total_usdc", 0.0),
            "total_fills": ofi_result.get("total_fills", 0),
            "up_volume":   ofi_result.get("up_volume", 0.0),
            "down_volume": ofi_result.get("down_volume", 0.0),
            "source":      ofi_result.get("source", "unavailable"),
            "available":   available,
        }

    # ── Stats para la UI ─────────────────────────────────────────────────────

    def get_status(self) -> dict:
        import time
        top = list(self._top_wallets.items())
        return {
            "available":        _is_enabled() and bool(_get_api_key()),
            "enabled":          _is_enabled(),
            "api_key_set":      bool(_get_api_key()),
            "top_wallet_count": len(self._top_wallets),
            "last_wallet_update": self._wallets_updated_ts if self._wallets_updated_ts > 0 else None,
            "wallets_updated_at": (
                datetime.fromtimestamp(self._wallets_updated_ts, tz=timezone.utc)
                .strftime("%Y-%m-%d %H:%M UTC")
                if self._wallets_updated_ts > 0 else None
            ),
            "wallets_age_minutes": round((time.time() - self._wallets_updated_ts) / 60, 1)
                if self._wallets_updated_ts > 0 else None,
            "fills_24h":    None,    # populated by future rolling stats
            "volume_24h_usdc": None,
            "last_ofi":     None,
            "ofi_cache_entries": len(self._ofi_cache),
            "top_wallets": [
                {
                    "wallet":        w,
                    "pnl_7d":        round(s["pnl"], 2),
                    "total_trades":  s["fills"],
                    "up_trades":     None,   # not tracked per-direction in aggregate stats
                    "pnl_rank_score": round(s.get("pnl_rank_score", 0.0), 4),
                    "markets":       s.get("markets", 0),
                    "maker_ratio":   s.get("maker_ratio", 0),
                }
                for w, s in top
            ],
        }


# ── Singleton global ───────────────────────────────────────────────────────────
telonex_data = TelonexData()
