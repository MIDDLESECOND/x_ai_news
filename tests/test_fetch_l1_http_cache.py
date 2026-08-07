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

    def test_gone_source_is_logged_distinctly_and_not_polled_again(self):
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
            gone = requests.Response()
            gone.status_code = 410
            gone.url = "https://example.test/feed.xml"
            gone._content = b"gone"
            with (patch.object(fl, "ROOT", root),
                  patch.object(fl, "STATE_DIR", root / "data/state"),
                  patch.object(fl, "HTTP_CACHE_ROOT", root / "data/state/http_cache"),
                  patch.object(sys, "argv", argv)):
                with patch.object(fl.requests, "get", return_value=gone) as get:
                    self.assertEqual(fl.main(), 1)
                get.assert_called_once()

                first_log = json.loads(
                    (root / "data/raw/2026-08-06/_fetch_log.json").read_text(
                        encoding="utf-8"))
                self.assertEqual(
                    first_log["sources"]["official"]["status"], "gone")
                self.assertEqual(
                    first_log["sources"]["official"]["http"]
                    ["terminal_statuses"], [410])

                with patch.object(fl.requests, "get") as get:
                    self.assertEqual(fl.main(), 1)
                get.assert_not_called()

            second_log = json.loads(
                (root / "data/raw/2026-08-06/_fetch_log.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(
                second_log["sources"]["official"]["status"], "gone")
            self.assertIn(
                "FetchGone", second_log["sources"]["official"]["error"])

    def test_formal_reddit_sources_rotate_one_slot_per_day(self):
        sources = [
            {"id": "ordinary", "url": "https://example.test/feed", "enabled": True},
            *[
                {"id": f"reddit-{index}",
                 "url": f"https://www.reddit.com/r/test{index}/.rss",
                 "enabled": True}
                for index in range(4)
            ],
        ]

        selections = [
            fl.select_daily_reddit_sources(
                sources, f"2026-08-{day:02d}")
            for day in range(6, 10)
        ]

        self.assertTrue(all(len(selection) == 1 for selection in selections))
        self.assertEqual(
            set().union(*selections),
            {f"reddit-{index}" for index in range(4)})

    def test_full_run_fetches_only_the_daily_reddit_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            sources = "sources:\n" + "".join(
                "  - id: reddit-{index}\n"
                "    name: Reddit {index}\n"
                "    type: reddit_rss\n"
                "    tier: community\n"
                "    url: https://www.reddit.com/r/test{index}/.rss\n".format(
                    index=index)
                for index in range(4)
            )
            (root / "config/sources.yaml").write_text(
                sources, encoding="utf-8")
            (root / "config/topics.yaml").write_text(
                "{}\n", encoding="utf-8")
            calls = []

            def fetch(source):
                calls.append(source["id"])
                return []

            argv = ["fetch_l1.py", "--date", "2026-08-06"]
            with (patch.object(fl, "ROOT", root),
                  patch.object(fl, "STATE_DIR", root / "data/state"),
                  patch.object(
                      fl, "HTTP_CACHE_ROOT", root / "data/state/http_cache"),
                  patch.object(sys, "argv", argv),
                  patch.dict(fl.FETCHERS, {"reddit_rss": fetch})):
                self.assertEqual(fl.main(), 0)

            self.assertEqual(len(calls), 1)
            log = json.loads(
                (root / "data/raw/2026-08-06/_fetch_log.json").read_text(
                    encoding="utf-8"))
            skipped = [
                source_id for source_id, row in log["sources"].items()
                if row.get("reason") == "reddit_daily_rotation"]
            self.assertEqual(len(skipped), 3)

    def test_only_cannot_bypass_one_formal_reddit_source_per_day(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config/sources.yaml").write_text(
                "sources:\n"
                "  - id: reddit-0\n"
                "    name: Reddit 0\n"
                "    type: reddit_rss\n"
                "    tier: community\n"
                "    url: https://www.reddit.com/r/test0/.rss\n"
                "  - id: reddit-1\n"
                "    name: Reddit 1\n"
                "    type: reddit_rss\n"
                "    tier: community\n"
                "    url: https://www.reddit.com/r/test1/.rss\n",
                encoding="utf-8")
            (root / "config/topics.yaml").write_text("{}\n", encoding="utf-8")
            calls = []

            def fetch(source):
                calls.append(source["id"])
                return []

            argv = [
                "fetch_l1.py", "--date", "2026-08-06",
                "--only", "reddit-0,reddit-1",
            ]
            with (patch.object(fl, "ROOT", root),
                  patch.object(fl, "STATE_DIR", root / "data/state"),
                  patch.object(
                      fl, "HTTP_CACHE_ROOT", root / "data/state/http_cache"),
                  patch.object(sys, "argv", argv),
                  patch.dict(fl.FETCHERS, {"reddit_rss": fetch})):
                self.assertEqual(fl.main(), 0)

            self.assertEqual(len(calls), 1)
            log = json.loads(
                (root / "data/raw/2026-08-06/_fetch_log.only.json").read_text(
                    encoding="utf-8"))
            skipped = [
                row for row in log["sources"].values()
                if row.get("reason") == "reddit_daily_rotation"]
            self.assertEqual(len(skipped), 1)

            designated = calls[0]
            other = "reddit-1" if designated == "reddit-0" else "reddit-0"
            calls.clear()
            argv = [
                "fetch_l1.py", "--date", "2026-08-06", "--only", other]
            with (patch.object(fl, "ROOT", root),
                  patch.object(fl, "STATE_DIR", root / "data/state"),
                  patch.object(
                      fl, "HTTP_CACHE_ROOT", root / "data/state/http_cache"),
                  patch.object(sys, "argv", argv),
                  patch.dict(fl.FETCHERS, {"reddit_rss": fetch})):
                self.assertEqual(fl.main(), 1)

            self.assertEqual(calls, [])

    def test_baseline_refresh_failure_does_not_block_l1_results(self):
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
            (root / "config/reddit_audit.yaml").write_text(
                "audit:\n"
                "  keep_days: 900\n"
                "  signal_match_window_days: 30\n"
                "  duration_days: 19\n",
                encoding="utf-8")
            expired = root / "data/raw/2020-01-01"
            expired.mkdir(parents=True)
            (expired / "old.json").write_text("{}\n", encoding="utf-8")
            argv = ["fetch_l1.py", "--date", "2026-08-06"]
            with (patch.object(fl, "ROOT", root),
                  patch.object(fl, "STATE_DIR", root / "data/state"),
                  patch.object(
                      fl, "HTTP_CACHE_ROOT", root / "data/state/http_cache"),
                  patch.object(sys, "argv", argv),
                  patch.dict(fl.FETCHERS, {"rss": lambda _: []}),
                  patch.object(
                      fl, "refresh_l1_baseline",
                      side_effect=RuntimeError("index blocked")) as refresh):
                self.assertEqual(fl.main(), 0)

            log = json.loads(
                (root / "data/raw/2026-08-06/_fetch_log.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(log["sources"]["official"]["status"], "ok")
            self.assertEqual(log["reddit_audit_baseline"]["status"], "error")
            self.assertTrue(expired.exists())
            self.assertEqual(refresh.call_args.args[-1], 19)


if __name__ == "__main__":
    unittest.main()
