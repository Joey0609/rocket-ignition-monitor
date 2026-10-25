"""图片区间检测：只确认检出与撤销，不估计点火起点。"""

import argparse
import json
import pathlib
from dataclasses import asdict

import cv2

from app.detector import IgnitionFSM, ImageEvidence
from app.engine_glow import EngineGlowPipeline, make_engine_fsm


def main():
    p = argparse.ArgumentParser()
    p.add_argument("folder", type=pathlib.Path)
    p.add_argument(
        "--detector-mode", choices=["engine_glow", "legacy"], default="engine_glow"
    )
    p.add_argument("--no-auto-viewport", action="store_true")
    p.add_argument(
        "--roi", required=True, help="x1,y1,x2,y2，0~1归一化，框住喷口与邻近照亮区域"
    )
    p.add_argument(
        "--fps", type=float, default=10, help="图片序列的采样速率，不是原始视频FPS"
    )
    p.add_argument("--threshold", type=float, default=0.0005)
    p.add_argument("--method", choices=["color", "contrast", "novel"], default="novel")
    p.add_argument(
        "--strong-threshold",
        type=float,
        default=None,
        help="连续确认阈值，按机位和误报样本校准",
    )
    p.add_argument(
        "--corrections",
        type=pathlib.Path,
        help='可选误报反馈JSON，例如 [{"sample_index":25}]',
    )
    p.add_argument(
        "--reference", type=pathlib.Path, help="仅旧版模式：同机位点火前参考图"
    )
    p.add_argument(
        "--out", type=pathlib.Path, default=pathlib.Path("image_predictions.json")
    )
    a = p.parse_args()
    if a.fps <= 0:
        p.error("--fps must be positive")
    files = sorted(
        x
        for x in a.folder.iterdir()
        if x.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
    )
    if not files:
        p.error("No images found")
    try:
        roi = list(map(float, a.roi.split(",")))
    except ValueError:
        p.error("--roi must contain four numbers")
    if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
        p.error("--roi must be ordered normalized x1,y1,x2,y2")
    cv2.setNumThreads(1)
    engine_mode = a.detector_mode == "engine_glow"
    if engine_mode and abs(a.fps - 10) > 1e-9:
        p.error("Engine mode expects a 10 FPS sampled image sequence")
    if engine_mode and a.reference:
        p.error(
            "Engine mode uses the first 50 sequence frames for background; --reference is legacy-only"
        )
    strong = (
        a.strong_threshold
        if a.strong_threshold is not None
        else (0.001 if engine_mode else 0.05)
    )
    if not 0 < a.threshold <= strong <= 1:
        p.error("Thresholds must satisfy 0 < candidate <= strong <= 1")
    ev = (
        EngineGlowPipeline(roi, not a.no_auto_viewport)
        if engine_mode
        else ImageEvidence(roi, 1 if a.reference else 8)
    )
    ref_shape = None
    if a.reference:
        ref = cv2.imread(str(a.reference))
        if ref is None:
            raise FileNotFoundError(a.reference)
        ref_shape = ref.shape
        ev.update(ref)
    fsm = (
        make_engine_fsm(a.threshold, strong)
        if engine_mode
        else IgnitionFSM(a.threshold, strong=strong)
    )
    rows = []
    updates = []
    active = {}
    corrections = set()
    if a.corrections:
        corrections = {
            int(x["sample_index"]) for x in json.loads(a.corrections.read_text())
        }
    for i, file in enumerate(files):
        bgr = cv2.imread(str(file))
        if bgr is None:
            raise FileNotFoundError(file)
        if ref_shape is not None and bgr.shape != ref_shape:
            raise ValueError("Reference and all images must match dimensions")
        if engine_mode:
            bgr = cv2.resize(bgr, (480, 270), interpolation=cv2.INTER_LINEAR)
        features = ev.update(bgr)
        state = fsm.update(
            features["score"] if engine_mode else features[a.method + "_score"],
            i / a.fps,
            features["ready"],
            features["scene_cut"],
            features["baseline_return"],
            i in corrections,
        )
        rows.append(
            {
                "path": str(file),
                "sample_index": i,
                "timestamp_s": i / a.fps,
                **features,
                **asdict(state),
            }
        )
        if state.action:
            update = {"sample_index": i, "path": str(file), **asdict(state)}
            updates.append(update)
            if state.action == "confirmed":
                active[state.decision_id] = update
            elif state.action == "retracted":
                active.pop(state.decision_id, None)
    output = {
        "interval_detected": int(bool(active)),
        "valid_confirmations": list(active.values()),
        "updates": updates,
        "predictions": rows,
        "score_is_probability": False,
        "detector_mode": a.detector_mode,
        "notes": "Engine mode checks initial engine glow and bright cores, without requiring liftoff. interval_detected is only an eventual-hit summary; updates give actual confirmation times, never backdated. Need an ignition-free background prefix (50 frames in engine mode).",
    }
    a.out.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {
                "interval_detected": output["interval_detected"],
                "valid_confirmations": len(active),
                "correction_actions": sum(x["action"] == "retracted" for x in updates),
                "output": str(a.out),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
