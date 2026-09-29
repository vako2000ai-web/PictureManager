import os

from app import catalog
from tests.conftest import FakeCtx, make_jpeg, scan, write


def test_recursive_scan_and_skipped(env, tmp_path):
    root_dir = tmp_path / "photos"
    make_jpeg(root_dir / "a" / "b" / "c" / "deep.jpg", "2024:07:15 10:15:00")
    write(str(root_dir / "clip.mp4"))
    write(str(root_dir / "notes.txt"))
    write(str(root_dir / "doc.pdf"))
    root, report = scan(env, root_dir)
    assert report["new"] == 2 and report["skipped_unsupported"] == 2
    assert report["skipped_by_ext"] == {"txt": 1, "pdf": 1}
    row = env[1].one("SELECT * FROM files WHERE rel_path='a/b/c/deep.jpg'")
    assert row["taken_at"] == "2024-07-15T10:15:00" and row["date_source"] == "exif"
    assert row["kind"] == "photo" and row["status"] == "present"


def test_incremental_and_missing(env, tmp_path):
    from app import scanner

    cfg, db = env
    root_dir = tmp_path / "p"
    f1 = make_jpeg(root_dir / "1.jpg")
    make_jpeg(root_dir / "2.jpg")
    root, _ = scan(env, root_dir)
    report = scanner.scan_root(db, cfg, root["id"], FakeCtx())
    assert report["unchanged"] == 2 and report["new"] == 0
    os.remove(f1)
    report = scanner.scan_root(db, cfg, root["id"], FakeCtx())
    assert report["missing"] == 1
    assert db.one("SELECT status FROM files WHERE rel_path='1.jpg'")["status"] == "missing"


def test_cancel_keeps_processed_and_does_not_mark_missing(env, tmp_path):
    from app import scanner

    cfg, db = env
    root_dir = tmp_path / "p"
    for i in range(5):
        make_jpeg(root_dir / f"{i}.jpg")
    root, _ = scan(env, root_dir)
    ctx = FakeCtx()
    ctx._cancel.set()
    report = scanner.scan_root(db, cfg, root["id"], ctx)
    assert report["cancelled"] and report["missing"] == 0
    assert db.one("SELECT COUNT(*) n FROM files WHERE status='present'")["n"] == 5


def test_unreadable_dir_reported(env, tmp_path, monkeypatch):
    from app import scanner

    cfg, db = env
    root_dir = tmp_path / "p"
    make_jpeg(root_dir / "ok.jpg")
    os.makedirs(root_dir / "locked")
    real_walk = os.walk

    def fake_walk(top, onerror=None, **kw):
        yield from real_walk(top, onerror=onerror, **kw)
        onerror(PermissionError(13, "Permission denied", str(root_dir / "locked")))

    monkeypatch.setattr(scanner.os, "walk", fake_walk)
    root = catalog.add_root(db, str(root_dir))
    report = scanner.scan_root(db, cfg, root["id"], FakeCtx())
    assert report["new"] == 1 and len(report["errors"]) == 1


def test_add_root_validation_and_rebind(env, tmp_path):
    db = env[1]
    try:
        catalog.add_root(db, str(tmp_path / "nope"))
        assert False
    except ValueError:
        pass
    d = tmp_path / "x"
    d.mkdir()
    r1 = catalog.add_root(db, str(d))
    assert r1["volume_id"]
    # тот же том и тот же хвост пути → не дублируется
    r2 = catalog.add_root(db, str(d))
    assert r2["id"] == r1["id"]
