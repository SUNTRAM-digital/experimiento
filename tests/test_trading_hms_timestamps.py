"""Point 11 — HH:MM:SS timestamps on buy/sell records.
Validates open_position adds entry_hms/entry_iso, close_position adds exit_hms/exit_iso/duration_secs."""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_open_position_adds_hms_fields(tmp_path, monkeypatch):
    import trading_positions as tp
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"phantom": {}, "real": {}, "meta": {"phantom_balance": 1000.0}}))
    monkeypatch.setattr(tp, "_DATA_PATH", str(f), raising=False)

    pos = tp.open_position(
        slug="test-mkt", interval=5, end_ts=9999999999,
        side="UP", token_id="tkn",
        entry_price=0.30, target_price=0.50, stake_usdc=5.0,
        is_real=False,
    )
    assert "entry_hms" in pos
    assert "entry_iso" in pos
    assert "exit_hms" in pos and pos["exit_hms"] is None
    assert "exit_iso" in pos and pos["exit_iso"] is None
    # formato HH:MM:SS
    parts = pos["entry_hms"].split(":")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_close_position_sets_exit_hms(tmp_path, monkeypatch):
    import trading_positions as tp
    import time
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"phantom": {}, "real": {}, "meta": {"phantom_balance": 1000.0}}))
    monkeypatch.setattr(tp, "_DATA_PATH", str(f), raising=False)

    pos = tp.open_position(
        slug="test-mkt", interval=5, end_ts=9999999999,
        side="UP", token_id="tkn",
        entry_price=0.30, target_price=0.50, stake_usdc=5.0,
        is_real=False,
    )
    time.sleep(1)
    closed = tp.close_position(
        slug="test-mkt", position_id=pos["id"], exit_price=0.50,
        pnl_usdc=1.0, exit_reason="TARGET_HIT", is_real=False,
    )
    assert closed["exit_hms"] is not None
    assert closed["exit_iso"] is not None
    parts = closed["exit_hms"].split(":")
    assert len(parts) == 3
    assert "duration_secs" in closed
    assert closed["duration_secs"] >= 1


def test_hms_rendered_in_history_table():
    """thead should have Compra + Venta columns and tbody empty colspan=11."""
    root = os.path.join(os.path.dirname(__file__), '..')
    with open(os.path.join(root, 'static', 'index.html'), encoding='utf-8') as f:
        html = f.read()
    assert ">Compra <" in html
    assert ">Venta <" in html
    assert 'tbl-trading-hist"><tr><td colspan="11"' in html
    # render: entry_hms and exit_hms variables referenced
    assert "p.entry_hms" in html
    assert "p.exit_hms" in html
