"""区间命中、短闪光纠错、确认撤销及遮挡保护等行为验证。"""

import unittest
from app.detector import IgnitionFSM


class StateBehavior(unittest.TestCase):
    def stream(self, scores, **kwargs):
        f = IgnitionFSM(high=0.1, strong=0.5)
        return f, [f.update(s, i / 10, **kwargs) for i, s in enumerate(scores)]

    def test_single_flash_not_confirmed(self):
        _, r = self.stream([0, 0, 0.9, 0, 0, 0, 0, 0, 0, 0])
        self.assertFalse(any(x.action == "confirmed" for x in r))

    def test_old_three_frame_flash_cancelled(self):
        _, r = self.stream([0.9, 0.9, 0.9, 0, 0, 0, 0, 0, 0, 0])
        self.assertFalse(any(x.action == "confirmed" for x in r))
        self.assertTrue(any(x.action == "candidate_cancelled" for x in r))

    def test_confirmation_once_no_onset_fields(self):
        _, r = self.stream([0.9] * 30)
        self.assertEqual(sum(x.action == "confirmed" for x in r), 1)
        self.assertEqual(r[-1].event, 1)
        self.assertFalse(hasattr(r[-1], "onset_timestamp"))

    def test_returns_to_background_retracts_linked_decision(self):
        f, r = self.stream([0.9] * 10)
        confirmed = next(x for x in r if x.action == "confirmed")
        tail = [f.update(0, 1 + i / 10, baseline_return=True) for i in range(9)]
        revoked = next(x for x in tail if x.action == "retracted")
        self.assertEqual(revoked.decision_id, confirmed.decision_id)
        self.assertEqual(tail[-1].event, 0)

    def test_smoke_or_departure_does_not_retract(self):
        f, _ = self.stream([0.9] * 10)
        tail = [f.update(0, 1 + i / 10, baseline_return=False) for i in range(30)]
        self.assertTrue(all(x.event == 1 for x in tail))
        self.assertFalse(any(x.action == "retracted" for x in tail))

    def test_background_return_long_after_launch_is_not_automatic_retraction(self):
        f, _ = self.stream([0.9] * 50)
        tail = [f.update(0, 5 + i / 10, baseline_return=True) for i in range(10)]
        self.assertTrue(all(x.event == 1 for x in tail))

    def test_explicit_false_alarm_feedback_retracts(self):
        f, r = self.stream([0.9] * 50)
        decision = r[-1].decision_id
        result = f.update(0.9, 5, false_alarm=True)
        self.assertEqual(result.action, "retracted")
        self.assertEqual(result.event, 0)
        self.assertEqual(result.decision_id, decision)

    def test_late_event_still_hits_interval(self):
        _, r = self.stream([0] * 300 + [0.9] * 12)
        self.assertTrue(any(x.action == "confirmed" for x in r))

    def test_no_confirmation_across_gap_or_cut(self):
        f, _ = self.stream([0.9] * 7)
        self.assertIsNone(f.update(0.9, 2).event)
        self.assertNotEqual(f.update(0.9, 2.1).action, "confirmed")
        self.assertIsNone(f.update(0.9, 2.2, scene_cut=True).event)

    def test_unknown_input_not_negative_evidence(self):
        f, _ = self.stream([0.9] * 10)
        result = f.update(0, 1, ready=False)
        self.assertIsNone(result.event)
        self.assertNotEqual(result.action, "retracted")

    def test_nonmonotonic_timestamps_rejected(self):
        f = IgnitionFSM()
        f.update(0, 1)
        with self.assertRaises(ValueError):
            f.update(0, 1)

    def test_retracted_confirmation_does_not_count_as_interval_hit(self):
        import json, pathlib, subprocess, sys, tempfile
        import cv2, numpy as np

        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            frames = root / "frames"
            frames.mkdir()
            for i in range(36):
                image = np.zeros((128, 128, 3), dtype=np.uint8)
                if 12 <= i < 21:
                    image[44:84, 44:84] = (0, 180, 255)
                cv2.imwrite(str(frames / f"{i:04d}.png"), image)
            out = root / "result.json"
            subprocess.run(
                [
                    sys.executable,
                    str(
                        pathlib.Path(__file__).resolve().parent.parent / "run_images.py"
                    ),
                    str(frames),
                    "--detector-mode",
                    "legacy",
                    "--roi",
                    "0.25,0.25,0.75,0.75",
                    "--out",
                    str(out),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(out.read_text())
            self.assertEqual(result["interval_detected"], 0)
            self.assertEqual(result["valid_confirmations"], [])
            actions = [x["action"] for x in result["updates"]]
            self.assertIn("confirmed", actions)
            self.assertIn("retracted", actions)

    def test_slow_lighting_change_is_absorbed_before_false_fire(self):
        import numpy as np
        from app.detector import ImageEvidence

        ev = ImageEvidence([0.25, 0.25, 0.75, 0.75], adapt_alpha=0.02)
        image = np.full((128, 128, 3), 40, dtype=np.uint8)
        image[44:52, 44:52] = (0, 50, 150)
        for _ in range(8):
            ev.update(image)
        scores = []
        for i in range(200):
            image[44:52, 44:52] = (0, 50, 150 + i // 5)
            scores.append(ev.update(image)["novel_score"])
        self.assertEqual(max(scores), 0)

    def test_strong_fire_is_not_learned_as_background(self):
        import numpy as np
        from app.detector import ImageEvidence

        ev = ImageEvidence([0.25, 0.25, 0.75, 0.75], adapt_alpha=0.02)
        image = np.full((128, 128, 3), 40, dtype=np.uint8)
        for _ in range(8):
            ev.update(image)
        image[44:84, 44:84] = (0, 180, 255)
        for _ in range(30):
            self.assertGreater(ev.update(image)["novel_score"], 0.01)
        self.assertEqual(float(ev.reference[64, 64]), 40)

    def test_camera_translation_reinitialization_signal(self):
        import cv2, numpy as np

        from app.camera_guard import CameraGuard

        image = np.zeros((180, 320, 3), dtype=np.uint8)
        for x in range(30, 240, 20):
            cv2.rectangle(image, (x, 30), (x + 9, 90), (255, 255, 255), 1)
        guard = CameraGuard()
        self.assertFalse(guard.update(image))
        self.assertFalse(guard.update(image))
        moved = cv2.warpAffine(image, np.float32([[1, 0, 4], [0, 1, 0]]), (320, 180))
        self.assertTrue(guard.update(moved))
        self.assertFalse(guard.update(moved))


if __name__ == "__main__":
    unittest.main()
