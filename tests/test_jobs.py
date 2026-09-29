import threading

from app.jobs import JobManager


def test_progress_and_result(env, jobs):
    def work(ctx):
        ctx.set_total(files=2, bytes=100)
        ctx.advance(files=1, bytes=40)
        ctx.advance(files=1, bytes=60)
        return {"ok": True}

    job = jobs.wait(jobs.submit("t", work))
    assert job["status"] == "done"
    assert (job["files_done"], job["bytes_done"], job["bytes_total"]) == (2, 100, 100)
    assert job["result"] == {"ok": True}
    stored = env[1].one("SELECT * FROM jobs WHERE id=?", (job["id"],))
    assert stored["status"] == "done" and stored["bytes_done"] == 100


def test_cancel(env, jobs):
    started = threading.Event()

    def work(ctx):
        started.set()
        while not ctx.cancelled:
            pass
        return {"stopped": True}

    jid = jobs.submit("t", work)
    assert started.wait(5)
    assert jobs.cancel(jid)
    assert jobs.wait(jid)["status"] == "cancelled"
    assert not jobs.cancel(jid)


def test_failure_is_reported(env, jobs):
    def work(ctx):
        raise RuntimeError("boom")

    job = jobs.wait(jobs.submit("t", work))
    assert job["status"] == "failed" and "boom" in job["error"]


def test_interrupted_on_restart(env):
    db = env[1]
    db.execute("INSERT INTO jobs(kind,status,created) VALUES('x','running','now')")
    JobManager(db)
    assert db.one("SELECT status FROM jobs")["status"] == "interrupted"
