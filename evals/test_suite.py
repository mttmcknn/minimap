"""Evaluator regressions; run with python3 -m unittest discover -s evals -p test_suite.py."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_trial import PROXY, agent_spec, usage_from_events
from analyze_suite import analyze, break_even
from paired_navigation import PairedEval, verifies


class EvaluationContract(unittest.TestCase):
    def test_agent_destination_survives_a_legitimate_tab_rename(self):
        layout = [{"text": "Categories"}, {"text": "Lifestyles"}, {"text": "DISCOVER"}]
        self.assertTrue(verifies(layout, agent_spec("jetsnack")["checks"]))
        self.assertFalse(verifies([{"text": "Subtotal"}], agent_spec("jetsnack")["checks"]))

    def test_startup_wait_requires_home_and_has_a_deadline(self):
        evaluation = object.__new__(PairedEval)
        evaluation.args = SimpleNamespace(startup_seconds=3, serial="emulator-fixture")
        evaluation.metadata = {}
        clock = [0]
        layouts = iter(["", "[]", '[{"text":"Home"}]'])

        def capture(*args):
            clock[0] += 1
            return next(layouts)

        evaluation.run = capture
        with patch("paired_navigation.time.perf_counter", side_effect=lambda: clock[0]), patch("paired_navigation.time.sleep"):
            evaluation.wait_for_start({"home": ["text=Home"]}, "setup")
        self.assertEqual(evaluation.metadata["startup_checks"][0]["attempts"], 3)
        self.assertTrue(evaluation.metadata["startup_checks"][0]["ready"])

        layouts = iter(["[]"] * 3)
        with patch("paired_navigation.time.perf_counter", side_effect=lambda: clock[0]), patch("paired_navigation.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "Start-state oracle failed"):
                evaluation.wait_for_start({"home": ["text=Home"]}, "setup-failed")
        self.assertFalse(evaluation.metadata["startup_checks"][1]["ready"])
        self.assertEqual(evaluation.command_timeout, 60)

    def test_unique_visible_goal_anchors(self):
        self.assertTrue(verifies([{"text": "Alice"}], ["text=Alice"]))
        self.assertFalse(verifies([{"text": "Bob"}], ["text=Alice"]))
        self.assertFalse(verifies([{"text": "Alice"}, {"text": "Alice"}], ["text=Alice"]))
        self.assertFalse(verifies([{"text": "Alice", "off-screen": True}], ["text=Alice"]))

    def test_break_even_includes_initial_use(self):
        self.assertEqual(break_even(10, 30, 5), 5)
        self.assertIsNone(break_even(5, 30, 10))
        self.assertIsNone(break_even(10, 8, 12))
        self.assertIsNone(break_even(10, 8, 10))
        self.assertEqual(break_even(10, 8, 5), 1)

    def test_unstarted_trials_fail_quality_without_zero_time_speedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            trials = []
            for sample in ["jetsnack", "jetnews", "jetchat"]:
                for arm in ["raw", "new_graph", "reused_graph"]:
                    for repeat in [1, 2]:
                        trials.append({"api": "36", "sample": sample, "arm": arm, "repeat": repeat,
                                       "setup_passed": True, "reported_ok": True, "oracle_reached_target": True,
                                       "error": None, "seconds": 10, "stdout_bytes": 100, "commands": 3,
                                       "total_seconds_including_setup_oracle": 20})
            trials[0].update({"setup_passed": False, "reported_ok": False, "oracle_reached_target": False,
                              "error": "empty startup layout", "seconds": 0, "stdout_bytes": 0})
            path.write_text(json.dumps({"metadata": {"binary_sha256": "frozen", "api_level": "36"}, "trials": trials}))
            report = analyze([path], repetitions=2)
            self.assertFalse(report["gates"]["at_least_95_percent_each_arm"])
            raw = report["groups"][0]["arms"]["raw"]
            self.assertEqual(raw["median_seconds_started"], 10)
            self.assertEqual(raw["setup_failures"], 1)
            self.assertEqual(report["groups"][0]["paired_reuse_minus_raw"]["seconds"]["blocks"], 1)

    def test_missing_model_usage_is_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events"
            path.write_text('{"type":"turn.failed"}\n')
            self.assertIsNone(usage_from_events(path))
            path.write_text(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}) + "\n")
            self.assertEqual(usage_from_events(path), {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20})

    def test_analysis_rejects_mixed_protocols_and_duplicate_assignments(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / str(index) for index in range(2)]
            run = {"metadata": {"binary_sha256": "frozen", "api_level": "36", "suite": "v1"},
                   "trials": [{"api": "36", "sample": "jetsnack", "repeat": 1, "arm": "raw"}]}
            for path in paths:
                path.write_text(json.dumps(run))
            with self.assertRaisesRegex(AssertionError, "Duplicate trial"):
                analyze(paths)
            run["metadata"]["suite"] = "v2"
            paths[1].write_text(json.dumps(run))
            with self.assertRaisesRegex(AssertionError, "different fixture protocols"):
                analyze(paths)

    def test_bridge_input_budget_stops_before_the_extra_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proxy = root / "adb"
            proxy.write_text(PROXY)
            proxy.chmod(0o755)
            fake = root / "real-adb"
            fake.write_text('#!/bin/sh\nprintf "acted\\n" >> "$EVAL_TRIAL_ROOT/acted"\n')
            fake.chmod(0o755)
            (root / "inputs.jsonl").write_text('[]\n' * 31)
            env = dict(os.environ, EVAL_TRIAL_ROOT=str(root), EVAL_REAL_ADB=str(fake), ANDROID_SERIAL="emulator-fixture")
            args = [str(proxy), "-s", "emulator-fixture", "shell", "input", "tap", "10", "20"]
            self.assertEqual(subprocess.run(args, env=env, capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(args, env=env, capture_output=True).returncode, 0)
            self.assertEqual((root / "acted").read_text().splitlines(), ["acted"])
            self.assertEqual(len((root / "inputs.jsonl").read_text().splitlines()), 32)


if __name__ == "__main__":
    unittest.main()
