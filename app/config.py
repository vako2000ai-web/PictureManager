import os
import shutil
from pathlib import Path

PHOTO_EXTS = frozenset({"jpg", "jpeg", "png", "webp", "heic", "heif", "tif", "tiff", "bmp", "gif"})
RAW_EXTS = frozenset({"cr2", "cr3", "nef", "arw", "dng", "orf", "rw2", "raf", "srw", "pef"})
VIDEO_EXTS = frozenset({"mp4", "mov", "mkv", "avi", "m4v", "wmv", "mts", "m2ts", "3gp", "webm", "mpg", "mpeg"})

QUARANTINE_DIR = "_duplicates"
NO_DATE_DIR = "_без даты"

DEFAULTS = {
    "path_template": "{YYYY}/{MM}/{DD}",
    "day_boundary_hour": 0,
    "jpeg_quality": 92,
    "ffprobe_path": "",
    "ffmpeg_path": "",
}


class Config:
    """Настройки приложения; изменяемые значения хранятся в таблице settings."""

    def __init__(self, data_dir: str | os.PathLike | None = None):
        self.data_dir = Path(data_dir or os.environ.get("PM_DATA_DIR") or "data").resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "catalog.db"
        self.thumb_dir = self.data_dir / "thumbnails"
        self.photo_exts = set(PHOTO_EXTS)
        self.raw_exts = set(RAW_EXTS)
        self.video_exts = set(VIDEO_EXTS)
        self.values = dict(DEFAULTS)
        self._db = None

    def attach(self, db) -> None:
        self._db = db
        for row in db.query("SELECT key, value FROM settings"):
            key = row["key"]
            if key in DEFAULTS:
                self.values[key] = type(DEFAULTS[key])(row["value"])

    def __getattr__(self, name):
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    def update(self, changes: dict) -> dict:
        clean = {}
        for key, value in changes.items():
            if key not in DEFAULTS:
                raise ValueError(f"неизвестная настройка: {key}")
            value = type(DEFAULTS[key])(value)
            if key == "path_template":
                if "{YYYY}" not in value or value.startswith(("/", "\\")) or ".." in value:
                    raise ValueError("шаблон должен содержать {YYYY} и быть относительным")
            if key == "day_boundary_hour" and not 0 <= value <= 23:
                raise ValueError("граница суток: час от 0 до 23")
            if key == "jpeg_quality" and not 1 <= value <= 100:
                raise ValueError("качество JPEG: от 1 до 100")
            clean[key] = value
        for key, value in clean.items():
            self.values[key] = value
            if self._db is not None:
                self._db.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )
        return self.snapshot()

    def snapshot(self) -> dict:
        return dict(self.values)

    def kind_of(self, ext: str) -> str | None:
        ext = ext.lower().lstrip(".")
        if ext in self.photo_exts:
            return "photo"
        if ext in self.raw_exts:
            return "raw"
        if ext in self.video_exts:
            return "video"
        return None

    def find_tool(self, name: str) -> str | None:
        configured = self.values.get(f"{name}_path")
        return shutil.which(configured or name)
