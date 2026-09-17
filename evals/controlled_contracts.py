#!/usr/bin/env python3
"""Recovery and negative controls using the frozen paired-suite binary/seeds."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from compose_recovery import RecoveryEval, graph_digest
from compose_relabel import RelabelEval
from paired_navigation import PairedEval, SAMPLES, verifies


class SettledRecovery(RecoveryEval):
    def reset_home(self):
        PairedEval.wait_for_start(self, {"home": ["content_desc=HOME"]}, "fixture")
        super().reset_home()


class NegativeEval(PairedEval):
    def execute(self):
        self.metadata["suite"] = "controlled-v1.2-negatives"
        self.metadata["checks"] = []
        spec = {**SAMPLES["jetchat"], "apk": self.args.apks / "jetchat-baseline.apk"}
        for repeat in range(3):
            phase = f"wrong-person-{repeat + 1}"
            record = {"case": phase, "passed": False}
            try:
                self.restart(spec)
                repo = self.copy_graph(self.args.paired_run / "seed-jetchat", phase)
                reached = self.go(phase, repo, spec)
                assert reached["status"] == "ok"
                assert verifies(self.android_layout("oracle"), spec["checks"])
                before = graph_digest(repo)
                wrong = self.minimap(phase, repo, "go", "profile", "--expect", "text=Wrong person", check=False)
                assert wrong["status"] == "goal_mismatch", "Wrong person falsely satisfied the goal"
                assert before == graph_digest(repo), "Wrong-person check changed the graph"
                assert verifies(self.android_layout("oracle"), spec["checks"])
                record.update({"passed": True, "status": wrong["status"], "graph_unchanged": True})
            except Exception as error:
                record["error"] = str(error)
            record.update(self.measure(phase))
            self.metadata["checks"].append(record)
            self.save()

        spec = {**SAMPLES["jetsnack"], "apk": self.args.apks / "jetsnack-baseline.apk"}
        phase = "external-navigation"
        record = {"case": phase, "passed": False}
        try:
            self.restart(spec)
            repo = self.copy_graph(self.args.paired_run / "seed-jetsnack", "roundtrip-seed")
            assert self.go("learning", repo, spec)["status"] == "ok"
            self.minimap("learning", repo, "tap", "--selector", "content_desc=HOME", "--label", "home")
            self.minimap("fixture", repo, "whereami")
            self.raw_tap("fixture", self.android_layout("fixture"), "content_desc=SEARCH")
            assert verifies(self.android_layout("oracle"), spec["checks"])
            before = graph_digest(repo)
            result = self.minimap(phase, repo, "go", "home", "--expect", "text=Android's picks", check=False)
            assert result["status"] == "ok" and verifies(self.android_layout("oracle"), spec["home"])
            assert before == graph_digest(repo)
            record.update({"passed": True, "status": result["status"], "graph_unchanged": True,
                           "planned_path": result["data"]["planned_path"]})
        except Exception as error:
            record["error"] = str(error)
        record.update(self.measure(phase))
        self.metadata["checks"].append(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["binary", "apks", "paired-run", "output"]:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    args = parser.parse_args()
    assert args.serial.startswith("emulator-")
    for name in ["binary", "apks", "paired_run", "output"]:
        setattr(args, name, getattr(args, name).resolve())
    paired = json.loads((args.paired_run / "results.json").read_text())
    binary_hash = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    assert paired["metadata"]["binary_sha256"] == binary_hash
    assert paired["metadata"]["serial"] == args.serial
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"binary_sha256": binary_hash, "serial": args.serial,
              "paired_report_sha256": hashlib.sha256((args.paired_run / "results.json").read_bytes()).hexdigest(),
              "apks": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(args.apks.glob("*.apk"))},
              "runs": []}

    def run(name, evaluator, **extra):
        options = SimpleNamespace(**{**vars(args), "output": args.output / name, "repetitions": 1,
                                     "apk": args.apks / "jetsnack-baseline.apk", "startup_seconds": 30,
                                     "host_seconds": 180, **extra})
        instance = evaluator(options)
        entry = {"name": name, "output": str(options.output), "completed": False}
        try:
            instance.execute()
            entry["completed"] = True
        except Exception as error:
            entry["error"] = str(error)
        finally:
            instance.save()
        entry["metadata"] = instance.metadata
        entry["results_sha256"] = hashlib.sha256((options.output / "results.json").read_bytes()).hexdigest()
        report["runs"].append(entry)
        (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")

    run("recovery", SettledRecovery)
    run("negative", NegativeEval)
    run("relabel", RelabelEval, seed=args.output / "negative/roundtrip-seed")
    report["passed"] = (
        all(row["completed"] for row in report["runs"])
        and len(report["runs"][0]["metadata"].get("scenarios", [])) == 3
        and all(row.get("passed") for row in report["runs"][0]["metadata"].get("scenarios", []))
        and len(report["runs"][1]["metadata"].get("checks", [])) == 4
        and all(row["passed"] for row in report["runs"][1]["metadata"].get("checks", []))
        and report["runs"][2]["metadata"].get("relabel", {}).get("passed", False)
    )
    (args.output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "output": str(args.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
