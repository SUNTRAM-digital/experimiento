"""
Tests para telonex_data.py — Fase 11 (Telonex Integration).
Todos los tests corren sin API key real usando DataFrames falsos (mock).
"""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_fills_df(rows):
    """Crea un DataFrame de fills on-chain con los datos provistos."""
    pd = pytest.importorskip("pandas")
    return pd.DataFrame(rows)


def _base_fill(taker_asset_id, taker_side, amount=10.0, price=0.55):
    return {
        "taker_asset_id":     taker_asset_id,
        "taker_side":         taker_side,
        "maker":              "0xMaker001",
        "taker":              "0xTaker001",
        "amount":             amount,
        "price":              price,
        "block_timestamp_us": int(time.time() * 1_000_000),
    }


UP_TOKEN = "0xUP_TOKEN_ADDR"
NOW_TS   = int(time.time())
WINDOW_TS = NOW_TS - 600   # 10 min ago


# ── Tests de _canonical_side ─────────────────────────────────────────────────

def test_canonical_side_taker_buys_up_token():
    from telonex_data import _canonical_side
    row = {"taker_asset_id": UP_TOKEN, "taker_side": "buy", "_up_token": UP_TOKEN}
    assert _canonical_side(row) == "buy"


def test_canonical_side_taker_sells_up_token():
    from telonex_data import _canonical_side
    row = {"taker_asset_id": UP_TOKEN, "taker_side": "sell", "_up_token": UP_TOKEN}
    assert _canonical_side(row) == "sell"


def test_canonical_side_taker_buys_down_token():
    from telonex_data import _canonical_side
    row = {"taker_asset_id": "0xDOWN", "taker_side": "buy", "_up_token": UP_TOKEN}
    assert _canonical_side(row) == "sell"   # buying DOWN = bearish for UP


def test_canonical_side_taker_sells_down_token():
    from telonex_data import _canonical_side
    row = {"taker_asset_id": "0xDOWN", "taker_side": "sell", "_up_token": UP_TOKEN}
    assert _canonical_side(row) == "buy"    # selling DOWN = bullish for UP


# ── Tests de _calc_ofi_from_fills ────────────────────────────────────────────

def test_calc_ofi_bullish():
    """Fills mayoritariamente en UP → OFI positivo."""
    from telonex_data import _calc_ofi_from_fills
    window_start_us = WINDOW_TS * 1_000_000
    rows = [
        {**_base_fill(UP_TOKEN, "buy", amount=80), "block_timestamp_us": NOW_TS * 1_000_000},
        {**_base_fill(UP_TOKEN, "sell", amount=20), "block_timestamp_us": NOW_TS * 1_000_000},
    ]
    df = _make_fills_df(rows)
    r  = _calc_ofi_from_fills(df, UP_TOKEN, window_start_us)
    assert r["ofi"] > 0
    assert r["up_volume"] > r["down_volume"]


def test_calc_ofi_bearish():
    from telonex_data import _calc_ofi_from_fills
    window_start_us = WINDOW_TS * 1_000_000
    rows = [
        {**_base_fill(UP_TOKEN, "buy", amount=20), "block_timestamp_us": NOW_TS * 1_000_000},
        {**_base_fill(UP_TOKEN, "sell", amount=80), "block_timestamp_us": NOW_TS * 1_000_000},
    ]
    df = _make_fills_df(rows)
    r  = _calc_ofi_from_fills(df, UP_TOKEN, window_start_us)
    assert r["ofi"] < 0


def test_calc_ofi_balanced_returns_zero():
    from telonex_data import _calc_ofi_from_fills
    window_start_us = WINDOW_TS * 1_000_000
    rows = [
        {**_base_fill(UP_TOKEN, "buy",  amount=50), "block_timestamp_us": NOW_TS * 1_000_000},
        {**_base_fill(UP_TOKEN, "sell", amount=50), "block_timestamp_us": NOW_TS * 1_000_000},
    ]
    df = _make_fills_df(rows)
    r  = _calc_ofi_from_fills(df, UP_TOKEN, window_start_us)
    assert r["ofi"] == 0.0


def test_calc_ofi_empty_df():
    from telonex_data import _calc_ofi_from_fills
    pd = pytest.importorskip("pandas")
    r  = _calc_ofi_from_fills(pd.DataFrame(), UP_TOKEN, WINDOW_TS * 1_000_000)
    assert r["ofi"] == 0.0
    assert r["total_fills"] == 0


def test_calc_ofi_filters_old_fills():
    """Fills anteriores a la ventana deben ignorarse."""
    from telonex_data import _calc_ofi_from_fills
    past_ts = (WINDOW_TS - 1000) * 1_000_000  # antes de la ventana
    rows = [
        {**_base_fill(UP_TOKEN, "buy", amount=999), "block_timestamp_us": past_ts},
    ]
    df = _make_fills_df(rows)
    r  = _calc_ofi_from_fills(df, UP_TOKEN, WINDOW_TS * 1_000_000)
    assert r["total_fills"] == 0
    assert r["ofi"] == 0.0


def test_calc_ofi_below_min_volume():
    """Volumen muy pequeño → OFI=0 (sin suficiente evidencia)."""
    from telonex_data import _calc_ofi_from_fills
    rows = [
        {**_base_fill(UP_TOKEN, "buy", amount=0.001), "block_timestamp_us": NOW_TS * 1_000_000},
    ]
    df = _make_fills_df(rows)
    r  = _calc_ofi_from_fills(df, UP_TOKEN, WINDOW_TS * 1_000_000)
    assert r["ofi"] == 0.0   # total < _MIN_FILLS_VOLUME


# ── Tests de _calc_wallet_pnl ────────────────────────────────────────────────

def test_calc_wallet_pnl_buyer_wins():
    """Wallet que compra a 0.4 gana si settlement=0.5."""
    from telonex_data import _calc_wallet_pnl
    rows = [{
        "taker_asset_id": UP_TOKEN, "taker_side": "buy",
        "taker": "0xWinWallet", "maker": "0xOther",
        "amount": 100.0, "price": 0.40,
        "block_timestamp_us": NOW_TS * 1_000_000,
    }]
    df     = _make_fills_df(rows)
    result = _calc_wallet_pnl(df, UP_TOKEN, settlement_value=0.5)
    assert "0xWinWallet" in result
    assert result["0xWinWallet"]["pnl"] > 0


def test_calc_wallet_pnl_buyer_loses():
    """Wallet que compra a 0.7 pierde si settlement=0.5."""
    from telonex_data import _calc_wallet_pnl
    rows = [{
        "taker_asset_id": UP_TOKEN, "taker_side": "buy",
        "taker": "0xLoser", "maker": "0xOther",
        "amount": 100.0, "price": 0.70,
        "block_timestamp_us": NOW_TS * 1_000_000,
    }]
    df     = _make_fills_df(rows)
    result = _calc_wallet_pnl(df, UP_TOKEN, settlement_value=0.5)
    assert "0xLoser" in result
    assert result["0xLoser"]["pnl"] < 0


def test_calc_wallet_pnl_empty_df():
    from telonex_data import _calc_wallet_pnl
    pd = pytest.importorskip("pandas")
    assert _calc_wallet_pnl(pd.DataFrame(), UP_TOKEN) == {}


# ── Tests de TelonexData (sin API) ───────────────────────────────────────────

@pytest.fixture
def tx(tmp_path, monkeypatch):
    """TelonexData con _DATA_DIR y _WALLETS_CACHE_FILE redirigidos a tmp_path."""
    import telonex_data as _mod
    monkeypatch.setattr(_mod, "_DATA_DIR",          tmp_path)
    monkeypatch.setattr(_mod, "_CACHE_DIR",         tmp_path / "cache")
    monkeypatch.setattr(_mod, "_WALLETS_CACHE_FILE", tmp_path / "wallets.json")
    (tmp_path / "cache").mkdir()
    from telonex_data import TelonexData
    return TelonexData()


def test_get_status_no_api_key(tx, monkeypatch):
    monkeypatch.setattr("telonex_data._get_api_key", lambda: "")
    monkeypatch.setattr("telonex_data._is_enabled",  lambda: False)
    s = tx.get_status()
    assert s["available"] is False
    assert s["top_wallet_count"] == 0
    assert isinstance(s["top_wallets"], list)


def test_get_status_with_wallets(tx):
    tx._top_wallets = {
        "0xAlpha": {"pnl": 100.0, "fills": 50, "markets": 10,
                    "pnl_rank_score": 0.9, "maker_ratio": 0.4},
    }
    tx._wallets_updated_ts = time.time() - 60
    s = tx.get_status()
    assert s["top_wallet_count"] == 1
    assert len(s["top_wallets"]) == 1
    assert s["top_wallets"][0]["wallet"] == "0xAlpha"
    assert s["top_wallets"][0]["pnl_7d"] == 100.0


def test_get_real_ofi_no_api_key(tx, monkeypatch):
    monkeypatch.setattr("telonex_data._get_api_key", lambda: "")
    monkeypatch.setattr("telonex_data._is_enabled",  lambda: False)
    r = asyncio.get_event_loop().run_until_complete(
        tx.get_real_ofi(UP_TOKEN, WINDOW_TS, 15)
    )
    assert r["ofi"] == 0.0
    assert r["source"] in ("proxy", "unavailable")


def test_get_smart_wallet_bias_no_wallets(tx, monkeypatch):
    monkeypatch.setattr("telonex_data._get_api_key", lambda: "fake_key")
    monkeypatch.setattr("telonex_data._is_enabled",  lambda: True)
    tx._top_wallets = {}
    bias = asyncio.get_event_loop().run_until_complete(
        tx.get_smart_wallet_bias(UP_TOKEN, WINDOW_TS)
    )
    assert bias == 0.0


def test_get_updown_signals_missing_market(tx, monkeypatch):
    monkeypatch.setattr("telonex_data._get_api_key", lambda: "")
    monkeypatch.setattr("telonex_data._is_enabled",  lambda: False)
    r = asyncio.get_event_loop().run_until_complete(
        tx.get_updown_signals({}, btc_price_start=50000.0)
    )
    assert r["available"] is False
    assert r["real_ofi"] == 0.0


def test_get_updown_signals_returns_dict(tx, monkeypatch):
    monkeypatch.setattr("telonex_data._get_api_key", lambda: "")
    monkeypatch.setattr("telonex_data._is_enabled",  lambda: False)
    market = {"up_token": UP_TOKEN, "window_start_ts": WINDOW_TS, "interval_minutes": 15}
    r = asyncio.get_event_loop().run_until_complete(
        tx.get_updown_signals(market, btc_price_start=50000.0)
    )
    assert "real_ofi" in r
    assert "smart_bias" in r
    assert "available" in r


def test_load_save_wallet_cache(tx):
    tx._top_wallets = {"0xBeta": {"pnl": 55.0, "fills": 20, "markets": 5,
                                   "pnl_rank_score": 0.5, "maker_ratio": 0.3}}
    tx._wallets_updated_ts = 1234567890.0
    tx._save_wallet_cache()
    tx._top_wallets = {}
    tx._wallets_updated_ts = 0.0
    tx._load_wallet_cache()
    assert "0xBeta" in tx._top_wallets
    assert tx._wallets_updated_ts == 1234567890.0


# ── Tests de strategy_updown con telonex_signals ──────────────────────────────

def test_build_signal_without_telonex():
    from strategy_updown import build_btc_direction_signal
    ta = {"signal": 0.5, "rsi": 60, "ema20": 50100, "ema50": 49800, "macd": 0.1}
    r  = build_btc_direction_signal(ta, 50000.0)
    assert "combined" in r
    assert r["telonex_available"] is False
    assert r["real_ofi"] == 0.0


def test_build_signal_with_telonex_bullish():
    from strategy_updown import build_btc_direction_signal
    ta  = {"signal": 0.5, "rsi": 60, "ema20": 50100, "ema50": 49800, "macd": 0.1}
    tx  = {"real_ofi": 0.8, "smart_bias": 0.6, "available": True}
    r_without = build_btc_direction_signal(ta, 50000.0)
    r_with    = build_btc_direction_signal(ta, 50000.0, telonex_signals=tx)
    assert r_with["telonex_available"] is True
    assert r_with["real_ofi"] == 0.8
    assert r_with["smart_bias"] == 0.6
    # Con OFI bullish fuerte, combined debería ser más alcista
    assert r_with["combined"] >= r_without["combined"]


def test_build_signal_telonex_unavailable():
    """Si available=False, debe comportarse como sin telonex."""
    from strategy_updown import build_btc_direction_signal
    ta   = {"signal": 0.3, "rsi": 55, "ema20": 50050, "ema50": 49900, "macd": 0.05}
    tx   = {"real_ofi": 0.9, "smart_bias": 0.9, "available": False}
    r_no = build_btc_direction_signal(ta, 50000.0)
    r_tx = build_btc_direction_signal(ta, 50000.0, telonex_signals=tx)
    assert r_tx["combined"] == r_no["combined"]
    assert r_tx["telonex_available"] is False


def test_evaluate_updown_market_telonex_changes_signal(monkeypatch):
    """evaluate_updown_market con telonex_signals usa pesos diferentes (ofi_real vs proxy)."""
    from strategy_updown import build_btc_direction_signal
    ta = {"signal": 0.4, "rsi": 58, "ema20": 50100, "ema50": 49900, "macd": 0.05}

    # Strong bullish telonex signals
    tx_bullish = {"real_ofi": 0.9, "smart_bias": 0.9, "available": True}
    r_no_tx = build_btc_direction_signal(ta, 50100.0, btc_price_window_start=50000.0)
    r_tx    = build_btc_direction_signal(ta, 50100.0, btc_price_window_start=50000.0,
                                         telonex_signals=tx_bullish)
    # With strong bullish OFI+smart bias Telonex signals, combined must be higher
    assert r_tx["combined"] > r_no_tx["combined"]
    assert r_tx["telonex_available"] is True

    # Strong bearish telonex signals should reduce (or flip) combined
    tx_bearish = {"real_ofi": -0.9, "smart_bias": -0.9, "available": True}
    r_bear = build_btc_direction_signal(ta, 50100.0, btc_price_window_start=50000.0,
                                        telonex_signals=tx_bearish)
    assert r_bear["combined"] < r_no_tx["combined"]
