"""FastAPI-приложение: REST API + статический интерфейс на localhost."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import catalog, converter, duplicates, ops, organizer, scanner, thumbs
from .config import Settings, load_settings
from .db import Database
from .jobs import JobManager, TERMINAL

STATIC = Path(__file__).parent / "static"


class FreshStatic(StaticFiles):
    """Статика с обязательной ревалидацией: после обновления браузер не держит старый app.js."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class RootIn(BaseModel):
    path: str
    label: str | None = None


class DateIn(BaseModel):
    file_ids: list[int]
    date: str | None = None            # ISO 'YYYY-MM-DDTHH:MM:SS'; None снимает ручную дату


class KeeperIn(BaseModel):
    file_id: int


class PlanIn(BaseModel):
    dest_path: str
    file_ids: list[int] | None = None
    root_id: int | None = None
    kind: str | None = None
    undated: bool = False
    date_from: str | None = None
    date_to: str | None = None


class ExecIn(BaseModel):
    confirm: bool = False


class ConvertIn(BaseModel):
    file_ids: list[int] | None = None
    root_id: int | None = None


class IdsIn(BaseModel):
    file_ids: list[int]


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    settings: Settings = load_settings(data_dir)
    db = Database(settings.db_path)
    jobs = JobManager(db)
    recovered = ops.recover_pending(db)   # доводим операции, прерванные сбоем

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        jobs.shutdown()

    app = FastAPI(title="PictureManager", lifespan=lifespan)
    app.state.settings, app.state.db, app.state.jobs, app.state.recovered = settings, db, jobs, recovered

    def _ids_for(kind_filter: str | None, root_id, file_ids, ext=None, **flt) -> list[int]:
        if file_ids:
            return file_ids
        rows = catalog.query_files(db, limit=10_000_000, kind=kind_filter, root_id=root_id, **flt)
        if ext:
            rows = [r for r in rows if (r["ext"] or "") in ext]
        return [r["id"] for r in rows]

    # ---------- служебное ----------
    @app.get("/api/health")
    def health():
        return {"ok": True, "ffprobe": bool(settings.ffprobe_path), "ffmpeg": bool(settings.ffmpeg_path),
                "recovered": recovered}

    @app.get("/api/settings")
    def get_settings():
        return settings.to_public()

    @app.put("/api/settings")
    def put_settings(values: dict):
        try:
            settings.update(values)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return settings.to_public()

    # ---------- корни и сканирование ----------
    @app.get("/api/roots")
    def roots():
        return scanner.list_roots(db)

    @app.post("/api/roots")
    def add_root(body: RootIn):
        try:
            return scanner.add_root(db, body.path, body.label)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/roots/{root_id}")
    def del_root(root_id: int):
        scanner.delete_root(db, root_id)
        return {"ok": True}

    @app.post("/api/roots/{root_id}/scan")
    def scan(root_id: int):
        if not any(r["id"] == root_id for r in scanner.list_roots(db)):
            raise HTTPException(404, "Корень не найден")
        return {"job_id": jobs.submit("scan", lambda ctx: scanner.scan_root(db, root_id, settings, ctx))}

    # ---------- задачи ----------
    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        return {"cancelled": jobs.cancel(job_id)}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: int):
        if not jobs.get(job_id):
            raise HTTPException(404, "Задача не найдена")

        async def stream():
            last = None
            while True:
                job = jobs.get(job_id)
                payload = json.dumps({k: job[k] for k in ("id", "kind", "status", "total_bytes", "done_bytes",
                                                          "total_files", "done_files", "error", "result")},
                                     ensure_ascii=False, default=str)
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                if job["status"] in TERMINAL:
                    return
                await asyncio.sleep(0.4)

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ---------- каталог ----------
    @app.get("/api/catalog/days")
    def days(sort: str = "desc", root_id: int | None = None, kind: str | None = None,
             status: str | None = "present", undated: bool = False):
        return catalog.query_days(db, sort=sort, root_id=root_id, kind=kind, status=status, undated=undated)

    @app.get("/api/catalog/files")
    def files(day: str | None = None, sort: str = "desc", root_id: int | None = None, kind: str | None = None,
              status: str | None = "present", undated: bool = False, ext: str | None = None,
              limit: int = 200, offset: int = 0):
        return catalog.query_files(db, day=day, sort=sort, limit=min(limit, 1000), offset=offset,
                                   root_id=root_id, kind=kind, status=status, undated=undated, ext=ext)

    @app.post("/api/catalog/date")
    def set_date(body: DateIn):
        try:
            return {"updated": catalog.set_manual_date(db, body.file_ids, body.date)}
        except ValueError:
            raise HTTPException(400, "Дата должна быть в формате YYYY-MM-DDTHH:MM:SS")

    @app.get("/api/thumb/{file_id}")
    def thumb(file_id: int):
        path = thumbs.get_thumbnail(db, settings, file_id)
        if path is None:
            return Response(thumbs.PLACEHOLDER_SVG, media_type="image/svg+xml")
        return FileResponse(path, media_type="image/jpeg")

    # ---------- дубли ----------
    @app.post("/api/duplicates/search")
    def dup_search():
        return {"job_id": jobs.submit("duplicates", lambda ctx: duplicates.find_duplicates(db, ctx))}

    @app.get("/api/duplicates")
    def dup_list(limit: int = 100, offset: int = 0):
        return duplicates.list_groups(db, limit, offset)

    @app.post("/api/duplicates/{group_id}/keeper")
    def dup_keeper(group_id: int, body: KeeperIn):
        try:
            duplicates.set_keeper(db, group_id, body.file_id)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/duplicates/{group_id}/quarantine")
    def dup_quarantine(group_id: int, body: ExecIn):
        if not body.confirm:
            raise HTTPException(400, "Нужно подтверждение (confirm=true)")
        return duplicates.quarantine_group(db, group_id)

    @app.get("/api/quarantine")
    def quarantine_list():
        return catalog.query_files(db, limit=1000, status="quarantined")

    @app.post("/api/files/{file_id}/restore")
    def restore(file_id: int):
        try:
            ops.restore_file(db, file_id)
        except (FileExistsError, Exception) as e:  # noqa: BLE001
            raise HTTPException(409, f"{type(e).__name__}: {e}")
        return {"ok": True}

    # ---------- перемещение ----------
    @app.post("/api/plans")
    def make_plan(body: PlanIn):
        ids = _ids_for(body.kind, body.root_id, body.file_ids, undated=body.undated,
                       date_from=body.date_from, date_to=body.date_to)
        if not ids:
            raise HTTPException(400, "Нет файлов для плана")
        try:
            return organizer.build_plan(db, ids, body.dest_path, settings)
        except (OSError, ValueError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/plans/{plan_id}")
    def plan(plan_id: int, limit: int = 200, offset: int = 0):
        p = organizer.get_plan(db, plan_id, limit, offset)
        if p is None:
            raise HTTPException(404, "План не найден")
        return p

    @app.post("/api/plans/{plan_id}/execute")
    def plan_execute(plan_id: int, body: ExecIn):
        if not body.confirm:
            raise HTTPException(400, "Нужно подтверждение (confirm=true)")
        p = organizer.get_plan(db, plan_id, limit=0)
        if p is None:
            raise HTTPException(404, "План не найден")
        if p["status"] != "draft":
            raise HTTPException(409, f"План уже в статусе {p['status']}")
        return {"job_id": jobs.submit("move", lambda ctx: organizer.execute_plan(db, plan_id, True, ctx))}

    @app.post("/api/plans/{plan_id}/rollback")
    def plan_rollback(plan_id: int, body: ExecIn):
        if not body.confirm:
            raise HTTPException(400, "Нужно подтверждение (confirm=true)")
        return {"job_id": jobs.submit("rollback", lambda ctx: organizer.rollback_plan(db, plan_id, ctx))}

    @app.get("/api/plans")
    def plans():
        rows = db.conn().execute("SELECT id, dest_path, status, created, summary FROM plans ORDER BY id DESC LIMIT 50")
        return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]

    # ---------- HEIC ----------
    @app.post("/api/convert/heic")
    def convert(body: ConvertIn):
        ids = _ids_for(None, body.root_id, body.file_ids, ext={".heic", ".heif"})
        if not ids:
            raise HTTPException(400, "Нет HEIC-файлов")
        return {"job_id": jobs.submit("convert", lambda ctx: converter.convert_heic_files(db, ids, settings, ctx))}

    @app.post("/api/convert/quarantine-originals")
    def convert_quarantine(body: IdsIn):
        return converter.quarantine_originals(db, body.file_ids)

    # ---------- журнал ----------
    @app.get("/api/operations")
    def operations(limit: int = 200, plan_id: int | None = None):
        q, args = "SELECT * FROM operations", []
        if plan_id:
            q += " WHERE plan_id=?"; args.append(plan_id)
        rows = db.conn().execute(q + " ORDER BY id DESC LIMIT ?", args + [min(limit, 1000)]).fetchall()
        return [dict(r) for r in rows]

    app.mount("/", FreshStatic(directory=STATIC, html=True), name="static")
    return app
