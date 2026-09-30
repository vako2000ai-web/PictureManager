"""Удаление выбранных файлов: в карантин (восстановимо) или навсегда."""
import os

from . import catalog, dupes, journal


def delete_files(db, file_ids: list[int], permanent: bool = False) -> dict:
    done, failed, roots = 0, [], set()
    for fid in file_ids:
        f = catalog.get_file(db, fid)
        if f is None or f["status"] not in ("present", "missing", "quarantined"):
            failed.append({"file_id": fid, "reason": "файл не найден"})
            continue
        if not permanent:
            if f["status"] != "present" or dupes.quarantine_file(db, fid, note="удалено пользователем") is None:
                failed.append({"file_id": fid, "reason": "не удалось перенести в карантин"})
                continue
        else:
            try:
                if os.path.exists(f["abs"]):
                    os.remove(f["abs"])
            except OSError as exc:
                failed.append({"file_id": fid, "reason": exc.strerror or str(exc)})
                continue
            journal.record(db, "delete", fid, f["root_id"], f["rel_path"], None, None, sha256=f["sha256"],
                           size=f["size"], mtime_ns=f["mtime_ns"], status="done", note="удалено навсегда")
            with db.tx():  # производные записи и членство в дублях удаляются каскадом
                db.execute("DELETE FROM files WHERE id=?", (fid,))
        done += 1
        roots.add(f["root_id"])
    for rid in roots:
        catalog.recompute_groups(db, rid)
    return {"deleted": done, "permanent": permanent, "failed": failed}
