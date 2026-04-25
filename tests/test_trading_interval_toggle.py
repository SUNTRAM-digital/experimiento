"""v9.5.5 — Trading mode: toggles por intervalo (5m/15m/1d) + R/R ajustado.

Fix: 5m WR=25% sangraba. Sin gate por intervalo no había forma de matar 5m
sin matar todo trading. Además R/R era 1.0 (offset=0.30 vs max_entry=0.30) →
break-even WR 50%; subimos offset a 0.40 → 1.33 → break-even 43%.
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_botparams_exposes_trading_interval_flags():
    from config import BotParams
    p = BotParams()
    assert hasattr(p, "trading_5m_enabled")
    assert hasattr(p, "trading_15m_enabled")
    assert hasattr(p, "trading_1d_enabled")
    # Defaults: 5m OFF (catastrófico), 15m ON, 1d OFF
    assert p.trading_5m_enabled  is False
    assert p.trading_15m_enabled is True
    assert p.trading_1d_enabled  is False


def test_botparams_to_dict_includes_trading_intervals():
    from config import BotParams
    d = BotParams().to_dict()
    assert "trading_5m_enabled"  in d
    assert "trading_15m_enabled" in d
    assert "trading_1d_enabled"  in d


def test_api_valid_keys_includes_trading_intervals():
    """update_params (Claude advisor) debe poder cambiar las 3 keys."""
    import api
    src = inspect.getsource(api)
    start = src.find("valid_keys = {")
    assert start != -1
    end = src.find("}", start)
    block = src[start:end]
    assert "trading_5m_enabled"  in block
    assert "trading_15m_enabled" in block
    assert "trading_1d_enabled"  in block


def test_api_trading_params_mapping_includes_intervals():
    """/api/trading/params POST debe aceptar las 3 keys como bool."""
    import api
    src = inspect.getsource(api.set_trading_params)
    assert '"trading_5m_enabled"'  in src
    assert '"trading_15m_enabled"' in src
    assert '"trading_1d_enabled"'  in src


def test_trading_state_includes_intervals_dict():
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    r = client.get("/api/trading/state").json()
    assert "intervals" in r
    iv = r["intervals"]
    assert "5m"  in iv
    assert "15m" in iv
    assert "1d"  in iv
    assert isinstance(iv["5m"], bool)


def test_trading_interval_toggle_endpoint_round_trip():
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    # Snapshot
    s0 = client.get("/api/trading/state").json()["intervals"]
    # Activar 5m
    r = client.post("/api/trading/interval_toggle", json={"interval": "5m", "enabled": True}).json()
    assert r.get("ok") is True
    s1 = client.get("/api/trading/state").json()["intervals"]
    assert s1["5m"] is True
    # Apagar de nuevo
    r = client.post("/api/trading/interval_toggle", json={"interval": "5m", "enabled": False}).json()
    assert r.get("ok") is True
    # Restaurar valores originales
    for iv in ("5m", "15m", "1d"):
        client.post("/api/trading/interval_toggle", json={"interval": iv, "enabled": s0[iv]})


def test_trading_interval_toggle_rejects_invalid():
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    r = client.post("/api/trading/interval_toggle", json={"interval": "30m", "enabled": True}).json()
    assert r.get("ok") is False
    assert "interval inválido" in r.get("error", "")


def test_scan_updown_has_trading_interval_gate():
    """bot._scan_updown debe seleccionar attr según interval_minutes y AND con trading_mode_enabled."""
    import bot
    src = inspect.getsource(bot._scan_updown)
    assert "trading_5m_enabled"  in src
    assert "trading_15m_enabled" in src
    assert "trading_1d_enabled"  in src
    assert "_trading_iv_attr"    in src


def test_trading_interval_gate_logic():
    """Lógica pura: solo dispara si trading_mode_enabled AND interval flag."""
    def gate(mode_on, interval_minutes, on_5m, on_15m, on_1d):
        attr = (
            "trading_5m_enabled"  if interval_minutes <= 5    else
            "trading_1d_enabled"  if interval_minutes >= 1440 else
            "trading_15m_enabled"
        )
        flags = {
            "trading_5m_enabled":  on_5m,
            "trading_15m_enabled": on_15m,
            "trading_1d_enabled":  on_1d,
        }
        return bool(mode_on) and bool(flags[attr])

    # 15m solamente
    assert gate(True, 5,    False, True, False) is False
    assert gate(True, 15,   False, True, False) is True
    assert gate(True, 1440, False, True, False) is False
    # mode off bloquea todo
    assert gate(False, 15, True, True, True) is False
    # 1d
    assert gate(True, 1440, False, False, True) is True
    # 5m
    assert gate(True, 5, True, False, False) is True


def test_rr_defaults_break_even_under_50_percent():
    """Con max_entry=0.30 y profit_offset=0.40 break-even WR debe ser <=45%."""
    from config import BotParams
    p = BotParams()
    max_entry = p.trading_max_entry_price
    offset    = p.trading_profit_offset
    # Worst-case loss == max_entry; ganancia = offset
    break_even_wr = max_entry / (max_entry + offset)
    assert break_even_wr <= 0.45, (
        f"R/R defaults dan break-even WR={break_even_wr:.2%} — "
        f"max_entry={max_entry}, offset={offset}. Debería ser <=45%."
    )
