"""
Tests for storage.py — Data Access Layer.
Tests run against the JSON backend (same interface SQLite will use).
"""
import json
import os
import sys
import pytest


def _fresh_storage(tmp_path):
    """Return a Storage instance backed by a temp directory."""
    if "storage" in sys.modules:
        del sys.modules["storage"]
    import storage as st
    backend = st._JsonBackend(str(tmp_path))
    return st.Storage(backend)


# ── load_doc / save_doc ───────────────────────────────────────────────────────

class TestDocOps:
    def test_load_missing_returns_default(self, tmp_path):
        s = _fresh_storage(tmp_path)
        assert s.load_doc("nonexistent") == {}

    def test_load_missing_custom_default(self, tmp_path):
        s = _fresh_storage(tmp_path)
        assert s.load_doc("nonexistent", default=[]) == []

    def test_save_and_load_dict(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.save_doc("state", {"balance": 100.0, "running": True})
        d = s.load_doc("state")
        assert d["balance"] == 100.0
        assert d["running"] is True

    def test_save_and_load_list(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.save_doc("logs", [{"msg": "hello"}, {"msg": "world"}])
        d = s.load_doc("logs")
        assert len(d) == 2
        assert d[0]["msg"] == "hello"

    def test_persisted_to_disk(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.save_doc("params", {"kelly": 0.25})
        path = os.path.join(str(tmp_path), "params.json")
        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["kelly"] == 0.25


# ── get_record / set_record ───────────────────────────────────────────────────

class TestRecordOps:
    def test_set_and_get_record(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.set_record("learner", "5m", {"total": 10, "wins": 6})
        r = s.get_record("learner", "5m")
        assert r["total"] == 10
        assert r["wins"] == 6

    def test_get_missing_record_returns_default(self, tmp_path):
        s = _fresh_storage(tmp_path)
        assert s.get_record("learner", "missing") is None
        assert s.get_record("learner", "missing", default={}) == {}

    def test_set_record_overwrites(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.set_record("learner", "5m", {"total": 10})
        s.set_record("learner", "5m", {"total": 20})
        assert s.get_record("learner", "5m")["total"] == 20

    def test_multiple_keys_independent(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.set_record("learner", "5m",  {"total": 5})
        s.set_record("learner", "15m", {"total": 15})
        assert s.get_record("learner", "5m")["total"]  == 5
        assert s.get_record("learner", "15m")["total"] == 15


# ── append_record / list_records ──────────────────────────────────────────────

class TestListOps:
    def test_append_and_list(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"id": 1, "result": "WIN"})
        s.append_record("trades", {"id": 2, "result": "LOSS"})
        records = s.list_records("trades")
        assert len(records) == 2

    def test_list_with_filter(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"id": 1, "result": "WIN",  "side": "UP"})
        s.append_record("trades", {"id": 2, "result": "LOSS", "side": "DOWN"})
        s.append_record("trades", {"id": 3, "result": "WIN",  "side": "DOWN"})
        wins = s.list_records("trades", result="WIN")
        assert len(wins) == 2
        assert all(r["result"] == "WIN" for r in wins)

    def test_list_with_multiple_filters(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"result": "WIN",  "side": "UP"})
        s.append_record("trades", {"result": "WIN",  "side": "DOWN"})
        s.append_record("trades", {"result": "LOSS", "side": "UP"})
        result = s.list_records("trades", result="WIN", side="UP")
        assert len(result) == 1

    def test_list_empty_collection(self, tmp_path):
        s = _fresh_storage(tmp_path)
        assert s.list_records("nonexistent") == []

    def test_count_records(self, tmp_path):
        s = _fresh_storage(tmp_path)
        for _ in range(5):
            s.append_record("trades", {"result": "WIN"})
        s.append_record("trades", {"result": "LOSS"})
        assert s.count_records("trades") == 6
        assert s.count_records("trades", result="WIN") == 5
        assert s.count_records("trades", result="LOSS") == 1


# ── update_records ────────────────────────────────────────────────────────────

class TestUpdateOps:
    def test_update_matching_records(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"id": 1, "result": "PENDING"})
        s.append_record("trades", {"id": 2, "result": "PENDING"})
        s.append_record("trades", {"id": 3, "result": "WIN"})
        count = s.update_records("trades", match={"id": 1}, update={"result": "WIN"})
        assert count == 1
        wins = s.list_records("trades", result="WIN")
        assert len(wins) == 2

    def test_update_returns_zero_when_no_match(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"id": 1, "result": "WIN"})
        count = s.update_records("trades", match={"id": 99}, update={"result": "LOSS"})
        assert count == 0


# ── delete_records ────────────────────────────────────────────────────────────

class TestDeleteOps:
    def test_delete_by_filter(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"id": 1, "result": "WIN"})
        s.append_record("trades", {"id": 2, "result": "LOSS"})
        s.append_record("trades", {"id": 3, "result": "WIN"})
        deleted = s.delete_records("trades", result="LOSS")
        assert deleted == 1
        assert s.count_records("trades") == 2

    def test_delete_returns_zero_when_no_match(self, tmp_path):
        s = _fresh_storage(tmp_path)
        s.append_record("trades", {"id": 1, "result": "WIN"})
        assert s.delete_records("trades", result="LOSS") == 0


# ── backend_info ──────────────────────────────────────────────────────────────

class TestBackendInfo:
    def test_backend_is_json(self, tmp_path):
        s = _fresh_storage(tmp_path)
        info = s.backend_info()
        assert info["backend"] == "json"

    def test_collection_exists(self, tmp_path):
        s = _fresh_storage(tmp_path)
        assert s.collection_exists("state") is False
        s.save_doc("state", {})
        assert s.collection_exists("state") is True
