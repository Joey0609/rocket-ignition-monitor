"""Complete, independent per-P replay. Labels never enter the detector."""
import argparse
import csv
from dataclasses import asdict
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))
from app.engine_glow import EngineGlowPipeline, make_engine_fsm


def verify_freeze():
    frozen = json.loads((HERE / 'freeze.json').read_text())
    current = {p: hashlib.sha256((REPO / p).read_bytes()).hexdigest() for p in frozen['source_hashes']}
    if current != frozen['source_hashes']:
        raise RuntimeError('Frozen production sources changed')
    return current


def replay(video):
    hashes = verify_freeze()
    video = Path(video).resolve()
    out = HERE / 'runs' / video.stem
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').unlink(missing_ok=True)
    duration = float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', str(video)]))
    with video.open('rb') as f:
        video_hash = hashlib.file_digest(f, 'sha256').hexdigest()
    cv2.setNumThreads(1)
    engine = EngineGlowPipeline()
    fsm = make_engine_fsm()
    events = []
    counts = {'images': 0, 'unknown': 0, 'scene_cuts': 0}
    start = time.monotonic()
    fields = ['sample', 'source_s', 'score', 'warm_score', 'white_score', 'ready', 'scene_cut', 'baseline_return', 'state', 'event', 'action', 'decision_id', 'reason', 'viewport', 'active_roi']
    with (out / 'decode.log').open('w') as err, gzip.open(out / 'predictions.csv.gz', 'wt', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        proc = subprocess.Popen(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '2', '-i', str(video), '-an', '-vf', 'fps=10,scale=480:270', '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1'], stdout=subprocess.PIPE, stderr=err)
        try:
            while raw := proc.stdout.read(480 * 270 * 3):
                if len(raw) != 480 * 270 * 3:
                    raise RuntimeError('Truncated decoded image')
                i = counts['images']
                t = i / 10
                image = np.frombuffer(raw, np.uint8).reshape(270, 480, 3)
                z = engine.update(image)
                r = fsm.update(z['score'], t, z['ready'], z['scene_cut'], z['baseline_return'])
                writer.writerow({'sample': i, 'source_s': t, **{k: z[k] for k in ['score', 'warm_score', 'white_score', 'ready', 'scene_cut', 'baseline_return', 'viewport', 'active_roi']}, **asdict(r)})
                counts['images'] += 1
                counts['unknown'] += r.event is None
                counts['scene_cuts'] += z['scene_cut']
                if r.action:
                    event = {'sample': i, 'source_s': t, **asdict(r), 'score': z['score'], 'viewport': z['viewport'], 'active_roi': z['active_roi']}
                    events.append(event)
                    if r.action in ('confirmed', 'retracted'):
                        cv2.imwrite(str(out / f'{r.action}_{i:07d}.jpg'), image)
                        print(video.stem, json.dumps(event), flush=True)
                if counts['images'] % 3000 == 0:
                    progress = {**counts, 'source_s': t, 'duration_s': duration, 'wall_s': round(time.monotonic()-start, 2)}
                    (out / 'progress.json').write_text(json.dumps(progress))
                    print(video.stem, 'PROGRESS', counts['images'], round(100*t/duration,1), flush=True)
        finally:
            proc.stdout.close()
            code = proc.wait()
    if code != 0 or (out / 'decode.log').stat().st_size:
        raise RuntimeError(f'Incomplete decode: {video}, return={code}, frames={counts}, duration={duration}')
    completion_verification = {'ffmpeg_exit_code': code, 'decode_errors': 0}
    if abs(counts['images']/10 - duration) > .15:
        # Container duration may include timing beyond the last presented frame.
        # Independently count a complete fps=10 decode instead of relaxing bounds.
        check = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-threads', '2', '-i', str(video), '-an', '-vf', 'fps=10', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-'], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        frames = [int(line.split('=', 1)[1]) for line in check.stdout.splitlines() if line.startswith('frame=')]
        if check.stderr.strip() or not frames or frames[-1] != counts['images']:
            raise RuntimeError('Independent complete decode frame count differs')
        completion_verification['independent_full_decode_frames'] = frames[-1]
    verify_freeze()
    summary = {'complete': True, 'input': str(video), 'video_sha256': video_hash, 'duration_s': duration, 'sample_fps': 10, 'resolution': [480,270], 'counts': counts, 'events': events, 'source_hashes': hashes, 'layout_adaptation': 'None. Existing automatic viewport and fixed ROI unchanged.', 'reset_policy': 'New detector at selected P start. No reset within P. No manual false-alarm feedback.', 'wall_s': round(time.monotonic()-start,2)}
    summary['completion_verification'] = completion_verification
    temp = out / 'summary.json.tmp'
    temp.write_text(json.dumps(summary, indent=2))
    temp.replace(out / 'summary.json')
    print('COMPLETE', video.stem, counts, summary['wall_s'], flush=True)
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('videos', nargs='+')
    args = p.parse_args()
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(replay, args.videos))
