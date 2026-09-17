"""Audit recorded tool output and calculate benchmark data without running a device.

Token counts describe each saved stdout/stderr string once, under a named
encoding. They are deliberately separate from unmeasured model/API usage.
This report adapter targets the controlled Compose suite, not arbitrary logs.
"""
import hashlib
from datetime import date
import json
import math
from pathlib import Path
from statistics import median

ARMS = ("raw", "new_graph", "reused_graph")
SAMPLES = ("jetsnack", "jetnews", "jetchat")
ENCODINGS = ("o200k_base", "cl100k_base")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def load_verified(path, expected_hash):
    content = path.read_bytes()
    require(sha256(content) == expected_hash, f"Source hash mismatch: {path}")
    return json.loads(content)


def trial_key(row):
    return (row["api"], row["sample"], row["repeat"], row["arm"])


def succeeded(row):
    return (row["setup_passed"] and row["reported_ok"]
            and row["oracle_reached_target"] and row["error"] is None)


def cumulative_cost(uses, raw, first, reuse, *, upfront=0, per_use=0):
    """Return raw and learned costs; the first task already reaches its target."""
    require(isinstance(uses, int) and uses >= 1, "Uses must be a positive integer")
    require(all(math.isfinite(x) and x >= 0 for x in (raw, first, reuse, upfront, per_use)),
            "Costs must be finite and nonnegative")
    return uses * raw, first + (uses - 1) * reuse + upfront + uses * per_use


def break_even(raw, first, reuse, *, upfront=0, per_use=0):
    """First integer N with learned <= raw for N and all subsequent uses.

    An early discount with a worse long-run slope is not a sustained break-even.
    Equality counts as break-even; it does not mean strictly positive savings.
    """
    cumulative_cost(1, raw, first, reuse, upfront=upfront, per_use=per_use)
    advantage = raw - reuse - per_use
    intercept = first - reuse + upfront
    if advantage < 0:
        return None
    if advantage == 0:
        return 1 if intercept <= 0 else None
    return max(1, math.ceil(intercept / advantage))


def audit_calls(report_path, run, encoders, source_name):
    """Count streams separately; preserve boundaries, whitespace, and failures."""
    calls = []
    for index, call in enumerate(run["calls"]):
        row = {"source": source_name, "index": index, "phase": call["phase"],
               "seconds": call["seconds"], "exit_code": call["exit_code"],
               "timed_out": call.get("timed_out", False)}
        for stream in ("stdout", "stderr"):
            path = report_path.parent / f"{index:03d}.{stream}"
            content = path.read_bytes()
            require(len(content) == call[f"{stream}_bytes"], f"Byte count mismatch: {path}")
            text = content.decode("utf-8", errors="strict")
            row[f"{stream}_bytes"] = len(content)
            row[f"{stream}_sha256"] = sha256(content)
            for name, encoding in encoders.items():
                row[f"{stream}_{name}"] = len(encoding.encode_ordinary(text))
        calls.append(row)
    return calls


def phase_metrics(calls, phase, expected=None):
    selected = [call for call in calls if call["phase"] == phase]
    metrics = {"commands": len(selected), "seconds": sum(c["seconds"] for c in selected)}
    for stream in ("stdout", "stderr"):
        for suffix in ("bytes", *ENCODINGS):
            key = f"{stream}_{suffix}"
            metrics[key] = sum(c[key] for c in selected)
    for name in ENCODINGS:
        metrics[name] = metrics[f"stdout_{name}"] + metrics[f"stderr_{name}"]
    if expected is not None:
        for key in ("commands", "seconds", "stdout_bytes", "stderr_bytes"):
            require(math.isclose(metrics[key], expected[key], rel_tol=1e-10, abs_tol=1e-8),
                    f"Phase {phase}: {key} does not reconcile with its recorded trial")
    return metrics


def summarize(trials, skill_tokens):
    keys = [trial_key(t) for t in trials]
    require(len(keys) == len(set(keys)), "Duplicate trial assignment")
    groups = []
    for api in sorted({t["api"] for t in trials}):
        for sample in SAMPLES:
            rows = [t for t in trials if t["api"] == api and t["sample"] == sample]
            repeats = sorted({t["repeat"] for t in rows})
            expected = {(api, sample, rep, arm) for rep in repeats for arm in ARMS}
            require(rows and {trial_key(t) for t in rows} == expected, "Incomplete matched block")
            require(all(t["success"] for t in rows), "Unverified trial cannot enter cost projections")
            by_key = {trial_key(t): t for t in rows}
            group = {"api": api, "sample": sample, "repeats": repeats, "arms": {}}
            for arm in ARMS:
                arm_rows = [t for t in rows if t["arm"] == arm]
                group["arms"][arm] = {metric: median(t[metric] for t in arm_rows)
                                      for metric in ("seconds", "commands", *ENCODINGS)}
            group["paired"] = []
            for rep in repeats:
                raw = by_key[(api, sample, rep, "raw")]
                reuse = by_key[(api, sample, rep, "reused_graph")]
                require(all(raw[n] > 0 for n in ENCODINGS), "Raw token denominator must be positive")
                group["paired"].append({"repeat": rep, "seconds": reuse["seconds"] - raw["seconds"],
                                        **{n: 100 * (1 - reuse[n] / raw[n]) for n in ENCODINGS}})
            group["projections"] = {}
            for metric in ("seconds", *ENCODINGS):
                raw, first, reuse = (group["arms"][arm][metric] for arm in ARMS)
                scenarios = {"payload_only": {}} if metric != "seconds" else {"tool_time_only": {}}
                if metric != "seconds":
                    scenarios.update({"skill_once": {"upfront": skill_tokens[metric]},
                                      "skill_every_use": {"per_use": skill_tokens[metric]}})
                group["projections"][metric] = {}
                for scenario, kwargs in scenarios.items():
                    r10, m10 = cumulative_cost(10, raw, first, reuse, **kwargs)
                    group["projections"][metric][scenario] = {
                        "break_even_total_uses": break_even(raw, first, reuse, **kwargs),
                        "raw_at_10": r10, "minimap_at_10": m10,
                        "savings_at_10": r10 - m10, "savings_percent_at_10": 100 * (1 - m10 / r10)}
            groups.append(group)
    return groups


def quality_rows(report):
    """Retain the entire original denominator, including never-started cases."""
    result = []
    for cohort, key in (("Original startup", "original_experiment"), ("Readiness wait", "primary")):
        source = report[key]
        observed = {trial_key(t): t for t in source["trials"]}
        require(len(observed) == len(source["trials"]), "Duplicate quality assignment")
        expected_keys = set()
        for group in source["groups"]:
            for arm in ARMS:
                for repeat in range(1, group["arms"][arm]["planned"] + 1):
                    key = (group["api"], group["sample"], repeat, arm)
                    require(key not in expected_keys, "Duplicate planned quality assignment")
                    expected_keys.add(key)
                    trial = observed.get(key)
                    state = ("missing" if trial is None else "passed" if succeeded(trial)
                             else "setup_failed" if not trial["setup_passed"] else "failed")
                    result.append({"cohort": cohort, "api": key[0], "sample": key[1],
                                   "repeat": repeat, "arm": arm, "state": state})
        require(set(observed) <= expected_keys, "Unexpected quality assignment")
        require(len(expected_keys) == source["planned_device_trials"], "Quality denominator mismatch")
    return result


def collect(report_path, raw_root, encoders, versions):
    report = json.loads(report_path.read_text())
    primary = report["primary"]
    old_root = Path(report["raw_artifact_directory"])
    raw_root = raw_root or old_root

    def relocated(path):
        return raw_root / Path(path).relative_to(old_root)

    sources, calls, trials, controls = [], [], [], []
    for entry in primary["input_reports"]:
        path = relocated(entry["path"])
        run = load_verified(path, entry["sha256"])
        require(run["metadata"] == entry["metadata"], "Primary metadata mismatch")
        require(run["metadata"]["binary_sha256"] == primary["binary_sha256"] == report["binary_sha256"],
                "Mixed product binaries")
        require(run["metadata"]["suite"] == primary["suite"], "Mixed fixture protocols")
        name = str(path.relative_to(raw_root))
        sources.append({"path": name, "sha256": entry["sha256"], "role": "primary"})
        counted = audit_calls(path, run, encoders, name)
        calls.extend(counted)
        api = run["metadata"]["api_level"]
        for row in run["trials"]:
            require(row["api"] == api, "API does not match worker")
            phase = f"{row['sample']}-{row['repeat']}-{row['arm']}"
            value = {k: row[k] for k in ("api", "sample", "repeat", "arm")}
            value.update({"success": succeeded(row), "source": name, "phase": phase,
                          "total_seconds_including_setup_oracle": row["total_seconds_including_setup_oracle"]})
            value.update(phase_metrics(counted, phase, row))
            for scope in ("setup", "oracle"):
                metrics = phase_metrics(counted, f"{scope}-{phase}", row[scope])
                value.update({f"{scope}_{key}": val for key, val in metrics.items()})
            require(row in primary["trials"], "Source trial not present in controlled report")
            trials.append(value)
    require(len(trials) == primary["planned_device_trials"] == len(primary["trials"]),
            "This cost chart requires a complete cohort; retain incomplete runs in the quality chart")
    require(all(t["success"] for t in trials), "Primary cohort has failed or unverified outcomes")
    for api in sorted({t["api"] for t in trials}):
        for entry in report[f"contracts_api{api}"]["runs"]:
            path = relocated(entry["output"]) / "results.json"
            run = load_verified(path, entry["results_sha256"])
            require(run["metadata"] == entry["metadata"], "Control metadata mismatch")
            require(run["metadata"]["binary_sha256"] == report["binary_sha256"], "Control binary mismatch")
            name = str(path.relative_to(raw_root))
            sources.append({"path": name, "sha256": entry["results_sha256"], "role": "control"})
            counted = audit_calls(path, run, encoders, name)
            calls.extend(counted)
            metadata = run["metadata"]
            if entry["name"] == "recovery":
                cases = [(c["scenario"], c) for c in metadata["scenarios"]]
            elif entry["name"] == "negative":
                cases = [(c["case"], c) for c in metadata["checks"]]
            elif entry["name"] == "relabel":
                cases = [("relabel", metadata["relabel"])]
            else:
                raise ValueError(f"Unknown control family: {entry['name']}")
            for phase, case in cases:
                controls.append({"api": api, "case": phase, "passed": case["passed"], "source": name,
                                 **phase_metrics(counted, phase, case)})
    for entry in report["original_experiment"]["input_reports"]:
        path = relocated(entry["path"])
        original = load_verified(path, entry["sha256"])
        require(all(t in report["original_experiment"]["trials"] for t in original["trials"]),
                "Original trial not preserved in report")
        sources.append({"path": str(path.relative_to(raw_root)), "sha256": entry["sha256"], "role": "original"})

    layer = report["agent_layer"]
    manifest_path = relocated(layer["manifest"])
    manifest = load_verified(manifest_path, layer["manifest_sha256"])
    case = next(c for c in manifest["cases"] if c["arm"] == "reused_graph")
    prompt_path = relocated(case["prepared_root"]) / "PROMPT.md"
    prompt = prompt_path.read_bytes()
    require(sha256(prompt) == case["prompt_sha256"], "Frozen skill prompt hash mismatch")
    marker = "Canonical Minimap skill:\n"
    require(prompt.decode().count(marker) == 1, "Skill boundary missing or ambiguous")
    skill = prompt.decode().split(marker)[1]
    skill_tokens = {n: len(e.encode_ordinary(skill)) for n, e in encoders.items()}
    trials.sort(key=trial_key)
    return {
        "schema_version": 1, "analysis_date": date.today().isoformat(), "experiment_date": report["date"],
        "suite": primary["suite"], "binary_sha256": report["binary_sha256"],
        "compose_samples_revision": report["compose_samples_revision"],
        "controlled_report_sha256": sha256(report_path.read_bytes()),
        "controlled_report": report_path.name, "original_raw_root": str(old_root),
        "tokenization": {"versions": versions, "encodings": list(encoders),
                         "method": "UTF-8 strict; encode_ordinary each stdout/stderr independently; sum once per selected phase",
                         "model_mapping": None, "model_usage": None},
        "skill": {"tokens": skill_tokens, "sha256": sha256(skill.encode()),
                  "prompt_sha256": case["prompt_sha256"], "manifest_sha256": layer["manifest_sha256"],
                  "source": str(prompt_path.relative_to(raw_root)), "marker": marker,
                  "scope": "Full frozen skill suffix, including trailing whitespace; hypothetical text load, not an executed agent turn"},
        "sources": sources, "trials": trials, "groups": summarize(trials, skill_tokens),
        "calls": calls, "controls": controls, "quality": quality_rows(report),
        "team_git": report["team_git"], "graph_audit": report["graph_audit"],
        "retained_preflight": report["retained_preflight"],
        "agent_layer": {"planned": layer["prepared_trials"], "started": layer["trials_started"], "usage": layer["usage"]},
        "limits": [
            "Offline tokenization of saved tool text is not total model input/output, reasoning tokens, or billed usage.",
            "Known scripted routes in all arms; source exploration, action selection, and autonomous repair diagnosis are unmeasured.",
            "First use includes init, labeling, and recording; replay uses a copied graph without runtime state.",
            "Navigation timing excludes setup/readiness and the independent grader; both are retained per trial.",
            "Five matched blocks per app/API on one host with two emulators; no population confidence, p95, or SLA claim.",
            "API/device cases are separate strata; different emulator viewports and shared host load prevent attributing differences solely to Android version.",
            "Cost projections use measured arm medians, not a longitudinal trial; no context replay, caching, or repair frequency is modeled.",
            "Skill-once and skill-every-use are explicit text-volume scenarios, not observed agent policies.",
            "Earlier incomplete runs remain separate; their failures are not pooled into v1.2 timing measurements.",
            "Recovery controls are scripted and source-informed, with different work per scenario; their durations are not interchangeable repair latencies.",
        ],
    }
