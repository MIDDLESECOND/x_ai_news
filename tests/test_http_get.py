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

    @staticmethod
    def streamed_response(chunks, *, status=200, headers=None, url=None):
        response = requests.Response()
        response.status_code = status
        response.url = url or "https://stream.example/feed"
        response.headers.update(headers or {})
        response._content = False
        response._content_consumed = False
        response.iter_content = Mock(return_value=iter(chunks))
        response.close = Mock()
        return response

    @staticmethod
    def dns_answers(*addresses, port=443):
        return [
            (fl.socket.AF_INET, fl.socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

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

    def test_content_length_over_download_cap_is_rejected_before_read(self):
        url = "https://stream.example/declared-large"
        response = self.streamed_response(
            [b"should-not-read"], headers={"Content-Length": "101"}, url=url)
        source = {"id": "bounded", "fetch_policy": {"max_download_bytes": 100}}
        with patch.object(fl.requests, "get", return_value=response) as get:
            with self.assertRaises(fl.ResponseTooLarge):
                self.get(url, source=source)

        response.iter_content.assert_not_called()
        response.close.assert_called()
        self.assertTrue(get.call_args.kwargs["stream"])
        entry = load_entry(self.cache_root, request_key(url, source_id="bounded"))
        self.assertFalse(entry.get("body_sha256"))
        self.assertTrue(entry.get("retry_at"))

    def test_download_cap_can_tighten_but_not_exceed_hard_default(self):
        hard_cap = 15 * 1024 * 1024
        self.assertEqual(fl.resolve_policy({
            "fetch_policy": {"max_download_bytes": hard_cap * 10},
        })["max_download_bytes"], hard_cap)
        self.assertEqual(fl.resolve_policy({
            "fetch_policy": {"max_download_bytes": 1024},
        })["max_download_bytes"], 1024)

    def test_download_time_cap_can_tighten_but_not_exceed_hard_default(self):
        hard_cap = 120
        self.assertEqual(fl.resolve_policy({
            "fetch_policy": {"max_download_seconds": hard_cap * 10},
        })["max_download_seconds"], hard_cap)
        self.assertEqual(fl.resolve_policy({
            "fetch_policy": {"max_download_seconds": 5},
        })["max_download_seconds"], 5)

    def test_chunked_body_is_stopped_when_decoded_bytes_cross_cap(self):
        url = "https://stream.example/chunked-large"
        response = self.streamed_response([b"a" * 60, b"b" * 41], url=url)
        source = {"id": "bounded", "fetch_policy": {"max_download_bytes": 100}}
        with patch.object(fl.requests, "get", return_value=response):
            with self.assertRaises(fl.ResponseTooLarge):
                self.get(url, source=source)

        response.iter_content.assert_called_once_with(chunk_size=64 * 1024)
        response.close.assert_called()

    def test_streamed_body_within_cap_is_materialized_and_cached(self):
        url = "https://stream.example/feed"
        response = self.streamed_response([b"feed-", b"v1"], url=url)
        source = {"id": "bounded", "fetch_policy": {"max_download_bytes": 100}}
        with patch.object(fl.requests, "get", return_value=response):
            result = self.get(url, source=source)

        self.assertEqual(result.content, b"feed-v1")
        response.close.assert_called_once()
        entry = load_entry(self.cache_root, request_key(url, source_id="bounded"))
        self.assertTrue(entry.get("body_sha256"))

    def test_slow_stream_is_stopped_after_download_time_cap(self):
        url = "https://stream.example/slow"
        response = self.streamed_response([b"feed-", b"v1"], url=url)
        source = {
            "id": "bounded",
            "fetch_policy": {
                "max_download_bytes": 100,
                "max_download_seconds": 1,
            },
        }
        with patch.object(fl, "_monotonic", side_effect=[100.0, 100.5, 101.1]):
            with patch.object(fl.requests, "get", return_value=response):
                with self.assertRaises(fl.ResponseDownloadTimeout):
                    self.get(url, source=source)

        response.close.assert_called()
        entry = load_entry(
            self.cache_root, request_key(url, source_id="bounded"))
        self.assertTrue(entry.get("retry_at"))
        self.assertTrue(entry.get("allow_stale"))

    def test_streamed_body_within_time_cap_is_cached(self):
        url = "https://stream.example/fast"
        response = self.streamed_response([b"feed-", b"v1"], url=url)
        source = {
            "id": "bounded",
            "fetch_policy": {
                "max_download_bytes": 100,
                "max_download_seconds": 1,
            },
        }
        with patch.object(
                fl, "_monotonic",
                side_effect=[100.0, 100.4, 100.8, 100.9]):
            with patch.object(fl.requests, "get", return_value=response):
                result = self.get(url, source=source)

        self.assertEqual(result.content, b"feed-v1")
        entry = load_entry(
            self.cache_root, request_key(url, source_id="bounded"))
        self.assertTrue(entry.get("body_sha256"))

    def test_download_watchdog_closes_stream_at_deadline(self):
        url = "https://stream.example/watchdog"
        response = self.streamed_response([b"feed"], url=url)
        response.iter_content.side_effect = (
            requests.exceptions.ChunkedEncodingError("closed by watchdog"))
        source = {
            "id": "bounded",
            "fetch_policy": {"max_download_seconds": 5},
        }
        timer = Mock()

        def timer_factory(seconds, callback):
            self.assertEqual(seconds, 5)
            timer.start.side_effect = callback
            return timer

        with patch.object(fl, "_download_timer", side_effect=timer_factory):
            with patch.object(fl, "_monotonic", return_value=100.0):
                with patch.object(fl.requests, "get", return_value=response):
                    with self.assertRaises(
                            fl.ResponseDownloadTimeout) as raised:
                        self.get(url, source=source)

        self.assertIsInstance(
            raised.exception.__cause__,
            requests.exceptions.ChunkedEncodingError)
        timer.start.assert_called_once()
        timer.cancel.assert_called_once()
        response.close.assert_called()

    def test_zero_download_time_cap_rejects_before_stream_read(self):
        url = "https://stream.example/disabled-body"
        response = self.streamed_response([b"feed"], url=url)
        source = {
            "id": "bounded",
            "fetch_policy": {"max_download_seconds": 0},
        }
        with patch.object(fl.requests, "get", return_value=response):
            with self.assertRaises(fl.ResponseDownloadTimeout):
                self.get(url, source=source)

        response.iter_content.assert_not_called()
        response.close.assert_called()

    def test_download_time_failure_can_serve_prior_validated_stale_body(self):
        url = "https://stream.example/stale-after-slow"
        source = {
            "id": "bounded",
            "fetch_policy": {
                "max_download_bytes": 100,
                "max_download_seconds": 1,
            },
        }
        first = self.response(
            content=b"old-feed",
            headers={"ETag": '"v1"', "Cache-Control": "max-age=0"})
        with patch.object(fl.requests, "get", return_value=first):
            self.get(url, source=source)

        self.now += timedelta(minutes=1)
        slow = self.streamed_response([b"new-", b"feed"], url=url)
        with patch.object(fl, "_monotonic", side_effect=[100.0, 101.1]):
            with patch.object(fl.requests, "get", return_value=slow):
                with self.assertRaises(fl.ResponseDownloadTimeout):
                    self.get(url, source=source)

        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            stale = self.get(url, source=source)
        get.assert_not_called()
        self.assertEqual(stale.content, b"old-feed")
        self.assertEqual(stale.frontier_cache_status, "stale_backoff")

    def test_stream_decode_failure_remains_retryable_network_failure(self):
        url = "https://stream.example/broken"
        response = self.streamed_response([], url=url)
        response.iter_content.side_effect = requests.exceptions.ChunkedEncodingError("broken")
        source = {"id": "bounded", "fetch_policy": {"max_download_bytes": 100}}
        with patch.object(fl.requests, "get", return_value=response):
            with self.assertRaises(requests.exceptions.ChunkedEncodingError):
                self.get(url, source=source)

        entry = load_entry(self.cache_root, request_key(url, source_id="bounded"))
        self.assertTrue(entry.get("retry_at"))
        self.assertTrue(entry.get("allow_stale"))
        response.close.assert_called()

    def test_tightened_download_cap_invalidates_larger_fresh_cache(self):
        url = "https://stream.example/tightened"
        source = {"id": "bounded", "fetch_policy": {"max_download_bytes": 100}}
        with patch.object(
                fl.requests, "get",
                return_value=self.response(content=b"a" * 80)):
            self.get(url, source=source)

        tightened = {
            "id": "bounded", "fetch_policy": {"max_download_bytes": 50}}
        replacement = self.response(content=b"b" * 40)
        with patch.object(fl.requests, "get", return_value=replacement) as get:
            result = self.get(url, source=tightened)
        get.assert_called_once()
        self.assertEqual(result.content, b"b" * 40)

    def test_tightened_download_cap_rejects_larger_304_body(self):
        url = "https://stream.example/tightened-304"
        source = {"id": "bounded", "fetch_policy": {"max_download_bytes": 100}}
        with patch.object(
                fl.requests, "get",
                return_value=self.response(
                    content=b"a" * 80, headers={"ETag": '"v1"'})):
            self.get(url, source=source)

        self.now += timedelta(minutes=31)
        tightened = {
            "id": "bounded", "fetch_policy": {"max_download_bytes": 50}}
        not_modified = self.response(
            status=304, content=b"", headers={"ETag": '"v1"'})
        with patch.object(fl.requests, "get", return_value=not_modified) as get:
            with self.assertRaises(fl.ResponseTooLarge):
                self.get(url, source=tightened)
        get.assert_called_once()

    def test_redirect_hops_each_pass_through_rate_gate(self):
        start = "https://redirect.example/feed"
        target = "https://www.reddit.com/r/test/.rss"
        redirected = self.response(
            status=302, content=b"", headers={"Location": target})
        redirected.url = start
        final = self.response()
        final.url = target

        with patch.object(fl, "reserve_request") as reserve:
            with patch.object(
                    fl.socket, "getaddrinfo",
                    return_value=self.dns_answers("151.101.1.140")):
                with patch.object(
                        fl.requests, "get", side_effect=[redirected, final]):
                    response = self.get(start)

        self.assertEqual(response.content, b"feed-v1")
        redirected.close.assert_called_once()
        self.assertEqual(
            [entry.args[0] for entry in reserve.call_args_list], [start, target])
        self.assertEqual(
            reserve.call_args_list,
            [call(start, sleep_fn=ANY), call(target, sleep_fn=ANY)])

    def test_permanent_redirect_is_remembered_and_revalidated_directly(self):
        start = "https://old.example/feed"
        target = "https://new.example/feed"
        moved = self.response(
            status=301, content=b"", headers={"Location": target})
        moved.url = start
        final = self.response(headers={"ETag": '"v1"'})
        final.url = target
        public_dns = self.dns_answers("93.184.216.34")
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_url"), target)

        not_modified = self.response(
            status=304, content=b"", headers={"ETag": '"v1"'})
        not_modified.url = target
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", return_value=not_modified) as get:
                response = self.get(start, force_revalidate=True)

        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], target)
        self.assertEqual(
            get.call_args.kwargs["headers"].get("If-None-Match"), '"v1"')
        self.assertEqual(response.content, b"feed-v1")
        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_url"), target)

    def test_temporary_redirect_is_not_remembered(self):
        start = "https://old.example/feed"
        target = "https://temporary.example/feed"
        moved = self.response(
            status=302, content=b"", headers={"Location": target})
        moved.url = start
        final = self.response()
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertNotIn("permanent_redirect_url", entry)

    def test_mixed_permanent_and_temporary_chain_is_not_remembered(self):
        start = "https://old.example/feed"
        intermediate = "https://moved.example/feed"
        target = "https://temporary.example/feed"
        permanent = self.response(
            status=301, content=b"", headers={"Location": intermediate})
        permanent.url = start
        temporary = self.response(
            status=302, content=b"", headers={"Location": target})
        temporary.url = intermediate
        final = self.response()
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get",
                    side_effect=[permanent, temporary, final]):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertNotIn("permanent_redirect_url", entry)

    def test_redirect_cache_directives_can_forbid_memory(self):
        cases = {
            "no-store": {"Cache-Control": "no-store"},
            "no-cache": {"Cache-Control": "no-cache"},
            "must-revalidate": {"Cache-Control": "must-revalidate"},
            "proxy-revalidate": {"Cache-Control": "proxy-revalidate"},
            "vary-star": {"Vary": "*"},
            "invalid-max-age": {"Cache-Control": "max-age=bogus"},
            "duplicate-max-age": {
                "Cache-Control": "max-age=0, max-age=3600"},
            "invalid-expires": {"Expires": "0"},
            "expires-invalid-age": {
                "Expires": "Thu, 06 Aug 2026 13:00:00 GMT",
                "Age": "bogus",
            },
            "expires-invalid-date": {
                "Expires": "Thu, 06 Aug 2026 13:00:00 GMT",
                "Date": "not-a-date",
            },
            "already-aged": {
                "Cache-Control": "max-age=60",
                "Date": "Thu, 06 Aug 2026 11:58:00 GMT",
            },
        }
        for label, redirect_headers in cases.items():
            with self.subTest(label=label):
                start = f"https://old.example/{label}"
                target = f"https://new.example/{label}"
                moved = self.response(
                    status=301, content=b"",
                    headers={"Location": target, **redirect_headers})
                moved.url = start
                final = self.response()
                final.url = target
                with patch.object(
                        fl.socket, "getaddrinfo",
                        return_value=self.dns_answers("93.184.216.34")):
                    with patch.object(
                            fl.requests, "get", side_effect=[moved, final]):
                        self.get(start)

                entry = load_entry(self.cache_root, request_key(start))
                self.assertNotIn("permanent_redirect_url", entry)
                self.assertNotIn("permanent_redirect_until", entry)

    def test_redirect_deadline_is_not_extended_by_target_success(self):
        start = "https://old.example/expiring"
        target = "https://new.example/expiring"
        moved = self.response(
            status=301, content=b"", headers={
                "Location": target, "Cache-Control": "max-age=60",
                "Age": "10"})
        moved.url = start
        final = self.response()
        final.url = target
        public_dns = self.dns_answers("93.184.216.34")
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        first_entry = load_entry(self.cache_root, request_key(start))
        deadline = first_entry.get("permanent_redirect_until")
        self.assertEqual(
            deadline, (self.now + timedelta(seconds=50)).isoformat())

        self.now += timedelta(seconds=30)
        direct = self.response(content=b"updated", headers={"ETag": '"v2"'})
        direct.url = target
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(fl.requests, "get", return_value=direct) as get:
                self.get(start, force_revalidate=True)
        self.assertEqual(get.call_args.args[0], target)
        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_until"), deadline)

        self.now += timedelta(seconds=21)
        replacement = self.response(content=b"configured-origin")
        replacement.url = start
        with patch.object(fl.requests, "get", return_value=replacement) as get:
            self.get(start)
        self.assertEqual(get.call_args.args[0], start)
        self.assertNotIn("If-None-Match", get.call_args.kwargs["headers"])
        entry = load_entry(self.cache_root, request_key(start))
        self.assertNotIn("permanent_redirect_url", entry)

    def test_redirect_expires_header_returns_to_configured_url(self):
        start = "https://old.example/expires"
        target = "https://new.example/expires"
        moved = self.response(
            status=308, content=b"", headers={
                "Location": target,
                "Expires": "Thu, 06 Aug 2026 12:01:00 GMT",
            })
        moved.url = start
        final = self.response()
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        self.now += timedelta(seconds=61)
        replacement = self.response(content=b"configured-origin")
        replacement.url = start
        with patch.object(fl.requests, "get", return_value=replacement) as get:
            self.get(start)
        self.assertEqual(get.call_args.args[0], start)

    def test_redirect_expires_accounts_for_age_and_date(self):
        self.now += timedelta(minutes=10)
        start = "https://old.example/aged-expires"
        target = "https://new.example/aged-expires"
        moved = self.response(
            status=301, content=b"", headers={
                "Location": target,
                "Date": "Thu, 06 Aug 2026 12:00:00 GMT",
                "Expires": "Thu, 06 Aug 2026 13:00:00 GMT",
                "Age": "1800",
            })
        moved.url = start
        final = self.response()
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(
            entry.get("permanent_redirect_until"),
            (self.now + timedelta(minutes=30)).isoformat())

    def test_permanent_redirect_chain_uses_earliest_deadline(self):
        start = "https://old.example/chain"
        intermediate = "https://middle.example/chain"
        target = "https://new.example/chain"
        first = self.response(
            status=301, content=b"", headers={
                "Location": intermediate, "Cache-Control": "max-age=120"})
        first.url = start
        second = self.response(
            status=308, content=b"", headers={
                "Location": target, "Cache-Control": "max-age=60"})
        second.url = intermediate
        final = self.response()
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[first, second, final]):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_url"), target)
        self.assertEqual(
            entry.get("permanent_redirect_until"),
            (self.now + timedelta(seconds=60)).isoformat())

    def test_remembered_target_permanent_move_inherits_earliest_deadline(self):
        start = "https://old.example/inherited-chain"
        remembered = "https://middle.example/inherited-chain"
        target = "https://new.example/inherited-chain"
        first = self.response(
            status=301, content=b"", headers={
                "Location": remembered, "Cache-Control": "max-age=60"})
        first.url = start
        initial_final = self.response()
        initial_final.url = remembered
        public_dns = self.dns_answers("93.184.216.34")
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", side_effect=[first, initial_final]):
                self.get(start)
        inherited_deadline = load_entry(
            self.cache_root, request_key(start)).get(
                "permanent_redirect_until")

        self.now += timedelta(seconds=30)
        moved_again = self.response(
            status=308, content=b"", headers={
                "Location": target, "Cache-Control": "max-age=120"})
        moved_again.url = remembered
        final = self.response(content=b"new-target")
        final.url = target
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get",
                    side_effect=[moved_again, final]) as get:
                self.get(start, force_revalidate=True)

        self.assertEqual(get.call_args_list[0].args[0], remembered)
        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_url"), target)
        self.assertEqual(
            entry.get("permanent_redirect_until"), inherited_deadline)

    def test_remembered_target_temporary_move_does_not_replace_it(self):
        start = "https://old.example/temporary-chain"
        remembered = "https://middle.example/temporary-chain"
        temporary = "https://temporary.example/temporary-chain"
        first = self.response(
            status=301, content=b"", headers={"Location": remembered})
        first.url = start
        initial_final = self.response()
        initial_final.url = remembered
        public_dns = self.dns_answers("93.184.216.34")
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", side_effect=[first, initial_final]):
                self.get(start)

        temporary_move = self.response(
            status=302, content=b"", headers={"Location": temporary})
        temporary_move.url = remembered
        final = self.response(content=b"temporary-target")
        final.url = temporary
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get",
                    side_effect=[temporary_move, final]):
                self.get(start, force_revalidate=True)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_url"), remembered)
        self.assertIsNone(entry.get("permanent_redirect_until"))

    def test_permanent_redirect_memory_respects_disabled_http_state(self):
        start = "https://old.example/feed"
        target = "https://new.example/feed"
        source = {"id": "disabled", "fetch_policy": {"enabled": False}}
        moved = self.response(
            status=301, content=b"", headers={"Location": target})
        moved.url = start
        final = self.response()
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start, source=source)

        entry = load_entry(
            self.cache_root, request_key(start, source_id="disabled"))
        self.assertNotIn("permanent_redirect_url", entry)

    def test_permanent_redirect_survives_no_store_without_caching_body(self):
        start = "https://old.example/no-store"
        target = "https://new.example/no-store"
        moved = self.response(
            status=301, content=b"", headers={"Location": target})
        moved.url = start
        final = self.response(headers={"Cache-Control": "no-store"})
        final.url = target
        public_dns = self.dns_answers("93.184.216.34")
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertEqual(entry.get("permanent_redirect_url"), target)
        self.assertFalse(entry.get("body_sha256"))

        replacement = self.response(headers={"Cache-Control": "no-store"})
        replacement.url = target
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", return_value=replacement) as get:
                self.get(start)
        self.assertEqual(get.call_args.args[0], target)

    def test_remembered_redirect_is_revalidated_as_untrusted(self):
        start = "https://old.example/feed"
        target = "https://moved.example/feed"
        moved = self.response(
            status=301, content=b"", headers={"Location": target})
        moved.url = start
        final = self.response(headers={"ETag": '"v1"'})
        final.url = target
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("127.0.0.1")):
            with patch.object(fl.requests, "get") as get:
                with self.assertRaises(fl.UnsafeRedirectTarget):
                    self.get(start, force_revalidate=True)

        get.assert_not_called()
        entry = load_entry(self.cache_root, request_key(start))
        self.assertNotIn("permanent_redirect_url", entry)
        self.assertFalse(entry.get("allow_stale"))

    def test_failed_remembered_redirect_is_cleared_for_next_run(self):
        start = "https://old.example/feed"
        target = "https://failed.example/feed"
        moved = self.response(
            status=308, content=b"", headers={"Location": target})
        moved.url = start
        final = self.response(headers={"ETag": '"v1"'})
        final.url = target
        public_dns = self.dns_answers("93.184.216.34")
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(
                    fl.requests, "get", side_effect=[moved, final]):
                self.get(start)

        failed = requests.Response()
        failed.status_code = 404
        failed.url = target
        failed._content = b"gone"
        with patch.object(fl.socket, "getaddrinfo", return_value=public_dns):
            with patch.object(fl.requests, "get", return_value=failed) as get:
                with self.assertRaises(requests.HTTPError):
                    self.get(start, force_revalidate=True)

        self.assertEqual(get.call_args.args[0], target)
        entry = load_entry(self.cache_root, request_key(start))
        self.assertNotIn("permanent_redirect_url", entry)
        self.assertNotIn("permanent_redirect_until", entry)

    def test_redirect_to_literal_private_address_is_blocked_before_request(self):
        start = "https://redirect.example/feed"
        target = "http://169.254.169.254/latest/meta-data"
        redirected = self.response(
            status=302, content=b"", headers={"Location": target})
        redirected.url = start
        with patch.object(fl.requests, "get", return_value=redirected) as get:
            with self.assertRaises(fl.UnsafeRedirectTarget):
                self.get(start)

        get.assert_called_once()
        entry = load_entry(self.cache_root, request_key(start))
        self.assertTrue(entry.get("retry_at"))
        self.assertFalse(entry.get("allow_stale"))

    def test_redirect_with_any_private_dns_answer_is_blocked(self):
        start = "https://redirect.example/feed"
        target = "https://mixed.example/feed"
        redirected = self.response(
            status=302, content=b"", headers={"Location": target})
        redirected.url = start
        answers = self.dns_answers("93.184.216.34", "10.0.0.8")
        with patch.object(fl.socket, "getaddrinfo", return_value=answers):
            with patch.object(fl.requests, "get", return_value=redirected) as get:
                with self.assertRaises(fl.UnsafeRedirectTarget):
                    self.get(start)
        get.assert_called_once()

    def test_redirect_is_reresolved_immediately_before_request(self):
        start = "https://redirect.example/feed"
        target = "https://rebound.example/feed"
        redirected = self.response(
            status=302, content=b"", headers={"Location": target})
        redirected.url = start
        answers = [
            self.dns_answers("93.184.216.34"),
            self.dns_answers("127.0.0.1"),
        ]
        with patch.object(fl.socket, "getaddrinfo", side_effect=answers) as dns:
            with patch.object(fl.requests, "get", return_value=redirected) as get:
                with self.assertRaises(fl.UnsafeRedirectTarget):
                    self.get(start)

        self.assertEqual(dns.call_count, 2)
        get.assert_called_once()

    def test_redirect_rejects_credentials_and_nonstandard_ports(self):
        for target in (
                "https://user:secret@public.example/feed",
                "https://public.example:8443/feed"):
            with self.subTest(target=target):
                with self.assertRaises(fl.UnsafeRedirectTarget):
                    fl.validate_public_redirect_target(target)

    def test_redirect_rejects_ipv6_loopback_and_multicast_literals(self):
        for target in (
                "https://[::1]/feed",
                "http://224.0.0.1/feed",
                "https://[ff0e::1]/feed"):
            with self.subTest(target=target):
                with self.assertRaises(fl.UnsafeRedirectTarget):
                    fl.validate_public_redirect_target(target)

    def test_redirect_dns_failure_is_blocked(self):
        with patch.object(
                fl.socket, "getaddrinfo",
                side_effect=fl.socket.gaierror("not found")):
            with self.assertRaises(fl.UnsafeRedirectTarget):
                fl.validate_public_redirect_target(
                    "https://unresolved.example/feed")

    def test_unsafe_redirect_tombstones_cached_body_and_enters_cooldown(self):
        start = "https://redirect.example/cached-feed"
        first = self.response(
            headers={"ETag": '"v1"', "Cache-Control": "max-age=0"})
        with patch.object(fl.requests, "get", return_value=first):
            self.get(start)

        self.now += timedelta(minutes=1)
        redirected = self.response(
            status=302, content=b"",
            headers={"Location": "http://127.0.0.1/admin"})
        redirected.url = start
        with patch.object(fl.requests, "get", return_value=redirected):
            with self.assertRaises(fl.UnsafeRedirectTarget):
                self.get(start)

        entry = load_entry(self.cache_root, request_key(start))
        self.assertFalse(entry.get("body_sha256"))
        self.assertFalse(entry.get("allow_stale"))
        self.assertTrue(entry.get("retry_at"))
        self.now += timedelta(minutes=1)
        with patch.object(fl.requests, "get") as get:
            with self.assertRaises(fl.FetchCooldown):
                self.get(start)
        get.assert_not_called()

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
        with patch.object(
                fl.socket, "getaddrinfo",
                return_value=self.dns_answers("93.184.216.34")):
            with patch.object(
                    fl.requests, "get", side_effect=[redirected, limited]):
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
