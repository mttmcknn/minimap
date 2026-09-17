#!/usr/bin/env python3
"""Exercise real Git merges of learned graph additions without a device.

Inputs are preserved graph snapshots from compose_recovery.py. Only newly
learned files are merged in the disjoint case; concurrent edits to existing
places are tested separately as an explicit conflict, never silently resolved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from compose_recovery import graph_digest


def execute(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = output / "repo"
    shutil.copytree(args.seed / ".minimap", repo / ".minimap")
    calls = []
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def run(argv, check=True):
        result = subprocess.run([str(arg) for arg in argv], cwd=repo, env=env,
                                capture_output=True, text=True, timeout=30)
        calls.append({"argv": [str(arg) for arg in argv], "exit_code": result.returncode,
                      "stdout": result.stdout, "stderr": result.stderr})
        if check and result.returncode:
            raise RuntimeError(f"Command failed: {argv}: {result.stderr}")
        return result

    def git(*argv, check=True):
        return run([args.git, *argv], check)

    report = {"binary_sha256": hashlib.sha256(args.binary.read_bytes()).hexdigest(),
              "source_graph_sha256": graph_digest(args.seed), "calls": calls}
    try:
        git("init", "-b", "main")
        git("config", "user.name", "Minimap evaluation")
        git("config", "user.email", "minimap-eval@example.invalid")
        git("add", ".minimap")
        git("commit", "-m", "Seed from verified Compose navigation")
        seed_commit = git("rev-parse", "HEAD").stdout.strip()
        additions = {}
        for index, source in enumerate(args.additions):
            branch = f"addition-{index}"
            git("switch", "-c", branch, seed_commit)
            files = []
            for file in sorted((source / ".minimap/graph").rglob("*.json")):
                relative = file.relative_to(source)
                if (args.seed / relative).exists():
                    continue
                destination = repo / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, destination)
                files.append(str(relative))
            assert files, f"No learned additions in {source}"
            git("add", ".minimap")
            git("commit", "-m", f"Learned graph additions {index}")
            additions[branch] = files
        git("switch", "main")
        for branch in additions:
            git("merge", "--no-edit", branch)
        before = graph_digest(repo)
        valid = run([args.binary, "doctor", "--repo-only"])
        assert json.loads(valid.stdout)["status"] == "ok"
        assert before == graph_digest(repo), "Doctor modified a merged graph"
        assert all((repo / file).is_file() for files in additions.values() for file in files)
        report["disjoint_additions"] = {"passed": True, "files": additions,
                                          "merged_graph_sha256": before, "doctor_read_only": True}

        merged = git("rev-parse", "HEAD").stdout.strip()
        place_path = next((repo / ".minimap/graph/places").glob("*.json"))
        for branch, label in [("label-a", "first label"), ("label-b", "second label")]:
            git("switch", "-c", branch, merged)
            place = json.loads(place_path.read_text())
            place["label"] = label
            place["slug"] = label.replace(" ", "-")
            place_path.write_text(json.dumps(place, indent=2, sort_keys=True) + "\n")
            git("add", ".minimap")
            git("commit", "-m", label)
        git("switch", "label-a")
        conflict = git("merge", "--no-edit", "label-b", check=False)
        assert conflict.returncode != 0, "Contradictory place labels merged silently"
        assert "<<<<<<<" in place_path.read_text()
        before = graph_digest(repo)
        invalid = run([args.binary, "doctor", "--repo-only"], check=False)
        assert invalid.returncode != 0
        assert json.loads(invalid.stdout)["status"] != "ok"
        assert before == graph_digest(repo), "Doctor changed a conflicted graph"
        report["conflicting_edits"] = {"passed": True, "git_rejected": True,
                                        "doctor_rejected": True, "graph_unchanged": True}
        git("merge", "--abort")
    finally:
        (output / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "calls"}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--additions", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git", type=Path, default=Path(shutil.which("git") or "git"))
    args = parser.parse_args()
    args.binary, args.seed = args.binary.resolve(), args.seed.resolve()
    args.additions = [path.resolve() for path in args.additions]
    execute(args)


if __name__ == "__main__":
    main()
