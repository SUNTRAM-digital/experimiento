"""
Backtest Engine — simula estrategias sobre datos historicos de Polymarket.

Estrategias disponibles:
  1. value_strategy:    compra cuando precio_mercado < prob_estimada - threshold
  2. momentum_strategy: compra cuando momentum 7+14+30d todos positivos
  3. contrarian_strategy: compra cuando precio < 12% o vende cuando > 88%
  4. combined_strategy: combina value + momentum + tiempo (el enfoque del bot)

Flujo de cada backtest:
  fetch datos historicos → simular señales → calcular PnL → calcular metricas

Limitacion importante: los backtests usan como "prob estimada" el precio final
de resolucion (1.0 o 0.0), no el forecast meteorologico real del momento.
Esto es una aproximacion — para un backtest perfecto se necesita el historial
de forecasts NOAA de cada dia, que no esta disponible via API publica.
El backtest es util para validar patrones de timing y momentum, no la
precision del modelo meteorologico.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

from backtesting.data_fetcher import get_resolved_weather_markets, get_price_history
from backtesting.metrics import calc_metrics


# ── Estrategia 1: Value ───────────────────────────────────────────────────────

def _value_signal(
    price_series: list[dict],
    resolved_yes: float,
    entry_threshold: float = 0.15,
) -> Optional[dict]:
    """
    Señal de value: entra cuando el precio esta al menos entry_threshold
    por debajo de la probabilidad estimada (aproximada como precio final).

    En un backtest real, la prob estimada vendria del modelo meteorologico.
    Aqui usamos el precio en el momento anterior al pico como proxy.
    """
    if not price_series or resolved_yes is None:
        return None

    # Encontrar el precio minimo en los primeros 2/3 de la vida del contrato
    # (zona tipica de entrada antes de que el mercado se mueva)
    n = len(price_series)
    entry_window = price_series[: max(1, n * 2 // 3)]
    if not entry_window:
        return None

    best_entry = min(entry_window, key=lambda x: x["price"])
    entry_price = best_entry["price"]
    edge = resolved_yes - entry_price

    if edge >= entry_threshold:
        exit_price = resolved_yes  # settlement
        pnl = exit_price - entry_price
        return {
            "strategy":    "value",
            "entry_price": entry_price,
            "exit_price":  exit_price,
            "pnl":         pnl,
            "won":         pnl > 0,
            "edge_at_entry": edge,
        }
    return None


# ── Estrategia 2: Momentum ────────────────────────────────────────────────────

def _momentum_signal(
    price_series: list[dict],
    resolved_yes: float,
    lookbacks: tuple[int, ...] = (7, 14, 30),
) -> Optional[dict]:
    """
    Señal de momentum: entra cuando el precio ha subido en los ultimos
    7, 14 Y 30 dias (todos los timeframes positivos = señal de calidad).

    Entra en el primer punto donde los 3 momentums son positivos,
    sale al settlement.
    """
    if not price_series or len(price_series) < max(lookbacks) or resolved_yes is None:
        return None

    prices = [p["price"] for p in price_series]

    # Buscar primer punto con momentum positivo en los 3 timeframes
    for i in range(max(lookbacks), len(prices)):
        momentums = []
        for lb in lookbacks:
            if i >= lb:
                mom = prices[i] - prices[i - lb]
                momentums.append(mom > 0)
            else:
                momentums.append(False)

        if all(momentums):
            entry_price = prices[i]
            exit_price  = resolved_yes
            pnl = exit_price - entry_price
            return {
                "strategy":    "momentum",
                "entry_price": entry_price,
                "exit_price":  exit_price,
                "pnl":         pnl,
                "won":         pnl > 0,
                "days_to_entry": i,
            }

    return None


# ── Estrategia 3: Contrarian ──────────────────────────────────────────────────

def _contrarian_signal(
    price_series: list[dict],
    resolved_yes: float,
    high_threshold: float = 0.88,
    low_threshold:  float = 0.12,
) -> Optional[dict]:
    """
    Señal contrarian: compra NO cuando precio YES > 88% (crowd sobreoptimista)
    o compra YES cuando precio < 12% (crowd demasiado pesimista).
    Sale al settlement.
    """
    if not price_series or resolved_yes is None:
        return None

    prices = [p["price"] for p in price_series]

    for i, price in enumerate(prices):
        # Crowd sobreoptimista: comprar NO
        if price >= high_threshold:
            entry_no  = 1 - price
            exit_no   = 1 - resolved_yes
            pnl = exit_no - entry_no
            return {
                "strategy":    "contrarian_sell",
                "entry_price": entry_no,
                "exit_price":  exit_no,
                "pnl":         pnl,
                "won":         pnl > 0,
                "trigger":     "high",
                "trigger_yes": price,
            }

        # Crowd demasiado pesimista: comprar YES
        if price <= low_threshold:
            entry_yes = price
            exit_yes  = resolved_yes
            pnl = exit_yes - entry_yes
            return {
                "strategy":    "contrarian_buy",
                "entry_price": entry_yes,
                "exit_price":  exit_yes,
                "pnl":         pnl,
                "won":         pnl > 0,
                "trigger":     "low",
                "trigger_yes": price,
            }

    return None


# ── Estrategia 4: Combined (72h rule + value) ─────────────────────────────────

def _combined_signal(
    price_series: list[dict],
    resolved_yes: float,
    ev_threshold: float = 0.15,
    hours_before_resolution: int = 72,
) -> Optional[dict]:
    """
    Estrategia combinada: entra en la ventana de 72h antes del cierre
    solo si hay edge suficiente respecto al precio actual.

    Simula el comportamiento del Patron 1 (72-hour rule) + value.
    """
    if not price_series or len(price_series) < 2 or resolved_yes is None:
        return None

    n = len(price_series)
    # Asumir que la serie abarca 30 dias, cada punto es 1 dia
    # La ventana de 72h = ultimos 3 puntos de la serie
    entry_window_start = max(0, n - 3)
    entry_window = price_series[entry_window_start:]

    if not entry_window:
        return None

    for point in entry_window:
        price = point["price"]
        edge  = resolved_yes - price

        if edge >= ev_threshold:
            pnl = resolved_yes - price
            return {
                "strategy":    "combined_72h",
                "entry_price": price,
                "exit_price":  resolved_yes,
                "pnl":         pnl,
                "won":         pnl > 0,
                "edge":        edge,
            }

    return None


# ── Runner principal ──────────────────────────────────────────────────────────

async def run_backtest(
    strategy: str = "combined_72h",
    limit_markets: int = 100,
    city_filter: Optional[str] = None,
    verbose: bool = False,
) -> dict:
    """
    Ejecuta el backtest de una estrategia sobre mercados historicos.

    Args:
        strategy:       "value" | "momentum" | "contrarian" | "combined_72h"
        limit_markets:  cuantos mercados historicos analizar
        city_filter:    filtrar por ciudad (ej: "new york")
        verbose:        imprimir detalle de cada trade

    Returns:
        {
            "metrics":  dict con win_rate, profit_factor, sharpe, etc.
            "trades":   lista de trades simulados
            "n_markets_analyzed": int,
            "strategy": str,
        }
    """
    strategy_fns: dict[str, Callable] = {
        "value":       _value_signal,
        "momentum":    _momentum_signal,
        "contrarian":  _contrarian_signal,
        "combined_72h": _combined_signal,
    }

    if strategy not in strategy_fns:
        return {"error": f"Estrategia desconocida: {strategy}"}

    signal_fn = strategy_fns[strategy]

    # Obtener mercados historicos
    markets = await get_resolved_weather_markets(limit=limit_markets, city_filter=city_filter)
    if not markets:
        from backtesting.metrics import _empty_metrics
        return {
            "error": "No se pudieron obtener mercados historicos",
            "trades": [], "metrics": _empty_metrics("Sin mercados historicos"),
            "strategy": strategy, "n_markets_analyzed": 0, "n_trades": 0,
        }

    trades = []
    n_analyzed = 0

    # Procesar en paralelo (lotes de 10 para no saturar la API)
    semaphore = asyncio.Semaphore(5)

    async def process_market(market: dict):
        nonlocal n_analyzed
        async with semaphore:
            cid = market.get("condition_id", "")
            resolved = market.get("resolved_yes")
            if not cid or resolved is None:
                return

            price_history = await get_price_history(cid, days_back=30)
            if not price_history:
                return

            n_analyzed += 1
            trade = signal_fn(price_history, resolved)
            if trade:
                trade["market_title"] = market["title"]
                trade["condition_id"] = cid
                if verbose:
                    print(f"  {strategy} | {market['title'][:60]} | "
                          f"PnL: {trade['pnl']:+.3f} | Won: {trade['won']}")
                trades.append(trade)

    await asyncio.gather(*[process_market(m) for m in markets])

    from backtesting.metrics import calc_metrics
    metrics = calc_metrics(trades)

    return {
        "strategy":            strategy,
        "n_markets_analyzed":  n_analyzed,
        "n_trades":            len(trades),
        "trades":              trades,
        "metrics":             metrics,
    }


async def run_all_strategies(
    limit_markets: int = 100,
    city_filter: Optional[str] = None,
) -> dict:
    """
    Corre las 4 estrategias en paralelo y compara resultados.
    """
    strategies = ["value", "momentum", "contrarian", "combined_72h"]
    tasks = [
        run_backtest(s, limit_markets=limit_markets, city_filter=city_filter)
        for s in strategies
    ]
    results_list = await asyncio.gather(*tasks)
    results = {s: r for s, r in zip(strategies, results_list)}

    from backtesting.metrics import compare_strategies
    ranking = compare_strategies({s: r["metrics"] for s, r in results.items() if "metrics" in r})

    return {
        "results":  results,
        "ranking":  ranking,
        "best":     ranking[0][0] if ranking else None,
    }
