"""Real localhost RTMP, 60 FPS input -> 10 FPS detector. Requires running MediaMTX."""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import Monitor, Store

url = sys.argv[1] if len(sys.argv) > 1 else "rtmp://127.0.0.1:19355/rocket"
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    store = Store(root)
    cfg = store.config | {
        "commands_enabled": True,
        "confirm_command": f"printf confirmed >> {root}/confirmed",
        "retract_command": f"printf retracted >> {root}/retracted",
    }
    store.save(cfg)
    # A synthetic orange rectangle is evidence for plumbing validation, not accuracy data.
    publisher = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x141414:s=640x360:r=60:d=20",
            "-vf",
            "drawbox=x=310:y=300:w=24:h=20:color=orange:t=fill:enable='gte(t,12)'",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-g",
            "60",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "flv",
            url,
        ],
        stderr=subprocess.PIPE,
    )
    monitor = Monitor(store)
    try:
        time.sleep(0.6)
        monitor.start(url, "RTMP 60 FPS smoke", True)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            s = monitor.snapshot()
            if s["event"] == 1:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(monitor.snapshot())
        assert monitor.jpeg and monitor.jpeg.startswith(b"\xff\xd8")
        monitor.retract()
        deadline = time.monotonic() + 3
        while not (root / "retracted").exists() and time.monotonic() < deadline:
            time.sleep(0.03)
        assert (root / "confirmed").read_text() == "confirmed"
        assert (root / "retracted").read_text() == "retracted"
        publisher.terminate()
        publisher.wait(timeout=5)
        deadline = time.monotonic() + 8
        while (
            monitor.snapshot()["phase"] != "reconnecting"
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert monitor.snapshot()["phase"] == "reconnecting", monitor.snapshot()
        print(
            json.dumps(
                {
                    "ok": True,
                    "input_fps": 60,
                    "detection_fps": 10,
                    "at_confirmation": s,
                    "commands": [e for e in store.events() if e["kind"] == "command"],
                    "disconnect_phase": monitor.snapshot()["phase"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        monitor.stop()
        monitor.commands.close()
        if publisher.poll() is None:
            publisher.kill()
            publisher.wait()
