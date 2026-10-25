"""Replay complete input with the production V4 image pipeline; labels never arm it."""

import argparse
import csv
import gzip
import hashlib
import json
import math
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from app.engine_glow import EngineGlowPipeline, make_engine_fsm

p = argparse.ArgumentParser()
p.add_argument("video", type=Path)
p.add_argument("--out", type=Path, required=True)
p.add_argument("--segments", type=Path, default=ROOT / "run_config_v4.json")
p.add_argument("--roi", default=".47,.79,.55,.91")
p.add_argument("--strong", type=float, default=0.001)
p.add_argument("--mission")
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=True)
(a.out / "summary.json").unlink(missing_ok=True)
config = json.loads(a.segments.read_text())
roi = list(map(float, a.roi.split(",")))
video_duration = float(
    subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(a.video),
        ]
    )
)
if config.get("input_sha256"):
    with a.video.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != config["input_sha256"]:
            p.error("Video SHA256 differs from the benchmark source")
if a.mission:
    config["segments"] = [s for s in config["segments"] if s["id"] == a.mission]
    if len(config["segments"]) != 1:
        p.error("Unknown mission")
out = a.out
out.mkdir(parents=True, exist_ok=True)
cv2.setNumThreads(1)
segment = None
i = math.ceil(config["segments"][0]["start_s"] * 10) if a.mission else 0
events = []
counts = {}
tick = time.monotonic()
source_hashes = {
    name: hashlib.sha256((ROOT.parent / "app" / name).read_bytes()).hexdigest()
    for name in ["engine_glow.py", "detector.py"]
}
input_options = []
filter_graph = "fps=10,scale=480:270"
if a.mission:
    first = i / 10
    end = config["segments"][0]["end_s"]
    seek = max(0, math.floor(first) - 2)
    input_options = ["-ss", str(seek), "-t", str(end - seek + 0.2), "-copyts"]
    filter_graph = (
        f"fps=10,trim=start_pts={i}:end_pts={math.ceil(end * 10)},scale=480:270"
    )
with (
    (out / "decode.log").open("w") as err,
    gzip.open(out / "predictions.csv.gz", "wt", newline="") as f,
):
    wr = csv.DictWriter(
        f,
        fieldnames=[
            "sample",
            "global_s",
            "segment",
            "local_s",
            "score",
            "warm_score",
            "white_score",
            "ready",
            "scene_cut",
            "state",
            "event",
            "action",
            "decision_id",
        ],
    )
    wr.writeheader()
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-threads",
            "2",
            *input_options,
            "-i",
            str(a.video),
            "-an",
            "-vf",
            filter_graph,
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=err,
    )
    try:
        while data := proc.stdout.read(480 * 270 * 3):
            if len(data) != 480 * 270 * 3:
                raise ValueError("truncated frame")
            t = i / 10
            seg = next(
                (s for s in config["segments"] if s["start_s"] <= t < s["end_s"]),
                config["segments"][-1],
            )
            if segment != seg["id"]:
                segment = seg["id"]
                engine = EngineGlowPipeline(roi)
                fsm = make_engine_fsm(strong=a.strong)
                counts[segment] = {"images": 0, "unknown": 0, "scene_cuts": 0}
            im = np.frombuffer(data, np.uint8).reshape(270, 480, 3)
            z = engine.update(im)
            r = fsm.update(
                z["score"], t, z["ready"], z["scene_cut"], z["baseline_return"]
            )
            local = t - seg["start_s"]
            c = counts[segment]
            c["images"] += 1
            c["unknown"] += r.event is None
            c["scene_cuts"] += z["scene_cut"]
            wr.writerow(
                {
                    "sample": i,
                    "global_s": t,
                    "segment": segment,
                    "local_s": round(local, 6),
                    **{
                        k: z[k]
                        for k in [
                            "score",
                            "warm_score",
                            "white_score",
                            "ready",
                            "scene_cut",
                        ]
                    },
                    **{k: v for k, v in asdict(r).items() if k != "reason"},
                }
            )
            if r.action:
                event = {
                    "sample": i,
                    "global_s": t,
                    "local_s": local,
                    "segment": segment,
                    "score": z["score"],
                    **asdict(r),
                }
                events.append(event)
                if r.action in ("confirmed", "retracted"):
                    cv2.imwrite(str(out / f"{r.action}_{segment}_{i:07d}.jpg"), im)
                    if engine.evidence.reference is not None:
                        x1, y1, x2, y2 = engine.viewport
                        view = im[y1:y2, x1:x2]
                        h, w = view.shape[:2]
                        rx1, ry1, rx2, ry2 = [
                            round(v * d) for v, d in zip(engine.roi, (w, h, w, h))
                        ]
                        cv2.imwrite(
                            str(out / f"baseline_{segment}_{i:07d}.jpg"),
                            engine.evidence.reference.astype(np.uint8),
                        )
                        cv2.imwrite(
                            str(out / f"crop_{segment}_{i:07d}.jpg"),
                            view[ry1:ry2, rx1:rx2],
                        )
                    print(json.dumps(event | {"features": z}), flush=True)
            i += 1
            if i % 6000 == 0:
                progress = {
                    "images": i,
                    "video_hours": round(i / 36000, 3),
                    "wall_seconds": round(time.monotonic() - tick, 1),
                    "counts": counts,
                    "confirmations": sum(e["action"] == "confirmed" for e in events),
                }
                (out / "progress.json").write_text(json.dumps(progress))
                print(json.dumps(progress), flush=True)
        code = proc.wait()
        if code:
            raise RuntimeError(("decode error", code))
        first_sample = (
            math.ceil(config["segments"][0]["start_s"] * 10) if a.mission else 0
        )
        end_sample = min(
            math.ceil(config["segments"][-1]["end_s"] * 10), round(video_duration * 10)
        )
        if i - first_sample != end_sample - first_sample:
            raise RuntimeError(("Incomplete sampled-frame coverage", i, end_sample))
        if source_hashes != {
            name: hashlib.sha256((ROOT.parent / "app" / name).read_bytes()).hexdigest()
            for name in source_hashes
        }:
            raise RuntimeError("Algorithm files changed during replay")
        summary = {
            "video_sha256": config.get("input_sha256"),
            "complete": True,
            "source_hashes": source_hashes,
            "first_sample": math.ceil(config["segments"][0]["start_s"] * 10)
            if a.mission
            else 0,
            "last_sample": i - 1,
            "processed_images": sum(c["images"] for c in counts.values()),
            "processed_seconds": sum(c["images"] for c in counts.values()) / 10,
            "sample_fps": 10,
            "events": events,
            "counts": counts,
            "config": {
                "segments": config["segments"],
                "roi": roi,
                "candidate": 0.0005,
                "strong": a.strong,
                "resolution": [480, 270],
                "confirm_hits": 3,
                "confirm_window": 4,
                "min_confirm_seconds": 0.2,
                "warmup_frames": 50,
                "reacquire_frames": 20,
                "pipeline_sha256": hashlib.sha256(
                    (ROOT.parent / "app/engine_glow.py").read_bytes()
                ).hexdigest(),
            },
            "wall_seconds": time.monotonic() - tick,
        }
        pending = out / "summary.json.tmp"
        pending.write_text(json.dumps(summary, indent=2))
        pending.replace(out / "summary.json")
        print("COMPLETE", i, round(time.monotonic() - tick, 1), flush=True)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
