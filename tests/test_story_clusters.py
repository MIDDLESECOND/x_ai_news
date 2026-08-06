# -*- coding: utf-8 -*-
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_digest  # noqa: E402
import build_story_clusters as stories  # noqa: E402


NOW = datetime(2026, 8, 5, 18, tzinfo=timezone.utc)
TOPICS = {
    "model_keywords": ["GPT", "Claude", "DeepSeek"],
    "topics": {
        "release": {"section": "今日发布", "keywords": ["release", "launch"]},
    },
}


def candidate(item_id, title, url, source_id, *, tier="community", hour=10):
    return {
        "title": title,
        "url": url,
        "source": source_id,
        "tier": tier,
        "tier_label": tier,
        "published": f"2026-08-05T{hour:02d}:00:00+00:00",
        "summary": title,
        "match_text": title,
        "source_item_id": f"{source_id}:{item_id:0>20}",
        "snapshot_hash": str(item_id)[-1:] * 64,
        "section": "今日发布",
    }


class StoryClusterTest(unittest.TestCase):
    def test_tracking_url_variants_merge_and_preserve_both_observations(self):
        items = [
            candidate(1, "OpenAI launches GPT model today",
                      "https://example.com/news?utm_source=rss", "official", tier="official"),
            candidate(2, "OpenAI launches GPT model today",
                      "https://example.com/news", "community"),
        ]

        clusters, events = stories.cluster_items(items, now=NOW)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["item_count"], 2)
        self.assertEqual(clusters[0]["source_count"], 2)
        self.assertEqual(len(clusters[0]["items"]), 2)
        self.assertEqual(events[0]["reason"], "canonical_url")
        self.assertEqual(clusters[0]["evidence_tiers"], {"community": 1, "official": 1})

    def test_similar_titles_merge_inside_time_window(self):
        items = [
            candidate(1, "OpenAI launches new GPT coding model", "https://a.test/1", "a", hour=10),
            candidate(2, "OpenAI launches its new GPT coding model", "https://b.test/2", "b", hour=12),
        ]

        clusters, events = stories.cluster_items(items, now=NOW)

        self.assertEqual(len(clusters), 1)
        self.assertEqual(events[0]["reason"], "title_similarity")
        self.assertGreaterEqual(events[0]["similarity"], 0.86)

    def test_different_vendors_do_not_merge_even_with_similar_wording(self):
        items = [
            candidate(1, "OpenAI launches new GPT coding model", "https://a.test/1", "a"),
            candidate(2, "Anthropic launches new Claude coding model", "https://b.test/2", "b"),
        ]

        clusters, events = stories.cluster_items(items, now=NOW)

        self.assertEqual(len(clusters), 2)
        self.assertEqual(events, [])

    def test_similar_titles_without_dates_do_not_bypass_time_window(self):
        first = candidate(
            1, "OpenAI launches new GPT coding model", "https://a.test/1", "a")
        second = candidate(
            2, "OpenAI launches its new GPT coding model", "https://b.test/2", "b")
        first["published"] = ""
        second["published"] = ""

        clusters, events = stories.cluster_items([first, second], now=NOW)

        self.assertEqual(len(clusters), 2)
        self.assertEqual(events, [])

    def test_same_source_generic_url_with_distinct_titles_does_not_merge(self):
        items = [
            candidate(1, "OpenAI releases GPT coding model today", "https://example.com/updates", "feed"),
            candidate(2, "DeepSeek releases model weights today", "https://example.com/updates", "feed"),
        ]

        clusters, events = stories.cluster_items(items, now=NOW)

        self.assertEqual(len(clusters), 2)
        self.assertEqual(events, [])

    def test_classify_can_retain_cross_source_duplicates_for_story_layer(self):
        payloads = [
            {"source": "a", "name": "A", "tier": "official", "items": [{
                "title": "OpenAI GPT release", "url": "https://example.com/news",
                "published": "2026-08-05T10:00:00Z", "summary": "release",
            }]},
            {"source": "b", "name": "B", "tier": "community", "items": [{
                "title": "OpenAI GPT release", "url": "https://example.com/news",
                "published": "2026-08-05T10:05:00Z", "summary": "release",
            }]},
        ]

        _, default_hits = build_digest.classify(payloads, TOPICS, "2026-08-05", 3)
        _, story_hits = build_digest.classify(
            payloads, TOPICS, "2026-08-05", 3, deduplicate_urls=False)

        self.assertEqual(len(default_hits), 1)
        self.assertEqual(len(story_hits), 2)

    def test_snapshot_timestamp_is_derived_from_inputs(self):
        payloads = [
            {"fetched_at": "2026-08-05T10:00:00Z"},
            {"fetched_at": "2026-08-05T12:30:00+00:00"},
        ]

        first = stories.source_snapshot_at(payloads, "2026-08-05")
        second = stories.source_snapshot_at(payloads, "2026-08-05")

        self.assertEqual(first, "2026-08-05T12:30:00+00:00")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
