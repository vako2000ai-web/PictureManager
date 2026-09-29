import time

from app.errors import Cancelled


def test_progress_and_result(jobs):
    def work(ctx):
        ctx.set_total(bytes=100, files=2)
        ctx.add(bytes=60, files=1)
        ctx.add(bytes=40, files=1)
        return {"ok": True}

    job = jobs.wait(jobs.submit("t", work))
    assert job["status"] == "done"
    assert (job["done_bytes"], job["total_bytes"], job["done_files"]) == (100, 100, 2)
    assert job["result"]["ok"] is True


def test_cancel(jobs):
    def work(ctx):
        for _ in range(500):
            ctx.check()
            time.sleep(0.01)

    jid = jobs.submit("t", work)
    time.sleep(0.05)
    assert jobs.cancel(jid)
    assert jobs.wait(jid)["status"] == "cancelled"


def test_error_is_reported(jobs):
    def work(ctx):
        raise RuntimeError("boom")

    job = jobs.wait(jobs.submit("t", work))
    assert job["status"] == "error" and "boom" in job["error"]


def test_cancelled_exception_from_helper(jobs):
    def work(ctx):
        raise Cancelled()

    assert jobs.wait(jobs.submit("t", work))["status"] == "cancelled"
