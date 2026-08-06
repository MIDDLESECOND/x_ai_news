# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import reddit_rate_limit as rate  # noqa: E402


POLICY = {
    "enabled": True,
    "min_interval_seconds": 1800,
    "max_requests_per_utc_day": 2,
}


class RedditRateLimitTest(unittest.TestCase):
    def test_configuration_can_tighten_but_cannot_loosen_hard_limits(self):
        self.assertEqual(rate.normalize_policy({
            "min_interval_seconds": 0,
            "max_requests_per_utc_day": 999,
        }), {
            "enabled": True,
            "min_interval_seconds": 1800,
            "max_requests_per_utc_day": 2,
        })
        self.assertEqual(rate.normalize_policy({
            "min_interval_seconds": 3600,
            "max_requests_per_utc_day": 1,
        }), {
            "enabled": True,
            "min_interval_seconds": 3600,
            "max_requests_per_utc_day": 1,
        })

    def test_non_reddit_url_does_not_consume_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            waited = rate.reserve_request(
                "https://example.com/feed", policy=POLICY, state_path=state,
                lock_path=Path(tmp) / "lock")
            self.assertEqual(waited, 0)
            self.assertFalse(state.exists())

    def test_all_reddit_subdomains_share_the_hard_gate(self):
        self.assertTrue(rate.is_reddit_url("https://oauth.reddit.com/api/v1/me"))
        self.assertTrue(rate.is_reddit_url("https://www.reddit.com/r/test/.rss"))
        self.assertFalse(rate.is_reddit_url("https://reddit.com.evil.example/feed"))

    def test_second_request_is_scheduled_thirty_minutes_later(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            lock = Path(tmp) / "lock"
            now = lambda: datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            sleeps = []
            first = rate.reserve_request(
                "https://www.reddit.com/r/a/.rss", policy=POLICY,
                state_path=state, lock_path=lock, now_fn=now, sleep_fn=sleeps.append)
            second = rate.reserve_request(
                "https://www.reddit.com/r/b/.rss", policy=POLICY,
                state_path=state, lock_path=lock, now_fn=now, sleep_fn=sleeps.append)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(first, 0)
            self.assertEqual(second, 1800)
            self.assertEqual(sleeps, [1800])
            self.assertEqual(payload["request_count"], 2)

    def test_failed_or_manual_extra_attempt_cannot_exceed_daily_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            lock = Path(tmp) / "lock"
            now = lambda: datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
            for suffix in ("a", "b"):
                rate.reserve_request(
                    f"https://reddit.com/r/{suffix}/.rss", policy=POLICY,
                    state_path=state, lock_path=lock, now_fn=now, sleep_fn=lambda _: None)
            with self.assertRaises(rate.RedditDailyBudgetExceeded):
                rate.reserve_request(
                    "https://reddit.com/r/c/.rss", policy=POLICY,
                    state_path=state, lock_path=lock, now_fn=now, sleep_fn=lambda _: None)


if __name__ == "__main__":
    unittest.main()
