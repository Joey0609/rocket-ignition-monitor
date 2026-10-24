"""图片区间检测：只确认检出与撤销，不估计点火起点。"""

import argparse, json, pathlib
from dataclasses import asdict
import cv2
from app.detector import ImageEvidence, IgnitionFSM


def main():
    p = argparse.ArgumentParser()
    p.add_argument("folder", type=pathlib.Path)
    p.add_argument("--roi", required=True, help="x1,y1,x2,y2，0~1归一化，框住喷口下方")
    p.add_argument(
        "--fps", type=float, default=10, help="图片序列的采样速率，不是原始视频FPS"
    )
    p.add_argument("--threshold", type=float, default=0.0005)
    p.add_argument("--method", choices=["color", "contrast", "novel"], default="novel")
    p.add_argument(
        "--strong-threshold",
        type=float,
        default=0.05,
        help="连续确认阈值，按机位和误报样本校准",
    )
    p.add_argument(
        "--corrections",
        type=pathlib.Path,
        help='可选误报反馈JSON，例如 [{"sample_index":25}]',
    )
    p.add_argument(
        "--reference", type=pathlib.Path, help="同机位点火前参考图，建议提供"
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
    roi = list(map(float, a.roi.split(",")))
    ev = ImageEvidence(roi, 1 if a.reference else 8)
    ref_shape = None
    if a.reference:
        ref = cv2.imread(str(a.reference))
        if ref is None:
            raise FileNotFoundError(a.reference)
        ref_shape = ref.shape
        ev.update(ref)
    fsm = IgnitionFSM(a.threshold, strong=a.strong_threshold)
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
        features = ev.update(bgr)
        state = fsm.update(
            features[a.method + "_score"],
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
        "notes": "Success means at least one non-retracted confirmation in the image interval. No ignition-onset estimation, RTMP, video input or liftoff confirmation. Need fixed camera and ignition-free baseline.",
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
