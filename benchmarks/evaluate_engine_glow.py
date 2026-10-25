"""Rescore a complete run against first visible engine glow, never liftoff plume."""

import argparse, csv, gzip, json
from pathlib import Path


def evaluate(summary, labels, predictions=None):
    if not summary.get("complete"):
        raise ValueError("Cannot evaluate an incomplete run")
    results = []
    for m in labels["missions"]:
        events = [e for e in summary["events"] if e["segment"] == m["id"]]
        confirmations = [e for e in events if e["action"] == "confirmed"]
        retracted = {e["decision_id"] for e in events if e["action"] == "retracted"}
        before = [
            e for e in confirmations if e["global_s"] <= m["last_no_glow_global_s"]
        ]
        valid = [
            e
            for e in confirmations
            if e["global_s"] >= m["first_glow_global_s"]
            and e["decision_id"] not in retracted
        ]
        first = min(valid, key=lambda e: e["global_s"]) if valid else None
        results.append(
            {
                **m,
                "confirmed_global_s": first["global_s"] if first else None,
                "delay_lower_s": round(first["global_s"] - m["first_glow_global_s"], 3)
                if first
                else None,
                "delay_upper_s": round(
                    first["global_s"] - m["last_no_glow_global_s"], 3
                )
                if first
                else None,
                "pre_glow_false_confirmations": len(before),
                "eventual_hit": bool(first),
                "pre_glow_unknown_images": 0,
                "pre_glow_images": 0,
            }
        )
    if predictions:
        by_id = {r["id"]: r for r in results}
        with gzip.open(predictions, "rt") as f:
            for row in csv.DictReader(f):
                r = by_id[row["segment"]]
                if float(row["global_s"]) <= r["last_no_glow_global_s"]:
                    r["pre_glow_images"] += 1
                    r["pre_glow_unknown_images"] += row["event"] in ("", "None")
    totals = {
        "processed_images": summary["processed_images"],
        "video_seconds": summary["processed_seconds"],
        "pre_glow_seconds": round(
            sum(
                r["last_no_glow_global_s"] - r["mission_start_global_s"]
                for r in results
            ),
            3,
        ),
        "pre_glow_false_confirmations": sum(
            r["pre_glow_false_confirmations"] for r in results
        ),
        "eventual_hits": sum(r["eventual_hit"] for r in results),
        "missions": len(results),
        "pre_glow_images": sum(r["pre_glow_images"] for r in results),
        "pre_glow_unknown_images": sum(r["pre_glow_unknown_images"] for r in results),
        "timeliness_counts": {
            f"within_{limit}s": sum(
                r["delay_upper_s"] is not None and r["delay_upper_s"] <= limit
                for r in results
            )
            for limit in (1, 2, 5)
        },
    }
    return {
        "definition": labels["definition"],
        "totals": totals,
        "missions": results,
        "latency_scope": "Decoded video-time confirmation delay. Does not include RTMP transport, decoder buffering, WebSocket delivery, or command execution.",
        "verdict": "Eventual interval hits do not establish timely engine-ignition detection.",
        "limitations": [
            "Labels are single-reviewer visual observations, not hardware ignition timestamps.",
            "The same videos were used to tune the detector rules, so this is not blind validation.",
            "1s/2s/5s are reporting bands, not a user-approved service-level objective.",
            "Every pre-glow confirmation counts as a false alert even if later retracted.",
        ],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--summary",
        type=Path,
        default=Path(__file__).with_name("reference_summary.json"),
    )
    p.add_argument(
        "--labels",
        type=Path,
        default=Path(__file__).with_name("engine_glow_labels.json"),
    )
    p.add_argument("--predictions", type=Path)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    result = evaluate(
        json.loads(a.summary.read_text()),
        json.loads(a.labels.read_text()),
        a.predictions,
    )
    a.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result["totals"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
