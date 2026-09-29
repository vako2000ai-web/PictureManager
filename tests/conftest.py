import os
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from app.config import load_settings
from app.db import Database
from app.jobs import JobManager


@pytest.fixture
def settings(tmp_path):
    s = load_settings(tmp_path / "data")
    s.ffprobe_path = None
    s.ffmpeg_path = None
    return s


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def jobs(db):
    m = JobManager(db)
    yield m
    m.shutdown()


def make_jpeg(path: Path, taken: datetime | None = None, color=(200, 30, 30), size=(64, 48), text_seed=0):
    """JPEG с EXIF-датой (или без неё). Разный color/seed даёт разное содержимое."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color)
    img.paste(((text_seed * 53) % 256, (text_seed * 97) % 256, (text_seed * 29) % 256), (8, 8, 40, 40))
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")
        exif[0x0132] = taken.strftime("%Y:%m:%d %H:%M:%S")
    img.save(path, "JPEG", exif=exif.tobytes() if taken else b"")
    return path


def set_mtime(path: Path, dt: datetime):
    ts = dt.timestamp()
    os.utime(path, (ts, ts))
