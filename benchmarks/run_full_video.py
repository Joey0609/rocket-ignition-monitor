"""拼接视频全量10FPS检测。发射标注不传入检测器，仅在评估脚本使用。"""

import argparse, csv, gzip, hashlib, json, pathlib, subprocess, sys, time
from dataclasses import asdict
import cv2, numpy as np

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from app.detector import ImageEvidence, IgnitionFSM
from app.camera_guard import CameraGuard

p = argparse.ArgumentParser()
p.add_argument("--config", default="run_config.json")
p.add_argument("--output", default="full_run_v1")
args = p.parse_args()
config = json.loads((ROOT / args.config).read_text())
out = ROOT / args.output
out.mkdir(parents=True, exist_ok=True)
cv2.setNumThreads(1)
w = config["width"]
h = config["height"]
fps = config["sample_fps"]
video = ROOT / config["input"]
seg_index = -1
counts = {}
events = []
resets = []
cuts = 0
not_ready = 0
start = time.time()
proc = subprocess.Popen(
    [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-threads",
        "2",
        "-i",
        str(video),
        "-an",
        "-vf",
        f"fps={fps},scale={w}:{h}",
        "-pix_fmt",
        "bgr24",
        "-f",
        "rawvideo",
        "pipe:1",
    ],
    stdout=subprocess.PIPE,
    stderr=open(out / "decode.log", "w"),
)
with gzip.open(out / "predictions.csv.gz", "wt", newline="") as f:
    cols = [
        "sample",
        "global_s",
        "segment",
        "local_s",
        "novel_score",
        "warm_new_score",
        "effective_score",
        "ready",
        "scene_cut",
        "state",
        "event",
        "action",
        "decision_id",
    ]
    writer = csv.DictWriter(f, fieldnames=cols)
    writer.writeheader()
    i = 0
    while True:
        data = proc.stdout.read(w * h * 3)
        if not data:
            break
        if len(data) != w * h * 3:
            raise RuntimeError("Truncated decoded image")
        t = i / fps
        target = next(
            (
                j
                for j, s in enumerate(config["segments"])
                if s["start_s"] <= t < s["end_s"]
            ),
            len(config["segments"]) - 1,
        )
        if target != seg_index:
            seg_index = target
            seg = config["segments"][target]
            ev = ImageEvidence(config["roi"], adapt_alpha=config.get("adapt_alpha", 0))
            fsm = IgnitionFSM(
                config["candidate_threshold"], strong=config["strong_threshold"]
            )
            guard = CameraGuard()
            counts[seg["id"]] = {"images": 0, "not_ready": 0, "scene_cuts": 0}
            resets.append(
                {
                    "sample": i,
                    "global_s": t,
                    "segment": seg["id"],
                    "reason": "new_launch_mission",
                }
            )
        frame = np.frombuffer(data, np.uint8).reshape(h, w, 3)
        camera_changed = config.get("camera_guard", False) and guard.update(frame)
        if camera_changed:
            ev = ImageEvidence(config["roi"], adapt_alpha=config.get("adapt_alpha", 0))
        features = ev.update(frame)
        if camera_changed:
            features["scene_cut"] = True
        evidence = features["novel_score"]
        if features["warm_new_score"] < config.get("min_warm_score", 0):
            evidence = 0.0
        result = fsm.update(
            evidence,
            t,
            features["ready"],
            features["scene_cut"],
            features["baseline_return"],
        )
        local = t - seg["start_s"]
        c = counts[seg["id"]]
        c["images"] += 1
        c["not_ready"] += int(not features["ready"])
        c["scene_cuts"] += int(features["scene_cut"])
        writer.writerow(
            {
                "sample": i,
                "global_s": t,
                "segment": seg["id"],
                "local_s": round(local, 6),
                "novel_score": features["novel_score"],
                "warm_new_score": features["warm_new_score"],
                "effective_score": evidence,
                "ready": features["ready"],
                "scene_cut": features["scene_cut"],
                "state": result.state,
                "event": result.event,
                "action": result.action,
                "decision_id": result.decision_id,
            }
        )
        if result.action:
            event = {
                "sample": i,
                "global_s": t,
                "local_s": local,
                "segment": seg["id"],
                "score": evidence,
                "warm_new_score": features["warm_new_score"],
                **asdict(result),
            }
            events.append(event)
            if result.action in {"confirmed", "retracted"}:
                cv2.imwrite(
                    str(out / f"{result.action}_{seg['id']}_{i:07d}.jpg"), frame
                )
                print(json.dumps(event), flush=True)
        if i % 18000 == 0:
            checkpoint = {
                "processed_images": i + 1,
                "video_hours": round(t / 3600, 3),
                "wall_seconds": round(time.time() - start, 1),
                "counts": counts,
                "confirmations": sum(e["action"] == "confirmed" for e in events),
            }
            (out / "progress.json").write_text(json.dumps(checkpoint, indent=2))
            print(json.dumps(checkpoint), flush=True)
        i += 1
code = proc.wait()
if code != 0:
    raise RuntimeError(("Decoder failed", code))
summary = {
    "config": config,
    "config_sha256": hashlib.sha256((ROOT / args.config).read_bytes()).hexdigest(),
    "complete": True,
    "processed_images": i,
    "sample_fps": fps,
    "processed_seconds": i / fps,
    "wall_seconds": time.time() - start,
    "counts": counts,
    "resets": resets,
    "events": events,
    "evaluation_note": "All confirmed actions count when scoring false alerts, even if later retracted. No ignition time is available to detector.",
}
(out / "summary.json").write_text(json.dumps(summary, indent=2))
print(
    "COMPLETE",
    json.dumps(
        {
            "images": i,
            "wall_seconds": summary["wall_seconds"],
            "confirmations": sum(e["action"] == "confirmed" for e in events),
        }
    ),
    flush=True,
)
