"""
Tests for points 6+7 — manual phantom balance edit + exposure release.
"""
import os, sys, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _patch_state_file(monkeypatch, tmp_path, initial_state):
    """Redirect trading_positions to a tmp JSON file."""
    import trading_positions as tp
    f = tmp_path / "tp_state.json"
    f.write_text(json.dumps(initial_state))
    monkeypatch.setattr(tp, "_DATA_PATH", str(f), raising=False)
    if hasattr(tp, "_state_cache"):
        with tp._LOCK:
            tp._state_cache = None
    return f


class TestSetPhantomBalance:
    def test_set_balance_does_not_touch_positions(self, tmp_path, monkeypatch):
        import trading_positions as tp
        initial = {
            "phantom": {"slug-a": [{"status": "OPEN", "stake_usdc": 5.0, "interval": 5, "side": "UP", "entry_price": 0.5}]},
            "real": {},
            "meta": {"phantom_balance": 50.0, "realized_pnl_phantom": 0.0},
        }
        _patch_state_file(monkeypatch, tmp_path, initial)
        out = tp.set_phantom_balance(123.45)
        assert out == 123.45
        meta = tp.get_meta()
        assert meta["phantom_balance"] == 123.45
        # Posición sigue OPEN
        opens = tp.all_open_positions(is_real=False)
        assert len(opens) == 1
        assert opens[0]["status"] == "OPEN"

    def test_set_balance_rounds_to_2(self, tmp_path, monkeypatch):
        import trading_positions as tp
        _patch_state_file(monkeypatch, tmp_path, {"phantom": {}, "real": {}, "meta": {"phantom_balance": 50.0}})
        out = tp.set_phantom_balance(99.999)
        assert out == 100.0


class TestResetRealExposure:
    def test_releases_open_real_positions(self, tmp_path, monkeypatch):
        import trading_positions as tp
        initial = {
            "phantom": {},
            "real": {
                "slug-a": [
                    {"status": "OPEN", "stake_usdc": 5.0, "interval": 5, "side": "UP", "entry_price": 0.5},
                    {"status": "TARGET_HIT", "stake_usdc": 3.0, "pnl_usdc": 1.0, "interval": 5, "side": "DOWN", "entry_price": 0.4},
                ],
                "slug-b": [{"status": "OPEN", "stake_usdc": 7.0, "interval": 15, "side": "UP", "entry_price": 0.3}],
            },
            "meta": {"phantom_balance": 50.0},
        }
        _patch_state_file(monkeypatch, tmp_path, initial)
        assert tp.real_exposure_usdc() == 12.0
        out = tp.reset_real_exposure()
        assert out["released"] == 2
        assert tp.real_exposure_usdc() == 0.0
        # historial preservado (TARGET_HIT intacto)
        flat = tp.get_all_positions_flat(is_real=True)
        statuses = [p["status"] for p in flat]
        assert "TARGET_HIT" in statuses
        assert statuses.count("RELEASED") == 2
