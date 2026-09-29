import asyncio
import json
import os
import string
import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, dupes, heic, journal, mover, scanner, thumbs
from .config import Config
from .db import Database
from .jobs import FINISHED, JobManager

STATIC = Path(__file__).parent / "static"


def _confirm(body: dict) -> None:
    if not body.get("confirm"):
        raise HTTPException(400, "требуется явное подтверждение (confirm: true)")


def create_app(data_dir=None) -> FastAPI:
    cfg = Config(data_dir)
    db = Database(cfg.db_path)
    cfg.attach(db)
    jobs = JobManager(db)
    recovered = journal.recover(db)  # довести до конца операции, прерванные сбоем

    app = FastAPI(title="PictureManager")
    app.state.cfg, app.state.db, app.state.jobs, app.state.recovered = cfg, db, jobs, recovered

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as exc:
            raise HTTPException(404, f"не найдено: {exc}") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---- состояние и настройки ----
    @app.get("/api/status")
    def status():
        warnings = []
        if not cfg.find_tool("ffprobe"):
            warnings.append("ffprobe не найден: дата видео определяется по имени файла или времени изменения")
        if not cfg.find_tool("ffmpeg"):
            warnings.append("ffmpeg не найден: вместо миниатюр видео показывается заглушка")
        return {"warnings": warnings, "stats": catalog.stats(db), "recovered": recovered}

    @app.get("/api/settings")
    def get_settings():
        return cfg.snapshot()

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        return guard(cfg.update, body)

    # ---- выбор каталога ----
    @app.get("/api/fs/list")
    def fs_list(path: str = ""):
        if not path:
            if sys.platform == "win32":
                return {"path": "", "parent": None,
                        "dirs": [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]}
            path = os.path.expanduser("~")
        path = os.path.abspath(path)
        try:
            dirs = sorted(e.path for e in os.scandir(path) if e.is_dir() and not e.name.startswith("."))
        except OSError as exc:
            raise HTTPException(400, f"нет доступа: {exc.strerror}") from exc
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent if parent != path else "", "dirs": dirs}

    # ---- корни ----
    @app.get("/api/roots")
    def roots_list():
        return catalog.list_roots(db)

    @app.post("/api/roots", status_code=201)
    def roots_add(body: dict = Body(...)):
        return guard(catalog.add_root, db, body.get("path", ""), body.get("label"))

    @app.delete("/api/roots/{root_id}")
    def roots_delete(root_id: int):
        catalog.remove_root(db, root_id)
        return {"ok": True}

    @app.post("/api/roots/{root_id}/scan", status_code=202)
    def roots_scan(root_id: int):
        guard(catalog.get_root, db, root_id)
        return {"job_id": jobs.submit("scan", lambda ctx: scanner.scan_root(db, cfg, root_id, ctx), "Сканирование")}

    # ---- задачи ----
    @app.get("/api/jobs")
    def jobs_list():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def jobs_get(job_id: int):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "нет такой задачи")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def jobs_cancel(job_id: int):
        return {"cancelled": jobs.cancel(job_id)}

    @app.get("/api/jobs/{job_id}/events")
    async def jobs_events(job_id: int):
        if jobs.get(job_id) is None:
            raise HTTPException(404, "нет такой задачи")

        async def gen():
            last = None
            while True:
                job = jobs.get(job_id)
                payload = json.dumps(job, ensure_ascii=False)
                if payload != last:
                    yield f"data: {payload}\n\n"
                    last = payload
                if job["status"] in FINISHED:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- каталог ----
    @app.get("/api/catalog")
    def catalog_list(sort: str = "desc", kind: str | None = None, root_id: int | None = None,
                     status: str | None = None, no_date: bool = False, derived: bool | None = None,
                     limit: int = Query(200, le=2000), offset: int = 0):
        return catalog.list_catalog(db, sort, kind, root_id, status, no_date, derived, limit, offset)

    @app.patch("/api/files/date")
    def files_date(body: dict = Body(...)):
        return {"updated": guard(catalog.set_manual_date, db, body.get("file_ids", []), body.get("taken_at"))}

    @app.get("/api/files/{file_id}/thumb")
    def files_thumb(file_id: int):
        path = thumbs.get_thumbnail(db, cfg, file_id)
        if path:
            return FileResponse(path, media_type="image/jpeg")
        return Response(thumbs.PLACEHOLDER_SVG, media_type="image/svg+xml")

    # ---- дубли ----
    @app.post("/api/dupes/scan", status_code=202)
    def dupes_scan():
        return {"job_id": jobs.submit("dupes", lambda ctx: dupes.find_duplicates(db, cfg, ctx), "Поиск дублей")}

    @app.get("/api/dupes")
    def dupes_list():
        return dupes.list_groups(db)

    @app.post("/api/dupes/{group_id}/keeper")
    def dupes_keeper(group_id: int, body: dict = Body(...)):
        guard(dupes.set_keeper, db, group_id, body.get("file_id"))
        return {"ok": True}

    @app.post("/api/dupes/{group_id}/quarantine")
    def dupes_quarantine(group_id: int, body: dict = Body(...)):
        _confirm(body)
        return guard(dupes.process_group, db, group_id)

    @app.get("/api/quarantine")
    def quarantine_list():
        return dupes.list_quarantine(db)

    @app.post("/api/quarantine/restore")
    def quarantine_restore(body: dict = Body(...)):
        return [dupes.restore_file(db, fid) for fid in body.get("file_ids", [])]

    # ---- перемещение ----
    @app.post("/api/plans", status_code=201)
    def plans_build(body: dict = Body(...)):
        file_ids = body.get("file_ids")
        if file_ids is None:
            rows = db.query("SELECT id FROM files WHERE status='present' AND id NOT IN (SELECT file_id FROM derived_files) "
                            "AND root_id=COALESCE(?, root_id)", (body.get("source_root_id"),))
            file_ids = [r["id"] for r in rows]
        plan_id = guard(mover.build_plan, db, cfg, file_ids, body.get("dest_root_id"))
        return mover.get_plan(db, plan_id)

    @app.get("/api/plans/{plan_id}")
    def plans_get(plan_id: int):
        plan = mover.get_plan(db, plan_id)
        if plan is None:
            raise HTTPException(404, "нет такого плана")
        return plan

    @app.post("/api/plans/{plan_id}/execute", status_code=202)
    def plans_execute(plan_id: int, body: dict = Body(...)):
        _confirm(body)
        plan = guard(mover.get_plan, db, plan_id)
        if plan is None or plan["status"] != "draft":
            raise HTTPException(400, "план не найден или уже выполнялся")
        if not plan["summary"].get("enough_space", True):
            raise HTTPException(400, "недостаточно свободного места на целевом диске")
        return {"job_id": jobs.submit("move", lambda ctx: mover.execute_plan(db, cfg, plan_id, ctx), "Перемещение")}

    @app.post("/api/plans/{plan_id}/rollback", status_code=202)
    def plans_rollback(plan_id: int, body: dict = Body(...)):
        _confirm(body)
        return {"job_id": jobs.submit("rollback", lambda ctx: mover.rollback_plan(db, plan_id, ctx), "Откат")}

    # ---- HEIC ----
    @app.get("/api/heic")
    def heic_list():
        return heic.list_heic(db)

    @app.post("/api/heic/convert", status_code=202)
    def heic_convert(body: dict = Body(...)):
        ids = body.get("file_ids")
        if ids is None:
            ids = heic.files_in_folder(db, body.get("root_id"), body.get("subpath", ""))
        quality = body.get("quality")
        return {"job_id": jobs.submit("heic", lambda ctx: heic.convert_files(db, cfg, ids, ctx, quality), "Конвертация HEIC")}

    @app.post("/api/heic/quarantine-originals")
    def heic_quarantine(body: dict = Body(...)):
        _confirm(body)
        return heic.quarantine_originals(db, body.get("file_ids"))

    # ---- журнал ----
    @app.get("/api/operations")
    def operations(limit: int = Query(200, le=2000), plan_id: int | None = None):
        cond, params = ("WHERE plan_id=?", [plan_id]) if plan_id else ("", [])
        return [dict(r) for r in db.query(f"SELECT * FROM operations {cond} ORDER BY id DESC LIMIT ?", (*params, limit))]

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
