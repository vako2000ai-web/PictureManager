from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from .db import Database
from .errors import Cancelled

TERMINAL = ("done", "cancelled", "error", "interrupted")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobContext:
    """Передаётся в функцию задачи: прогресс, проверка отмены, результат."""

    def __init__(self, manager: "JobManager", job_id: int):
        self._m = manager
        self.job_id = job_id
        self.cancel_event = threading.Event()
        self.total_bytes = 0
        self.done_bytes = 0
        self.total_files = 0
        self.done_files = 0
        self.result: dict = {}
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def set_total(self, bytes: int | None = None, files: int | None = None) -> None:
        with self._lock:
            if bytes is not None:
                self.total_bytes = bytes
            if files is not None:
                self.total_files = files
        self.flush()

    def add(self, bytes: int = 0, files: int = 0, total_bytes: int = 0, total_files: int = 0) -> None:
        with self._lock:
            self.done_bytes += bytes
            self.done_files += files
            self.total_bytes += total_bytes
            self.total_files += total_files
        self.flush()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total_bytes": self.total_bytes, "done_bytes": self.done_bytes,
                "total_files": self.total_files, "done_files": self.done_files,
            }

    def flush(self, force: bool = False) -> None:
        t = time.monotonic()
        if not force and t - self._last_flush < 0.25:
            return
        self._last_flush = t
        self._m._write_progress(self)


class JobManager:
    def __init__(self, db: Database, workers: int = 2):
        self.db = db
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._ctx: dict[int, JobContext] = {}
        self._lock = threading.Lock()
        # Задачи, оставшиеся «running» после аварийного завершения, помечаем прерванными.
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET status='interrupted', updated=? WHERE status IN ('queued','running')",
                      (now_iso(),))

    def submit(self, kind: str, fn: Callable[[JobContext], dict | None]) -> int:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO jobs(kind,status,created,updated) VALUES (?,?,?,?)",
                (kind, "queued", now_iso(), now_iso()))
            job_id = cur.lastrowid
        ctx = JobContext(self, job_id)
        with self._lock:
            self._ctx[job_id] = ctx
        self._pool.submit(self._run, ctx, fn)
        return job_id

    def _run(self, ctx: JobContext, fn) -> None:
        c = self.db.conn()
        c.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (now_iso(), ctx.job_id))
        status, error = "done", None
        try:
            res = fn(ctx)
            if res:
                ctx.result.update(res)
            if ctx.result.get("cancelled"):
                status = "cancelled"
        except Cancelled:
            status = "cancelled"
        except Exception as e:  # noqa: BLE001 - задача не должна ронять пул
            status, error = "error", f"{type(e).__name__}: {e}"
            ctx.result.setdefault("traceback", traceback.format_exc())
        snap = ctx.snapshot()
        c.execute(
            "UPDATE jobs SET status=?, total_bytes=?, done_bytes=?, total_files=?, done_files=?, "
            "error=?, result=?, updated=? WHERE id=?",
            (status, snap["total_bytes"], snap["done_bytes"], snap["total_files"], snap["done_files"],
             error, json.dumps(ctx.result, ensure_ascii=False, default=str), now_iso(), ctx.job_id))
        with self._lock:
            self._ctx.pop(ctx.job_id, None)

    def _write_progress(self, ctx: JobContext) -> None:
        snap = ctx.snapshot()
        self.db.conn().execute(
            "UPDATE jobs SET total_bytes=?, done_bytes=?, total_files=?, done_files=?, updated=? WHERE id=?",
            (snap["total_bytes"], snap["done_bytes"], snap["total_files"], snap["done_files"],
             now_iso(), ctx.job_id))

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            ctx = self._ctx.get(job_id)
        if ctx is None:
            return False
        ctx.cancel_event.set()
        return True

    def get(self, job_id: int) -> dict | None:
        row = self.db.conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.db.conn().execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def wait(self, job_id: int, timeout: float = 60.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            job = self.get(job_id)
            if job and job["status"] in TERMINAL:
                return job
            time.sleep(0.02)
        raise TimeoutError(f"job {job_id} not finished")

    @staticmethod
    def _row(row) -> dict:
        d = dict(row)
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d

    def shutdown(self) -> None:
        with self._lock:
            for ctx in self._ctx.values():
                ctx.cancel_event.set()
        self._pool.shutdown(wait=True)
