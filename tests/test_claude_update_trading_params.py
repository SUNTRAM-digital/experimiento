"""Point 17 — Claude puede modificar parámetros de Trading Mode.
Valida que:
  1. valid_keys en update_params incluye todas las claves trading_*.
  2. BotParams.update() acepta esos cambios y persisten en to_dict().
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

API_PATH = os.path.join(os.path.dirname(__file__), '..', 'api.py')

TRADING_KEYS = [
    "trading_mode_enabled", "trading_real_enabled",
    "trading_entry_threshold", "trading_min_entry_price", "trading_max_entry_price",
    "trading_trend_prefer_winning", "trading_profit_offset",
    "trading_exit_deadline_min", "trading_min_entry_minutes_left",
    "trading_max_entries_per_market", "trading_max_open_per_side",
    "trading_stake_usdc", "trading_one_open_at_a_time",
    "trading_real_max_exposure_usdc", "trading_real_daily_loss_limit_usdc",
    "trading_real_max_consec_losses", "trading_real_killed",
    "trading_sl_enabled", "trading_sl_trigger_drop", "trading_sl_wait_min",
    "trading_sl_min_recover_factor",
    "trading_panic_trigger_drop", "trading_panic_min_recover_factor",
    "trading_buy_probable", "trading_probable_min_price",
    "trading_probable_max_price", "trading_probable_profit_offset",
]


def test_update_params_valid_keys_include_trading():
    with open(API_PATH, encoding='utf-8') as f:
        src = f.read()
    # Extraer literal set asignado a valid_keys dentro de update_params
    assert "elif name == \"update_params\":" in src
    # search by parsing: find the exact block
    idx = src.find("elif name == \"update_params\":")
    chunk = src[idx:idx+3000]
    for key in TRADING_KEYS:
        assert f'"{key}"' in chunk, f'valid_keys debe incluir {key}'


def test_botparams_update_accepts_trading_changes():
    """Verifica que BotParams.update() acepta claves trading_* sin persistir en params.json."""
    from config import BotParams
    bp = BotParams()
    # Guardar snapshot para restaurar después (update() hace self.save())
    snapshot = bp.to_dict().copy()
    try:
        bp.update({
            "trading_stake_usdc": 7.5,
            "trading_profit_offset": 0.30,
            "trading_buy_probable": False,
            "trading_sl_enabled": False,
            "trading_real_killed": True,
        })
        d = bp.to_dict()
        assert d["trading_stake_usdc"] == 7.5
        assert d["trading_profit_offset"] == 0.30
        assert d["trading_buy_probable"] is False
        assert d["trading_sl_enabled"] is False
        assert d["trading_real_killed"] is True
    finally:
        # restaurar snapshot para no corromper data/params.json
        bp.update(snapshot)


def test_update_params_tool_description_mentions_trading_sl():
    import importlib
    api = importlib.import_module('api')
    tool = next((t for t in api.CHAT_TOOLS if t["name"] == "update_params"), None)
    assert tool is not None
    desc = tool["input_schema"]["properties"]["params"]["description"]
    for k in ["trading_sl_enabled", "trading_buy_probable", "trading_stake_usdc",
              "trading_real_killed"]:
        assert k in desc
