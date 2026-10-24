"""Bounded, persisted event dispatch and one-decoder monitoring engine."""

import copy
import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np

from .camera_guard import CameraGuard
from .detector import ImageEvidence, IgnitionFSM

DEFAULTS = {
    "roi": [0.03, 0.10, 0.80, 0.83],
    "candidate_threshold": 0.0005,
    "strong_threshold": 0.01,
    "min_warm_score": 0.0003,
    "confirm_command": "",
    "retract_command": "",
    "commands_enabled": False,
    "command_timeout": 10,
}


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "events.sqlite3", check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, data TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS dispatch (key TEXT PRIMARY KEY, status TEXT, data TEXT)"
        )
        self.db.commit()
        p = self.root / "config.json"
        self.config = DEFAULTS | (json.loads(p.read_text()) if p.exists() else {})

    def save(self, config):
        with self.lock:
            tmp = self.root / "config.tmp"
            tmp.write_text(json.dumps(config, indent=2))
            tmp.chmod(0o600)
            tmp.replace(self.root / "config.json")
            self.config = copy.deepcopy(config)

    def event(self, kind, **data):
        with self.lock:
            now = time.time()
            cur = self.db.execute(
                "INSERT INTO events(ts,kind,data) VALUES(?,?,?)",
                (now, kind, json.dumps(data)),
            )
            self.db.commit()
            return {"id": cur.lastrowid, "ts": now, "kind": kind, **data}

    def events(self, limit=120):
        with self.lock:
            rows = self.db.execute(
                "SELECT id,ts,kind,data FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                {"id": i, "ts": ts, "kind": k, **json.loads(d)} for i, ts, k, d in rows
            ]


class Commands:
    def __init__(self, store):
        self.store = store
        self.slot = threading.BoundedSemaphore(4)
        self.lock = threading.Lock()
        self.active = set()
        self.closed = False

    def submit(self, job, result, config):
        action = result.action
        if action not in ("confirmed", "retracted") or not config["commands_enabled"]:
            return
        command = config[
            "confirm_command" if action == "confirmed" else "retract_command"
        ]
        if not command.strip():
            return
        key = f"{job}:{result.decision_id}:{action}"
        with self.store.lock:
            try:
                self.store.db.execute(
                    "INSERT INTO dispatch VALUES(?,?,?)", (key, "claimed", "{}")
                )
                self.store.db.commit()
            except sqlite3.IntegrityError:
                return
        if self.closed or not self.slot.acquire(blocking=False):
            self._finish(key, "rejected", error="command queue unavailable")
            return
        threading.Thread(
            target=self._run,
            args=(key, job, result, command, config["command_timeout"]),
            daemon=True,
        ).start()

    def _finish(self, key, status, **data):
        with self.store.lock:
            self.store.db.execute(
                "UPDATE dispatch SET status=?,data=? WHERE key=?",
                (status, json.dumps(data), key),
            )
            self.store.db.commit()
        self.store.event("command", key=key, status=status, **data)

    def _run(self, key, job, result, command, timeout):
        proc = None
        try:
            env = os.environ.copy()
            env.update(
                IGNITION_JOB_ID=job,
                IGNITION_ACTION=result.action,
                IGNITION_DECISION_ID=str(result.decision_id),
            )
            # Admin-authored shell script is intentional. Event fields are environment only.
            chunks = deque(maxlen=8)

            def drain():
                while block := proc.stdout.read(1024):
                    chunks.append(block)

            with self.lock:
                if self.closed:
                    raise RuntimeError("server shutting down")
                proc = subprocess.Popen(
                    ["/bin/sh", "-c", command],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=True,
                )
                self.active.add(proc)
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill(proc)
                proc.wait()
            self.kill(proc)
            reader.join(timeout=2)
            proc.stdout.close()
            output = b"".join(chunks)[-8192:].decode(errors="replace")
            self._finish(
                key,
                "timeout"
                if timed_out
                else ("ok" if proc.returncode == 0 else "failed"),
                exit_code=proc.returncode,
                output=output,
            )
        except Exception as e:
            self._finish(key, "failed", error=str(e))
        finally:
            with self.lock:
                self.active.discard(proc)
            self.slot.release()

    @staticmethod
    def kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        with self.lock:
            self.closed = True
            for proc in list(self.active):
                self.kill(proc)


class Monitor:
    def __init__(self, store):
        self.store = store
        self.commands = Commands(store)
        self.lock = threading.RLock()
        self.control = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.proc = None
        self.jpeg = None
        self.frame_id = 0
        self.fsm = None
        self.job_config = None
        self.status = {
            "phase": "idle",
            "state": "IDLE",
            "event": None,
            "frames": 0,
            "source_seconds": 0,
            "score": 0,
            "fps": 0,
            "job_id": None,
            "source_name": "",
            "error": None,
            "decision_id": None,
        }

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.status)

    def stop(self):
        with self.control:
            self._stop()

    def _stop(self):
        self.stop_event.set()
        with self.lock:
            proc = self.proc
        if proc and proc.poll() is None:
            proc.kill()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=8)
        if self.thread and self.thread.is_alive():
            raise RuntimeError("decoder did not stop")
        with self.lock:
            if self.status["phase"] not in ("idle", "ended", "error"):
                self.status.update(phase="stopped", state="STOPPED", event=None, fps=0)

    def start(self, source, name, is_stream=False):
        with self.control:
            self._stop()
            self.stop_event = threading.Event()
            config = copy.deepcopy(self.store.config)
            job = uuid.uuid4().hex
            with self.lock:
                self.fsm = IgnitionFSM(
                    config["candidate_threshold"], config["strong_threshold"]
                )
                self.job_config = config
                self.jpeg = None
                self.frame_id += 1
                self.status = {
                    "phase": "connecting",
                    "state": "WARMUP",
                    "event": None,
                    "frames": 0,
                    "source_seconds": 0,
                    "score": 0,
                    "fps": 0,
                    "job_id": job,
                    "source_name": name,
                    "error": None,
                    "decision_id": None,
                }
            self.store.event("started", job_id=job, source_name=name, stream=is_stream)
            self.thread = threading.Thread(
                target=self._run, args=(source, is_stream, job, config), daemon=True
            )
            self.thread.start()
            return self.snapshot()

    def _action(self, result, timestamp):
        if not result.action:
            return
        self.store.event(
            result.action,
            job_id=self.status["job_id"],
            source_seconds=timestamp,
            decision_id=result.decision_id,
            reason=result.reason,
        )
        self.commands.submit(self.status["job_id"], result, self.job_config)

    def retract(self):
        with self.lock:
            if not self.fsm or not self.fsm.confirmed:
                raise ValueError("当前没有可撤销的点火确认")
            t = (self.fsm.last_timestamp or 0) + 0.000001
            result = self.fsm.update(0, t, false_alarm=True)
            self.status.update(
                state=result.state, event=result.event, decision_id=result.decision_id
            )
            self._action(result, self.status["source_seconds"])
            return self.snapshot()

    def _run(self, source, is_stream, job, config):
        cv2.setNumThreads(1)
        count = 0
        started = time.monotonic()
        last_frame = None
        reconnects = 0
        timestamp = 0.0
        ev = None
        guard = None
        while not self.stop_event.is_set():
            ev = ImageEvidence(config["roi"], adapt_alpha=0.02)
            guard = CameraGuard()
            opts = (
                [
                    "-rw_timeout",
                    "5000000",
                    "-analyzeduration",
                    "300000",
                    "-probesize",
                    "32000",
                ]
                if is_stream
                else ["-re"]
            )
            if is_stream and source.startswith(("rtmp://", "rtmps://")):
                opts += ["-rtmp_buffer", "100", "-rtmp_live", "live"]
            if is_stream and source.startswith(("rtsp://", "rtsps://")):
                opts += ["-rtsp_transport", "tcp"]
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "2",
                *opts,
                "-i",
                source,
                "-an",
                "-vf",
                "fps=10,scale=320:180",
                "-pix_fmt",
                "bgr24",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                with self.lock:
                    self.proc = proc
                while not self.stop_event.is_set():
                    data = proc.stdout.read(320 * 180 * 3)
                    if not data:
                        break
                    if len(data) != 320 * 180 * 3:
                        raise RuntimeError("incomplete video frame")
                    frame = np.frombuffer(data, np.uint8).reshape(180, 320, 3)
                    changed = guard.update(frame)
                    if changed:
                        ev = ImageEvidence(config["roi"], adapt_alpha=0.02)
                    features = ev.update(frame)
                    score = (
                        features["novel_score"]
                        if features["warm_new_score"] >= config["min_warm_score"]
                        else 0
                    )
                    now = time.monotonic()
                    # Media frame order establishes 10 FPS evidence; wall gaps invalidate the FSM.
                    with self.lock:
                        if last_frame is not None and now - last_frame > 0.35:
                            timestamp += now - last_frame
                            ev = ImageEvidence(config["roi"], adapt_alpha=0.02)
                            features["ready"] = False
                        timestamp = max(
                            timestamp + 0.1, (self.fsm.last_timestamp or -1) + 0.1
                        )
                        result = self.fsm.update(
                            score,
                            timestamp,
                            features["ready"],
                            changed or features["scene_cut"],
                            features["baseline_return"],
                        )
                        count += 1
                        self.status.update(
                            phase="running",
                            state=result.state,
                            event=result.event,
                            frames=count,
                            source_seconds=round(count / 10, 1),
                            score=score,
                            fps=round(count / max(0.1, now - started), 1),
                            decision_id=result.decision_id,
                            error=None,
                            reconnects=reconnects,
                        )
                        self._action(result, count / 10)
                        ok, jpg = cv2.imencode(
                            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85]
                        )
                        if ok:
                            self.jpeg = jpg.tobytes()
                            self.frame_id += 1
                    last_frame = now
                if self.stop_event.is_set() and proc.poll() is None:
                    proc.kill()
                code = proc.wait(timeout=6)
                proc.stdout.close()
                if self.stop_event.is_set():
                    break
                if not is_stream:
                    if code:
                        raise RuntimeError("视频解码失败，请检查媒体格式")
                    with self.lock:
                        self.status.update(phase="ended", fps=0)
                    self.store.event("ended", job_id=job, frames=count)
                    return
                reconnects += 1
                with self.lock:
                    self.status.update(
                        phase="reconnecting",
                        state="PAUSED",
                        event=None,
                        fps=0,
                        error="信号断开，正在重连",
                    )
                self.store.event("reconnecting", job_id=job, attempt=reconnects)
                if self.stop_event.wait(min(10, 2 * reconnects)):
                    break
            except Exception as e:
                if self.stop_event.is_set():
                    break
                with self.lock:
                    self.status.update(
                        phase="error", event=None, fps=0, error=str(e)[:240]
                    )
                self.store.event("error", job_id=job, message=str(e)[:240])
                return
            finally:
                with self.lock:
                    p = self.proc
                    self.proc = None
                if p and p.poll() is None:
                    p.kill()
                    p.wait()
        with self.lock:
            self.status.update(phase="stopped", state="STOPPED", event=None, fps=0)


def validate_stream(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("rtmp", "rtmps", "rtsp", "rtsps")
        or not parsed.hostname
        or any(c.isspace() for c in url)
    ):
        raise ValueError("请输入有效的 RTMP / RTSP 流地址")
    return url
