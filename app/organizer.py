"""План и выполнение перемещения файлов в структуру ГГГГ/ММ/ДД."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .catalog import ISO, recompute_groups
from .config import QUARANTINE_DIR, UNDATED_DIR, Settings
from .db import Database
from .errors import ChangedError
from .hashing import ensure_hash, file_abs_path, sha256_file
from .jobs import JobContext
from . import ops
from .scanner import add_root


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def dest_folder(effective_at: str | None, settings: Settings) -> str:
    """Относительная папка назначения; граница суток сдвигает ночные кадры на предыдущий день."""
    if not effective_at:
        return UNDATED_DIR
    dt = datetime.strptime(effective_at, ISO) - timedelta(hours=settings.day_boundary_hour)
    return settings.path_template.format(YYYY=f"{dt.year:04d}", MM=f"{dt.month:02d}", DD=f"{dt.day:02d}")


def _norm(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def build_plan(db: Database, file_ids: list[int], dest_path: str, settings: Settings) -> dict:
    """Строит и сохраняет план (ничего не перемещает)."""
    dest = Path(dest_path)
    dest.mkdir(parents=True, exist_ok=True)
    root = add_root(db, str(dest))
    dest_root = Path(root["path"])
    c = db.conn()

    selected: dict[int, object] = {}
    for fid in file_ids:
        r = c.execute("SELECT * FROM files WHERE id=? AND status='present'", (fid,)).fetchone()
        if r:
            selected[r["id"]] = r
    # Связанные файлы едут вместе с выбранным.
    for r in list(selected.values()):
        if r["group_key"]:
            for m in c.execute("SELECT * FROM files WHERE group_key=? AND status='present'", (r["group_key"],)):
                selected.setdefault(m["id"], m)

    units: dict[str, list] = {}
    for r in sorted(selected.values(), key=lambda r: r["id"]):
        units.setdefault(r["group_key"] or f"single:{r['id']}", []).append(r)

    claimed: dict[str, int] = {}   # dst -> file_id (уже занято другими элементами плана)
    items: list[dict] = []

    def taken(p: Path) -> bool:
        return p.exists() or _norm(p) in claimed

    def hash_of_src(r) -> str:
        return ensure_hash(c, c.execute("SELECT * FROM files WHERE id=?", (r["id"],)).fetchone())

    for members in units.values():
        folder = dest_root.joinpath(*dest_folder(members[0]["effective_at"], settings).split("/"))
        srcs = {m["id"]: file_abs_path(c, m) for m in members}
        if len(members) == 1:
            m = members[0]
            src, name = srcs[m["id"]], Path(m["rel_path"]).name
            dst = folder / name
            if _norm(dst) == _norm(src):
                items.append(dict(file_id=m["id"], src=str(src), dst=str(dst), action="skip",
                                  note="уже на месте", size=m["size"]))
                continue
            if not taken(dst):
                claimed[_norm(dst)] = m["id"]
                items.append(dict(file_id=m["id"], src=str(src), dst=str(dst), action="move", note="", size=m["size"]))
                continue
            # Конфликт имени: тот же хеш — дубль (в карантин), иначе суффикс.
            my_hash = hash_of_src(m)
            other_hash = None
            if dst.exists():
                other_hash = sha256_file(dst)
            elif claimed.get(_norm(dst)):
                other = c.execute("SELECT * FROM files WHERE id=?", (claimed[_norm(dst)],)).fetchone()
                other_hash = hash_of_src(other)
            if other_hash == my_hash:
                q = ops.unique_path(dest_root / QUARANTINE_DIR / f"{my_hash[:8]}_{name}", taken)
                claimed[_norm(q)] = m["id"]
                items.append(dict(file_id=m["id"], src=str(src), dst=str(q), action="quarantine",
                                  note="в целевой папке уже есть такой файл", size=m["size"]))
            else:
                alt = ops.unique_path(dst, taken)
                claimed[_norm(alt)] = m["id"]
                items.append(dict(file_id=m["id"], src=str(src), dst=str(alt), action="move",
                                  note=f"имя занято, новое имя {alt.name}", size=m["size"]))
            continue
        # Группа: единый суффикс для всех файлов, чтобы имена без расширения оставались общими.
        in_place = [(_norm(folder / Path(m["rel_path"]).name) == _norm(srcs[m["id"]])) for m in members]
        if all(in_place):
            for m in members:
                items.append(dict(file_id=m["id"], src=str(srcs[m["id"]]), dst=str(srcs[m["id"]]),
                                  action="skip", note="уже на месте", size=m["size"]))
            continue
        n = 0
        while True:
            suffix = f"_{n}" if n else ""
            dsts = {m["id"]: folder / f"{Path(m['rel_path']).stem}{suffix}{Path(m['rel_path']).suffix}" for m in members}
            if not any(taken(d) and _norm(d) != _norm(srcs[i]) for i, d in dsts.items()):
                break
            n += 1
        for m in members:
            d = dsts[m["id"]]
            claimed[_norm(d)] = m["id"]
            items.append(dict(file_id=m["id"], src=str(srcs[m["id"]]), dst=str(d),
                              action="skip" if _norm(d) == _norm(srcs[m["id"]]) else "move",
                              note=f"имя занято, суффикс {suffix}" if n else "", size=m["size"]))

    dest_dev = os.stat(dest_root).st_dev
    need = sum(i["size"] for i in items if i["action"] != "skip" and os.stat(Path(i["src"]).parent).st_dev != dest_dev)
    free = shutil.disk_usage(dest_root).free
    move_items = [i for i in items if i["action"] != "skip"]
    summary = {
        "files": len(move_items), "bytes": sum(i["size"] for i in move_items),
        "skipped": len(items) - len(move_items),
        "conflicts": sum(1 for i in items if i["action"] == "quarantine" or "занято" in (i["note"] or "")),
        "quarantine": sum(1 for i in items if i["action"] == "quarantine"),
        "undated": sum(1 for i in move_items if Path(i["dst"]).parent.name == UNDATED_DIR),
        "need_bytes": need, "free_bytes": free, "enough_space": free >= need * 1.01,
    }
    with db.tx() as tc:
        pid = tc.execute("INSERT INTO plans(dest_root_id,dest_path,status,created,summary) VALUES (?,?,?,?,?)",
                         (root["id"], str(dest_root), "draft", _now(), json.dumps(summary))).lastrowid
        tc.executemany("INSERT INTO plan_items(plan_id,file_id,src,dst,action,note,size) VALUES (?,?,?,?,?,?,?)",
                       [(pid, i["file_id"], i["src"], i["dst"], i["action"], i["note"], i["size"]) for i in items])
    return {"id": pid, "status": "draft", "dest_path": str(dest_root), "summary": summary}


def get_plan(db: Database, plan_id: int, limit: int = 200, offset: int = 0) -> dict | None:
    c = db.conn()
    p = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if p is None:
        return None
    items = c.execute("SELECT * FROM plan_items WHERE plan_id=? ORDER BY id LIMIT ? OFFSET ?",
                      (plan_id, limit, offset)).fetchall()
    return {"id": p["id"], "status": p["status"], "dest_path": p["dest_path"], "created": p["created"],
            "summary": json.loads(p["summary"] or "{}"), "items": [dict(i) for i in items]}


def execute_plan(db: Database, plan_id: int, confirmed: bool, ctx: JobContext | None = None) -> dict:
    if not confirmed:
        raise ValueError("Выполнение требует явного подтверждения плана")
    c = db.conn()
    plan = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if plan is None:
        raise ValueError("План не найден")
    if plan["status"] != "draft":
        raise ValueError(f"План уже в статусе {plan['status']}")
    items = c.execute("SELECT * FROM plan_items WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
    todo = [i for i in items if i["action"] != "skip"]
    if ctx:
        ctx.set_total(bytes=sum(i["size"] or 0 for i in todo), files=len(todo))
    c.execute("UPDATE plans SET status='running' WHERE id=?", (plan_id,))
    dest_root_id = plan["dest_root_id"]
    dest_root = c.execute("SELECT path FROM roots WHERE id=?", (dest_root_id,)).fetchone()["path"]
    res = {"moved": 0, "quarantined": 0, "skipped": len(items) - len(todo), "errors": [], "cancelled": False}
    touched_roots = {dest_root_id}
    for it in todo:
        if ctx and ctx.cancelled():
            res["cancelled"] = True
            break
        row = c.execute("SELECT root_id FROM files WHERE id=?", (it["file_id"],)).fetchone()
        if row:
            touched_roots.add(row["root_id"])
        rel = Path(os.path.relpath(it["dst"], dest_root)).as_posix()
        progress = ctx.add if ctx else None
        try:
            if it["action"] == "quarantine":
                ops.move_file(db, it["file_id"], dest_root_id, rel, kind="quarantine",
                              new_status="quarantined", plan_id=plan_id, progress=progress)
                res["quarantined"] += 1
            else:
                ops.move_file(db, it["file_id"], dest_root_id, rel, plan_id=plan_id, progress=progress)
                res["moved"] += 1
        except ChangedError as e:
            res["skipped"] += 1
            res["errors"].append({"src": it["src"], "error": f"пропущен: {e}"})
        except Exception as e:  # noqa: BLE001 - ошибка одного файла не останавливает план
            res["errors"].append({"src": it["src"], "error": f"{type(e).__name__}: {e}"})
        if ctx:
            ctx.add(files=1)
    c.execute("UPDATE plans SET status=? WHERE id=?", ("partial" if res["cancelled"] else "done", plan_id))
    for rid in touched_roots:
        recompute_groups(db, rid)
    return res


def rollback_plan(db: Database, plan_id: int, ctx: JobContext | None = None) -> dict:
    c = db.conn()
    done = c.execute("SELECT id FROM operations WHERE plan_id=? AND status='done' "
                     "AND kind IN ('move','quarantine') ORDER BY id DESC", (plan_id,)).fetchall()
    if ctx:
        ctx.set_total(files=len(done))
    res = {"rolled_back": 0, "skipped": [], "cancelled": False}
    roots = set()
    for op in done:
        if ctx and ctx.cancelled():
            res["cancelled"] = True
            break
        try:
            outcome = ops.rollback_op(db, op["id"])
        except Exception as e:  # noqa: BLE001
            outcome = f"{type(e).__name__}: {e}"
        if outcome == "rolled_back":
            res["rolled_back"] += 1
        else:
            res["skipped"].append({"op": op["id"], "reason": outcome})
        if ctx:
            ctx.add(files=1)
    for r in c.execute("SELECT id FROM roots"):
        roots.add(r["id"])
    for rid in roots:
        recompute_groups(db, rid)
    left = c.execute("SELECT COUNT(*) FROM operations WHERE plan_id=? AND status='done' "
                     "AND kind IN ('move','quarantine')", (plan_id,)).fetchone()[0]
    c.execute("UPDATE plans SET status=? WHERE id=?", ("rolled_back" if left == 0 else "partial", plan_id))
    return res
