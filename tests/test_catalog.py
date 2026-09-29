from datetime import datetime

from app import catalog
from app.metadata import date_from_filename, _parse_video_dt
from app.scanner import add_root, scan_root
from conftest import make_jpeg, set_mtime


def scan(db, settings, path):
    root = add_root(db, str(path))
    scan_root(db, root["id"], settings)
    return root


def row(db, rel):
    return db.conn().execute("SELECT * FROM files WHERE rel_path=?", (rel,)).fetchone()


def test_date_from_exif(db, settings, tmp_path):
    make_jpeg(tmp_path / "p" / "x.jpg", datetime(2023, 5, 6, 7, 8, 9))
    scan(db, settings, tmp_path / "p")
    r = row(db, "x.jpg")
    assert (r["taken_at"], r["date_source"]) == ("2023-05-06T07:08:09", "exif")


def test_date_from_filename(db, settings, tmp_path):
    make_jpeg(tmp_path / "p" / "IMG_20240715_101500.jpg")
    scan(db, settings, tmp_path / "p")
    r = row(db, "IMG_20240715_101500.jpg")
    assert (r["taken_at"], r["date_source"]) == ("2024-07-15T10:15:00", "filename")


def test_date_from_mtime_with_low_confidence(db, settings, tmp_path):
    p = make_jpeg(tmp_path / "p" / "plain.jpg")
    set_mtime(p, datetime(2020, 2, 3, 4, 5, 6))
    scan(db, settings, tmp_path / "p")
    r = row(db, "plain.jpg")
    assert r["date_source"] == "mtime" and r["taken_at"].startswith("2020-02-03")
    assert r["date_confidence"] < 0.5


def test_filename_patterns():
    assert date_from_filename("WhatsApp Image 2024-07-15 at 10.15.00.jpeg").strftime("%Y-%m-%d") == "2024-07-15"
    assert date_from_filename("PXL_20230102_030405123.jpg").strftime("%Y%m%d") == "20230102"
    assert date_from_filename("IMG_1234.jpg") is None
    assert date_from_filename("20241340_x.jpg") is None  # месяц 13


def test_video_time_policy():
    assert _parse_video_dt("2024-07-15T10:15:00.000000Z", "local") == datetime(2024, 7, 15, 10, 15)
    assert _parse_video_dt("1904-01-01T00:00:00Z", "local") is None
    assert _parse_video_dt("2024-07-15T10:15:00+0300", "local") == datetime(2024, 7, 15, 10, 15)


def test_video_without_ffprobe_falls_back_to_filename(db, settings, tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "VID_20240102_030405.mp4").write_bytes(b"0" * 20)
    scan(db, settings, tmp_path / "p")
    r = row(db, "VID_20240102_030405.mp4")
    assert r["kind"] == "video" and r["date_source"] == "filename"


def test_manual_date_wins_and_survives_rescan(db, settings, tmp_path):
    p = make_jpeg(tmp_path / "p" / "a.jpg", datetime(2023, 1, 1, 1, 1, 1))
    root = scan(db, settings, tmp_path / "p")
    fid = row(db, "a.jpg")["id"]
    catalog.set_manual_date(db, [fid], "2019-09-09T09:09:09")
    assert row(db, "a.jpg")["effective_at"] == "2019-09-09T09:09:09"
    make_jpeg(p, datetime(2023, 1, 1, 1, 1, 1), text_seed=9)  # файл изменился -> перечитывается
    scan_root(db, root["id"], settings)
    r = row(db, "a.jpg")
    assert r["manual_taken_at"] == "2019-09-09T09:09:09"
    assert r["effective_at"] == "2019-09-09T09:09:09"
    catalog.set_manual_date(db, [fid], None)
    assert row(db, "a.jpg")["effective_at"] == "2023-01-01T01:01:01"


def test_undated_file_has_no_effective_date(db, settings, tmp_path, monkeypatch):
    make_jpeg(tmp_path / "p" / "a.jpg")
    monkeypatch.setattr(catalog, "datetime", type("D", (), {
        "fromtimestamp": staticmethod(lambda *_: (_ for _ in ()).throw(OverflowError())),
        "strptime": datetime.strptime}))
    scan(db, settings, tmp_path / "p")
    r = row(db, "a.jpg")
    assert r["taken_at"] is None and r["effective_at"] is None


def test_raw_jpeg_group_shares_primary_date(db, settings, tmp_path):
    make_jpeg(tmp_path / "p" / "IMG_0001.JPG", datetime(2022, 3, 3, 3, 3, 3))
    (tmp_path / "p" / "IMG_0001.CR2").write_bytes(b"raw" * 10)
    scan(db, settings, tmp_path / "p")
    a, b = row(db, "IMG_0001.JPG"), row(db, "IMG_0001.CR2")
    assert a["group_key"] and a["group_key"] == b["group_key"]
    assert b["effective_at"] == "2022-03-03T03:03:03"


def test_timeline_grouping_and_sorting(db, settings, tmp_path):
    make_jpeg(tmp_path / "p" / "a.jpg", datetime(2024, 1, 1, 9, 0, 0), text_seed=1)
    make_jpeg(tmp_path / "p" / "b.jpg", datetime(2024, 1, 1, 18, 0, 0), text_seed=2)
    make_jpeg(tmp_path / "p" / "c.jpg", datetime(2024, 2, 5, 8, 0, 0), text_seed=3)
    scan(db, settings, tmp_path / "p")
    days = catalog.query_days(db, sort="desc")
    assert [(d["day"], d["count"]) for d in days] == [("2024-02-05", 1), ("2024-01-01", 2)]
    assert [d["day"] for d in catalog.query_days(db, sort="asc")] == ["2024-01-01", "2024-02-05"]
    files = catalog.query_files(db, day="2024-01-01", sort="asc")
    assert [f["rel_path"] for f in files] == ["a.jpg", "b.jpg"]
    assert catalog.query_files(db, kind="video") == []
