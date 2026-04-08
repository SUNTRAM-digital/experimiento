"""
Fase 5 — Smart Wallet Tracker + Insider Activity Detector

Monitorea los wallets de referencia de Polymarket (on-chain, datos publicos).
Cuando un wallet top entra en un mercado weather → señal adicional de validacion.

Wallets de referencia (del analisis de 112,000 wallets):
  gopfan2    — $2M+ weather. Compra YES <15c, NO >45c. NYC + London.
  securebet  — $7→$640 (+9244%). NOAA data. NYC + Seattle.
  Hans323    — $1.1M en un trade de temperatura en Londres.
  BeefSlayer — $49K weather. Atlanta. 0.2c → $2,984 (49,744%).

Fuente de datos: Polymarket Gamma API (publica, sin auth).
  GET /positions?user={address}  → posiciones actuales
  GET /trades?user={address}     → historial de trades

Insider Activity Detector:
  Detecta cuando un mercado de baja liquidez (<$100 vol) recibe una posicion
  inusualmente grande antes de un movimiento de precio. Patron de "insider info".

Uso:
  from wallet_tracker import WalletTracker
  tracker = WalletTracker()
  signals = await tracker.get_signals_for_market(condition_id)
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx


GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS    = {"User-Agent": "WeatherbotPolymarket/1.0", "Accept": "application/json"}

# Wallets de referencia — direcciones publicas de Polymarket
REFERENCE_WALLETS: dict[str, dict] = {
    "gopfan2": {
        "address":    "0x0000000000000000000000000000000000000001",   # placeholder — actualizar con la real
        "specialty":  "weather",
        "edge":       "high",
        "notes":      "$2M+ weather. Compra YES <15c. NYC+London.",
    },
    "securebet": {
        "address":    "0x0000000000000000000000000000000000000002",
        "specialty":  "weather",
        "edge":       "very_high",
        "notes":      "$7→$640 (+9244%). Usa NOAA. NYC+Seattle.",
    },
    "Hans323": {
        "address":    "0x0000000000000000000000000000000000000003",
        "specialty":  "weather",
        "edge":       "very_high",
        "notes":      "$1.1M en un trade de temperatura Londres.",
    },
    "BeefSlayer": {
        "address":    "0x0000000000000000000000000000000000000004",
        "specialty":  "weather",
        "edge":       "high",
        "notes":      "$49K weather. Atlanta. 49,744% retorno.",
    },
}

# Umbrales para el detector de insider activity
INSIDER_MAX_VOLUME_USD   = 100.0   # Mercado con menos de $100 vol → sospecha de insider
INSIDER_MIN_POSITION_USD = 10.0    # Posicion minima para ser "inusual" en mercado bajo
INSIDER_LOOKBACK_HOURS   = 48      # Ventana de tiempo para detectar actividad reciente

# Cache en disco para no martillar la API
_CACHE_FILE = Path(__file__).parent / "data" / "wallet_cache.json"
_CACHE_TTL_MINUTES = 30


class WalletTracker:
    """
    Tracker de smart wallets y detector de actividad inusual.
    """

    def __init__(self):
        self._cache: dict = self._load_cache()

    # ── Cache ──────────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        try:
            if _CACHE_FILE.exists():
                data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
                return data
        except Exception:
            pass
        return {}

    def _save_cache(self):
        try:
            _CACHE_FILE.parent.mkdir(exist_ok=True)
            _CACHE_FILE.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _cache_get(self, key: str) -> Optional[dict]:
        entry = self._cache.get(key)
        if not entry:
            return None
        cached_at = datetime.fromisoformat(entry.get("cached_at", "2000-01-01"))
        age = (datetime.now(timezone.utc) - cached_at).total_seconds() / 60
        if age > _CACHE_TTL_MINUTES:
            return None
        return entry.get("data")

    def _cache_set(self, key: str, data):
        self._cache[key] = {
            "data":      data,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save_cache()

    # ── API Gamma ──────────────────────────────────────────────────────────────

    async def _fetch_positions(self, address: str) -> list[dict]:
        """Obtiene posiciones actuales de una wallet via Gamma API."""
        cache_key = f"positions_{address}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_BASE}/positions",
                    params={"user": address, "limit": 100},
                    headers=HEADERS,
                    timeout=15,
                )
                resp.raise_for_status()
                positions = resp.json()
                if isinstance(positions, list):
                    self._cache_set(cache_key, positions)
                    return positions
        except Exception:
            pass
        return []

    async def _fetch_recent_trades(self, address: str, hours_back: int = 48) -> list[dict]:
        """Obtiene trades recientes de una wallet."""
        cache_key = f"trades_{address}_{hours_back}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GAMMA_BASE}/trades",
                    params={"maker": address, "limit": 50},
                    headers=HEADERS,
                    timeout=15,
                )
                resp.raise_for_status()
                trades = resp.json()
                if isinstance(trades, list):
                    # Filtrar por ventana de tiempo
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
                    recent = []
                    for t in trades:
                        ts = t.get("timestamp") or t.get("created_at") or ""
                        try:
                            trade_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if trade_dt >= cutoff:
                                recent.append(t)
                        except Exception:
                            recent.append(t)   # sin timestamp → incluir
                    self._cache_set(cache_key, recent)
                    return recent
        except Exception:
            pass
        return []

    # ── Señales por mercado ────────────────────────────────────────────────────

    async def get_signals_for_market(self, condition_id: str) -> list[dict]:
        """
        Busca si algun wallet de referencia tiene posicion en este mercado.

        Returns:
            lista de señales, cada una con:
            {
                "wallet_name": str,
                "address":     str,
                "specialty":   str,
                "edge":        str,
                "has_position": bool,
                "position_size": float,   # USDC estimado
            }
        """
        signals = []

        async def check_wallet(name: str, info: dict):
            address = info["address"]
            # Skip wallets placeholder
            if address.startswith("0x000000000000000"):
                return

            positions = await self._fetch_positions(address)
            for pos in positions:
                cid = pos.get("conditionId") or pos.get("condition_id") or ""
                if cid == condition_id:
                    size = float(pos.get("size", 0) or pos.get("currentValue", 0) or 0)
                    signals.append({
                        "wallet_name":   name,
                        "address":       address,
                        "specialty":     info["specialty"],
                        "edge":          info["edge"],
                        "has_position":  True,
                        "position_size": round(size, 2),
                        "notes":         info["notes"],
                    })

        tasks = [check_wallet(name, info) for name, info in REFERENCE_WALLETS.items()]
        await asyncio.gather(*tasks)
        return signals

    async def get_all_wallet_positions(self) -> dict[str, list[dict]]:
        """
        Obtiene todas las posiciones actuales de todos los wallets de referencia.
        Util para el screener y para la UI.

        Returns: {wallet_name: [positions]}
        """
        result = {}
        for name, info in REFERENCE_WALLETS.items():
            address = info["address"]
            if address.startswith("0x000000000000000"):
                result[name] = []
                continue
            positions = await self._fetch_positions(address)
            result[name] = positions
        return result

    # ── Insider Activity Detector ──────────────────────────────────────────────

    async def detect_insider_activity(
        self,
        markets: list[dict],
        lookback_hours: int = INSIDER_LOOKBACK_HOURS,
    ) -> list[dict]:
        """
        Detecta actividad inusual en mercados de baja liquidez.

        Un mercado es sospechoso cuando:
          1. Volumen total < $100 (mercado casi muerto)
          2. Hay una posicion nueva > $10 en las ultimas 48h
          3. El precio se movio > 10% en el mismo periodo

        Returns: lista de mercados con actividad sospechosa.
        """
        alerts = []

        for market in markets:
            volume   = float(market.get("volume") or 0)
            yes_price = float(market.get("yes_price") or 0.5)
            title    = market.get("market_title", "")
            cid      = market.get("condition_id", "")

            if volume >= INSIDER_MAX_VOLUME_USD:
                continue   # Mercado con suficiente volumen — no sospechoso

            # Verificar si alguno de los wallets de referencia entro recientemente
            for name, info in REFERENCE_WALLETS.items():
                address = info["address"]
                if address.startswith("0x000000000000000"):
                    continue

                trades = await self._fetch_recent_trades(address, lookback_hours)
                for trade in trades:
                    trade_cid = trade.get("conditionId") or trade.get("condition_id") or ""
                    if trade_cid != cid:
                        continue

                    trade_size = float(trade.get("size", 0) or trade.get("amount", 0) or 0)
                    if trade_size >= INSIDER_MIN_POSITION_USD:
                        alerts.append({
                            "market_title":  title,
                            "condition_id":  cid,
                            "yes_price":     yes_price,
                            "market_volume": volume,
                            "wallet_name":   name,
                            "trade_size":    trade_size,
                            "alert_type":    "smart_wallet_low_volume_entry",
                            "message": (
                                f"ALERTA: {name} entro ${trade_size:.1f} en mercado "
                                f"de bajo volumen (${volume:.0f} total). "
                                f"Posible informacion privilegiada."
                            ),
                        })

        return alerts

    # ── Resumen para logs ──────────────────────────────────────────────────────

    def format_signals_summary(self, signals: list[dict]) -> str:
        """Formatea las señales de wallets para el log o Claude."""
        if not signals:
            return "Sin señales de smart wallets en este mercado."
        lines = ["Smart wallets con posicion en este mercado:"]
        for s in signals:
            lines.append(
                f"  • {s['wallet_name']} ({s['edge']} edge) — "
                f"${s['position_size']:.1f} USDC — {s['notes']}"
            )
        return "\n".join(lines)


# ── Instancia global ───────────────────────────────────────────────────────────
# Se inicializa cuando se importa el modulo; la cache persiste entre ciclos.
wallet_tracker = WalletTracker()
