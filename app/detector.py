"""传统视觉证据与状态机。只接受 BGR 图片/时间戳，无神经网络、无视频输入。"""

from collections import deque
from dataclasses import dataclass
import argparse, json
import cv2
import numpy as np


def roi_pixels(shape, roi):
    h, w = shape[:2]
    x1, y1, x2, y2 = roi
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise ValueError("ROI must be normalized x1,y1,x2,y2")
    return round(x1 * w), round(y1 * h), round(x2 * w), round(y2 * h)


def max_component(mask):
    mask = cv2.morphologyEx(
        mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
    )
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    return float(stats[1:, cv2.CC_STAT_AREA].max() / mask.size) if n > 1 else 0.0


def flame_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    # 黄橙红火焰、过曝白色核心；蓝紫氢焰可能漏检，属于已知失效边界。
    warm = ((h <= 38) | (h >= 170)) & (s >= 65) & (v >= 160)
    white = (s <= 70) & (v >= 225)
    return warm | white, v


class ImageEvidence:
    def __init__(self, roi, warmup_frames=8, delta_v=30, adapt_alpha=0.0):
        self.roi = roi
        self.warmup_frames = warmup_frames
        self.delta_v = delta_v
        self.samples = []
        self.reference = None
        self.previous = None
        self.static_lights = None
        self.mask_samples = []
        self.adapt_alpha = adapt_alpha
        self.light_frequency = None

    def update(self, bgr):
        x1, y1, x2, y2 = roi_pixels(bgr.shape, self.roi)
        mask, v = flame_mask(bgr)
        outside = np.ones(v.shape, dtype=bool)
        outside[y1:y2, x1:x2] = False
        color = max_component(mask[y1:y2, x1:x2])
        cut = False
        if self.previous is not None:
            if v.shape != self.previous.shape:
                cut = True
            else:
                d = np.abs(v.astype(np.int16) - self.previous.astype(np.int16))
                # 简易切镜保护；不保证发现所有切镜，完整应用须手动/外部确认机位。
                cut = float(np.mean(d[outside])) > 65
        self.previous = v.copy()
        if cut:
            self.samples = []
            self.reference = None
            self.mask_samples = []
            self.static_lights = None
        if self.reference is None:
            self.samples.append(v.copy())
            self.mask_samples.append(mask.copy())
            if len(self.samples) >= self.warmup_frames:
                self.reference = np.median(np.stack(self.samples), axis=0).astype(
                    np.float32
                )
                stable = np.mean(np.stack(self.mask_samples), axis=0) >= 0.5
                self.light_frequency = stable.astype(np.float32)
                # 已有灯光及2像素邻域不作为新增火焰。抑制亮灯边缘的轻微位移/曝光误差。
                self.static_lights = cv2.dilate(
                    stable.astype(np.uint8), np.ones((5, 5), np.uint8)
                ).astype(bool)
                self.samples = []
                self.mask_samples = []
            return {
                "color_score": color,
                "contrast_score": 0.0,
                "novel_score": 0.0,
                "warm_new_score": 0.0,
                "ready": False,
                "scene_cut": cut,
                "exposure_delta": 0.0,
                "baseline_return": False,
            }
        # 用ROI外中位变化抵消全局曝光漂移；火焰覆盖全图时此假设会失效。
        shift = float(
            np.median(v.astype(np.float32)[outside] - self.reference[outside])
        )
        increased = (v.astype(np.float32) - self.reference - shift) > self.delta_v
        changed = mask & increased
        contrast = max_component(changed[y1:y2, x1:x2])
        novel = max_component((changed & ~self.static_lights)[y1:y2, x1:x2])
        # 白色放气/水汽本身不足以确认火焰；供长录像核验使用的新增高饱和暖色证据。
        hh, ss, vv = cv2.split(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV))
        warm_new = (
            ((hh <= 38) | (hh >= 170))
            & (ss >= 100)
            & (vv >= 170)
            & increased
            & ~self.static_lights
        )
        warm_score = max_component(warm_new[y1:y2, x1:x2])
        residual = np.abs(v.astype(np.float32) - self.reference - shift)[y1:y2, x1:x2]
        # 同机位画面确实恢复到点火前背景才作为撤销的反证；单纯火光消失不够。
        baseline_return = float(np.mean(residual > 25)) <= 0.04
        result = {
            "color_score": color,
            "contrast_score": contrast,
            "novel_score": novel,
            "warm_new_score": warm_score,
            "ready": True,
            "scene_cut": cut,
            "exposure_delta": shift,
            "baseline_return": baseline_return,
        }
        # 长时段缓慢光照/轻微漂移不能一直和几小时前的静态背景比较。
        # 已出现较强火焰证据时冻结更新，避免把点火吸收进背景。
        if self.adapt_alpha and not (novel >= 0.01 and warm_score >= 0.0003):
            a = self.adapt_alpha
            self.reference *= 1 - a
            self.reference += a * v.astype(np.float32)
            self.light_frequency *= 1 - a
            self.light_frequency += a * mask.astype(np.float32)
            self.static_lights = cv2.dilate(
                (self.light_frequency >= 0.7).astype(np.uint8),
                np.ones((5, 5), np.uint8),
            ).astype(bool)
        return result


@dataclass
class EventResult:
    state: str
    event: int | None
    action: str | None = None
    decision_id: int | None = None
    reason: str = ""


class IgnitionFSM:
    """区间检出，可取消疑似、撤销错误确认；不估计或回溯点火时刻。

    high为弱候选阈值，strong为校准后的确认阈值。新任务区间需新建实例。
    确认后短期复核：持续低分且画面恢复到点火前背景，才自动撤销。
    火焰被烟雾遮住或离开ROI并不等于事件从未发生，不单凭低分撤销。
    """

    def __init__(self, high=0.0005, strong=0.05, low=None, review_seconds=3.0):
        if high <= 0 or strong < high:
            raise ValueError("0 < high <= strong required")
        self.high = high
        self.strong = strong
        self.low = high * 0.65 if low is None else low
        self.review_seconds = review_seconds
        self.window = deque(maxlen=10)
        self.clear_window = deque(maxlen=8)
        self.last_timestamp = None
        self.state = "WARMUP"
        self.confirmed = False
        self.sequence_id = 0
        self.decision_id = None
        self.candidate_since = None
        self.confirmed_at = None
        self.cooldown_until = -1

    def _result(self, action=None, reason="", unknown=False, decision_id=None):
        return EventResult(
            self.state,
            None if unknown else int(self.confirmed),
            action,
            self.decision_id if decision_id is None else decision_id,
            reason,
        )

    def _cancel(self, timestamp, confirmed, reason):
        previous = self.decision_id
        self.confirmed = False
        self.decision_id = None
        self.candidate_since = None
        self.confirmed_at = None
        self.window.clear()
        self.clear_window.clear()
        self.cooldown_until = timestamp + 0.5
        self.state = "COOLDOWN"
        return self._result(
            "retracted" if confirmed else "candidate_cancelled",
            reason,
            decision_id=previous,
        )

    def update(
        self,
        score,
        timestamp,
        ready=True,
        scene_cut=False,
        baseline_return=False,
        false_alarm=False,
    ):
        if self.last_timestamp is not None and timestamp <= self.last_timestamp:
            raise ValueError("Images must have strictly increasing timestamps")
        gap = self.last_timestamp is not None and timestamp - self.last_timestamp > 0.35
        self.last_timestamp = timestamp
        if false_alarm and self.decision_id is not None:
            return self._cancel(
                timestamp, self.confirmed, "explicit_false_alarm_feedback"
            )
        if gap or scene_cut or not ready:
            self.window.clear()
            self.clear_window.clear()
            self.candidate_since = None
            if not self.confirmed:
                self.decision_id = None
            self.state = "PAUSED" if self.confirmed else "WARMUP"
            return self._result(reason="camera_or_input_not_valid", unknown=True)
        if self.confirmed:
            clear = score < self.low and baseline_return
            self.clear_window.append((timestamp, clear))
            self.state = "CONFIRMED" if score >= self.low else "REVIEW"
            if (
                timestamp - self.confirmed_at <= self.review_seconds
                and len(self.clear_window) == 8
            ):
                if (
                    all(x[1] for x in self.clear_window)
                    and timestamp - self.clear_window[0][0] >= 0.6
                ):
                    return self._cancel(
                        timestamp, True, "sustained_return_to_preignition_background"
                    )
            return self._result()
        if timestamp < self.cooldown_until:
            self.state = "COOLDOWN"
            return self._result()
        self.window.append((timestamp, score >= self.high, score >= self.strong))
        short = list(self.window)[-4:]
        if self.candidate_since is None:
            if sum(x[1] for x in short) < 3:
                self.state = "ARMED"
                return self._result()
            self.sequence_id += 1
            self.decision_id = self.sequence_id
            self.candidate_since = timestamp
        self.state = "VERIFYING"
        if (
            len(self.window) == 10
            and sum(x[2] for x in self.window) >= 8
            and score >= self.strong
        ):
            if timestamp - self.window[0][0] >= 0.6:
                self.confirmed = True
                self.confirmed_at = timestamp
                self.state = "CONFIRMED"
                return self._result("confirmed", "sustained_strong_evidence")
        if sum(x[1] for x in short) == 0:
            return self._cancel(timestamp, False, "candidate_disappeared")
        if timestamp - self.candidate_since >= 1.5:
            return self._cancel(timestamp, False, "strong_evidence_not_sustained")
        return self._result()


def main():
    p = argparse.ArgumentParser(description="只读图片。批量时序与评估见 evaluate.py。")
    p.add_argument("image")
    p.add_argument("--reference")
    p.add_argument("--roi", default="0.2,0.5,0.8,0.99")
    p.add_argument("--threshold", type=float, default=0.0005)
    p.add_argument("--method", choices=["color", "contrast", "novel"], default="novel")
    a = p.parse_args()
    roi = list(map(float, a.roi.split(",")))
    bgr = cv2.imread(a.image)
    if bgr is None:
        raise FileNotFoundError(a.image)
    ev = ImageEvidence(roi, warmup_frames=1)
    if a.reference:
        reference = cv2.imread(a.reference)
        if reference is None:
            raise FileNotFoundError(a.reference)
        if reference.shape != bgr.shape:
            raise ValueError("Reference must match image size and camera")
        ev.update(reference)
    result = ev.update(bgr)
    key = a.method + "_score" if a.reference else "color_score"
    result.update(
        {
            "image": a.image,
            "fire_candidate": int(result[key] >= a.threshold),
            "score_is_probability": False,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
