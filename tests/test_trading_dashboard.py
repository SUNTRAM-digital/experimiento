"""Point 9 — /api/trading/dashboard endpoint shape + aggregates."""
import os, sys, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_dashboard_returns_required_keys():
    from api import get_trading_dashboard
    d = asyncio.run(get_trading_dashboard())
    assert "error" not in d, f"unexpected error: {d.get('error')}"
    for k in ("phantom", "real", "params"):
        assert k in d, f"missing top-level key {k}"


def test_phantom_section_has_expected_fields():
    from api import get_trading_dashboard
    d = asyncio.run(get_trading_dashboard())
    ph = d["phantom"]
    for k in ("balance", "today", "all_time", "open", "by_tf"):
        assert k in ph, f"phantom missing {k}"
    for k in ("trades", "wins", "losses", "win_rate", "pnl_usdc"):
        assert k in ph["today"], f"phantom.today missing {k}"
    for tf in ("5m", "15m", "1d"):
        assert tf in ph["by_tf"], f"phantom.by_tf missing {tf}"


def test_real_section_has_expected_fields():
    from api import get_trading_dashboard
    d = asyncio.run(get_trading_dashboard())
    rl = d["real"]
    for k in ("exposure", "today", "all_time", "open", "consec_losses", "pending_redeem"):
        assert k in rl, f"real missing {k}"


def test_params_flags_present():
    from api import get_trading_dashboard
    d = asyncio.run(get_trading_dashboard())
    pp = d["params"]
    for k in ("trading_mode_enabled", "trading_real_enabled", "killed"):
        assert k in pp
        assert isinstance(pp[k], bool)
