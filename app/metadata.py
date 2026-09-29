"""Чтение дат съёмки: EXIF фото, метаданные видео, дата в имени файла."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

try:  # HEIC/HEIF читаются Pillow только с этим плагином
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pillow_heif = None

Image.MAX_IMAGE_PIXELS = None  # большие снимки — не ошибка, а обычные файлы архива

MIN_YEAR = 1995  # нулевые даты камер и «эпохи» контейнеров (1904, 1970) отбрасываем


def _valid(dt: datetime | None) -> datetime | None:
    if dt is None or dt.year < MIN_YEAR or dt.year > datetime.now().year + 1:
        return None
    return dt


def parse_exif_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    s = str(value).strip().replace("\x00", "")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return _valid(datetime.strptime(s[:19] if "H" in fmt else s[:10], fmt))
        except ValueError:
            continue
    return None


def read_photo_date(path) -> datetime | None:
    try:
        with Image.open(path) as img:
            ex = img.getexif()
            try:
                sub = ex.get_ifd(0x8769)
            except Exception:  # noqa: BLE001
                sub = {}
            for v in (sub.get(0x9003), sub.get(0x9004), ex.get(0x0132)):
                dt = parse_exif_dt(v)
                if dt:
                    return dt
    except Exception:  # noqa: BLE001 - нечитаемое Pillow пробуем через exifread (RAW)
        pass
    try:
        import exifread

        with open(path, "rb") as f:
            tags = exifread.process_file(f, details=False, stop_tag="EXIF DateTimeOriginal")
        for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
            if key in tags:
                dt = parse_exif_dt(str(tags[key]))
                if dt:
                    return dt
    except Exception:  # noqa: BLE001
        pass
    return None


_FN_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[-_ T.]?([01]\d|2[0-3])[-_.:]?([0-5]\d)[-_.:]?([0-5]\d))?(?!\d)")


def date_from_filename(name: str) -> datetime | None:
    m = _FN_RE.search(Path(name).stem)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    try:
        return _valid(datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0)))
    except ValueError:
        return None


def _parse_video_dt(value: str, policy: str) -> datetime | None:
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    if re.search(r"[+-]\d{4}$", s):  # +0300 -> +03:00
        s = s[:-2] + ":" + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None) if policy == "machine" else dt.replace(tzinfo=None)
    return _valid(dt)


def probe_video_date(path, ffprobe: str | None, policy: str = "local") -> datetime | None:
    if not ffprobe:
        return None
    cmd = [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(path)]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=30, check=True, **kwargs).stdout
        tags = json.loads(out).get("format", {}).get("tags", {}) or {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    tags = {k.lower(): v for k, v in tags.items()}
    # Apple хранит локальное время с поясом: оно точнее UTC-метки creation_time.
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        dt = _parse_video_dt(tags.get(key, ""), "local" if key.startswith("com.apple") else policy)
        if dt:
            return dt
    return None
