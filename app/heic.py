"""Конвертация HEIC → JPEG с переносом EXIF и ICC и применением ориентации."""
import os

import piexif
from PIL import Image, ImageOps

from . import catalog, dupes, journal  # noqa: F401
from .fileops import unique_path
from .metadata import pillow_heif  # noqa: F401  (регистрирует HEIF-декодер)

HEIC_EXTS = ("heic", "heif")


def convert_file(src: str, dst: str, quality: int = 92) -> None:
    """Записывает JPEG в dst (путь не должен существовать)."""
    with Image.open(src) as img:
        img.load()
        exif_bytes = img.info.get("exif")
        icc = img.info.get("icc_profile")
        img = ImageOps.exif_transpose(img)  # ориентация применяется к пикселям
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        kwargs = {"quality": quality, "subsampling": 0 if quality >= 90 else -1}
        if icc:
            kwargs["icc_profile"] = icc
        if exif_bytes:
            try:
                data = piexif.load(exif_bytes)
                data["0th"][piexif.ImageIFD.Orientation] = 1  # повторного поворота быть не должно
                data["1st"], data["thumbnail"] = {}, None
                kwargs["exif"] = piexif.dump(data)
            except Exception:  # noqa: BLE001 - нечитаемый EXIF переносим как есть
                kwargs["exif"] = exif_bytes
        tmp = dst + ".part"
        try:
            img.save(tmp, "JPEG", **kwargs)
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def convert_files(db, cfg, file_ids: list[int], ctx, quality: int | None = None) -> dict:
    quality = quality or cfg.jpeg_quality
    files = []
    for fid in file_ids:
        f = catalog.get_file(db, fid)
        if f and f["ext"] in HEIC_EXTS and f["status"] == "present":
            files.append(f)
    ctx.set_total(files=len(files), bytes=sum(f["size"] for f in files))
    report = {"converted": 0, "skipped_existing": 0, "errors": []}
    roots = set()
    for f in files:
        if ctx.cancelled:
            break
        already = db.one("SELECT 1 FROM derived_files WHERE source_file_id=? AND op='heic_to_jpeg'", (f["id"],))
        if already:
            report["skipped_existing"] += 1
            ctx.advance(files=1, bytes=f["size"])
            continue
        stem = os.path.splitext(f["abs"])[0]
        dst = unique_path(stem + ".jpg")  # рядом с оригиналом, без перезаписи
        try:
            convert_file(f["abs"], dst, quality)
            st = os.stat(dst)
            rel = os.path.relpath(dst, f["root_path"]).replace(os.sep, "/")
            new_id = db.execute(
                "INSERT INTO files(root_id, rel_path, size, mtime_ns, kind, ext, status, taken_at, date_source, "
                "date_confidence, manual_taken_at) VALUES(?,?,?,?, 'photo','jpg','present',?,?,?,?)",
                (f["root_id"], rel, st.st_size, st.st_mtime_ns, f["taken_at"], f["date_source"],
                 f["date_confidence"], f["manual_taken_at"]),
            ).lastrowid
            db.execute("INSERT INTO derived_files VALUES(?,?, 'heic_to_jpeg')", (new_id, f["id"]))
            journal.record(db, "convert", f["id"], f["root_id"], f["rel_path"], f["root_id"], rel,
                           size=st.st_size, status="done")
            roots.add(f["root_id"])
            report["converted"] += 1
        except Exception as exc:  # noqa: BLE001 - повреждённый файл не останавливает пакет
            report["errors"].append({"file_id": f["id"], "path": f["rel_path"], "error": f"{type(exc).__name__}: {exc}"})
        ctx.advance(files=1, bytes=f["size"])
    for rid in roots:
        catalog.recompute_groups(db, rid)
    return report


def files_in_folder(db, root_id: int, subpath: str = "") -> list[int]:
    prefix = subpath.strip("/")
    like = (prefix + "/%") if prefix else "%"
    rows = db.query(
        "SELECT id FROM files WHERE root_id=? AND status='present' AND ext IN ('heic','heif') AND rel_path LIKE ?",
        (root_id, like),
    )
    return [r["id"] for r in rows]


def list_heic(db) -> list[dict]:
    rows = db.query(
        "SELECT f.id, f.rel_path, f.size, f.root_id, f.status, "
        "(SELECT d.file_id FROM derived_files d WHERE d.source_file_id=f.id LIMIT 1) AS jpeg_id "
        "FROM files f WHERE f.ext IN ('heic','heif') AND f.status IN ('present','quarantined') ORDER BY f.id"
    )
    return [dict(r) for r in rows]


def quarantine_originals(db, file_ids: list[int] | None = None) -> dict:
    """Явное действие: HEIC, для которых есть JPEG, уходят в карантин."""
    rows = db.query(
        "SELECT DISTINCT s.id FROM derived_files d JOIN files s ON s.id=d.source_file_id "
        "JOIN files j ON j.id=d.file_id WHERE d.op='heic_to_jpeg' AND s.status='present' AND j.status='present'"
    )
    ids = [r["id"] for r in rows if file_ids is None or r["id"] in file_ids]
    moved = failed = 0
    roots = set()
    for fid in ids:
        f = catalog.get_file(db, fid)
        if dupes.quarantine_file(db, fid, note="оригинал HEIC после конвертации") is None:
            failed += 1
        else:
            moved += 1
            roots.add(f["root_id"])
    for rid in roots:
        catalog.recompute_groups(db, rid)
    return {"quarantined": moved, "failed": failed}
