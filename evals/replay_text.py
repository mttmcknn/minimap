#!/usr/bin/env python3
"""Audit saved replay output tokens; these are not model usage or billed costs."""
import argparse
from importlib.metadata import version
import json
from pathlib import Path
from statistics import median

from analyze_replay import ARMS, successful
from benchmark_data import ENCODINGS, audit_calls, load_verified, phase_metrics, require, sha256


def collect(summary, raw_root, encoders, skill):
    calls, trials, sources = [], [], []
    for source in summary["inputs"]:
        original = Path(source["path"])
        path = raw_root / original.parent.name / original.name if raw_root else original
        run = load_verified(path, source["sha256"])
        require(run["metadata"] == source["metadata"], "Replay metadata mismatch")
        name = f"{path.parent.name}/{path.name}"
        counted = audit_calls(path, run, encoders, name)
        calls.extend(counted)
        sources.append({"path": name, "sha256": source["sha256"]})
        for row in run["trials"]:
            require(row in summary["trials"], "Trial missing from replay summary")
            trials.append({**{key: row[key] for key in ("api", "sample", "repeat", "arm", "phase")},
                           "success": successful(row), "source": name,
                           **phase_metrics(counted, row["phase"], row)})
    keys = [(row["api"], row["sample"], row["repeat"], row["arm"]) for row in trials]
    require(len(keys) == len(set(keys)) == summary["recorded_trials"], "Duplicate or missing trial")
    groups = []
    for group in summary["groups"]:
        rows = [row for row in trials if row["api"] == group["api"] and row["sample"] == group["sample"]]
        result = {"api": group["api"], "sample": group["sample"], "arms": {}, "paired": []}
        for arm in ARMS:
            assigned = [row for row in rows if row["arm"] == arm]
            good = [row for row in assigned if row["success"]]
            result["arms"][arm] = {"recorded": len(assigned), "successes": len(good),
                "median_successful_tokens": {name: median(row[name] for row in good) if good else None for name in ENCODINGS}}
        lookup = {(row["repeat"], row["arm"]): row for row in rows}
        for repeat in sorted({row["repeat"] for row in rows}):
            raw, candidate = lookup.get((repeat, "raw")), lookup.get((repeat, "candidate"))
            if raw and candidate and raw["success"] and candidate["success"]:
                require(all(raw[name] > 0 for name in ENCODINGS), "Empty raw token denominator")
                result["paired"].append({"repeat": repeat,
                    "tokens_saved": {name: raw[name] - candidate[name] for name in ENCODINGS},
                    "percent_less_text": {name: 100 * (1 - candidate[name] / raw[name]) for name in ENCODINGS}})
        groups.append(result)
    skill_bytes = skill.read_bytes()
    skill_text = skill_bytes.decode("utf-8")
    return {"scope": "Each exact stdout/stderr stream counted once; navigation only, excluding setup, grader, instructions and model context replay.",
            "model_usage": None, "api_cost_saving": None,
            "planned_trials": summary["planned_trials"], "recorded_trials": len(trials),
            "successes": sum(row["success"] for row in trials),
            "skill": {"sha256": sha256(skill_bytes), "bytes": len(skill_bytes),
                      "tokens": {name: len(encoding.encode_ordinary(skill_text)) for name, encoding in encoders.items()}},
            "sources": sources, "groups": groups, "trials": trials, "calls": calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("--raw-root", type=Path, help="Root of the unpacked evidence archive")
    parser.add_argument("--skill", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    import tiktoken
    encoders = {name: tiktoken.get_encoding(name) for name in ENCODINGS}
    result = collect(json.loads(args.summary.read_text()), args.raw_root, encoders, args.skill)
    result["summary_sha256"] = sha256(args.summary.read_bytes())
    result["tiktoken_version"] = version("tiktoken")
    with args.output.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("recorded_trials", "successes", "model_usage", "api_cost_saving")}))


if __name__ == "__main__":
    main()
