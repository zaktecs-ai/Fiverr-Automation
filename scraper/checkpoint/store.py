"""SQLite-backed checkpoint store.

Why SQLite over a giant JSON blob:
  * transactional integrity — a commit is atomic, so a mid-write crash never
    corrupts prior state
  * WAL mode for safe concurrent reader/writer
  * cheap fine-grained queries (per-query, per-record, per-stage)

The store tracks:
  * job-level progress (queries completed / in-flight)
  * discovered businesses (with their Maps identity) so restart never
    re-discovers or duplicate-commits
  * per-record stage progression (maps → filtered → enriched → emailed →
    mx → smtp → committed)
  * committed row offsets so CSV can be recovered safely

A human-readable JSON mirror + ".backup" copy are also maintained.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS queries (
    query TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done
    discovered INTEGER NOT NULL DEFAULT 0,
    committed INTEGER NOT NULL DEFAULT 0,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    identity_key TEXT,
    place_id TEXT,
    canonical_domain TEXT,
    normalized_phone TEXT,
    source_query TEXT,
    stage TEXT NOT NULL DEFAULT 'discovered', -- discovered|filtered|enriched|validated|committed
    raw_json TEXT,
    committed_row INTEGER,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_records_identity ON records(identity_key);
CREATE INDEX IF NOT EXISTS idx_records_domain ON records(canonical_domain);
CREATE INDEX IF NOT EXISTS idx_records_phone ON records(normalized_phone);
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
"""


class CheckpointStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._lock = threading.RLock()
        with self._conn:
            self._conn.executescript(_SCHEMA)
        self._json_path = self.path.with_suffix(".json")
        self._backup_path = self.path.with_name(self.path.name + ".backup.json")
        self._loaded_ids: set[str] = set()
        self._load_existing()

    # ------------------------------------------------------------------
    # Identity / dedup preload
    # ------------------------------------------------------------------
    def _load_existing(self) -> None:
        """Populate the in-memory set of already-seen identities/domains/phones.

        Bounded: we keep only identity keys (short strings) in memory, not raw rows.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT identity_key, canonical_domain, normalized_phone FROM records"
            ).fetchall()
        self._identities: set[str] = {r["identity_key"] for r in rows if r["identity_key"]}
        self._domains: set[str] = {r["canonical_domain"] for r in rows if r["canonical_domain"]}
        self._phones: set[str] = {r["normalized_phone"] for r in rows if r["normalized_phone"]}

    def has_identity(self, key: str) -> bool:
        return key in self._identities

    def has_domain(self, d: str) -> bool:
        return bool(d) and d in self._domains

    def has_phone(self, p: str) -> bool:
        return bool(p) and p in self._phones

    def register_record(self, record_id: str, identity_key: str, place_id: str,
                        canonical_domain: str, normalized_phone: str,
                        source_query: str, raw_json: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO records "
                "(record_id, identity_key, place_id, canonical_domain, normalized_phone, "
                " source_query, stage, raw_json, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (record_id, identity_key, place_id, canonical_domain, normalized_phone,
                 source_query, "discovered", raw_json, time.time()),
            )
            self._conn.commit()
        if identity_key:
            self._identities.add(identity_key)
        if canonical_domain:
            self._domains.add(canonical_domain)
        if normalized_phone:
            self._phones.add(normalized_phone)

    def set_stage(self, record_id: str, stage: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE records SET stage=?, updated_at=? WHERE record_id=?",
                (stage, time.time(), record_id),
            )
            self._conn.commit()

    def mark_committed(self, record_id: str, row_index: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE records SET stage='committed', committed_row=?, updated_at=? WHERE record_id=?",
                (row_index, time.time(), record_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Query progress
    # ------------------------------------------------------------------
    def query_status(self, query: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM queries WHERE query=?", (query,)).fetchone()
        return row["status"] if row else "pending"

    def set_query_status(self, query: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO queries (query, status, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(query) DO UPDATE SET status=excluded.status, "
                "updated_at=excluded.updated_at",
                (query, status, time.time()),
            )
            self._conn.commit()

    def remaining_queries(self, all_queries: list[str]) -> list[str]:
        return [q for q in all_queries if self.query_status(q) != "done"]

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------
    def incr(self, name: str, amount: int = 1) -> int:
        with self._lock:
            self._conn.execute(
                "INSERT INTO counters (name, value) VALUES (?,?) "
                "ON CONFLICT(name) DO UPDATE SET value=value+excluded.value",
                (name, amount),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            return row["value"]

    def get_counter(self, name: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        return row["value"] if row else 0

    def counters(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT name, value FROM counters").fetchall()
        return {r["name"]: r["value"] for r in rows}

    def committed_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM records WHERE stage='committed'").fetchone()
        return row["c"]

    # ------------------------------------------------------------------
    # JSON mirror + backup (for human inspection)
    # ------------------------------------------------------------------
    def write_json_mirror(self) -> None:
        """Write a human-readable summary; atomic via temp file + rename."""
        snapshot = {
            "checkpoint_path": str(self.path),
            "updated_at": time.time(),
            "queries": self._query_rows(),
            "counters": self.counters(),
            "committed_records": self.committed_count(),
        }
        tmp = self._json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        tmp.replace(self._json_path)
        # Maintain a single backup copy of the prior mirror.
        if self._json_path.exists():
            self._backup_path.write_text(self._json_path.read_text(encoding="utf-8"),
                                         encoding="utf-8")

    def _query_rows(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM queries ORDER BY query").fetchall()
        return [dict(r) for r in rows]

    def backup_checkpoint(self) -> None:
        """Create a durable copy of the SQLite file (best-effort)."""
        try:
            with self._lock:
                src = self._conn.execute("PRAGMA database_list").fetchone()
                self._conn.execute("PRAGMA wal_checkpoint(FULL)")
            import shutil
            shutil.copy2(str(self.path), str(self.path) + ".sqlite.bak")
        except Exception as e:  # pragma: no cover - defensive
            log.warning("checkpoint backup failed: %s", e)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
