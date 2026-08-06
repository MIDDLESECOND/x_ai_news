# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_story_lineage as lineage  # noqa: E402


def story(story_id, title, source_id, url, snapshot="a", *, external_url=""):
    return {
        "story_id": story_id,
        "title": title,
        "primary_source_item_id": f"{source_id}:0123456789abcdefabcd",
        "items": [{
            "source_item_id": f"{source_id}:0123456789abcdefabcd",
            "snapshot_hash": snapshot * 64,
            "source_id": source_id,
            "tier": "community",
            "title": title,
            "url": url,
            "external_url": external_url,
        }],
    }


class StoryLineageTest(unittest.TestCase):
    def test_identical_capture_repeats_in_same_lineage(self):
        first = story("story-1", "OpenAI launches new GPT coding model", "a",
                      "https://example.com/model")
        second = story("story-1", "OpenAI launches new GPT coding model", "a",
                       "https://example.com/model")

        result = lineage.build_lineages([
            {"day": "2026-08-04", "stories": [first]},
            {"day": "2026-08-05", "stories": [second]},
        ])

        self.assertEqual(result["lineage_count"], 1)
        self.assertEqual(result["entries"][1]["update_type"], "repeat")
        self.assertEqual(result["entries"][1]["link_reason"], "shared_source_item")

    def test_same_item_with_changed_snapshot_is_updated_capture(self):
        result = lineage.build_lineages([
            {"day": "2026-08-04", "stories": [
                story("story-1", "OpenAI pricing page", "a",
                      "https://example.com/pricing", snapshot="a")
            ]},
            {"day": "2026-08-05", "stories": [
                story("story-2", "OpenAI pricing page", "a",
                      "https://example.com/pricing", snapshot="b")
            ]},
        ])

        self.assertEqual(result["entries"][1]["update_type"], "updated-capture")

    def test_hn_and_direct_article_link_by_origin_url(self):
        first = story(
            "story-hn", "OpenAI launches new GPT coding model", "hn",
            "https://news.ycombinator.com/item?id=1",
            external_url="https://openai.com/index/model")
        second = story(
            "story-official", "OpenAI launches its GPT coding model", "official",
            "https://openai.com/index/model")

        result = lineage.build_lineages([
            {"day": "2026-08-04", "stories": [first]},
            {"day": "2026-08-05", "stories": [second]},
        ])

        self.assertEqual(result["lineage_count"], 1)
        self.assertEqual(result["entries"][1]["link_reason"], "shared_origin_url")
        self.assertEqual(result["entries"][1]["update_type"], "follow-up")

    def test_new_carrier_without_snapshot_change_is_follow_up(self):
        first = story("story-1", "OpenAI launches new GPT coding model", "official",
                      "https://openai.com/index/model")
        second = story("story-2", "OpenAI launches new GPT coding model", "official",
                       "https://openai.com/index/model")
        second["items"].append({
            "source_item_id": "hn:fedcba9876543210abcd",
            "snapshot_hash": "b" * 64,
            "source_id": "hn",
            "tier": "community",
            "title": "OpenAI launches new GPT coding model",
            "url": "https://news.ycombinator.com/item?id=2",
            "external_url": "https://openai.com/index/model",
        })

        result = lineage.build_lineages([
            {"day": "2026-08-04", "stories": [first]},
            {"day": "2026-08-05", "stories": [second]},
        ])

        self.assertEqual(result["entries"][1]["update_type"], "follow-up")

    def test_similar_title_links_only_inside_lookback(self):
        first = story("story-1", "OpenAI launches new GPT coding model", "a",
                      "https://a.example/one")
        second = story("story-2", "OpenAI launches its new GPT coding model", "b",
                       "https://b.example/two")

        linked = lineage.build_lineages([
            {"day": "2026-08-01", "stories": [first]},
            {"day": "2026-08-05", "stories": [second]},
        ], lookback_days=7)
        separated = lineage.build_lineages([
            {"day": "2026-08-01", "stories": [first]},
            {"day": "2026-08-05", "stories": [second]},
        ], lookback_days=3)

        self.assertEqual(linked["lineage_count"], 1)
        self.assertEqual(linked["entries"][1]["link_reason"], "title_similarity")
        self.assertEqual(separated["lineage_count"], 2)

    def test_same_day_stories_are_not_lineage_linked_again(self):
        result = lineage.build_lineages([{
            "day": "2026-08-05", "stories": [
                story("story-1", "OpenAI launches new GPT coding model", "a",
                      "https://a.example/one"),
                story("story-2", "OpenAI launches its new GPT coding model", "b",
                      "https://b.example/two"),
            ],
        }])

        self.assertEqual(result["lineage_count"], 2)
        self.assertTrue(all(entry["update_type"] == "new" for entry in result["entries"]))

    def test_generic_identical_titles_without_entity_anchor_do_not_link(self):
        result = lineage.build_lineages([
            {"day": "2026-08-04", "stories": [
                story("story-1", "not much happened today", "a",
                      "https://a.example/day-one")
            ]},
            {"day": "2026-08-05", "stories": [
                story("story-2", "not much happened today", "b",
                      "https://b.example/day-two")
            ]},
        ])

        self.assertEqual(result["lineage_count"], 2)
        self.assertEqual(result["entries"][1]["link_reason"], "new")

    def test_shared_generic_page_with_different_events_does_not_collide(self):
        result = lineage.build_lineages([{
            "day": "2026-08-05", "stories": [
                story("story-1", "API latency incident", "a",
                      "https://status.example.com/"),
                story("story-2", "Login outage incident", "b",
                      "https://status.example.com/"),
            ],
        }])

        self.assertEqual(result["lineage_count"], 2)
        self.assertEqual(len({entry["lineage_id"] for entry in result["entries"]}), 2)

    def test_boundary_forbids_claim_resolution_inference(self):
        result = lineage.build_lineages([])

        self.assertIn("不得据此推断纠正、解决", result["boundary"])


if __name__ == "__main__":
    unittest.main()
