"""Point 19 — preflight gates (drawdown kill, paper-to-live, stale price).
Implementado tras analizar imágenes 1-19 (base de conocimiento, 'Why your
Claude bot doesn't work').
Valida:
  A. trading_positions.real_equity_drawdown() detecta dd desde ATH.
  B. trading_positions.phantom_gate_status() bloquea real sin days/trades/wr.
  C. evaluate_and_open rechaza mercados con price_ts obsoleto.
  D. config.BotParams expone los nuevos params y persisten via update().
  E. _check_real_safety enforcea gate y drawdown.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _fresh_state(tmp_path: Path):
    """Reset trading_positions file to aislar test."""
    import trading_positions as tp
    tp._STATE_FILE = str(tmp_path / 'positions.json')
    if hasattr(tp, '_cache'):
        tp._cache = None
    # Limpiar por si hay contenido previo
    try:
        tp._save(tp._default_state())
    except Exception:
        pass
    return tp


# ───────── 19A — drawdown desde ATH ─────────

def test_drawdown_zero_when_no_trades(tmp_path):
    tp = _fresh_state(tmp_path)
    dd = tp.real_equity_drawdown()
    assert dd["peak"] == 0
    assert dd["drawdown_pct"] == 0


def test_drawdown_detects_dropoff(tmp_path):
    tp = _fresh_state(tmp_path)
    # secuencia: +10 → peak=10; -5 → current=5, dd=50%
    p1 = tp.open_position("s", interval=5, end_ts=9999999999,
                          side="UP", token_id="t1", entry_price=0.50,
                          target_price=0.80, stake_usdc=5, is_real=True)
    tp.close_position("s", p1["id"], exit_price=0.80,
                       pnl_usdc=10.0, exit_reason="TARGET_HIT", is_real=True)
    p2 = tp.open_position("s", interval=5, end_ts=9999999999,
                          side="UP", token_id="t2", entry_price=0.50,
                          target_price=0.80, stake_usdc=5, is_real=True)
    # forzar exit_ts posterior
    import trading_positions as _tp2
    with _tp2._LOCK:
        st = _tp2._load()
        for pl in st["real"].values():
            for pp in pl:
                if pp["id"] == p2["id"]:
                    pass
    tp.close_position("s", p2["id"], exit_price=0.10,
                       pnl_usdc=-5.0, exit_reason="STOP_LOSS", is_real=True)
    dd = tp.real_equity_drawdown()
    assert dd["peak"] == 10.0
    assert dd["current"] == 5.0
    assert abs(dd["drawdown_pct"] - 0.5) < 0.01


# ───────── 19B — paper-to-live gate ─────────

def test_paper_gate_fails_when_no_history(tmp_path):
    tp = _fresh_state(tmp_path)
    g = tp.phantom_gate_status(required_days=7.0, required_trades=200, required_wr=0.75)
    assert g["ok"] is False
    assert g["trades"] == 0
    assert len(g["reasons"]) >= 1


def test_paper_gate_passes_with_strong_stats(tmp_path):
    tp = _fresh_state(tmp_path)
    # crear 5 trades phantom, 4 wins → WR=80%, 8 días atrás
    old_ts = int(time.time()) - int(8 * 86400)
    for i in range(5):
        p = tp.open_position(f"s{i}", interval=5, end_ts=9999999999,
                             side="UP", token_id=f"t{i}", entry_price=0.50,
                             target_price=0.80, stake_usdc=5, is_real=False)
        # backdate entry_ts
        import trading_positions as _tp
        with _tp._LOCK:
            st = _tp._load()
            for pl in st["phantom"].values():
                for pp in pl:
                    if pp["id"] == p["id"]:
                        pp["entry_ts"] = old_ts
            _tp._save(st)
        pnl = 2.0 if i < 4 else -2.0
        tp.close_position(f"s{i}", p["id"], exit_price=0.80,
                           pnl_usdc=pnl, exit_reason="TARGET_HIT", is_real=False)
    g = tp.phantom_gate_status(required_days=7.0, required_trades=5, required_wr=0.75)
    assert g["ok"] is True, f"reasons={g['reasons']}"
    assert g["trades"] == 5


# ───────── 19C — stale price check ─────────

def test_stale_price_rejected():
    """evaluate_and_open debe saltar si market['price_ts'] es viejo."""
    import asyncio
    from config import BotParams
    from trading_runner import evaluate_and_open
    from strategy_trading import TradingParams
    bp = BotParams()
    bp.trading_max_price_age_sec = 10.0
    bp.trading_paper_gate_override = True  # no bloquear por gate
    market = {
        "slug": "test-stale",
        "interval_minutes": 5,
        "end_ts": 9999999999,
        "minutes_to_close": 3.0,
        "up_price": 0.5,
        "down_price": 0.5,
        "price_ts": time.time() - 30,  # 30s viejo
    }
    params = TradingParams()
    result = asyncio.run(evaluate_and_open(market, is_real=False, params=params, bot_params=bp))
    assert result is None


# ───────── 19D — params persistidos ─────────

def test_botparams_exposes_new_keys():
    from config import BotParams
    bp = BotParams()
    d = bp.to_dict()
    for k in ["trading_real_drawdown_halt_pct",
              "trading_paper_required_days", "trading_paper_required_trades",
              "trading_paper_required_wr", "trading_paper_gate_override",
              "trading_max_price_age_sec"]:
        assert k in d, f'missing {k} in to_dict'


def test_botparams_update_accepts_new_keys():
    from config import BotParams
    bp = BotParams()
    snapshot = bp.to_dict().copy()
    try:
        bp.update({
            "trading_real_drawdown_halt_pct": 0.35,
            "trading_paper_required_trades": 100,
            "trading_paper_gate_override": True,
            "trading_max_price_age_sec": 5.0,
        })
        assert bp.trading_real_drawdown_halt_pct == 0.35
        assert bp.trading_paper_required_trades == 100
        assert bp.trading_paper_gate_override is True
        assert bp.trading_max_price_age_sec == 5.0
    finally:
        bp.update(snapshot)


# ───────── 19E — _check_real_safety enforcement ─────────

def test_check_real_safety_blocks_on_paper_gate(tmp_path):
    tp = _fresh_state(tmp_path)
    from config import BotParams
    import trading_runner
    bp = BotParams()
    bp.trading_real_killed = False
    bp.trading_paper_gate_override = False
    bp.trading_paper_required_trades = 10
    ok, reason = trading_runner._check_real_safety(bp, prospective_stake=5.0)
    assert ok is False
    assert "paper-gate" in reason


def test_check_real_safety_blocks_on_drawdown(tmp_path):
    tp = _fresh_state(tmp_path)
    from config import BotParams
    import trading_runner
    bp = BotParams()
    bp.trading_real_killed = False
    bp.trading_paper_gate_override = True  # skip gate
    bp.trading_real_drawdown_halt_pct = 0.40
    # +10 → peak=10; -5 → current=5, dd=50%
    p1 = tp.open_position("z", interval=5, end_ts=9999999999,
                          side="UP", token_id="t1", entry_price=0.50,
                          target_price=0.80, stake_usdc=5, is_real=True)
    tp.close_position("z", p1["id"], exit_price=0.80,
                       pnl_usdc=10.0, exit_reason="TARGET_HIT", is_real=True)
    p2 = tp.open_position("z", interval=5, end_ts=9999999999,
                          side="UP", token_id="t2", entry_price=0.50,
                          target_price=0.80, stake_usdc=5, is_real=True)
    tp.close_position("z", p2["id"], exit_price=0.10,
                       pnl_usdc=-5.0, exit_reason="STOP_LOSS", is_real=True)
    ok, reason = trading_runner._check_real_safety(bp, prospective_stake=5.0)
    assert ok is False
    assert "drawdown" in reason.lower()
    # efecto secundario: kill-switch activado
    assert bp.trading_real_killed is True
