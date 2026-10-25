"""Parallelize independent missions while preserving P1/P2 and 10 FPS timestamps."""

import argparse
import csv
import gzip
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser()
    p.add_argument("video", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--roi", default=".47,.79,.55,.91")
    p.add_argument("--strong", type=float, default=0.001)
    p.add_argument(
        "--reuse-complete",
        action="store_true",
        help="Reuse only complete missions with matching source, algorithm, parameters and exact frame coverage",
    )
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "summary.json").unlink(missing_ok=True)
    tick = time.monotonic()
    config = json.loads((ROOT / "run_config_v4.json").read_text())
    with a.video.open("rb") as source:
        video_hash = hashlib.file_digest(source, "sha256").hexdigest()
    if video_hash != config["input_sha256"]:
        p.error("Video SHA256 differs from the benchmark source")
    a.out.mkdir(parents=True, exist_ok=True)

    duration = float(
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
    current_hashes = {
        name: hashlib.sha256((ROOT.parent / "app" / name).read_bytes()).hexdigest()
        for name in ["engine_glow.py", "detector.py"]
    }

    def run(seg):
        folder = a.out / seg["id"]
        folder.mkdir(exist_ok=True)
        saved = folder / "summary.json"
        first = math.ceil(seg["start_s"] * 10)
        end = min(math.ceil(seg["end_s"] * 10), round(duration * 10))
        if a.reuse_complete and saved.exists():
            previous = json.loads(saved.read_text())
            cfg = previous.get("config", {})
            if (
                previous.get("complete")
                and previous.get("video_sha256") == video_hash
                and previous.get("source_hashes") == current_hashes
                and previous.get("first_sample") == first
                and previous.get("last_sample") == end - 1
                and previous.get("processed_images") == end - first
                and cfg.get("roi") == list(map(float, a.roi.split(",")))
                and cfg.get("strong") == a.strong
                and cfg.get("resolution") == [480, 270]
                and cfg.get("confirm_hits") == 3
                and cfg.get("confirm_window") == 4
                and cfg.get("min_confirm_seconds") == 0.2
                and cfg.get("warmup_frames") == 50
                and cfg.get("reacquire_frames") == 20
            ):
                print(
                    "REUSE COMPLETE",
                    seg["id"],
                    previous["processed_images"],
                    flush=True,
                )
                return folder, previous
        with (folder / "console.log").open("w") as log:
            cmd = [
                sys.executable,
                "-u",
                str(ROOT / "run_engine_glow.py"),
                str(a.video.resolve()),
                "--out",
                str(folder.resolve()),
                "--mission",
                seg["id"],
                "--roi",
                a.roi,
                "--strong",
                str(a.strong),
            ]
            result = subprocess.run(
                cmd, check=False, stdout=log, stderr=subprocess.STDOUT
            )
        if result.returncode:
            raise RuntimeError((seg["id"], result.returncode))
        summary = json.loads((folder / "summary.json").read_text())
        if not summary["complete"]:
            raise RuntimeError("Incomplete mission")
        print("MISSION COMPLETE", seg["id"], summary["processed_images"], flush=True)
        return folder, summary

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, config["segments"]))
    cursor = 0
    events = []
    counts = {}
    hashes = None
    with gzip.open(a.out / "predictions.csv.gz", "wt", newline="") as output:
        writer = None
        for folder, summary in results:
            if hashes is None:
                hashes = summary["source_hashes"]
            if hashes != summary["source_hashes"]:
                raise RuntimeError("Mixed algorithm versions")
            if summary["first_sample"] != cursor:
                raise RuntimeError(("Mission gap", cursor, summary["first_sample"]))
            with gzip.open(folder / "predictions.csv.gz", "rt") as source:
                reader = csv.DictReader(source)
                if writer is None:
                    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
                    writer.writeheader()
                for row in reader:
                    if int(row["sample"]) != cursor:
                        raise RuntimeError(
                            ("Nonconsecutive image", cursor, row["sample"])
                        )
                    writer.writerow(row)
                    cursor += 1
            events.extend(summary["events"])
            counts.update(summary["counts"])
            for frame in folder.glob("*.jpg"):
                shutil.copyfile(frame, a.out / frame.name)
    if cursor != round(duration * 10):
        raise RuntimeError(("Frame coverage mismatch", cursor, duration))
    summary = {
        "complete": True,
        "video_sha256": video_hash,
        "processed_images": cursor,
        "processed_seconds": cursor / 10,
        "sample_fps": 10,
        "events": events,
        "counts": counts,
        "source_hashes": hashes,
        "config": {
            "segments": config["segments"],
            "roi": list(map(float, a.roi.split(","))),
            "strong": a.strong,
            "resolution": [480, 270],
            "confirm_hits": 3,
            "confirm_window": 4,
            "min_confirm_seconds": 0.2,
            "warmup_frames": 50,
            "reacquire_frames": 20,
        },
        "wall_seconds": time.monotonic() - tick,
        "execution": "Independent launch missions processed in parallel; same-launch pages never reset. copyts + fps before trim preserves global sampled-frame grid.",
    }
    pending = a.out / "summary.json.tmp"
    pending.write_text(json.dumps(summary, indent=2))
    pending.replace(a.out / "summary.json")
    print("COMPLETE", cursor, round(summary["wall_seconds"], 1), flush=True)


if __name__ == "__main__":
    main()
