# -*- coding: utf-8 -*-
"""信源发现按唯一推文计数，并落实实测特征晋升门槛。"""
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_digest as bd  # noqa: E402


def payload(*links):
    return [{"items": [{"x_links": list(links)}]}]


class CandidateDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_root = bd.ROOT
        bd.ROOT = Path(self.tmp.name)

    def tearDown(self):
        bd.ROOT = self.old_root
        self.tmp.cleanup()

    def test_same_status_on_multiple_days_counts_once(self):
        link = "x.com/Example/status/100"
        ledger, _ = bd.update_candidates_ledger(payload(link), {}, "2026-08-01")
        self.assertEqual(ledger["example"]["count"], 1)

        ledger, _ = bd.update_candidates_ledger(payload(link), {}, "2026-08-02")
        self.assertEqual(ledger["example"]["count"], 1)
        self.assertEqual(ledger["example"]["status_ids"], ["100"])

    def test_distinct_statuses_on_one_day_each_count(self):
        ledger, _ = bd.update_candidates_ledger(
            payload("x.com/Example/status/100", "x.com/example/status/101"),
            {}, "2026-08-01")
        self.assertEqual(ledger["example"]["count"], 2)
        self.assertEqual(ledger["example"]["status_ids"], ["100", "101"])

    def test_legacy_daily_count_is_migrated_to_unique_examples(self):
        state = Path(self.tmp.name) / "data" / "candidates_ledger.json"
        state.parent.mkdir(parents=True)
        state.write_text(
            '{"example":{"handle":"Example","count":5,'
            '"days":["2026-08-01","2026-08-02","2026-08-03"],'
            '"examples":["x.com/Example/status/100"]}}',
            encoding="utf-8")

        ledger, _ = bd.update_candidates_ledger([], {}, "2026-08-04")
        self.assertEqual(ledger["example"]["count"], 1)
        self.assertEqual(ledger["example"]["status_ids"], ["100"])

    def test_frequency_without_test_signal_is_not_nominated(self):
        links = [f"x.com/Example/status/{i}" for i in range(100, 103)]
        ledger, authors = bd.update_candidates_ledger(
            payload(*links), {}, "2026-08-01")
        rendered = "\n".join(bd.candidates_section(ledger, authors))
        self.assertIn("未验证实测特征", rendered)
        self.assertNotIn("可晋升", rendered)

    def test_manual_test_signal_and_three_unique_occurrences_is_nominated(self):
        accounts = {"candidates": [{
            "handle": "Example", "score": 1, "has_test_signal": True,
            "first_seen": "2026-08-01",
            "provenance": "两条人工确认记录（x.com/Example/status/100）",
        }]}
        ledger, authors = bd.update_candidates_ledger(
            payload("x.com/Example/status/101", "x.com/Example/status/102"),
            accounts, "2026-08-02")
        rendered = "\n".join(bd.candidates_section(ledger, authors))
        self.assertEqual(ledger["example"]["count"], 3)
        self.assertIn("可晋升 seed", rendered)
        self.assertIn("@Example", rendered)


if __name__ == "__main__":
    unittest.main()
