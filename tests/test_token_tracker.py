import unittest
from pathlib import Path
import tempfile
import json

from core.token_tracker import TokenTracker, calculate_cost

class TestTokenTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.file_path = Path(self.temp_dir.name) / "token_usage.json"
        self.tracker = TokenTracker(file_path=self.file_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_calculate_cost(self):
        # 1M prompt tokens with gemini-2.5-flash ($0.075/1M)
        cost_prompt = calculate_cost(1_000_000, 0, "google/gemini-2.5-flash")
        self.assertAlmostEqual(cost_prompt, 0.075, places=4)

        # 1M completion tokens ($0.30/1M)
        cost_comp = calculate_cost(0, 1_000_000, "google/gemini-2.5-flash")
        self.assertAlmostEqual(cost_comp, 0.30, places=4)

    def test_record_usage_and_persistence(self):
        stats = self.tracker.record_usage(
            prompt_tokens=4000,
            completion_tokens=250,
            model="google/gemini-2.5-flash",
            source="batch_nlp"
        )
        self.assertEqual(stats["total_prompt_tokens"], 4000)
        self.assertEqual(stats["total_completion_tokens"], 250)
        self.assertEqual(stats["total_tokens"], 4250)
        self.assertEqual(stats["total_requests"], 1)
        self.assertGreater(stats["total_cost_usd"], 0.0)

        # Load fresh instance from disk
        tracker2 = TokenTracker(file_path=self.file_path)
        stats2 = tracker2.load_stats()
        self.assertEqual(stats2["total_tokens"], 4250)
        self.assertIn("google/gemini-2.5-flash", stats2["by_model"])
        self.assertIn("batch_nlp", stats2["by_source"])

    def test_reset_stats(self):
        self.tracker.record_usage(1000, 50, "google/gemini-2.5-flash", "test")
        self.tracker.reset_stats()
        stats = self.tracker.load_stats()
        self.assertEqual(stats["total_tokens"], 0)
        self.assertEqual(stats["total_cost_usd"], 0.0)

if __name__ == "__main__":
    unittest.main()
