import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE roots(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    volume_id TEXT,
    label TEXT,
    last_scan TEXT
);

CREATE TABLE files(
    id INTEGER PRIMARY KEY,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT,
    hash_size INTEGER,
    hash_mtime_ns INTEGER,
    kind TEXT NOT NULL,
    ext TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'present',
    taken_at TEXT,
    date_source TEXT,
    date_confidence REAL,
    manual_taken_at TEXT,
    group_key TEXT,
    UNIQUE(root_id, rel_path)
);
CREATE INDEX files_size ON files(size);
CREATE INDEX files_sha ON files(sha256);
CREATE INDEX files_group ON files(group_key);
CREATE INDEX files_taken ON files(taken_at);

CREATE TABLE derived_files(
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    op TEXT NOT NULL
);

CREATE TABLE dup_groups(
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    manual_keeper INTEGER
);
CREATE TABLE dup_members(
    group_id INTEGER NOT NULL REFERENCES dup_groups(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    is_keeper INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(group_id, file_id)
);

CREATE TABLE plans(
    id INTEGER PRIMARY KEY,
    dest_root_id INTEGER NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT
);

CREATE TABLE operations(
    id INTEGER PRIMARY KEY,
    plan_id INTEGER REFERENCES plans(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    file_id INTEGER,
    src_root_id INTEGER,
    src_rel TEXT,
    dst_root_id INTEGER,
    dst_rel TEXT,
    sha256 TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    conflict INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    note TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX operations_plan ON operations(plan_id);
CREATE INDEX operations_file ON operations(file_id);

CREATE TABLE jobs(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    files_done INTEGER NOT NULL DEFAULT 0,
    files_total INTEGER NOT NULL DEFAULT 0,
    bytes_done INTEGER NOT NULL DEFAULT 0,
    bytes_total INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    result TEXT,
    error TEXT,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT
);
"""

MIGRATIONS = [SCHEMA]


class Database:
    """SQLite в WAL-режиме; у каждого потока своё соединение."""

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        self._migrate()

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i in range(version, len(MIGRATIONS)):
            self.conn.executescript("BEGIN;" + MIGRATIONS[i] + f"PRAGMA user_version={i + 1};COMMIT;")

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
