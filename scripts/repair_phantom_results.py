"""
repair_phantom_results.py — Audita y corrige los resultados de trades phantom
usando la resolución REAL de Polymarket (outcomePrices del Gamma API).

Fuente de verdad: outcomePrices[0]=="1" → Up ganó, outcomePrices[1]=="1" → Down ganó
Esto es exactamente lo que usa Polymarket para pagar, basado en Chainlink BTC/USD Data Stream.

Uso:
    python scripts/repair_phantom_results.py [--dry-run]
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
VPS_FILE = ROOT / "data" / "vps_phantom_experiment.json"
LEARNER_FILE = ROOT / "data" / "phantom_learner_stats.json"

DRY_RUN = "--dry-run" in sys.argv


async def get_true_result(client: httpx.AsyncClient, slug: str) -> tuple:
    """
    Retorna (winner, up_token, down_token, price_to_beat, final_price) desde Polymarket.
    winner = "Up" | "Down" | None si no disponible.
    price_to_beat = precio BTC Chainlink al inicio de la ventana.
    final_price   = precio BTC Chainlink al cierre de la ventana.
    """
    try:
        r = await client.get(
            f"https://gamma-api.polymarket.com/events?slug={slug}",
            timeout=10,
        )
        if r.status_code != 200:
            return None, None, None, None, None
        data = r.json()
        if not data:
            return None, None, None, None, None
        ev = data[0]
        markets = ev.get("markets", [])
        if not markets:
            return None, None, None, None, None
        m = markets[0]
        outcomes_raw = m.get("outcomes")       # JSON string or list: ["Up", "Down"]
        prices_raw   = m.get("outcomePrices")  # JSON string or list: ["1", "0"] o ["0", "1"]
        clob_ids_raw = m.get("clobTokenIds")

        # Gamma API returns these as JSON strings, not Python lists
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])
        prices   = json.loads(prices_raw)   if isinstance(prices_raw,   str) else (prices_raw   or [])
        clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else (clob_ids_raw or [])

        if not outcomes or not prices or len(outcomes) < 2 or len(prices) < 2:
            return None, None, None, None, None

        # El outcome con price "1" es el ganador
        winner = None
        for outcome, price in zip(outcomes, prices):
            if str(price) == "1":
                winner = outcome  # "Up" o "Down"
                break

        up_token   = clob_ids[0] if len(clob_ids) > 0 else None
        down_token = clob_ids[1] if len(clob_ids) > 1 else None

        # Precios Chainlink desde eventMetadata
        meta          = ev.get("eventMetadata") or {}
        price_to_beat = meta.get("priceToBeat")   # BTC price at window start (Chainlink)
        final_price   = meta.get("finalPrice")    # BTC price at window end   (Chainlink)

        return winner, up_token, down_token, price_to_beat, final_price

    except Exception as e:
        return None, None, None, None, None


async def main():
    data = json.loads(VPS_FILE.read_text(encoding="utf-8"))
    trades = data.get("trades", [])

    resolved = [t for t in trades if t.get("result") in ("WIN", "LOSS")]
    print(f"Total trades resueltos: {len(resolved)}")
    print(f"Modo: {'DRY RUN (sin cambios)' if DRY_RUN else 'ESCRITURA REAL'}")
    print()

    fixes   = 0
    errors  = 0
    correct = 0
    no_data = 0
    slug_to_result: dict[str, tuple] = {}

    async with httpx.AsyncClient() as client:
        for i, trade in enumerate(resolved):
            slug   = trade.get("slug", "")
            signal = trade.get("signal", "UP")   # "UP" o "DOWN"
            result = trade.get("result", "")      # "WIN" o "LOSS"

            if not slug:
                errors += 1
                continue

            # Consultar Polymarket (con caché para no repetir slugs)
            if slug not in slug_to_result:
                winner, up_tok, down_tok, price_to_beat, final_price = await get_true_result(client, slug)
                slug_to_result[slug] = (winner, up_tok, down_tok, price_to_beat, final_price)
                # Rate limit suave: 40 req/s max
                if i % 40 == 0 and i > 0:
                    await asyncio.sleep(1)
            else:
                winner, up_tok, down_tok, price_to_beat, final_price = slug_to_result[slug]

            if winner is None:
                no_data += 1
                if i % 50 == 0:
                    print(f"  [{i}/{len(resolved)}] {slug[:35]} — sin datos de Polymarket")
                continue

            # Determinar resultado correcto
            # signal="UP" + winner="Up" → WIN
            # signal="UP" + winner="Down" → LOSS
            # signal="DOWN" + winner="Down" → WIN
            # signal="DOWN" + winner="Up" → LOSS
            true_win = (signal == "UP" and winner == "Up") or (signal == "DOWN" and winner == "Down")
            true_result = "WIN" if true_win else "LOSS"

            if true_result != result:
                fixes += 1
                pnl_size = trade.get("position_size_vps", 3.0)
                new_pnl  = round(pnl_size * 0.98,  4) if true_win else -pnl_size
                old_pnl  = trade.get("pnl_vps")

                print(f"  [{i}] CORRECCION: {slug[:40]}")
                print(f"       signal={signal} | Polymarket gano={winner}")
                print(f"       resultado antiguo={result} (pnl={old_pnl}) -> nuevo={true_result} (pnl={new_pnl})")

                if not DRY_RUN:
                    trade["result"]         = true_result
                    trade["pnl_vps"]        = new_pnl
                    trade["pnl_fixed"]      = new_pnl
                    trade["pnl_difference"] = 0.0
                    if up_tok:
                        trade["up_token"]   = up_tok
                    if down_tok:
                        trade["down_token"] = down_tok
            else:
                correct += 1
                # Aprovechar para guardar tokens si faltan
                if not DRY_RUN and up_tok and not trade.get("up_token"):
                    trade["up_token"]   = up_tok
                    trade["down_token"] = down_tok

            # Siempre guardar precios Chainlink si están disponibles
            if not DRY_RUN:
                if price_to_beat is not None:
                    trade["btc_price_to_beat"] = round(float(price_to_beat), 2)
                if final_price is not None:
                    trade["btc_final_price"]   = round(float(final_price),   2)

            if i % 100 == 0:
                print(f"  Progreso: {i}/{len(resolved)} — correctos={correct} correcciones={fixes} sin_datos={no_data}")

    print()
    print(f"=== RESUMEN ===")
    print(f"Total procesados : {len(resolved)}")
    print(f"Correctos        : {correct}")
    print(f"Corregidos       : {fixes}")
    print(f"Sin datos Poly   : {no_data}")
    print(f"Errores          : {errors}")
    print()

    if not DRY_RUN:
        # Recalcular virtual balances desde cero (siempre, no solo al corregir)
        meta = data.setdefault("meta", {})
        initial = 10.0
        bal_vps   = initial
        bal_fixed = initial
        for t in trades:
            pnl_v = t.get("pnl_vps")
            pnl_f = t.get("pnl_fixed")
            if pnl_v is not None:
                bal_vps   += pnl_v
            if pnl_f is not None:
                bal_fixed += pnl_f
        meta["virtual_balance_vps"]   = round(bal_vps,   4)
        meta["virtual_balance_fixed"] = round(bal_fixed, 4)
        print(f"Balance VPS recalculado   : ${bal_vps:.2f}")
        print(f"Balance Fixed recalculado : ${bal_fixed:.2f}")

        VPS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nArchivo guardado: {VPS_FILE}")

        await rebuild_learner_stats(trades)
    elif DRY_RUN:
        print("DRY RUN — no se modifico ningun archivo.")


async def rebuild_learner_stats(trades: list[dict]):
    """Reconstruye phantom_learner_stats.json desde los trades corregidos."""
    from collections import defaultdict

    stats = {
        "5":  {"phantom": {"wins": 0, "total": 0, "recent": [], "by_signal": {}, "by_side": {}, "by_elapsed": {}, "by_skip_reason": {}}},
        "15": {"phantom": {"wins": 0, "total": 0, "recent": [], "by_signal": {}, "by_side": {}, "by_elapsed": {}, "by_skip_reason": {}}},
    }

    for t in trades:
        if t.get("result") not in ("WIN", "LOSS"):
            continue
        interval = "5" if t.get("market") == "updown_5m" else "15"
        won = t["result"] == "WIN"
        ph  = stats[interval]["phantom"]
        ph["total"] += 1
        if won:
            ph["wins"] += 1
        side = t.get("signal", "UP")
        ph["by_side"][side] = ph["by_side"].get(side, {"wins": 0, "total": 0})
        ph["by_side"][side]["total"] += 1
        if won:
            ph["by_side"][side]["wins"] += 1

    if LEARNER_FILE.exists():
        existing = json.loads(LEARNER_FILE.read_text(encoding="utf-8"))
        # Mantener otras keys (real, etc.) — solo reemplazar phantom
        for key in ("5", "15"):
            if key in existing:
                existing[key]["phantom"] = stats[key]["phantom"]
            else:
                existing[key] = stats[key]
        LEARNER_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        LEARNER_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    wins_5  = stats["5"]["phantom"]["wins"]
    tot_5   = stats["5"]["phantom"]["total"]
    wins_15 = stats["15"]["phantom"]["wins"]
    tot_15  = stats["15"]["phantom"]["total"]
    wr5  = wins_5  / tot_5  * 100 if tot_5  else 0
    wr15 = wins_15 / tot_15 * 100 if tot_15 else 0
    print(f"\nLearner reconstruido:")
    print(f"  5m:  {wins_5}/{tot_5}   WR={wr5:.1f}%")
    print(f"  15m: {wins_15}/{tot_15} WR={wr15:.1f}%")
    print(f"Archivo: {LEARNER_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
