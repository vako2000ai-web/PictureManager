import json
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import make_jpeg
from test_converter import make_heic


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "appdata")
    app.state.settings.ffprobe_path = None
    app.state.settings.ffmpeg_path = None
    with TestClient(app) as c:
        yield c


def wait_job(client, job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "cancelled", "error", "interrupted"):
            return j
        time.sleep(0.05)
    raise AssertionError("job timeout")


def library(tmp_path):
    base = tmp_path / "lib"
    make_jpeg(base / "a" / "one.jpg", datetime(2024, 7, 14, 10, 0, 0), text_seed=1)
    (base / "b").mkdir(parents=True)
    (base / "b" / "one_copy.jpg").write_bytes((base / "a" / "one.jpg").read_bytes())
    make_jpeg(base / "two.jpg", datetime(2024, 7, 15, 11, 0, 0), text_seed=2, size=(90, 60))
    make_heic(base / "phone.heic")
    return base


def test_static_ui_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and "PictureManager" in r.text
    assert client.get("/app.js").status_code == 200
    h = client.get("/api/health").json()
    assert h["ok"] and h["ffprobe"] is False


def test_full_flow(client, tmp_path):
    base = library(tmp_path)
    assert client.post("/api/roots", json={"path": str(tmp_path / "missing")}).status_code == 400
    root = client.post("/api/roots", json={"path": str(base)}).json()

    job = wait_job(client, client.post(f"/api/roots/{root['id']}/scan").json()["job_id"])
    assert job["status"] == "done" and job["result"]["added"] == 4

    days = client.get("/api/catalog/days?sort=asc").json()
    assert [d["day"] for d in days] == ["2023-08-09", "2024-07-14", "2024-07-15"] and days[1]["count"] == 2
    files = client.get("/api/catalog/files?day=2024-07-14").json()
    assert len(files) == 2

    thumb = client.get(f"/api/thumb/{files[0]['id']}")
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"

    # дубли
    job = wait_job(client, client.post("/api/duplicates/search").json()["job_id"])
    assert job["result"]["groups"] == 1
    groups = client.get("/api/duplicates").json()
    assert len(groups) == 1 and len(groups[0]["members"]) == 2
    gid = groups[0]["id"]
    assert client.post(f"/api/duplicates/{gid}/quarantine", json={}).status_code == 400   # без подтверждения
    res = client.post(f"/api/duplicates/{gid}/quarantine", json={"confirm": True}).json()
    assert res["quarantined"] == 1
    q = client.get("/api/quarantine").json()
    assert len(q) == 1
    assert client.post(f"/api/files/{q[0]['id']}/restore").status_code == 200

    # HEIC
    heic = client.get("/api/catalog/files?ext=.heic").json()
    assert len(heic) == 1
    job = wait_job(client, client.post("/api/convert/heic", json={"file_ids": [heic[0]["id"]]}).json()["job_id"])
    assert job["result"]["converted"] == 1 and (base / "phone.jpg").exists()

    # перемещение: план, отказ без подтверждения, выполнение, откат
    dest = tmp_path / "archive"
    plan = client.post("/api/plans", json={"dest_path": str(dest), "root_id": root["id"]}).json()
    assert plan["summary"]["files"] >= 4 and not list(dest.rglob("*.jpg"))
    assert client.post(f"/api/plans/{plan['id']}/execute", json={}).status_code == 400
    job = wait_job(client, client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": True}).json()["job_id"])
    assert job["status"] == "done" and not job["result"]["errors"], job
    assert (dest / "2024" / "07" / "15" / "two.jpg").exists()
    assert not (base / "two.jpg").exists()
    assert client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": True}).status_code == 409

    ops = client.get("/api/operations").json()
    assert any(o["kind"] == "move" and o["status"] == "done" for o in ops)

    job = wait_job(client, client.post(f"/api/plans/{plan['id']}/rollback", json={"confirm": True}).json()["job_id"])
    assert job["result"]["rolled_back"] >= 4 and (base / "two.jpg").exists()
    assert client.get(f"/api/plans/{plan['id']}").json()["status"] == "rolled_back"


def test_manual_date_via_api(client, tmp_path):
    base = library(tmp_path)
    root = client.post("/api/roots", json={"path": str(base)}).json()
    wait_job(client, client.post(f"/api/roots/{root['id']}/scan").json()["job_id"])
    f = client.get("/api/catalog/files?day=2024-07-15").json()[0]
    assert client.post("/api/catalog/date", json={"file_ids": [f["id"]], "date": "bad"}).status_code == 400
    client.post("/api/catalog/date", json={"file_ids": [f["id"]], "date": "2001-02-03T04:05:06"})
    assert client.get("/api/catalog/files?day=2001-02-03").json()[0]["id"] == f["id"]


def test_sse_progress_stream(client, tmp_path):
    base = library(tmp_path)
    root = client.post("/api/roots", json={"path": str(base)}).json()
    job_id = client.post(f"/api/roots/{root['id']}/scan").json()["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    assert events and events[-1]["status"] == "done" and events[-1]["done_files"] == 4


def test_thumbnail_placeholder_when_video_and_no_ffmpeg(client, tmp_path):
    base = tmp_path / "lib"
    base.mkdir()
    (base / "VID_20240102_030405.mp4").write_bytes(b"0" * 50)
    root = client.post("/api/roots", json={"path": str(base)}).json()
    wait_job(client, client.post(f"/api/roots/{root['id']}/scan").json()["job_id"])
    fid = client.get("/api/catalog/files").json()[0]["id"]
    r = client.get(f"/api/thumb/{fid}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"


def test_settings_roundtrip(client):
    s = client.put("/api/settings", json={"day_boundary_hour": 4, "jpeg_quality": 80}).json()
    assert s["day_boundary_hour"] == 4 and s["jpeg_quality"] == 80
    assert client.get("/api/settings").json()["jpeg_quality"] == 80
