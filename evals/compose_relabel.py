#!/usr/bin/env python3
"""Relabel a learned Jetsnack place and verify route IDs and teammate reuse."""
import argparse
import json
from pathlib import Path

from jetsnack_smoke import Eval


class RelabelEval(Eval):
    def execute(self):
        self.metadata["goal_checks"] = True
        self.run("fixture", ["android", "run", f"--device={self.args.serial}", f"--apks={self.args.apk}"])
        self.reset_home()
        repo = self.copy_graph(self.args.seed, "relabeled")
        edges = repo / ".minimap/graph/edges"
        before_ids = {path.stem for path in edges.glob("*.json")}
        self.minimap("relabel", repo, "go", "search", "--expect", "text=Categories")
        renamed = self.minimap("relabel", repo, "whereami", "--label", "catalog")
        assert renamed["place"]["id"] == "place_search"
        self.minimap("relabel", repo, "go", "home")
        orientation = self.minimap("relabel", repo, "whereami", "--fresh")
        assert any(edge["to"] == "catalog" for edge in orientation["known_exits"])
        learned = self.minimap("relabel", repo, "tap", "--selector", "content_desc=SEARCH", "--label", "catalog")
        assert learned["data"]["edge"] in before_ids, "Relabeling duplicated a verified route"
        assert before_ids == {path.stem for path in edges.glob("*.json")}
        self.minimap("relabel", repo, "go", "home")
        teammate = self.copy_graph(repo, "teammate")
        reached = self.minimap("teammate", teammate, "go", "catalog", "--expect", "text=Categories", "--expect", "text=Lifestyles")
        assert self.at(self.android_layout("oracle"), "search")
        self.metadata["relabel"] = {"passed": True, "place_id_preserved": True,
                                    "edge_ids_preserved": True, "edges": len(before_ids),
                                    "current_exit_labels": True, "teammate_status": reached["status"],
                                    **self.measure("relabel")}
        self.reset_home()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["binary", "apk", "seed", "output"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    args.binary, args.apk, args.seed = args.binary.resolve(), args.apk.resolve(), args.seed.resolve()
    args.repetitions = 1
    evaluation = RelabelEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()


if __name__ == "__main__":
    main()
