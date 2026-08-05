# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_report_dossiers import build_dossiers, render_dossier  # noqa: E402


class ReportDossierTest(unittest.TestCase):
    def test_evidence_is_layered_and_unclickable_source_is_labeled(self):
        text = render_dossier({
            "id": "claim-one", "claim": "测试悬案", "status": "open", "watch": "复测",
            "evidence": [
                {"src": "lab", "type": "controlled", "verdict": "固定任务", "link": "https://lab.test/a", "date": "2026-08-01"},
                {"src": "local", "type": "report", "stance": "neutral", "verdict": "本地转述", "link": "D:\\private.md", "date": "2026-08-02",
                 "source_item_id": "local:item", "snapshot_hash": "a" * 64},
            ]})
        self.assertIn("受控/可复核测试", text)
        self.assertIn("媒体或研究报告", text)
        self.assertIn("不可点本地材料或待补原始出处", text)
        self.assertIn("不是 L2 专题裁决报告", text)
        self.assertIn("记录总数（不代表支持强度）", text)
        self.assertIn("中立背景 1", text)
        self.assertIn("旧记录未标注 1", text)

    def test_career_claim_is_skipped_and_final_report_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            final = reports / "2026-08-01-final.md"
            final.write_text("final", encoding="utf-8")
            claims = [
                {"id": "normal", "claim": "普通", "status": "open", "evidence": [], "watch": "x"},
                {"id": "career", "claim": "职业", "status": "open", "ledger_ref": "C-1", "evidence": [], "watch": "x"},
            ]
            changed, generated, skipped = build_dossiers(root, claims)
            self.assertEqual((generated, skipped), (1, 1))
            self.assertEqual(final.read_text(encoding="utf-8"), "final")
            self.assertTrue((reports / "dossiers" / "normal.md").exists())
            self.assertFalse((reports / "dossiers" / "career.md").exists())
            before = (reports / "dossiers" / "normal.md").stat().st_mtime_ns
            changed2, _, _ = build_dossiers(root, claims)
            self.assertEqual(changed2, 0)
            self.assertEqual((reports / "dossiers" / "normal.md").stat().st_mtime_ns, before)
            self.assertGreaterEqual(changed, 2)  # dossier + index


if __name__ == "__main__":
    unittest.main()
