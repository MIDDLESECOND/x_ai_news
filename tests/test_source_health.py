# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_source_health as health  # noqa: E402


TOPICS = {
    "model_keywords": ["GPT"],
    "topics": {"release": {"section": "今日发布", "keywords": ["release"]}},
}
SOURCES = [
    {"id": "official", "name": "Official", "tier": "official",
     "type": "rss", "enabled": True},
    {"id": "community", "name": "Community", "tier": "community",
     "type": "rss", "enabled": True},
]


def payload(source_id, items, fetched_at):
    return {
        "source": source_id, "name": source_id, "tier": (
            "official" if source_id == "official" else "community"),
        "fetched_at": fetched_at, "items": items,
    }


def item(url, title="GPT release"):
    return {
        "title": title, "url": url, "summary": "release",
        "published": "2026-08-05T10:00:00Z",
    }


class SourceHealthTest(unittest.TestCase):
    def test_health_separates_attempts_skips_and_unique_items(self):
        samples = [
            {
                "day": "2026-08-04",
                "payloads": [payload("official", [item("https://a.test/1")],
                                     "2026-08-04T12:00:00Z")],
                "log": {"fetched_at": "2026-08-04T12:00:00Z", "sources": {
                    "official": {"status": "ok", "items": 1},
                    "community": {"status": "skipped"},
                }},
            },
            {
                "day": "2026-08-05",
                "payloads": [payload("official", [item("https://a.test/1")],
                                     "2026-08-05T12:00:00Z")],
                "log": {"fetched_at": "2026-08-05T12:00:00Z", "sources": {
                    "official": {"status": "error"},
                    "community": {"status": "cached"},
                }},
            },
        ]

        result = health.collect_health(SOURCES, TOPICS, samples)
        rows = {row["source_id"]: row for row in result["sources"]}

        self.assertEqual(rows["official"]["attempt_days"], 2)
        self.assertEqual(rows["official"]["success_days"], 1)
        self.assertEqual(rows["official"]["failure_days"], 1)
        self.assertEqual(rows["official"]["success_rate"], 0.5)
        self.assertEqual(rows["official"]["qualified_observations"], 2)
        self.assertEqual(rows["official"]["unique_qualified_items"], 1)
        self.assertEqual(rows["community"]["attempt_days"], 0)
        self.assertEqual(rows["community"]["skipped_days"], 1)
        self.assertEqual(rows["community"]["cached_days"], 1)
        self.assertEqual(rows["community"]["stale_days"], 0)
        self.assertEqual(rows["community"]["deferred_days"], 0)

    def test_deferred_cooldown_is_not_counted_as_network_attempt(self):
        samples = [{
            "day": "2026-08-05", "payloads": [],
            "log": {"fetched_at": "2026-08-05T12:00:00Z", "sources": {
                "official": {"status": "deferred"},
            }},
        }]
        rows = {row["source_id"]: row for row in
                health.collect_health(SOURCES, TOPICS, samples)["sources"]}
        self.assertEqual(rows["official"]["attempt_days"], 0)
        self.assertEqual(rows["official"]["deferred_days"], 1)

    def test_gone_is_tracked_separately_from_cooldown_and_attempts(self):
        samples = [{
            "day": "2026-08-05", "payloads": [],
            "log": {"fetched_at": "2026-08-05T12:00:00Z", "sources": {
                "official": {"status": "gone"},
            }},
        }]
        rows = {row["source_id"]: row for row in
                health.collect_health(SOURCES, TOPICS, samples)["sources"]}
        self.assertEqual(rows["official"]["attempt_days"], 0)
        self.assertEqual(rows["official"]["deferred_days"], 0)
        self.assertEqual(rows["official"]["gone_days"], 1)

    def test_partial_day_is_an_attempt_but_not_a_full_success(self):
        samples = [{
            "day": "2026-08-05", "payloads": [],
            "log": {"fetched_at": "2026-08-05T12:00:00Z", "sources": {
                "official": {"status": "partial"},
            }},
        }]
        rows = {row["source_id"]: row for row in
                health.collect_health(SOURCES, TOPICS, samples)["sources"]}
        self.assertEqual(rows["official"]["attempt_days"], 1)
        self.assertEqual(rows["official"]["partial_days"], 1)
        self.assertEqual(rows["official"]["success_days"], 0)
        self.assertEqual(rows["official"]["success_rate"], 0.0)

    def test_story_contribution_metrics_preserve_source_roles(self):
        samples = [{
            "day": "2026-08-05",
            "payloads": [
                payload("official", [item("https://a.test/shared")],
                        "2026-08-05T12:00:00Z"),
                payload("community", [item("https://a.test/shared")],
                        "2026-08-05T12:01:00Z"),
            ],
            "log": None,
        }]

        result = health.collect_health(SOURCES, TOPICS, samples)
        rows = {row["source_id"]: row for row in result["sources"]}

        self.assertEqual(rows["official"]["story_mentions"], 1)
        self.assertEqual(rows["community"]["story_mentions"], 1)
        self.assertEqual(rows["official"]["primary_stories"], 1)
        self.assertEqual(rows["community"]["primary_stories"], 0)
        self.assertEqual(rows["official"]["sole_source_stories"], 0)
        self.assertIn("不得自动晋退", result["boundary"])

    def test_report_states_sampling_boundary(self):
        result = health.collect_health(SOURCES, TOPICS, [], window_days=3)
        report = health.render_report(result, days=30)

        self.assertIn("含完整抓取日志", report)
        self.assertIn("不得自动晋退", report)


if __name__ == "__main__":
    unittest.main()
