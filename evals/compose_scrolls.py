#!/usr/bin/env python3
"""Verify that repeated Jetsnack scrolls survive learning and teammate replay.

Search remains a visible tab throughout; this checks recipe preservation,
not locating an off-screen item or navigating to a particular item instance.
"""
import argparse
import json
from pathlib import Path

from jetsnack_smoke import Eval


class ScrollEval(Eval):
    def restart_fixture(self):
        # A tab switch preserves scroll position; a fresh process restores the
        # specific unscrolled Home fixture required by reset_home's oracle.
        self.run("fixture", ["adb", "-s", self.args.serial, "shell", "am", "force-stop", "com.example.jetsnack"])
        self.run("fixture", ["android", "run", f"--device={self.args.serial}", f"--apks={self.args.apk}"])
        self.reset_home()

    def execute(self):
        self.restart_fixture()
        seed = self.output / "seed"
        seed.mkdir()
        self.minimap("learning", seed, "init", "--no-skills", "--package", "com.example.jetsnack")
        self.minimap("learning", seed, "whereami", "--label", "home")
        for _ in range(2):
            self.minimap("learning", seed, "scroll", "--direction", "down")
        learned = self.minimap("learning", seed, "tap", "--selector", "content_desc=SEARCH", "--label", "search")
        edge = json.loads((seed / ".minimap/graph/edges" / f"{learned['data']['edge']}.json").read_text())
        assert [step["kind"] for step in edge["recipe"]] == ["scroll", "scroll", "tap"]
        assert self.at(self.android_layout("oracle"), "search")
        self.minimap("learning", seed, "tap", "--selector", "content_desc=HOME", "--label", "home")
        teammate = self.copy_graph(seed, "teammate")
        replay = self.minimap("replay", teammate, "go", "search")
        assert replay["data"]["metrics"]["action_budget_used"] == 3
        assert self.at(self.android_layout("oracle"), "search")
        self.metadata["scroll_replay"] = {"passed": True, "recipe_actions": 3, **self.measure("replay")}
        self.metadata["learning"] = self.measure("learning")
        self.restart_fixture()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.binary, args.apk = args.binary.resolve(), args.apk.resolve()
    args.repetitions = 1
    evaluation = ScrollEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()


if __name__ == "__main__":
    main()
