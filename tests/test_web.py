"""网页 API 端到端测试（使用 Flask test client 与后台任务队列）。"""

from __future__ import annotations

import io
import json
import time

from musickey.tags import read_metadata
from musickey.web import WORK_DIR_NAME, JobManager, create_app, resolve_startup_output

from builders import (
    PLAIN,
    build_kgm,
    build_kwm,
    build_ncm,
    build_qmc_v1,
    build_qmc_v2,
    load_kgm_pub_key,
    make_ekey_v1,
)


def _manager(tmp_path, workers: int = 1) -> JobManager:
    return JobManager(
        workers=workers,
        output_dir=tmp_path / "out",
        config_path=tmp_path / "web.json",
    )


def _wait(client, job_id: str, timeout: float = 12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        job = response.get_json()["job"]
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.12)
    raise TimeoutError("任务超时")


def test_web_roundtrip(tmp_path):
    output = tmp_path / "out"
    manager = _manager(tmp_path, workers=2)
    app = create_app(manager)
    client = app.test_client()

    health = client.get("/api/health")
    assert health.status_code == 200
    assert ".kgm.flac" in health.get_json()["formats"]

    cases = [
        ("ncm-song.ncm", build_ncm(PLAIN, b"0123456789abcdef")),
        ("tkm-song.tkm", build_qmc_v1(PLAIN)),
        ("mgg-song.mgg", build_qmc_v2(PLAIN, bytes(range(24)), make_ekey_v1(bytes(range(24))))),
        ("kgm-song.kgm", build_kgm(PLAIN, load_kgm_pub_key())),
        ("kwm-song.kwm", build_kwm(PLAIN)),
    ]
    saved = []
    for name, data in cases:
        response = client.post(
            "/api/jobs",
            data={"file": (io.BytesIO(data), name), "format": "", "embed_cover": "0"},
            content_type="multipart/form-data",
        )
        assert response.status_code == 202, response.get_data(as_text=True)
        job_id = response.get_json()["id"]
        job = _wait(client, job_id)
        assert job["status"] == "done", job
        download = client.get(f"/api/download/{job_id}")
        assert download.data == PLAIN
        landed = output / job["saved_name"]
        assert landed.is_file()
        assert landed.read_bytes() == PLAIN
        assert not (output / WORK_DIR_NAME / job_id).exists()
        saved.append(job["saved_name"])

    assert saved[0] == "ncm-song.mp3"
    assert len(set(saved)) == len(cases)
    assert sorted(path.name for path in output.glob("*.mp3")) == sorted(saved)
    assert client.get("/api/zip").status_code == 404


def test_web_bad_file_rejected(tmp_path):
    output = tmp_path / "out"
    manager = _manager(tmp_path)
    app = create_app(manager)
    client = app.test_client()
    response = client.post(
        "/api/jobs",
        data={"file": (io.BytesIO(b"NOT A REAL FILE"), "bad.ncm")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    job_id = response.get_json()["id"]
    job = _wait(client, job_id)
    assert job["status"] == "failed"
    assert not (output / WORK_DIR_NAME / job_id).exists()
    assert list(output.glob("bad.*")) == []


def test_web_metadata_preserved(tmp_path):
    output = tmp_path / "out"
    manager = _manager(tmp_path)
    app = create_app(manager)
    client = app.test_client()
    meta = {"format": "mp3", "musicName": "网页测试", "artist": [["歌手A", 1]], "album": "专辑A"}
    response = client.post(
        "/api/jobs",
        data={"file": (io.BytesIO(build_ncm(PLAIN, b"0123456789abcdef", meta)), "meta.ncm"), "embed_cover": "1"},
        content_type="multipart/form-data",
    )
    job = _wait(client, response.get_json()["id"])
    assert job["status"] == "done"
    assert job["tags"]["title"] == "网页测试"
    assert job["saved_name"] == "meta.mp3"
    landed = output / "meta.mp3"
    assert landed.is_file()
    download = client.get(f"/api/download/{response.get_json()['id']}")
    assert download.data == landed.read_bytes()
    tags, _ = read_metadata(landed, "mp3")
    assert tags["title"] == "网页测试"
    assert tags["artist"] == "歌手A"

    client.post("/api/clear")
    assert landed.is_file()


def test_repeat_publish_overwrites(tmp_path):
    output = tmp_path / "out"
    manager = _manager(tmp_path)
    client = create_app(manager).test_client()
    payload = build_ncm(PLAIN, b"0123456789abcdef")
    for _ in range(2):
        response = client.post(
            "/api/jobs",
            data={"file": (io.BytesIO(payload), "再次.ncm"), "embed_cover": "0"},
            content_type="multipart/form-data",
        )
        job = _wait(client, response.get_json()["id"])
        assert job["status"] == "done", job
        assert job["saved_name"] == "再次.mp3"
    assert [path.name for path in output.glob("再次*")] == ["再次.mp3"]


def test_lyric_sidecar_is_copied(tmp_path):
    output = tmp_path / "out"
    manager = _manager(tmp_path)
    client = create_app(manager).test_client()
    body = "[00:01.00]测试歌词\n".encode("utf-8")
    response = client.post(
        "/api/jobs",
        data={"file": (io.BytesIO(body), "心太软 - 任贤齐.lrc")},
        content_type="multipart/form-data",
    )
    job = _wait(client, response.get_json()["id"])
    assert job["status"] == "done", job
    assert job["saved_name"] == "心太软 - 任贤齐.lrc"
    assert (output / "心太软 - 任贤齐.lrc").read_bytes() == body


def test_lyric_copied_from_library(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    lyric = "[00:01.00]旁边的歌词\n".encode("utf-8")
    (library / "心太软 - 任贤齐.lrc").write_bytes(lyric)
    output = tmp_path / "out"
    manager = JobManager(
        workers=1,
        output_dir=output,
        config_path=tmp_path / "web.json",
        lyric_dir=library,
    )
    client = create_app(manager).test_client()
    response = client.post(
        "/api/jobs",
        data={"file": (io.BytesIO(build_ncm(PLAIN, b"0123456789abcdef")), "心太软 - 任贤齐.ncm"), "embed_cover": "0"},
        content_type="multipart/form-data",
    )
    job = _wait(client, response.get_json()["id"])
    assert job["status"] == "done", job
    assert (output / "心太软 - 任贤齐.lrc").read_bytes() == lyric
    assert (output / "心太软 - 任贤齐.mp3").is_file()


def test_settings_roundtrip(tmp_path):
    manager = _manager(tmp_path)
    client = create_app(manager).test_client()
    dest = tmp_path / "elsewhere"
    response = client.put("/api/settings", json={"output_dir": str(dest)})
    assert response.status_code == 200
    assert response.get_json()["output_dir"] == str(dest.resolve())
    stored = json.loads((tmp_path / "web.json").read_text(encoding="utf-8"))
    assert stored["output_dir"] == str(dest.resolve())

    current = client.get("/api/settings")
    assert current.get_json()["output_dir"] == str(dest.resolve())

    blocked = tmp_path / "not-a-dir.txt"
    blocked.write_text("x", encoding="utf-8")
    rejected = client.put("/api/settings", json={"output_dir": str(blocked)})
    assert rejected.status_code == 400
    assert client.get("/api/settings").get_json()["output_dir"] == str(dest.resolve())


def test_startup_output_resolution(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "web.json"
    saved = tmp_path / "saved"
    config.write_text(json.dumps({"output_dir": str(saved)}), encoding="utf-8")

    override = tmp_path / "from-cli"
    assert resolve_startup_output(override, config_path=config) == override.resolve()
    assert resolve_startup_output(config_path=config) == saved.resolve()
    assert resolve_startup_output(config_path=tmp_path / "missing.json") == (tmp_path / "unlocked").resolve()
