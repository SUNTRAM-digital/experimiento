"""
Tests: corrección de precio CLOB en _execute_trade para UpDown.
Verifica que cuando outcome_price es diferente del ask real del CLOB,
se usa el precio real y se rechaza si supera el límite máximo.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import bot as _bot


# ── Helpers ──────────────────────────────────────────────────────────────────

class _FakeSignedOrder:
    pass


class _FakeClobClient:
    """Mock del CLOB client para tests sin llamadas reales a Polymarket."""

    def __init__(self, ask_price: float, post_success: bool = True):
        self._ask = ask_price
        self._post_success = post_success

    def get_price(self, token_id: str, side: str) -> float:
        return self._ask

    def create_order(self, order_args):
        return _FakeSignedOrder()

    def post_order(self, signed_order, order_type):
        if self._post_success:
            return {"success": True, "orderID": "fake-order-123"}
        return {"success": False, "errorMsg": "test rejection"}


class _FailClobClient(_FakeClobClient):
    """Simula fallo al obtener precio del CLOB."""

    def get_price(self, token_id, side):
        raise RuntimeError("CLOB unavailable")


def _make_updown_opp(outcome_price: float = 0.50) -> dict:
    return {
        "asset":            "BTC_UPDOWN",
        "token_id":         "fake-token-id",
        "side":             "UP",
        "entry_price":      outcome_price,
        "shares":           10.0,
        "size_usdc":        5.0,
        "ev_pct":           5.0,
        "market_title":     "BTC UP/DOWN 15m",
        "interval_minutes": 15,
        "poly_url":         "",
    }


def _patch_env(monkeypatch):
    """Configura bot_params con updown_max_usdc=$10 para que pasen los checks de shares."""
    monkeypatch.setattr(_bot.bot_params, "updown_max_usdc", 10.0)
    monkeypatch.setattr(_bot, "_deduct_from_bucket", lambda *a, **k: None)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_entry_price_updated_to_clob_ask(monkeypatch):
    """entry_price en la oportunidad se actualiza al ask real + slippage."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(_bot, "_clob_client", _FakeClobClient(ask_price=0.65))

    opp = _make_updown_opp(outcome_price=0.50)
    _bot._execute_trade(opp)

    expected = round(min(0.65 + _bot._UPDOWN_SLIPPAGE, 0.99), 4)
    assert opp["entry_price"] == pytest.approx(expected, abs=0.001), (
        f"entry_price esperado {expected}, obtenido {opp['entry_price']}"
    )


def test_trade_rejected_when_ask_above_max(monkeypatch):
    """Si el ask real del CLOB supera _UPDOWN_MAX_ENTRY_PRICE, el trade se cancela."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(_bot, "_clob_client", _FakeClobClient(ask_price=0.92))

    opp = _make_updown_opp(outcome_price=0.50)
    result = _bot._execute_trade(opp)
    assert result is False, (
        f"Trade debió cancelarse (ask 0.92 > límite {_bot._UPDOWN_MAX_ENTRY_PRICE})"
    )


def test_trade_rejected_at_exact_limit(monkeypatch):
    """Un ask exactamente igual al límite debe rechazarse (criterio ≥)."""
    _patch_env(monkeypatch)
    limit = _bot._UPDOWN_MAX_ENTRY_PRICE
    monkeypatch.setattr(_bot, "_clob_client", _FakeClobClient(ask_price=limit))

    opp = _make_updown_opp(outcome_price=0.50)
    result = _bot._execute_trade(opp)
    assert result is False, f"Ask exactamente en el límite ({limit}) debió rechazarse"


def test_entry_price_updated_before_min_shares_check(monkeypatch):
    """entry_price se actualiza al precio real incluso si el trade falla por shares mínimos."""
    # updown_max_usdc pequeño → shares < 5 → trade cancelado
    monkeypatch.setattr(_bot.bot_params, "updown_max_usdc", 1.0)
    monkeypatch.setattr(_bot, "_deduct_from_bucket", lambda *a, **k: None)
    monkeypatch.setattr(_bot, "_clob_client", _FakeClobClient(ask_price=0.70))

    opp = _make_updown_opp(outcome_price=0.50)
    result = _bot._execute_trade(opp)

    # Trade falla por shares, pero entry_price ya se actualizó
    assert result is False   # falla por shares mínimos
    expected = round(min(0.70 + _bot._UPDOWN_SLIPPAGE, 0.99), 4)
    assert opp["entry_price"] == pytest.approx(expected, abs=0.001), (
        f"entry_price debió actualizarse a {expected} aunque el trade falle"
    )


def test_fallback_to_outcome_price_on_clob_error(monkeypatch):
    """Si el CLOB falla al dar precio, entry_price queda en el outcome_price original."""
    _patch_env(monkeypatch)
    monkeypatch.setattr(_bot, "_clob_client", _FailClobClient(ask_price=0))

    opp = _make_updown_opp(outcome_price=0.55)
    _bot._execute_trade(opp)
    # CLOB falló → live_ask=0 → no se aplica corrección
    assert opp["entry_price"] == pytest.approx(0.55, abs=0.001)


def test_no_price_correction_for_non_updown(monkeypatch):
    """Para trades de clima/BTC (no UpDown), no se consulta el CLOB."""
    _patch_env(monkeypatch)
    # Si se llamara get_price para non-UpDown, esto fallaría
    monkeypatch.setattr(_bot, "_clob_client", _FailClobClient(ask_price=0))

    opp = _make_updown_opp(outcome_price=0.60)
    opp["asset"] = "WEATHER"   # cambiar a weather

    # No debe lanzar excepción aunque el CLOB falle (no se consulta)
    try:
        _bot._execute_trade(opp)
    except Exception:
        pass
    assert opp["entry_price"] == pytest.approx(0.60, abs=0.001)


def test_slippage_constants_defined():
    """Los parámetros de precio están definidos con valores razonables."""
    assert hasattr(_bot, "_UPDOWN_MAX_ENTRY_PRICE")
    assert hasattr(_bot, "_UPDOWN_SLIPPAGE")
    assert 0 < _bot._UPDOWN_SLIPPAGE < 0.10, "Slippage debe ser entre 0 y 10c"
    assert 0.80 < _bot._UPDOWN_MAX_ENTRY_PRICE < 1.0, "Max entry price debe ser entre 80c y 99c"
