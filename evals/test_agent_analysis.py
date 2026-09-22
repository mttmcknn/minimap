"""Full-usage comparisons must retain missing trials and verify original events."""
import json
from pathlib import Path
import tempfile
import unittest

from analyze_agent import analyze, digest
from paired_navigation import SAMPLES


PRICES = Path(__file__).parent / "prices/2026-09-22-gpt-6-astra-standard.json"


class AgentAnalysis(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.plan = {"planned_trials": 18, "model": "gpt-6-astra", "reasoning_effort": "xhigh",
                     "service_tier": "default", "ignore_user_config": True, "serial": "emulator-fixture",
                     "api": 36, "candidate_sha256": "binary", "price_record_sha256": digest(PRICES),
                     "seconds_per_trial": 180, "inputs_per_trial": 32,
                     "sources": {sample: {"files": {}} for sample in SAMPLES},
                     "apk_sha256": {sample: sample + "-apk" for sample in SAMPLES}, "trials": []}
        self.reports = []
        for sample in SAMPLES:
            for repeat in (1, 2, 3):
                for arm in ("raw", "reused_graph"):
                    identity = f"{sample}-{repeat}-{arm}"
                    folder = self.root / identity
                    (folder / "trial").mkdir(parents=True)
                    prompt = folder / "trial/PROMPT.md"; prompt.write_text(arm)
                    assignment = {"id": identity, "sample": sample, "repeat": repeat,
                                  "arm": arm, "prompt_sha256": digest(prompt), "graph_before": "graph"}
                    self.plan["trials"].append(assignment)
                    multiplier = 10 if arm == "raw" else 1
                    usage = {"input_tokens": 100 * multiplier, "cached_input_tokens": 60 * multiplier,
                             "output_tokens": 10 * multiplier}
                    (folder / "trial/events.jsonl").write_text(json.dumps({"type": "turn.completed", "usage": usage}) + "\n")
                    metadata = {key: self.plan[key] for key in ("model", "reasoning_effort", "service_tier", "ignore_user_config", "serial")}
                    metadata.update(api_level="36", binary_sha256="binary", apk_sha256=sample + "-apk",
                                    case_id=identity, sample=sample, arm=arm, prompt_sha256=digest(prompt),
                                    deadline_seconds=180, input_limit=32, price_record_sha256=digest(PRICES), graph_unchanged=True,
                                    graph_before="graph", codex_version="codex-fixture",
                                    agent_result={"answer": {"outcome": "completed"}, "exit_code": 0,
                                                  "timed_out": False, "oracle_reached_target": True,
                                                  "elapsed_seconds": 10 * multiplier, "input_actions": 2,
                                                  "usage": usage, "answer_error": None, "oracle_error": None})
                    path = folder / "results.json"
                    path.write_text(json.dumps({"metadata": metadata}))
                    self.reports.append(path)

    def result(self):
        return analyze(self.plan, self.root, PRICES)

    def change(self, mutate):
        path = self.reports[1]
        report = json.loads(path.read_text())
        mutate(report["metadata"])
        path.write_text(json.dumps(report))

    def test_complete_matching_usage_and_cost_reduce_without_double_counting(self):
        result = self.result()
        self.assertTrue(all(result["gates"].values()))
        self.assertEqual(result["successes"], 18)
        self.assertEqual(result["groups"][0]["median_tokens_saved"], 990)
        self.assertIsNone(result["subscription_charge"])

    def test_missing_failed_or_unknown_destination_blocks_claims(self):
        self.reports[0].unlink()
        self.change(lambda m: m["agent_result"].update(oracle_reached_target=None))
        result = self.result()
        self.assertEqual(result["planned"], 18)
        self.assertEqual(result["recorded"], 17)
        self.assertEqual(result["successes"], 16)
        self.assertFalse(result["gates"]["lower_median_tokens_every_app"])
        self.assertIsNone(result["trials"][0]["usage"])

    def test_usage_is_audited_against_events(self):
        self.change(lambda m: m["agent_result"]["usage"].update(input_tokens=90))
        result = self.result()
        self.assertFalse(result["gates"]["complete_usage"])
        self.assertFalse(result["gates"]["lower_median_tokens_every_app"])
        self.assertIsNone(result["trials"][1]["api_equivalent_cost"])
        self.assertIn("Usage differs from original events", result["trials"][1]["evidence_errors"])

    def test_mixed_model_or_prompt_cannot_be_pooled(self):
        self.change(lambda m: m.update(model="another-model"))
        with self.assertRaisesRegex(ValueError, "different model"):
            self.result()

    def test_timeout_and_graph_change_are_not_cheap_successes(self):
        self.change(lambda m: (m.update(graph_unchanged=False), m["agent_result"].update(timed_out=True)))
        result = self.result()
        self.assertEqual(result["successes"], 17)
        self.assertIsNone(result["trials"][1]["usage"])
        self.assertFalse(result["gates"]["lower_median_time_every_app"])

    def test_missing_event_evidence_stays_unknown(self):
        (self.reports[1].parent / "trial/events.jsonl").unlink()
        result = self.result()
        self.assertIsNone(result["trials"][1]["usage"])
        self.assertFalse(result["gates"]["lower_median_cost_even_at_bounds_every_app"])

    def test_missing_matching_source_prevents_a_verified_comparison(self):
        self.plan["sources"]["jetsnack"]["files"]["App.kt"] = "expected-source-hash"
        result = self.result()
        self.assertEqual(result["successes"], 12)
        self.assertFalse(result["gates"]["lower_median_tokens_every_app"])

    def test_codex_versions_cannot_change_between_arms(self):
        self.change(lambda m: m.update(codex_version="different-cli"))
        with self.assertRaisesRegex(ValueError, "different Codex versions"):
            self.result()

    def test_smaller_plan_cannot_claim_the_registered_goal(self):
        self.plan["trials"] = self.plan["trials"][:2]
        self.plan["planned_trials"] = 2
        result = self.result()
        self.assertTrue(result["gates"]["all_correct"])
        self.assertFalse(result["gates"]["registered_matrix_complete"])
        self.assertFalse(result["gates"]["lower_median_tokens_every_app"])


if __name__ == "__main__":
    unittest.main()
