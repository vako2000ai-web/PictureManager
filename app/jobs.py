import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

FINISHED = ("done", "cancelled", "failed", "interrupted")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobContext:
    """Передаётся задаче: прогресс, отмена, итоговый отчёт."""

    def __init__(self, manager: "JobManager", job_id: int):
        self._manager = manager
        self.job_id = job_id
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self.state = {
            "id": job_id, "status": "queued", "files_done": 0, "files_total": 0,
            "bytes_done": 0, "bytes_total": 0, "message": "", "result": None, "error": None,
        }

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def set_total(self, files: int | None = None, bytes: int | None = None) -> None:
        with self._lock:
            if files is not None:
                self.state["files_total"] = files
            if bytes is not None:
                self.state["bytes_total"] = bytes
        self._flush()

    def advance(self, files: int = 0, bytes: int = 0) -> None:
        with self._lock:
            self.state["files_done"] += files
            self.state["bytes_done"] += bytes
        self._flush()

    def message(self, text: str) -> None:
        self.state["message"] = text
        self._flush()

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_flush > 0.5:
            self._last_flush = now
            self._manager._persist(self)


class JobManager:
    def __init__(self, db, workers: int = 4):
        self.db = db
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._contexts: dict[int, JobContext] = {}
        self._futures: dict[int, object] = {}
        self.db.execute(
            "UPDATE jobs SET status='interrupted', finished=? WHERE status IN ('queued','running')",
            (_now(),),
        )

    def submit(self, kind: str, fn, message: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO jobs(kind, status, message, created) VALUES(?, 'queued', ?, ?)",
            (kind, message, _now()),
        )
        job_id = cur.lastrowid
        ctx = JobContext(self, job_id)
        ctx.state["message"] = message
        self._contexts[job_id] = ctx
        self._futures[job_id] = self._pool.submit(self._run, ctx, fn)
        return job_id

    def _run(self, ctx: JobContext, fn) -> None:
        ctx.state["status"] = "running"
        self.db.execute("UPDATE jobs SET status='running', started=? WHERE id=?", (_now(), ctx.job_id))
        try:
            result = fn(ctx)
            ctx.state["result"] = result
            ctx.state["status"] = "cancelled" if ctx.cancelled else "done"
        except Exception as exc:  # noqa: BLE001 - задача не должна ронять пул
            ctx.state["status"] = "failed"
            ctx.state["error"] = f"{type(exc).__name__}: {exc}"
        self._persist(ctx, finished=True)

    def _persist(self, ctx: JobContext, finished: bool = False) -> None:
        s = ctx.state
        self.db.execute(
            "UPDATE jobs SET status=?, files_done=?, files_total=?, bytes_done=?, bytes_total=?, "
            "message=?, result=?, error=?, finished=COALESCE(?, finished) WHERE id=?",
            (s["status"], s["files_done"], s["files_total"], s["bytes_done"], s["bytes_total"],
             s["message"], json.dumps(s["result"], ensure_ascii=False) if s["result"] is not None else None,
             s["error"], _now() if finished else None, ctx.job_id),
        )

    def get(self, job_id: int) -> dict | None:
        ctx = self._contexts.get(job_id)
        if ctx is not None:
            with ctx._lock:
                return dict(ctx.state)
        row = self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return self._row_to_dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT id FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [self.get(r["id"]) for r in rows]

    def cancel(self, job_id: int) -> bool:
        ctx = self._contexts.get(job_id)
        if ctx is None or ctx.state["status"] in FINISHED:
            return False
        ctx._cancel.set()
        return True

    def wait(self, job_id: int, timeout: float = 30) -> dict:
        future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.get(job_id)

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        return d
