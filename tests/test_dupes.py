import os

from app import catalog, dupes, hashing
from tests.conftest import FakeCtx, make_jpeg, scan, write


def find(env):
    cfg, db = env
    return dupes.find_duplicates(db, cfg, FakeCtx())


def test_exact_duplicates_only(env, tmp_path):
    db = env[1]
    d = tmp_path / "p"
    write(str(d / "a" / "one.jpg"), b"same-content")
    write(str(d / "b" / "two.jpg"), b"same-content")
    write(str(d / "b" / "diff-same-size.jpg"), b"SAME-CONTENT")
    write(str(d / "c" / "unique.jpg"), b"different length!")
    scan(env, d)
    res = find(env)
    assert res["groups"] == 1
    groups = dupes.list_groups(db)
    assert {m["rel_path"] for m in groups[0]["members"]} == {"a/one.jpg", "b/two.jpg"}
    # уникальный размер не хешируется на этапе поиска
    assert db.one("SELECT sha256 FROM files WHERE rel_path='c/unique.jpg'")["sha256"] is None


def test_hash_cache_used(env, tmp_path, monkeypatch):
    d = tmp_path / "p"
    write(str(d / "a.jpg"), b"dup")
    write(str(d / "b.jpg"), b"dup")
    scan(env, d)
    find(env)
    calls = []
    real = hashing.sha256_file
    monkeypatch.setattr(hashing, "sha256_file", lambda *a, **k: calls.append(a) or real(*a, **k))
    find(env)
    assert calls == []


def test_linked_and_derived_files_excluded(env, tmp_path):
    db = env[1]
    d = tmp_path / "p"
    write(str(d / "IMG_1.HEIC"), b"same")
    write(str(d / "IMG_1.MOV"), b"same")  # Live Photo
    write(str(d / "x.jpg"), b"jpegjpeg")
    write(str(d / "y" / "x_conv.jpg"), b"jpegjpeg")
    scan(env, d)
    src = db.one("SELECT id FROM files WHERE rel_path='x.jpg'")["id"]
    der = db.one("SELECT id FROM files WHERE rel_path='y/x_conv.jpg'")["id"]
    db.execute("INSERT INTO derived_files VALUES(?,?, 'heic_to_jpeg')", (der, src))
    assert find(env)["groups"] == 0


def test_keeper_rule_and_manual_choice(env, tmp_path):
    cfg, db = env
    d = tmp_path / "p"
    write(str(d / "inbox" / "a.jpg"), b"dup")
    write(str(d / "2024" / "07" / "15" / "longer_name.jpg"), b"dup")
    write(str(d / "z.jpg"), b"dup")
    scan(env, d)
    find(env)
    g = dupes.list_groups(db)[0]
    keeper = [m for m in g["members"] if m["is_keeper"]][0]
    assert keeper["rel_path"] == "2024/07/15/longer_name.jpg"
    other = [m for m in g["members"] if m["rel_path"] == "inbox/a.jpg"][0]
    dupes.set_keeper(db, g["id"], other["id"])
    find(env)  # ручной выбор переживает пересчёт
    g = dupes.list_groups(db)[0]
    assert [m["rel_path"] for m in g["members"] if m["is_keeper"]] == ["inbox/a.jpg"]


def test_quarantine_and_restore(env, tmp_path):
    cfg, db = env
    d = tmp_path / "p"
    write(str(d / "a.jpg"), b"dup")
    write(str(d / "sub" / "b.jpg"), b"dup")
    scan(env, d)
    find(env)
    g = dupes.list_groups(db)[0]
    res = dupes.process_group(db, g["id"])
    assert res == {"quarantined": 1, "failed": 0}
    q = dupes.list_quarantine(db)
    assert len(q) == 1 and q[0]["rel_path"].startswith("_duplicates/")
    assert os.path.exists(os.path.join(str(d), q[0]["rel_path"]))
    assert db.one("SELECT COUNT(*) n FROM operations WHERE kind='quarantine' AND status='done'")["n"] == 1
    out = dupes.restore_file(db, q[0]["id"])
    assert out["restored"]
    row = db.one("SELECT * FROM files WHERE id=?", (q[0]["id"],))
    assert row["status"] == "present" and not row["rel_path"].startswith("_duplicates")
    assert dupes.list_quarantine(db) == []


def test_restore_blocked_when_path_taken(env, tmp_path):
    cfg, db = env
    d = tmp_path / "p"
    write(str(d / "a.jpg"), b"dup")
    write(str(d / "sub" / "b.jpg"), b"dup")
    scan(env, d)
    find(env)
    g = dupes.list_groups(db)[0]
    victim = [m for m in g["members"] if not m["is_keeper"]][0]
    orig = victim["rel_path"]
    dupes.process_group(db, g["id"])
    write(os.path.join(str(d), *orig.split("/")), b"new occupant")
    assert not dupes.restore_file(db, victim["id"])["restored"]
    assert db.one("SELECT status FROM files WHERE id=?", (victim["id"],))["status"] == "quarantined"
