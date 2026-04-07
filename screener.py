"""
Screener de mercados Polymarket — identifica oportunidades con edge real.

Dos modulos:
  1. ValueScreener:    detecta mercados donde precio_mercado < prob_estimada - 15%
  2. MomentumScreener: detecta mercados con momentum positivo en 7, 14 y 30 dias

El screener es independiente del bot principal — puede ejecutarse en paralelo
para alimentar una lista de oportunidades, o como herramienta de analisis manual.

Uso:
  python screener.py                   # corre ambos screeners
  python screener.py --type value      # solo value
  python screener.py --type momentum   # solo momentum
  python screener.py --limit 50        # limitar a 50 mercados
"""
import asyncio
import argparse
from datetime import datetime, timezone
from typing import Optional

import httpx

from backtesting.data_fetcher import get_price_history

GAMMA_BASE = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "WeatherbotPolymarket/1.0", "Accept": "application/json"}

# Umbrales del screener
VALUE_GAP_MIN      = 0.15   # Brecha minima entre precio y prob estimada para señal de value
MOMENTUM_LOOKBACKS = (7, 14, 30)  # Dias de lookback para momentum
MIN_VOLUME         = 100.0  # Volumen minimo para considerar un mercado liquido


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _fetch_active_weather_markets(limit: int = 200) -> list[dict]:
    """
    Obtiene mercados de temperatura activos desde Gamma API.
    Solo incluye mercados que aceptan ordenes y tienen volumen minimo.
    """
    markets = []
    async with httpx.AsyncClient() as client:
        offset = 0
        while len(markets) < limit:
            try:
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit":  100,
                    "offset": offset,
                    "order":  "volume",
                }
                resp = await client.get(
                    f"{GAMMA_BASE}/markets",
                    params=params,
                    headers=HEADERS,
                    timeout=20,
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break

                for m in batch:
                    title = m.get("question") or m.get("title") or ""
                    title_lower = title.lower()

                    if not any(kw in title_lower for kw in ["temperature", "temp", "degrees", "f"]):
                        continue

                    volume = float(m.get("volume") or 0)
                    if volume < MIN_VOLUME:
                        continue

                    # Extraer precio YES actual del orderbook
                    outcome_prices = m.get("outcomePrices") or []
                    if isinstance(outcome_prices, str):
                        import json as _json
                        try:
                            outcome_prices = _json.loads(outcome_prices)
                        except Exception:
                            outcome_prices = []

                    yes_price = None
                    if outcome_prices and len(outcome_prices) >= 1:
                        try:
                            yes_price = float(outcome_prices[0])
                        except (ValueError, TypeError):
                            pass

                    if yes_price is None or yes_price <= 0 or yes_price >= 1:
                        continue

                    markets.append({
                        "title":        title,
                        "condition_id": m.get("conditionId") or m.get("id") or "",
                        "volume":       volume,
                        "yes_price":    yes_price,
                        "end_date":     m.get("endDate") or "",
                        "raw":          m,
                    })

                    if len(markets) >= limit:
                        break

                offset += 100
                if offset >= 1000:
                    break
            except Exception:
                break

    return markets


def _hours_to_close(end_date_str: str) -> float:
    """Calcula las horas restantes hasta el cierre dado un string ISO de fecha."""
    if not end_date_str:
        return 9999.0
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = end_dt - now
        return max(0.0, delta.total_seconds() / 3600)
    except Exception:
        return 9999.0


# ── Value Screener ─────────────────────────────────────────────────────────────

async def value_screen(
    markets: list[dict],
    gap_threshold: float = VALUE_GAP_MIN,
    semaphore_limit: int = 5,
) -> list[dict]:
    """
    Detecta mercados donde el precio de mercado esta al menos gap_threshold
    por debajo de la prob estimada (calculada como promedio ponderado del historial).

    La prob estimada en este screener NO usa el modelo meteorologico (no tenemos
    el forecast en tiempo real para cada ciudad aqui). En cambio, usamos el
    precio promedio ponderado de los ultimos 3 dias como proxy de lo que el
    mercado "deberia" valer antes de momentum reciente — si el precio bajo mucho
    en las ultimas 24h sin razon fundamental, hay potencial value.

    Esto es complementario al evaluate_market() del bot, que si usa NOAA+OpenMeteo.
    """
    semaphore = asyncio.Semaphore(semaphore_limit)
    results = []

    async def check_market(m: dict):
        async with semaphore:
            cid = m.get("condition_id", "")
            if not cid:
                return

            history = await get_price_history(cid, days_back=30)
            if len(history) < 5:
                return

            # Precio promedio de la primera mitad de la vida del contrato
            # como proxy de "valor antes del momentum reciente"
            n = len(history)
            baseline_window = history[: max(1, n * 2 // 3)]
            avg_baseline = sum(p["price"] for p in baseline_window) / len(baseline_window)

            current_price = m["yes_price"]
            gap = avg_baseline - current_price

            if gap >= gap_threshold:
                hours = _hours_to_close(m.get("end_date", ""))
                results.append({
                    "type":          "value",
                    "title":         m["title"],
                    "condition_id":  cid,
                    "current_price": round(current_price, 4),
                    "estimated_prob": round(avg_baseline, 4),
                    "gap":           round(gap, 4),
                    "volume":        m["volume"],
                    "hours_to_close": round(hours, 1),
                    "price_history_n": n,
                })

    await asyncio.gather(*[check_market(m) for m in markets])

    # Ordenar por gap descendente (mejor edge primero)
    results.sort(key=lambda x: x["gap"], reverse=True)
    return results


# ── Momentum Screener ──────────────────────────────────────────────────────────

async def momentum_screen(
    markets: list[dict],
    lookbacks: tuple[int, ...] = MOMENTUM_LOOKBACKS,
    semaphore_limit: int = 5,
) -> list[dict]:
    """
    Detecta mercados donde el precio YES ha subido en los ultimos 7, 14 Y 30 dias.
    Si los 3 timeframes son positivos = señal de momentum de calidad.

    Tambien calcula la fuerza del momentum (promedio de las 3 deltas normalizadas).
    """
    semaphore = asyncio.Semaphore(semaphore_limit)
    results = []

    async def check_momentum(m: dict):
        async with semaphore:
            cid = m.get("condition_id", "")
            if not cid:
                return

            history = await get_price_history(cid, days_back=max(lookbacks) + 5)
            if len(history) < max(lookbacks):
                return

            prices = [p["price"] for p in history]
            n = len(prices)
            current = prices[-1]

            # Calcular momentum para cada lookback
            moms = {}
            all_positive = True
            for lb in lookbacks:
                if n > lb:
                    past_price = prices[-(lb + 1)]
                    delta = current - past_price
                    moms[f"mom_{lb}d"] = round(delta, 4)
                    if delta <= 0:
                        all_positive = False
                else:
                    all_positive = False
                    moms[f"mom_{lb}d"] = None

            if not all_positive:
                return

            # Fuerza del momentum: promedio de cambios absolutos normalizados
            valid_moms = [abs(v) for v in moms.values() if v is not None]
            strength = sum(valid_moms) / len(valid_moms) if valid_moms else 0.0

            hours = _hours_to_close(m.get("end_date", ""))
            results.append({
                "type":          "momentum",
                "title":         m["title"],
                "condition_id":  cid,
                "current_price": round(current, 4),
                "volume":        m["volume"],
                "hours_to_close": round(hours, 1),
                "momentum_strength": round(strength, 4),
                **moms,
            })

    await asyncio.gather(*[check_momentum(m) for m in markets])

    # Ordenar por fuerza de momentum
    results.sort(key=lambda x: x["momentum_strength"], reverse=True)
    return results


# ── Runner combinado ───────────────────────────────────────────────────────────

async def run_screener(
    screen_type: str = "both",
    limit: int = 200,
) -> dict:
    """
    Ejecuta el screener y retorna las oportunidades encontradas.

    Args:
        screen_type: "value" | "momentum" | "both"
        limit:       cuantos mercados activos escanear

    Returns:
        {
            "value_opportunities":    list[dict],
            "momentum_opportunities": list[dict],
            "n_markets_scanned":      int,
            "timestamp":              str ISO,
        }
    """
    print(f"[screener] Fetching active weather markets (limit={limit})...")
    markets = await _fetch_active_weather_markets(limit=limit)
    print(f"[screener] Found {len(markets)} markets with volume >= {MIN_VOLUME}")

    value_ops = []
    mom_ops   = []

    if screen_type in ("value", "both"):
        print("[screener] Running value screen...")
        value_ops = await value_screen(markets)
        print(f"[screener] Value opportunities: {len(value_ops)}")

    if screen_type in ("momentum", "both"):
        print("[screener] Running momentum screen...")
        mom_ops = await momentum_screen(markets)
        print(f"[screener] Momentum opportunities: {len(mom_ops)}")

    return {
        "value_opportunities":    value_ops,
        "momentum_opportunities": mom_ops,
        "n_markets_scanned":      len(markets),
        "timestamp":              datetime.now(timezone.utc).isoformat(),
    }


def _print_results(results: dict):
    """Imprime los resultados del screener en formato legible."""
    print(f"\n{'='*70}")
    print(f"  SCREENER RESULTS — {results['timestamp'][:19]}Z")
    print(f"  Markets scanned: {results['n_markets_scanned']}")
    print(f"{'='*70}")

    if results["value_opportunities"]:
        print(f"\n  VALUE OPPORTUNITIES ({len(results['value_opportunities'])} found):")
        print(f"  {'Title':<45} {'Price':>6} {'Est.Prob':>8} {'Gap':>6} {'Hours':>7}")
        print(f"  {'-'*45} {'-'*6} {'-'*8} {'-'*6} {'-'*7}")
        for op in results["value_opportunities"][:10]:  # Top 10
            title = op["title"][:44]
            print(
                f"  {title:<45} {op['current_price']:>6.3f} "
                f"{op['estimated_prob']:>8.3f} {op['gap']:>6.3f} "
                f"{op['hours_to_close']:>7.1f}h"
            )
    else:
        print("\n  VALUE: No opportunities found.")

    if results["momentum_opportunities"]:
        print(f"\n  MOMENTUM OPPORTUNITIES ({len(results['momentum_opportunities'])} found):")
        print(f"  {'Title':<45} {'Price':>6} {'7d':>6} {'14d':>6} {'30d':>6} {'Str':>5}")
        print(f"  {'-'*45} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*5}")
        for op in results["momentum_opportunities"][:10]:
            title = op["title"][:44]
            m7  = op.get("mom_7d")  or 0
            m14 = op.get("mom_14d") or 0
            m30 = op.get("mom_30d") or 0
            print(
                f"  {title:<45} {op['current_price']:>6.3f} "
                f"{m7:>+6.3f} {m14:>+6.3f} {m30:>+6.3f} "
                f"{op['momentum_strength']:>5.3f}"
            )
    else:
        print("\n  MOMENTUM: No opportunities found.")

    print(f"\n{'='*70}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket weather market screener")
    parser.add_argument("--type",  default="both", choices=["value", "momentum", "both"])
    parser.add_argument("--limit", type=int, default=200, help="Markets to scan")
    args = parser.parse_args()

    results = asyncio.run(run_screener(screen_type=args.type, limit=args.limit))
    _print_results(results)
