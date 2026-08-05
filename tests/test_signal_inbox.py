# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_monthly_claim_review import (candidate_gate, load_review_window,
                                        render_review)  # noqa: E402
from signal_inbox import add_records, canonical_url, load_month, validate_record  # noqa: E402


def record(day="2026-08-01", url="https://Example.com/a?utm_source=x", **overrides):
    value = {
        "date": day, "title": "具体实测", "url": url, "source_type": "n1-user",
        "matched_claim": "existing", "why_it_matters": "若复现会改变路由选择",
        "main_alternative": "可能是 harness 差异", "next_check": "固定任务复测",
        "action": "watch_signal",
    }
    value.update(overrides)
    return value


class SignalInboxTest(unittest.TestCase):
    def test_tracking_parameters_do_not_defeat_dedup(self):
        self.assertEqual(canonical_url("https://www.Example.com/a/?utm_source=x#top"),
                         "https://example.com/a")

    def test_duplicate_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            added, duplicates, path = add_records(root, [record()])
            self.assertEqual((added, duplicates), (1, 0))
            mtime = path.stat().st_mtime_ns
            added, duplicates, _ = add_records(root, [record(url="https://example.com/a")])
            self.assertEqual((added, duplicates), (0, 1))
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual(len(load_month(path)), 1)

    def test_same_url_on_a_later_day_is_a_new_observation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = record(day="2026-08-01")
            second = record(day="2026-08-02", url="https://example.com/a")
            added, duplicates, path = add_records(root, [first, second])
            self.assertEqual((added, duplicates), (2, 0))
            self.assertEqual(len(load_month(path)), 2)

    def test_duplicate_records_in_one_batch_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            added, duplicates, path = add_records(
                root, [record(), record(url="https://example.com/a")])
            self.assertEqual((added, duplicates), (1, 1))
            self.assertEqual(len(load_month(path)), 1)

    def test_review_window_crosses_month_shards(self):
        with tempfile.TemporaryDirectory() as td:
            inbox_root = Path(td)
            july = inbox_root / "2026-07.jsonl"
            august = inbox_root / "2026-08.jsonl"
            july.write_text(
                __import__("json").dumps(validate_record(record(day="2026-07-31"))) + "\n",
                encoding="utf-8")
            august.write_text(
                __import__("json").dumps(validate_record(record(day="2026-08-01"))) + "\n",
                encoding="utf-8")
            rows, start, end = load_review_window(inbox_root, "2026-08", 60)
            self.assertEqual([row["date"] for row in rows],
                             ["2026-07-31", "2026-08-01"])
            self.assertEqual((start.isoformat(), end.isoformat()),
                             ("2026-07-03", "2026-08-31"))

    def test_candidate_requires_key(self):
        with self.assertRaisesRegex(ValueError, "candidate_key"):
            validate_record(record(action="claim_candidate", matched_claim=None))

    def test_candidate_gate_requires_recurrence_diversity_and_independence(self):
        rows = [validate_record(record(
            day=f"2026-08-0{i}", url=f"https://source{i}.test/a",
            source_type=("vendor" if i == 1 else "controlled"), matched_claim=None,
            candidate_key="new-question", action="claim_candidate")) for i in range(1, 4)]
        passed, stats = candidate_gate(rows)
        self.assertTrue(passed, stats)

    def test_career_claim_is_labeled_not_promoted(self):
        rows = [validate_record(record(matched_claim="career"))]
        text = render_review("2026-08", rows, [{
            "id": "career", "claim": "职业相关外部证据", "status": "open", "ledger_ref": "C-1"
        }])
        self.assertIn("只建议复查正典，不得自动改判", text)


if __name__ == "__main__":
    unittest.main()
