# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import backtest_classification as backtest  # noqa: E402


DAY = "2026-08-05"
BASELINE = {
    "model_keywords": [],
    "topics": {
        "release": {"section": "今日发布", "keywords": ["release"]},
        "pricing": {"section": "定价与额度变动", "keywords": ["price"]},
    },
}
CANDIDATE = {
    "model_keywords": [],
    "topics": {
        "release": {"section": "今日发布", "keywords": ["launch"]},
        "field": {"section": "一线实测", "keywords": ["price"]},
    },
}


def payload(items, source="feed"):
    return [{
        "source": source,
        "name": source,
        "tier": "community",
        "items": items,
    }]


def item(name, text):
    return {
        "title": text,
        "url": f"https://example.com/{name}",
        "published": f"{DAY}T10:00:00+00:00",
        "summary": text,
    }


class ClassificationBacktestTest(unittest.TestCase):
    def test_compare_detects_added_removed_and_section_moves(self):
        payloads = payload([
            item("removed", "model release"),
            item("added", "model launch"),
            item("moved", "price update"),
        ])

        result = backtest.compare_observations(
            [(DAY, payloads)], BASELINE, CANDIDATE, window_days=3)

        self.assertEqual(result["summary"], {
            "baseline_kept": 2,
            "candidate_kept": 2,
            "added": 1,
            "removed": 1,
            "section_moves": 1,
        })
        self.assertEqual(result["added"][0]["url"], "https://example.com/added")
        self.assertEqual(result["removed"][0]["url"], "https://example.com/removed")
        self.assertEqual(result["section_moves"][0]["baseline_section"], "定价与额度变动")
        self.assertEqual(result["section_moves"][0]["candidate_section"], "一线实测")
        self.assertEqual(result["per_source"]["feed"]["baseline"], 2)
        self.assertEqual(result["per_source"]["feed"]["candidate"], 2)

    def test_same_item_on_two_days_remains_two_observations(self):
        first = payload([item("same", "model release")])
        second_item = item("same", "model release")
        second_item["published"] = "2026-08-04T10:00:00+00:00"
        second = payload([second_item])

        result = backtest.compare_observations(
            [("2026-08-04", second), (DAY, first)], BASELINE, BASELINE, window_days=3)

        self.assertEqual(result["summary"]["baseline_kept"], 2)
        self.assertEqual(result["summary"]["candidate_kept"], 2)

    def test_baseline_and_candidate_can_use_different_classifier_versions(self):
        def baseline_classifier(payloads, topics, sample_day, window_days):
            del payloads, topics, sample_day, window_days
            return {section: [] for section in backtest.build_digest.SECTION_ORDER}, []

        result = backtest.compare_observations(
            [(DAY, payload([item("new", "model launch")]))],
            CANDIDATE, CANDIDATE, window_days=3,
            baseline_classifier=baseline_classifier,
            candidate_classifier=backtest.build_digest.classify,
        )

        self.assertEqual(result["summary"]["baseline_kept"], 0)
        self.assertEqual(result["summary"]["candidate_kept"], 1)
        self.assertEqual(result["summary"]["added"], 1)

    def test_load_raw_window_ignores_non_dates_and_outside_window(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw = root / "data" / "raw"
            for day in ("2026-08-02", "2026-08-04", "2026-08-05", "not-a-day"):
                folder = raw / day
                folder.mkdir(parents=True)
                (folder / "feed.json").write_text(json.dumps({
                    "source": "feed", "name": "feed", "tier": "community", "items": []
                }), encoding="utf-8")
                (folder / "_fetch_log.json").write_text("{}", encoding="utf-8")

            rows = backtest.load_raw_window(root, DAY, days=2)

        self.assertEqual([day for day, _ in rows], ["2026-08-04", "2026-08-05"])
        self.assertEqual(len(rows[0][1]), 1)

    def test_render_report_exposes_evidence_boundary(self):
        result = backtest.compare_observations(
            [(DAY, payload([item("added", "model launch")]))],
            BASELINE, CANDIDATE, window_days=3)

        text = backtest.render_report(result, end_day=DAY, days=14, baseline_label="HEAD")

        self.assertIn("分类规则历史回放", text)
        self.assertIn("只比较召回与归栏变化", text)
        self.assertIn("https://example.com/added", text)
        self.assertIn("逐信源", text)
        self.assertIn("分类器与 topics", text)


if __name__ == "__main__":
    unittest.main()
