#!/usr/bin/env python3
"""Matched raw / first-use / reused-graph trials on one emulator."""
import argparse
import hashlib
import json
from pathlib import Path
import time

from jetsnack_smoke import Eval

SAMPLES = {
    "jetsnack": {"package": "com.example.jetsnack", "activity": ".ui.MainActivity", "target": "search",
                 "steps": ["content_desc=SEARCH"], "checks": ["text=Categories", "text=Lifestyles", "text=SEARCH"],
                 "home": ["text=HOME", "text=Android's picks"]},
    "jetnews": {"package": "com.example.jetnews", "activity": ".ui.MainActivity", "target": "interests",
                "steps": ["content_desc=Open navigation drawer", "text=Interests"],
                "checks": ["text=Topics", "text=People", "text=Publications"], "home": ["text=Top stories for you"]},
    "jetchat": {"package": "com.example.compose.jetchat", "activity": ".NavActivity", "target": "profile",
                "steps": ["content_desc=Open navigation drawer", "text=Ali Conors (you)"],
                "checks": ["text=Ali Conors", "content_desc=Edit Profile"], "home": ["text=#composers"]},
}
ARMS = ["raw", "new_graph", "reused_graph"]


def visible_matches(layout, selector):
    kind, value = selector.split("=", 1)
    keys = {"text": ["text"], "content_desc": ["content-desc", "contentDesc", "contentDescription"],
            "resource_id": ["resourceId", "resource-id"], "test_tag": ["testTag", "test-tag"]}[kind]
    return [node for node in layout if any(node.get(key) == value for key in keys)
            and node.get("off-screen") not in [True, "true"] and node.get("visible", True) not in [False, "false"]]


def verifies(layout, checks):
    return all(len(visible_matches(layout, selector)) == 1 for selector in checks)


def graph_size(repo):
    files = sorted((repo / ".minimap/graph").rglob("*.json"))
    return {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}


class PairedEval(Eval):
    def wait_for_start(self, spec, phase):
        """Wait only during fixture setup; never retry a measured navigation."""
        started = time.perf_counter()
        deadline = started + self.args.startup_seconds
        attempts = 0
        ready = False
        last = "No layout captured"
        previous_timeout = getattr(self, "command_timeout", 60)
        try:
            while time.perf_counter() < deadline:
                self.command_timeout = deadline - time.perf_counter()
                attempts += 1
                text = self.run(phase, ["android", "layout", f"--device={self.args.serial}"])
                if not text.strip():
                    last = "Empty Android CLI response"
                else:
                    layout = json.loads(text)
                    if not isinstance(layout, list):
                        raise ValueError("Expected the Android CLI's flat layout array")
                    if verifies(layout, spec["home"]):
                        ready = True
                        return
                    last = "Home anchors absent from the fresh layout"
                time.sleep(min(0.25, max(0, deadline - time.perf_counter())))
            raise RuntimeError(f"Start-state oracle failed within {self.args.startup_seconds}s: {last}")
        finally:
            self.command_timeout = previous_timeout
            self.metadata.setdefault("startup_checks", []).append({
                "phase": phase, "attempts": attempts, "ready": ready,
                "seconds": time.perf_counter() - started,
            })

    def restart(self, spec, phase="fixture"):
        self.run(phase, ["adb", "-s", self.args.serial, "shell", "am", "force-stop", spec["package"]])
        self.run(phase, ["android", "run", f"--device={self.args.serial}",
                         f"--apks={spec['apk']}"])
        self.wait_for_start(spec, phase)

    def raw_tap(self, phase, layout, selector):
        matches = visible_matches(layout, selector)
        assert len(matches) == 1, f"Selector {selector} not uniquely visible"
        node = matches[0]
        assert node.get("enabled", True) not in [False, "false"], "Control is disabled"
        point = node["center"]
        x, y = json.loads(point) if isinstance(point, str) else point
        self.run(phase, ["adb", "-s", self.args.serial, "shell", "input", "tap", int(x), int(y)])

    def learn(self, phase, repo, spec):
        self.minimap(phase, repo, "init", "--no-skills", "--package", spec["package"])
        self.minimap(phase, repo, "whereami", "--label", "home")
        for index, selector in enumerate(spec["steps"]):
            labels = ("--label", spec["target"]) if index == len(spec["steps"]) - 1 else ()
            result = self.minimap(phase, repo, "tap", "--selector", selector, *labels, check=False)
            assert result["status"] in ({"ok"} if labels else {"ok", "needs_label"}), result
        return self.go(phase, repo, spec)

    def go(self, phase, repo, spec):
        checks = [arg for selector in spec["checks"] for arg in ("--expect", selector)]
        return self.minimap(phase, repo, "go", spec["target"], *checks, check=False)

    def execute(self):
        self.metadata.update({"suite": "controlled-v1.2", "arms": ARMS, "order_offset": self.args.order_offset,
                              "startup_seconds": self.args.startup_seconds,
                              "goal_checks": True, "sample_results": {}, "apks": {}})
        self.metadata["api_level"] = self.run("environment", ["adb", "-s", self.args.serial, "shell", "getprop", "ro.build.version.sdk"]).strip()
        self.metadata["viewport"] = self.run("environment", ["adb", "-s", self.args.serial, "shell", "wm", "size"]).strip()
        self.metadata["android_version"] = self.run("environment", ["android", "--version"]).strip()
        for sample in self.args.samples:
            apk = self.args.apks / f"{sample}-baseline.apk"
            spec = {**SAMPLES[sample], "apk": apk}
            self.metadata["apks"][sample] = hashlib.sha256(apk.read_bytes()).hexdigest()
            seed = self.output / f"seed-{sample}"
            seed.mkdir()
            phase = f"seed-{sample}"
            seed_error = None
            try:
                self.run("deployment", ["android", "run", f"--device={self.args.serial}", f"--apks={apk}"])
                self.restart(spec)
                learned = self.learn(phase, seed, spec)
                assert learned["status"] == "ok" and verifies(self.android_layout("seed-oracle"), spec["checks"])
            except Exception as error:
                seed_error = str(error)[:3000]
            self.metadata["sample_results"][sample] = {"seed_learning": self.measure(phase), "seed_graph": graph_size(seed),
                                                       "seed_error": seed_error}
            for repeat in range(self.args.repetitions):
                offset = (repeat + self.args.order_offset) % len(ARMS)
                for arm in ARMS[offset:] + ARMS[:offset]:
                    phase = f"{sample}-{repeat + 1}-{arm}"
                    repo = self.copy_graph(seed, phase) if arm == "reused_graph" and seed_error is None else self.output / phase
                    repo.mkdir(exist_ok=True)
                    row = {"sample": sample, "api": self.metadata["api_level"], "arm": arm, "repeat": repeat + 1,
                           "reported_ok": False, "oracle_reached_target": False, "error": None, "setup_passed": False}
                    start = time.perf_counter()
                    try:
                        if seed_error is not None:
                            raise RuntimeError(f"Sample seed preparation failed: {seed_error}")
                        self.restart(spec, f"setup-{phase}")
                        row["setup_passed"] = True
                        if arm == "raw":
                            for selector in spec["steps"]:
                                self.raw_tap(phase, self.android_layout(phase), selector)
                            row["reported_ok"] = verifies(self.android_layout(phase), spec["checks"])
                        else:
                            result = self.learn(phase, repo, spec) if arm == "new_graph" else self.go(phase, repo, spec)
                            row["reported_ok"] = result["status"] == "ok"
                            row["status"] = result["status"]
                        row["oracle_reached_target"] = verifies(self.android_layout(f"oracle-{phase}"), spec["checks"])
                    except Exception as error:
                        row["error"] = str(error)[:3000]
                    row.update(self.measure(phase))
                    row.update({"total_seconds_including_setup_oracle": time.perf_counter() - start,
                                "setup": self.measure(f"setup-{phase}"), "oracle": self.measure(f"oracle-{phase}"),
                                "false_success": row["reported_ok"] and not row["oracle_reached_target"],
                                "graph": graph_size(repo)})
                    self.results.append(row)
                    with (self.output / "trials.jsonl").open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                    print(json.dumps(row), flush=True)
                    self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["binary", "apks", "output"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--order-offset", type=int, default=0)
    parser.add_argument("--startup-seconds", type=float, default=30,
                        help="Bounded Home readiness wait before each trial; recorded as setup cost")
    parser.add_argument("--samples", nargs="+", choices=list(SAMPLES), default=list(SAMPLES))
    args = parser.parse_args()
    assert args.serial.startswith("emulator-"), "This suite is limited to emulators"
    assert args.repetitions > 0
    assert 0 < args.startup_seconds <= 60
    args.binary, args.apks = args.binary.resolve(), args.apks.resolve()
    args.apk = args.apks / "jetsnack-baseline.apk"
    evaluation = PairedEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()
    if any(not row["reported_ok"] or not row["oracle_reached_target"] for row in evaluation.results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
