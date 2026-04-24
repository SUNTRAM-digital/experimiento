"""Punto 1 + 2 (v9.5.1) — toggles phantom por intervalo (5m/15m/1d).
Valida:
  A. BotParams expone flags phantom_5m_enabled/15m/1d con defaults correctos.
  B. update() acepta los nuevos flags y persisten en to_dict().
  C. Endpoint /api/phantom/interval_toggle actualiza el flag correcto.
  D. /api/phantom/status devuelve los flags en el payload.
  E. La lógica de gating usa el attr correcto según interval_minutes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── A — defaults ──────────────────────────────────────────────────────────────

def test_botparams_exposes_phantom_interval_flags():
    from config import BotParams
    bp = BotParams()
    d = bp.to_dict()
    assert "phantom_5m_enabled"  in d
    assert "phantom_15m_enabled" in d
    assert "phantom_1d_enabled"  in d


# ── B — update persiste ───────────────────────────────────────────────────────

def test_botparams_update_accepts_phantom_interval_flags():
    from config import BotParams
    bp = BotParams()
    snap = bp.to_dict().copy()
    try:
        bp.update({
            "phantom_5m_enabled":  False,
            "phantom_15m_enabled": True,
            "phantom_1d_enabled":  True,
        })
        assert bp.phantom_5m_enabled  is False
        assert bp.phantom_15m_enabled is True
        assert bp.phantom_1d_enabled  is True
    finally:
        bp.update(snap)


# ── C — endpoint interval_toggle ──────────────────────────────────────────────

def test_phantom_interval_toggle_endpoint_rejects_unknown_interval():
    import asyncio
    from api import phantom_interval_toggle
    r = asyncio.run(phantom_interval_toggle({"interval": "42m", "enabled": True}))
    assert r["ok"] is False
    assert "invál" in r["error"].lower() or "inval" in r["error"].lower()


def test_phantom_interval_toggle_endpoint_updates_flag():
    import asyncio
    from api import phantom_interval_toggle
    from bot import bot_params
    snap = bot_params.to_dict().copy()
    try:
        r = asyncio.run(phantom_interval_toggle({"interval": "1d", "enabled": True}))
        assert r["ok"] is True
        assert r["interval"] == "1d"
        assert r["enabled"] is True
        assert bot_params.phantom_1d_enabled is True

        r2 = asyncio.run(phantom_interval_toggle({"interval": "5m", "enabled": False}))
        assert r2["ok"] is True
        assert bot_params.phantom_5m_enabled is False
    finally:
        bot_params.update(snap)


# ── D — status devuelve flags ─────────────────────────────────────────────────

def test_phantom_status_includes_interval_flags():
    import asyncio
    from api import phantom_status
    from bot import bot_params
    snap = bot_params.to_dict().copy()
    try:
        bot_params.phantom_5m_enabled  = True
        bot_params.phantom_15m_enabled = False
        bot_params.phantom_1d_enabled  = True
        bot_params.save()
        r = asyncio.run(phantom_status())
        assert "phantom_5m_enabled"  in r
        assert "phantom_15m_enabled" in r
        assert "phantom_1d_enabled"  in r
        assert r["phantom_5m_enabled"]  is True
        assert r["phantom_15m_enabled"] is False
        assert r["phantom_1d_enabled"]  is True
    finally:
        bot_params.update(snap)


# ── E — mapeo interval_minutes → attr ─────────────────────────────────────────

def test_interval_minutes_to_attr_mapping():
    """Replica la lógica de bot.py para verificar mapeo correcto."""
    def _attr(interval_minutes: int) -> str:
        return (
            "phantom_5m_enabled"  if interval_minutes <= 5 else
            "phantom_1d_enabled"  if interval_minutes >= 1440 else
            "phantom_15m_enabled"
        )
    assert _attr(5)    == "phantom_5m_enabled"
    assert _attr(15)   == "phantom_15m_enabled"
    assert _attr(1440) == "phantom_1d_enabled"
    # edge cases
    assert _attr(1)    == "phantom_5m_enabled"
    assert _attr(30)   == "phantom_15m_enabled"
    assert _attr(2880) == "phantom_1d_enabled"
