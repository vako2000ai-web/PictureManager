import os
from datetime import datetime
from pathlib import Path

import pytest

from app import ops, organizer
from app.errors import IntegrityError
from app.hashing import sha256_file
from app.scanner import add_root, scan_root
from conftest import make_jpeg


def prep(db, settings, tmp_path, files):
    src = tmp_path / "src"
    for rel, dt, seed in files:
        make_jpeg(src / rel, dt, text_seed=seed)
    root = add_root(db, str(src))
    scan_root(db, root["id"], settings)
    ids = [r["id"] for r in db.conn().execute("SELECT id FROM files ORDER BY id")]
    return src, ids


def test_plan_layout_summary_and_no_side_effects(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1),
                                              ("b.jpg", None, 2)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    items = organizer.get_plan(db, plan["id"])["items"]
    dsts = {Path(i["src"]).name: Path(i["dst"]) for i in items}
    dest = Path(plan["dest_path"])
    assert dsts["a.jpg"] == dest / "2024" / "07" / "14" / "a.jpg"
    assert dsts["b.jpg"].parent.parts[-3:] != ("_без даты",)  # b.jpg датируется по mtime, а не «без даты»
    assert plan["summary"]["files"] == 2 and plan["summary"]["enough_space"]
    assert (src / "a.jpg").exists() and not (dest / "2024").exists()   # план ничего не двигает


def test_undated_goes_to_special_folder(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    db.conn().execute("UPDATE files SET effective_at=NULL")
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    it = organizer.get_plan(db, plan["id"])["items"][0]
    assert Path(it["dst"]).parent.name == "_без даты" and plan["summary"]["undated"] == 1


def test_day_boundary(db, settings, tmp_path):
    settings.day_boundary_hour = 4
    assert organizer.dest_folder("2024-07-15T01:30:00", settings) == "2024/07/14"
    assert organizer.dest_folder("2024-07-15T05:00:00", settings) == "2024/07/15"


def test_execute_requires_confirmation(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    with pytest.raises(ValueError):
        organizer.execute_plan(db, plan["id"], confirmed=False)
    assert (src / "a.jpg").exists()


def test_execute_same_volume_and_db_update(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    res = organizer.execute_plan(db, plan["id"], confirmed=True)
    assert res["moved"] == 1 and not res["errors"]
    moved = Path(plan["dest_path"]) / "2024" / "07" / "14" / "a.jpg"
    assert moved.exists() and not (src / "a.jpg").exists()
    row = db.conn().execute("SELECT f.rel_path, r.path FROM files f JOIN roots r ON r.id=f.root_id").fetchone()
    assert row["rel_path"] == "2024/07/14/a.jpg" and row["path"] == plan["dest_path"]
    assert organizer.get_plan(db, plan["id"])["status"] == "done"


def test_cross_volume_copy_verify_then_delete(db, settings, tmp_path, monkeypatch):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    original = (src / "a.jpg").read_bytes()
    monkeypatch.setattr(ops, "same_volume", lambda *a: False)
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    res = organizer.execute_plan(db, plan["id"], confirmed=True)
    moved = Path(plan["dest_path"]) / "2024" / "07" / "14" / "a.jpg"
    assert res["moved"] == 1 and moved.read_bytes() == original and not (src / "a.jpg").exists()
    assert not list(Path(plan["dest_path"]).rglob("*.pmtmp"))


def test_hash_mismatch_keeps_original(db, settings, tmp_path, monkeypatch):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    monkeypatch.setattr(ops, "same_volume", lambda *a: False)
    monkeypatch.setattr(ops, "sha256_file", lambda *a, **k: "0" * 64)
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    res = organizer.execute_plan(db, plan["id"], confirmed=True)
    assert res["moved"] == 0 and len(res["errors"]) == 1
    assert (src / "a.jpg").exists()
    dest = Path(plan["dest_path"])
    assert not list(dest.rglob("a.jpg")) and not list(dest.rglob("*.pmtmp"))
    op = db.conn().execute("SELECT status FROM operations").fetchone()
    assert op["status"] == "error"
    assert db.conn().execute("SELECT rel_path FROM files").fetchone()["rel_path"] == "a.jpg"


def test_name_conflict_gets_suffix_and_same_content_goes_to_quarantine(db, settings, tmp_path):
    dt = datetime(2024, 7, 14, 10, 0, 0)
    src = tmp_path / "src"
    make_jpeg(src / "x" / "IMG.jpg", dt, text_seed=1)
    make_jpeg(src / "y" / "IMG.jpg", dt, text_seed=2)            # то же имя, другое содержимое
    (src / "z").mkdir()
    (src / "z" / "IMG.jpg").write_bytes((src / "x" / "IMG.jpg").read_bytes())  # то же содержимое
    root = add_root(db, str(src))
    scan_root(db, root["id"], settings)
    ids = [r["id"] for r in db.conn().execute("SELECT id FROM files ORDER BY rel_path")]
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    items = organizer.get_plan(db, plan["id"])["items"]
    outcome = sorted((i["action"], Path(i["dst"]).name if i["action"] == "move" else "") for i in items)
    # Порядок обработки не важен: одно имя свободно, другое получает суффикс, точная копия уходит в карантин.
    assert outcome == [("move", "IMG.jpg"), ("move", "IMG_1.jpg"), ("quarantine", "")]
    q = [i for i in items if i["action"] == "quarantine"][0]
    assert Path(q["src"]).parent.name in ("x", "z")            # карантин получает копию одинакового содержимого
    organizer.execute_plan(db, plan["id"], confirmed=True)
    dest = Path(plan["dest_path"])
    assert (dest / "2024/07/14/IMG.jpg").exists() and (dest / "2024/07/14/IMG_1.jpg").exists()
    assert len(list((dest / "_duplicates").iterdir())) == 1


def test_existing_file_in_destination_is_not_overwritten(db, settings, tmp_path):
    dt = datetime(2024, 7, 14, 10, 0, 0)
    src = tmp_path / "src"
    make_jpeg(src / "IMG.jpg", dt, text_seed=1)
    dest = tmp_path / "dest"
    make_jpeg(dest / "2024" / "07" / "14" / "IMG.jpg", dt, text_seed=9)
    keep = (dest / "2024" / "07" / "14" / "IMG.jpg").read_bytes()
    root = add_root(db, str(src))
    scan_root(db, root["id"], settings)
    ids = [r["id"] for r in db.conn().execute("SELECT id FROM files")]
    plan = organizer.build_plan(db, ids, str(dest), settings)
    organizer.execute_plan(db, plan["id"], confirmed=True)
    assert (dest / "2024/07/14/IMG.jpg").read_bytes() == keep
    assert (dest / "2024/07/14/IMG_1.jpg").exists()


def test_related_files_move_together(db, settings, tmp_path):
    src = tmp_path / "src"
    make_jpeg(src / "IMG_5.HEIC.jpg".replace(".HEIC", ""), datetime(2023, 3, 3, 3, 3, 3), text_seed=5)
    (src / "IMG_5.CR2").write_bytes(b"raw" * 10)
    root = add_root(db, str(src))
    scan_root(db, root["id"], settings)
    jpg_id = db.conn().execute("SELECT id FROM files WHERE rel_path='IMG_5.jpg'").fetchone()["id"]
    plan = organizer.build_plan(db, [jpg_id], str(tmp_path / "dest"), settings)  # выбран только JPG
    organizer.execute_plan(db, plan["id"], confirmed=True)
    d = Path(plan["dest_path"]) / "2023" / "03" / "03"
    assert (d / "IMG_5.jpg").exists() and (d / "IMG_5.CR2").exists()


def test_changed_file_is_skipped(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    with open(src / "a.jpg", "ab") as f:
        f.write(b"tail")                                   # файл изменён после построения плана
    res = organizer.execute_plan(db, plan["id"], confirmed=True)
    assert res["moved"] == 0 and res["skipped"] == 1 and "пропущен" in res["errors"][0]["error"]
    assert (src / "a.jpg").exists()


def test_cancel_leaves_journal_consistent(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [(f"{i}.jpg", datetime(2024, 7, 14, 10, 0, i), i) for i in range(4)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)

    class Ctx:
        n = 0
        def cancelled(self): return self.n >= 2
        def set_total(self, **k): pass
        def add(self, bytes=0, files=0, **k): self.n += files

    res = organizer.execute_plan(db, plan["id"], confirmed=True, ctx=Ctx())
    assert res["cancelled"] and res["moved"] == 2
    assert organizer.get_plan(db, plan["id"])["status"] == "partial"
    assert db.conn().execute("SELECT COUNT(*) FROM operations WHERE status='pending'").fetchone()[0] == 0


def test_rollback_restores_original_paths(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1),
                                              ("b.jpg", datetime(2024, 7, 15, 10, 0, 0), 2)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    organizer.execute_plan(db, plan["id"], confirmed=True)
    res = organizer.rollback_plan(db, plan["id"])
    assert res["rolled_back"] == 2 and not res["skipped"]
    assert (src / "a.jpg").exists() and (src / "b.jpg").exists()
    rows = db.conn().execute("SELECT f.rel_path, r.path FROM files f JOIN roots r ON r.id=f.root_id ORDER BY f.id").fetchall()
    assert [r["rel_path"] for r in rows] == ["a.jpg", "b.jpg"] and rows[0]["path"] == str(src.resolve())


def test_rollback_skips_modified_file(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    plan = organizer.build_plan(db, ids, str(tmp_path / "dest"), settings)
    organizer.execute_plan(db, plan["id"], confirmed=True)
    moved = Path(plan["dest_path"]) / "2024/07/14/a.jpg"
    with open(moved, "ab") as f:
        f.write(b"edit")
    res = organizer.rollback_plan(db, plan["id"])
    assert res["rolled_back"] == 0 and res["skipped"] and moved.exists()


def test_recovery_after_crash_between_replace_and_source_delete(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    row = db.conn().execute("SELECT * FROM files").fetchone()
    sha = sha256_file(src / "a.jpg")
    (tmp_path / "dest").mkdir()
    dest_root = add_root(db, str(tmp_path / "dest"))
    dst = Path(dest_root["path"]) / "2024" / "07" / "14" / "a.jpg"
    dst.parent.mkdir(parents=True)
    dst.write_bytes((src / "a.jpg").read_bytes())          # копия подменена, оригинал ещё не удалён
    ops.journal_begin(db, None, "move", row["id"], src / "a.jpg", dst, sha,
                      {"src_root_id": row["root_id"], "src_rel": "a.jpg", "dst_root_id": dest_root["id"],
                       "dst_rel": "2024/07/14/a.jpg", "new_status": "present"})
    stats = ops.recover_pending(db)
    assert stats["completed"] == 1 and dst.exists() and not (src / "a.jpg").exists()
    assert db.conn().execute("SELECT rel_path FROM files").fetchone()["rel_path"] == "2024/07/14/a.jpg"


def test_recovery_of_unstarted_operation_is_cancelled(db, settings, tmp_path):
    src, ids = prep(db, settings, tmp_path, [("a.jpg", datetime(2024, 7, 14, 10, 0, 0), 1)])
    ops.journal_begin(db, None, "move", ids[0], src / "a.jpg", tmp_path / "nowhere" / "a.jpg", "x", {})
    assert ops.recover_pending(db)["cancelled"] == 1 and (src / "a.jpg").exists()
