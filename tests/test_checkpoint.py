"""Tests for checkpoint store (SQLite) and CSV recovery."""
import pytest

from scraper.checkpoint import CheckpointStore
from scraper.export import AtomicCSVWriter
from scraper.models import OUTPUT_COLUMNS


class TestCheckpointStore:
    def test_register_and_commit(self, tmp_path):
        s = CheckpointStore(tmp_path / "ck.db")
        s.register_record("id1", "key1", "pid1", "example.com", "12145551234",
                          "Dallas", "q1", '{"a":1}')
        s.set_stage("id1", "accepted")
        s.mark_committed("id1", 0)
        assert s.committed_count() == 1
        # Seen sets load committed records only.
        assert s.has_identity("key1")
        assert s.has_domain("example.com")
        assert s.has_phone("12145551234")

    def test_in_flight_record_not_treated_as_duplicate_on_reload(self, tmp_path):
        """A record that crashed before commit must NOT reseed dedup sets."""
        path = tmp_path / "ck.db"
        s1 = CheckpointStore(path)
        s1.register_record("id1", "key1", "pid1", "example.com", "12145551234",
                           "Dallas", "q1", "{}")
        # stage stays 'discovered' (crash before commit)
        s1.close()

        s2 = CheckpointStore(path)  # simulate restart
        assert s2.has_identity("key1") is False
        assert s2.has_domain("example.com") is False
        assert s2.has_phone("12145551234") is False

    def test_query_status_lifecycle(self, tmp_path):
        s = CheckpointStore(tmp_path / "ck.db")
        assert s.query_status("q1") == "pending"
        s.set_query_status("q1", "running")
        assert s.query_status("q1") == "running"
        s.set_query_status("q1", "done")
        assert s.remaining_queries(["q1", "q2"]) == ["q2"]

    def test_resume_reloads_seen_sets_committed_only(self, tmp_path):
        path = tmp_path / "ck.db"
        s1 = CheckpointStore(path)
        s1.register_record("id1", "key1", "pid1", "example.com", "12145551234",
                           "Dallas", "q1", "{}")
        s1.set_stage("id1", "accepted")
        s1.mark_committed("id1", 0)
        s1.close()

        s2 = CheckpointStore(path)  # re-open simulates restart
        assert s2.has_identity("key1")
        assert s2.has_domain("example.com")
        assert s2.has_phone("12145551234")

    def test_counters(self, tmp_path):
        s = CheckpointStore(tmp_path / "ck.db")
        s.incr("duplicates_removed", 5)
        s.incr("duplicates_removed", 3)
        assert s.get_counter("duplicates_removed") == 8

    def test_json_mirror_writes(self, tmp_path):
        s = CheckpointStore(tmp_path / "ck.db")
        s.write_json_mirror()
        assert (tmp_path / "ck.json").exists()


class TestCSVRecovery:
    def test_append_and_count(self, tmp_path):
        w = AtomicCSVWriter(tmp_path / "out.csv", OUTPUT_COLUMNS)
        row = {c: "x" for c in OUTPUT_COLUMNS}
        idx = w.append(row)
        assert idx == 0
        assert w.row_count == 1
        w.close()

    def test_reopen_preserves_rows(self, tmp_path):
        p = tmp_path / "out.csv"
        w = AtomicCSVWriter(p, OUTPUT_COLUMNS)
        w.append({c: "x" for c in OUTPUT_COLUMNS})
        w.close()

        w2 = AtomicCSVWriter(p, OUTPUT_COLUMNS)
        assert w2.row_count == 1

    def test_trims_malformed_trailing_row(self, tmp_path):
        p = tmp_path / "out.csv"
        w = AtomicCSVWriter(p, OUTPUT_COLUMNS)
        w.append({c: "x" for c in OUTPUT_COLUMNS})
        w.close()
        # Append a corrupt partial line (simulate crash mid-write).
        with open(p, "a", encoding="utf-8") as f:
            f.write("only,three,columns\n")
        w2 = AtomicCSVWriter(p, OUTPUT_COLUMNS)
        # The trailing malformed row should be trimmed, leaving 1 good row.
        assert w2.row_count == 1
