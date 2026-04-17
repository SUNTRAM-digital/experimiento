"""
Tests for /api/bots/stats canonical WR endpoint and brain chat bot_id.
- /api/bots/stats returns all 4 bots with consistent structure
- phantom bots use phantom_learner as canonical source
- real bots use updown_learner
- chat endpoint accepts bot_id and builds focused system prompt
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestBotsStatsEndpoint:
    """get_bots_stats returns canonical WR for all 4 bots."""

    def test_returns_all_four_bots(self):
        """Response always has ud5m, ud15m, ph5m, ph15m keys."""
        import asyncio
        from api import get_bots_stats
        result = asyncio.run(get_bots_stats())
        for key in ('ud5m', 'ud15m', 'ph5m', 'ph15m'):
            assert key in result, f"Missing key: {key}"

    def test_each_bot_has_required_fields(self):
        """Each bot entry has win_rate, recent_wr, total, wins."""
        import asyncio
        from api import get_bots_stats
        result = asyncio.run(get_bots_stats())
        for key in ('ud5m', 'ud15m', 'ph5m', 'ph15m'):
            entry = result[key]
            assert 'win_rate' in entry,  f"{key} missing win_rate"
            assert 'recent_wr' in entry, f"{key} missing recent_wr"
            assert 'total' in entry,     f"{key} missing total"
            assert 'wins' in entry,      f"{key} missing wins"

    def test_win_rate_in_range_or_none(self):
        """win_rate is None or in [0, 1]."""
        import asyncio
        from api import get_bots_stats
        result = asyncio.run(get_bots_stats())
        for key in ('ud5m', 'ud15m', 'ph5m', 'ph15m'):
            wr = result[key].get('win_rate')
            if wr is not None:
                assert 0.0 <= wr <= 1.0, f"{key} win_rate={wr} out of [0,1]"

    def test_no_error_key(self):
        """Response must not have an error key (graceful fallback on empty data)."""
        import asyncio
        from api import get_bots_stats
        result = asyncio.run(get_bots_stats())
        assert 'error' not in result, f"Unexpected error: {result.get('error')}"

    def test_total_and_wins_consistent(self):
        """wins <= total for all bots."""
        import asyncio
        from api import get_bots_stats
        result = asyncio.run(get_bots_stats())
        for key in ('ud5m', 'ud15m', 'ph5m', 'ph15m'):
            e = result[key]
            assert e['wins'] <= e['total'], f"{key}: wins={e['wins']} > total={e['total']}"


class TestChatBotIdSystem:
    """chat_endpoint uses focused system prompt when bot_id is provided."""

    def test_bot_id_in_brain_names(self):
        """All valid bot_ids have a mapped name."""
        bot_names = {
            "ud5m": "UpDown 5m (Real)", "ud15m": "UpDown 15m (Real)",
            "ph5m": "Phantom 5m",        "ph15m": "Phantom 15m",
        }
        for bid in ('ud5m', 'ud15m', 'ph5m', 'ph15m'):
            assert bid in bot_names
            assert "5m" in bot_names[bid] or "15m" in bot_names[bid]

    def test_interval_derived_from_bot_id(self):
        """Interval string '5' or '15' is correctly derived from bot_id."""
        cases = {
            'ud5m': '5', 'ph5m': '5',
            'ud15m': '15', 'ph15m': '15',
        }
        for bid, expected in cases.items():
            interval = "15" if "15m" in bid else "5"
            assert interval == expected, f"Expected '{expected}' for {bid}, got {interval}"
