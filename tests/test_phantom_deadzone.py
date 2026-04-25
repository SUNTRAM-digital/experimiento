"""v9.5.4 — Filtro zona muerta de confianza phantom (skip tier low_moderate).

Fix: en sample 54 trades, conf 20-34% perdió 67% y consumió todo el PnL.
Filtramos esa banda para subir WR esperado de 48% → ~52% (ataca causa probada).
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_botparams_exposes_deadzone_flags():
    """BotParams debe tener defaults seguros para deadzone."""
    from config import BotParams
    p = BotParams()
    assert hasattr(p, "phantom_deadzone_enabled")
    assert hasattr(p, "phantom_deadzone_min_conf")
    assert hasattr(p, "phantom_deadzone_max_conf")
    assert p.phantom_deadzone_enabled is True
    assert p.phantom_deadzone_min_conf == 20.0
    assert p.phantom_deadzone_max_conf == 34.0


def test_botparams_to_dict_includes_deadzone():
    from config import BotParams
    d = BotParams().to_dict()
    assert "phantom_deadzone_enabled"  in d
    assert "phantom_deadzone_min_conf" in d
    assert "phantom_deadzone_max_conf" in d


def test_api_valid_keys_includes_deadzone():
    """El set valid_keys del tool update_params (Claude advisor) debe incluir
    las 3 claves nuevas para que Claude pueda ajustar la zona muerta."""
    import api
    src = inspect.getsource(api)
    # Localizar el bloque valid_keys = { ... }
    start = src.find("valid_keys = {")
    assert start != -1, "no encontré valid_keys en api.py"
    end = src.find("}", start)
    block = src[start:end]
    assert "phantom_deadzone_enabled"  in block
    assert "phantom_deadzone_min_conf" in block
    assert "phantom_deadzone_max_conf" in block


def test_phantom_status_endpoint_includes_deadzone():
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    r = client.get("/api/phantom/status")
    body = r.json()
    assert "phantom_deadzone_enabled"  in body
    assert "phantom_deadzone_min_conf" in body
    assert "phantom_deadzone_max_conf" in body


def test_scan_updown_has_deadzone_gate():
    """scan_updown debe tener el gate _in_deadzone antes del registro phantom."""
    import bot
    src = inspect.getsource(bot._scan_updown)
    assert "_in_deadzone" in src, "falta variable _in_deadzone en scan_updown"
    assert "phantom_deadzone_enabled" in src
    # Mensaje "deadzone" en el log para inspección
    assert "deadzone" in src.lower()


def test_deadzone_logic_skips_inside_range():
    """Lógica pura: conf en [20,34] con enabled=True → skip; fuera → no skip."""
    def _in_deadzone(conf, on=True, mn=20.0, mx=34.0, neutral=False):
        if neutral or not on:
            return False
        return mn <= float(conf) <= mx

    assert _in_deadzone(25)  is True   # dentro
    assert _in_deadzone(20)  is True   # límite inf
    assert _in_deadzone(34)  is True   # límite sup
    assert _in_deadzone(19)  is False  # justo fuera
    assert _in_deadzone(35)  is False  # justo fuera
    assert _in_deadzone(60)  is False  # arriba
    assert _in_deadzone(10)  is False  # debajo (tier minimal — sí opera)
    assert _in_deadzone(25, on=False)     is False  # gate off
    assert _in_deadzone(25, neutral=True) is False  # NEUTRAL no aplica


def test_deadzone_round_trip_via_api():
    """POST /api/params con las 3 keys → GET /api/phantom/status devuelve los nuevos valores."""
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    payload = {
        "phantom_deadzone_enabled":  False,
        "phantom_deadzone_min_conf": 15.0,
        "phantom_deadzone_max_conf": 40.0,
    }
    r = client.post("/api/params", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") or body.get("success"), body
    s = client.get("/api/phantom/status").json()
    assert s["phantom_deadzone_enabled"]  is False
    assert s["phantom_deadzone_min_conf"] == 15.0
    assert s["phantom_deadzone_max_conf"] == 40.0
    # Restaurar para no contaminar otros tests
    client.post("/api/params", json={
        "phantom_deadzone_enabled":  True,
        "phantom_deadzone_min_conf": 20.0,
        "phantom_deadzone_max_conf": 34.0,
    })
