#!/usr/bin/env python3
"""Summarize frozen replay cohorts without pooling profiles, pilots, or binaries."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from statistics import median

from paired_navigation import SAMPLES

ARMS = ("raw", "baseline", "candidate")


def successful(row):
    return (row.get("setup_passed") is True and row.get("reported_ok") is True
            and row.get("oracle_reached_target") is True and row.get("graph_unchanged") is True
            and row.get("error") is None and row.get("oracle_error") is None)


def summarize(paths):
    runs = [json.loads(path.read_text()) for path in paths]
    if not runs:
        raise ValueError("Provide at least one report")
    for field in ("suite", "stage", "profiled", "protocol_sha256", "runner_sha256", "binary_sha256_by_arm", "repetitions", "settle_override"):
        if any(run["metadata"].get(field) != runs[0]["metadata"].get(field) for run in runs):
            raise ValueError(f"Cannot combine different {field}")
    metadata = runs[0]["metadata"]
    if set(metadata["binary_sha256_by_arm"]) != {"baseline", "candidate"}:
        raise ValueError("Comparison requires both frozen binaries")
    for sample in SAMPLES:
        hashes = {run["metadata"].get("apks", {}).get(sample) for run in runs} - {None}
        if len(hashes) > 1:
            raise ValueError("Cannot combine different sample APKs")
    rows, seen, groups = [], set(), defaultdict(list)
    for run in runs:
        api = run["metadata"]["api_level"]
        for row in run["trials"]:
            key = (row["api"], row["sample"], row["repeat"], row["arm"])
            if key in seen:
                raise ValueError("Duplicate trial assignment")
            if row["api"] != api or row["sample"] not in SAMPLES or row["arm"] not in ARMS:
                raise ValueError("Unexpected trial assignment")
            if not 1 <= row["repeat"] <= metadata["repetitions"]:
                raise ValueError("Unexpected repeat")
            seen.add(key)
            rows.append(row)
            groups[(api, row["sample"])].append(row)
    summaries = []
    for (api, sample), trials in sorted(groups.items()):
        group = {"api": api, "sample": sample, "arms": {}, "paired": {}}
        lookup = {(row["repeat"], row["arm"]): row for row in trials}
        for arm in ARMS:
            assigned = [row for row in trials if row["arm"] == arm]
            good = [row["seconds"] for row in assigned if successful(row)]
            group["arms"][arm] = {
                "planned": metadata["repetitions"], "recorded": len(assigned), "successes": len(good),
                "failures": len(assigned) - len(good), "missing": metadata["repetitions"] - len(assigned),
                "median_successful_seconds": median(good) if good else None,
                "range_successful_seconds": [min(good), max(good)] if good else None,
            }
        for control in ("raw", "baseline"):
            pairs = []
            for repeat in range(1, metadata["repetitions"] + 1):
                a, b = lookup.get((repeat, control)), lookup.get((repeat, "candidate"))
                if a and b and successful(a) and successful(b):
                    pairs.append({"repeat": repeat, "seconds": b["seconds"] - a["seconds"],
                                  "percent": 100 * (b["seconds"] / a["seconds"] - 1)})
            group["paired"][control] = {
                "pairs": pairs, "blocks": len(pairs),
                "median_seconds": median(p["seconds"] for p in pairs) if pairs else None,
                "median_percent": median(p["percent"] for p in pairs) if pairs else None,
            }
        summaries.append(group)
    planned = sum(run["metadata"]["planned_trials"] for run in runs)
    complete = len(rows) == planned
    quality = complete and all(successful(row) for row in rows)
    full_matrix = ({(group["api"], group["sample"]) for group in summaries}
                   == {(api, sample) for api in ("36", "37") for sample in SAMPLES})
    faster = quality and all(group["paired"][control]["blocks"] == metadata["repetitions"]
                             and group["paired"][control]["median_seconds"] < 0
                             for group in summaries for control in ("raw", "baseline"))
    eligible = metadata["stage"] == "confirmation" and metadata["repetitions"] == 10 and full_matrix and not metadata["profiled"]
    return {
        "stage": metadata["stage"], "profiled": metadata["profiled"],
        "timing_scope": metadata["timing_scope"], "planned_trials": planned,
        "recorded_trials": len(rows), "successes": sum(successful(row) for row in rows),
        "confirmed_false_successes": sum(row["reported_ok"] and row["oracle_reached_target"] is False for row in rows),
        "unverified_successes": sum(row["reported_ok"] and row["oracle_reached_target"] is None for row in rows),
        "gates": {"complete_assignments": complete, "all_correct_and_graphs_unchanged": quality,
                  "lower_paired_median_than_both_controls_every_case": faster,
                  "confirmation_eligible": eligible, "speed_goal_met": eligible and faster},
        "model_usage": None, "api_cost_saving": None, "groups": summaries, "trials": rows,
        "inputs": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "metadata": run["metadata"]} for path, run in zip(paths, runs)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = summarize(args.reports)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("recorded_trials", "successes", "gates")}))


if __name__ == "__main__":
    main()
