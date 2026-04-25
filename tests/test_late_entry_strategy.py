"""v9.6.0 — Late-entry: BTC lead + CLOB flow + stakes dinámicos.

Estrategia: no predecir a T=0 (50/50 puro). Esperar T≥8min cuando BTC
ya mostró dirección vs price_to_beat. Usar matemática Browniana para
calcular confianza real de que el lead aguante hasta resolución.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from strategy_updown import calc_lead_confidence
from strategy_trading import TradingParams, evaluate_entry_verbose


# ── calc_lead_confidence ───────────────────────────────────────────────────────

def test_lead_neutral_before_8min():
    """Antes de 8min → NEUTRAL sin importar el lead."""
    r = calc_lead_confidence(btc_now=95200, price_to_beat=95000,
                             elapsed_minutes=5.0, minutes_remaining=10.0)
    assert r["direction"] == "NEUTRAL"
    assert r["confidence"] == 0.0


def test_lead_neutral_tiny_move():
    """Lead < 0.05% → NEUTRAL (ruido)."""
    r = calc_lead_confidence(btc_now=95010, price_to_beat=95000,
                             elapsed_minutes=10.0, minutes_remaining=5.0)
    assert r["direction"] == "NEUTRAL"
    assert abs(r["lead_pct"]) < 0.05


def test_lead_up_large_move():
    """BTC +0.30% arriba con 5min left → alta confianza UP."""
    r = calc_lead_confidence(btc_now=95285, price_to_beat=95000,
                             elapsed_minutes=10.0, minutes_remaining=5.0)
    assert r["direction"] == "UP"
    assert r["confidence"] >= 75.0
    assert r["lead_pct"] > 0


def test_lead_down_large_move():
    """BTC -0.25% abajo con 7min left → confianza DOWN."""
    r = calc_lead_confidence(btc_now=94762, price_to_beat=95000,
                             elapsed_minutes=8.0, minutes_remaining=7.0)
    assert r["direction"] == "DOWN"
    assert r["confidence"] >= 60.0


def test_lead_confidence_increases_less_time():
    """Mismo lead, menos tiempo → más confianza (más difícil revertir)."""
    base = dict(btc_now=95190, price_to_beat=95000, elapsed_minutes=9.0)
    r10 = calc_lead_confidence(**base, minutes_remaining=10.0)
    r3  = calc_lead_confidence(**base, minutes_remaining=3.0)
    assert r3["confidence"] > r10["confidence"]


def test_lead_confidence_increases_bigger_lead():
    """Mismo tiempo, mayor lead → más confianza."""
    base = dict(price_to_beat=95000, elapsed_minutes=9.0, minutes_remaining=5.0)
    r_small = calc_lead_confidence(btc_now=95100, **base)  # 0.105%
    r_large = calc_lead_confidence(btc_now=95400, **base)  # 0.42%
    assert r_large["confidence"] > r_small["confidence"]


# ── evaluate_entry_verbose — elapsed gate ─────────────────────────────────────

def _params(**kw):
    p = TradingParams(
        buy_probable=True, probable_min_price=0.45, probable_max_price=0.85,
        probable_profit_offset=0.45, min_entry_minutes_left=1.0,
        max_entries_per_market=8, max_open_per_side=1, one_open_at_a_time=False,
        min_elapsed_for_entry=8.0, stake_usdc=3.0,
        stake_tier_60=5.0, stake_tier_70=10.0, stake_tier_80=15.0, stake_tier_90=20.0,
    )
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _mkt(up=0.495, down=0.505, elapsed=10.0, minutes=5.0,
         signal_dir="DOWN", signal_conf=75.0, lead_dir="DOWN", clob=None):
    m = {
        "slug": "test-market",
        "up_price": up, "down_price": down,
        "up_token": "tok_up", "down_token": "tok_dn",
        "minutes_to_close": minutes,
        "elapsed_minutes": elapsed,
        "lead_direction": lead_dir,
    }
    if signal_dir:
        m["signal_direction"]  = signal_dir
        m["signal_confidence"] = signal_conf
    if clob:
        m["clob_flow"] = clob
    return m


def test_elapsed_gate_blocks_early_entry():
    """elapsed=5min < 8min → bloqueado por gate de elapsed."""
    m = _mkt(elapsed=5.0)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is None
    assert "elapsed" in reason


def test_elapsed_gate_allows_late_entry():
    """elapsed=10min ≥ 8min → entrada permitida."""
    m = _mkt(elapsed=10.0)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None, f"debería permitir entrada: {reason}"


# ── Stakes dinámicos ──────────────────────────────────────────────────────────

def test_stake_scales_with_confidence():
    """Confianza alta → stake mayor."""
    m60 = _mkt(signal_conf=62.0)
    m80 = _mkt(signal_conf=82.0)
    m90 = _mkt(signal_conf=92.0)
    p = _params()
    s60, _ = evaluate_entry_verbose(m60, [], p)
    s80, _ = evaluate_entry_verbose(m80, [], p)
    s90, _ = evaluate_entry_verbose(m90, [], p)
    assert s60 is not None and s80 is not None and s90 is not None
    assert s60.stake_usdc == 5.0   # tier_60
    assert s80.stake_usdc == 15.0  # tier_80
    assert s90.stake_usdc == 20.0  # tier_90


def test_stake_base_below_60():
    """Confianza < 60% → stake base ($3)."""
    m = _mkt(signal_conf=52.0)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None, reason
    assert sig.stake_usdc == 3.0


# ── CLOB flow adjustment ──────────────────────────────────────────────────────

def test_clob_confirms_boosts_conf():
    """CLOB confirma dirección → stake sube (confianza ajustada al alza)."""
    clob_confirms = {"available": True, "direction": "DOWN", "strength": 0.8,
                     "up_vol": 20, "down_vol": 60, "flow_ratio": 0.25}
    # signal_conf=68% (tier_60=$5), CLOB boost → debería subir a tier_70=$10
    m = _mkt(signal_conf=68.0, clob=clob_confirms)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    assert sig is not None
    assert sig.stake_usdc >= 10.0, f"CLOB debería boost a tier_70+, got {sig.stake_usdc}"


def test_clob_contradicts_reduces_conf():
    """CLOB contradice → confianza baja → stake menor."""
    clob_contra = {"available": True, "direction": "UP", "strength": 0.9,
                   "up_vol": 80, "down_vol": 10, "flow_ratio": 0.89}
    # signal_conf=82% (tier_80=$15), fuerte contradicción → debería bajar a tier_70 o menos
    m = _mkt(signal_conf=82.0, clob=clob_contra)
    sig, reason = evaluate_entry_verbose(m, [], _params())
    # Podría ser None (si conf cae a 0) o stake reducido
    if sig is not None:
        assert sig.stake_usdc < 15.0, f"CLOB fuerte contradicción debería reducir stake"


def test_config_has_new_params():
    """BotParams expone todos los params de v9.6.0."""
    from config import BotParams
    bp = BotParams()
    assert bp.trading_min_elapsed_for_entry == 8.0
    assert bp.trading_stake_tier_60  == 5.0
    assert bp.trading_stake_tier_70  == 10.0
    assert bp.trading_stake_tier_80  == 15.0
    assert bp.trading_stake_tier_90  == 20.0


def test_calc_lead_exported_from_strategy_updown():
    """calc_lead_confidence importable desde strategy_updown."""
    from strategy_updown import calc_lead_confidence
    r = calc_lead_confidence(95500, 95000, 10.0, 5.0)
    assert "direction" in r
    assert "confidence" in r
    assert "lead_pct" in r
