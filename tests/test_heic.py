import os

import piexif
import pillow_heif
import pytest
from PIL import Image

from app import catalog, heic
from tests.conftest import FakeCtx, scan, write

pillow_heif.register_heif_opener()


def make_heic(path, date="2024:07:15 10:15:00", orientation=1, icc=None, size=(16, 8)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGB", size, (10, 200, 10))
    img.paste((255, 0, 0), (0, 0, size[0] // 2, size[1]))  # левая половина красная
    exif = {"0th": {piexif.ImageIFD.Orientation: orientation},
            "Exif": {piexif.ExifIFD.DateTimeOriginal: date.encode()}}
    kw = {"exif": piexif.dump(exif), "quality": 90}
    if icc:
        kw["icc_profile"] = icc
    try:
        img.save(path, "HEIF", **kw)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"HEIC-энкодер недоступен: {exc}")
    return str(path)


def convert(env, ids, quality=None):
    cfg, db = env
    return heic.convert_files(db, cfg, ids, FakeCtx(), quality)


def test_convert_preserves_date_and_registers_derived(env, tmp_path):
    cfg, db = env
    d = tmp_path / "p"
    make_heic(d / "IMG_1.HEIC")
    scan(env, d)
    src_id = db.one("SELECT id FROM files WHERE ext='heic'")["id"]
    report = convert(env, [src_id])
    assert report["converted"] == 1 and not report["errors"]
    out = d / "IMG_1.jpg"
    assert out.exists() and (d / "IMG_1.HEIC").exists()  # оригинал на месте
    with Image.open(out) as im:
        assert im.format == "JPEG"
        ex = piexif.load(im.info["exif"])
        assert ex["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2024:07:15 10:15:00"
    row = db.one("SELECT * FROM files WHERE ext='jpg'")
    assert row["taken_at"] == "2024-07-15T10:15:00"
    der = db.one("SELECT * FROM derived_files WHERE file_id=?", (row["id"],))
    assert der["source_file_id"] == src_id and der["op"] == "heic_to_jpeg"


def test_no_double_rotation(env, tmp_path):
    d = tmp_path / "p"
    make_heic(d / "r.HEIC", orientation=6, size=(16, 8))
    scan(env, d)
    convert(env, [env[1].one("SELECT id FROM files")["id"]])
    with Image.open(d / "r.jpg") as im:
        assert im.size in ((16, 8), (8, 16))
        orient = piexif.load(im.info["exif"])["0th"].get(piexif.ImageIFD.Orientation, 1)
        assert orient == 1  # тег сброшен


def test_icc_profile_and_quality(env, tmp_path):
    from PIL import ImageCms

    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    d = tmp_path / "p"
    make_heic(d / "c.HEIC", icc=icc)
    scan(env, d)
    convert(env, [env[1].one("SELECT id FROM files")["id"]], quality=80)
    with Image.open(d / "c.jpg") as im:
        assert im.info.get("icc_profile") == icc
    hi = tmp_path / "hi.jpg"
    lo = tmp_path / "lo.jpg"
    heic.convert_file(str(d / "c.HEIC"), str(hi), 95)
    heic.convert_file(str(d / "c.HEIC"), str(lo), 20)
    assert hi.stat().st_size > lo.stat().st_size


def test_name_conflict_gets_suffix(env, tmp_path):
    d = tmp_path / "p"
    make_heic(d / "IMG_2.HEIC")
    write(str(d / "IMG_2.jpg"), b"existing")
    scan(env, d)
    hid = env[1].one("SELECT id FROM files WHERE ext='heic'")["id"]
    convert(env, [hid])
    assert (d / "IMG_2_1.jpg").exists()
    assert (d / "IMG_2.jpg").read_bytes() == b"existing"


def test_corrupt_file_reported_others_continue(env, tmp_path):
    d = tmp_path / "p"
    make_heic(d / "good.HEIC")
    write(str(d / "bad.HEIC"), b"garbage")
    scan(env, d)
    ids = [r["id"] for r in env[1].query("SELECT id FROM files")]
    report = convert(env, ids)
    assert report["converted"] == 1 and len(report["errors"]) == 1
    assert (d / "good.jpg").exists() and not (d / "bad.jpg").exists() and not list(d.glob("*.part"))


def test_live_photo_mov_untouched_and_group_kept(env, tmp_path):
    d = tmp_path / "p"
    make_heic(d / "L.HEIC")
    write(str(d / "L.MOV"), b"movie")
    scan(env, d)
    hid = env[1].one("SELECT id FROM files WHERE ext='heic'")["id"]
    convert(env, [hid])
    assert (d / "L.MOV").read_bytes() == b"movie"
    keys = {r["ext"]: r["group_key"] for r in env[1].query("SELECT ext, group_key FROM files")}
    assert keys["mov"] and keys["mov"] == keys["heic"] == keys["jpg"]


def test_originals_to_quarantine_only_on_explicit_action(env, tmp_path):
    cfg, db = env
    d = tmp_path / "p"
    make_heic(d / "q.HEIC")
    scan(env, d)
    hid = db.one("SELECT id FROM files")["id"]
    convert(env, [hid])
    assert db.one("SELECT status FROM files WHERE id=?", (hid,))["status"] == "present"
    res = heic.quarantine_originals(db)
    assert res["quarantined"] == 1
    assert db.one("SELECT status FROM files WHERE id=?", (hid,))["status"] == "quarantined"
    assert (d / "_duplicates" / "q.HEIC").exists() and (d / "q.jpg").exists()
    assert db.one("SELECT COUNT(*) n FROM operations WHERE kind='quarantine' AND status='done'")["n"] == 1


def test_convert_is_idempotent(env, tmp_path):
    d = tmp_path / "p"
    make_heic(d / "i.HEIC")
    scan(env, d)
    hid = env[1].one("SELECT id FROM files")["id"]
    convert(env, [hid])
    assert convert(env, [hid])["skipped_existing"] == 1
    assert not (d / "i_1.jpg").exists()
