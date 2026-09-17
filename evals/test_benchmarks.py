"""Cost/provenance contracts; no plotting, tokenizer, network, or device needed."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from benchmark_data import (ARMS, ENCODINGS, SAMPLES, audit_calls, break_even,
                            cumulative_cost, load_verified, phase_metrics,
                            quality_rows, sha256, summarize)


class BenchmarkContract(unittest.TestCase):
    def test_learning_is_paid_once_and_the_first_use_is_included(self):
        self.assertEqual(cumulative_cost(1, 10, 30, 5), (10, 30))
        self.assertEqual(cumulative_cost(5, 10, 30, 5), (50, 50))
        self.assertEqual(break_even(10, 30, 5), 5)
        self.assertEqual(break_even(10, 30, 5, upfront=5), 6)
        self.assertEqual(break_even(10, 30, 5, per_use=2), 9)

    def test_instruction_frequency_can_remove_savings(self):
        self.assertEqual(break_even(1598, 886, 243, upfront=1480), 2)
        self.assertIsNone(break_even(1598, 886, 243, per_use=1480))
        self.assertEqual(break_even(1979, 760, 243, per_use=1480), 3)
        self.assertEqual(cumulative_cost(10, 1598, 886, 243, upfront=1480), (15980, 4553))
        self.assertEqual(cumulative_cost(10, 1598, 886, 243, per_use=1480), (15980, 17873))

    def test_transient_discount_is_not_sustained_break_even(self):
        self.assertIsNone(break_even(10, 8, 12))
        self.assertIsNone(break_even(10, 30, 10))
        self.assertEqual(break_even(10, 8, 10), 1)
        self.assertEqual(break_even(10, 10, 10), 1)
        for kwargs in ({"upfront": -1}, {"per_use": float("nan")}, {"upfront": float("inf")}):
            with self.assertRaises(ValueError):
                break_even(10, 20, 5, **kwargs)
        for uses in (0, -1, 1.5):
            with self.assertRaises(ValueError):
                cumulative_cost(uses, 10, 20, 5)

    def test_cost_effects_use_matched_pairs_and_reject_incomplete_data(self):
        rows = []
        seconds = {"raw": [8, 20, 10], "new_graph": [25, 25, 25], "reused_graph": [11, 10, 14]}
        for sample in SAMPLES:
            for arm in ARMS:
                for rep in range(1, 4):
                    rows.append({"api": "36", "sample": sample, "arm": arm, "repeat": rep,
                                 "success": True, "seconds": seconds[arm][rep - 1], "commands": 1,
                                 **{n: 100 if arm == "raw" else 30 for n in ENCODINGS}})
        skill = dict.fromkeys(ENCODINGS, 50)
        result = summarize(rows, skill)
        self.assertEqual([p["seconds"] for p in result[0]["paired"]], [3, -10, 4])
        self.assertEqual(result[0]["arms"]["reused_graph"]["seconds"], 11)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            summarize(rows + [rows[0]], skill)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            summarize(rows[1:], skill)
        invalid = copy.deepcopy(rows)
        invalid[0]["success"] = False
        with self.assertRaisesRegex(ValueError, "Unverified"):
            summarize(invalid, skill)

    def test_saved_streams_are_separate_and_totals_are_reconciled(self):
        class RecordingEncoder:
            def __init__(self):
                self.seen = []

            def encode_ordinary(self, text):
                self.seen.append(text)
                # A test double whose count changes if streams are concatenated.
                return text.split()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "000.stdout").write_text("hel")
            (root / "000.stderr").write_text("lo")
            encoders = {n: RecordingEncoder() for n in ENCODINGS}
            run = {"calls": [{"phase": "measured", "seconds": 2.5, "exit_code": 1,
                              "stdout_bytes": 3, "stderr_bytes": 2}]}
            calls = audit_calls(root / "results.json", run, encoders, "fixture")
            measured = phase_metrics(calls, "measured", {"seconds": 2.5, "commands": 1,
                                                         "stdout_bytes": 3, "stderr_bytes": 2})
            self.assertEqual(measured["o200k_base"], 2)
            self.assertEqual(encoders["o200k_base"].seen, ["hel", "lo"])
            self.assertEqual(calls[0]["exit_code"], 1)  # Failed calls remain in the ledger.
            self.assertEqual(calls[0]["stdout_sha256"], sha256(b"hel"))
            with self.assertRaisesRegex(ValueError, "does not reconcile"):
                phase_metrics(calls, "measured", {"seconds": 0, "commands": 1,
                                                  "stdout_bytes": 3, "stderr_bytes": 2})
            (root / "000.stdout").write_text("altered")
            with self.assertRaisesRegex(ValueError, "Byte count mismatch"):
                audit_calls(root / "results.json", run, encoders, "fixture")
            (root / "000.stdout").write_bytes(b"\xff\xff\xff")
            with self.assertRaises(UnicodeDecodeError):
                audit_calls(root / "results.json", run, encoders, "fixture")

    def test_modified_source_report_fails_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            content = b'{"source": 1}'
            path.write_bytes(content)
            self.assertEqual(load_verified(path, sha256(content)), {"source": 1})
            path.write_text(json.dumps({"source": 2}))
            with self.assertRaisesRegex(ValueError, "Source hash mismatch"):
                load_verified(path, sha256(content))

    def test_missing_and_setup_failed_assignments_keep_their_denominator(self):
        base = {"api": "36", "sample": "jetsnack", "repeat": 1, "setup_passed": True,
                "reported_ok": True, "oracle_reached_target": True, "error": None}
        original = {"trials": [{**base, "arm": "raw", "setup_passed": False},
                               {**base, "arm": "new_graph"}],
                    "groups": [{"api": "36", "sample": "jetsnack",
                                "arms": {a: {"planned": 1} for a in ARMS}}], "planned_device_trials": 3}
        followup = {**original, "trials": [{**base, "arm": a} for a in ARMS]}
        rows = quality_rows({"original_experiment": original, "primary": followup})
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["state"] for r in rows], ["setup_failed", "passed", "missing", "passed", "passed", "passed"])


if __name__ == "__main__":
    unittest.main()
