import importlib
import sys
import time
from dataclasses import replace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.core import Commands, Monitor, Store, validate_stream
from app.detector import EventResult, IgnitionFSM


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    sys.modules.pop("app.main", None)
    module = importlib.import_module("app.main")
    with TestClient(module.app) as client:
        client.module = module
        yield client


def login(client):
    assert (
        client.post("/api/login", json={"token": "test-admin-token"}).status_code == 200
    )


def wait_until(predicate, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("Timed out")


def test_auth_origin_and_validation(client):
    assert client.get("/api/config").status_code == 401
    assert (
        client.post(
            "/api/start", json={"kind": "rtmp", "url": "rtmp://x/live"}
        ).status_code
        == 401
    )
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"token": "错误口令"}).status_code == 401
    login(client)
    assert client.put("/api/config", json={"roi": [0, 0, 1, 1]}).status_code == 422
    assert (
        client.post("/api/stop", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/start", json={"kind": "file", "file_id": "../secret"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/start", json={"kind": "rtmp", "url": "concat:/etc/passwd"}
        ).status_code
        == 422
    )
    assert client.get("/api/health").json()["ok"]
    assert client.get("/").headers["content-security-policy"]
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["status"]["phase"] == "idle"
    client.post("/api/logout")
    assert client.get("/api/status").status_code == 401


def test_unauthorized_websocket(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws"):
            pass


def test_commands_deduplicate_retract_and_persist(tmp_path):
    store = Store(tmp_path)
    commands = Commands(store)
    out = tmp_path / "sentinel"
    cfg = store.config | {
        "commands_enabled": True,
        "confirm_command": f'printf "confirm\\n" >> "{out}"',
        "retract_command": f'printf "retract\\n" >> "{out}"',
    }
    result = EventResult("CONFIRMED", 1, "confirmed", 3)
    commands.submit("job", result, cfg)
    commands.submit("job", result, cfg)
    wait_until(lambda: out.exists() and out.read_text() == "confirm\n")
    commands.submit("job", replace(result, action="retracted"), cfg)
    wait_until(lambda: out.read_text() == "confirm\nretract\n")
    commands.close()
    restarted = Commands(Store(tmp_path))
    restarted.submit("job", result, cfg)
    time.sleep(0.1)
    assert out.read_text() == "confirm\nretract\n"
    restarted.close()


def test_timeout_and_failure_visible(tmp_path):
    store = Store(tmp_path)
    commands = Commands(store)
    cfg = store.config | {
        "commands_enabled": True,
        "confirm_command": "sleep 5",
        "command_timeout": 1,
    }
    commands.submit("x", EventResult("CONFIRMED", 1, "confirmed", 1), cfg)
    wait_until(lambda: any(e.get("status") == "timeout" for e in store.events()), 3)
    cfg["confirm_command"] = "echo failed; exit 7"
    commands.submit("y", EventResult("CONFIRMED", 1, "confirmed", 1), cfg)
    wait_until(
        lambda: any(
            e.get("exit_code") == 7 and e.get("output", "").strip() == "failed"
            for e in store.events()
        )
    )
    commands.close()


def test_manual_retraction_exactly_once(tmp_path):
    m = Monitor(Store(tmp_path))
    m.fsm = IgnitionFSM(0.001, 0.01)
    m.job_config = m.store.config
    m.status["job_id"] = "test"
    for i in range(12):
        m.fsm.update(0.1, i / 10)
    assert m.fsm.confirmed
    assert m.retract()["event"] == 0
    with pytest.raises(ValueError):
        m.retract()
    assert [e["kind"] for e in m.store.events()] == ["retracted"]
    m.commands.close()


def make_video(path, seconds=3):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    for i in range(seconds * 10):
        frame = np.zeros((180, 320, 3), np.uint8) + 20
        if i >= 15:
            cv2.rectangle(frame, (90, 90), (200, 140), (0, 180, 255), -1)
        writer.write(frame)
    writer.release()


def test_real_decode_upload_eof_and_job_config_lock(client, tmp_path):
    login(client)
    path = tmp_path / "sample.mp4"
    make_video(path)
    with path.open("rb") as f:
        response = client.post(
            "/api/upload", files={"file": ("../../bad.mp4", f, "video/mp4")}
        )
    assert response.status_code == 200
    file_id = response.json()["file_id"]
    assert (client.module.UPLOADS / (file_id + ".media")).exists()
    assert (
        client.post("/api/start", json={"kind": "file", "file_id": file_id}).status_code
        == 200
    )
    wait_until(lambda: client.get("/api/status").json()["frames"] >= 3)
    assert client.put("/api/config", json={}).status_code == 409
    wait_until(lambda: client.get("/api/status").json()["phase"] == "ended", 7)
    s = client.get("/api/status").json()
    assert s["frames"] == 30
    assert client.module.monitor.jpeg.startswith(b"\xff\xd8")
    assert sum(e["kind"] == "confirmed" for e in client.get("/api/events").json()) == 1
    assert client.post("/api/retract").status_code == 200
    assert client.post("/api/retract").status_code == 409
    assert not (tmp_path.parent / "bad.mp4").exists()


def test_bad_upload_cleanup(client):
    login(client)
    assert (
        client.post(
            "/api/upload", files={"file": ("x.mp4", b"not video", "video/mp4")}
        ).status_code
        == 422
    )
    assert not list(client.module.UPLOADS.iterdir())


def test_stop_and_restart(tmp_path):
    path = tmp_path / "video.mp4"
    make_video(path, 5)
    m = Monitor(Store(tmp_path / "data"))
    a = m.start(str(path), "test")
    wait_until(lambda: m.snapshot()["frames"] >= 2)
    m.stop()
    assert not m.thread.is_alive()
    b = m.start(str(path), "test2")
    assert a["job_id"] != b["job_id"]
    wait_until(lambda: m.snapshot()["frames"] >= 2)
    m.stop()
    m.commands.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "concat:x|y",
        "https://example.com/a",
        "rtmp:///bad",
        "rtmp://x/space here",
    ],
)
def test_input_protocols(url):
    with pytest.raises(ValueError):
        validate_stream(url)


def test_video_automatic_correction_dispatches_compensation(tmp_path):
    path = tmp_path / "false-flame.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    for i in range(60):
        frame = np.zeros((180, 320, 3), np.uint8) + 20
        if 15 <= i < 33:
            cv2.rectangle(frame, (90, 90), (200, 140), (0, 180, 255), -1)
        writer.write(frame)
    writer.release()
    store = Store(tmp_path / "data")
    sentinel = tmp_path / "actions"
    store.save(
        store.config
        | {
            "commands_enabled": True,
            "confirm_command": f'printf C >> "{sentinel}"',
            "retract_command": f'printf R >> "{sentinel}"',
        }
    )
    monitor = Monitor(store)
    try:
        monitor.start(str(path), "synthetic correction")
        wait_until(lambda: monitor.snapshot()["phase"] == "ended", 10)
        wait_until(
            lambda: len([e for e in store.events() if e["kind"] == "command"]) == 2
        )
        events = store.events()
        assert sum(e["kind"] == "confirmed" for e in events) == 1
        assert sum(e["kind"] == "retracted" for e in events) == 1
        assert sentinel.read_text() == "CR"
        assert monitor.snapshot()["event"] == 0
    finally:
        monitor.stop()
        monitor.commands.close()
