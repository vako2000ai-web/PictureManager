from datetime import datetime
from pathlib import Path

from app import duplicates, ops
from app.hashing import ensure_hash
from app.scanner import add_root, scan_root
from conftest import make_jpeg


def setup_dups(db, settings, tmp_path):
    base = tmp_path / "lib"
    make_jpeg(base / "a" / "one.jpg", datetime(2024, 1, 1, 1, 1, 1), text_seed=1)
    (base / "b").mkdir(parents=True)
    (base / "b" / "copy_of_one.jpg").write_bytes((base / "a" / "one.jpg").read_bytes())
    (base / "2024" / "01" / "01").mkdir(parents=True)
    (base / "2024" / "01" / "01" / "same.jpg").write_bytes((base / "a" / "one.jpg").read_bytes())
    make_jpeg(base / "a" / "other.jpg", datetime(2024, 1, 2, 1, 1, 1), text_seed=2, size=(96, 72))
    root = add_root(db, str(base))
    scan_root(db, root["id"], settings)
    return base, root


def test_exact_duplicates_grouped_and_different_not(db, settings, tmp_path):
    setup_dups(db, settings, tmp_path)
    res = duplicates.find_duplicates(db)
    assert res["groups"] == 1
    g = duplicates.list_groups(db)[0]
    assert sorted(Path(m["rel_path"]).name for m in g["members"]) == ["copy_of_one.jpg", "one.jpg", "same.jpg"]


def test_unique_size_files_are_not_hashed(db, settings, tmp_path):
    setup_dups(db, settings, tmp_path)
    duplicates.find_duplicates(db)
    hashed = {r["rel_path"] for r in db.conn().execute("SELECT rel_path FROM files WHERE sha256 IS NOT NULL")}
    assert "a/other.jpg" not in hashed and "a/one.jpg" in hashed


def test_hash_cache_is_used(db, settings, tmp_path, monkeypatch):
    setup_dups(db, settings, tmp_path)
    duplicates.find_duplicates(db)
    import app.hashing as h
    monkeypatch.setattr(h, "sha256_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("reread")))
    duplicates.find_duplicates(db)  # второй проход читает хеши из кэша


def test_default_keeper_prefers_target_structure(db, settings, tmp_path):
    setup_dups(db, settings, tmp_path)
    duplicates.find_duplicates(db)
    g = duplicates.list_groups(db)[0]
    keeper = [m for m in g["members"] if m["is_keeper"]][0]
    assert keeper["rel_path"] == "2024/01/01/same.jpg"


def test_manual_keeper_choice_is_kept(db, settings, tmp_path):
    setup_dups(db, settings, tmp_path)
    duplicates.find_duplicates(db)
    g = duplicates.list_groups(db)[0]
    pick = [m for m in g["members"] if m["rel_path"] == "b/copy_of_one.jpg"][0]
    duplicates.set_keeper(db, g["id"], pick["id"])
    duplicates.find_duplicates(db)  # пересчёт не сбрасывает выбор
    g = duplicates.list_groups(db)[0]
    assert [m for m in g["members"] if m["is_keeper"]][0]["id"] == pick["id"]


def test_related_and_derived_files_are_not_duplicates(db, settings, tmp_path):
    base = tmp_path / "lib"
    base.mkdir()
    (base / "IMG_1.heic").write_bytes(b"same-bytes" * 5)
    (base / "IMG_1.mov").write_bytes(b"same-bytes" * 5)  # Live Photo с совпавшим содержимым
    (base / "x.jpg").write_bytes(b"other" * 5)
    (base / "y.jpg").write_bytes(b"other" * 5)
    root = add_root(db, str(base))
    scan_root(db, root["id"], settings)
    ids = {r["rel_path"]: r["id"] for r in db.conn().execute("SELECT id, rel_path FROM files")}
    db.conn().execute("INSERT INTO derived_files(file_id, source_file_id, op) VALUES (?,?,?)",
                      (ids["y.jpg"], ids["x.jpg"], "heic->jpg"))
    duplicates.find_duplicates(db)
    assert duplicates.list_groups(db) == []


def test_quarantine_and_restore(db, settings, tmp_path):
    base, root = setup_dups(db, settings, tmp_path)
    duplicates.find_duplicates(db)
    g = duplicates.list_groups(db)[0]
    res = duplicates.quarantine_group(db, g["id"])
    assert res["quarantined"] == 2 and not res["errors"]
    assert (base / "2024" / "01" / "01" / "same.jpg").exists()          # оригинал на месте
    assert not (base / "b" / "copy_of_one.jpg").exists()                 # копия ушла
    q = list((base / "_duplicates").iterdir())
    assert len(q) == 2
    assert db.conn().execute("SELECT COUNT(*) FROM files WHERE status='quarantined'").fetchone()[0] == 2
    assert duplicates.list_groups(db) == []
    fid = db.conn().execute("SELECT id FROM files WHERE rel_path LIKE '_duplicates/%' AND rel_path LIKE '%copy_of_one%'").fetchone()["id"]
    ops.restore_file(db, fid)
    assert (base / "b" / "copy_of_one.jpg").exists()
    assert db.conn().execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()["status"] == "present"
    kinds = [r["kind"] for r in db.conn().execute("SELECT kind FROM operations ORDER BY id")]
    assert kinds.count("quarantine") == 2 and kinds.count("restore") == 1
