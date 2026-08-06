# -*- coding: utf-8 -*-
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_l1 as fl  # noqa: E402


class HttpGetTest(unittest.TestCase):
    def test_retries_one_transient_connection_failure(self):
        response = Mock(status_code=200, headers={})
        response.raise_for_status.return_value = None
        with patch.object(fl.requests, "get", side_effect=[requests.ConnectionError("tls"), response]) as get:
            with patch.object(fl.time, "sleep") as sleep:
                result = fl.http_get("https://status.example/feed.rss")
        self.assertIs(result, response)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_does_not_hide_repeated_connection_failure(self):
        with patch.object(fl.requests, "get", side_effect=requests.ConnectionError("down")) as get:
            with patch.object(fl.time, "sleep"):
                with self.assertRaises(requests.ConnectionError):
                    fl.http_get("https://status.example/feed.rss")
        self.assertEqual(get.call_count, 2)

    def test_rss_preserves_outbound_href_before_stripping_markup(self):
        response = Mock()
        response.content = b'''<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Benchmark</title>
    <link href="https://www.reddit.com/r/test/comments/abc/post/" />
    <updated>2026-08-05T10:00:00Z</updated>
    <content type="html">&lt;a href="https://arxiv.org/abs/2608.00001"&gt;[link]&lt;/a&gt;</content>
  </entry>
</feed>'''
        with patch.object(fl, "http_get", return_value=response):
            item = fl.fetch_rss({"url": "https://www.reddit.com/r/test/new/.rss"})[0]

        self.assertEqual(item["summary"], "[link]")
        self.assertEqual(item["external_urls"], ["https://arxiv.org/abs/2608.00001"])

    def test_rss_exposes_full_text_only_when_audit_requests_it(self):
        long_body = "x" * 2200 + " controlled experiment benchmark at the tail"
        response = Mock()
        response.content = f'''<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><item>
  <title>Long technical post</title>
  <link>https://author.test/post</link>
  <pubDate>Wed, 05 Aug 2026 10:00:00 GMT</pubDate>
  <description>&lt;p&gt;{long_body}&lt;/p&gt;</description>
</item></channel></rss>'''.encode()
        with patch.object(fl, "http_get", return_value=response):
            normal = fl.fetch_rss({"url": "https://author.test/feed"})[0]
            audited = fl.fetch_rss({
                "url": "https://author.test/feed", "audit_fulltext": True})[0]

        self.assertNotIn("_audit_fulltext", normal)
        self.assertNotIn("controlled experiment", audited["summary"])
        self.assertIn("controlled experiment", audited["_audit_fulltext"])


if __name__ == "__main__":
    unittest.main()
