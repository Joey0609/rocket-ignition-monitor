"""Causal traditional CV for small, first-visible engine glow (no learned model)."""

import cv2
import numpy as np


class MainViewport:
    """Find SpaceLens's orange broadcast frame; otherwise use the complete image."""

    def bounds(self, frame):
        h, w = frame.shape[:2]
        factor = 2 if w >= 700 else 1
        small = cv2.resize(
            frame, (w // factor, h // factor), interpolation=cv2.INTER_AREA
        )
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        mask = (
            ((hsv[:, :, 0] <= 25) | (hsv[:, :, 0] >= 170))
            & (hsv[:, :, 1] >= 120)
            & (hsv[:, :, 2] >= 110)
        )
        sh, sw = mask.shape
        columns = mask.sum(axis=0)
        lefts = np.flatnonzero(columns[: int(sw * 0.12)] >= sh * 0.42)
        rights = np.flatnonzero(
            columns[int(sw * 0.65) : int(sw * 0.87)] >= sh * 0.35
        ) + int(sw * 0.65)
        if not len(lefts) or not len(rights):
            return (0, 0, w, h)
        left, right = int(lefts[0]), int(rights[-1])
        row_counts = mask[:, left : right + 1].sum(axis=1)
        tops = np.flatnonzero(row_counts[: int(sh * 0.15)] >= (right - left) * 0.72)
        bottoms = np.flatnonzero(
            row_counts[int(sh * 0.65) : int(sh * 0.91)] >= (right - left) * 0.72
        ) + int(sh * 0.65)
        if not len(tops) or not len(bottoms):
            return (0, 0, w, h)
        top, bottom = int(tops[0]), int(bottoms[-1])
        if not 0.45 * sh <= bottom - top <= 0.9 * sh:
            return (0, 0, w, h)
        margin = max(2, round(w * 0.004))
        return (
            left * factor + margin,
            top * factor + margin,
            right * factor - margin,
            bottom * factor - margin,
        )


class BackgroundGuard:
    """Large scene changes invalidate evidence; local ignition light does not."""

    def __init__(self):
        self.previous = None

    def update(self, view):
        gray = cv2.cvtColor(cv2.resize(view, (256, 144)), cv2.COLOR_BGR2GRAY)
        # Upper and lateral background, away from the nozzle and central vehicle.
        mask = np.zeros(gray.shape, np.uint8)
        mask[8:94, 4:94] = 1
        mask[8:94, 166:246] = 1
        cut = False
        if self.previous is not None:
            residual = np.abs(gray.astype(np.int16) - self.previous.astype(np.int16))
            residual = residual[mask.astype(bool)]
            cut = np.mean(residual > 35) > 0.45 and np.mean(residual) > 40
        self.previous = gray
        return bool(cut)


class Stabilizer:
    """Track static upper-scene corners; align current pixels to the background."""

    def __init__(self):
        self.reference = None
        self.points = None
        self.frames = 0
        self.changed = False
        self.registration = {}

    def _reference(self, gray):
        self.reference = gray.copy()
        mask = np.zeros(gray.shape, np.uint8)
        mask[10:125, 8:110] = 255
        mask[10:125, 210:308] = 255
        self.points = cv2.goodFeaturesToTrack(
            gray, maxCorners=90, qualityLevel=0.025, minDistance=5, mask=mask
        )

    def _failed(self, gray):
        # A locally illuminated vehicle is not a changed camera.
        residual = np.abs(gray.astype(np.int16) - self.reference.astype(np.int16))
        lateral = np.concatenate(
            [residual[10:125, 8:110].ravel(), residual[10:125, 210:308].ravel()]
        )
        self.changed = bool(np.mean(lateral) > 3 and np.mean(lateral > 15) > 0.08)
        if self.changed:
            self._reference(gray)
        self.registration = {"valid": False}

    def align(self, view):
        self.changed = False
        small = cv2.resize(view, (320, 180), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        if self.reference is None:
            self._reference(gray)
            return view
        if self.points is None or len(self.points) < 8:
            return view
        q, status, _ = cv2.calcOpticalFlowPyrLK(
            self.reference,
            gray,
            self.points,
            None,
            winSize=(21, 21),
            maxLevel=2,
            criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 15, 0.02),
        )
        back, back_status, _ = cv2.calcOpticalFlowPyrLK(
            gray,
            self.reference,
            q,
            None,
            winSize=(21, 21),
            maxLevel=2,
            criteria=(cv2.TERM_CRITERIA_COUNT | cv2.TERM_CRITERIA_EPS, 15, 0.02),
        )
        good = (
            status.ravel().astype(bool)
            & back_status.ravel().astype(bool)
            & (np.linalg.norm(back - self.points, axis=2).ravel() < 0.8)
        )
        if np.count_nonzero(good) < 8:
            self._failed(gray)
            return view
        matrix, inliers = cv2.estimateAffinePartial2D(
            self.points[good],
            q[good],
            method=cv2.RANSAC,
            ransacReprojThreshold=0.8,
            maxIters=400,
            confidence=0.98,
        )
        if matrix is None or np.mean(inliers) < 0.5:
            self._failed(gray)
            return view
        scale = float(np.hypot(matrix[0, 0], matrix[0, 1]))
        self.registration = {
            "scale": scale,
            "dx": float(matrix[0, 2]),
            "dy": float(matrix[1, 2]),
            "inliers": float(np.mean(inliers)),
        }
        if not 0.98 < scale < 1.02 or max(abs(matrix[0, 2]), abs(matrix[1, 2])) > 4:
            self._failed(gray)
            return view
        h, w = view.shape[:2]
        full = matrix.copy()
        # Convert the small-image transform to source-pixel coordinates.
        full[0, 1] *= (w / 320) / (h / 180)
        full[1, 0] *= (h / 180) / (w / 320)
        full[0, 2] *= w / 320
        full[1, 2] *= h / 180
        aligned = cv2.warpAffine(
            view,
            full,
            (w, h),
            flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
            borderMode=cv2.BORDER_REFLECT,
        )
        aligned_gray = cv2.cvtColor(
            cv2.resize(aligned, (320, 180), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2GRAY,
        )
        before = float(
            np.mean(
                np.abs(
                    gray[10:120, 8:308].astype(float) - self.reference[10:120, 8:308]
                )
            )
        )
        after = float(
            np.mean(
                np.abs(
                    aligned_gray[10:120, 8:308].astype(float)
                    - self.reference[10:120, 8:308]
                )
            )
        )
        if before < 2.5 or after >= before * 0.97:
            aligned = view
        self.frames += 1
        return aligned


class LocalGlowEvidence:
    """Adaptive RGB/HSV background; retain small hot cores without 3x3 opening."""

    def __init__(self, roi, warmup_frames=50):
        self.roi = roi
        self.warmup_frames = warmup_frames
        self.samples = []
        self.reference = None
        self.noise = None
        self.level_reference = None
        self.peak_value = None

    def update(self, view):
        h, w = view.shape[:2]
        x1, y1, x2, y2 = [round(a * b) for a, b in zip(self.roi, (w, h, w, h))]
        crop = view[y1:y2, x1:x2]
        level = float(
            np.median(cv2.cvtColor(view[: int(h * 0.6) : 4, ::4], cv2.COLOR_BGR2GRAY))
        )
        empty = {
            "score": 0.0,
            "warm_score": 0.0,
            "white_score": 0.0,
            "ready": False,
            "baseline_return": False,
        }
        if self.reference is None:
            self.samples.append(crop.copy())
            if len(self.samples) >= self.warmup_frames:
                stack = np.stack(self.samples).astype(np.float32)
                self.reference = np.median(stack, axis=0)
                self.peak_value = np.max(stack, axis=(0, 3))
                self.noise = np.mean(np.abs(stack - self.reference), axis=0)
                self.level_reference = level
                self.samples = []
            return empty
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        rhsv = cv2.cvtColor(self.reference.astype(np.uint8), cv2.COLOR_BGR2HSV)
        hue, saturation, value = cv2.split(hsv)
        rh, rs, rv = cv2.split(rhsv)
        shift = level - self.level_reference
        rgb_residual = np.abs(crop.astype(np.float32) - self.reference - shift)
        new = np.any(rgb_residual > np.maximum(35, 6 * self.noise), axis=2)
        delta = value.astype(np.float32) - rv - shift
        warm = (
            (hue >= 8)
            & (hue <= 40)
            & (saturation >= 65)
            & (value >= (140 if level < 35 else 180))
            & ((delta > 18) | ((saturation.astype(float) - rs) > 30))
            & new
        )
        prior_warm = ((rh <= 45) | (rh >= 170)) & (rs >= 55) & (rv >= 100)
        near_prior_warm = cv2.dilate(
            prior_warm.astype(np.uint8), np.ones((3, 3), np.uint8)
        ).astype(bool)
        warm &= ~near_prior_warm
        peak_near = cv2.dilate(self.peak_value, np.ones((3, 3), np.uint8))
        # A red cover changing saturation is not an emitter: require genuinely
        # new light above the history of the same registered neighborhood.
        warm &= (delta > 30) & ((value.astype(np.float32) - peak_near - shift) > 25)
        # Daylight steam usually lacks a compact newly saturated hot white core.
        white = (
            (saturation <= 95)
            & (value >= (225 if level < 35 else 245))
            & (delta > (35 if level < 35 else 80))
            & new
        )
        prior_bright = (rs <= 110) & (rv >= 165)
        near_prior_bright = cv2.dilate(
            prior_bright.astype(np.uint8), np.ones((3, 3), np.uint8)
        ).astype(bool)
        white &= ~near_prior_bright
        white &= (value.astype(np.float32) - peak_near - shift) > 35
        if level < 35:
            # SpaceLens night-flight clock occupies the bottom strip.
            white[max(0, int(h * 0.90) - y1) :, :] = False
        mask = (warm | white).astype(np.uint8)
        if not np.any(mask):
            self.peak_value = np.maximum(
                self.peak_value, value.astype(np.float32) - shift
            )
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        areas = stats[:, cv2.CC_STAT_AREA].copy()
        areas[0] = 0
        warm_counts = np.bincount(labels.ravel(), weights=warm.ravel(), minlength=n)
        white_counts = np.bincount(labels.ravel(), weights=white.ravel(), minlength=n)
        widths = stats[:, cv2.CC_STAT_WIDTH]
        heights = stats[:, cv2.CC_STAT_HEIGHT]
        compact = (widths <= 6 * np.maximum(1, heights)) & (
            heights <= 6 * np.maximum(1, widths)
        )
        valid = (
            compact
            & (areas >= 5)
            & ((warm_counts >= 3) | (white_counts >= (10 if level < 35 else 4)))
        )
        # Motion creates paired bright/dark edges. An emitter adds local light.
        positive = cv2.integral(np.maximum(delta - 10, 0).astype(np.float64))
        negative = cv2.integral(np.maximum(-delta - 10, 0).astype(np.float64))
        px1 = np.maximum(0, stats[:, cv2.CC_STAT_LEFT] - 5)
        py1 = np.maximum(0, stats[:, cv2.CC_STAT_TOP] - 5)
        px2 = np.minimum(
            mask.shape[1], stats[:, cv2.CC_STAT_LEFT] + stats[:, cv2.CC_STAT_WIDTH] + 5
        )
        py2 = np.minimum(
            mask.shape[0], stats[:, cv2.CC_STAT_TOP] + stats[:, cv2.CC_STAT_HEIGHT] + 5
        )
        gain = (
            positive[py2, px2]
            - positive[py1, px2]
            - positive[py2, px1]
            + positive[py1, px1]
        )
        loss = (
            negative[py2, px2]
            - negative[py1, px2]
            - negative[py2, px1]
            + negative[py1, px1]
        )
        valid &= gain > 2 * loss + 40
        interior = (
            (stats[:, cv2.CC_STAT_LEFT] > 0)
            & (stats[:, cv2.CC_STAT_TOP] > 0)
            & (stats[:, cv2.CC_STAT_LEFT] + stats[:, cv2.CC_STAT_WIDTH] < mask.shape[1])
            & (stats[:, cv2.CC_STAT_TOP] + stats[:, cv2.CC_STAT_HEIGHT] < mask.shape[0])
        )
        valid &= (warm_counts >= 3) | interior
        index = int(np.argmax(np.where(valid, areas, 0)))
        score = float(areas[index] / mask.size) if valid[index] else 0.0
        baseline_return = float(np.mean(np.max(rgb_residual, axis=2) > 25)) <= 0.005
        if score < 0.0005:
            alpha = 0.02
            self.noise = (1 - alpha) * self.noise + alpha * rgb_residual
            self.reference = (1 - alpha) * self.reference + alpha * crop
            self.level_reference = (1 - alpha) * self.level_reference + alpha * level
        return {
            "score": score,
            "warm_score": float(warm_counts[index] / mask.size) if score else 0.0,
            "white_score": float(white_counts[index] / mask.size) if score else 0.0,
            "ready": True,
            "baseline_return": baseline_return,
            "component_bbox": stats[index, :4].tolist() if score else None,
        }


class EngineGlowPipeline:
    def __init__(self, roi=(0.47, 0.79, 0.55, 0.91), auto_viewport=True):
        self.roi = list(roi)
        self.auto_viewport = auto_viewport
        self.viewport_finder = MainViewport()
        self.viewport = None
        self.guard = BackgroundGuard()
        self.evidence = LocalGlowEvidence(self.roi)
        self.stabilizer = Stabilizer()

    def update(self, frame):
        h, w = frame.shape[:2]
        bounds = (
            self.viewport_finder.bounds(frame) if self.auto_viewport else (0, 0, w, h)
        )
        changed = (
            self.viewport is not None
            and max(abs(a - b) for a, b in zip(bounds, self.viewport)) > 5
        )
        if self.viewport is not None and not changed:
            bounds = self.viewport
        self.viewport = bounds
        x1, y1, x2, y2 = bounds
        view = frame[y1:y2, x1:x2]
        if changed:
            self.guard = BackgroundGuard()
        cut = self.guard.update(view) or changed
        if cut:
            self.evidence = LocalGlowEvidence(self.roi, warmup_frames=20)
            self.stabilizer = Stabilizer()
        aligned = self.stabilizer.align(view)
        if self.stabilizer.changed:
            cut = True
            self.evidence = LocalGlowEvidence(self.roi, warmup_frames=20)
        result = self.evidence.update(aligned)
        result["registration"] = self.stabilizer.registration
        result["scene_cut"] = cut
        result["active_roi"] = [
            (x1 + self.roi[0] * (x2 - x1)) / w,
            (y1 + self.roi[1] * (y2 - y1)) / h,
            (x1 + self.roi[2] * (x2 - x1)) / w,
            (y1 + self.roi[3] * (y2 - y1)) / h,
        ]
        result["viewport"] = [v / d for v, d in zip(bounds, (w, h, w, h))]
        return result


def make_engine_fsm(candidate=0.0005, strong=0.001):
    from .detector import IgnitionFSM

    return IgnitionFSM(
        candidate,
        strong,
        confirm_window=4,
        confirm_hits=3,
        min_confirm_seconds=0.2,
        defer_timeout_on_strong=True,
    )
