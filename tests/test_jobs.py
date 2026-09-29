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


def test_running_job_exposes_live_progress_and_current_file(jobs):
    import threading
    started, release = threading.Event(), threading.Event()

    def work(ctx):
        ctx.add(bytes=10, files=1, total_bytes=10, total_files=1, current="C:/photos/a.jpg")
        started.set()
        release.wait(5)

    jid = jobs.submit("scan", work)
    assert started.wait(5)
    live = jobs.get(jid)          # ещё выполняется: счётчики и текущий файл видны сразу, без задержки записи в БД
    assert live["status"] == "running" and live["done_files"] == 1 and live["current"] == "C:/photos/a.jpg"
    release.set()
    assert jobs.wait(jid)["status"] == "done"
