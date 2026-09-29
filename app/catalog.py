"""Каталог: определение даты, связанные файлы, ручные даты, выборки для ленты."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import PurePosixPath

from .config import Settings
from .db import Database
from .metadata import date_from_filename, probe_video_date, read_photo_date

ISO = "%Y-%m-%dT%H:%M:%S"
KIND_RANK = {"photo": 0, "raw": 1, "video": 2}


def resolve_date(path, kind: str, mtime_ns: int, settings: Settings):
    """Каскад источников: EXIF/контейнер -> имя файла -> mtime -> неизвестно."""
    dt = None
    if kind in ("photo", "raw"):
        dt = read_photo_date(path)
        if dt:
            return dt.strftime(ISO), "exif", 0.95
    elif kind == "video":
        dt = probe_video_date(path, settings.ffprobe_path, settings.video_time_policy)
        if dt:
            return dt.strftime(ISO), "container", 0.9
    dt = date_from_filename(os.path.basename(path))
    if dt:
        return dt.strftime(ISO), "filename", 0.7
    try:
        return datetime.fromtimestamp(mtime_ns / 1e9).strftime(ISO), "mtime", 0.3
    except (OSError, OverflowError, ValueError):
        return None, None, None


def recompute_groups(db: Database, root_id: int) -> None:
    """Связывает файлы одной съёмки (одно имя без расширения в одном каталоге)
    и вычисляет effective_at: у группы дата берётся у основного файла."""
    c = db.conn()
    rows = c.execute(
        "SELECT id, rel_path, kind, taken_at, manual_taken_at FROM files "
        "WHERE root_id=? AND status IN ('present','missing')", (root_id,)).fetchall()
    derived = {r[0] for r in c.execute("SELECT file_id FROM derived_files")}
    buckets: dict[str, list] = {}
    for r in rows:
        p = PurePosixPath(r["rel_path"])
        buckets.setdefault(f"{p.parent.as_posix()}/{p.stem}".lower(), []).append(r)
    updates = []
    for key, members in buckets.items():
        own = lambda m: m["manual_taken_at"] or m["taken_at"]  # noqa: E731
        if len(members) == 1:
            m = members[0]
            updates.append((None, own(m), m["id"]))
            continue
        primary = min(members, key=lambda m: (KIND_RANK.get(m["kind"], 9), m["id"] in derived, m["id"]))
        gk = f"{root_id}:{key}"
        eff = own(primary)
        for m in members:
            updates.append((gk, eff, m["id"]))
    with db.tx() as tc:
        tc.executemany("UPDATE files SET group_key=?, effective_at=? WHERE id=?", updates)


def set_manual_date(db: Database, file_ids: list[int], iso: str | None) -> int:
    """Ручная дата всей группы связанных файлов; None снимает ручную дату."""
    if iso:
        datetime.strptime(iso, ISO)  # ValueError на некорректной строке
    c = db.conn()
    ids = set(file_ids)
    for fid in list(ids):
        row = c.execute("SELECT group_key FROM files WHERE id=?", (fid,)).fetchone()
        if row and row["group_key"]:
            ids.update(r[0] for r in c.execute("SELECT id FROM files WHERE group_key=?", (row["group_key"],)))
    roots = set()
    with db.tx() as tc:
        for fid in ids:
            tc.execute("UPDATE files SET manual_taken_at=? WHERE id=?", (iso, fid))
            r = tc.execute("SELECT root_id FROM files WHERE id=?", (fid,)).fetchone()
            if r:
                roots.add(r[0])
    for rid in roots:
        recompute_groups(db, rid)
    return len(ids)


def _filters(root_id=None, kind=None, status="present", date_from=None, date_to=None, undated=False, ext=None):
    where, args = [], []
    if status:
        where.append("f.status=?"); args.append(status)
    if root_id:
        where.append("f.root_id=?"); args.append(root_id)
    if kind:
        where.append("f.kind=?"); args.append(kind)
    if ext:
        where.append("f.ext=?"); args.append(ext.lower())
    if undated:
        where.append("f.effective_at IS NULL")
    if date_from:
        where.append("f.effective_at>=?"); args.append(date_from)
    if date_to:
        where.append("f.effective_at<?"); args.append(date_to + "T99")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def query_days(db: Database, sort: str = "desc", **flt) -> list[dict]:
    where, args = _filters(**flt)
    order = "ASC" if sort == "asc" else "DESC"
    rows = db.conn().execute(
        f"SELECT substr(f.effective_at,1,10) AS day, COUNT(*) AS n, SUM(f.size) AS bytes "
        f"FROM files f{where} GROUP BY day ORDER BY day IS NULL, day {order}", args).fetchall()
    return [{"day": r["day"], "count": r["n"], "bytes": r["bytes"]} for r in rows]


def query_files(db: Database, day: str | None = None, sort: str = "desc", limit: int = 200,
                offset: int = 0, **flt) -> list[dict]:
    where, args = _filters(**flt)
    if day:
        where += (" AND " if where else " WHERE ") + "substr(f.effective_at,1,10)=?"
        args.append(day)
    order = "ASC" if sort == "asc" else "DESC"
    rows = db.conn().execute(
        f"SELECT f.id, f.root_id, f.rel_path, f.ext, f.size, f.kind, f.status, f.effective_at, "
        f"f.date_source, f.date_confidence, f.manual_taken_at, f.group_key, r.path AS root_path "
        f"FROM files f JOIN roots r ON r.id=f.root_id{where} "
        f"ORDER BY f.effective_at IS NULL, f.effective_at {order}, f.id LIMIT ? OFFSET ?",
        args + [limit, offset]).fetchall()
    return [dict(r) for r in rows]
