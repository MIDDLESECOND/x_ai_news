# -*- coding: utf-8 -*-
from datetime import datetime, timedelta, timezone
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

import requests
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_l1 as fl  # noqa: E402
from http_fetch_state import load_entry, prune_cache, request_key, request_lease  # noqa: E402


class HttpGetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.tmp.name)
        self.now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
        self.cache_patch = patch.object(fl, "HTTP_CACHE_ROOT", self.cache_root)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def get(self, url, **kwargs):
        logical_day = kwargs.pop("logical_day", self.now.date().isoformat())
        return fl.http_get(
            url, cache_root=self.cache_root, now=self.now,
            logical_day=logical_day, **kwargs)

    @staticmethod
    def response(status=200, content=b"feed-v1", headers=None):
        response = Mock(status_code=status, headers=headers or {})
        response.content = content
        response.url = "https://status.example/feed.rss"
        response.encoding = "utf-8"
        response.raise_for_status.return_value = None
        return response

    def test_reddit_request_reserves_shared_rate_limit_slot(self):
        response = self.response()
        with patch.object(fl, "reserve_request") as reserve:
            with patch.object(fl.requests, "get", return_value=response):
                fl.http_get("https://www.reddit.com/r/test/.rss")
        reserve.assert_called_once_with(
            "https://www.reddit.com/r/test/.rss", sleep_fn=ANY)

    def test_reddit_connection_retry_reserves_a_second_slot(self):
        response = self.response()
        with patch.object(fl, "reserve_request") as reserve:
            with patch.object(fl.requests, "get",
                              side_effect=[requests.ConnectionError("tls"), response]):
                with patch.object(fl.time, "sleep"):
                    fl.http_get("https://www.reddit.com/r/test/.rss")
        self.assertEqual(reserve.call_count, 2)

    def test_reddit_429_retry_reserves_a_second_slot(self):
        limited = self.response(status=429, content=b"", headers={"Retry-After": "30"})
        response = self.response()
        with patch.object(fl, "reserve_request") as reserve:
            with patch.object(fl.requests, "get", side_effect=[limited, response]):
                with patch.object(fl.time, "sleep") as sleep:
                    fl.http_get("https://www.reddit.com/r/test/.rss")
        self.assertEqual(reserve.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_retries_one_transient_connection_failure(self):
        response = self.response()
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

    def test_fresh_cached_response_avoids_network_and_reddit_budget(self):
        response = self.response(headers={"ETag": '"v1"'})
        url = "https://www.reddit.com/r/test/.rss"
        with patch.object(fl, "reserve_request") as reserve:
            with patch.object(fl.requests, "get", return_value=response):
                first = self.get(url)
            self.now += timedelta(minutes=15)
            with patch.object(fl.requests, "get") as get:
                second = self.get(url)

        self.assertEqual(first.content, second.content)
        self.assertEqual(second.frontier_cache_status, "fresh")
        get.assert_not_called()
        reserve.assert_called_once_with(url, sleep_fn=ANY)

    def test_stale_cache_revalidates_with_etag_and_reuses_body_on_304(self):
        first = self.response(headers={"ETag": '"v1"', "Content-Type": "application/xml"})
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=first):
            self.get(url)

        self.now += timedelta(minutes=31)
        not_modified = self.response(status=304, content=b"", headers={"ETag": '"v1"'})
        with patch.object(fl.requests, "get", return_value=not_modified) as get:
            response = self.get(url)

        sent_headers = get.call_args.kwargs["headers"]
        self.assertEqual(sent_headers["If-None-Match"], '"v1"')
        self.assertEqual(response.content, b"feed-v1")
        self.assertEqual(response.frontier_cache_status, "revalidated")

        # 一次确认“未变化”后，下一检查间隔从 30 分钟自适应到 60 分钟。
        self.now += timedelta(minutes=45)
        with patch.object(fl.requests, "get") as get:
            cached = self.get(url)
        get.assert_not_called()
        self.assertEqual(cached.frontier_cache_status, "fresh")

    def test_new_logical_day_revalidates_even_inside_twelve_hour_window(self):
        self.now = datetime(2026, 8, 6, 23, 50, tzinfo=timezone.utc)
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response(headers={"ETag": '"v1"'})):
            self.get(url)

        self.now += timedelta(minutes=20)
        not_modified = self.response(status=304, content=b"", headers={"ETag": '"v1"'})
        with patch.object(fl.requests, "get", return_value=not_modified) as get:
            response = self.get(url)
        get.assert_called_once()
        self.assertEqual(response.frontier_cache_status, "revalidated")

    def test_explicit_short_max_age_caps_application_polling_window(self):
        url = "https://status.example/feed.rss"
        first = self.response(headers={
            "ETag": '"v1"', "Cache-Control": "max-age=60", "Age": "0"})
        with patch.object(fl.requests, "get", return_value=first):
            self.get(url)

        self.now += timedelta(minutes=2)
        not_modified = self.response(status=304, content=b"", headers={"ETag": '"v1"'})
        with patch.object(fl.requests, "get", return_value=not_modified) as get:
            self.get(url)
        get.assert_called_once()

    def test_failure_cooldown_serves_explicit_stale_cache_without_new_request(self):
        first = self.response(headers={"ETag": '"v1"'})
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=first):
            self.get(url)

        self.now += timedelta(minutes=31)
        with patch.object(fl.requests, "get", side_effect=requests.ConnectionError("down")):
            with patch.object(fl.time, "sleep"):
                with self.assertRaises(requests.ConnectionError):
                    self.get(url)

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            cached = self.get(url)
        get.assert_not_called()
        self.assertEqual(cached.content, b"feed-v1")
        self.assertEqual(cached.frontier_cache_status, "stale_backoff")

    def test_failure_without_cached_body_defers_next_attempt(self):
        url = "https://new-source.example/feed.rss"
        with patch.object(fl.requests, "get", side_effect=requests.ConnectionError("down")):
            with patch.object(fl.time, "sleep"):
                with self.assertRaises(requests.ConnectionError):
                    self.get(url)

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            with self.assertRaises(requests.RequestException):
                self.get(url)
        get.assert_not_called()

    def test_must_revalidate_cache_is_not_served_stale_during_failure_cooldown(self):
        url = "https://status.example/feed.rss"
        first = self.response(headers={
            "ETag": '"v1"', "Cache-Control": "max-age=0, must-revalidate"})
        with patch.object(fl.requests, "get", return_value=first):
            self.get(url)
        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get", side_effect=requests.ConnectionError("down")):
            with patch.object(fl.time, "sleep"):
                with self.assertRaises(requests.ConnectionError):
                    self.get(url)

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            with self.assertRaises(requests.RequestException):
                self.get(url)
        get.assert_not_called()

    def test_stale_fallback_expires_after_bounded_age(self):
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response()):
            self.get(url)
        self.now += timedelta(hours=13)
        with patch.object(fl.requests, "get", side_effect=requests.ConnectionError("down")):
            with patch.object(fl.time, "sleep"):
                with self.assertRaises(requests.ConnectionError):
                    self.get(url)
        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            with self.assertRaises(requests.RequestException):
                self.get(url)
        get.assert_not_called()

    def test_non_retryable_404_does_not_create_failure_cooldown(self):
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response()):
            self.get(url)
        self.now += timedelta(minutes=31)
        missing = requests.Response()
        missing.status_code = 404
        missing.url = url
        missing._content = b"missing"
        with patch.object(fl.requests, "get", return_value=missing):
            with self.assertRaises(requests.HTTPError):
                self.get(url)

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get", return_value=self.response(content=b"back")) as get:
            response = self.get(url)
        get.assert_called_once()
        self.assertEqual(response.content, b"back")

    def test_source_http_summary_distinguishes_network_cache_and_stale(self):
        network = fl.summarize_http_events([
            {"cache_status": "revalidated", "network_attempts": 1,
             "last_network_success_at": "2026-08-06T12:00:00+00:00"},
            {"cache_status": "fresh", "network_attempts": 0},
        ])
        cached = fl.summarize_http_events([
            {"cache_status": "fresh", "network_attempts": 0},
        ])
        stale = fl.summarize_http_events([
            {"cache_status": "stale_backoff", "network_attempts": 0},
        ])

        self.assertEqual(network["source_status"], "ok")
        self.assertEqual(network["network_attempts"], 1)
        self.assertEqual(cached["source_status"], "cached")
        self.assertEqual(stale["source_status"], "stale")

    def test_source_http_summary_does_not_call_all_failures_success(self):
        failed = fl.summarize_http_events([
            {"cache_status": "error", "network_attempts": 2,
             "error": "ConnectionError"},
        ])
        partial = fl.summarize_http_events([
            {"cache_status": "fresh", "network_attempts": 0},
            {"cache_status": "error", "network_attempts": 1,
             "error": "ConnectionError"},
        ])

        self.assertEqual(failed["source_status"], "error")
        self.assertEqual(failed["network_successes"], 0)
        self.assertEqual(partial["source_status"], "partial")
        self.assertEqual(partial["network_errors"], 1)

    def test_invalid_json_200_is_tombstoned_instead_of_fresh_cached(self):
        url = "https://api.example/data.json"
        invalid = requests.Response()
        invalid.status_code = 200
        invalid.url = url
        invalid.headers["Content-Type"] = "text/html"
        invalid._content = b"<html>challenge</html>"
        with patch.object(fl.requests, "get", return_value=invalid):
            with self.assertRaises(fl.ResponseValidationError):
                self.get(url, accept="application/json", response_kind="json")

        entry = load_entry(self.cache_root, request_key(url, "application/json"))
        self.assertFalse(entry.get("body_sha256"))
        self.assertTrue(entry.get("retry_at"))

        self.now += timedelta(minutes=31)
        valid = requests.Response()
        valid.status_code = 200
        valid.url = url
        valid.headers["Content-Type"] = "application/json"
        valid._content = b'{"ok": true}'
        with patch.object(fl.requests, "get", return_value=valid) as get:
            response = self.get(
                url, accept="application/json", response_kind="json")
        get.assert_called_once()
        self.assertEqual(response.json(), {"ok": True})

    def test_redirect_hops_each_pass_through_rate_gate(self):
        start = "https://redirect.example/feed"
        target = "https://www.reddit.com/r/test/.rss"
        redirected = self.response(
            status=302, content=b"", headers={"Location": target})
        redirected.url = start
        final = self.response()
        final.url = target

        with patch.object(fl, "reserve_request") as reserve:
            with patch.object(fl.requests, "get", side_effect=[redirected, final]):
                response = self.get(start)

        self.assertEqual(response.content, b"feed-v1")
        self.assertEqual(
            [entry.args[0] for entry in reserve.call_args_list], [start, target])
        self.assertEqual(
            reserve.call_args_list,
            [call(start, sleep_fn=ANY), call(target, sleep_fn=ANY)])

    def test_source_delay_runs_only_for_a_real_non_reddit_request(self):
        url = "https://status.example/delayed-feed"
        source = {"id": "delayed", "delay_before": 120}
        with patch.object(fl, "_sleep_with_lease_heartbeat") as sleep:
            with patch.object(fl.requests, "get", return_value=self.response()):
                self.get(url, source=source)
            with patch.object(fl.requests, "get") as get:
                self.get(url, source=source)
        sleep.assert_called_once()
        self.assertEqual(sleep.call_args.args[0], 120)
        get.assert_not_called()

    def test_reddit_policy_block_is_not_preceded_by_source_delay(self):
        url = "https://www.reddit.com/r/test/.rss"
        source = {"id": "reddit", "delay_before": 1800}
        with patch.object(fl, "_sleep_with_lease_heartbeat") as sleep:
            with patch.object(
                    fl, "reserve_request", side_effect=RuntimeError("disabled")):
                with self.assertRaises(RuntimeError):
                    self.get(url, source=source)
        sleep.assert_not_called()

    def test_redirect_target_retry_after_cools_target_host(self):
        start = "https://redirect.example/feed"
        target = "https://limited.example/feed"
        redirected = self.response(
            status=302, content=b"", headers={"Location": target})
        redirected.url = start
        limited = requests.Response()
        limited.status_code = 503
        limited.url = target
        limited.headers["Retry-After"] = "3600"
        limited._content = b"unavailable"
        with patch.object(fl.requests, "get", side_effect=[redirected, limited]):
            with self.assertRaises(requests.HTTPError):
                self.get(start)

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            with self.assertRaises(requests.RequestException):
                self.get("https://limited.example/another")
        get.assert_not_called()

    def test_lease_owner_does_not_unlink_a_successor_lock(self):
        key = "owner-token"
        path = self.cache_root / "request_locks" / f"{key}.lock"
        with request_lease(self.cache_root, key):
            path.unlink()
            path.write_text("successor\n", encoding="ascii")
        self.assertEqual(path.read_text(encoding="ascii"), "successor\n")

    def test_no_store_response_invalidates_previous_reusable_entry(self):
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response()):
            self.get(url)

        self.now += timedelta(minutes=1)
        no_store = self.response(content=b"private", headers={"Cache-Control": "no-store"})
        with patch.object(fl.requests, "get", return_value=no_store):
            self.get(url, force_revalidate=True)

        self.now += timedelta(minutes=1)
        fresh = self.response(content=b"public")
        with patch.object(fl.requests, "get", return_value=fresh) as get:
            response = self.get(url)
        get.assert_called_once()
        self.assertEqual(response.content, b"public")

    def test_304_no_store_tombstones_previous_body_after_current_call(self):
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response(
                headers={"ETag": '"v1"'})):
            self.get(url)
        self.now += timedelta(minutes=31)
        not_modified = self.response(
            status=304, content=b"",
            headers={"ETag": '"v1"', "Cache-Control": "no-store"})
        with patch.object(fl.requests, "get", return_value=not_modified):
            current = self.get(url)
        self.assertEqual(current.content, b"feed-v1")

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get", return_value=self.response(
                content=b"new-public")) as get:
            following = self.get(url)
        get.assert_called_once()
        self.assertEqual(following.content, b"new-public")

    def test_failure_state_honors_retry_after_beyond_local_backoff_cap(self):
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response()):
            self.get(url)
        self.now += timedelta(minutes=31)
        limited = requests.Response()
        limited.status_code = 429
        limited.url = url
        limited.headers["Retry-After"] = "86400"
        limited._content = b"limited"
        with patch.object(fl.requests, "get", return_value=limited) as get:
            with patch.object(fl.time, "sleep") as sleep:
                with self.assertRaises(requests.HTTPError):
                    self.get(url)
        get.assert_called_once()
        sleep.assert_not_called()

        entry = load_entry(self.cache_root, request_key(url))
        retry_at = datetime.fromisoformat(entry["retry_at"])
        self.assertGreaterEqual(retry_at - self.now, timedelta(days=1))

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            with self.assertRaises(requests.RequestException):
                self.get("https://status.example/another-feed")
        get.assert_not_called()

        with patch.object(fl.requests, "get", return_value=self.response()) as get:
            self.get("https://other.example/feed")
        get.assert_called_once()

    def test_second_429_retry_after_is_measured_from_second_response(self):
        url = "https://limited.example/feed"
        source = {"id": "limited", "fetch_policy": {
            "failure_base_minutes": 0, "failure_max_minutes": 0}}
        limited = requests.Response()
        limited.status_code = 429
        limited.url = url
        limited.headers["Retry-After"] = "30"
        limited._content = b"limited"
        with patch.object(fl.requests, "get", return_value=limited) as get:
            with patch.object(fl.time, "sleep"):
                with self.assertRaises(requests.HTTPError):
                    self.get(url, source=source)
        self.assertEqual(get.call_count, 2)
        entry = load_entry(self.cache_root, request_key(url, source_id="limited"))
        retry_at = datetime.fromisoformat(entry["retry_at"])
        self.assertGreaterEqual(retry_at - self.now, timedelta(seconds=60))

    def test_same_url_uses_independent_source_schedule_keys(self):
        url = "https://status.example/feed.rss"
        source_a = {"id": "a"}
        source_b = {"id": "b"}
        with patch.object(fl.requests, "get", return_value=self.response(content=b"same")):
            self.get(url, source=source_a)
        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get", return_value=self.response(content=b"same")) as get:
            self.get(url, source=source_b)
        get.assert_called_once()

    def test_concurrent_same_key_calls_singleflight_network_request(self):
        url = "https://status.example/feed.rss"
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []

        def slow_get(*args, **kwargs):
            entered.set()
            release.wait(timeout=2)
            return self.response(content=b"one-network-response")

        def worker():
            try:
                results.append(self.get(url, force_revalidate=True).content)
            except Exception as error:  # pragma: no cover - assertion reports details
                errors.append(error)

        with patch.object(fl.requests, "get", side_effect=slow_get) as get:
            first = threading.Thread(target=worker)
            second = threading.Thread(target=worker)
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            second.start()
            time.sleep(0.1)
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(errors)
        self.assertEqual(results, [b"one-network-response"] * 2)
        get.assert_called_once()

    def test_cache_pruning_removes_unreferenced_body_but_keeps_current_entry(self):
        url = "https://status.example/feed.rss"
        with patch.object(fl.requests, "get", return_value=self.response(content=b"v1")):
            self.get(url)
        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get", return_value=self.response(content=b"v2")):
            self.get(url, force_revalidate=True)

        self.assertEqual(len(list((self.cache_root / "bodies").glob("*.bin"))), 2)
        result = prune_cache(self.cache_root, now=self.now, max_age_days=45)
        self.assertEqual(result["removed_bodies"], 1)
        self.assertEqual(len(list((self.cache_root / "bodies").glob("*.bin"))), 1)
        self.assertTrue(load_entry(self.cache_root, request_key(url)))

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
