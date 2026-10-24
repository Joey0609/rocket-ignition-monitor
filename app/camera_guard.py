"""传统边缘相位相关：相机平移时重建同机位背景。"""

import cv2
import numpy as np


class CameraGuard:
    def __init__(self):
        self.reference = None

    def update(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        hh, ww = gray.shape
        crop = gray[int(hh * 0.12) : int(hh * 0.58), int(ww * 0.06) : int(ww * 0.78)]
        edge = cv2.Canny(crop, 40, 100).astype(np.float32)
        if self.reference is None:
            self.reference = edge
            return False
        if np.count_nonzero(edge) < 50 or np.count_nonzero(self.reference) < 50:
            return False
        shift, response = cv2.phaseCorrelate(self.reference, edge)
        changed = response > 0.25 and (abs(shift[0]) > 2.5 or abs(shift[1]) > 2.5)
        if changed:
            self.reference = edge
        return changed
