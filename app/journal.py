"""Журнал операций: запись до и после перемещения, восстановление после сбоя."""
import os
from datetime import datetime

from . import catalog
from .fileops import HashMismatch, move_file, sha256_file


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def op_paths(db, op) -> tuple[str, str]:
    src = catalog.abs_path(catalog.root_path(db, op["src_root_id"]), op["src_rel"])
    dst = catalog.abs_path(catalog.root_path(db, op["dst_root_id"]), op["dst_rel"])
    return src, dst


def record(db, kind, file_id, src_root_id, src_rel, dst_root_id, dst_rel, *, plan_id=None, sha256=None,
           size=None, mtime_ns=None, status="pending", conflict=0, note=None) -> int:
    cur = db.execute(
        "INSERT INTO operations(plan_id, kind, file_id, src_root_id, src_rel, dst_root_id, dst_rel, sha256, size, "
        "mtime_ns, conflict, status, note, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plan_id, kind, file_id, src_root_id, src_rel, dst_root_id, dst_rel, sha256, size, mtime_ns, conflict,
         status, note, now()),
    )
    return cur.lastrowid


def set_status(db, op_id: int, status: str, note: str | None = None) -> None:
    db.execute("UPDATE operations SET status=?, note=COALESCE(?, note), ts=? WHERE id=?", (status, note, now(), op_id))


def finalize(db, op, dst_path: str | None = None) -> None:
    """Отражает завершённую операцию в каталоге и помечает её выполненной (одной транзакцией)."""
    dst_root = op["dst_root_id"]
    new_status = "quarantined" if op["kind"] == "quarantine" else "present"
    size = mtime = None
    if dst_path is None:
        dst_path = catalog.abs_path(catalog.root_path(db, dst_root), op["dst_rel"])
    try:
        st = os.stat(dst_path)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        pass
    with db.tx():
        db.execute(
            "DELETE FROM files WHERE root_id=? AND rel_path=? AND id!=? AND status='missing'",
            (dst_root, op["dst_rel"], op["file_id"]),
        )
        db.execute(
            "UPDATE files SET root_id=?, rel_path=?, status=?, group_key=NULL WHERE id=?",
            (dst_root, op["dst_rel"], new_status, op["file_id"]),
        )
        if mtime is not None:  # копия между томами могла сохранить mtime с иной точностью
            db.execute(
                "UPDATE files SET mtime_ns=?, hash_mtime_ns=CASE WHEN sha256 IS NOT NULL THEN ? END WHERE id=? "
                "AND mtime_ns!=?",
                (mtime, mtime, op["file_id"], mtime),
            )
        db.execute("UPDATE operations SET status='done', ts=? WHERE id=?", (now(), op["id"]))


def perform(db, op_id: int, expected_sha: str | None = None, on_bytes=None) -> str:
    """Выполняет запись журнала со статусом pending. Возвращает 'done' или 'failed'."""
    op = db.one("SELECT * FROM operations WHERE id=?", (op_id,))
    src, dst = op_paths(db, op)
    try:
        move_file(src, dst, expected_sha or op["sha256"], on_bytes)
    except HashMismatch:
        set_status(db, op_id, "failed", "hash_mismatch: копия удалена, оригинал сохранён")
        return "failed"
    except OSError as exc:
        set_status(db, op_id, "failed", f"{type(exc).__name__}: {exc}")
        return "failed"
    finalize(db, op, dst)
    return "done"


def recover(db) -> dict:
    """При запуске доводит до конца или отменяет операции, прерванные сбоем."""
    result = {"completed": 0, "aborted": 0, "failed": 0}
    for op in db.query("SELECT * FROM operations WHERE status='pending' ORDER BY id"):
        try:
            src, dst = op_paths(db, op)
        except KeyError:
            set_status(db, op["id"], "failed", "корень не найден")
            result["failed"] += 1
            continue
        for stale in (dst + ".part",):
            if os.path.exists(stale):
                os.remove(stale)
        src_ok, dst_ok = os.path.exists(src), os.path.exists(dst)
        if src_ok and not dst_ok:
            set_status(db, op["id"], "aborted", "прервана до начала перемещения")
            result["aborted"] += 1
        elif dst_ok and not src_ok:
            finalize(db, op, dst)
            result["completed"] += 1
        elif src_ok and dst_ok:  # копия готова, оригинал ещё не удалён
            expected = op["sha256"] or sha256_file(src)
            if sha256_file(dst) == expected:
                os.remove(src)
                finalize(db, op, dst)
                result["completed"] += 1
            else:
                os.remove(dst)
                set_status(db, op["id"], "aborted", "копия не совпала по хешу и удалена")
                result["aborted"] += 1
        else:
            set_status(db, op["id"], "failed", "файл не найден ни на старом, ни на новом месте")
            result["failed"] += 1
    return result
