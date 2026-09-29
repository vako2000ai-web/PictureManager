"""Перемещение файлов в структуру ГГГГ/ММ/ДД: план, выполнение, откат."""
import json
import os
import shutil
from datetime import datetime

from . import catalog, dupes, journal
from .config import NO_DATE_DIR, QUARANTINE_DIR
from .fileops import Cancelled, sha256_file, same_volume, suffixed, unique_path
from .hashing import cached_hash, ensure_hash
from .metadata import apply_day_boundary

MTIME_TOLERANCE_NS = 2_000_000_000  # FAT хранит время с точностью 2 с


def dest_folder(template: str, taken_at: str | None, boundary_hour: int) -> str:
    if not taken_at:
        return NO_DATE_DIR
    dt = apply_day_boundary(datetime.fromisoformat(taken_at), boundary_hour)
    return template.replace("{YYYY}", f"{dt.year:04d}").replace("{MM}", f"{dt.month:02d}").replace("{DD}", f"{dt.day:02d}")


def build_plan(db, cfg, file_ids: list[int], dest_root_id: int) -> int:
    dest_base = catalog.root_path(db, dest_root_id)
    selected = {}
    for fid in file_ids:
        f = catalog.get_file(db, fid)
        if f and f["status"] == "present":
            selected[fid] = f
    # связанные файлы едут вместе
    for f in list(selected.values()):
        if f["group_key"]:
            for r in db.query("SELECT id FROM files WHERE group_key=? AND status='present'", (f["group_key"],)):
                if r["id"] not in selected:
                    selected[r["id"]] = catalog.get_file(db, r["id"])

    groups: dict[str, list[dict]] = {}
    for f in selected.values():
        groups.setdefault(f["group_key"] or f"id{f['id']}", []).append(f)

    plan_id = db.execute(
        "INSERT INTO plans(dest_root_id, created, status) VALUES(?,?, 'draft')",
        (dest_root_id, journal.now()),
    ).lastrowid

    planned: set[str] = set()
    summary = {"files": 0, "bytes": 0, "conflicts": 0, "no_date": 0, "skipped": 0, "cross_volume_bytes": 0}
    with db.tx():
        for members in groups.values():
            primary = catalog.primary_of(members)
            folder = dest_folder(cfg.path_template, primary["eff_date"], cfg.day_boundary_hour)
            for f in sorted(members, key=lambda m: m["id"]):
                name = f["rel_path"].rsplit("/", 1)[-1]
                dst_rel = f"{folder}/{name}"
                dst_abs = catalog.abs_path(dest_base, dst_rel)
                key = os.path.normcase(dst_abs)
                status, note, conflict = "planned", None, 0
                if os.path.normcase(f["abs"]) == key:
                    status, note = "skipped", "уже на месте"
                    summary["skipped"] += 1
                else:
                    conflict = int(os.path.lexists(dst_abs) or key in planned)
                    planned.add(key)
                    summary["files"] += 1
                    summary["bytes"] += f["size"]
                    summary["conflicts"] += conflict
                    summary["no_date"] += int(folder == NO_DATE_DIR)
                    if not same_volume(f["abs"], dst_abs):
                        summary["cross_volume_bytes"] += f["size"]
                journal.record(db, "move", f["id"], f["root_id"], f["rel_path"], dest_root_id, dst_rel,
                               plan_id=plan_id, size=f["size"], mtime_ns=f["mtime_ns"], status=status,
                               conflict=conflict, note=note)
    try:
        free = shutil.disk_usage(dest_base).free
    except OSError:
        free = 0
    summary["free_bytes"] = free
    summary["enough_space"] = free >= summary["cross_volume_bytes"]
    db.execute("UPDATE plans SET summary=? WHERE id=?", (json.dumps(summary), plan_id))
    return plan_id


def get_plan(db, plan_id: int, items_limit: int = 500) -> dict | None:
    p = db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if p is None:
        return None
    ops = db.query("SELECT * FROM operations WHERE plan_id=? ORDER BY id LIMIT ?", (plan_id, items_limit))
    counts = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) AS n FROM operations WHERE plan_id=? GROUP BY status", (plan_id,))}
    return {"id": p["id"], "dest_root_id": p["dest_root_id"], "created": p["created"], "status": p["status"],
            "summary": json.loads(p["summary"] or "{}"), "counts": counts, "items": [dict(o) for o in ops]}


def _resolve_destination(db, src_file: dict, dst_abs: str):
    """('move', путь) либо ('duplicate', путь существующего файла с тем же хешем)."""
    src_hash = None
    candidate, n = dst_abs, 0
    while os.path.lexists(candidate):
        if src_hash is None:
            src_hash = ensure_hash(db, src_file)
        if sha256_file(candidate) == src_hash:
            return "duplicate", candidate
        n += 1
        candidate = suffixed(dst_abs, n)
    return "move", candidate


def _changed(f: dict) -> str | None:
    try:
        st = os.stat(f["abs"])
    except OSError:
        return "файл не найден"
    if st.st_size != f["size"] or st.st_mtime_ns != f["mtime_ns"]:
        return "размер или время изменения не совпали с каталогом"
    return None


def execute_plan(db, cfg, plan_id: int, ctx) -> dict:
    plan = db.one("SELECT * FROM plans WHERE id=?", (plan_id,))
    if plan is None or plan["status"] != "draft":
        raise ValueError("план не найден или уже выполнялся")
    db.execute("UPDATE plans SET status='running' WHERE id=?", (plan_id,))
    dest_base = catalog.root_path(db, plan["dest_root_id"])
    ops = db.query("SELECT * FROM operations WHERE plan_id=? AND status='planned' ORDER BY id", (plan_id,))
    ctx.set_total(files=len(ops), bytes=sum(o["size"] or 0 for o in ops))
    report = {"moved": 0, "quarantined_duplicates": 0, "skipped": [], "failed": [], "cancelled": False}
    roots = {plan["dest_root_id"]}

    for op in ops:
        if ctx.cancelled:
            report["cancelled"] = True
            db.execute("UPDATE operations SET status='cancelled' WHERE plan_id=? AND status='planned'", (plan_id,))
            break
        f = catalog.get_file(db, op["file_id"])
        reason = "файл не найден в каталоге" if f is None or f["status"] != "present" else _changed(f)
        if reason:  # защита от внешних изменений
            journal.set_status(db, op["id"], "skipped", reason)
            report["skipped"].append({"file_id": op["file_id"], "path": op["src_rel"], "reason": reason})
            ctx.advance(files=1, bytes=op["size"] or 0)
            continue
        roots.add(f["root_id"])
        dst_abs = catalog.abs_path(dest_base, op["dst_rel"])
        try:
            action, target = _resolve_destination(db, f, dst_abs)
        except OSError as exc:
            journal.set_status(db, op["id"], "failed", str(exc))
            report["failed"].append({"file_id": op["file_id"], "path": op["src_rel"], "reason": str(exc)})
            ctx.advance(files=1, bytes=op["size"] or 0)
            continue
        kind = "move"
        if action == "duplicate":  # тот же файл уже на месте: в карантин, без перезаписи
            kind = "quarantine"
            target = unique_path(os.path.join(dest_base, QUARANTINE_DIR, f["rel_path"].rsplit("/", 1)[-1]))
        dst_rel = os.path.relpath(target, dest_base).replace(os.sep, "/")
        db.execute(  # запись в журнал ДО выполнения
            "UPDATE operations SET kind=?, dst_rel=?, sha256=?, status='pending', ts=? WHERE id=?",
            (kind, dst_rel, cached_hash(f), journal.now(), op["id"]),
        )
        result = journal.perform(db, op["id"], on_bytes=lambda n: ctx.advance(bytes=n))
        if result == "done":
            report["quarantined_duplicates" if kind == "quarantine" else "moved"] += 1
        else:
            note = db.one("SELECT note FROM operations WHERE id=?", (op["id"],))["note"]
            report["failed"].append({"file_id": op["file_id"], "path": op["src_rel"], "reason": note})
        ctx.advance(files=1)

    for rid in roots:
        catalog.recompute_groups(db, rid)
    db.execute("UPDATE plans SET status=? WHERE id=?", ("cancelled" if report["cancelled"] else "done", plan_id))
    return report


def rollback_plan(db, plan_id: int, ctx) -> dict:
    """Возвращает файлы на прежние пути, если на новом месте они не менялись."""
    ops = db.query(
        "SELECT * FROM operations WHERE plan_id=? AND status='done' AND kind IN ('move','quarantine') ORDER BY id DESC",
        (plan_id,),
    )
    ctx.set_total(files=len(ops), bytes=sum(o["size"] or 0 for o in ops))
    report = {"restored": 0, "skipped": [], "cancelled": False}
    roots = set()
    for op in ops:
        if ctx.cancelled:
            report["cancelled"] = True
            break
        src, dst = journal.op_paths(db, op)  # src — прежнее место, dst — текущее
        reason = None
        try:
            st = os.stat(dst)
            if op["size"] is not None and st.st_size != op["size"]:
                reason = "файл на новом месте изменён"
            elif op["mtime_ns"] is not None and abs(st.st_mtime_ns - op["mtime_ns"]) > MTIME_TOLERANCE_NS:
                reason = "файл на новом месте изменён"
            elif os.path.lexists(src):
                reason = "прежний путь занят"
        except OSError:
            reason = "файл на новом месте не найден"
        if reason:
            report["skipped"].append({"file_id": op["file_id"], "path": op["dst_rel"], "reason": reason})
            ctx.advance(files=1, bytes=op["size"] or 0)
            continue
        back = journal.record(db, "restore", op["file_id"], op["dst_root_id"], op["dst_rel"], op["src_root_id"],
                              op["src_rel"], plan_id=plan_id, sha256=op["sha256"], size=op["size"],
                              mtime_ns=op["mtime_ns"])
        if journal.perform(db, back, on_bytes=lambda n: ctx.advance(bytes=n)) == "done":
            journal.set_status(db, op["id"], "rolled_back")
            report["restored"] += 1
            roots.update((op["src_root_id"], op["dst_root_id"]))
        else:
            report["skipped"].append({"file_id": op["file_id"], "path": op["dst_rel"], "reason": "ошибка перемещения"})
        ctx.advance(files=1)
    for rid in roots:
        catalog.recompute_groups(db, rid)
    if not report["skipped"] and not report["cancelled"]:
        db.execute("UPDATE plans SET status='rolled_back' WHERE id=?", (plan_id,))
    return report
