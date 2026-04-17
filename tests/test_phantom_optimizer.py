"""
Tests for phantom_optimizer.py — autonomous strategy optimizer.
- State tracking (consec wins/losses, preset progression)
- Real money toggle rules (WR thresholds + consecutive streaks)
- Preset rotation when WR < 50% after TRIAL_MIN trades
- get_status() returns expected keys
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import patch, MagicMock


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_phantom_stats(wins: int, total: int):
    """Fake updown_learner._stats with phantom data."""
    return {
        "5":  {"phantom": {"wins": wins, "total": total, "recent": [], "by_signal": {}, "by_side": {}, "by_elapsed": {}, "by_skip_reason": {}}},
        "15": {"phantom": {"wins": wins, "total": total, "recent": [], "by_signal": {}, "by_side": {}, "by_elapsed": {}, "by_skip_reason": {}}},
    }


class TestStateTracking:

    def setup_method(self):
        """Reset optimizer state before each test."""
        import phantom_optimizer as opt
        opt._state = opt._default_state()

    def test_consec_wins_increments(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(8, 10)
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, True)
            opt.check_and_act(5, True)
            assert opt._state["5"]["consec_wins"] == 2
            assert opt._state["5"]["consec_losses"] == 0

    def test_consec_losses_increments(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(3, 10)
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(15, False)
            opt.check_and_act(15, False)
            assert opt._state["15"]["consec_losses"] == 2
            assert opt._state["15"]["consec_wins"] == 0

    def test_win_resets_loss_streak(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(5, 10)
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, False)
            opt.check_and_act(5, False)
            opt.check_and_act(5, True)
            assert opt._state["5"]["consec_wins"] == 1
            assert opt._state["5"]["consec_losses"] == 0

    def test_trades_in_preset_increments(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(5, 10)
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            for _ in range(5):
                opt.check_and_act(5, True)
            assert opt._state["5"]["trades_in_preset"] == 5


class TestRealMoneyRules:

    def setup_method(self):
        import phantom_optimizer as opt
        opt._state = opt._default_state()

    def test_enables_on_high_wr(self):
        """WR >= 75% with enough trades → enable real money."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(16, 20)  # 80% WR
        toggle_calls = []
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money', side_effect=lambda e, r, b: toggle_calls.append(e)), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, True)
        assert True in toggle_calls, "Should have called toggle with enable=True"

    def test_disables_on_low_wr(self):
        """WR < 50% with enough trades → disable real money."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(8, 20)  # 40% WR
        toggle_calls = []
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money', side_effect=lambda e, r, b: toggle_calls.append(e)), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, False)
        assert False in toggle_calls, "Should have called toggle with enable=False"

    def test_enables_on_7_consec_wins(self):
        """7 consecutive wins → enable real money regardless of WR."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(2, 5)  # Only 5 trades, insufficient for WR rule
        toggle_calls = []
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money', side_effect=lambda e, r, b: toggle_calls.append(e)), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            for _ in range(7):
                opt.check_and_act(5, True)
        assert True in toggle_calls

    def test_disables_on_3_consec_losses(self):
        """3 consecutive losses → disable real money."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(2, 5)  # Insufficient for WR rule
        toggle_calls = []
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money', side_effect=lambda e, r, b: toggle_calls.append(e)), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            for _ in range(3):
                opt.check_and_act(5, False)
        assert False in toggle_calls

    def test_no_action_in_neutral_zone(self):
        """WR between 50-75% with < TRIAL_MIN trades → no toggle."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(6, 10)  # 60% WR but only 10 trades (< TRIAL_MIN=20)
        toggle_calls = []
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money', side_effect=lambda e, r, b: toggle_calls.append(e)), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, True)
        assert len(toggle_calls) == 0


class TestPresetRotation:

    def setup_method(self):
        import phantom_optimizer as opt
        opt._state = opt._default_state()

    def test_preset_rotates_on_low_wr_after_trial(self):
        """WR < 50% after TRIAL_MIN trades → next preset applied."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(8, 25)  # 32% WR with 25 trades > TRIAL_MIN
        # Force trades_in_preset to TRIAL_MIN
        opt._state["5"]["trades_in_preset"] = opt.TRIAL_MIN

        preset_calls = []
        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset', side_effect=lambda k, p, b, r: preset_calls.append(p['name'])), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, False)
        assert len(preset_calls) > 0, "Should have rotated preset"
        assert opt._state["5"]["preset_idx"] == 1

    def test_preset_wraps_around(self):
        """After last preset, wraps to index 0."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(8, 25)
        last_idx = len(opt.STRATEGY_PRESETS["5"]) - 1
        opt._state["5"]["preset_idx"] = last_idx
        opt._state["5"]["trades_in_preset"] = opt.TRIAL_MIN

        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, False)
        assert opt._state["5"]["preset_idx"] == 0

    def test_trades_in_preset_resets_after_rotation(self):
        """After preset rotation, trades_in_preset resets to 0."""
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(8, 25)
        opt._state["5"]["trades_in_preset"] = opt.TRIAL_MIN

        with patch('updown_learner._stats', fake_stats), \
             patch.object(opt, '_toggle_real_money'), \
             patch.object(opt, '_apply_preset'), \
             patch.object(opt, '_save_state'):
            opt.check_and_act(5, False)
        assert opt._state["5"]["trades_in_preset"] == 0


class TestGetStatus:

    def test_returns_expected_keys(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(10, 20)
        with patch('updown_learner._stats', fake_stats):
            status = opt.get_status(5)
        required = {
            'interval', 'win_rate', 'wins', 'total',
            'consec_wins', 'consec_losses',
            'preset_idx', 'preset_name', 'preset_conf', 'preset_mom',
            'trades_in_preset', 'trial_min',
            'wr_enable', 'wr_disable',
            'consec_win_enable', 'consec_loss_disable',
        }
        for k in required:
            assert k in status, f"Missing key: {k}"

    def test_win_rate_correct(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(15, 20)
        with patch('updown_learner._stats', fake_stats):
            status = opt.get_status(15)
        assert abs(status['win_rate'] - 0.75) < 0.001

    def test_no_data_returns_none_wr(self):
        import phantom_optimizer as opt
        fake_stats = _make_phantom_stats(0, 0)
        with patch('updown_learner._stats', fake_stats):
            status = opt.get_status(5)
        assert status['win_rate'] is None
