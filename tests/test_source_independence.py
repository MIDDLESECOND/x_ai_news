# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_digest  # noqa: E402
import build_source_independence as independence  # noqa: E402


def observation(source_id, url, *, external_url="", tier="community"):
    return {
        "source_item_id": f"{source_id}:0123456789abcdefabcd",
        "snapshot_hash": "a" * 64,
        "source_id": source_id,
        "tier": tier,
        "url": url,
        "external_url": external_url,
    }


class SourceIndependenceTest(unittest.TestCase):
    def test_hn_carrier_and_official_page_share_one_origin_group(self):
        official = observation(
            "openai_blog", "https://openai.com/index/model-news/", tier="official")
        hn = observation(
            "hn", "https://news.ycombinator.com/item?id=123",
            external_url="https://www.openai.com/index/model-news?utm_source=hn")

        result = independence.analyze_story({
            "story_id": "story-1", "title": "Model news", "items": [official, hn],
        })

        self.assertEqual(result["coverage_count"], 2)
        self.assertEqual(result["observation_count"], 2)
        self.assertEqual(result["carrier_type_count"], 2)
        self.assertEqual(result["carrier_types"], ["direct", "hn"])
        self.assertEqual(result["estimated_independent_origin_count"], 1)
        self.assertEqual(result["observations"][1]["carrier"], "hn")
        self.assertEqual(result["observations"][1]["classification_basis"], "external_url")

    def test_two_distinct_publishers_remain_candidate_independent_origins(self):
        result = independence.analyze_story({
            "story_id": "story-2", "title": "Shared event", "items": [
                observation("a", "https://a.example/report"),
                observation("b", "https://b.example/report"),
            ],
        })

        self.assertEqual(result["estimated_independent_origin_count"], 2)
        self.assertEqual(result["origin_domains"], ["a.example", "b.example"])

    def test_payload_states_evidence_boundary(self):
        payload = independence.build_independence_payload({
            "date": "2026-08-05", "generated_at": "2026-08-05T12:00:00+00:00",
            "stories": [],
        })

        self.assertIn("不等于独立证实", payload["boundary"])

    def test_classification_preserves_external_url_for_derived_layers(self):
        payloads = [{
            "source": "hn", "name": "HN", "tier": "community", "items": [{
                "title": "GPT release", "url": "https://news.ycombinator.com/item?id=1",
                "external_url": "https://openai.com/index/release",
                "published": "2026-08-05T10:00:00Z", "summary": "release",
            }],
        }]
        topics = {"model_keywords": ["GPT"], "topics": {}}

        _, items = build_digest.classify(payloads, topics, "2026-08-05", 3)

        self.assertEqual(items[0]["external_url"], "https://openai.com/index/release")


if __name__ == "__main__":
    unittest.main()
