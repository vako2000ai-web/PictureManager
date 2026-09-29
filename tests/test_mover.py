import os
import shutil

import pytest

from app import catalog, fileops, journal, mover
from tests.conftest import FakeCtx, make_jpeg, scan, write


def setup_two_roots(env, tmp_path):
    cfg, db = env
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    dest.mkdir()
    make_jpeg(src / "in" / "a.jpg", "2024:07:15 10:15:00")
    make_jpeg(src / "b.jpg", "2023:01:02 09:00:00", color=(1, 2, 3))
    make_jpeg(src / "nodate.jpg", color=(9, 9, 9))
    root, _ = scan(env, src)
    droot = catalog.add_root(db, str(dest))
    # «без даты»: убираем mtime-дату
    db.execute("UPDATE files SET taken_at=NULL, date_source=NULL WHERE rel_path='nodate.jpg'")
    ids = [r["id"] for r in db.query("SELECT id FROM files")]
    return root, droot, ids, src, dest


def run(env, plan_id):
    cfg, db = env
    ctx = FakeCtx()
    return mover.execute_plan(db, cfg, plan_id, ctx), ctx


def test_plan_preview_does_not_move(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    plan = mover.get_plan(db, mover.build_plan(db, cfg, ids, droot["id"]))
    dsts = {i["src_rel"]: i["dst_rel"] for i in plan["items"]}
    assert dsts["in/a.jpg"] == "2024/07/15/a.jpg"
    assert dsts["b.jpg"] == "2023/01/02/b.jpg"
    assert dsts["nodate.jpg"] == "_без даты/nodate.jpg"
    s = plan["summary"]
    assert s["files"] == 3 and s["no_date"] == 1 and s["conflicts"] == 0 and s["enough_space"]
    assert os.path.exists(src / "in" / "a.jpg") and not any(dest.iterdir())  # без подтверждения ничего не двигается


def test_execute_moves_and_rollback(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    report, ctx = run(env, pid)
    assert report["moved"] == 3 and not report["failed"]
    assert (dest / "2024" / "07" / "15" / "a.jpg").exists() and not (src / "in" / "a.jpg").exists()
    row = db.one("SELECT * FROM files WHERE rel_path='2024/07/15/a.jpg'")
    assert row["root_id"] == droot["id"] and row["status"] == "present"
    assert db.one("SELECT COUNT(*) n FROM operations WHERE plan_id=? AND status='done'", (pid,))["n"] == 3
    assert mover.get_plan(db, pid)["status"] == "done"

    back = mover.rollback_plan(db, pid, FakeCtx())
    assert back["restored"] == 3 and not back["skipped"]
    assert (src / "in" / "a.jpg").exists() and not (dest / "2024" / "07" / "15" / "a.jpg").exists()
    assert db.one("SELECT root_id FROM files WHERE rel_path='in/a.jpg'")["root_id"] == root["id"]


def test_rollback_skips_modified_file(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    run(env, pid)
    with open(dest / "2023" / "01" / "02" / "b.jpg", "ab") as f:
        f.write(b"edited")
    back = mover.rollback_plan(db, pid, FakeCtx())
    assert back["restored"] == 2 and len(back["skipped"]) == 1
    assert (dest / "2023" / "01" / "02" / "b.jpg").exists()


def test_conflict_different_content_gets_suffix(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    make_jpeg(dest / "2024" / "07" / "15" / "a.jpg", color=(0, 0, 255))
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    assert mover.get_plan(db, pid)["summary"]["conflicts"] == 1
    report, _ = run(env, pid)
    assert (dest / "2024" / "07" / "15" / "a_1.jpg").exists()
    assert report["moved"] == 3


def test_conflict_same_content_goes_to_quarantine(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    shutil.copy(src / "in" / "a.jpg", dest / "a_existing.jpg")
    os.makedirs(dest / "2024" / "07" / "15")
    shutil.copy(src / "in" / "a.jpg", dest / "2024" / "07" / "15" / "a.jpg")
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    report, _ = run(env, pid)
    assert report["quarantined_duplicates"] == 1
    assert (dest / "_duplicates" / "a.jpg").exists()
    assert (dest / "2024" / "07" / "15" / "a.jpg").exists()  # существующий не перезаписан
    assert db.one("SELECT status FROM files WHERE rel_path='_duplicates/a.jpg'")["status"] == "quarantined"


def test_linked_files_move_together(env, tmp_path):
    cfg, db = env
    src, dest = tmp_path / "src", tmp_path / "dest"
    dest.mkdir()
    make_jpeg(src / "IMG_1.HEIC", "2024:07:15 10:00:00")
    write(str(src / "IMG_1.MOV"), b"movie")
    scan(env, src)
    droot = catalog.add_root(db, str(dest))
    heic_id = db.one("SELECT id FROM files WHERE ext='heic'")["id"]
    pid = mover.build_plan(db, cfg, [heic_id], droot["id"])  # выбран только HEIC
    run(env, pid)
    assert (dest / "2024" / "07" / "15" / "IMG_1.MOV").exists()
    assert (dest / "2024" / "07" / "15" / "IMG_1.HEIC").exists()


def test_external_change_is_skipped(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    with open(src / "b.jpg", "ab") as f:
        f.write(b"changed after plan")
    report, _ = run(env, pid)
    assert len(report["skipped"]) == 1 and report["skipped"][0]["path"] == "b.jpg"
    assert (src / "b.jpg").exists() and report["moved"] == 2


def test_cancel_leaves_journal_consistent(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    ctx = FakeCtx()
    ctx._cancel.set()
    report = mover.execute_plan(db, cfg, pid, ctx)
    assert report["cancelled"] and report["moved"] == 0
    assert db.one("SELECT COUNT(*) n FROM operations WHERE plan_id=? AND status='cancelled'", (pid,))["n"] == 3
    assert db.one("SELECT COUNT(*) n FROM operations WHERE status='pending'")["n"] == 0


def test_day_boundary_and_template(env, tmp_path):
    cfg, db = env
    src, dest = tmp_path / "src", tmp_path / "dest"
    dest.mkdir()
    make_jpeg(src / "night.jpg", "2024:07:15 01:30:00")
    scan(env, src)
    droot = catalog.add_root(db, str(dest))
    cfg.update({"day_boundary_hour": 4, "path_template": "{YYYY}/{YYYY}-{MM}-{DD}"})
    pid = mover.build_plan(db, cfg, [db.one("SELECT id FROM files")["id"]], droot["id"])
    assert mover.get_plan(db, pid)["items"][0]["dst_rel"] == "2024/2024-07-14/night.jpg"


def test_cross_volume_hash_mismatch_keeps_original(env, tmp_path, monkeypatch):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    monkeypatch.setattr(fileops, "same_volume", lambda a, b: False)
    real = fileops.sha256_file
    monkeypatch.setattr(fileops, "sha256_file", lambda p, *a, **k: "0" * 64 if p.endswith(".part") else real(p, *a, **k))
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    report, _ = run(env, pid)
    assert report["moved"] == 0 and len(report["failed"]) == 3
    assert (src / "in" / "a.jpg").exists()
    assert not list(dest.rglob("*.jpg")) and not list(dest.rglob("*.part"))
    assert db.one("SELECT COUNT(*) n FROM operations WHERE status='failed'")["n"] == 3
    assert db.one("SELECT root_id FROM files WHERE rel_path='in/a.jpg'")["root_id"] == root["id"]


def test_cross_volume_success_verifies_and_removes_original(env, tmp_path, monkeypatch):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    monkeypatch.setattr(fileops, "same_volume", lambda a, b: False)
    pid = mover.build_plan(db, cfg, ids, droot["id"])
    report, ctx = run(env, pid)
    assert report["moved"] == 3 and not (src / "in" / "a.jpg").exists()
    assert ctx.bytes > 0


def _pending_op(db, root, droot, src_rel, dst_rel):
    f = db.one("SELECT * FROM files WHERE rel_path=?", (src_rel,))
    return journal.record(db, "move", f["id"], root["id"], src_rel, droot["id"], dst_rel, size=f["size"],
                          mtime_ns=f["mtime_ns"])


def test_recovery_crash_before_move(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    op = _pending_op(db, root, droot, "b.jpg", "2023/01/02/b.jpg")
    assert journal.recover(db)["aborted"] == 1
    assert (src / "b.jpg").exists()
    assert db.one("SELECT status FROM operations WHERE id=?", (op,))["status"] == "aborted"


def test_recovery_crash_after_move(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    op = _pending_op(db, root, droot, "b.jpg", "2023/01/02/b.jpg")
    os.makedirs(dest / "2023" / "01" / "02")
    os.replace(src / "b.jpg", dest / "2023" / "01" / "02" / "b.jpg")  # сбой после перемещения
    assert journal.recover(db)["completed"] == 1
    row = db.one("SELECT * FROM files WHERE rel_path='2023/01/02/b.jpg'")
    assert row["root_id"] == droot["id"]


def test_recovery_crash_midway_between_volumes(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    op = _pending_op(db, root, droot, "b.jpg", "2023/01/02/b.jpg")
    os.makedirs(dest / "2023" / "01" / "02")
    shutil.copy(src / "b.jpg", dest / "2023" / "01" / "02" / "b.jpg")  # копия готова, оригинал на месте
    assert journal.recover(db)["completed"] == 1
    assert not (src / "b.jpg").exists() and (dest / "2023" / "01" / "02" / "b.jpg").exists()


def test_recovery_partial_copy_is_discarded(env, tmp_path):
    cfg, db = env
    root, droot, ids, src, dest = setup_two_roots(env, tmp_path)
    op = _pending_op(db, root, droot, "b.jpg", "2023/01/02/b.jpg")
    os.makedirs(dest / "2023" / "01" / "02")
    write(str(dest / "2023" / "01" / "02" / "b.jpg"), b"truncated")
    db.execute("UPDATE operations SET sha256=? WHERE id=?", (fileops.sha256_file(str(src / "b.jpg")), op))
    assert journal.recover(db)["aborted"] == 1
    assert (src / "b.jpg").exists() and not (dest / "2023" / "01" / "02" / "b.jpg").exists()
