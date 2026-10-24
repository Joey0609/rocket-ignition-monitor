"""Exercise the real Uvicorn HTTP, cookie auth, WebSocket and MJPEG paths."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import httpx
import numpy as np
from websockets.sync.client import connect

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ | {
        "DATA_DIR": str(root / "data"),
        "ADMIN_TOKEN": "temporary-smoke-token",
    }
    with (root / "server.log").open("w") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        if client.get("/api/health").is_success:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("server startup failed")
                assert client.get("/api/config").status_code == 401
                assert client.post(
                    "/api/login", json={"token": "temporary-smoke-token"}
                ).is_success
                for path in ["/", "/static/app.js", "/static/style.css"]:
                    assert client.get(path).status_code == 200
                writer = cv2.VideoWriter(
                    str(root / "sample.mp4"),
                    cv2.VideoWriter_fourcc(*"mp4v"),
                    10,
                    (320, 180),
                )
                for i in range(60):
                    image = np.zeros((180, 320, 3), np.uint8) + 20
                    if i >= 20:
                        cv2.rectangle(image, (90, 90), (200, 140), (0, 180, 255), -1)
                    writer.write(image)
                writer.release()
                cfg = client.get("/api/config").json() | {
                    "commands_enabled": True,
                    "confirm_command": f"printf confirmed > {root}/confirm",
                    "retract_command": f"printf retracted > {root}/retract",
                }
                assert client.put("/api/config", json=cfg).is_success
                with (root / "sample.mp4").open("rb") as f:
                    upload = client.post(
                        "/api/upload", files={"file": ("sample.mp4", f, "video/mp4")}
                    )
                assert upload.status_code == 200, upload.text
                assert client.post(
                    "/api/start",
                    json={"kind": "file", "file_id": upload.json()["file_id"]},
                ).is_success
                cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
                with connect(
                    f"ws://127.0.0.1:{port}/ws",
                    additional_headers={"Cookie": cookie},
                    origin=base,
                    proxy=None,
                ) as ws:
                    payload = json.loads(ws.recv(timeout=5))
                    assert "status" in payload
                with client.stream("GET", "/preview.mjpg") as response:
                    assert response.status_code == 200
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if b"\xff\xd8" in chunks:
                            break
                    assert b"Content-Type: image/jpeg" in chunks
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = client.get("/api/status").json()
                    if state["event"] == 1:
                        break
                    time.sleep(0.05)
                else:
                    raise AssertionError(state)
                assert client.post("/api/retract").is_success
                assert client.post("/api/retract").status_code == 409
                deadline = time.monotonic() + 3
                while not (root / "retract").exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
                assert (root / "confirm").read_text() == "confirmed"
                assert (root / "retract").read_text() == "retracted"
                client.post("/api/stop")
                events = client.get("/api/events").json()
                assert sum(e["kind"] == "confirmed" for e in events) == 1
                assert sum(e["kind"] == "retracted" for e in events) == 1
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "http": True,
                            "cookie_auth": True,
                            "websocket": True,
                            "mjpeg_jpeg": True,
                            "upload_decode": True,
                            "confirmed_events": 1,
                            "retracted_events": 1,
                            "commands_executed_once": True,
                        },
                        indent=2,
                    )
                )
        finally:
            server.terminate()
            server.wait(timeout=10)
