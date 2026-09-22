"""Accounting contracts for real usage, missing evidence, and API cost bounds."""
import json
from pathlib import Path
import tempfile
import unittest

from agent_cost import cost_bounds, normalize_usage, savings_bounds, usage_from_events


PRICES = json.loads((Path(__file__).parent / "prices/2026-09-22-gpt-6-astra-standard.json").read_text())


class UsageAccounting(unittest.TestCase):
    def events(self, events):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("\n".join(json.dumps(event) for event in events))
            return usage_from_events(path)

    def test_subcategories_are_not_extra_tokens_or_cost(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 600,
                 "cache_write_tokens": 100, "output_tokens": 200,
                 "reasoning_output_tokens": 150}
        cost = cost_bounds(usage, PRICES, "gpt-6-astra", "default", "short")
        self.assertEqual(cost["total_tokens"], 1200)
        # 300 ordinary input + 600 cache reads + 100 writes + 200 output.
        self.assertAlmostEqual(cost["api_equivalent_usd_min"], .01485)
        self.assertEqual(cost["api_equivalent_usd_min"], cost["api_equivalent_usd_max"])

    def test_missing_writes_and_context_are_bounded_not_assumed_free(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 200}
        cost = cost_bounds(usage, PRICES, "gpt-6-astra", "default")
        self.assertAlmostEqual(cost["api_equivalent_usd_min"], .0146)
        self.assertAlmostEqual(cost["api_equivalent_usd_max"], .0262)
        self.assertEqual(cost["unknown_fields"], ["cache_write_tokens", "per_request_context_tier"])
        self.assertIsNone(cost["subscription_charge"])

    def test_unknown_model_tier_or_missing_usage_has_no_price(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 200}
        for value, model, tier in ((None, "gpt-6-astra", "default"),
                                   (usage, "another-model", "default"),
                                   (usage, "gpt-6-astra", "fast"),
                                   ({"input_tokens": 3, "output_tokens": 1}, "gpt-6-astra", "default")):
            self.assertIsNone(cost_bounds(value, PRICES, model, tier))

    def test_malformed_counts_do_not_become_savings(self):
        for invalid in (True, -1, 2.5, None):
            self.assertIsNone(normalize_usage({"input_tokens": invalid, "output_tokens": 1}))
        self.assertIsNone(normalize_usage({"input_tokens": 4, "cached_input_tokens": 3,
                                          "cache_write_tokens": 2, "output_tokens": 1}))
        self.assertIsNone(normalize_usage({"input_tokens": 4, "output_tokens": 1,
                                          "reasoning_output_tokens": 2}))

    def test_optional_totals_require_every_turn(self):
        first = {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 5}
        second = {"input_tokens": 8, "output_tokens": 1}
        self.assertEqual(self.events([{"type": "turn.completed", "usage": first},
                                      {"type": "turn.completed", "usage": second}]),
                         {"input_tokens": 18, "output_tokens": 3})

    def test_null_optional_counts_remain_unknown(self):
        usage = {"input_tokens": 1000, "output_tokens": 200, "cached_input_tokens": 600,
                 "cache_write_tokens": None, "reasoning_output_tokens": None}
        cost = cost_bounds(usage, PRICES, "gpt-6-astra", "default")
        self.assertEqual(cost["total_tokens"], 1200)
        self.assertIn("cache_write_tokens", cost["unknown_fields"])

    def test_failed_truncated_or_duplicate_stream_is_not_complete_usage(self):
        completed = {"type": "turn.completed", "turn_id": "a",
                     "usage": {"input_tokens": 10, "output_tokens": 2}}
        for suffix in ({"type": "turn.failed"}, {"type": "error"}, completed,
                       {"type": "turn.completed", "usage": None}):
            self.assertIsNone(self.events([completed, suffix]))
        self.assertIsNone(self.events([{"type": "turn.started"}, completed, {"type": "turn.started"}]))

    def test_nested_usage_and_conflicting_flat_usage(self):
        usage = {"input_tokens": 10, "output_tokens": 4,
                 "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 2},
                 "output_tokens_details": {"reasoning_tokens": 3}}
        self.assertEqual(normalize_usage(usage), {"input_tokens": 10, "output_tokens": 4,
                                                 "cached_input_tokens": 5, "cache_write_tokens": 2,
                                                 "reasoning_output_tokens": 3})
        self.assertIsNone(normalize_usage(dict(usage, cached_input_tokens=9)))

    def test_malformed_turn_identity_is_unknown_usage(self):
        for identity in ([], {}, True, 1, ""):
            self.assertIsNone(self.events([{
                "type": "turn.completed", "turn_id": identity,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }]))

    def test_savings_interval_can_cross_zero(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 200}
        cost = cost_bounds(usage, PRICES, "gpt-6-astra", "default")
        saved = savings_bounds(cost, cost)
        self.assertLess(saved["api_equivalent_usd_saved_min"], 0)
        self.assertGreater(saved["api_equivalent_usd_saved_max"], 0)
        self.assertEqual(saved["total_tokens_saved"], 0)
        self.assertIsNone(savings_bounds(cost, None))


if __name__ == "__main__":
    unittest.main()
