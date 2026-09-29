import io
import os
import subprocess

import piexif
from PIL import Image, ImageOps

from . import catalog
from .metadata import pillow_heif  # noqa: F401

SIZE = (320, 320)

PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">'
    '<rect width="160" height="160" fill="#e5e7eb"/><path d="M30 120l30-40 22 28 16-20 32 32z" fill="#9ca3af"/>'
    '<circle cx="55" cy="55" r="12" fill="#9ca3af"/></svg>'
)


def _photo_thumb(src: str, dst: str) -> bool:
    try:
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail(SIZE)
            img.convert("RGB").save(dst, "JPEG", quality=80)
        return True
    except Exception:  # noqa: BLE001
        pass
    try:  # RAW: встроенное превью
        thumb = piexif.load(src).get("thumbnail")
        if thumb:
            with Image.open(io.BytesIO(thumb)) as img:
                img.thumbnail(SIZE)
                img.convert("RGB").save(dst, "JPEG", quality=80)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _video_thumb(src: str, dst: str, ffmpeg: str | None) -> bool:
    if not ffmpeg:
        return False
    try:
        subprocess.run([ffmpeg, "-v", "quiet", "-y", "-ss", "1", "-i", src, "-frames:v", "1",
                        "-vf", f"scale={SIZE[0]}:-2", dst], check=True, timeout=60)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:  # видео короче секунды
            subprocess.run([ffmpeg, "-v", "quiet", "-y", "-i", src, "-frames:v", "1",
                            "-vf", f"scale={SIZE[0]}:-2", dst], check=True, timeout=60)
        return os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception:  # noqa: BLE001
        return False


def get_thumbnail(db, cfg, file_id: int) -> str | None:
    """Путь к миниатюре в кэше (создаётся по требованию) либо None → заглушка."""
    f = catalog.get_file(db, file_id)
    if f is None:
        return None
    cfg.thumb_dir.mkdir(parents=True, exist_ok=True)
    cached = cfg.thumb_dir / f"{f['id']}_{f['mtime_ns']}.jpg"
    if cached.exists():
        return str(cached)
    tmp = str(cached) + ".tmp.jpg"
    ok = (_video_thumb(f["abs"], tmp, cfg.find_tool("ffmpeg")) if f["kind"] == "video"
          else _photo_thumb(f["abs"], tmp))
    if ok:
        os.replace(tmp, cached)
        return str(cached)
    if os.path.exists(tmp):
        os.remove(tmp)
    return None
