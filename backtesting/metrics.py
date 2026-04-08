"""
Metricas de performance para backtesting.

Metricas calculadas:
  - win_rate:       % de trades ganadores
  - profit_factor:  ganancia bruta / perdida bruta (>1.5 es bueno)
  - sharpe_ratio:   retorno / volatilidad ajustada (>1 aceptable, >2 excelente)
  - max_drawdown:   mayor caida pico-a-valle (< 20% es aceptable)
  - avg_return:     retorno promedio por trade
  - total_return:   retorno total acumulado

Umbrales minimos para considerar una estrategia rentable:
  win_rate    > 55%
  profit_factor > 1.5
  max_drawdown < 20%
  min_trades   >= 100 (para significancia estadistica)
"""
import math
from typing import Optional


MIN_WIN_RATE      = 0.55
MIN_PROFIT_FACTOR = 1.5
MAX_DRAWDOWN      = 0.20
MIN_TRADES        = 100


def calc_metrics(trades: list[dict]) -> dict:
    """
    Calcula todas las metricas de performance a partir de una lista de trades.

    Cada trade debe tener:
      - 'pnl':     ganancia/perdida por trade (en % o USDC, consistente)
      - 'won':     True/False
      - [opcional] 'entry_price', 'exit_price', 'size_usdc'

    Returns dict con todas las metricas + diagnostico de validez.
    """
    if not trades:
        return _empty_metrics("Sin trades")

    n = len(trades)
    pnls = [t.get("pnl", 0.0) for t in trades]

    # ── Win Rate ──────────────────────────────────────────────────────────────
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p < 0]
    win_rate = len(winners) / n if n > 0 else 0.0

    # ── Profit Factor ─────────────────────────────────────────────────────────
    gross_profit = sum(winners) if winners else 0.0
    gross_loss   = abs(sum(losers)) if losers else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    # ── Curva de equity y Max Drawdown ────────────────────────────────────────
    equity = [0.0]
    for p in pnls:
        equity.append(equity[-1] + p)

    peak = equity[0]
    max_dd = 0.0
    for val in equity:
        if val > peak:
            peak = val
        if peak != 0:
            dd = (peak - val) / abs(peak)
            max_dd = max(max_dd, dd)

    # ── Sharpe Ratio (anualizado, asumiendo ~365 trades/año como aproximacion) ─
    avg_pnl = sum(pnls) / n
    std_pnl = _std(pnls)
    sharpe = (avg_pnl / std_pnl * math.sqrt(365)) if std_pnl > 0 else 0.0

    # ── Retorno total ─────────────────────────────────────────────────────────
    total_return = equity[-1]
    avg_return   = avg_pnl

    # ── Diagnostico de validez ────────────────────────────────────────────────
    issues = []
    if n < MIN_TRADES:
        issues.append(f"muestra pequena ({n} trades, minimo {MIN_TRADES})")
    if win_rate < MIN_WIN_RATE:
        issues.append(f"win rate bajo ({win_rate:.1%} < {MIN_WIN_RATE:.0%})")
    if profit_factor < MIN_PROFIT_FACTOR:
        issues.append(f"profit factor bajo ({profit_factor:.2f} < {MIN_PROFIT_FACTOR})")
    if max_dd > MAX_DRAWDOWN:
        issues.append(f"max drawdown alto ({max_dd:.1%} > {MAX_DRAWDOWN:.0%})")

    valid = len(issues) == 0

    return {
        "n_trades":      n,
        "win_rate":      round(win_rate, 4),
        "profit_factor": round(profit_factor, 3),
        "sharpe_ratio":  round(sharpe, 3),
        "max_drawdown":  round(max_dd, 4),
        "total_return":  round(total_return, 4),
        "avg_return":    round(avg_return, 4),
        "gross_profit":  round(gross_profit, 4),
        "gross_loss":    round(gross_loss, 4),
        "n_winners":     len(winners),
        "n_losers":      len(losers),
        "equity_curve":  equity,
        "valid":         valid,
        "issues":        issues,
        "summary":       _summary(win_rate, profit_factor, max_dd, sharpe, n, valid),
    }


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _empty_metrics(reason: str) -> dict:
    return {
        "n_trades": 0, "win_rate": 0, "profit_factor": 0,
        "sharpe_ratio": 0, "max_drawdown": 0, "total_return": 0,
        "avg_return": 0, "gross_profit": 0, "gross_loss": 0,
        "n_winners": 0, "n_losers": 0, "equity_curve": [],
        "valid": False, "issues": [reason], "summary": reason,
    }


def _summary(wr: float, pf: float, dd: float, sr: float, n: int, valid: bool) -> str:
    status = "ESTRATEGIA VALIDA" if valid else "NO VALIDA"
    return (
        f"{status} | {n} trades | "
        f"WR: {wr:.1%} | PF: {pf:.2f} | "
        f"Sharpe: {sr:.2f} | MaxDD: {dd:.1%}"
    )


def compare_strategies(results: dict[str, dict]) -> list[tuple[str, dict]]:
    """
    Ordena estrategias de mejor a peor segun score combinado.
    Score = win_rate * profit_factor / max(max_drawdown, 0.01)
    """
    scored = []
    for name, m in results.items():
        if m["n_trades"] == 0:
            continue
        score = (m["win_rate"] * m["profit_factor"]) / max(m["max_drawdown"], 0.01)
        scored.append((name, m, score))
    scored.sort(key=lambda x: x[2], reverse=True)
    return [(name, m) for name, m, _ in scored]
