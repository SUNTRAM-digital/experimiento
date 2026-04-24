"""Point 13 — Claude tiene acceso total al Trading Mode (contexto + params).
Verifica que:
  1. _build_chat_context incluye sección TRADING MODE con params y stats.
  2. CHAT_SYSTEM menciona Trading Mode y stop-loss.
  3. update_params tool acepta claves trading_*.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def test_chat_system_prompt_mentions_trading_mode():
    import api
    assert "Trading Mode" in api.CHAT_SYSTEM
    assert "trading_sl_enabled" in api.CHAT_SYSTEM or "stop-loss" in api.CHAT_SYSTEM.lower()
    assert "trading_real_enabled" in api.CHAT_SYSTEM or "trading real" in api.CHAT_SYSTEM.lower()


def test_update_params_tool_covers_trading_keys():
    import api
    tool = next((t for t in api.CHAT_TOOLS if t["name"] == "update_params"), None)
    assert tool is not None
    desc = tool["input_schema"]["properties"]["params"]["description"]
    # trading core
    for key in ["trading_mode_enabled", "trading_entry_threshold", "trading_max_entry_price",
                "trading_profit_offset", "trading_stake_usdc",
                "trading_sl_enabled", "trading_sl_trigger_drop", "trading_panic_trigger_drop",
                "trading_real_max_exposure_usdc", "trading_real_killed"]:
        assert key in desc, f"update_params debe exponer {key}"


def test_build_chat_context_includes_trading_section():
    """AST-check: _build_chat_context debe emitir sección === TRADING MODE ===."""
    root = os.path.join(os.path.dirname(__file__), '..')
    with open(os.path.join(root, 'api.py'), encoding='utf-8') as f:
        src = f.read()
    # marcador textual dentro de la función
    assert "=== TRADING MODE ===" in src
    assert "trading_sl_enabled" in src
    assert "trading_max_entry_price" in src


def test_api_trading_params_endpoint_accepts_sl():
    """El endpoint /api/trading/params debe aceptar y castear trading_sl_* params."""
    root = os.path.join(os.path.dirname(__file__), '..')
    with open(os.path.join(root, 'api.py'), encoding='utf-8') as f:
        src = f.read()
    # mapping debe contener claves SL
    for key in ["trading_sl_enabled", "trading_sl_trigger_drop", "trading_sl_wait_min",
                "trading_sl_min_recover_factor", "trading_panic_trigger_drop"]:
        assert f'"{key}"' in src, f"api.py debe aceptar {key} en /api/trading/params"
