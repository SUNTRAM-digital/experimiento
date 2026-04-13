"""
Tests for VPS phantom pending-trade recovery:
  - get_pending_for_restore()
  - get_stale_pending()
  - resolve_phantom_vps() (verify the trade resolves correctly)
"""
import json
import os
import sys
import time
import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fresh_vps(tmp_path, trades=None):
    """Return a fresh vps_experiment module backed by a temp DATA_FILE."""
    if "vps_experiment" in sys.modules:
        del sys.modules["vps_experiment"]
    import vps_experiment as ve
    data_file = str(tmp_path / "vps_phantom_experiment.json")
    ve.DATA_FILE = data_file
    # Bootstrap the file with minimal structure
    now_ts = int(time.time())
    data = {
        "meta": {
            "status": "RUNNING",
            "start_date": "2026-01-01T00:00:00Z",
            "end_target": "2099-01-01T00:00:00Z",
            "virtual_balance_vps": 50.0,
            "virtual_balance_fixed": 50.0,
        },
        "trades": trades or [],
    }
    with open(data_file, "w") as f:
        json.dump(data, f)
    return ve


def _make_pending_trade(slug="t1", market="updown_5m", end_ts_offset=-300,
                        signal="UP", btc_start=80000.0, conf=55.0):
    """Create a PENDING trade dict. end_ts_offset=seconds from now."""
    now_ts = int(time.time())
    return {
        "trade_id": 1,
        "slug": slug,
        "timestamp": "2026-01-01T00:00:00Z",
        "market": market,
        "signal": signal,
        "confidence_pct": conf,
        "confidence_tier": "high",
        "entry_price": 0.50,
        "position_size_vps": 6.0,
        "position_size_fixed": 3.0,
        "ta_scores": {},
        "btc_start_price": btc_start,
        "end_ts": now_ts + end_ts_offset,
        "result": "PENDING",
        "result_timestamp": None,
        "btc_end_price": None,
        "pnl_vps": None,
        "pnl_fixed": None,
        "pnl_difference": None,
    }


# ── get_pending_for_restore ────────────────────────────────────────────────────

class TestGetPendingForRestore:
    def test_returns_recent_pending_trades(self, tmp_path):
        trade = _make_pending_trade(end_ts_offset=-60)  # 1 min ago → recent
        ve = _fresh_vps(tmp_path, [trade])
        result = ve.get_pending_for_restore()
        assert "t1" in result

    def test_excludes_resolved_trades(self, tmp_path):
        trade = _make_pending_trade()
        trade["result"] = "WIN"
        ve = _fresh_vps(tmp_path, [trade])
        assert ve.get_pending_for_restore() == {}

    def test_excludes_very_old_pending(self, tmp_path):
        trade = _make_pending_trade(end_ts_offset=-10000)  # >2h ago
        ve = _fresh_vps(tmp_path, [trade])
        assert ve.get_pending_for_restore() == {}

    def test_correct_interval_5m(self, tmp_path):
        trade = _make_pending_trade(market="updown_5m", end_ts_offset=-60)
        ve = _fresh_vps(tmp_path, [trade])
        result = ve.get_pending_for_restore()
        assert result["t1"]["interval"] == 5

    def test_correct_interval_15m(self, tmp_path):
        trade = _make_pending_trade(slug="t2", market="updown_15m", end_ts_offset=-60)
        ve = _fresh_vps(tmp_path, [trade])
        result = ve.get_pending_for_restore()
        assert result["t2"]["interval"] == 15

    def test_preserves_side_and_btc_start(self, tmp_path):
        trade = _make_pending_trade(signal="DOWN", btc_start=75000.0, end_ts_offset=-60)
        ve = _fresh_vps(tmp_path, [trade])
        entry = ve.get_pending_for_restore()["t1"]
        assert entry["side"] == "DOWN"
        assert entry["btc_start"] == 75000.0

    def test_empty_when_no_pending(self, tmp_path):
        ve = _fresh_vps(tmp_path, [])
        assert ve.get_pending_for_restore() == {}


# ── get_stale_pending ─────────────────────────────────────────────────────────

class TestGetStalePending:
    def test_returns_old_pending(self, tmp_path):
        trade = _make_pending_trade(end_ts_offset=-10000)  # >2h ago
        ve = _fresh_vps(tmp_path, [trade])
        result = ve.get_stale_pending()
        assert len(result) == 1
        assert result[0]["slug"] == "t1"

    def test_excludes_recent_pending(self, tmp_path):
        trade = _make_pending_trade(end_ts_offset=-60)   # recent
        ve = _fresh_vps(tmp_path, [trade])
        assert ve.get_stale_pending() == []

    def test_excludes_resolved(self, tmp_path):
        trade = _make_pending_trade(end_ts_offset=-10000)
        trade["result"] = "LOSS"
        ve = _fresh_vps(tmp_path, [trade])
        assert ve.get_stale_pending() == []

    def test_multiple_stale_all_returned(self, tmp_path):
        trades = [
            _make_pending_trade(slug="a", end_ts_offset=-10000),
            _make_pending_trade(slug="b", end_ts_offset=-20000),
            _make_pending_trade(slug="c", end_ts_offset=-60),   # recent
        ]
        ve = _fresh_vps(tmp_path, trades)
        stale = ve.get_stale_pending()
        slugs = {t["slug"] for t in stale}
        assert "a" in slugs
        assert "b" in slugs
        assert "c" not in slugs


# ── resolve_phantom_vps resolves stale correctly ──────────────────────────────

class TestResolveStale:
    def test_resolve_stale_win(self, tmp_path):
        trade = _make_pending_trade(slug="stale1", signal="UP",
                                     btc_start=80000.0, end_ts_offset=-10000)
        ve = _fresh_vps(tmp_path, [trade])
        # BTC went up → WIN
        ve.resolve_phantom_vps(slug="stale1", btc_end=81000.0, won=True)
        # Reload and verify
        with open(ve.DATA_FILE) as f:
            data = json.load(f)
        resolved = data["trades"][0]
        assert resolved["result"] == "WIN"
        assert resolved["pnl_vps"] == pytest.approx(6.0 * 0.98, abs=0.01)
        assert resolved["pnl_fixed"] == pytest.approx(3.0 * 0.98, abs=0.01)

    def test_resolve_stale_loss(self, tmp_path):
        trade = _make_pending_trade(slug="stale2", signal="UP",
                                     btc_start=80000.0, end_ts_offset=-10000)
        ve = _fresh_vps(tmp_path, [trade])
        ve.resolve_phantom_vps(slug="stale2", btc_end=79000.0, won=False)
        with open(ve.DATA_FILE) as f:
            data = json.load(f)
        resolved = data["trades"][0]
        assert resolved["result"] == "LOSS"
        assert resolved["pnl_vps"] == pytest.approx(-6.0, abs=0.01)

    def test_resolve_updates_virtual_balance(self, tmp_path):
        trade = _make_pending_trade(slug="stale3", signal="UP",
                                     end_ts_offset=-10000)
        ve = _fresh_vps(tmp_path, [trade])
        ve.resolve_phantom_vps(slug="stale3", btc_end=81000.0, won=True)
        with open(ve.DATA_FILE) as f:
            data = json.load(f)
        expected_vps   = 50.0 + 6.0 * 0.98
        expected_fixed = 50.0 + 3.0 * 0.98
        assert data["meta"]["virtual_balance_vps"]   == pytest.approx(expected_vps,   abs=0.01)
        assert data["meta"]["virtual_balance_fixed"] == pytest.approx(expected_fixed, abs=0.01)

    def test_double_resolve_is_noop(self, tmp_path):
        """Resolving an already-resolved trade should not change state."""
        trade = _make_pending_trade(slug="stale4", end_ts_offset=-10000)
        ve = _fresh_vps(tmp_path, [trade])
        ve.resolve_phantom_vps(slug="stale4", btc_end=81000.0, won=True)
        # Second call with opposite result — should be ignored
        ve.resolve_phantom_vps(slug="stale4", btc_end=70000.0, won=False)
        with open(ve.DATA_FILE) as f:
            data = json.load(f)
        assert data["trades"][0]["result"] == "WIN"
