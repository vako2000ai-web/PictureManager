import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_jpeg, write


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as c:
        yield c


def wait_job(client, job_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise TimeoutError


def test_full_scenario(client, tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    dest.mkdir()
    make_jpeg(src / "a.jpg", "2024:07:15 10:15:00")
    write(str(src / "copy" / "a_copy.jpg"), (src / "a.jpg").read_bytes())
    r = client.post("/api/roots", json={"path": str(src)})
    assert r.status_code == 201
    root_id = r.json()["id"]
    assert client.post("/api/roots", json={"path": str(tmp_path / "missing")}).status_code == 400
    droot = client.post("/api/roots", json={"path": str(dest)}).json()["id"]

    job = wait_job(client, client.post(f"/api/roots/{root_id}/scan").json()["job_id"])
    assert job["status"] == "done" and job["result"]["new"] == 2

    feed = client.get("/api/catalog", params={"root_id": root_id}).json()
    assert feed["total"] == 2 and feed["days"]

    wait_job(client, client.post("/api/dupes/scan").json()["job_id"])
    groups = client.get("/api/dupes").json()
    assert len(groups) == 1
    gid = groups[0]["id"]
    assert client.post(f"/api/dupes/{gid}/quarantine", json={}).status_code == 400  # нужно подтверждение
    assert client.post(f"/api/dupes/{gid}/quarantine", json={"confirm": True}).json()["quarantined"] == 1

    plan = client.post("/api/plans", json={"dest_root_id": droot, "source_root_id": root_id}).json()
    assert plan["summary"]["files"] == 1
    assert client.post(f"/api/plans/{plan['id']}/execute", json={}).status_code == 400
    assert not (dest / "2024").exists()
    job = wait_job(client, client.post(f"/api/plans/{plan['id']}/execute", json={"confirm": True}).json()["job_id"])
    assert job["result"]["moved"] == 1 and (dest / "2024" / "07" / "15" / "a.jpg").exists()
    assert client.get("/api/operations").json()
    job = wait_job(client, client.post(f"/api/plans/{plan['id']}/rollback", json={"confirm": True}).json()["job_id"])
    assert job["result"]["restored"] == 1 and (src / "a.jpg").exists()


def test_sse_and_thumbnail_placeholder(client, tmp_path):
    src = tmp_path / "src"
    make_jpeg(src / "a.jpg")
    write(str(src / "v.mp4"), b"not a video")
    rid = client.post("/api/roots", json={"path": str(src)}).json()["id"]
    jid = client.post(f"/api/roots/{rid}/scan").json()["job_id"]
    body = client.get(f"/api/jobs/{jid}/events").text
    assert body.startswith("data: ") and '"status": "done"' in body.strip().split("\n\n")[-1]
    wait_job(client, jid)
    files = client.get("/api/catalog").json()["days"]
    ids = {f["rel_path"]: f["id"] for d in files for f in d["files"]}
    r = client.get(f"/api/files/{ids['a.jpg']}/thumb")
    assert r.headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/files/{ids['a.jpg']}/thumb").status_code == 200  # из кэша
    r = client.get(f"/api/files/{ids['v.mp4']}/thumb")
    assert r.headers["content-type"] == "image/svg+xml"  # заглушка


def test_settings_status_and_static(client):
    assert client.get("/").status_code == 200
    assert client.put("/api/settings", json={"jpeg_quality": 80}).json()["jpeg_quality"] == 80
    assert client.put("/api/settings", json={"jpeg_quality": 500}).status_code == 400
    assert "warnings" in client.get("/api/status").json()
    assert client.get("/api/fs/list").status_code == 200


def test_server_binds_localhost_only():
    import app.__main__ as m

    assert m.HOST == "127.0.0.1"


def test_view_and_delete(client, tmp_path):
    from tests.test_heic import make_heic

    src = tmp_path / "src"
    make_jpeg(src / "a.jpg")
    make_jpeg(src / "b.jpg", color=(1, 2, 3))
    make_heic(src / "c.HEIC")
    rid = client.post("/api/roots", json={"path": str(src)}).json()["id"]
    wait_job(client, client.post(f"/api/roots/{rid}/scan").json()["job_id"])
    ids = {f["rel_path"]: f["id"] for d in client.get("/api/catalog").json()["days"] for f in d["files"]}

    r = client.get(f"/api/files/{ids['a.jpg']}/view")
    assert r.status_code == 200 and r.content == (src / "a.jpg").read_bytes()
    r = client.get(f"/api/files/{ids['c.HEIC']}/view")  # HEIC отдаётся как JPEG-превью
    assert r.headers["content-type"] == "image/jpeg"

    assert client.post("/api/files/delete", json={"file_ids": [ids["a.jpg"]]}).status_code == 400
    r = client.post("/api/files/delete", json={"file_ids": [ids["a.jpg"]], "confirm": True}).json()
    assert r["deleted"] == 1 and not (src / "a.jpg").exists() and (src / "_duplicates" / "a.jpg").exists()
    assert client.post("/api/quarantine/restore", json={"file_ids": [ids["a.jpg"]]}).json()[0]["restored"]
    assert (src / "a.jpg").exists()

    r = client.post("/api/files/delete", json={"file_ids": [ids["b.jpg"]], "permanent": True, "confirm": True}).json()
    assert r["deleted"] == 1 and not (src / "b.jpg").exists()
    left = [f["rel_path"] for d in client.get("/api/catalog").json()["days"] for f in d["files"]]
    assert "b.jpg" not in left
