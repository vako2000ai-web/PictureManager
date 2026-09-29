from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Callable

from .errors import Cancelled

CHUNK = 1024 * 1024


def sha256_file(path, progress: Callable[[int], None] | None = None,
                cancel: Callable[[], bool] | None = None) -> str:
    """Потоковый SHA-256: файл целиком в память не читается."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel and cancel():
                raise Cancelled()
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            if progress:
                progress(len(chunk))
    return h.hexdigest()


def file_abs_path(conn: sqlite3.Connection, row) -> Path:
    root = conn.execute("SELECT path FROM roots WHERE id=?", (row["root_id"],)).fetchone()
    return Path(root["path"]).joinpath(*row["rel_path"].split("/"))


def ensure_hash(conn: sqlite3.Connection, row, progress=None, cancel=None) -> str:
    """Хеш из кэша, если размер и mtime не менялись с момента вычисления."""
    if row["sha256"] and row["hash_size"] == row["size"] and row["hash_mtime_ns"] == row["mtime_ns"]:
        if progress:
            progress(0)
        return row["sha256"]
    path = file_abs_path(conn, row)
    st = path.stat()
    digest = sha256_file(path, progress, cancel)
    conn.execute("UPDATE files SET sha256=?, hash_size=?, hash_mtime_ns=?, size=?, mtime_ns=? WHERE id=?",
                 (digest, st.st_size, st.st_mtime_ns, st.st_size, st.st_mtime_ns, row["id"]))
    return digest
