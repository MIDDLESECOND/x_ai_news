# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_l1 as fl  # noqa: E402


FEED = b'''<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><item>
  <title>Release</title><link>https://example.test/release</link>
  <pubDate>Thu, 06 Aug 2026 10:00:00 GMT</pubDate>
  <description>Model release</description>
</item></channel></rss>'''


class FetchL1HttpCacheIntegrationTest(unittest.TestCase):
    def response(self):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://example.test/feed.xml"
        response.headers["ETag"] = '"v1"'
        response.headers["Content-Type"] = "application/rss+xml; charset=utf-8"
        response._content = FEED
        response.encoding = "utf-8"
        return response

    def test_second_same_day_run_writes_full_payload_from_cache_and_logs_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config/sources.yaml").write_text(
                "sources:\n"
                "  - id: official\n"
                "    name: Official\n"
                "    type: rss\n"
                "    tier: official\n"
                "    url: https://example.test/feed.xml\n",
                encoding="utf-8")
            (root / "config/topics.yaml").write_text("{}\n", encoding="utf-8")
            argv = ["fetch_l1.py", "--date", "2026-08-06"]
            with (patch.object(fl, "ROOT", root),
                  patch.object(fl, "STATE_DIR", root / "data/state"),
                  patch.object(fl, "HTTP_CACHE_ROOT", root / "data/state/http_cache"),
                  patch.object(sys, "argv", argv)):
                with patch.object(fl.requests, "get", return_value=self.response()) as get:
                    self.assertEqual(fl.main(), 0)
                get.assert_called_once()

                with patch.object(fl.requests, "get") as get:
                    self.assertEqual(fl.main(), 0)
                get.assert_not_called()

            raw = root / "data/raw/2026-08-06"
            payload = json.loads((raw / "official.json").read_text(encoding="utf-8"))
            log = json.loads((raw / "_fetch_log.json").read_text(encoding="utf-8"))
            self.assertEqual([item["title"] for item in payload["items"]], ["Release"])
            self.assertEqual(payload["retrieval"]["network_attempts"], 0)
            self.assertEqual(log["sources"]["official"]["status"], "cached")


if __name__ == "__main__":
    unittest.main()
