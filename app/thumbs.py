"""Миниатюры фото и видео с дисковым кэшем."""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageOps

from .config import Settings
from .db import Database
from .hashing import file_abs_path
from . import metadata  # noqa: F401  (HEIF-плагин)

SIZE = 320

PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="240" viewBox="0 0 320 240">'
    '<rect width="320" height="240" fill="#2a2f3a"/>'
    '<text x="160" y="126" fill="#8b93a7" font-family="sans-serif" font-size="20" text-anchor="middle">нет превью</text>'
    "</svg>")


def _photo_thumb(path: Path, out: Path) -> bool:
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((SIZE, SIZE))
            img.convert("RGB").save(out, "JPEG", quality=80)
        return True
    except Exception:  # noqa: BLE001 - RAW: пробуем встроенное превью
        pass
    try:
        import exifread

        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=True)
        raw = tags.get("JPEGThumbnail")
        if raw:
            with Image.open(io.BytesIO(raw)) as img:
                img.thumbnail((SIZE, SIZE))
                img.convert("RGB").save(out, "JPEG", quality=80)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _video_thumb(path: Path, out: Path, ffmpeg: str | None) -> bool:
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-v", "quiet", "-ss", "1", "-i", str(path), "-frames:v", "1",
           "-vf", f"scale={SIZE}:-1", str(out)]
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True, **kwargs)
        return out.exists() and out.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        return False


def get_thumbnail(db: Database, settings: Settings, file_id: int) -> Path | None:
    """Путь к JPEG-миниатюре (из кэша или созданной сейчас); None — показывать заглушку."""
    c = db.conn()
    row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if row is None:
        return None
    settings.thumbs_dir.mkdir(parents=True, exist_ok=True)
    out = settings.thumbs_dir / f"{row['id']}_{row['mtime_ns']}.jpg"
    if out.exists():
        return out
    src = file_abs_path(c, row)
    if not src.exists():
        return None
    ok = _video_thumb(src, out, settings.ffmpeg_path) if row["kind"] == "video" else _photo_thumb(src, out)
    if not ok and out.exists():
        out.unlink()
    return out if ok else None
