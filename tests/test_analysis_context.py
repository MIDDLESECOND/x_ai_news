# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from build_analysis_context import (build_context, claim_broad_matches, claim_matches,
                                    enforce_budget, select_evidence)  # noqa: E402
from state_io import atomic_write_if_changed  # noqa: E402


class AnalysisContextTest(unittest.TestCase):
    def setUp(self):
        self.claims = [{
            "id": "matched",
            "claim": "Model X 是否稳定",
            "status": "open",
            "watch_keywords": ["Model X"],
            "watch": "等待固定任务复测",
            "evidence": [
                {"src": "old", "type": "vendor", "verdict": "官方宣称", "link": "x.test/a", "date": "2026-08-01"},
                {"src": "counter", "type": "controlled", "verdict": "反证：固定任务回落", "link": "x.test/b", "date": "2026-08-02"},
                {"src": "today", "type": "n1-user", "verdict": "今日复现", "link": "x.test/c", "date": "2026-08-04"},
            ],
        }, {
            "id": "unmatched",
            "claim": "另一悬案",
            "status": "open",
            "watch_keywords": ["never appears"],
            "evidence": [],
        }]
        self.hits = [{
            "title": "Model X failed again", "url": "https://example.test/1",
            "source": "test", "tier": "community", "tier_label": "社区一手",
            "summary": "fixed harness", "match_text": "Model X fixed harness",
            "injection_warning": False,
        }]

    def test_only_matched_claim_gets_detail(self):
        context = build_context("2026-08-04", self.claims, self.hits, {})
        self.assertEqual([c["id"] for c in context["active_claim_directory"]],
                         ["matched", "unmatched"])
        self.assertEqual([c["id"] for c in context["matched_claim_details"]], ["matched"])
        self.assertEqual(context["unmatched_active_claim_count"], 1)

    def test_selection_keeps_today_and_reversal(self):
        selected = select_evidence(self.claims[0], "2026-08-04", limit=2)
        self.assertEqual([e["src"] for e in selected], ["today", "counter"])

    def test_many_today_entries_do_not_crowd_out_reversal(self):
        claim = dict(self.claims[0], evidence=[
            {"src": f"today-{i}", "type": "vendor", "verdict": f"今日观察 {i}",
             "link": f"x.test/today-{i}", "date": "2026-08-04"}
            for i in range(10)
        ] + [{"src": "older-counter", "type": "controlled", "verdict": "反证：未复现",
              "link": "x.test/counter", "date": "2026-07-30"}])
        selected = select_evidence(claim, "2026-08-04", limit=8)
        self.assertIn("older-counter", [e["src"] for e in selected])

    def test_derived_match_requires_entity_and_concept_axes(self):
        axes = {"explicit": [], "entity": ["Model X"], "concept": ["quota"]}
        self.assertFalse(claim_matches("Model X launched", axes))
        self.assertFalse(claim_matches("quota changed", axes))
        self.assertTrue(claim_matches("Model X quota changed", axes))
        self.assertTrue(claim_broad_matches("Model X launched", axes))
        self.assertFalse(claim_broad_matches("quota changed", axes))

    def test_broad_candidate_is_separate_and_not_given_evidence(self):
        claims = [dict(self.claims[0], watch_keywords=None, watch="等待 quota 复测")]
        topics = {"model_keywords": ["Model X"],
                  "topics": {"pricing": {"keywords": ["quota"]}}}
        context = build_context("2026-08-04", claims, self.hits, topics)
        self.assertEqual(context["matched_claim_details"], [])
        self.assertEqual(context["broad_claim_candidates"][0]["id"], "matched")
        self.assertNotIn("evidence_selected", context["broad_claim_candidates"][0])

    def test_budget_is_hard_and_declared(self):
        claims = [dict(self.claims[0], id=f"c{i}", watch="长" * 500,
                       watch_keywords=[f"Model {i}"]) for i in range(8)]
        hits = [dict(self.hits[0], match_text=f"Model {i}", title="T" * 200,
                     url=f"https://example.test/{i}") for i in range(8)]
        context = build_context("2026-08-04", claims, hits, {})
        data = enforce_budget(context, 9000)
        self.assertLessEqual(len(data), 9000)
        parsed = json.loads(data)
        self.assertTrue(parsed["truncated"])
        self.assertEqual(len(parsed["active_claim_directory"]), 8)

    def test_budget_records_omitted_broad_candidate(self):
        context = build_context("2026-08-04", self.claims, self.hits, {})
        context["broad_claim_candidates"] = [{
            "id": "broad", "candidate_signals_total": 1,
            "sample": {"title": "x" * 1000}, "policy": "recall-only",
        }]
        context["broad_candidate_count"] = 1
        before = len(json.dumps(context, ensure_ascii=False, indent=2,
                                sort_keys=True).encode("utf-8")) + 1
        parsed = json.loads(enforce_budget(context, before - 10))
        self.assertEqual(parsed["omitted_broad_claim_ids"], ["broad"])
        self.assertEqual(parsed["broad_candidate_count"], 1)

    def test_budget_removes_the_selected_largest_detail(self):
        context = build_context("2026-08-04", self.claims, self.hits, {})
        base = context["matched_claim_details"][0]
        large = dict(base, id="large", suspected_signals_total=10,
                     suspected_signals=[base["suspected_signals"][0]],
                     evidence_selected=[base["evidence_selected"][0]])
        small = dict(base, id="small", suspected_signals_total=1,
                     suspected_signals=[base["suspected_signals"][0]],
                     evidence_selected=[base["evidence_selected"][0]])
        context["matched_claim_details"] = [large, small]
        context["matched_claim_count"] = 2
        before = len(json.dumps(context, ensure_ascii=False, indent=2,
                                sort_keys=True).encode("utf-8")) + 1
        parsed = json.loads(enforce_budget(context, before - 10))
        self.assertIn("large", parsed["omitted_detail_claim_ids"])
        self.assertEqual([item["id"] for item in parsed["matched_claim_details"]],
                         ["small"])

    def test_atomic_writer_preserves_mtime_for_same_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            self.assertTrue(atomic_write_if_changed(path, b"same\n"))
            first = path.stat().st_mtime_ns
            self.assertFalse(atomic_write_if_changed(path, b"same\n"))
            self.assertEqual(path.stat().st_mtime_ns, first)


if __name__ == "__main__":
    unittest.main()
