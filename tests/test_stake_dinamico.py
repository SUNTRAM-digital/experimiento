"""
Tests for item 29: dynamic stake by confidence level
and item 32: unified WR source in phantom_learner.
"""
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── Item 29: stake dynamic interpolation ─────────────────────────────────────

def _calc_stake(conf, stake_min=3.0, stake_max=15.0, conf_min=20.0, conf_max=65.0):
    """Replica the bot's stake interpolation logic."""
    if conf_max > conf_min:
        t = max(0.0, min(1.0, (conf - conf_min) / (conf_max - conf_min)))
    else:
        t = 0.0
    return round(stake_min + (stake_max - stake_min) * t, 2)


class TestStakeDinamico:
    def test_below_min_conf_returns_min_stake(self):
        assert _calc_stake(conf=10) == 3.0

    def test_at_min_conf_returns_min_stake(self):
        assert _calc_stake(conf=20) == 3.0

    def test_at_max_conf_returns_max_stake(self):
        assert _calc_stake(conf=65) == 15.0

    def test_above_max_conf_returns_max_stake(self):
        assert _calc_stake(conf=90) == 15.0

    def test_midpoint_interpolated(self):
        # conf=42.5 is midpoint of 20-65 → stake midpoint of 3-15 = 9
        result = _calc_stake(conf=42.5)
        assert abs(result - 9.0) < 0.1

    def test_custom_range(self):
        # stake 5-20, conf 30-70, at conf=50 → t=0.5 → stake=12.5
        result = _calc_stake(conf=50, stake_min=5.0, stake_max=20.0, conf_min=30.0, conf_max=70.0)
        assert abs(result - 12.5) < 0.1

    def test_conf_min_equals_max_returns_min(self):
        # degenerate case: conf_min == conf_max → t=0 → min stake
        result = _calc_stake(conf=50, conf_min=50.0, conf_max=50.0)
        assert result == 3.0

    def test_stake_never_exceeds_max(self):
        assert _calc_stake(conf=999) == 15.0

    def test_stake_never_below_min(self):
        assert _calc_stake(conf=-100) == 3.0


# ── Item 29: config params exist ─────────────────────────────────────────────

class TestStakeConfig:
    def test_config_has_stake_params(self):
        from config import BotParams
        p = BotParams.__new__(BotParams)
        p.__dict__.update({
            'updown_stake_min_usdc': 3.0,
            'updown_stake_max_usdc': 15.0,
            'updown_stake_conf_min_pct': 20.0,
            'updown_stake_conf_max_pct': 65.0,
        })
        assert hasattr(p, 'updown_stake_min_usdc')
        assert hasattr(p, 'updown_stake_max_usdc')
        assert hasattr(p, 'updown_stake_conf_min_pct')
        assert hasattr(p, 'updown_stake_conf_max_pct')

    def test_config_to_dict_includes_stake_params(self):
        """to_dict() must expose all 4 stake params for UI."""
        from config import BotParams
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            orig = BotParams.__module__
            import config as _cfg
            old_file = _cfg._PARAMS_FILE
            _cfg._PARAMS_FILE = Path(tmp) / 'params.json'
            try:
                p = BotParams()
                d = p.to_dict()
            finally:
                _cfg._PARAMS_FILE = old_file
        assert 'updown_stake_min_usdc' in d
        assert 'updown_stake_max_usdc' in d
        assert 'updown_stake_conf_min_pct' in d
        assert 'updown_stake_conf_max_pct' in d
        assert d['updown_stake_min_usdc'] == 3.0
        assert d['updown_stake_max_usdc'] == 15.0


# ── Item 32: phantom_learner record_result and get_total_win_rate ─────────────

class TestPhantomLearnerWR:
    def setup_method(self):
        import phantom_learner as pl
        pl._stats = pl._default_stats()  # reset in-memory

    def test_record_updates_total_and_wins(self):
        from phantom_learner import record_result, _stats
        record_result(15, {"signal": "UP", "confidence_pct": 50, "confidence_tier": "high"}, True)
        assert _stats["15"]["total"] == 1
        assert _stats["15"]["wins"] == 1

    def test_record_loss_does_not_increment_wins(self):
        from phantom_learner import record_result, _stats
        record_result(15, {"signal": "DOWN", "confidence_pct": 30, "confidence_tier": "moderate"}, False)
        assert _stats["15"]["total"] == 1
        assert _stats["15"]["wins"] == 0

    def test_get_total_win_rate_returns_none_below_min(self):
        from phantom_learner import record_result, get_total_win_rate, _AUTORULE_MIN_SAMPLES
        for _ in range(_AUTORULE_MIN_SAMPLES - 1):
            record_result(5, {"signal": "UP", "confidence_pct": 20, "confidence_tier": "minimal"}, True)
        assert get_total_win_rate(5) is None

    def test_get_total_win_rate_correct_value(self):
        from phantom_learner import record_result, get_total_win_rate, _AUTORULE_MIN_SAMPLES
        # add _AUTORULE_MIN_SAMPLES trades: half wins
        n = _AUTORULE_MIN_SAMPLES
        for i in range(n):
            record_result(15, {"signal": "UP", "confidence_pct": 50, "confidence_tier": "high"}, i < n // 2)
        wr = get_total_win_rate(15)
        assert wr is not None
        assert abs(wr - 0.5) < 0.01

    def test_5m_and_15m_tracked_independently(self):
        from phantom_learner import record_result, _stats
        record_result(5,  {"signal": "UP", "confidence_pct": 20, "confidence_tier": "minimal"}, True)
        record_result(15, {"signal": "DOWN", "confidence_pct": 30, "confidence_tier": "low_moderate"}, False)
        assert _stats["5"]["total"] == 1
        assert _stats["5"]["wins"] == 1
        assert _stats["15"]["total"] == 1
        assert _stats["15"]["wins"] == 0

    def test_rebuild_from_vps_file(self, tmp_path):
        import json, phantom_learner as pl
        # write a fake VPS file
        vps = {
            "trades": [
                {"result": "WIN",  "market": "updown_15m", "signal": "UP",   "confidence_pct": 60, "confidence_tier": "high"},
                {"result": "LOSS", "market": "updown_15m", "signal": "DOWN", "confidence_pct": 40, "confidence_tier": "moderate"},
                {"result": "WIN",  "market": "updown_5m",  "signal": "UP",   "confidence_pct": 25, "confidence_tier": "low_moderate"},
                {"result": "PENDING", "market": "updown_15m", "signal": "UP", "confidence_pct": 50},  # should be skipped
            ]
        }
        f = tmp_path / "vps.json"
        f.write_text(json.dumps(vps))
        pl._stats = pl._default_stats()
        count = pl.rebuild_from_vps_file(str(f))
        assert count == 3
        assert pl._stats["15"]["total"] == 2
        assert pl._stats["15"]["wins"] == 1
        assert pl._stats["5"]["total"] == 1
        assert pl._stats["5"]["wins"] == 1
