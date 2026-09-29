import os
import threading

import piexif
import pytest
from PIL import Image

from app.catalog import add_root
from app.config import Config
from app.db import Database
from app.jobs import JobManager


class FakeCtx:
    def __init__(self):
        self._cancel = threading.Event()
        self.files = self.bytes = 0
        self.total = (0, 0)

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def set_total(self, files=None, bytes=None):
        self.total = (files, bytes)

    def advance(self, files=0, bytes=0):
        self.files += files
        self.bytes += bytes

    def message(self, text):
        pass


@pytest.fixture
def env(tmp_path):
    cfg = Config(tmp_path / "data")
    db = Database(cfg.db_path)
    cfg.attach(db)
    return cfg, db


@pytest.fixture
def jobs(env):
    return JobManager(env[1])


def make_jpeg(path, date=None, color=(200, 10, 10), size=(8, 8)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", size, color)
    if date:
        exif = piexif.dump({"Exif": {piexif.ExifIFD.DateTimeOriginal: date.encode()}})
        img.save(path, "JPEG", exif=exif)
    else:
        img.save(path, "JPEG")
    return str(path)


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def scan(env, root_path):
    from app import scanner

    cfg, db = env
    root = add_root(db, str(root_path))
    ctx = FakeCtx()
    report = scanner.scan_root(db, cfg, root["id"], ctx)
    return root, report
