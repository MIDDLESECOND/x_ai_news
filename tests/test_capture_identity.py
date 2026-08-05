# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from capture_identity import (source_collection_metadata,
                              validate_capture_bindings)  # noqa: E402


class CaptureIdentityTest(unittest.TestCase):
    def make_capture(self, td):
        root = Path(td)
        (root / "config").mkdir()
        (root / "config/sources.yaml").write_text(
            "sources:\n"
            "  - id: feed\n"
            "    url: https://api.example.com/items\n"
            "    audit_urls: [https://example.com/feed]\n",
            encoding="utf-8")
        payload = {
            "source": "feed", "name": "Feed", "tier": "official",
            "fetched_at": "2026-08-05T00:00:00Z",
            "items": [{"title": "item", "url": "https://example.com/item",
                       "summary": "same", "changed": True,
                       "matched_query": "temporary"}],
        }
        raw = root / "data/raw/2026-08-05/feed.json"
        raw.parent.mkdir(parents=True)
        raw.write_text(json.dumps(payload), encoding="utf-8")
        return root, payload

    def test_collection_requires_configured_exact_audit_url(self):
        with tempfile.TemporaryDirectory() as td:
            root, payload = self.make_capture(td)
            source_id, digest = source_collection_metadata(payload)
            base = {"date": "2026-08-05", "source_item_id": source_id,
                    "snapshot_hash": digest}
            validate_capture_bindings(
                root, [dict(base, link="https://example.com/feed")], url_key="link")
            with self.assertRaisesRegex(ValueError, "不是该抓取源配置的审计地址"):
                validate_capture_bindings(
                    root, [dict(base, link="https://example.com/unrelated")],
                    url_key="link")

    def test_collection_without_configured_audit_url_cannot_use_item_url(self):
        with tempfile.TemporaryDirectory() as td:
            root, payload = self.make_capture(td)
            (root / "config/sources.yaml").write_text(
                "sources: []\n", encoding="utf-8")
            source_id, digest = source_collection_metadata(payload)
            record = {
                "date": "2026-08-05",
                "source_item_id": source_id,
                "snapshot_hash": digest,
                "link": "https://example.com/item",
            }
            with self.assertRaisesRegex(ValueError, "未配置审计地址"):
                validate_capture_bindings(root, [record], url_key="link")


if __name__ == "__main__":
    unittest.main()
