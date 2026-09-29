"""Корни сканирования и рекурсивный инкрементальный обход."""
from __future__ import annotations

import ctypes
import os
import sys
from datetime import datetime
from pathlib import Path

from .catalog import recompute_groups, resolve_date
from .config import QUARANTINE_DIR, Settings, classify_ext
from .db import Database
from .jobs import JobContext

SKIP_DIRS = {QUARANTINE_DIR.lower(), "system volume information", "$recycle.bin", ".thumbnails"}


def volume_id(path) -> str:
    """Серийный номер тома: не меняется при смене буквы диска."""
    p = os.path.abspath(str(path))
    if sys.platform == "win32":
        serial = ctypes.c_uint32(0)
        drive = os.path.splitdrive(p)[0] + "\\"
        if ctypes.windll.kernel32.GetVolumeInformationW(drive, None, 0, ctypes.byref(serial), None, None, None, 0):
            return f"{serial.value:08X}"
    return str(os.stat(p).st_dev)


def _tail(path: str) -> str:
    return os.path.splitdrive(os.path.normpath(path))[1].lower()


def add_root(db: Database, path: str, label: str | None = None) -> dict:
    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"Каталог не найден: {path}")
    norm = os.path.normpath(str(p.resolve()))
    vid = volume_id(norm)
    c = db.conn()
    row = c.execute("SELECT * FROM roots WHERE path=?", (norm,)).fetchone()
    if row is None:
        # Тот же том и тот же путь без буквы диска: диск подключили под другой буквой.
        for r in c.execute("SELECT * FROM roots WHERE volume_id=?", (vid,)):
            if _tail(r["path"]) == _tail(norm):
                c.execute("UPDATE roots SET path=? WHERE id=?", (norm, r["id"]))
                row = c.execute("SELECT * FROM roots WHERE id=?", (r["id"],)).fetchone()
                break
    if row is None:
        cur = c.execute("INSERT INTO roots(path, volume_id, label) VALUES (?,?,?)", (norm, vid, label))
        row = c.execute("SELECT * FROM roots WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_roots(db: Database) -> list[dict]:
    rows = db.conn().execute(
        "SELECT r.*, (SELECT COUNT(*) FROM files f WHERE f.root_id=r.id) AS files "
        "FROM roots r ORDER BY r.id").fetchall()
    return [dict(r) for r in rows]


def delete_root(db: Database, root_id: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM roots WHERE id=?", (root_id,))


def _walk(root: str, skip_paths: set[str], errors: list):
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            it = os.scandir(cur)
        except OSError as e:
            errors.append({"path": cur, "error": str(e)})
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name.lower()
                        if name in SKIP_DIRS or os.path.normcase(entry.path) in skip_paths:
                            continue
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        yield entry
                except OSError as e:
                    errors.append({"path": entry.path, "error": str(e)})


def scan_root(db: Database, root_id: int, settings: Settings, ctx: JobContext | None = None) -> dict:
    c = db.conn()
    root = c.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
    if root is None:
        raise ValueError("Корень не найден")
    root_path = root["path"]
    if not os.path.isdir(root_path):
        raise ValueError(f"Каталог недоступен: {root_path}")
    # Вложенные в этот каталог другие корни пропускаем: у них своя запись каталога.
    skip = {os.path.normcase(r["path"]) for r in c.execute("SELECT path FROM roots WHERE id<>?", (root_id,))}
    known = {r["rel_path"]: r for r in c.execute("SELECT * FROM files WHERE root_id=?", (root_id,))}
    seen: set[str] = set()
    errors: list = []
    stats = {"added": 0, "updated": 0, "unchanged": 0, "missing": 0, "unsupported": 0,
             "revived": 0, "errors": errors, "ffprobe_missing": not settings.ffprobe_path}
    cancelled = False
    batch = 0
    c.execute("BEGIN IMMEDIATE")
    try:
        for entry in _walk(root_path, skip, errors):
            if ctx and ctx.cancelled():
                cancelled = True
                break
            kind = classify_ext(os.path.splitext(entry.name)[1])
            if kind is None:
                stats["unsupported"] += 1
                continue
            try:
                st = entry.stat()
            except OSError as e:
                errors.append({"path": entry.path, "error": str(e)})
                continue
            rel = Path(os.path.relpath(entry.path, root_path)).as_posix()
            seen.add(rel)
            if ctx:
                ctx.add(bytes=st.st_size, files=1, total_bytes=st.st_size, total_files=1, current=entry.path)
            old = known.get(rel)
            if old and old["size"] == st.st_size and old["mtime_ns"] == st.st_mtime_ns and old["status"] == "present":
                stats["unchanged"] += 1
                continue
            taken, source, conf = resolve_date(entry.path, kind, st.st_mtime_ns, settings)
            ext = os.path.splitext(entry.name)[1].lower()
            if old:
                changed = old["size"] != st.st_size or old["mtime_ns"] != st.st_mtime_ns
                c.execute(
                    "UPDATE files SET size=?, mtime_ns=?, kind=?, ext=?, taken_at=?, date_source=?, "
                    "date_confidence=?, status='present' WHERE id=?",
                    (st.st_size, st.st_mtime_ns, kind, ext, taken, source, conf, old["id"]))
                stats["updated" if changed else "revived"] += 1
            else:
                c.execute(
                    "INSERT INTO files(root_id, rel_path, ext, size, mtime_ns, kind, taken_at, date_source, "
                    "date_confidence) VALUES (?,?,?,?,?,?,?,?,?)",
                    (root_id, rel, ext, st.st_size, st.st_mtime_ns, kind, taken, source, conf))
                stats["added"] += 1
            batch += 1
            if batch >= 500:
                c.execute("COMMIT"); c.execute("BEGIN IMMEDIATE"); batch = 0
        if not cancelled:
            for rel, old in known.items():
                if rel not in seen and old["status"] == "present":
                    c.execute("UPDATE files SET status='missing' WHERE id=?", (old["id"],))
                    stats["missing"] += 1
            c.execute("UPDATE roots SET last_scan=? WHERE id=?",
                      (datetime.now().isoformat(timespec="seconds"), root_id))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    recompute_groups(db, root_id)
    stats["cancelled"] = cancelled
    return stats
