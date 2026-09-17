#!/usr/bin/env python3
"""Live Jetsnack smoke: learning, cold/warm replay, and stale-session correctness.

Uses Android CLI for deployment and layouts, and adb for input (Android CLI has
no input command). Graph/cache/output files live under a new output folder;
device locks and recovery tokens use Minimap's private host runtime directory.
This measures subprocess time and bytes, not an agent's billed tokens.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time


class Eval:
    def __init__(self, args):
        self.args = args
        self.output = args.output.resolve()
        self.output.mkdir(parents=True, exist_ok=False)
        self.runtime = self.output / "runtime"
        self.runtime.mkdir()
        self.env = dict(os.environ, TMPDIR=str(self.runtime))
        self.calls = []
        self.results = []
        self.metadata = {
            "binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
            "apk_sha256": hashlib.sha256(args.apk.read_bytes()).hexdigest(),
            "serial": args.serial,
            "repetitions": args.repetitions,
            "timing_scope": "top-level subprocess wall time; excludes fixture setup and independent oracle",
            "token_usage": None,
            "goal_checks": getattr(args, "goal_checks", False),
        }

    def run(self, phase, argv, cwd=None, check=True):
        start = time.perf_counter()
        timed_out = False
        try:
            result = subprocess.run(
                [str(arg) for arg in argv], cwd=cwd, env=self.env,
                capture_output=True, timeout=getattr(self, "command_timeout", 60),
            )
        except subprocess.TimeoutExpired as error:
            # A watchdog failure is part of the trial cost, not a missing row.
            timed_out = True
            result = subprocess.CompletedProcess(argv, 124, error.stdout or b"", error.stderr or b"")
        call = {
            "phase": phase, "argv": [str(arg) for arg in argv],
            "seconds": time.perf_counter() - start,
            "exit_code": result.returncode,
            "stdout_bytes": len(result.stdout), "stderr_bytes": len(result.stderr),
            "timed_out": timed_out,
        }
        index = len(self.calls)
        self.calls.append(call)
        (self.output / f"{index:03d}.stdout").write_bytes(result.stdout)
        (self.output / f"{index:03d}.stderr").write_bytes(result.stderr)
        if timed_out or (check and result.returncode):
            raise RuntimeError(f"Command failed: {argv}; see output {index:03d}")
        return result.stdout.decode("utf-8")

    def android_layout(self, phase):
        for attempt in range(3):
            text = self.run(phase, ["android", "layout", f"--device={self.args.serial}"])
            if text.strip() and "null root node returned by UiTestAutomationBridge" not in text:
                break
            time.sleep(0.15)
        layout = json.loads(text)
        if not isinstance(layout, list):
            raise ValueError("Expected the installed Android CLI's flat layout array")
        return layout

    def tap(self, phase, layout, description):
        matches = [node for node in layout if node.get("content-desc") == description]
        if len(matches) != 1:
            raise ValueError(f"Expected one visible {description} control; found {len(matches)}")
        x, y = json.loads(matches[0]["center"])
        self.run(phase, ["adb", "-s", self.args.serial, "shell", "input", "tap", x, y])

    def reset_home(self):
        layout = self.android_layout("fixture")
        self.tap("fixture", layout, "HOME")
        if not self.at(self.android_layout("fixture"), "home"):
            raise RuntimeError("Could not establish the Home fixture")

    @staticmethod
    def at(layout, target):
        texts = {node.get("text") for node in layout}
        expected = {"Categories", "Lifestyles", "SEARCH"} if target == "search" else {"HOME", "Android's picks"}
        return expected <= texts

    def minimap(self, phase, repo, *command, check=True):
        return json.loads(self.run(
            phase, [self.args.binary, "--serial", self.args.serial, *command], repo, check,
        ))

    def copy_graph(self, seed, name):
        repo = self.output / name
        shutil.copytree(seed / ".minimap", repo / ".minimap")
        return repo

    def measure(self, phase):
        calls = [call for call in self.calls if call["phase"] == phase]
        return {
            "seconds": sum(call["seconds"] for call in calls),
            "commands": len(calls),
            "stdout_bytes": sum(call["stdout_bytes"] for call in calls),
            "stderr_bytes": sum(call["stderr_bytes"] for call in calls),
        }

    def execute(self):
        self.metadata["android_version"] = self.run("environment", ["android", "--version"]).strip()
        self.metadata["adb_version"] = self.run("environment", ["adb", "version"]).strip()
        self.metadata["api_level"] = self.run("environment", ["adb", "-s", self.args.serial, "shell", "getprop", "ro.build.version.sdk"]).strip()
        self.metadata["viewport"] = self.run("environment", ["adb", "-s", self.args.serial, "shell", "wm", "size"]).strip()
        self.run("deployment", ["android", "run", f"--device={self.args.serial}", f"--apks={self.args.apk}"])
        self.reset_home()
        seed = self.output / "seed"
        seed.mkdir()
        self.minimap("learning", seed, "init", "--no-skills")
        config_path = seed / ".minimap/config.json"
        config = json.loads(config_path.read_text())
        config["app_profiles"]["default"]["android_package"] = "com.example.jetsnack"
        config_path.write_text(json.dumps(config, indent=2) + "\n")
        self.minimap("learning", seed, "whereami", "--label", "home")
        self.minimap("learning", seed, "tap", "--selector", "content_desc=SEARCH", "--label", "search")
        self.minimap("learning", seed, "tap", "--selector", "content_desc=HOME", "--label", "home")
        if not self.at(self.android_layout("oracle"), "home"):
            raise RuntimeError("Learning did not return to Home")
        self.metadata["learning"] = self.measure("learning")

        arms = ["raw", "cold", "warm"]
        for repeat in range(self.args.repetitions):
            # Rotate arm order to limit systematic warm-up and thermal bias.
            for arm in arms[repeat % 3:] + arms[:repeat % 3]:
                phase = f"{arm}-{repeat + 1}"
                repo = self.copy_graph(seed, phase)
                self.reset_home()
                if arm == "warm":
                    self.minimap("warmup", repo, "whereami")
                if arm == "raw":
                    self.tap(phase, self.android_layout(phase), "SEARCH")
                    reported_ok = self.at(self.android_layout(phase), "search")
                    start_source = None
                else:
                    checks = ("--expect", "text=Categories", "--expect", "text=Lifestyles", "--expect", "text=SEARCH") if getattr(self.args, "goal_checks", False) else ()
                    result = self.minimap(phase, repo, "go", "search", *checks, check=False)
                    reported_ok = result.get("status") == "ok"
                    start_source = result.get("data", {}).get("start_source")
                    # Include the common product-verification read in both arms.
                    if not checks:
                        self.minimap(phase, repo, "layout")
                reached = self.at(self.android_layout("oracle"), "search")
                row = {
                    "arm": arm, "repeat": repeat + 1, **self.measure(phase),
                    "reported_ok": reported_ok, "oracle_reached_target": reached,
                    "start_source": start_source,
                }
                self.results.append(row)
                print(json.dumps(row), flush=True)

        self.reset_home()
        repo = self.copy_graph(seed, "external-navigation")
        self.minimap("adversarial-setup", repo, "whereami")
        self.tap("adversarial-setup", self.android_layout("adversarial-setup"), "SEARCH")
        if not self.at(self.android_layout("oracle"), "search"):
            raise RuntimeError("External-navigation fixture did not reach Search")
        result = self.minimap("adversarial-go", repo, "go", "home", check=False)
        reached = self.at(self.android_layout("oracle"), "home")
        self.metadata["external_navigation"] = {
            "reported_status": result.get("status"), "oracle_reached_home": reached,
            "false_success": result.get("status") == "ok" and not reached,
            "planned_path": result.get("data", {}).get("planned_path"),
            **self.measure("adversarial-go"),
        }
        self.reset_home()

    def save(self):
        summary = {}
        for arm in ("raw", "cold", "warm"):
            rows = [row for row in self.results if row["arm"] == arm]
            if rows:
                summary[arm] = {
                    "trials": len(rows),
                    "successes": sum(row["reported_ok"] and row["oracle_reached_target"] for row in rows),
                    **{f"median_{key}": statistics.median(row[key] for row in rows)
                       for key in ("seconds", "commands", "stdout_bytes", "stderr_bytes")},
                    "max_seconds": max(row["seconds"] for row in rows),
                }
        report = {"metadata": self.metadata, "summary": summary, "trials": self.results, "calls": self.calls}
        (self.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"summary": summary, "external_navigation": self.metadata.get("external_navigation")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New directory; must not already exist")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--goal-checks", action="store_true", help="Use fresh declarative destination checks in go instead of a separate Minimap layout read")
    args = parser.parse_args()
    if args.repetitions < 1 or not args.binary.is_file() or not args.apk.is_file():
        parser.error("Provide existing binary/APK files and a positive repetition count")
    args.binary, args.apk = args.binary.resolve(), args.apk.resolve()
    evaluation = Eval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()
    if evaluation.metadata["external_navigation"]["false_success"] or any(
        not row["reported_ok"] or not row["oracle_reached_target"] for row in evaluation.results
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
