import json

import cv2
import numpy as np
import pytest

from app.core import Store
from app.engine_glow import EngineGlowPipeline, MainViewport, make_engine_fsm


def seeded(background=20):
    frame = np.full((270, 480, 3), background, np.uint8)
    engine = EngineGlowPipeline(auto_viewport=False)
    fsm = make_engine_fsm()
    for i in range(60):
        z = engine.update(frame)
        fsm.update(z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"])
    return frame, engine, fsm


def test_two_pixel_wide_ignition_is_not_removed():
    frame, engine, fsm = seeded()
    frame[228:231, 240:242] = (0, 180, 255)
    actions = []
    for i in range(60, 65):
        z = engine.update(frame)
        r = fsm.update(
            z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"]
        )
        if r.action:
            actions.append((i / 10, r.action))
    assert actions == [(6.2, "confirmed")]


@pytest.mark.parametrize("background", [20, 80])
def test_white_core_alone_is_ignition_day_and_night(background):
    frame, engine, fsm = seeded(background)
    frame[228:233, 240:242] = (255, 255, 255)
    for i in range(60, 64):
        z = engine.update(frame)
        fsm.update(z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"])
    assert fsm.confirmed


def test_night_clock_changes_do_not_count_as_glow():
    frame, engine, fsm = seeded()
    for i in range(60, 80):
        im = frame.copy()
        im[250:260, 240 + i % 3 : 248 + i % 3] = 255
        z = engine.update(im)
        r = fsm.update(
            z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"]
        )
        assert r.action != "confirmed"


def test_established_lamp_edge_jitter_does_not_trigger():
    engine = EngineGlowPipeline(auto_viewport=False)
    fsm = make_engine_fsm()
    for i in range(100):
        frame = np.full((270, 480, 3), 20, np.uint8)
        x = 240 + (i % 2)
        frame[228:235, x : x + 4] = 255
        z = engine.update(frame)
        r = fsm.update(
            z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"]
        )
        assert r.action != "confirmed"


def test_local_flash_is_not_a_camera_cut():
    frame, engine, _ = seeded()
    frame[228:237, 239:246] = 255
    assert not engine.update(frame)["scene_cut"]


def test_broad_scene_change_invalidates_pending_evidence():
    frame, engine, _ = seeded()
    frame[:] = 160
    z = engine.update(frame)
    assert z["scene_cut"] and not z["ready"]


def test_orange_broadcast_border_is_cropped():
    frame = np.full((270, 480, 3), 10, np.uint8)
    cv2.rectangle(frame, (12, 14), (384, 224), (0, 70, 255), 2)
    x1, y1, x2, y2 = MainViewport().bounds(frame)
    assert 12 < x1 < 20 and 14 < y1 < 22 and 375 < x2 < 384 and 215 < y2 < 224


def test_old_settings_and_commands_survive_upgrade(tmp_path):
    old = {
        "roi": [0.2, 0.3, 0.7, 0.8],
        "strong_threshold": 0.02,
        "confirm_command": "echo old",
        "commands_enabled": True,
    }
    (tmp_path / "config.json").write_text(json.dumps(old))
    store = Store(tmp_path)
    assert store.config["roi"] == old["roi"]
    assert store.config["strong_threshold"] == 0.02
    assert store.config["engine_strong_threshold"] == 0.001
    assert store.config["confirm_command"] == "echo old"
    assert store.config["detector_mode"] == "engine_glow"
    assert store.events()[0]["kind"] == "config_migrated"


def test_new_strong_evidence_is_not_cancelled_by_old_weak_timeout():
    fsm = make_engine_fsm(0.0005, 0.001)
    # Extend weak verification without strong evidence until just before timeout.
    for i in range(17):
        fsm.update(0.0006, i / 10)
    r = fsm.update(0.003, 1.7)
    assert r.action != "candidate_cancelled"
    fsm.update(0.003, 1.8)
    r = fsm.update(0.003, 1.9)
    assert r.action == "confirmed"


def test_image_cli_detects_initial_core_and_honors_retraction(tmp_path):
    import subprocess
    import sys

    folder = tmp_path / "frames"
    folder.mkdir()
    for i in range(70):
        frame = np.full((270, 480, 3), 20, np.uint8)
        if i >= 60:
            frame[228:231, 240:242] = (0, 180, 255)
        assert cv2.imwrite(str(folder / f"{i:04d}.png"), frame)
    feedback = tmp_path / "corrections.json"
    feedback.write_text(json.dumps([{"sample_index": 65}]))
    out = tmp_path / "result.json"
    subprocess.run(
        [
            sys.executable,
            "run_images.py",
            str(folder),
            "--roi",
            ".47,.79,.55,.91",
            "--corrections",
            str(feedback),
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(out.read_text())
    assert result["detector_mode"] == "engine_glow"
    assert [(x["sample_index"], x["action"]) for x in result["updates"]] == [
        (62, "confirmed"),
        (65, "retracted"),
    ]
    assert result["interval_detected"] == 0
    assert not result["valid_confirmations"]


def test_existing_lamp_changing_color_without_added_light_is_not_ignition():
    engine = EngineGlowPipeline(auto_viewport=False)
    fsm = make_engine_fsm()
    for i in range(100):
        frame = np.full((270, 480, 3), 80, np.uint8)
        frame[228:234, 240:244] = (240, 240, 240) if i < 60 else (0, 180, 255)
        z = engine.update(frame)
        r = fsm.update(
            z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"]
        )
        assert r.action != "confirmed"


def test_ignition_soon_after_camera_change_is_not_absorbed_by_reacquisition():
    _, engine, fsm = seeded()
    actions = []
    for i in range(60, 100):
        frame = np.full((270, 480, 3), 80, np.uint8)
        if i >= 85:
            frame[228:233, 240:244] = 255
        z = engine.update(frame)
        r = fsm.update(
            z["score"], i / 10, z["ready"], z["scene_cut"], z["baseline_return"]
        )
        if r.action:
            actions.append((i, r.action))
    assert actions == [(87, "confirmed")]
