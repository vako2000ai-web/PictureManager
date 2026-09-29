import os
from datetime import datetime

from app import catalog
from app.metadata import apply_day_boundary, date_from_filename, resolve_date
from tests.conftest import make_jpeg, scan, write


def test_filename_patterns():
    assert date_from_filename("IMG_20240715_101500.jpg") == datetime(2024, 7, 15, 10, 15, 0)
    assert date_from_filename("PXL_20240715_101500123.jpg") == datetime(2024, 7, 15, 10, 15, 0)
    assert date_from_filename("IMG-20240715-WA0001.jpg") == datetime(2024, 7, 15)
    assert date_from_filename("WhatsApp Image 2024-07-15 at 10.15.jpg") == datetime(2024, 7, 15)
    assert date_from_filename("IMG_0001.jpg") is None
    assert date_from_filename("scan_20241340.jpg") is None


def test_cascade_sources(env, tmp_path):
    d = tmp_path / "p"
    make_jpeg(d / "IMG_20200101_000000.jpg", "2024:07:15 10:15:00")  # EXIF важнее имени
    make_jpeg(d / "IMG_20240715_101500.jpg")
    make_jpeg(d / "plain.jpg")
    scan(env, d)
    q = lambda n: env[1].one("SELECT * FROM files WHERE rel_path=?", (n,))
    assert q("IMG_20200101_000000.jpg")["date_source"] == "exif"
    assert q("IMG_20240715_101500.jpg")["date_source"] == "filename"
    row = q("plain.jpg")
    assert row["date_source"] == "mtime" and row["date_confidence"] < 0.5


def test_unknown_date(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(b"not an image")
    assert resolve_date(str(p), "x.jpg", "photo", 0, None) == (None, None, None)


def test_video_without_ffprobe_uses_filename(env, tmp_path):
    d = tmp_path / "v"
    write(str(d / "VID_20230101_120000.mp4"))
    scan(env, d)
    row = env[1].one("SELECT * FROM files")
    assert row["date_source"] == "filename" and row["kind"] == "video"


def test_manual_date_survives_rescan(env, tmp_path):
    from app import scanner
    from tests.conftest import FakeCtx

    cfg, db = env
    d = tmp_path / "p"
    path = make_jpeg(d / "a.jpg", "2024:07:15 10:15:00")
    root, _ = scan(env, d)
    fid = db.one("SELECT id FROM files")["id"]
    catalog.set_manual_date(db, [fid], "2001-02-03T04:05:06")
    row = db.one("SELECT * FROM files")
    assert row["date_source"] == "manual"
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # файл «изменился»
    report = scanner.scan_root(db, cfg, root["id"], FakeCtx())
    assert report["updated"] == 1
    row = db.one("SELECT * FROM files")
    assert row["manual_taken_at"] == "2001-02-03T04:05:06" and row["date_source"] == "manual"
    catalog.set_manual_date(db, [fid], None)
    assert db.one("SELECT * FROM files")["date_source"] == "exif"


def test_groups_and_primary(env, tmp_path):
    d = tmp_path / "p"
    make_jpeg(d / "IMG_0001.JPG", "2024:07:15 10:15:00")
    write(str(d / "IMG_0001.CR2"))
    write(str(d / "IMG_0002.MOV"))
    make_jpeg(d / "IMG_0002.HEIC")  # притворяется HEIC — важно только расширение
    make_jpeg(d / "alone.jpg")
    scan(env, d)
    rows = env[1].query("SELECT * FROM files")
    keys = {r["rel_path"]: r["group_key"] for r in rows}
    assert keys["IMG_0001.JPG"] == keys["IMG_0001.CR2"] is not None
    assert keys["IMG_0002.MOV"] == keys["IMG_0002.HEIC"] is not None
    assert keys["alone.jpg"] is None
    members = [dict(r) for r in rows if r["rel_path"].startswith("IMG_0001")]
    assert catalog.primary_of(members)["ext"] == "jpg"
    members = [dict(r) for r in rows if r["rel_path"].startswith("IMG_0002")]
    assert catalog.primary_of(members)["ext"] == "heic"


def test_catalog_feed_grouping_and_filters(env, tmp_path):
    db = env[1]
    d = tmp_path / "p"
    make_jpeg(d / "a.jpg", "2024:07:15 10:00:00")
    make_jpeg(d / "b.jpg", "2024:07:15 11:00:00")
    make_jpeg(d / "c.jpg", "2023:01:01 11:00:00")
    write(str(d / "v.mp4"))
    scan(env, d)
    feed = catalog.list_catalog(db, sort="desc", kind="photo")
    assert [x["day"] for x in feed["days"]] == ["2024-07-15", "2023-01-01"]
    assert feed["days"][0]["count"] == 2
    feed = catalog.list_catalog(db, sort="asc", kind="photo")
    assert feed["days"][0]["day"] == "2023-01-01"
    assert catalog.list_catalog(db, kind="video")["total"] == 1


def test_day_boundary():
    assert apply_day_boundary(datetime(2024, 7, 15, 1, 30), 4) == datetime(2024, 7, 14, 1, 30)
    assert apply_day_boundary(datetime(2024, 7, 15, 5, 0), 4) == datetime(2024, 7, 15, 5, 0)
    assert apply_day_boundary(datetime(2024, 7, 15, 0, 30), 0) == datetime(2024, 7, 15, 0, 30)
