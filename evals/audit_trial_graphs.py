#!/usr/bin/env python3
"""Check graph validity and mutation after completed paired device trials."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from compose_recovery import graph_digest


def audit(binary, reports):
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    checks = []
    for path in reports:
        run = json.loads(path.read_text())
        assert run["metadata"]["binary_sha256"] == binary_hash, "Use the frozen trial binary"
        for row in run["trials"]:
            name = f"{row['sample']}-{row['repeat']}-{row['arm']}"
            repo = path.parent / name
            seed = path.parent / f"seed-{row['sample']}"
            graph_present = (repo / ".minimap/config.json").is_file()
            before = graph_digest(repo)
            check = {key: row[key] for key in ["api", "sample", "repeat", "arm"]}
            check.update({"graph_present": graph_present, "graph_sha256": before})
            if graph_present:
                result = subprocess.run([str(binary), "doctor", "--repo-only"], cwd=repo,
                                        capture_output=True, text=True, timeout=30)
                try:
                    payload = json.loads(result.stdout)
                except ValueError:
                    payload = {}
                check["doctor_valid"] = result.returncode == 0 and payload.get("status") == "ok"
                check["doctor_read_only"] = before == graph_digest(repo)
                if not check["doctor_valid"]:
                    check["doctor_output"] = {"stdout": result.stdout, "stderr": result.stderr}
            if row["arm"] == "reused_graph" and graph_present:
                check["replay_graph_unchanged"] = before == graph_digest(seed)
            check["passed"] = (
                not graph_present if row["arm"] == "raw" else
                check.get("doctor_valid", False) and check.get("doctor_read_only", False)
                and check.get("replay_graph_unchanged", True)
            )
            checks.append(check)
    return {"binary_sha256": binary_hash, "checks": checks,
            "all_recorded_graph_checks_passed": all(check["passed"] for check in checks),
            "limits": ["Valid graph files do not establish the semantic correctness of an untested route.",
                       "Only recorded assignments are audited; the paired analyzer separately rejects missing trials."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), "Preserve previous audit results"
    result = audit(args.binary.resolve(), [path.resolve() for path in args.reports])
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"checks": len(result["checks"]), "passed": result["all_recorded_graph_checks_passed"]}))
    if not result["all_recorded_graph_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
