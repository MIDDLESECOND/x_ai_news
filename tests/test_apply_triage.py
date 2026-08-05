# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from apply_triage import apply_additions_text, apply_proposal, validate_proposal  # noqa: E402
from capture_identity import source_item_metadata  # noqa: E402


LEDGER = """# status history must survive exactly
claims:
  - id: first
    # 2026-08-03 open -> leaning-yes: reason
    status: leaning-yes
    evidence:
      - {src: old, type: vendor, verdict: old claim, link: "https://example.com/old", date: 2026-08-01}
    watch: "next test"
  - id: career
    status: open
    ledger_ref: C-1
    evidence: []
    watch: "external evidence only"
    career_implication:
      - "inference：do not rewrite"
"""


def addition(**overrides):
    value = {"claim_id": "first", "src": "test", "type": "controlled",
             "verdict": "支持但仍有替代解释", "link": "https://source.test/item",
             "date": "2026-08-04", "stance": "support",
             "source_item_id": "test-feed:0123456789abcdefabcd",
             "snapshot_hash": "a" * 64}
    value.update(overrides)
    return value


class ApplyTriageTest(unittest.TestCase):
    def write_capture(self, root, row):
        item = {"title": "captured", "url": row["link"], "published": row["date"],
                "summary": "source snapshot"}
        item_id, digest = source_item_metadata("test-feed", item)
        raw = root / "data" / "raw" / row["date"] / "test-feed.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps({"source": "test-feed", "name": "test",
                                   "tier": "community", "items": [item]}),
                       encoding="utf-8")
        row.update(source_item_id=item_id, snapshot_hash=digest)
        return row

    def test_minimal_insert_preserves_comments_and_status(self):
        updated, added, duplicates = apply_additions_text(LEDGER, [addition()])
        self.assertEqual((added, duplicates), (1, 0))
        self.assertIn("# 2026-08-03 open -> leaning-yes: reason", updated)
        parsed = yaml.safe_load(updated)
        first = parsed["claims"][0]
        self.assertEqual(first["status"], "leaning-yes")
        self.assertEqual(len(first["evidence"]), 2)
        self.assertIn("career_implication", parsed["claims"][1])

    def test_normalized_duplicate_is_not_written(self):
        duplicate = addition(src="old", type="vendor", verdict="old claim",
                             link="https://www.example.com/old/?utm_source=x",
                             date="2026-08-01")
        updated, added, duplicates = apply_additions_text(LEDGER, [duplicate])
        self.assertEqual((added, duplicates), (0, 1))
        self.assertEqual(updated, LEDGER)

    def test_same_url_with_a_new_snapshot_is_new_evidence(self):
        observation = addition(link="https://www.example.com/old/?utm_source=x",
                               date="2026-08-04", snapshot_hash="b" * 64)
        updated, added, duplicates = apply_additions_text(LEDGER, [observation])
        self.assertEqual((added, duplicates), (1, 0))
        self.assertIn('"date": "2026-08-04"', updated)

    def test_automatic_status_change_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不得立案或改判"):
            validate_proposal({"evidence_additions": [], "status_changes": [{"id": "first"}]})

    def test_non_clickable_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "http/https"):
            validate_proposal({"evidence_additions": [addition(link="local/report.md")]})

    def test_stance_and_capture_identity_are_required(self):
        for missing in ("stance", "source_item_id", "snapshot_hash"):
            row = addition()
            row.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                validate_proposal({"evidence_additions": [row]})

    def test_rewording_one_snapshot_does_not_create_more_evidence(self):
        first = addition()
        second = addition(src="renamed", verdict="改写后的判断", stance="neutral")
        _, added, duplicates = apply_additions_text(LEDGER, [first, second])
        self.assertEqual((added, duplicates), (1, 1))

    def test_career_ledger_claim_is_rejected_at_mutation_boundary(self):
        with self.assertRaisesRegex(ValueError, "职业正典关联悬案禁止自动写入"):
            apply_additions_text(LEDGER, [addition(claim_id="career")])

    def test_forged_capture_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "claims.yaml").write_text(LEDGER, encoding="utf-8")
            row = self.write_capture(root, addition())
            row["snapshot_hash"] = "f" * 64
            with self.assertRaisesRegex(ValueError, "无法在 data/raw"):
                apply_proposal(root, {"evidence_additions": [row]})

    def test_writer_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "claims.yaml").write_text(LEDGER, encoding="utf-8")
            proposal = {"evidence_additions": [self.write_capture(root, addition())]}
            self.assertEqual(apply_proposal(root, proposal), (1, 0))
            mtime = (root / "config" / "claims.yaml").stat().st_mtime_ns
            self.assertEqual(apply_proposal(root, proposal), (0, 1))
            self.assertEqual((root / "config" / "claims.yaml").stat().st_mtime_ns, mtime)


if __name__ == "__main__":
    unittest.main()
