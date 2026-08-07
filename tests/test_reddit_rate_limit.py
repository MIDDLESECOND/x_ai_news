# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_wait_scheduled_after_midnight_counts_against_execution_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            lock = Path(tmp) / "lock"
            moments = [
                datetime(2026, 8, 6, 23, 40, tzinfo=timezone.utc),
                datetime(2026, 8, 6, 23, 50, tzinfo=timezone.utc),
                datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc),
            ]
            for index, moment in enumerate(moments):
                rate.reserve_request(
                    f"https://reddit.com/r/{index}/.rss", policy=POLICY,
                    state_path=state, lock_path=lock,
                    now_fn=lambda moment=moment: moment,
                    sleep_fn=lambda _: None)

            with self.assertRaises(rate.RedditDailyBudgetExceeded):
                rate.reserve_request(
                    "https://reddit.com/r/overflow/.rss", policy=POLICY,
                    state_path=state, lock_path=lock,
                    now_fn=lambda: datetime(
                        2026, 8, 7, 0, 41, tzinfo=timezone.utc),
                    sleep_fn=lambda _: None)

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["scheduled_counts"]["2026-08-07"], 2)

    def test_legacy_same_day_count_remains_enforced_after_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            lock = Path(tmp) / "lock"
            state.write_text(json.dumps({
                "utc_date": "2026-08-06",
                "request_count": 2,
                "last_reserved_at": "2026-08-06T12:30:00+00:00",
                "reservations": [],
            }), encoding="utf-8")

            with self.assertRaises(rate.RedditDailyBudgetExceeded):
                rate.reserve_request(
                    "https://reddit.com/r/blocked/.rss", policy=POLICY,
                    state_path=state, lock_path=lock,
                    now_fn=lambda: datetime(
                        2026, 8, 6, 13, tzinfo=timezone.utc),
                    sleep_fn=lambda _: None)

    def test_stale_lock_file_without_a_live_kernel_owner_is_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            lock = Path(tmp) / "lock"
            lock.write_text("old-owner\n", encoding="ascii")
            os.utime(lock, (1, 1))

            rate.reserve_request(
                "https://reddit.com/r/allowed/.rss", policy=POLICY,
                state_path=state, lock_path=lock,
                now_fn=lambda: datetime(
                    2026, 8, 6, 13, tzinfo=timezone.utc),
                sleep_fn=lambda _: None)

            self.assertTrue(state.exists())
            self.assertTrue(lock.exists())

    def test_live_kernel_owner_rejects_a_second_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            with rate._budget_lock(lock):
                with self.assertRaises(rate.RedditDailyBudgetExceeded):
                    with rate._budget_lock(lock):
                        self.fail("second owner unexpectedly acquired the lock")

    def test_owner_token_change_stops_state_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            with rate._budget_lock(lock) as (token, fd):
                os.lseek(fd, 1, os.SEEK_SET)
                os.write(fd, b"replacement\n")
                os.ftruncate(fd, 1 + len("replacement\n"))
                with self.assertRaises(rate.RedditDailyBudgetExceeded):
                    rate._assert_budget_lock_owned(lock, fd, token)

    def test_replaced_lock_path_is_not_the_owned_kernel_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "lock"
            with rate._budget_lock(lock) as (token, fd):
                opened = os.fstat(fd)
                replacement = SimpleNamespace(
                    st_dev=opened.st_dev, st_ino=opened.st_ino + 1)
                with (patch.object(Path, "stat", return_value=replacement),
                      self.assertRaises(rate.RedditDailyBudgetExceeded)):
                    rate._assert_budget_lock_owned(lock, fd, token)


if __name__ == "__main__":
    unittest.main()
