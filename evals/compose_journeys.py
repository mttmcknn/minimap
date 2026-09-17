#!/usr/bin/env python3
"""Learn and replay multi-action Compose sample drawer routes on a real emulator."""
import argparse
import json
from pathlib import Path

from jetsnack_smoke import Eval
from compose_recovery import graph_digest


class JourneyEval(Eval):
    def execute(self):
        self.metadata["goal_checks"] = True
        is_chat = self.args.sample == "jetchat"
        package = "com.example.compose.jetchat" if is_chat else "com.example.jetnews"
        destination = "profile" if is_chat else "interests"
        outgoing = "Ali Conors (you)" if is_chat else "Interests"
        incoming = "composers" if is_chat else "Home"
        expectations = (["text=Ali Conors", "content_desc=Edit Profile"] if is_chat
                        else ["text=Topics", "text=People", "text=Publications"])
        goal_checks = [argument for selector in expectations for argument in ("--expect", selector)]
        self.run("fixture", ["android", "run", f"--device={self.args.serial}", f"--apks={self.args.apk}"])
        self.android_layout("fixture")
        seed = self.output / "seed"
        seed.mkdir()
        self.minimap("learning", seed, "init", "--no-skills", "--package", package)
        self.minimap("learning", seed, "whereami", "--label", "home")
        for label, text in [(destination, outgoing), ("home", incoming)]:
            opened = self.minimap("learning", seed, "tap", "--selector", "content_desc=Open navigation drawer", check=False)
            assert opened["status"] in {"ok", "needs_label"}
            learned = self.minimap("learning", seed, "tap", "--selector", f"text={text}", "--label", label)
            edge = json.loads((seed / ".minimap/graph/edges" / f"{learned['data']['edge']}.json").read_text())
            assert len(edge["recipe"]) == 2, "The drawer-opening action was lost"

        for repeat in range(self.args.repetitions):
            repo = self.copy_graph(seed, f"teammate-{repeat}")
            phase = f"replay-{repeat}"
            reached = self.minimap(phase, repo, "go", destination, *goal_checks)
            layout = self.android_layout("oracle")
            texts = {node.get("text") for node in layout}
            if is_chat:
                assert "Ali Conors" in texts
                assert any(node.get("content-desc") == "Edit Profile" for node in layout)
            else:
                assert {"Topics", "People", "Publications"} <= texts
            if is_chat:
                before = graph_digest(repo)
                wrong = self.minimap("wrong-instance", repo, "go", destination,
                                     "--expect", "text=Wrong person", check=False)
                assert wrong["status"] == "goal_mismatch", "A generic profile falsely proved a requested person"
                assert before == graph_digest(repo), "An unverified instance changed the shared graph"
                self.metadata.setdefault("wrong_instance", []).append({"status": wrong["status"], "graph_unchanged": True})
            self.minimap(phase, repo, "go", "home", "--expect",
                         "text=#composers" if is_chat else "text=Top stories for you")
            texts = {node.get("text") for node in self.android_layout("oracle")}
            assert ("#composers" if is_chat else "Top stories for you") in texts
            row = {"repeat": repeat + 1, "passed": reached["status"] == "ok", **self.measure(phase)}
            self.metadata.setdefault("journeys", []).append(row)
            print(json.dumps(row), flush=True)
        self.metadata["learning"] = self.measure("learning")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--sample", choices=["jetnews", "jetchat"], default="jetnews")
    args = parser.parse_args()
    args.binary, args.apk = args.binary.resolve(), args.apk.resolve()
    evaluation = JourneyEval(args)
    try:
        evaluation.execute()
    finally:
        evaluation.save()


if __name__ == "__main__":
    main()
