#!/usr/bin/env python3
"""Audit a planned raw/replay agent cohort without launching agents or devices."""
import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

from agent_cost import cost_bounds, normalize_usage, savings_bounds, usage_from_events
from paired_navigation import SAMPLES


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def middle(values):
    return median(values) if values else None


def analyze(plan, runs, prices):
    assignments = plan["trials"]
    identities = [(r["sample"], r["repeat"], r["arm"]) for r in assignments]
    if (len(assignments) != plan["planned_trials"] or not assignments
            or len({r["id"] for r in assignments}) != len(assignments)
            or len(set(identities)) != len(assignments)
            or any(r["arm"] not in ("raw", "reused_graph") or Path(r["id"]).name != r["id"] for r in assignments)):
        raise ValueError("Invalid or duplicate planned assignments")
    price_record = json.loads(prices.read_text())
    price_hash = digest(prices)
    if price_hash != plan["price_record_sha256"]:
        raise ValueError("Price record differs from the frozen plan")
    rows, versions = [], set()
    for assignment in assignments:
        row = {k: assignment[k] for k in ("id", "sample", "repeat", "arm")}
        row.update(recorded=False, success=False, false_success=False,
                   usage=None, api_equivalent_cost=None, elapsed_seconds=None,
                   evidence_errors=[])
        directory = runs / assignment["id"]
        report = directory / "results.json"
        if not report.is_file():
            rows.append(row)
            continue
        row.update(recorded=True, report_sha256=digest(report))
        metadata = json.loads(report.read_text())["metadata"]
        version = metadata.get("codex_version")
        if isinstance(version, str) and version:
            versions.add(version)
        else:
            row["evidence_errors"].append("Missing Codex version")
        expected = {"model": plan["model"], "reasoning_effort": plan["reasoning_effort"],
                    "service_tier": plan["service_tier"], "ignore_user_config": plan["ignore_user_config"],
                    "serial": plan["serial"], "api_level": str(plan["api"]),
                    "binary_sha256": plan["candidate_sha256"], "case_id": assignment["id"],
                    "apk_sha256": plan["apk_sha256"][assignment["sample"]],
                    "sample": assignment["sample"], "arm": assignment["arm"],
                    "prompt_sha256": assignment["prompt_sha256"],
                    "graph_before": assignment["graph_before"],
                    "deadline_seconds": plan["seconds_per_trial"], "input_limit": plan["inputs_per_trial"]}
        for field, value in expected.items():
            if field not in metadata:
                row["evidence_errors"].append(f"Missing {field}")
            elif metadata[field] != value:
                raise ValueError(f"{assignment['id']}: different {field}")
        if metadata.get("price_record_sha256") not in (None, price_hash):
            raise ValueError(f"{assignment['id']}: different price record")
        prompt = directory / "trial/PROMPT.md"
        if not prompt.is_file() or digest(prompt) != assignment["prompt_sha256"]:
            row["evidence_errors"].append("Missing or changed prompt evidence")
        for name, expected_hash in plan["sources"][assignment["sample"]]["files"].items():
            source = directory / "trial/app-source" / name
            if not source.is_file() or digest(source) != expected_hash:
                row["evidence_errors"].append(f"Missing or changed app source: {name}")
        result = metadata.get("agent_result") or {}
        answer = result.get("answer") or {}
        reported = isinstance(answer, dict) and answer.get("outcome") == "completed"
        reached = result.get("oracle_reached_target")
        inputs, elapsed = result.get("input_actions"), result.get("elapsed_seconds")
        complete = result.get("exit_code") == 0 and result.get("timed_out") is False
        row.update(false_success=reported and reached is False,
                   oracle_reached_target=reached, graph_unchanged=metadata.get("graph_unchanged"),
                   elapsed_seconds=elapsed, input_actions=inputs,
                   timed_out=result.get("timed_out"), answer=answer)
        row["success"] = bool(complete and reported and reached is True
                              and metadata.get("graph_unchanged") is True
                              and result.get("answer_error") is None and result.get("oracle_error") is None
                              and type(inputs) is int and 0 <= inputs <= plan["inputs_per_trial"]
                              and type(elapsed) in (int, float) and 0 < elapsed <= plan["seconds_per_trial"]
                              and not row["evidence_errors"])
        events = directory / "trial/events.jsonl"
        if complete and events.is_file():
            row["events_sha256"] = digest(events)
            usage = usage_from_events(events)
            if usage != normalize_usage(result.get("usage")):
                row["evidence_errors"].append("Usage differs from original events")
            elif usage is not None:
                row["usage"] = usage
                row["api_equivalent_cost"] = cost_bounds(usage, price_record, plan["model"], plan["service_tier"])
        rows.append(row)
    if len(versions) > 1:
        raise ValueError("Cannot combine different Codex versions")
    groups = []
    for sample in sorted({r["sample"] for r in assignments}):
        trials = [r for r in rows if r["sample"] == sample]
        group = {"sample": sample, "arms": {}, "pairs": []}
        for arm in ("raw", "reused_graph"):
            arm_rows = [r for r in trials if r["arm"] == arm]
            good = [r for r in arm_rows if r["success"]]
            group["arms"][arm] = {"planned": len(arm_rows), "recorded": sum(r["recorded"] for r in arm_rows),
                                  "successes": len(good), "complete_usage": sum(r["usage"] is not None for r in arm_rows),
                                  "median_successful_seconds": middle([r["elapsed_seconds"] for r in good]),
                                  "median_successful_total_tokens": middle([r["usage"]["input_tokens"] + r["usage"]["output_tokens"]
                                                                           for r in good if r["usage"] is not None])}
        lookup = {(r["repeat"], r["arm"]): r for r in trials}
        for repeat in sorted({r["repeat"] for r in trials}):
            control, candidate = lookup.get((repeat, "raw")), lookup.get((repeat, "reused_graph"))
            if not control or not candidate or not control["success"] or not candidate["success"]:
                continue
            saving = savings_bounds(control["api_equivalent_cost"], candidate["api_equivalent_cost"])
            tokens = None
            if control["usage"] is not None and candidate["usage"] is not None:
                total = lambda r: r["usage"]["input_tokens"] + r["usage"]["output_tokens"]
                tokens = total(control) - total(candidate)
            group["pairs"].append({"repeat": repeat,
                                   "seconds_saved": control["elapsed_seconds"] - candidate["elapsed_seconds"],
                                   "total_tokens_saved": tokens, "api_cost_saving": saving})
        group["median_seconds_saved"] = middle([p["seconds_saved"] for p in group["pairs"]])
        group["median_tokens_saved"] = middle([p["total_tokens_saved"] for p in group["pairs"] if p["total_tokens_saved"] is not None])
        for bound in ("min", "max"):
            field = f"api_equivalent_usd_saved_{bound}"
            group[f"median_{field}"] = middle([p["api_cost_saving"][field] for p in group["pairs"] if p["api_cost_saving"] is not None])
        groups.append(group)
    correct = all(r["success"] for r in rows)
    usage_complete = all(r["usage"] is not None and not r["evidence_errors"] for r in rows)
    costs_complete = all(r["api_equivalent_cost"] is not None for r in rows)
    paired = all(len(g["pairs"]) == g["arms"]["raw"]["planned"] == g["arms"]["reused_graph"]["planned"] for g in groups)
    eligible = (set(identities) == {(sample, repeat, arm) for sample in SAMPLES
                                  for repeat in (1, 2, 3) for arm in ("raw", "reused_graph")}
                and plan["api"] == 36 and plan["seconds_per_trial"] == 180 and plan["inputs_per_trial"] == 32)
    return {"planned": len(rows), "recorded": sum(r["recorded"] for r in rows),
            "successes": sum(r["success"] for r in rows),
            "confirmed_false_successes": sum(r["false_success"] for r in rows),
            "gates": {"registered_matrix_complete": eligible, "all_correct": correct, "complete_usage": usage_complete,
                      "lower_median_tokens_every_app": eligible and correct and paired and usage_complete and all(g["median_tokens_saved"] > 0 for g in groups),
                      "lower_median_time_every_app": eligible and correct and paired and all(g["median_seconds_saved"] > 0 for g in groups),
                      "lower_median_cost_even_at_bounds_every_app": eligible and correct and paired and usage_complete and costs_complete
                      and all(g["median_api_equivalent_usd_saved_min"] > 0 for g in groups)},
            "groups": groups, "trials": rows, "price_record": price_record,
            "price_record_sha256": price_hash, "subscription_charge": None,
            "codex_version": next(iter(versions), None),
            "limits": ["API-equivalent bounds are conditional price scenarios, not subscription charges.",
                       "Medians use successful trials with complete evidence; missing and failed assignments remain in all gates.",
                       "Cached input and reasoning are subsets of input and output, never extra tokens."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    parser.add_argument("--prices", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = analyze(json.loads(args.plan.read_text()), args.runs, args.prices)
    result["plan_sha256"] = digest(args.plan)
    with args.output.open("x") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: result[key] for key in ("planned", "recorded", "successes", "gates")}))


if __name__ == "__main__":
    main()
