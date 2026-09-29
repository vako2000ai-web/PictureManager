"""Точные дубли: группировка по размеру, затем SHA-256; карантин лишних копий."""
import os
import re

from . import catalog, journal
from .config import QUARANTINE_DIR
from .fileops import Cancelled, unique_path
from .hashing import ensure_hash


def _structure_regex(template: str) -> re.Pattern:
    """Регулярное выражение для относительного пути «в целевой структуре»."""
    parts = re.escape(template).replace(r"\{YYYY\}", r"\d{4}").replace(r"\{MM\}", r"\d{2}").replace(r"\{DD\}", r"\d{2}")
    return re.compile("^" + parts.replace(r"\/", "/") + "/")


def pick_keeper(files: list[dict], template: str) -> dict:
    """Файл в структуре ГГГГ/ММ/ДД, затем самый короткий путь, затем самый ранний mtime."""
    rx = _structure_regex(template)
    return min(files, key=lambda f: (not rx.match(f["rel_path"]), len(f["abs"]), f["mtime_ns"], f["id"]))


def find_duplicates(db, cfg, ctx) -> dict:
    old_keepers = {r["sha256"]: r["manual_keeper"] for r in db.query("SELECT sha256, manual_keeper FROM dup_groups")}
    rows = db.query(
        "SELECT id, size FROM files WHERE status='present' AND id NOT IN (SELECT file_id FROM derived_files) "
        "AND size IN (SELECT size FROM files WHERE status='present' GROUP BY size HAVING COUNT(*)>1)"
    )
    ctx.set_total(files=len(rows), bytes=sum(r["size"] for r in rows))
    by_hash: dict[str, list[dict]] = {}
    hashed = errors = 0
    for r in rows:
        if ctx.cancelled:
            break
        f = catalog.get_file(db, r["id"])
        try:
            digest = ensure_hash(db, f, cancel=lambda: ctx.cancelled)
        except Cancelled:
            break
        except OSError:
            errors += 1
            ctx.advance(files=1, bytes=r["size"])
            continue
        hashed += 1
        by_hash.setdefault(digest, []).append(f)
        ctx.advance(files=1, bytes=r["size"])
    cancelled = ctx.cancelled

    groups = {}
    for digest, members in by_hash.items():
        distinct, seen = [], set()
        for m in members:  # файлы одной связанной группы не считаются дублями друг друга
            identity = m["group_key"] or f"id{m['id']}"
            if identity not in seen:
                seen.add(identity)
                distinct.append(m)
        if len(distinct) > 1:
            groups[digest] = distinct

    if not cancelled:
        with db.tx():
            db.execute("DELETE FROM dup_groups")
            for digest, members in groups.items():
                manual = old_keepers.get(digest)
                ids = {m["id"] for m in members}
                keeper = manual if manual in ids else pick_keeper(members, cfg.path_template)["id"]
                gid = db.execute(
                    "INSERT INTO dup_groups(sha256, manual_keeper) VALUES(?,?)",
                    (digest, manual if manual in ids else None),
                ).lastrowid
                for m in members:
                    db.execute("INSERT INTO dup_members VALUES(?,?,?)", (gid, m["id"], int(m["id"] == keeper)))
    return {"groups": len(groups), "hashed": hashed, "errors": errors, "cancelled": cancelled}


def list_groups(db) -> list[dict]:
    out = []
    for g in db.query("SELECT * FROM dup_groups ORDER BY id"):
        members = db.query(
            "SELECT f.id, f.rel_path, f.size, f.mtime_ns, f.kind, f.root_id, r.path AS root_path, m.is_keeper, "
            "COALESCE(f.manual_taken_at, f.taken_at) AS eff_date FROM dup_members m "
            "JOIN files f ON f.id=m.file_id JOIN roots r ON r.id=f.root_id WHERE m.group_id=? ORDER BY f.id",
            (g["id"],),
        )
        out.append({"id": g["id"], "sha256": g["sha256"], "size": members[0]["size"] if members else 0,
                    "members": [dict(m) for m in members]})
    return out


def set_keeper(db, group_id: int, file_id: int) -> None:
    if not db.one("SELECT 1 FROM dup_members WHERE group_id=? AND file_id=?", (group_id, file_id)):
        raise ValueError("файл не входит в группу")
    with db.tx():
        db.execute("UPDATE dup_members SET is_keeper=(file_id=?) WHERE group_id=?", (file_id, group_id))
        db.execute("UPDATE dup_groups SET manual_keeper=? WHERE id=?", (file_id, group_id))


def quarantine_file(db, file_id: int, plan_id=None, note=None) -> int | None:
    """Переносит файл в <корень>/_duplicates. Возвращает id операции (или None при ошибке)."""
    f = catalog.get_file(db, file_id)
    if f is None or f["status"] != "present" or not os.path.exists(f["abs"]):
        return None
    name = f["rel_path"].rsplit("/", 1)[-1]
    dst_abs = unique_path(os.path.join(f["root_path"], QUARANTINE_DIR, name))
    dst_rel = os.path.relpath(dst_abs, f["root_path"]).replace(os.sep, "/")
    op_id = journal.record(db, "quarantine", file_id, f["root_id"], f["rel_path"], f["root_id"], dst_rel,
                           plan_id=plan_id, sha256=f["sha256"], size=f["size"], mtime_ns=f["mtime_ns"], note=note)
    return op_id if journal.perform(db, op_id) == "done" else None


def process_group(db, group_id: int) -> dict:
    members = db.query("SELECT file_id, is_keeper FROM dup_members WHERE group_id=?", (group_id,))
    if not members:
        raise KeyError(group_id)
    if sum(m["is_keeper"] for m in members) != 1:
        raise ValueError("в группе не выбран оригинал")
    moved, failed, roots = 0, 0, set()
    for m in members:
        if m["is_keeper"]:
            continue
        f = catalog.get_file(db, m["file_id"])
        if quarantine_file(db, m["file_id"]) is None:
            failed += 1
        else:
            moved += 1
            roots.add(f["root_id"])
    for rid in roots:
        catalog.recompute_groups(db, rid)
    db.execute("DELETE FROM dup_groups WHERE id=?", (group_id,))
    return {"quarantined": moved, "failed": failed}


def list_quarantine(db) -> list[dict]:
    rows = db.query(
        "SELECT f.id, f.rel_path, f.size, f.root_id, r.path AS root_path FROM files f JOIN roots r ON r.id=f.root_id "
        "WHERE f.status='quarantined' ORDER BY f.id"
    )
    return [dict(r) for r in rows]


def restore_file(db, file_id: int) -> dict:
    """Возвращает файл из карантина на прежний путь, если он свободен."""
    f = catalog.get_file(db, file_id)
    if f is None or f["status"] != "quarantined":
        return {"file_id": file_id, "restored": False, "reason": "файл не в карантине"}
    op = db.one(
        "SELECT * FROM operations WHERE file_id=? AND kind='quarantine' AND status='done' ORDER BY id DESC LIMIT 1",
        (file_id,),
    )
    if op is None:
        return {"file_id": file_id, "restored": False, "reason": "нет записи о карантине"}
    target = catalog.abs_path(catalog.root_path(db, op["src_root_id"]), op["src_rel"])
    if os.path.lexists(target):
        return {"file_id": file_id, "restored": False, "reason": "прежний путь занят"}
    op_id = journal.record(db, "restore", file_id, f["root_id"], f["rel_path"], op["src_root_id"], op["src_rel"],
                           sha256=f["sha256"], size=f["size"], mtime_ns=f["mtime_ns"])
    ok = journal.perform(db, op_id) == "done"
    if ok:
        catalog.recompute_groups(db, op["src_root_id"])
    return {"file_id": file_id, "restored": ok, "reason": None if ok else "ошибка перемещения"}
