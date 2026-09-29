"""Точные дубли: группировка по размеру, затем SHA-256; выбор оригинала; карантин."""
from __future__ import annotations

import re

from .db import Database
from .errors import Cancelled
from .hashing import ensure_hash
from .jobs import JobContext
from . import ops

_IN_STRUCTURE = re.compile(r"(^|/)(19|20)\d{2}/\d{2}/\d{2}/")


def choose_keeper(members: list) -> int:
    """Правило по умолчанию: уже в структуре ГГГГ/ММ/ДД, затем короче путь, затем раньше mtime."""
    manual = [m for m in members if m["keeper_manual"]]
    if manual:
        return manual[0]["id"]
    best = min(members, key=lambda m: (0 if _IN_STRUCTURE.search(m["rel_path"]) else 1,
                                       len(m["rel_path"]), m["mtime_ns"], m["id"]))
    return best["id"]


def find_duplicates(db: Database, ctx: JobContext | None = None) -> dict:
    c = db.conn()
    derived = {r[0] for r in c.execute("SELECT file_id FROM derived_files")}
    sizes = c.execute("SELECT size FROM files WHERE status='present' GROUP BY size HAVING COUNT(*)>1").fetchall()
    candidates = []
    for s in sizes:
        candidates += [r for r in c.execute("SELECT * FROM files WHERE status='present' AND size=?", (s["size"],))
                       if r["id"] not in derived]
    # Размер без пары не хешируется (кроме того, что уже в кэше).
    need = [r for r in candidates
            if not (r["sha256"] and r["hash_size"] == r["size"] and r["hash_mtime_ns"] == r["mtime_ns"])]
    need_ids = {r["id"] for r in need}
    if ctx:
        ctx.set_total(bytes=sum(r["size"] for r in need), files=len(need))
    by_hash: dict[str, list] = {}
    skipped = 0
    for r in candidates:
        if ctx:
            ctx.check()
        try:
            cached = r["id"] in need_ids
            digest = ensure_hash(c, r, progress=(ctx.add if ctx and cached else None),
                                 cancel=(ctx.cancelled if ctx else None))
            if ctx and cached:
                ctx.add(files=1)
        except Cancelled:
            raise
        except OSError:
            skipped += 1
            continue
        fresh = c.execute("SELECT * FROM files WHERE id=?", (r["id"],)).fetchone()
        by_hash.setdefault(digest, []).append(fresh)

    groups = 0
    with db.tx() as tc:
        tc.execute("DELETE FROM dup_groups")
        for digest, members in by_hash.items():
            # Файлы одной съёмки (Live Photo, RAW+JPEG) — не дубли друг другу.
            counts: dict[str, int] = {}
            for m in members:
                if m["group_key"]:
                    counts[m["group_key"]] = counts.get(m["group_key"], 0) + 1
            members = [m for m in members if not (m["group_key"] and counts[m["group_key"]] > 1)]
            if len(members) < 2:
                continue
            gid = tc.execute("INSERT INTO dup_groups(sha256) VALUES (?)", (digest,)).lastrowid
            keeper = choose_keeper(members)
            tc.executemany("INSERT INTO dup_members(group_id,file_id,is_keeper) VALUES (?,?,?)",
                           [(gid, m["id"], 1 if m["id"] == keeper else 0) for m in members])
            groups += 1
    return {"groups": groups, "skipped_unreadable": skipped}


def list_groups(db: Database, limit: int = 100, offset: int = 0) -> list[dict]:
    c = db.conn()
    out = []
    for g in c.execute("SELECT * FROM dup_groups ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall():
        members = c.execute(
            "SELECT f.id, f.rel_path, f.size, f.effective_at, f.mtime_ns, m.is_keeper, r.path AS root_path "
            "FROM dup_members m JOIN files f ON f.id=m.file_id JOIN roots r ON r.id=f.root_id "
            "WHERE m.group_id=? ORDER BY f.id", (g["id"],)).fetchall()
        out.append({"id": g["id"], "sha256": g["sha256"], "members": [dict(m) for m in members]})
    return out


def set_keeper(db: Database, group_id: int, file_id: int) -> None:
    c = db.conn()
    ids = [r[0] for r in c.execute("SELECT file_id FROM dup_members WHERE group_id=?", (group_id,))]
    if file_id not in ids:
        raise ValueError("Файл не входит в группу")
    with db.tx() as tc:
        tc.executemany("UPDATE files SET keeper_manual=? WHERE id=?", [(1 if i == file_id else 0, i) for i in ids])
        tc.execute("UPDATE dup_members SET is_keeper=(file_id=?) WHERE group_id=?", (file_id, group_id))


def quarantine_group(db: Database, group_id: int) -> dict:
    """Все файлы группы, кроме оригинала, — в карантин (с записью в журнал)."""
    c = db.conn()
    rows = c.execute("SELECT file_id FROM dup_members WHERE group_id=? AND is_keeper=0", (group_id,)).fetchall()
    moved, errors = 0, []
    for r in rows:
        try:
            ops.quarantine_file(db, r["file_id"])
            moved += 1
            c.execute("DELETE FROM dup_members WHERE group_id=? AND file_id=?", (group_id, r["file_id"]))
        except Exception as e:  # noqa: BLE001 - одну ошибку не считаем остановкой группы
            errors.append({"file_id": r["file_id"], "error": f"{type(e).__name__}: {e}"})
    left = c.execute("SELECT COUNT(*) FROM dup_members WHERE group_id=?", (group_id,)).fetchone()[0]
    if left < 2:
        c.execute("DELETE FROM dup_groups WHERE id=?", (group_id,))
    return {"quarantined": moved, "errors": errors}
