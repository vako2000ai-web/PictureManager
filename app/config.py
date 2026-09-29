from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".gif"}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf", ".srw", ".pef"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".3gp", ".wmv", ".mts", ".m2ts", ".webm"}

QUARANTINE_DIR = "_duplicates"
UNDATED_DIR = "_без даты"

# Поля, которые пользователь может менять через API и файл настроек.
EDITABLE = ("path_template", "day_boundary_hour", "jpeg_quality", "video_time_policy",
            "ffprobe_path", "ffmpeg_path")


def classify_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in RAW_EXTS:
        return "raw"
    if ext in VIDEO_EXTS:
        return "video"
    return None


@dataclass
class Settings:
    data_dir: Path
    path_template: str = "{YYYY}/{MM}/{DD}"
    day_boundary_hour: int = 0
    jpeg_quality: int = 92
    # local: creation_time видео считается локальным временем; machine: UTC переводится в пояс ПК
    video_time_policy: str = "local"
    ffprobe_path: str | None = None
    ffmpeg_path: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "catalog.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def settings_file(self) -> Path:
        return self.data_dir / "settings.json"

    def to_public(self) -> dict:
        d = asdict(self)
        d["data_dir"] = str(self.data_dir)
        d["ffprobe_found"] = bool(self.ffprobe_path)
        d["ffmpeg_found"] = bool(self.ffmpeg_path)
        return d

    def update(self, values: dict) -> None:
        for key in EDITABLE:
            if key in values and values[key] is not None:
                setattr(self, key, values[key])
        self.day_boundary_hour = max(0, min(23, int(self.day_boundary_hour)))
        self.jpeg_quality = max(1, min(100, int(self.jpeg_quality)))
        if self.video_time_policy not in ("local", "machine"):
            self.video_time_policy = "local"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        saved = {k: getattr(self, k) for k in EDITABLE}
        self.settings_file.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")


def load_settings(data_dir: str | Path | None = None) -> Settings:
    data_dir = Path(data_dir or os.environ.get("PICTUREMANAGER_DATA") or Path.cwd() / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    s = Settings(data_dir=data_dir)
    if s.settings_file.exists():
        try:
            saved = json.loads(s.settings_file.read_text(encoding="utf-8"))
            for key in EDITABLE:
                if key in saved:
                    setattr(s, key, saved[key])
        except (OSError, ValueError):
            pass
    s.ffprobe_path = s.ffprobe_path or shutil.which("ffprobe")
    s.ffmpeg_path = s.ffmpeg_path or shutil.which("ffmpeg")
    return s
