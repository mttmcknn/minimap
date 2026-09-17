#!/usr/bin/env python3
"""Consolidate paired device runs without pooling unlike tasks or dropping failures."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics

from paired_navigation import ARMS, SAMPLES


def median(values):
    return statistics.median(values) if values else None


def break_even(raw, first, reuse):
    if any(value is None for value in (raw, first, reuse)):
        return None
    if raw <= reuse:
        return None
    if first <= raw:
        return 1
    return max(1, math.ceil((first - reuse) / (raw - reuse)))


def bootstrap(values):
    if not values:
        return None
    rng = random.Random(20260916)
    samples = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(2000))
    return [samples[49], samples[1949]]


def analyze(paths, repetitions=5):
    assert repetitions > 0 and paths, "Provide reports and a positive repetition count"
    data = [json.loads(path.read_text()) for path in paths]
    hashes = {run["metadata"]["binary_sha256"] for run in data}
    assert len(hashes) == 1, "Do not combine different product binaries"
    suites = {run["metadata"].get("suite", "controlled-v1") for run in data}
    assert len(suites) == 1, "Do not combine different fixture protocols"
    apis = sorted({run["metadata"]["api_level"] for run in data})
    for run in data:
        assert all(row["api"] == run["metadata"]["api_level"] for row in run["trials"]), "Trial API disagrees with its worker"
    for sample in SAMPLES:
        apks = {run["metadata"].get("apks", {}).get(sample) for run in data} - {None}
        assert len(apks) <= 1, "Do not combine different sample APKs"
    rows = [row for run in data for row in run["trials"]]
    keys = [(row["api"], row["sample"], row["repeat"], row["arm"]) for row in rows]
    assert len(keys) == len(set(keys)), "Duplicate trial assignment"
    assignments = {(api, sample, repeat, arm) for api in apis for sample in SAMPLES
                   for repeat in range(1, repetitions + 1) for arm in ARMS}
    assert set(keys) <= assignments, "Unexpected trial assignment"
    by_key = dict(zip(keys, rows))
    groups = []
    for api in apis:
        for sample in SAMPLES:
            group = {"api": api, "sample": sample, "arms": {}, "paired_reuse_minus_raw": {}}
            for arm in ARMS:
                trials = [row for row in rows if row["api"] == api and row["sample"] == sample and row["arm"] == arm]
                success = [row for row in trials if row["reported_ok"] and row["oracle_reached_target"] and row["error"] is None]
                started = [row for row in trials if row["setup_passed"]]
                group["arms"][arm] = {
                    "planned": repetitions, "recorded": len(trials), "successes": len(success),
                    "failures": len(trials) - len(success), "missing": max(0, repetitions - len(trials)),
                    "setup_failures": len(trials) - len(started), "started": len(started),
                    "confirmed_false_successes": sum(row["reported_ok"] and not row["oracle_reached_target"] and row["error"] is None for row in trials),
                    "unverified_successes": sum(row["reported_ok"] and row["error"] is not None for row in trials),
                    "median_seconds_started": median([row["seconds"] for row in started]),
                    "median_seconds_successful": median([row["seconds"] for row in success]),
                    "range_seconds_started": [min(row["seconds"] for row in started), max(row["seconds"] for row in started)] if started else None,
                    "median_stdout_bytes": median([row["stdout_bytes"] for row in started]),
                    "median_commands": median([row["commands"] for row in started]),
                    "total_seconds_including_setup_oracle": sum(row["total_seconds_including_setup_oracle"] for row in trials),
                    "median_setup_seconds": median([row["setup"]["seconds"] for row in trials if "setup" in row]),
                    "median_oracle_seconds": median([row["oracle"]["seconds"] for row in started if "oracle" in row]),
                    "median_graph_bytes": median([row["graph"]["bytes"] for row in success if "graph" in row]),
                    "trial_errors": [row["error"] for row in trials if row["error"]],
                }
            paired = []
            for repeat in range(1, repetitions + 1):
                raw = by_key.get((api, sample, repeat, "raw"))
                reused = by_key.get((api, sample, repeat, "reused_graph"))
                if raw and reused and raw["setup_passed"] and reused["setup_passed"]:
                    paired.append((raw, reused))
            for metric in ["seconds", "stdout_bytes", "commands"]:
                differences = [reused[metric] - raw[metric] for raw, reused in paired]
                group["paired_reuse_minus_raw"][metric] = {"blocks": len(differences), "differences": differences,
                                                            "median": median(differences), "bootstrap_median_95_interval": bootstrap(differences)}
            a = group["arms"]
            raw_bytes = a["raw"]["median_stdout_bytes"]
            reused_bytes = a["reused_graph"]["median_stdout_bytes"]
            group["response_bytes_reduction_percent"] = 100 * (1 - reused_bytes / raw_bytes) if raw_bytes and reused_bytes is not None else None
            group["break_even_total_uses"] = {
                metric: break_even(*(a[arm][field] for arm in ARMS))
                for metric, field in [("wall_seconds", "median_seconds_started"), ("response_bytes", "median_stdout_bytes")]
            }
            group["break_even_eligible"] = all(a[arm]["successes"] == repetitions for arm in ARMS)
            if not group["break_even_eligible"]:
                group["break_even_total_uses"] = {"wall_seconds": None, "response_bytes": None}
            groups.append(group)
    expected = len(assignments)
    successes = {arm: sum(row["reported_ok"] and row["oracle_reached_target"] and row["error"] is None for row in rows if row["arm"] == arm) for arm in ARMS}
    planned_per_arm = expected // len(ARMS)
    complete = set(keys) == assignments
    false_success = sum(g["arms"][arm]["confirmed_false_successes"] for g in groups for arm in ARMS)
    unverified_successes = sum(g["arms"][arm]["unverified_successes"] for g in groups for arm in ARMS)
    return {
        "suite": next(iter(suites)), "binary_sha256": next(iter(hashes)), "planned_device_trials": expected,
        "recorded_device_trials": len(rows), "successful_device_trials": sum(successes.values()),
        "successes_by_arm": successes, "planned_per_arm": planned_per_arm,
        "confirmed_false_successes": false_success, "unverified_successes": unverified_successes,
        "missing_assignments": [dict(zip(["api", "sample", "repeat", "arm"], key)) for key in sorted(assignments - set(keys))],
        "input_reports": [{"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "metadata": run["metadata"]} for path, run in zip(paths, data)],
        "groups": groups, "trials": rows, "usage": None,
        "gates": {"complete_assignment": complete, "zero_false_successes": complete and false_success == 0 and unverified_successes == 0,
                  "at_least_95_percent_each_arm": complete and all(n / planned_per_arm >= .95 for n in successes.values()),
                  "reuse_correctness_not_lower_than_raw": complete and successes["reused_graph"] >= successes["raw"],
                  "at_least_50_percent_fewer_response_bytes_each_task": complete and all(g["response_bytes_reduction_percent"] is not None and g["response_bytes_reduction_percent"] >= 50 for g in groups),
                  "agent_token_reduction": None},
        "limits": ["Scripted routes are known in every arm; model discovery/diagnosis costs are absent.",
                   "Unstarted trials stay in correctness denominators; zero navigation time from a failed setup is excluded from latency/byte medians and paired cost comparisons.",
                   "Bootstrap intervals are descriptive with five matched blocks per task/device, not p95 or an SLA.",
                   "No cost break-even is reported when a task has failures or missing trials.",
                   "Oracle transport failures are unverified outcomes, not confirmed false-success evidence."]}


def markdown(report):
    lines = ["# Controlled device results", "", f"{report['successful_device_trials']}/{report['planned_device_trials']} planned trials succeeded; {report['recorded_device_trials']} recorded.", "",
             "Each cell is median navigation seconds among trials with a verified start; setup failures remain in correctness denominators. Initialization is included in first use, and every arm verifies the requested destination.", "",
             "| App | API | Raw control | First use | Reused graph | Reuse bytes reduction | Time break-even |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for group in report["groups"]:
        times = [group["arms"][arm]["median_seconds_started"] for arm in ARMS]
        cells = [f"{value:.3f}" if value is not None else "unmeasured" for value in times]
        reduction = group["response_bytes_reduction_percent"]
        b = group["break_even_total_uses"]["wall_seconds"]
        lines.append(f"| {group['sample']} | {group['api']} | {' | '.join(cells)} | {reduction:.1f}% | {b if b is not None else 'none demonstrated'} |" if reduction is not None else f"| {group['sample']} | {group['api']} | {' | '.join(cells)} | unmeasured | unmeasured |")
    lines.extend(["", "Gates:", ""])
    for gate, value in report["gates"].items():
        lines.append(f"- {gate}: {'unmeasured' if value is None else 'pass' if value else 'fail'}")
    lines.extend(["", *report["limits"], ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    result = analyze([path.resolve() for path in args.reports], args.repetitions)
    args.output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(result))
    print(json.dumps(result["gates"]))


if __name__ == "__main__":
    main()
