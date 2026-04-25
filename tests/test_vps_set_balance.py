"""v9.5.3 — Set saldo virtual VPS manual + reset stats.

Endpoint POST /api/vps-experiment/set-balance acepta {balance: float}, borra
trades, daily_summaries y reinicia balances VPS+Fixed al valor pedido.
get_status() devuelve virtual_balance_initial = bal custom (no constante 50).
"""
import os
import sys
import json
import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _isolate_data_file(tmp_path, monkeypatch):
    """Apunta vps_experiment.DATA_FILE a un tmpfile + recarga el módulo."""
    import vps_experiment as ve
    importlib.reload(ve)
    fake = str(tmp_path / "vps.json")
    monkeypatch.setattr(ve, "DATA_FILE", fake)
    return ve


def test_reset_with_balance_creates_clean_state(tmp_path, monkeypatch):
    ve = _isolate_data_file(tmp_path, monkeypatch)
    ve.reset_with_balance(123.5)
    data = ve._load()
    assert data["meta"]["virtual_balance_vps"]   == 123.5
    assert data["meta"]["virtual_balance_fixed"] == 123.5
    assert data["meta"]["virtual_balance_initial_custom"] == 123.5
    assert data["config"]["virtual_balance_initial"] == 123.5
    assert data["trades"] == []
    assert data["daily_summaries"] == []


def test_reset_with_balance_zero_allowed(tmp_path, monkeypatch):
    ve = _isolate_data_file(tmp_path, monkeypatch)
    ve.reset_with_balance(0)
    data = ve._load()
    assert data["meta"]["virtual_balance_vps"] == 0


def test_reset_with_balance_rejects_negative(tmp_path, monkeypatch):
    ve = _isolate_data_file(tmp_path, monkeypatch)
    try:
        ve.reset_with_balance(-1)
        assert False, "debería haber lanzado ValueError"
    except ValueError:
        pass


def test_get_status_uses_custom_initial(tmp_path, monkeypatch):
    ve = _isolate_data_file(tmp_path, monkeypatch)
    ve.reset_with_balance(200)
    s = ve.get_status()
    assert s["virtual_balance_initial"] == 200
    assert s["virtual_balance_vps"]     == 200
    assert s["virtual_balance_fixed"]   == 200


def test_endpoint_set_balance_ok(tmp_path, monkeypatch):
    ve = _isolate_data_file(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    r = client.post("/api/vps-experiment/set-balance", json={"balance": 75.5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["balance"] == 75.5
    # Verificar persistido
    data = ve._load()
    assert data["meta"]["virtual_balance_vps"] == 75.5


def test_endpoint_set_balance_rejects_negative(tmp_path, monkeypatch):
    _isolate_data_file(tmp_path, monkeypatch)
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    r = client.post("/api/vps-experiment/set-balance", json={"balance": -10})
    body = r.json()
    assert body["ok"] is False
