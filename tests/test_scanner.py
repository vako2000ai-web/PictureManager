from datetime import datetime

import pytest

from app import scanner
from app.scanner import add_root, scan_root
from conftest import make_jpeg, set_mtime


def test_add_root_rejects_missing_path(db, tmp_path):
    with pytest.raises(ValueError):
        add_root(db, str(tmp_path / "nope"))


def test_scan_finds_nested_and_counts_unsupported(db, settings, tmp_path):
    photos = tmp_path / "p"
    make_jpeg(photos / "a.jpg", datetime(2024, 7, 14, 10, 0, 0))
    make_jpeg(photos / "deep" / "er" / "b.JPG", datetime(2024, 7, 15, 11, 0, 0))
    (photos / "notes.txt").write_text("x")
    (photos / "clip.mp4").write_bytes(b"\x00" * 10)
    root = add_root(db, str(photos))
    stats = scan_root(db, root["id"], settings)
    assert stats["added"] == 3 and stats["unsupported"] == 1
    rows = db.conn().execute("SELECT rel_path, kind FROM files ORDER BY rel_path").fetchall()
    assert [r["rel_path"] for r in rows] == ["a.jpg", "clip.mp4", "deep/er/b.JPG"]
    assert stats["ffprobe_missing"] is True


def test_rescan_is_incremental_and_marks_missing(db, settings, tmp_path):
    photos = tmp_path / "p"
    a = make_jpeg(photos / "a.jpg", datetime(2024, 1, 1, 1, 1, 1))
    make_jpeg(photos / "b.jpg", datetime(2024, 1, 2, 1, 1, 1))
    root = add_root(db, str(photos))
    scan_root(db, root["id"], settings)
    db.conn().execute("UPDATE files SET sha256='keep', hash_size=size, hash_mtime_ns=mtime_ns")
    stats = scan_root(db, root["id"], settings)
    assert stats["unchanged"] == 2 and stats["added"] == 0
    a.unlink()
    stats = scan_root(db, root["id"], settings)
    assert stats["missing"] == 1
    st = db.conn().execute("SELECT status FROM files WHERE rel_path='a.jpg'").fetchone()["status"]
    assert st == "missing"  # запись не удаляется
    make_jpeg(photos / "a.jpg", datetime(2024, 1, 1, 1, 1, 1))
    scan_root(db, root["id"], settings)
    assert db.conn().execute("SELECT status FROM files WHERE rel_path='a.jpg'").fetchone()["status"] == "present"


def test_unreadable_directory_does_not_stop_scan(db, settings, tmp_path, monkeypatch):
    photos = tmp_path / "p"
    make_jpeg(photos / "ok" / "a.jpg", datetime(2024, 1, 1, 1, 1, 1))
    make_jpeg(photos / "locked" / "b.jpg", datetime(2024, 1, 1, 1, 1, 1))
    real = scanner.os.scandir

    def fake(path):
        if str(path).endswith("locked"):
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(scanner.os, "scandir", fake)
    root = add_root(db, str(photos))
    stats = scan_root(db, root["id"], settings)
    assert stats["added"] == 1 and len(stats["errors"]) == 1


def test_root_recognised_after_drive_letter_change(db, tmp_path, monkeypatch):
    a = tmp_path / "X" / "Photos"
    a.mkdir(parents=True)
    root1 = add_root(db, str(a))
    # «Смена буквы»: тот же том, путь без буквы диска совпадает, новый корень не создаётся.
    monkeypatch.setattr(scanner, "_tail", lambda p: "same")
    b = tmp_path / "Y" / "Photos"
    b.mkdir(parents=True)
    root2 = add_root(db, str(b))
    assert root1["id"] == root2["id"] and len(scanner.list_roots(db)) == 1
    assert scanner.list_roots(db)[0]["path"].endswith("Y\\Photos") or scanner.list_roots(db)[0]["path"].endswith("Y/Photos")


def test_nested_roots_and_quarantine_dir_skipped(db, settings, tmp_path):
    base = tmp_path / "base"
    make_jpeg(base / "a.jpg", datetime(2024, 1, 1, 1, 1, 1))
    make_jpeg(base / "_duplicates" / "q.jpg", datetime(2024, 1, 1, 1, 1, 1))
    make_jpeg(base / "sub" / "c.jpg", datetime(2024, 1, 1, 1, 1, 1))
    outer = add_root(db, str(base))
    inner = add_root(db, str(base / "sub"))
    scan_root(db, outer["id"], settings)
    rels = [r["rel_path"] for r in db.conn().execute("SELECT rel_path FROM files WHERE root_id=?", (outer["id"],))]
    assert rels == ["a.jpg"]
    scan_root(db, inner["id"], settings)
    assert db.conn().execute("SELECT COUNT(*) FROM files WHERE root_id=?", (inner["id"],)).fetchone()[0] == 1


def test_cancel_stops_scan_and_keeps_processed(db, settings, tmp_path):
    photos = tmp_path / "p"
    for i in range(5):
        make_jpeg(photos / f"{i}.jpg", datetime(2024, 1, 1, 1, 1, 1), text_seed=i)
    root = add_root(db, str(photos))

    class Ctx:
        n = 0

        def cancelled(self):
            return self.n >= 2

        def add(self, **kw):
            self.n += 1

    stats = scan_root(db, root["id"], settings, Ctx())
    assert stats["cancelled"] is True
    assert db.conn().execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
