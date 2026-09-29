from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image

from app import catalog, converter, duplicates
from app.scanner import add_root, scan_root

DATE = datetime(2023, 8, 9, 10, 11, 12)


def make_heic(path: Path, orientation: int = 1, with_icc: bool = True, size=(80, 40)):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, (10, 120, 200))
    for x in range(10):
        for y in range(10):
            img.putpixel((x, y), (255, 0, 0))          # красный угол: по нему проверяем поворот
    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = DATE.strftime("%Y:%m:%d %H:%M:%S")
    exif[0x0112] = orientation
    kwargs = {"exif": exif.tobytes()}
    if with_icc:
        kwargs["icc_profile"] = Image.new("RGB", (1, 1)).info.get("icc_profile") or _fake_icc()
    pillow_heif.from_pillow(img).save(path, quality=90, **kwargs)
    return path


def _fake_icc() -> bytes:
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def scan(db, settings, base):
    root = add_root(db, str(base))
    scan_root(db, root["id"], settings)
    return root


def ids_of_heic(db):
    return [r["id"] for r in db.conn().execute("SELECT id FROM files WHERE ext='.heic'")]


def test_convert_keeps_date_and_icc_and_registers_derived(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "IMG_1.heic")
    scan(db, settings, base)
    res = converter.convert_heic_files(db, ids_of_heic(db), settings)
    assert res["converted"] == 1 and not res["errors"]
    out = base / "IMG_1.jpg"
    with Image.open(out) as im:
        assert im.format == "JPEG"
        assert im.info.get("icc_profile")
        ex = im.getexif()
        assert ex.get_ifd(0x8769)[0x9003] == DATE.strftime("%Y:%m:%d %H:%M:%S")
    d = db.conn().execute("SELECT f.rel_path, f.taken_at, f.date_source FROM derived_files d "
                          "JOIN files f ON f.id=d.file_id").fetchone()
    assert d["rel_path"] == "IMG_1.jpg" and d["taken_at"].startswith("2023-08-09")
    assert (base / "IMG_1.heic").exists()                           # оригинал по умолчанию остаётся


def test_converted_date_survives_rescan(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "IMG_1.heic")
    root = scan(db, settings, base)
    converter.convert_heic_files(db, ids_of_heic(db), settings)
    scan_root(db, root["id"], settings)
    r = db.conn().execute("SELECT taken_at, date_source FROM files WHERE ext='.jpg'").fetchone()
    assert r["taken_at"] == "2023-08-09T10:11:12" and r["date_source"] == "exif"


def test_orientation_applied_to_pixels_once(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "rot.heic", orientation=6, size=(80, 40))  # 6 = повернуть на 90° по часовой
    scan(db, settings, base)
    converter.convert_heic_files(db, ids_of_heic(db), settings)
    with Image.open(base / "rot.jpg") as im:
        assert im.getexif().get(0x0112, 1) == 1                  # тега поворота нет — нет двойного поворота
        assert im.size == (40, 80)                               # кадр повёрнут в пикселях


def test_name_conflict_never_overwrites(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "IMG_1.heic")
    (base / "IMG_1.jpg").write_bytes(b"precious")
    scan(db, settings, base)
    converter.convert_heic_files(db, ids_of_heic(db), settings)
    assert (base / "IMG_1.jpg").read_bytes() == b"precious"
    assert (base / "IMG_1_1.jpg").exists()


def test_quality_setting_changes_size(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "a.heic", size=(400, 300))
    make_heic(base / "b.heic", size=(400, 300))
    scan(db, settings, base)
    ids = {r["rel_path"]: r["id"] for r in db.conn().execute("SELECT id, rel_path FROM files")}
    settings.jpeg_quality = 95
    converter.convert_heic_files(db, [ids["a.heic"]], settings)
    settings.jpeg_quality = 30
    converter.convert_heic_files(db, [ids["b.heic"]], settings)
    assert (base / "a.jpg").stat().st_size > (base / "b.jpg").stat().st_size


def test_corrupt_file_is_reported_and_others_continue(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "good.heic")
    (base / "bad.heic").write_bytes(b"not a heic at all")
    scan(db, settings, base)
    res = converter.convert_heic_files(db, ids_of_heic(db), settings)
    assert res["converted"] == 1 and len(res["errors"]) == 1
    assert not list(base.glob("*.pmtmp"))


def test_not_converted_twice(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "a.heic")
    scan(db, settings, base)
    converter.convert_heic_files(db, ids_of_heic(db), settings)
    res = converter.convert_heic_files(db, ids_of_heic(db), settings)
    assert res["converted"] == 0 and res["skipped"] == 1


def test_live_photo_mov_untouched_and_pair_stays_grouped(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "IMG_9.heic")
    (base / "IMG_9.mov").write_bytes(b"video" * 20)
    scan(db, settings, base)
    converter.convert_heic_files(db, ids_of_heic(db), settings)
    assert (base / "IMG_9.mov").read_bytes() == b"video" * 20
    keys = {r["group_key"] for r in db.conn().execute("SELECT group_key FROM files")}
    assert len(keys) == 1 and None not in keys


def test_derived_jpeg_is_not_a_duplicate_and_originals_can_be_quarantined(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_heic(base / "a.heic")
    scan(db, settings, base)
    sid = ids_of_heic(db)[0]
    assert converter.quarantine_originals(db, [sid])["errors"]     # без копии — отказ
    converter.convert_heic_files(db, [sid], settings)
    duplicates.find_duplicates(db)
    assert duplicates.list_groups(db) == []
    res = converter.quarantine_originals(db, [sid])
    assert res["quarantined"] == 1 and not (base / "a.heic").exists()
    assert (base / "a.jpg").exists() and list((base / "_duplicates").iterdir())
    assert db.conn().execute("SELECT COUNT(*) FROM operations WHERE kind='quarantine'").fetchone()[0] == 1
