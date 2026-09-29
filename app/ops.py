"""Безопасные файловые операции с журналом: перемещение, карантин, восстановление, откат."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import QUARANTINE_DIR
from .db import Database
from .errors import ChangedError, IntegrityError
from .hashing import CHUNK, ensure_hash, file_abs_path, sha256_file

import hashlib


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def unique_path(dst: Path, taken: Callable[[Path], bool] | None = None) -> Path:
    """Если имя занято, добавляет суффикс _1, _2 ..."""
    taken = taken or (lambda p: p.exists())
    if not taken(dst):
        return dst
    n = 1
    while True:
        cand = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
        if not taken(cand):
            return cand
        n += 1


def same_volume(src: Path, dst_parent: Path) -> bool:
    return os.stat(src).st_dev == os.stat(dst_parent).st_dev


def safe_move(src: Path, dst: Path, expected_sha: str | None = None,
              progress: Callable[[int], None] | None = None) -> str | None:
    """Перемещение без потери данных.

    Тот же том: переименование. Другой том: копия во временный файл, сверка SHA-256
    копии с хешем источника, атомарная подмена и только потом удаление оригинала.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(str(dst))
    if same_volume(src, dst.parent):
        size = src.stat().st_size
        os.rename(src, dst)
        if progress:
            progress(size)
        return expected_sha
    tmp = dst.with_name(dst.name + ".pmtmp")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                chunk = fi.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                fo.write(chunk)
                if progress:
                    progress(len(chunk))
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, tmp)
        src_sha = h.hexdigest()
        if expected_sha and expected_sha != src_sha:
            raise IntegrityError("Источник изменился: хеш не совпал с каталогом")
        if sha256_file(tmp) != src_sha:
            raise IntegrityError("Хеш копии не совпал с оригиналом")
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.unlink(src)
    return src_sha


# ---------- журнал ----------

def journal_begin(db: Database, plan_id, kind, file_id, src, dst, sha, detail=None) -> int:
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO operations(plan_id,kind,file_id,src,dst,sha256,status,ts,detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (plan_id, kind, file_id, str(src), str(dst), sha, "pending", _now(),
             json.dumps(detail or {}, ensure_ascii=False)))
        return cur.lastrowid


def journal_finish(db: Database, op_id: int, status: str, extra: dict | None = None) -> None:
    c = db.conn()
    row = c.execute("SELECT detail FROM operations WHERE id=?", (op_id,)).fetchone()
    detail = json.loads(row["detail"] or "{}")
    detail.update(extra or {})
    c.execute("UPDATE operations SET status=?, ts=?, detail=? WHERE id=?",
              (status, _now(), json.dumps(detail, ensure_ascii=False), op_id))


def _rel(root_path: str, abs_path: Path) -> str:
    return Path(os.path.relpath(abs_path, root_path)).as_posix()


def _mark_file(db: Database, file_id: int, root_id: int, rel: str, status: str) -> None:
    db.conn().execute("UPDATE files SET root_id=?, rel_path=?, status=? WHERE id=?",
                      (root_id, rel, status, file_id))


def move_file(db: Database, file_id: int, dest_root_id: int, dest_rel: str, kind: str = "move",
              new_status: str = "present", plan_id: int | None = None,
              progress: Callable[[int], None] | None = None) -> int:
    """Перемещает файл каталога с журналом. Возвращает id операции."""
    c = db.conn()
    row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        raise ChangedError("Файл не найден в каталоге")
    src = file_abs_path(c, row)
    try:
        st = src.stat()
    except OSError as e:
        raise ChangedError(f"Файл недоступен: {e}") from e
    if st.st_size != row["size"] or st.st_mtime_ns != row["mtime_ns"]:
        raise ChangedError("Размер или время изменения не совпадают с каталогом")
    root = c.execute("SELECT path FROM roots WHERE id=?", (dest_root_id,)).fetchone()
    dst = Path(root["path"]).joinpath(*dest_rel.split("/"))
    detail = {"src_root_id": row["root_id"], "src_rel": row["rel_path"], "dst_root_id": dest_root_id,
              "dst_rel": dest_rel, "prev_status": row["status"], "new_status": new_status,
              "size": row["size"], "mtime_ns": row["mtime_ns"]}
    op = journal_begin(db, plan_id, kind, file_id, src, dst, row["sha256"], detail)
    try:
        sha = safe_move(src, dst, row["sha256"] if row["hash_size"] == row["size"] and
                        row["hash_mtime_ns"] == row["mtime_ns"] else None, progress)
    except BaseException as e:
        journal_finish(db, op, "error", {"error": f"{type(e).__name__}: {e}"})
        raise
    try:
        new_st = dst.stat()
        with db.tx() as tc:
            tc.execute("UPDATE files SET root_id=?, rel_path=?, status=?, mtime_ns=?, "
                       "sha256=COALESCE(?, sha256), hash_size=CASE WHEN ? IS NULL THEN hash_size ELSE ? END, "
                       "hash_mtime_ns=CASE WHEN ? IS NULL THEN hash_mtime_ns ELSE ? END WHERE id=?",
                       (dest_root_id, dest_rel, new_status, new_st.st_mtime_ns, sha,
                        sha, new_st.st_size, sha, new_st.st_mtime_ns, file_id))
        journal_finish(db, op, "done", {"dst_size": new_st.st_size, "dst_mtime_ns": new_st.st_mtime_ns})
    except BaseException as e:
        journal_finish(db, op, "error", {"error": f"db update failed: {e}"})
        raise
    return op


def quarantine_file(db: Database, file_id: int, dest_root_id: int | None = None,
                    plan_id: int | None = None, progress=None) -> int:
    """Перенос в `_duplicates` корня (по умолчанию — корня самого файла)."""
    c = db.conn()
    row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    root_id = dest_root_id or row["root_id"]
    sha = ensure_hash(c, row)
    root = c.execute("SELECT path FROM roots WHERE id=?", (root_id,)).fetchone()
    name = Path(row["rel_path"]).name
    base = Path(root["path"]) / QUARANTINE_DIR / f"{sha[:8]}_{name}"
    dst = unique_path(base)
    return move_file(db, file_id, root_id, _rel(root["path"], dst), kind="quarantine",
                     new_status="quarantined", plan_id=plan_id, progress=progress)


def restore_file(db: Database, file_id: int) -> int:
    """Возвращает файл из карантина на прежний путь (если он свободен)."""
    c = db.conn()
    op = c.execute("SELECT * FROM operations WHERE file_id=? AND kind='quarantine' AND status='done' "
                   "ORDER BY id DESC LIMIT 1", (file_id,)).fetchone()
    if op is None:
        raise ChangedError("В журнале нет записи о переносе в карантин")
    d = json.loads(op["detail"])
    root = c.execute("SELECT path FROM roots WHERE id=?", (d["src_root_id"],)).fetchone()
    if root is None:
        raise ChangedError("Исходный корень удалён")
    target = Path(root["path"]).joinpath(*d["src_rel"].split("/"))
    if target.exists():
        raise FileExistsError(str(target))
    return move_file(db, file_id, d["src_root_id"], d["src_rel"], kind="restore", new_status="present")


def rollback_op(db: Database, op_id: int) -> str:
    """Откат выполненной операции. Возвращает 'rolled_back' или причину пропуска."""
    c = db.conn()
    op = c.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
    d = json.loads(op["detail"] or "{}")
    dst, src = Path(op["dst"]), Path(op["src"])
    if not dst.exists():
        return "файл на новом месте не найден"
    st = dst.stat()
    if st.st_size != d.get("dst_size") or st.st_mtime_ns != d.get("dst_mtime_ns"):
        return "файл на новом месте изменён"
    if src.exists():
        return "прежний путь занят"
    safe_move(dst, src, op["sha256"])
    back = src.stat()
    with db.tx() as tc:
        tc.execute("UPDATE files SET root_id=?, rel_path=?, status=?, mtime_ns=? WHERE id=?",
                   (d["src_root_id"], d["src_rel"], d.get("prev_status") or "present", back.st_mtime_ns,
                    op["file_id"]))
    journal_finish(db, op_id, "rolled_back")
    return "rolled_back"


def recover_pending(db: Database) -> dict:
    """Довод или отмена операций, прерванных сбоем (запись 'pending' в журнале)."""
    c = db.conn()
    stats = {"completed": 0, "cancelled": 0, "failed": 0}
    for op in c.execute("SELECT * FROM operations WHERE status='pending' AND kind IN ('move','quarantine','restore')").fetchall():
        d = json.loads(op["detail"] or "{}")
        src, dst = Path(op["src"]), Path(op["dst"])
        tmp = dst.with_name(dst.name + ".pmtmp")
        try:
            tmp.unlink()
        except OSError:
            pass
        if dst.exists() and not src.exists():
            ok = True
        elif dst.exists() and src.exists():
            # Копия подменена, но оригинал не удалён: удаляем оригинал только при совпадении хеша.
            ok = bool(op["sha256"]) and sha256_file(dst) == op["sha256"]
            if ok:
                os.unlink(src)
        else:
            journal_finish(db, op["id"], "cancelled")
            stats["cancelled"] += 1
            continue
        if not ok:
            journal_finish(db, op["id"], "error", {"error": "после сбоя хеш копии не подтверждён"})
            stats["failed"] += 1
            continue
        st = dst.stat()
        _mark_file(db, op["file_id"], d["dst_root_id"], d["dst_rel"], d.get("new_status", "present"))
        c.execute("UPDATE files SET size=?, mtime_ns=? WHERE id=?", (st.st_size, st.st_mtime_ns, op["file_id"]))
        journal_finish(db, op["id"], "done", {"dst_size": st.st_size, "dst_mtime_ns": st.st_mtime_ns,
                                              "recovered": True})
        stats["completed"] += 1
    return stats
