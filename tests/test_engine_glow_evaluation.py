import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "glow_evaluation",
    Path(__file__).resolve().parents[1] / "benchmarks/evaluate_engine_glow.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

LABELS = {
    "definition": "first engine glow",
    "missions": [
        {
            "id": "mission",
            "mission_start_global_s": 0,
            "last_no_glow_global_s": 10,
            "first_glow_global_s": 10.1,
        }
    ],
}


def event(t, action, decision=1):
    return {
        "segment": "mission",
        "global_s": t,
        "action": action,
        "decision_id": decision,
    }


def run(events):
    return module.evaluate(
        {
            "complete": True,
            "processed_images": 200,
            "processed_seconds": 20,
            "events": events,
        },
        LABELS,
    )


def test_eventual_hit_does_not_mean_timely_ignition():
    result = run([event(18, "confirmed")])
    assert result["totals"]["eventual_hits"] == 1
    assert result["missions"][0]["delay_lower_s"] == 7.9
    assert result["totals"]["timeliness_counts"]["within_2s"] == 0


def test_retraction_does_not_erase_pre_glow_false_confirmation():
    result = run(
        [event(5, "confirmed"), event(6, "retracted"), event(11, "confirmed", 2)]
    )
    assert result["totals"]["pre_glow_false_confirmations"] == 1
    assert result["totals"]["timeliness_counts"]["within_1s"] == 1


def test_retracted_confirmation_is_not_successful_hit():
    result = run([event(11, "confirmed"), event(12, "retracted")])
    assert result["totals"]["eventual_hits"] == 0


def test_incomplete_run_cannot_be_reported_as_full_validation():
    with pytest.raises(ValueError):
        module.evaluate({"complete": False}, LABELS)
