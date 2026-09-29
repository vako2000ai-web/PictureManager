"""Определение даты съёмки: EXIF / контейнер видео → имя файла → mtime."""
import json
import re
import subprocess
from datetime import datetime, timedelta

import piexif
from PIL import Image

try:  # HEIC-декодер регистрируется в Pillow
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001
    pillow_heif = None

CONF_EXIF, CONF_FILENAME, CONF_MTIME = 1.0, 0.7, 0.3

_NAME_WITH_TIME = re.compile(
    r"(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])[-_. T]?([01]\d)([0-5]\d)([0-5]\d)"
)
_NAME_DATE_ONLY = re.compile(r"(?<!\d)((?:19|20)\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_exif_dt(value) -> datetime | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    if not value:
        return None
    value = value.strip().strip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S%z"):
        try:
            dt = datetime.strptime(value[:19] if "%z" not in fmt else value, fmt)
        except ValueError:
            continue
        dt = dt.replace(tzinfo=None)  # EXIF-время считается локальным
        return dt if dt.year >= 1900 else None
    return None


def photo_exif_date(path: str) -> datetime | None:
    """EXIF DateTimeOriginal (затем DateTimeDigitized) для фото, в том числе HEIC и RAW на базе TIFF."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            sub = exif.get_ifd(0x8769)
            for tag in (36867, 36868):  # DateTimeOriginal, DateTimeDigitized
                dt = _parse_exif_dt(sub.get(tag))
                if dt:
                    return dt
            return None
    except Exception:  # noqa: BLE001
        pass
    try:  # RAW и прочее, что Pillow не открывает
        data = piexif.load(path)
        for tag in (piexif.ExifIFD.DateTimeOriginal, piexif.ExifIFD.DateTimeDigitized):
            dt = _parse_exif_dt(data.get("Exif", {}).get(tag))
            if dt:
                return dt
    except Exception:  # noqa: BLE001
        pass
    return None


def video_creation_time(path: str, ffprobe: str | None) -> datetime | None:
    """creation_time контейнера; пояс отбрасывается (считается локальным временем)."""
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_entries", "format_tags=creation_time", path],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        value = json.loads(out).get("format", {}).get("tags", {}).get("creation_time")
        if not value:
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        return dt if dt.year >= 1990 else None
    except Exception:  # noqa: BLE001
        return None


def date_from_filename(name: str) -> datetime | None:
    limit = datetime.now().year + 1
    for pattern, has_time in ((_NAME_WITH_TIME, True), (_NAME_DATE_ONLY, False)):
        for m in pattern.finditer(name):
            parts = [int(x) for x in m.groups()]
            try:
                dt = datetime(*parts) if has_time else datetime(parts[0], parts[1], parts[2])
            except ValueError:
                continue
            if 1990 <= dt.year <= limit:
                return dt
    return None


def resolve_date(path: str, name: str, kind: str, mtime_ns: int, ffprobe: str | None):
    """Возвращает (iso-дата | None, источник | None, уверенность | None)."""
    if kind == "video":
        dt = video_creation_time(path, ffprobe)
        if dt:
            return iso(dt), "container", CONF_EXIF
    else:
        dt = photo_exif_date(path)
        if dt:
            return iso(dt), "exif", CONF_EXIF
    dt = date_from_filename(name)
    if dt:
        return iso(dt), "filename", CONF_FILENAME
    try:
        dt = datetime.fromtimestamp(mtime_ns / 1e9)
        if dt.year >= 1990:
            return iso(dt), "mtime", CONF_MTIME
    except (OverflowError, OSError, ValueError):
        pass
    return None, None, None


def apply_day_boundary(dt: datetime, hour: int) -> datetime:
    """Снимки раньше часа границы суток относятся к предыдущему дню."""
    return dt - timedelta(days=1) if hour and dt.hour < hour else dt
