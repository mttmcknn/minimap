#!/usr/bin/env python3
"""Controlled Jetsnack repairs, independently checked with Android CLI layouts.

This tests the CLI/host recovery contract, not autonomous model reasoning or
billed tokens. Fixture source changes and APK hashes belong in the run manifest.
Build variants with build_compose_fixtures.py before running this evaluation.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

from jetsnack_smoke import Eval


def graph_digest(repo):
    digest = hashlib.sha256()
    for file in sorted((repo / ".minimap/graph").rglob("*.json")):
        digest.update(str(file.relative_to(repo)).encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


class RecoveryEval(Eval):
    def minimap(self, phase, repo, *command, check=True):
        if phase not in {"renamed_control", "relocated_route", "wrong_callback"}:
            return super().minimap(phase, repo, *command, check=check)
        budgets = self.metadata.setdefault("host_budgets", {})
        budget = budgets.setdefault(phase, {"actions_used": 0, "action_limit": 32,
                                             "seconds_limit": self.args.host_seconds})
        starts = getattr(self, "budget_starts", {})
        starts.setdefault(phase, time.perf_counter())
        self.budget_starts = starts
        remaining = self.args.host_seconds - (time.perf_counter() - starts[phase])
        assert remaining >= 1 and budget["actions_used"] < 32, "Host recovery budget exhausted"
        if command[0] == "go":
            command = (*command, "--max-actions", str(32 - budget["actions_used"]),
                       "--recovery-seconds", str(min(300, int(remaining))))
            if command[1] == "search":
                command = (*command, "--expect", "text=Categories", "--expect", "text=Lifestyles")
        token = getattr(self, "recovery_tokens", {}).get(phase)
        if token:
            command = (*command, "--recovery", token)
        result = super().minimap(phase, repo, *command, check=check)
        context = result.get("data", {}).get("recovery", {})
        self.recovery_tokens = getattr(self, "recovery_tokens", {})
        if context.get("token"):
            self.recovery_tokens[phase] = None if context.get("complete") else context["token"]
        budget["actions_used"] += (result.get("data", {}).get("metrics", {}).get("action_budget_used", 0)
                                   if command[0] == "go" else int(command[0] in {"tap", "scroll", "back"}))
        budget["elapsed_seconds"] = time.perf_counter() - starts[phase]
        return result

    def deploy(self, variant):
        apk = self.args.apks / f"jetsnack-{variant}.apk"
        self.metadata.setdefault("apks", {})[variant] = hashlib.sha256(apk.read_bytes()).hexdigest()
        self.run("fixture", ["android", "run", f"--device={self.args.serial}", f"--apks={apk}"])
        self.reset_home()

    def assert_search(self):
        texts = {node.get("text") for node in self.android_layout("oracle")}
        assert {"Categories", "Lifestyles"} <= texts, "Independent oracle did not see Search"

    def record(self, scenario, **fields):
        self.metadata["host_budgets"][scenario]["elapsed_seconds"] = time.perf_counter() - self.budget_starts[scenario]
        row = {"scenario": scenario, **fields, **self.measure(scenario)}
        self.metadata.setdefault("scenarios", []).append(row)
        print(json.dumps(row), flush=True)

    def execute(self):
        self.metadata["goal_checks"] = True
        manifest = self.args.apks.parent / "manifest.json"
        if manifest.is_file():
            self.metadata["fixture_manifest"] = json.loads(manifest.read_text())
        self.deploy("baseline")
        seed = self.output / "seed"
        seed.mkdir()
        self.minimap("learning", seed, "init", "--no-skills", "--package", "com.example.jetsnack")
        self.minimap("learning", seed, "whereami", "--label", "home")
        learned = self.minimap("learning", seed, "tap", "--selector", "content_desc=SEARCH", "--label", "search")
        old_edge = learned["data"]["edge"]
        self.minimap("learning", seed, "tap", "--selector", "content_desc=HOME", "--label", "home")
        self.metadata["learning"] = self.measure("learning")

        for scenario, variant in [("renamed_control", "renamed"), ("relocated_route", "relocated")]:
            self.deploy(variant)
            repo = self.copy_graph(seed, scenario)
            before = graph_digest(repo)
            failed = self.minimap(scenario, repo, "go", "search", check=False)
            assert failed["status"] != "ok", "Stale route unexpectedly reported success"
            assert failed["data"]["recovery"]["outcome"] == "needs_agent"
            assert before == graph_digest(repo), "Failed replay changed the shared graph"
            # The host has inspected the fixture's source diff and fresh UI;
            # Minimap performs every action used to learn and verify the repair.
            self.minimap(scenario, repo, "layout", "--fresh")
            if variant == "renamed":
                arrival = self.minimap(scenario, repo, "tap", "--selector", "content_desc=DISCOVER", check=False)
                self.assert_search()
                if arrival["status"] == "needs_label":
                    # Source diff changes only home_search; Categories/Lifestyles
                    # independently establish the same Search destination.
                    self.minimap(scenario, repo, "whereami", "--confirm-place", "place_search")
                else:
                    assert arrival["status"] == "ok"
            else:
                self.minimap(scenario, repo, "tap", "--selector", "content_desc=PROFILE", "--label", "profile")
                self.minimap(scenario, repo, "tap", "--selector", "content_desc=SEARCH", "--label", "search")
            self.assert_search()
            self.minimap(scenario, repo, "go", "home")
            promoted = self.minimap(scenario, repo, "go", "search", "--supersede", old_edge)
            self.assert_search()
            retained = json.loads((repo / ".minimap/graph/edges" / f"{old_edge}.json").read_text())
            assert retained["superseded_by"], "The verified replacement was not preferred"
            # A teammate receives only the committed graph, no runtime cache.
            self.reset_home()
            teammate = self.copy_graph(repo, scenario + "-teammate")
            replay = self.minimap(scenario, teammate, "go", "search")
            self.assert_search()
            self.deploy("baseline")
            older = self.copy_graph(repo, scenario + "-older-build")
            fallback = self.minimap(scenario, older, "go", "search")
            self.assert_search()
            fallback_used = old_edge in fallback["data"]["planned_path"]
            if variant == "renamed":
                assert fallback_used, "An older build lost its original valid route"
            self.record(scenario, passed=True, initial_status=failed["status"],
                        repaired_steps=len(promoted["data"]["executed_steps"]),
                        teammate_status=replay["status"], older_build_status=fallback["status"],
                        fallback_used=fallback_used, user_interventions=0)

        self.deploy("broken")
        repo = self.copy_graph(seed, "wrong_callback")
        before = graph_digest(repo)
        statuses = []
        for reproduction in range(2):
            self.reset_home()
            command = ("go", "search") if reproduction == 0 else ("tap", "--selector", "content_desc=SEARCH")
            result = self.minimap("wrong_callback", repo, *command, check=False)
            statuses.append(result["status"])
            texts = {node.get("text") for node in self.android_layout("oracle")}
            assert "Categories" not in texts
            assert any("Subtotal" in (text or "") for text in texts), "Expected the deliberately wrong Cart destination"
            assert result["status"] != "ok"
        assert before == graph_digest(repo), "The product regression was learned as a valid route"
        self.record("wrong_callback", passed=True, statuses=statuses, graph_unchanged=True,
                    diagnosis="Reproduced twice; fixture source maps Search to Cart. Escalate the product bug, preserve the original target.")
        self.deploy("baseline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--apks", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-seconds", type=int, default=180,
                        help="Shared wall-time budget per controlled host recovery, including oracle/setup between commands")
    args = parser.parse_args()
    args.binary, args.apks = args.binary.resolve(), args.apks.resolve()
    args.apk = args.apks / "jetsnack-baseline.apk"
    args.repetitions = 1
    evaluation = RecoveryEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()


if __name__ == "__main__":
    main()
