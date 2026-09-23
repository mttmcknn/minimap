"""Frozen-cohort analysis must not turn missing or incorrect runs into speedups."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from analyze_replay import summarize


def report():
    rows = [{"api": "36", "sample": sample, "repeat": repeat, "arm": arm,
             "setup_passed": True, "reported_ok": True, "oracle_reached_target": True,
             "graph_unchanged": True, "error": None, "seconds": seconds}
            for sample in ("jetsnack", "jetnews", "jetchat") for repeat in (1, 2)
            for arm, seconds in (("raw", 10), ("baseline", 12), ("candidate", 8))]
    return {"metadata": {"suite": "replay-performance-v2", "stage": "pilot", "profiled": False,
                         "protocol_sha256": "protocol", "runner_sha256": "runner",
                         "binary_sha256_by_arm": {"baseline": "old", "candidate": "new"},
                         "repetitions": 2, "planned_trials": 18, "api_level": "36",
                         "timing_scope": "subprocess wall time"}, "trials": rows}


class ReplayAnalysis(unittest.TestCase):
    def analyze(self, *runs):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, run in enumerate(runs):
                path = Path(directory) / f"{index}.json"
                path.write_text(json.dumps(run))
                paths.append(path)
            return summarize(paths)

    def test_good_pilot_is_never_confirmation(self):
        result = self.analyze(report())
        self.assertEqual(result["successes"], 18)
        self.assertTrue(result["gates"]["lower_paired_median_than_both_controls_every_case"])
        self.assertFalse(result["gates"]["speed_goal_met"])
        self.assertIsNone(result["model_usage"])
        self.assertIsNone(result["api_cost_saving"])

    def test_missing_setup_failure_and_wrong_destination_remain_failures(self):
        run = report()
        run["trials"].pop()
        run["trials"][0].update(setup_passed=False, seconds=0)
        run["trials"][1].update(oracle_reached_target=False)
        result = self.analyze(run)
        self.assertEqual(result["planned_trials"], 18)
        self.assertEqual(result["recorded_trials"], 17)
        self.assertEqual(result["successes"], 15)
        self.assertEqual(result["confirmed_false_successes"], 1)
        self.assertFalse(any(result["gates"].values()))
        jet = next(group for group in result["groups"] if group["sample"] == "jetsnack")
        self.assertEqual(jet["arms"]["raw"]["median_successful_seconds"], 10)
        self.assertEqual(jet["paired"]["raw"]["blocks"], 1)

    def test_graph_change_or_unavailable_oracle_prevents_speed_claim(self):
        for change in ({"graph_unchanged": False}, {"oracle_reached_target": None}):
            run = report()
            run["trials"][2].update(change)
            result = self.analyze(run)
            self.assertEqual(result["successes"], 17)
            self.assertFalse(result["gates"]["all_correct_and_graphs_unchanged"])

    def test_mixed_binaries_protocols_or_duplicate_assignments_are_rejected(self):
        first = report()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.analyze(first, first)
        for field, value in (("stage", "confirmation"), ("profiled", True),
                             ("binary_sha256_by_arm", {"baseline": "old", "candidate": "different"}),
                             ("protocol_sha256", "different"), ("runner_sha256", "different")):
            second = copy.deepcopy(first)
            second["metadata"][field] = value
            with self.assertRaisesRegex(ValueError, "Cannot combine"):
                self.analyze(first, second)


if __name__ == "__main__":
    unittest.main()
