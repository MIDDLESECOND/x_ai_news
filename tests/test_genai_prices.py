# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_l1  # noqa: E402
import build_digest  # noqa: E402


DATA = [{
    "id": "deepseek",
    "name": "DeepSeek",
    "pricing_urls": ["https://api-docs.deepseek.com/quick_start/pricing"],
    "models": [
        {
            "id": "deepseek-v4-flash",
            "name": "DeepSeek V4 Flash",
            "context_window": 1_000_000,
            "prices": {"input_mtok": 0.14, "output_mtok": 0.28},
        },
        {
            "id": "deepseek-chat",
            "name": "DeepSeek Chat",
            "prices": [
                {"prices": {"input_mtok": 0.135, "output_mtok": 0.55}},
                {"constraint": {"start_time": "00:30:00Z"},
                 "prices": {"input_mtok": 0.27, "output_mtok": 1.1}},
            ],
        },
    ],
}, {
    "id": "other",
    "name": "Other",
    "models": [{"id": "unrelated", "prices": {"input_mtok": 1}}],
}]


class GenAIPricesTest(unittest.TestCase):
    def test_extract_filters_models_and_keeps_conditional_prices(self):
        snapshot = fetch_l1.extract_genai_price_snapshot(DATA, ["DeepSeek V4", "deepseek chat"])

        self.assertEqual(set(snapshot), {
            "deepseek/deepseek-v4-flash", "deepseek/deepseek-chat",
        })
        flash = snapshot["deepseek/deepseek-v4-flash"]
        self.assertEqual(flash["prices"]["input_mtok"], 0.14)
        self.assertEqual(flash["pricing_url"],
                         "https://api-docs.deepseek.com/quick_start/pricing")
        self.assertEqual(len(snapshot["deepseek/deepseek-chat"]["prices"]), 2)

    def test_metadata_only_change_does_not_emit_price_change(self):
        old = fetch_l1.extract_genai_price_snapshot(DATA, ["deepseek-v4-flash"])
        changed = {key: dict(value, context_window=2_000_000) for key, value in old.items()}

        items = fetch_l1.diff_genai_price_snapshots(old, changed, "2026-08-05T00:00:00Z")

        self.assertEqual(items, [])

    def test_diff_reports_new_models_and_price_changes_as_index_evidence(self):
        old = fetch_l1.extract_genai_price_snapshot(DATA, ["deepseek-v4-flash"])
        current = {key: dict(value) for key, value in old.items()}
        current["deepseek/deepseek-v4-flash"]["prices"] = {
            "input_mtok": 0.2, "output_mtok": 0.4,
        }
        current["deepseek/deepseek-v4-pro"] = {
            "provider_id": "deepseek", "provider_name": "DeepSeek",
            "model_id": "deepseek-v4-pro", "model_name": "DeepSeek V4 Pro",
            "prices": {"input_mtok": 0.4, "output_mtok": 0.8},
            "pricing_url": "https://api-docs.deepseek.com/quick_start/pricing",
            "context_window": 1_000_000,
        }

        items = fetch_l1.diff_genai_price_snapshots(old, current, "2026-08-05T00:00:00Z")

        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["url"].startswith(
            "https://github.com/pydantic/genai-prices/blob/main/prices/providers/")
            for item in items))
        self.assertEqual(len({item["url"] for item in items}), 2)
        self.assertTrue(any("price change" in item["title"] for item in items))
        self.assertTrue(any("new model" in item["title"] for item in items))
        self.assertTrue(all("聚合价格索引" in item["summary"] for item in items))
        _, classified = build_digest.classify([{
            "source": "genai_prices", "name": "genai-prices", "tier": "index",
            "items": items,
        }], {"model_keywords": ["DeepSeek"], "topics": {}}, "2026-08-05", 3)
        self.assertEqual(len(classified), 2)

    def test_conditional_price_change_shows_changed_values(self):
        old = fetch_l1.extract_genai_price_snapshot(DATA, ["deepseek-chat"])
        current = {key: dict(value) for key, value in old.items()}
        current["deepseek/deepseek-chat"]["prices"] = [
            {"prices": {"input_mtok": 0.2, "output_mtok": 0.6}},
            {"constraint": {"start_time": "00:30:00Z"},
             "prices": {"input_mtok": 0.4, "output_mtok": 1.2}},
        ]

        item = fetch_l1.diff_genai_price_snapshots(
            old, current, "2026-08-05T00:00:00Z")[0]

        self.assertIn("input_mtok=$0.135/M", item["title"])
        self.assertIn("input_mtok=$0.2/M", item["title"])
        self.assertNotIn("2 组条件/阶梯价格 → 2 组条件/阶梯价格", item["title"])

    def test_genai_fetch_does_not_truncate_events_before_advancing_state(self):
        data = [{
            "id": "provider", "name": "Provider",
            "models": [
                {"id": f"gpt-{index}", "name": f"GPT {index}",
                 "prices": {"input_mtok": index}}
                for index in range(40)
            ],
        }]
        response = unittest.mock.Mock()
        response.json.return_value = data
        with (patch.object(fetch_l1, "http_get", return_value=response),
              patch.object(fetch_l1, "load_state", return_value={
                  "provider/gpt-0": {"prices": {"input_mtok": -1}},
              }),
              patch.object(fetch_l1, "save_state") as save_state):
            items = fetch_l1.fetch_genai_prices({
                "url": "https://example.test/data.json",
                "_model_keywords": ["GPT"],
            })

        self.assertEqual(len(items), 40)
        self.assertEqual(len(save_state.call_args.args[1]), 40)

    def test_empty_genai_snapshot_is_rejected_without_overwriting_state(self):
        response = unittest.mock.Mock()
        response.json.return_value = []
        with (patch.object(fetch_l1, "http_get", return_value=response),
              patch.object(fetch_l1, "load_state", return_value={"old": {}}),
              patch.object(fetch_l1, "save_state") as save_state):
            with self.assertRaisesRegex(RuntimeError, "空价格快照"):
                fetch_l1.fetch_genai_prices({
                    "url": "https://example.test/data.json",
                    "_model_keywords": ["GPT"],
                })

        save_state.assert_not_called()

    def test_openrouter_no_longer_truncates_or_accepts_empty_snapshot(self):
        response = unittest.mock.Mock()
        response.json.return_value = {"data": [
            {"id": f"gpt-{index}", "name": f"GPT {index}",
             "pricing": {"prompt": "0.1", "completion": "0.2"}}
            for index in range(25)
        ]}
        with (patch.object(fetch_l1, "http_get", return_value=response),
              patch.object(fetch_l1, "load_state", return_value={
                  "gpt-0": {"prompt": -1, "completion": -1},
              }),
              patch.object(fetch_l1, "save_state")):
            items = fetch_l1.fetch_openrouter_prices({
                "url": "https://example.test/models",
                "_model_keywords": ["GPT"],
            })
        self.assertEqual(len(items), 25)

        response.json.return_value = {"data": []}
        with (patch.object(fetch_l1, "http_get", return_value=response),
              patch.object(fetch_l1, "load_state", return_value={"old": {}}),
              patch.object(fetch_l1, "save_state") as save_state):
            with self.assertRaisesRegex(RuntimeError, "空价格快照"):
                fetch_l1.fetch_openrouter_prices({
                    "url": "https://example.test/models",
                    "_model_keywords": ["GPT"],
                })
        save_state.assert_not_called()

    def test_invalid_top_level_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "顶层必须是 provider 数组"):
            fetch_l1.extract_genai_price_snapshot({"providers": []}, ["GPT"])

    def test_structurally_collapsed_snapshot_is_rejected(self):
        previous = {f"provider/model-{index}": {} for index in range(10)}
        current = {"provider/model-0": {}, "provider/model-1": {}}

        with self.assertRaisesRegex(RuntimeError, "仅保留旧基线 2/10"):
            fetch_l1.validate_price_snapshot("prices", previous, current)


if __name__ == "__main__":
    unittest.main()
