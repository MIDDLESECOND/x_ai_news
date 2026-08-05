# -*- coding: utf-8 -*-
import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_monthly_claim_review import (candidate_gate, load_review_window,
                                        render_review)  # noqa: E402
from capture_identity import source_item_metadata  # noqa: E402
from signal_inbox import add_records, canonical_url, load_month, validate_record  # noqa: E402


def record(day="2026-08-01", url="https://Example.com/a?utm_source=x", **overrides):
    value = {
        "date": day, "title": "具体实测", "url": url, "source_type": "n1-user",
        "matched_claim": "existing", "why_it_matters": "若复现会改变路由选择",
        "main_alternative": "可能是 harness 差异", "next_check": "固定任务复测",
        "action": "watch_signal",
        "source_item_id": "test-feed:0123456789abcdefabcd",
        "snapshot_hash": "a" * 64,
    }
    value.update(overrides)
    return value


def captured_record(root, day="2026-08-01", url="https://Example.com/a?utm_source=x",
                    *, captured_title="具体实测", captured_published="2026-08-01",
                    **overrides):
    row = record(day=day, url=url, title=captured_title, **overrides)
    item = {"title": captured_title, "url": url, "published": captured_published,
            "summary": "captured source"}
    item_id, digest = source_item_metadata("test-feed", item)
    raw = root / "data" / "raw" / day / "test-feed.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    payload = ({"source": "test-feed", "name": "test", "tier": "community",
                "items": []} if not raw.exists()
               else json.loads(raw.read_text(encoding="utf-8")))
    payload["items"].append(item)
    raw.write_text(json.dumps(payload), encoding="utf-8")
    row.update(source_item_id=item_id, snapshot_hash=digest)
    return row


class SignalInboxTest(unittest.TestCase):
    def test_tracking_parameters_do_not_defeat_dedup(self):
        self.assertEqual(canonical_url("https://www.Example.com/a/?utm_source=x#top"),
                         "https://example.com/a")

    def test_duplicate_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = captured_record(root)
            added, duplicates, path = add_records(root, [first])
            self.assertEqual((added, duplicates), (1, 0))
            mtime = path.stat().st_mtime_ns
            duplicate = dict(first, url="https://example.com/a")
            added, duplicates, _ = add_records(root, [duplicate])
            self.assertEqual((added, duplicates), (0, 1))
            self.assertEqual(path.stat().st_mtime_ns, mtime)
            self.assertEqual(len(load_month(path)), 1)

    def test_same_url_only_becomes_new_when_snapshot_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = captured_record(root, day="2026-08-01", url="https://example.com/a")
            same = captured_record(root, day="2026-08-02", url="https://example.com/a")
            changed = captured_record(root, day="2026-08-02", url="https://example.com/a",
                                      captured_title="更新后的具体实测")
            added, duplicates, path = add_records(root, [first, same, changed])
            self.assertEqual((added, duplicates), (2, 1))
            self.assertEqual(len(load_month(path)), 2)

    def test_duplicate_records_in_one_batch_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = captured_record(root)
            duplicate = dict(first, url="https://example.com/a")
            added, duplicates, path = add_records(
                root, [first, duplicate])
            self.assertEqual((added, duplicates), (1, 1))
            self.assertEqual(len(load_month(path)), 1)

    def test_forged_capture_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            row = captured_record(root)
            row["snapshot_hash"] = "f" * 64
            with self.assertRaisesRegex(ValueError, "无法在 data/raw"):
                add_records(root, [row])

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
            candidate_key="new-question", action="claim_candidate",
            source_item_id=f"test-feed:item-{i}",
            snapshot_hash=f"{i:064x}")) for i in range(1, 4)]
        passed, stats = candidate_gate(rows)
        self.assertTrue(passed, stats)

    def test_career_claim_is_labeled_not_promoted(self):
        rows = [validate_record(record(matched_claim="career"))]
        text = render_review("2026-08", rows, [{
            "id": "career", "claim": "职业相关外部证据", "status": "open", "ledger_ref": "C-1"
        }])
        self.assertIn("只建议复查正典，不得自动改判", text)

    def test_previous_complete_month_and_as_of_are_explicit(self):
        from datetime import date
        from build_monthly_claim_review import previous_complete_month
        self.assertEqual(previous_complete_month(date(2026, 8, 1)), "2026-07")
        text = render_review("2026-07", [], [], as_of=date(2026, 7, 31))
        self.assertIn("证据截止（as_of）：2026-07-31", text)


if __name__ == "__main__":
    unittest.main()
