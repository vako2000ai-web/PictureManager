"""Конвертация HEIC -> JPEG с переносом EXIF/ICC и связью с оригиналом."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from . import ops
from .catalog import recompute_groups
from .config import Settings
from .db import Database
from .errors import Cancelled
from .hashing import file_abs_path
from .jobs import JobContext
from . import metadata  # noqa: F401  (регистрирует HEIF-плагин Pillow)

HEIC_EXTS = {".heic", ".heif"}


def _prepare_exif(img) -> bytes:
    exif = Image.Exif()
    raw = img.info.get("exif")
    if raw:
        try:
            exif.load(raw)
        except Exception:  # noqa: BLE001 - битый EXIF не должен ломать конвертацию
            exif = Image.Exif()
    for ifd in (0x8769, 0x8825):      # подгружаем Exif- и GPS-блоки, иначе tobytes их потеряет
        try:
            exif.get_ifd(ifd)
        except Exception:  # noqa: BLE001
            pass
    exif[0x0112] = 1                  # ориентация уже применена к пикселям
    return exif.tobytes()


def convert_image(src: Path, dst: Path, quality: int) -> None:
    """Сохраняет JPEG: ориентация — в пикселях, EXIF и ICC-профиль переносятся."""
    tmp = dst.with_name(dst.name + ".pmtmp")
    with Image.open(src) as img:
        icc = img.info.get("icc_profile")
        exif = _prepare_exif(img)
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        kwargs = {"quality": quality, "exif": exif}
        if icc:
            kwargs["icc_profile"] = icc
        try:
            img.save(tmp, "JPEG", **kwargs)
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()


def convert_heic_files(db: Database, file_ids: list[int], settings: Settings,
                       ctx: JobContext | None = None) -> dict:
    c = db.conn()
    rows = []
    for fid in file_ids:
        r = c.execute("SELECT * FROM files WHERE id=? AND status='present'", (fid,)).fetchone()
        if r and (r["ext"] or "").lower() in HEIC_EXTS:
            rows.append(r)
    if ctx:
        ctx.set_total(bytes=sum(r["size"] for r in rows), files=len(rows))
    res = {"converted": 0, "skipped": 0, "errors": [], "cancelled": False, "outputs": []}
    touched = set()
    for r in rows:
        if ctx and ctx.cancelled():
            res["cancelled"] = True
            break
        try:
            if c.execute("SELECT 1 FROM derived_files WHERE source_file_id=? AND op='heic-to-jpeg'",
                         (r["id"],)).fetchone():
                res["skipped"] += 1        # уже сконвертирован
                continue
            src = file_abs_path(c, r)
            dst = ops.unique_path((src.with_suffix(".jpg")))
            convert_image(src, dst, settings.jpeg_quality)
            st = dst.stat()
            rel = Path(os.path.relpath(dst, c.execute("SELECT path FROM roots WHERE id=?",
                                                       (r["root_id"],)).fetchone()["path"])).as_posix()
            with db.tx() as tc:
                fid = tc.execute(
                    "INSERT INTO files(root_id, rel_path, ext, size, mtime_ns, kind, taken_at, date_source, "
                    "date_confidence, manual_taken_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["root_id"], rel, ".jpg", st.st_size, st.st_mtime_ns, "photo", r["taken_at"],
                     r["date_source"], r["date_confidence"], r["manual_taken_at"])).lastrowid
                tc.execute("INSERT INTO derived_files(file_id, source_file_id, op) VALUES (?,?,?)",
                           (fid, r["id"], "heic-to-jpeg"))
                tc.execute("INSERT INTO operations(plan_id,kind,file_id,src,dst,status,ts,detail) "
                           "VALUES (NULL,'convert',?,?,?,'done',?,?)",
                           (fid, str(src), str(dst), datetime.now().isoformat(timespec="seconds"),
                            json.dumps({"source_file_id": r["id"], "quality": settings.jpeg_quality})))
            touched.add(r["root_id"])
            res["converted"] += 1
            res["outputs"].append(str(dst))
        except Cancelled:
            res["cancelled"] = True
            break
        except Exception as e:  # noqa: BLE001 - повреждённый файл пропускаем, остальные обрабатываем
            res["errors"].append({"file_id": r["id"], "error": f"{type(e).__name__}: {e}"})
        finally:
            if ctx:
                ctx.add(bytes=r["size"], files=1)
    for rid in touched:
        recompute_groups(db, rid)
    return res


def quarantine_originals(db: Database, source_ids: list[int]) -> dict:
    """Явное действие: HEIC-оригиналы, у которых есть JPEG-копия, — в карантин."""
    c = db.conn()
    moved, errors = 0, []
    for sid in source_ids:
        has_copy = c.execute("SELECT 1 FROM derived_files d JOIN files f ON f.id=d.file_id "
                             "WHERE d.source_file_id=? AND f.status='present'", (sid,)).fetchone()
        if not has_copy:
            errors.append({"file_id": sid, "error": "нет сконвертированной копии"})
            continue
        try:
            ops.quarantine_file(db, sid)
            moved += 1
        except Exception as e:  # noqa: BLE001
            errors.append({"file_id": sid, "error": f"{type(e).__name__}: {e}"})
    return {"quarantined": moved, "errors": errors}
