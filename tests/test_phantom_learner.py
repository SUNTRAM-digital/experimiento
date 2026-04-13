"""
Tests for phantom_learner.py — adaptive learning from phantom VPS trades.
"""
import importlib
import json
import os
import sys
import tempfile
import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fresh_module(tmp_dir):
    """Import phantom_learner with STATS_FILE pointing to a temp directory."""
    # Remove cached module so the module-level _load() runs again
    if "phantom_learner" in sys.modules:
        del sys.modules["phantom_learner"]

    # Patch the data directory via environment before import
    stats_path = os.path.join(tmp_dir, "phantom_learner_stats.json")
    import phantom_learner as pl
    pl.STATS_FILE = stats_path
    pl._stats = {}
    pl._load()
    return pl


def _make_trade(signal="UP", confidence_pct=50.0, tier="high", ta_scores=None):
    return {
        "signal":           signal,
        "confidence_pct":   confidence_pct,
        "confidence_tier":  tier,
        "ta_scores":        ta_scores or {},
    }


# ── Basic record_result ────────────────────────────────────────────────────────

class TestRecordResult:
    def test_win_increments_wins_and_total(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5, _make_trade(), won=True)
        s = pl._stats["5"]
        assert s["total"] == 1
        assert s["wins"] == 1

    def test_loss_increments_total_not_wins(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5, _make_trade(), won=False)
        s = pl._stats["5"]
        assert s["total"] == 1
        assert s["wins"] == 0

    def test_recent_window_capped(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        for _ in range(30):
            pl.record_result(5, _make_trade(), won=True)
        assert len(pl._stats["5"]["recent"]) == pl._RECENT_WINDOW

    def test_by_tier_updated(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5, _make_trade(tier="aggressive"), won=True)
        pl.record_result(5, _make_trade(tier="aggressive"), won=False)
        bt = pl._stats["5"]["by_tier"]["aggressive"]
        assert bt["w"] == 1
        assert bt["l"] == 1

    def test_by_side_updated(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(15, _make_trade(signal="DOWN"), won=True)
        bs = pl._stats["15"]["by_side"]["DOWN"]
        assert bs["w"] == 1
        assert bs["l"] == 0

    def test_by_conf_range_0_20(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5, _make_trade(confidence_pct=10.0), won=True)
        assert pl._stats["5"]["by_conf_range"]["0-20"]["w"] == 1

    def test_by_conf_range_80_100(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5, _make_trade(confidence_pct=90.0), won=False)
        assert pl._stats["5"]["by_conf_range"]["80-100"]["l"] == 1

    def test_persisted_to_disk(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5, _make_trade(), won=True)
        assert os.path.exists(pl.STATS_FILE)
        with open(pl.STATS_FILE) as f:
            data = json.load(f)
        assert data["5"]["wins"] == 1

    def test_5m_and_15m_independent(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        pl.record_result(5,  _make_trade(), won=True)
        pl.record_result(15, _make_trade(), won=False)
        assert pl._stats["5"]["wins"] == 1
        assert pl._stats["15"]["wins"] == 0


# ── get_adaptive_params — insufficient data ────────────────────────────────────

class TestAdaptiveParamsNoData:
    def test_has_data_false_when_empty(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        p = pl.get_adaptive_params(5)
        assert p["has_data"] is False

    def test_returns_defaults_when_no_data(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        p = pl.get_adaptive_params(5)
        assert p["min_confidence_tier"] == "minimal"
        assert p["preferred_side"] == "BOTH"
        assert p["block_up"] is False
        assert p["block_down"] is False

    def test_insights_mentions_min_samples(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        p = pl.get_adaptive_params(5)
        assert any("insuficientes" in i.lower() for i in p["insights"])


# ── get_adaptive_params — with data ───────────────────────────────────────────

def _fill_trades(pl, interval, win_count, loss_count,
                 side="UP", tier="high", conf=55.0):
    """Insert win_count wins and loss_count losses."""
    for _ in range(win_count):
        pl.record_result(interval, _make_trade(signal=side, tier=tier, confidence_pct=conf), won=True)
    for _ in range(loss_count):
        pl.record_result(interval, _make_trade(signal=side, tier=tier, confidence_pct=conf), won=False)


class TestAdaptiveParamsWithData:
    def test_has_data_true_after_min_samples(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        _fill_trades(pl, 5, win_count=5, loss_count=4)  # 9 trades ≥ _MIN_SAMPLES=8
        p = pl.get_adaptive_params(5)
        assert p["has_data"] is True

    def test_recent_wr_pct_calculated(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        _fill_trades(pl, 5, win_count=6, loss_count=4)   # 60% WR
        p = pl.get_adaptive_params(5)
        assert p["recent_wr_pct"] == pytest.approx(60.0, abs=1)

    def test_low_wr_insight_generated(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        _fill_trades(pl, 5, win_count=2, loss_count=8)   # 20% WR
        p = pl.get_adaptive_params(5)
        assert any("bajo" in i.lower() for i in p["insights"])

    def test_good_wr_insight_generated(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        _fill_trades(pl, 5, win_count=8, loss_count=2)   # 80% WR
        p = pl.get_adaptive_params(5)
        assert any("bueno" in i.lower() for i in p["insights"])

    def test_preferred_side_down_when_up_loses(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        # UP: 3 wins 10 losses → 23% WR
        _fill_trades(pl, 5, win_count=3,  loss_count=10, side="UP")
        # DOWN: 8 wins 4 losses → 67% WR
        _fill_trades(pl, 5, win_count=8,  loss_count=4,  side="DOWN")
        p = pl.get_adaptive_params(5)
        assert p["preferred_side"] == "DOWN"
        assert p["block_up"] is True

    def test_preferred_side_up_when_down_loses(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        _fill_trades(pl, 15, win_count=8,  loss_count=4,  side="UP")
        _fill_trades(pl, 15, win_count=3,  loss_count=10, side="DOWN")
        p = pl.get_adaptive_params(15)
        assert p["preferred_side"] == "UP"
        assert p["block_down"] is True

    def test_total_trades_returned(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        _fill_trades(pl, 5, win_count=5, loss_count=5)
        p = pl.get_adaptive_params(5)
        assert p["total_trades"] == 10


# ── rebuild_from_vps_file ──────────────────────────────────────────────────────

class TestRebuildFromVps:
    def _make_vps_file(self, tmp_path, trades):
        path = os.path.join(str(tmp_path), "vps.json")
        with open(path, "w") as f:
            json.dump({"trades": trades}, f)
        return path

    def test_rebuild_counts_resolved_trades(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        vps = self._make_vps_file(tmp_path, [
            {"market": "updown_5m",  "result": "WIN",     "signal": "UP",   "confidence_pct": 50, "confidence_tier": "high"},
            {"market": "updown_5m",  "result": "LOSS",    "signal": "DOWN", "confidence_pct": 30, "confidence_tier": "moderate"},
            {"market": "updown_5m",  "result": "PENDING", "signal": "UP",   "confidence_pct": 40, "confidence_tier": "moderate"},
            {"market": "updown_15m", "result": "WIN",     "signal": "UP",   "confidence_pct": 55, "confidence_tier": "high"},
        ])
        count = pl.rebuild_from_vps_file(vps)
        assert count == 3   # PENDING excluded

    def test_rebuild_missing_file_returns_0(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        count = pl.rebuild_from_vps_file("/nonexistent/path.json")
        assert count == 0

    def test_rebuild_populates_stats(self, tmp_path):
        pl = _fresh_module(str(tmp_path))
        vps = self._make_vps_file(tmp_path, [
            {"market": "updown_5m", "result": "WIN",  "signal": "UP",   "confidence_pct": 50, "confidence_tier": "high"},
            {"market": "updown_5m", "result": "LOSS", "signal": "UP",   "confidence_pct": 50, "confidence_tier": "high"},
        ])
        pl.rebuild_from_vps_file(vps)
        assert pl._stats["5"]["total"] == 2
        assert pl._stats["5"]["wins"]  == 1

    def test_rebuild_15m_not_classified_as_5m(self, tmp_path):
        """Regression: '5m' is a substring of '15m' so naive 'in' check misclassifies 15m trades."""
        pl = _fresh_module(str(tmp_path))
        vps = self._make_vps_file(tmp_path, [
            {"market": "updown_15m", "result": "WIN",  "signal": "UP", "confidence_pct": 55, "confidence_tier": "high"},
            {"market": "updown_15m", "result": "LOSS", "signal": "UP", "confidence_pct": 55, "confidence_tier": "high"},
            {"market": "updown_15m", "result": "WIN",  "signal": "UP", "confidence_pct": 55, "confidence_tier": "high"},
        ])
        pl.rebuild_from_vps_file(vps)
        assert pl._stats["15"]["total"] == 3, "15m trades must go into interval '15'"
        assert pl._stats["5"]["total"]  == 0, "5m bucket must stay empty"


# ── Pattern insights (_get_pattern_insights) ───────────────────────────────────

class TestPatternInsights:
    """Tests for _get_pattern_insights integration."""

    def _mock_analysis(self, monkeypatch, corr_conf=0.0, corr_mom=0.0,
                       wr_up=50, wr_down=50, n_up=10, n_down=10,
                       n_trades=20):
        """Patch phantom_analysis.analyze_phantom_trades with controlled return."""
        import sys
        import types
        mock_mod = types.ModuleType("phantom_analysis")
        mock_mod.analyze_phantom_trades = lambda interval=None: {
            "ok": True,
            "trades_analyzed": n_trades,
            "correlations": {
                "confidence_vs_result": corr_conf,
                "momentum_vs_result":   corr_mom,
                "ta_combined_vs_result": 0.0,
            },
            "wr_by_side": {"UP": wr_up, "DOWN": wr_down, "n_up": n_up, "n_down": n_down},
        }
        monkeypatch.setitem(sys.modules, "phantom_analysis", mock_mod)

    def test_negative_corr_sets_invert_confidence(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_conf=-0.25, n_trades=20)
        result = pl._get_pattern_insights(15, total_trades=20)
        assert result["invert_confidence"] is True
        assert any("INVERTIDA" in i or "invertida" in i.lower() for i in result["pattern_insights"])

    def test_weak_neg_corr_does_not_invert(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_conf=-0.08, n_trades=20)
        result = pl._get_pattern_insights(15, total_trades=20)
        assert result["invert_confidence"] is False

    def test_mean_reversion_hint(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_mom=-0.25, n_trades=20)
        result = pl._get_pattern_insights(15, total_trades=20)
        assert result["strategy_hint"] == "mean_reversion"

    def test_trend_following_hint(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_mom=0.20, n_trades=20)
        result = pl._get_pattern_insights(15, total_trades=20)
        assert result["strategy_hint"] == "trend_following"

    def test_neutral_when_low_mom_corr(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_mom=0.05, n_trades=20)
        result = pl._get_pattern_insights(15, total_trades=20)
        assert result["strategy_hint"] == "neutral"

    def test_returns_defaults_below_min_trades(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_conf=-0.5, corr_mom=-0.5, n_trades=20)
        result = pl._get_pattern_insights(15, total_trades=10)  # below _MIN_CORR_TRADES=15
        assert result["invert_confidence"] is False
        assert result["strategy_hint"] == "neutral"

    def test_analysis_side_detected(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, wr_up=65, wr_down=42, n_up=12, n_down=10, n_trades=22)
        result = pl._get_pattern_insights(15, total_trades=22)
        assert result["analysis_side"] == "UP"

    def test_get_adaptive_params_includes_pattern_fields(self, tmp_path, monkeypatch):
        pl = _fresh_module(str(tmp_path))
        self._mock_analysis(monkeypatch, corr_conf=-0.20, corr_mom=-0.22, n_trades=20)
        _fill_trades(pl, 15, win_count=10, loss_count=8)  # 18 trades ≥ _MIN_SAMPLES
        p = pl.get_adaptive_params(15)
        assert "invert_confidence" in p
        assert "strategy_hint" in p
        assert "conf_corr" in p
        # With corr_conf=-0.20 and 18 total trades >= 15, should be inverted
        assert p["invert_confidence"] is True
        assert p["strategy_hint"] == "mean_reversion"
