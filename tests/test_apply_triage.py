# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from apply_triage import apply_additions_text, apply_proposal, validate_proposal  # noqa: E402


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
             "date": "2026-08-04"}
    value.update(overrides)
    return value


class ApplyTriageTest(unittest.TestCase):
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

    def test_same_url_on_a_later_day_is_new_evidence(self):
        observation = addition(link="https://www.example.com/old/?utm_source=x",
                               date="2026-08-04")
        updated, added, duplicates = apply_additions_text(LEDGER, [observation])
        self.assertEqual((added, duplicates), (1, 0))
        self.assertIn('"date": "2026-08-04"', updated)

    def test_automatic_status_change_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不得立案或改判"):
            validate_proposal({"evidence_additions": [], "status_changes": [{"id": "first"}]})

    def test_non_clickable_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "http/https"):
            validate_proposal({"evidence_additions": [addition(link="local/report.md")]})

    def test_writer_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "claims.yaml").write_text(LEDGER, encoding="utf-8")
            proposal = {"evidence_additions": [addition()]}
            self.assertEqual(apply_proposal(root, proposal), (1, 0))
            mtime = (root / "config" / "claims.yaml").stat().st_mtime_ns
            self.assertEqual(apply_proposal(root, proposal), (0, 1))
            self.assertEqual((root / "config" / "claims.yaml").stat().st_mtime_ns, mtime)


if __name__ == "__main__":
    unittest.main()
