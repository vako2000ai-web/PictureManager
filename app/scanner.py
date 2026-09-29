import os

from . import catalog
from .config import QUARANTINE_DIR
from .metadata import iso  # noqa: F401

from datetime import datetime


def scan_root(db, cfg, root_id: int, ctx) -> dict:
    """Рекурсивный инкрементальный скан корня (запускается как фоновая задача)."""
    root = catalog.get_root(db, root_id)
    base = root["path"]
    if not os.path.isdir(base):
        raise FileNotFoundError(f"корень недоступен: {base}")

    report = {"found": 0, "new": 0, "updated": 0, "unchanged": 0, "missing": 0,
              "skipped_unsupported": 0, "skipped_by_ext": {}, "errors": []}

    def on_error(err: OSError) -> None:
        report["errors"].append(f"{err.filename}: {err.strerror or err}")

    entries = []  # (rel_path, size, mtime_ns)
    for dirpath, dirnames, filenames in os.walk(base, onerror=on_error):
        dirnames[:] = [d for d in dirnames if d != QUARANTINE_DIR]
        if ctx.cancelled:
            break
        for name in filenames:
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if cfg.kind_of(ext) is None:
                if not name.endswith(".part"):
                    report["skipped_unsupported"] += 1
                    key = ext or "(без расширения)"
                    report["skipped_by_ext"][key] = report["skipped_by_ext"].get(key, 0) + 1
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError as exc:
                report["errors"].append(f"{full}: {exc.strerror or exc}")
                continue
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            entries.append((rel, st))

    ctx.set_total(files=len(entries), bytes=sum(st.st_size for _, st in entries))
    known = {r["rel_path"]: r for r in db.query("SELECT * FROM files WHERE root_id=?", (root_id,))}
    seen = set()
    completed = not ctx.cancelled

    for rel, st in entries:
        if ctx.cancelled:
            completed = False
            break
        row = known.get(rel)
        seen.add(rel)
        report["found"] += 1
        if row is not None and row["size"] == st.st_size and row["mtime_ns"] == st.st_mtime_ns:
            if row["status"] == "missing":
                db.execute("UPDATE files SET status='present' WHERE id=?", (row["id"],))
            report["unchanged"] += 1
        else:
            try:
                res = catalog.index_file(db, cfg, root_id, base, rel, st, row)
                report[res] += 1
            except OSError as exc:
                report["errors"].append(f"{rel}: {exc.strerror or exc}")
        ctx.advance(files=1, bytes=st.st_size)

    if completed:  # исчезнувшие файлы: статус missing, записи не удаляются
        for rel, row in known.items():
            if rel not in seen and row["status"] == "present":
                db.execute("UPDATE files SET status='missing' WHERE id=?", (row["id"],))
                report["missing"] += 1
        db.execute("UPDATE roots SET last_scan=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), root_id))
    catalog.recompute_groups(db, root_id)
    report["cancelled"] = not completed
    return report
