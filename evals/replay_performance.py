#!/usr/bin/env python3
"""Compare raw navigation with frozen baseline/candidate replay; optionally profile.

Profiles add Python wrapper overhead and are diagnostics, never confirmation
measurements. Raw UI logs stay in the chosen, new output directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
from statistics import median
import sys
import time

from paired_navigation import PairedEval, SAMPLES, verifies


PROXY = '''import json, os, pathlib, subprocess, sys, time
tool = pathlib.Path(sys.argv[0]).name
depth = int(os.environ.get("MINIMAP_PROFILE_DEPTH", "0"))
env = dict(os.environ, MINIMAP_PROFILE_DEPTH=str(depth + 1))
start = time.perf_counter()
status = -1
try:
    status = subprocess.run([env["MINIMAP_REAL_" + tool.upper()], *sys.argv[1:]], env=env).returncode
finally:
    event = {"phase": env["MINIMAP_PROFILE_PHASE"], "tool": tool,
             "args": sys.argv[1:], "seconds": time.perf_counter() - start,
             "status": status, "depth": depth}
    fd = os.open(env["MINIMAP_PROFILE_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(event) + "\\n").encode())
    finally:
        os.close(fd)
sys.exit(status)
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def graph_digest(root):
    return {str(p.relative_to(root)): digest(p)
            for p in sorted((root / ".minimap").rglob("*.json"))}


class ReplayEval(PairedEval):
    def __init__(self, args):
        super().__init__(args)
        self.binaries = {"baseline": args.baseline}
        if args.candidate:
            self.binaries["candidate"] = args.candidate
        self.current_binary = args.baseline
        self.arms = ["raw", *self.binaries]
        harness = self.output / "harness"
        harness.mkdir()
        for name in ("replay_performance.py", "paired_navigation.py", "jetsnack_smoke.py", "PERFORMANCE_V2.md"):
            shutil.copy2(Path(__file__).with_name(name), harness / name)
        self.metadata.update({
            "suite": "replay-performance-v2", "stage": args.stage,
            "profiled": args.profile, "profile_overhead_included": args.profile,
            "binary_sha256_by_arm": {arm: digest(p) for arm, p in self.binaries.items()},
            "protocol_sha256": digest(Path(__file__).with_name("PERFORMANCE_V2.md")),
            "runner_sha256": digest(Path(__file__)),
            "planned_trials": len(args.samples) * len(self.arms) * args.repetitions,
            "settle_override": self.env.get("MINIMAP_ACTION_SETTLE_MS"),
            "goal_checks": True, "arms": self.arms,
            "order_offset": args.order_offset, "startup_seconds": args.startup_seconds,
            "host": {"system": platform.system(), "release": platform.release(),
                     "architecture": platform.machine()},
        })
        if args.profile:
            proxy = self.output / "proxy"
            proxy.mkdir()
            for tool in ("adb", "android"):
                real = shutil.which(tool)
                if not real:
                    raise RuntimeError(f"Missing {tool}")
                self.env["MINIMAP_REAL_" + tool.upper()] = real
                script = proxy / tool
                script.write_text(f"#!{sys.executable}\n" + PROXY)
                script.chmod(0o700)
            self.env.update({"PATH": str(proxy) + os.pathsep + self.env["PATH"],
                             "MINIMAP_PROFILE_LOG": str(self.output / "profile.jsonl")})

    def run(self, phase, argv, cwd=None, check=True):
        self.env["MINIMAP_PROFILE_PHASE"] = phase
        return super().run(phase, argv, cwd, check)

    def minimap(self, phase, repo, *command, check=True):
        return json.loads(self.run(phase, [self.current_binary, "--serial", self.args.serial,
                                          *command], repo, check))

    def execute(self):
        self.metadata["api_level"] = self.run("environment", ["adb", "-s", self.args.serial,
                                                             "shell", "getprop", "ro.build.version.sdk"]).strip()
        self.metadata["android_version"] = self.run("environment", ["android", "--version"]).strip()
        self.metadata["adb_version"] = self.run("environment", ["adb", "version"]).strip()
        self.metadata["viewport"] = self.run("environment", ["adb", "-s", self.args.serial,
                                                            "shell", "wm", "size"]).strip()
        self.metadata["apks"] = {}
        for sample in self.args.samples:
            spec = {**SAMPLES[sample], "apk": self.args.apks / f"{sample}-baseline.apk"}
            self.metadata["apks"][sample] = digest(spec["apk"])
            seed = self.output / f"seed-{sample}"
            seed.mkdir()
            seed_error = None
            self.current_binary = self.args.baseline
            try:
                self.restart(spec, f"seed-setup-{sample}")
                learned = self.learn(f"seed-{sample}", seed, spec)
                if learned["status"] != "ok" or not verifies(self.android_layout(f"seed-oracle-{sample}"), spec["checks"]):
                    raise RuntimeError("Seed destination check failed")
            except Exception as error:
                seed_error = str(error)
            for repeat in range(1, self.args.repetitions + 1):
                offset = (repeat - 1 + self.args.order_offset) % len(self.arms)
                for arm in self.arms[offset:] + self.arms[:offset]:
                    phase = f"{sample}-{repeat}-{arm}"
                    root = self.output / phase
                    row = {"sample": sample, "api": self.metadata["api_level"],
                           "repeat": repeat, "arm": arm, "phase": phase,
                           "setup_passed": False, "reported_ok": False,
                           "oracle_reached_target": None, "error": None}
                    start = time.perf_counter()
                    before = {}
                    try:
                        if seed_error:
                            raise RuntimeError(seed_error)
                        if arm == "raw":
                            root.mkdir()
                        else:
                            self.copy_graph(seed, phase)
                        before = graph_digest(root)
                        self.restart(spec, f"setup-{phase}")
                        row["setup_passed"] = True
                        if arm == "raw":
                            for selector in spec["steps"]:
                                self.raw_tap(phase, self.android_layout(phase), selector)
                            row["reported_ok"] = verifies(self.android_layout(phase), spec["checks"])
                        else:
                            self.current_binary = self.binaries[arm]
                            result = self.go(phase, root, spec)
                            row["status"] = result["status"]
                            row["reported_ok"] = result["status"] == "ok"
                    except Exception as error:
                        row["error"] = str(error)
                    if row["setup_passed"]:
                        try:
                            row["oracle_reached_target"] = verifies(self.android_layout(f"oracle-{phase}"), spec["checks"])
                        except Exception as error:
                            row["oracle_error"] = str(error)
                    row.update(self.measure(phase))
                    row.update({"setup": self.measure(f"setup-{phase}"),
                                "oracle": self.measure(f"oracle-{phase}"),
                                "total_seconds_including_setup_oracle": time.perf_counter() - start,
                                "graph_before": before, "graph_after": graph_digest(root)})
                    row["graph_unchanged"] = row["graph_before"] == row["graph_after"]
                    row["success"] = bool(row["setup_passed"] and row["reported_ok"]
                                          and row["oracle_reached_target"] and row["graph_unchanged"]
                                          and row["error"] is None)
                    row["false_success"] = bool(row["reported_ok"] and row["oracle_reached_target"] is False)
                    self.results.append(row)
                    self.save()
                    print(json.dumps({k: row[k] for k in ("sample", "api", "arm", "repeat", "success", "seconds", "error")}), flush=True)

    def save(self):
        summary = {}
        for sample in self.args.samples:
            summary[sample] = {}
            for arm in self.arms:
                rows = [r for r in self.results if r["sample"] == sample and r["arm"] == arm]
                good = [r["seconds"] for r in rows if r["success"]]
                summary[sample][arm] = {"assigned": len(rows), "successes": len(good),
                                        "median_successful_seconds": median(good) if good else None}
        report = {"metadata": self.metadata, "summary": summary,
                  "trials": self.results, "calls": self.calls}
        (self.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "apks", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--order-offset", type=int, default=0)
    parser.add_argument("--startup-seconds", type=float, default=30)
    parser.add_argument("--samples", nargs="+", choices=list(SAMPLES), default=list(SAMPLES))
    parser.add_argument("--stage", choices=("profile", "pilot", "confirmation"), default="pilot")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if not args.serial.startswith("emulator-") or args.repetitions < 1 or not 0 < args.startup_seconds <= 60:
        parser.error("Use an emulator, positive repetitions, and a 1–60 second startup bound")
    if len(args.samples) != len(set(args.samples)):
        parser.error("Duplicate samples")
    if args.stage == "confirmation" and (args.profile or not args.candidate or args.repetitions != 10 or set(args.samples) != set(SAMPLES)):
        parser.error("Confirmation requires both binaries, all samples, 10 blocks, and no profiling")
    for name in ("baseline", "candidate", "apks", "output"):
        if getattr(args, name) is not None:
            setattr(args, name, getattr(args, name).resolve())
    args.binary, args.apk = args.baseline, args.apks / "jetsnack-baseline.apk"
    evaluation = ReplayEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()
    if len(evaluation.results) != evaluation.metadata["planned_trials"] or not all(r["success"] for r in evaluation.results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
