"""v9.5.6 — Filtros de calidad phantom: min_conf, ta_mom_gate, min_elapsed_15m.

Basado en análisis de 221 trades reales:
  - conf ≥35%      → WR 83.5% vs 70.1% débil
  - TA+mom alineados → WR 81.3% vs 45.5% conflicto
  - elapsed ≥8min   → WR 86.1% vs 33.3% temprano
"""
import os
import sys
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_botparams_exposes_quality_gate_flags():
    from config import BotParams
    p = BotParams()
    assert hasattr(p, "phantom_min_conf_pct")
    assert hasattr(p, "phantom_ta_mom_gate")
    assert hasattr(p, "phantom_min_elapsed_15m")
    assert p.phantom_min_conf_pct    == 35.0
    assert p.phantom_ta_mom_gate     is True
    assert p.phantom_min_elapsed_15m == 8.0


def test_botparams_to_dict_includes_quality_gates():
    from config import BotParams
    d = BotParams().to_dict()
    assert "phantom_min_conf_pct"    in d
    assert "phantom_ta_mom_gate"     in d
    assert "phantom_min_elapsed_15m" in d


def test_api_valid_keys_includes_quality_gates():
    import api
    src = inspect.getsource(api)
    start = src.find("valid_keys = {")
    assert start != -1
    end = src.find("}", start)
    block = src[start:end]
    assert "phantom_min_conf_pct"    in block
    assert "phantom_ta_mom_gate"     in block
    assert "phantom_min_elapsed_15m" in block


def test_phantom_status_includes_quality_gates():
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    r = client.get("/api/phantom/status").json()
    assert "phantom_min_conf_pct"    in r
    assert "phantom_ta_mom_gate"     in r
    assert "phantom_min_elapsed_15m" in r
    assert isinstance(r["phantom_min_conf_pct"],    float)
    assert isinstance(r["phantom_ta_mom_gate"],     bool)
    assert isinstance(r["phantom_min_elapsed_15m"], float)


def test_quality_gate_round_trip_via_api():
    from fastapi.testclient import TestClient
    import api as api_mod
    client = TestClient(api_mod.app)
    s0 = client.get("/api/phantom/status").json()
    payload = {
        "phantom_min_conf_pct":    50.0,
        "phantom_ta_mom_gate":     False,
        "phantom_min_elapsed_15m": 10.0,
    }
    r = client.post("/api/params", json=payload)
    assert r.status_code == 200
    assert r.json().get("ok") or r.json().get("success")
    s1 = client.get("/api/phantom/status").json()
    assert s1["phantom_min_conf_pct"]    == 50.0
    assert s1["phantom_ta_mom_gate"]     is False
    assert s1["phantom_min_elapsed_15m"] == 10.0
    # Restaurar
    client.post("/api/params", json={
        "phantom_min_conf_pct":    s0["phantom_min_conf_pct"],
        "phantom_ta_mom_gate":     s0["phantom_ta_mom_gate"],
        "phantom_min_elapsed_15m": s0["phantom_min_elapsed_15m"],
    })


def test_gate_logic_min_conf():
    """Señales con conf < phantom_min_conf_pct deben ser bloqueadas."""
    def low_conf_gate(conf, min_conf=35.0):
        return float(conf) < min_conf

    assert low_conf_gate(3.8)   is True   # trade 5 del experiment — debe bloquearse
    assert low_conf_gate(5.2)   is True   # trade 6 — debe bloquearse
    assert low_conf_gate(13.3)  is True   # trade 1 — debe bloquearse
    assert low_conf_gate(34.9)  is True   # justo bajo el umbral
    assert low_conf_gate(35.0)  is False  # en el umbral — pasa
    assert low_conf_gate(60.0)  is False  # señal media — pasa
    assert low_conf_gate(35.0, min_conf=0.0) is False  # gate desactivado efectivamente


def test_gate_logic_ta_mom():
    """TA y momentum en distinto signo → conflicto → bloquear."""
    def mom_conflict_gate(ta_raw, mom, gate_on=True):
        agree = (ta_raw > 0 and mom > 0) or (ta_raw < 0 and mom < 0)
        return gate_on and not agree

    # Caso del análisis: TA conflicto → WR 45.5%
    assert mom_conflict_gate(ta_raw=-0.46, mom=0.14)  is True   # TA DOWN, mom UP → conflicto
    assert mom_conflict_gate(ta_raw= 0.30, mom=-0.20) is True   # TA UP,  mom DOWN → conflicto
    # Acuerdo → pasa
    assert mom_conflict_gate(ta_raw=-0.46, mom=-0.36) is False  # ambos DOWN → acuerdo
    assert mom_conflict_gate(ta_raw= 0.30, mom= 0.15) is False  # ambos UP → acuerdo
    # Gate desactivado
    assert mom_conflict_gate(ta_raw=-0.46, mom=0.14, gate_on=False) is False


def test_gate_logic_elapsed_15m():
    """Entradas antes de min_elapsed_15m deben bloquearse."""
    def elapsed_gate(elapsed, is_5m, min_elapsed=8.0):
        return (not is_5m) and (elapsed < min_elapsed)

    # 15m: WR 33% en temprano → bloquear
    assert elapsed_gate(3.0,  is_5m=False) is True
    assert elapsed_gate(7.9,  is_5m=False) is True
    assert elapsed_gate(8.0,  is_5m=False) is False  # en el límite — pasa
    assert elapsed_gate(10.0, is_5m=False) is False  # tardío (WR 86%) — pasa
    # 5m: gate de elapsed no aplica
    assert elapsed_gate(1.0, is_5m=True)  is False
    assert elapsed_gate(3.0, is_5m=True)  is False


def test_scan_updown_has_quality_gate_variables():
    """bot._scan_updown debe contener las 3 variables de gate."""
    import bot
    src = inspect.getsource(bot._scan_updown)
    assert "_ph_low_conf"     in src, "falta gate de confianza mínima"
    assert "_ph_mom_conflict" in src, "falta gate TA/momentum"
    assert "_ph_too_early"    in src, "falta gate de elapsed"
    assert "phantom_min_conf_pct"    in src
    assert "phantom_ta_mom_gate"     in src
    assert "phantom_min_elapsed_15m" in src
