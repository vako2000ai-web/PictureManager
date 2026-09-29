from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE roots (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    volume_id TEXT,
    label TEXT,
    last_scan TEXT
);
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    ext TEXT,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    hash_size INTEGER,
    hash_mtime_ns INTEGER,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'present',
    taken_at TEXT,
    date_source TEXT,
    date_confidence REAL,
    manual_taken_at TEXT,
    effective_at TEXT,
    group_key TEXT,
    keeper_manual INTEGER NOT NULL DEFAULT 0,
    UNIQUE (root_id, rel_path)
);
CREATE INDEX idx_files_size ON files(size);
CREATE INDEX idx_files_sha ON files(sha256);
CREATE INDEX idx_files_eff ON files(effective_at);
CREATE INDEX idx_files_group ON files(group_key);
CREATE TABLE derived_files (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    op TEXT NOT NULL
);
CREATE TABLE dup_groups (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL
);
CREATE TABLE dup_members (
    group_id INTEGER NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    is_keeper INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, file_id)
);
CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    dest_root_id INTEGER NOT NULL,
    dest_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    summary TEXT
);
CREATE TABLE plan_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    action TEXT NOT NULL,
    note TEXT,
    size INTEGER
);
CREATE INDEX idx_plan_items_plan ON plan_items(plan_id);
CREATE TABLE operations (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER,
    kind TEXT NOT NULL,
    file_id INTEGER,
    src TEXT,
    dst TEXT,
    sha256 TEXT,
    status TEXT NOT NULL,
    ts TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX idx_ops_plan ON operations(plan_id);
CREATE INDEX idx_ops_file ON operations(file_id);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    done_bytes INTEGER NOT NULL DEFAULT 0,
    total_files INTEGER NOT NULL DEFAULT 0,
    done_files INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    result TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
"""


class Database:
    """SQLite с отдельным соединением на поток, WAL и явными транзакциями."""

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._migrate()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.c = c
        return c

    @contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def _migrate(self) -> None:
        c = self.conn()
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            c.executescript(SCHEMA)
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        c = getattr(self._local, "c", None)
        if c is not None:
            c.close()
            self._local.c = None
