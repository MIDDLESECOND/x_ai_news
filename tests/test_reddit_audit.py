# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import audit_reddit_sources as audit  # noqa: E402
import reddit_audit_baseline as baseline  # noqa: E402
from reddit_audit_baseline import refresh_l1_baseline  # noqa: E402
from state_io import semantic_hash  # noqa: E402


CONFIG = {
    "audit": {
        "duration_days": 14,
        "max_subreddits_per_run": 2,
        "max_requests_per_day": 2,
        "request_delay_seconds": 0,
        "signal_match_window_days": 30,
        "direct_evidence_domains": ["github.com", "arxiv.org"],
        "technical_marker_groups": {
            "implementation": ["code", "github"],
            "measurement": ["benchmark", "latency"],
            "methods": ["inference"],
        },
        "noise_markers": ["career", "beginner"],
    },
    "subreddits": [
        {"name": "Technical", "category": "research", "role": "candidate"},
        {"name": "General", "category": "general-control", "role": "control"},
    ],
}


class RedditAuditTest(unittest.TestCase):
    def test_url_normalization_removes_tracking_and_fragment(self):
        got = audit.normalize_url("http://GitHub.com/org/repo/?utm_source=x&v=1#readme")
        self.assertEqual(got, "https://github.com/org/repo?v=1")

    def test_feature_proxies_are_explicit_and_reproducible(self):
        item = {
            "title": "Inference benchmark with code",
            "summary": "Latency 24 ms; repo [link]",
            "external_urls": ["https://github.com/acme/eval"],
        }
        features = audit.item_features(item, CONFIG["audit"])
        self.assertTrue(features["technical_proxy"])
        self.assertTrue(features["evidence_proxy"])
        self.assertTrue(features["direct_evidence"])
        self.assertEqual(features["direct_urls"], ["https://github.com/acme/eval"])

    def test_report_stays_provisional_before_fourteen_days(self):
        fetched = datetime(2026, 8, 5, 12, tzinfo=timezone.utc).isoformat()
        records = [{
            "subreddit": "Technical", "category": "research", "role": "candidate",
            "sample_day": "2026-08-05", "fetched_at": fetched,
            "items": [{
                "title": "Inference benchmark with code",
                "url": "https://www.reddit.com/r/Technical/comments/abc/test/",
                "published": "2026-08-05T10:00:00+00:00",
                "summary": "Latency 24 ms https://github.com/acme/eval",
            }],
        }]
        rows = audit.score_rows(CONFIG, records, {})
        technical = next(row for row in rows if row["name"] == "Technical")
        general = next(row for row in rows if row["name"] == "General")
        self.assertEqual(technical["decision"], "采集中 1/14")
        self.assertEqual(general["decision"], "对照组")

    def test_duplicate_rate_ignores_same_signal_months_apart(self):
        def payload(name, published):
            return {
                "subreddit": name, "category": "research", "role": "candidate",
                "sample_day": published[:10], "fetched_at": published,
                "items": [{
                    "title": "Paper discussion",
                    "url": f"https://reddit.com/r/{name}/comments/{name.lower()}/post",
                    "published": published,
                    "summary": "benchmark https://arxiv.org/abs/2608.00001",
                }],
            }

        rows = audit.score_rows(CONFIG, [
            payload("Technical", "2026-01-01T10:00:00+00:00"),
            payload("General", "2026-08-01T10:00:00+00:00"),
        ], {})

        self.assertTrue(all(row["duplicate_rate"] == 0.0 for row in rows))

    def test_generate_report_reads_private_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "audit" / "2026-08-05"
            raw.mkdir(parents=True)
            payload = {
                "source": "r_technical", "subreddit": "Technical", "category": "research",
                "role": "candidate", "fetched_at": "2026-08-05T12:00:00+00:00",
                "items": [{"title": "Code benchmark", "url": "https://reddit.com/x",
                           "published": "2026-08-05T11:00:00+00:00", "summary": "latency 10 ms"}],
            }
            (raw / "r_technical.json").write_text(json.dumps(payload), encoding="utf-8")
            path, rows = audit.generate_report(
                CONFIG, "2026-08-05", raw_root=root / "audit",
                existing_raw=root / "existing", report_dir=root / "reports")
            self.assertTrue(path.exists())
            self.assertIn("不改变正式日报信源", path.read_text(encoding="utf-8"))
            self.assertEqual(next(r for r in rows if r["name"] == "Technical")["unique_posts"], 1)

    def test_daily_budget_preserves_existing_log_and_skips_extra_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            day_dir = raw_root / "2026-08-05"
            day_dir.mkdir()
            (day_dir / "_audit_log.json").write_text(
                json.dumps({"date": "2026-08-05", "started_at": "earlier",
                            "sources": {"r_previous": {"status": "ok", "items": 3}}}),
                encoding="utf-8")
            old = audit.collect_one
            calls = []
            audit.collect_one = lambda *args: calls.append(args)
            try:
                log = audit.collect(CONFIG, "2026-08-05", only={"technical"}, delay=0,
                                    raw_root=raw_root)
            finally:
                audit.collect_one = old
            self.assertIn("r_previous", log["sources"])
            self.assertNotIn("r_technical", log["sources"])
            self.assertEqual(calls, [])
            self.assertEqual(log["started_at"], "earlier")

    def test_default_collection_remains_fair_across_calendar_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            entries = [{"name": f"S{i}"} for i in range(6)]
            first = date(2026, 1, 1)
            selected = []
            for run in range(18):
                day = (first + timedelta(days=run * 10)).isoformat()
                entry = audit.rotating_batch(
                    entries, day, 1, raw_root=raw_root)[0]
                selected.append(entry["name"])
                folder = raw_root / day
                folder.mkdir()
                sid = audit.subreddit_id(entry["name"])
                (folder / f"{sid}.json").write_text(json.dumps({
                    "source": sid, "subreddit": entry["name"],
                    "status": "ok", "items": [],
                }), encoding="utf-8")

            self.assertEqual(
                {name: selected.count(name) for name in {entry["name"] for entry in entries}},
                {f"S{index}": 3 for index in range(6)})

    def test_same_day_success_is_not_requested_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            day_dir = raw_root / "2026-08-05"
            day_dir.mkdir()
            (day_dir / "r_technical.json").write_text(json.dumps({
                "source": "r_technical", "subreddit": "Technical", "status": "ok",
                "fetched_at": "2026-08-05T12:00:00+00:00", "items": [],
            }), encoding="utf-8")
            calls = []
            old = audit.collect_one
            audit.collect_one = lambda *args: calls.append(args)
            try:
                log = audit.collect(CONFIG, "2026-08-05", only={"technical"}, delay=0,
                                    raw_root=raw_root)
            finally:
                audit.collect_one = old
            self.assertEqual(calls, [])
            self.assertEqual(log["selected_sources"], [])

    def test_failed_attempt_is_persisted_and_counts_toward_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            old = audit.collect_one
            audit.collect_one = lambda *args: (_ for _ in ()).throw(RuntimeError("blocked"))
            try:
                audit.collect(CONFIG, "2026-08-05", only={"technical"}, delay=0,
                              raw_root=raw_root)
            finally:
                audit.collect_one = old
            payload = json.loads(
                (raw_root / "2026-08-05" / "r_technical.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            records = audit.load_audit_records(raw_root, "2026-08-05", 14)
            row = next(r for r in audit.score_rows(CONFIG, records, {})
                       if r["name"] == "Technical")
            self.assertEqual(row["sample_days"], 0)
            self.assertEqual(row["attempt_days"], 1)
            self.assertEqual(row["score"], 0)

    def test_error_snapshot_recovery_does_not_spend_a_second_daily_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            day_dir = raw_root / "2026-08-05"
            day_dir.mkdir()
            (day_dir / "r_technical.json").write_text(json.dumps({
                "source": "r_technical", "subreddit": "Technical",
                "status": "error", "error": "interrupted", "items": [],
            }), encoding="utf-8")
            calls = []
            old = audit.collect_one
            audit.collect_one = lambda *args: calls.append(args)
            try:
                log = audit.collect(
                    CONFIG, "2026-08-05", delay=0, raw_root=raw_root)
            finally:
                audit.collect_one = old

            self.assertEqual(calls, [])
            self.assertEqual(log["selected_sources"], [])
            self.assertEqual(log["sources"]["r_technical"]["status"], "error")

    def test_started_snapshot_survives_base_exception_and_blocks_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            calls = []
            old = audit.collect_one

            def interrupted(entry, *_):
                calls.append(("first", entry["name"]))
                raise KeyboardInterrupt()

            audit.collect_one = interrupted
            try:
                with self.assertRaises(KeyboardInterrupt):
                    audit.collect(
                        CONFIG, "2026-08-05", delay=0, raw_root=raw_root)
            finally:
                audit.collect_one = old

            snapshot = json.loads(
                (raw_root / "2026-08-05/r_technical.json").read_text(
                    encoding="utf-8"))
            self.assertEqual(snapshot["status"], "pending")

            audit.collect_one = lambda entry, *_: calls.append(
                ("rerun", entry["name"]))
            try:
                log = audit.collect(
                    CONFIG, "2026-08-05", delay=0, raw_root=raw_root)
            finally:
                audit.collect_one = old

            self.assertEqual(calls, [("first", "Technical")])
            self.assertEqual(log["selected_sources"], [])

    def test_same_day_success_is_kept_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            day_dir = raw_root / "2026-08-05"
            day_dir.mkdir()
            success = {
                "source": "r_technical", "subreddit": "Technical", "category": "research",
                "role": "candidate", "status": "ok",
                "fetched_at": "2026-08-05T12:00:00+00:00", "items": [{"title": "kept"}]}
            path = day_dir / "r_technical.json"
            path.write_text(json.dumps(success), encoding="utf-8")
            old = audit.collect_one
            calls = []
            audit.collect_one = lambda *args: calls.append(args)
            try:
                log = audit.collect(CONFIG, "2026-08-05", only={"technical"}, delay=0,
                                    raw_root=raw_root)
            finally:
                audit.collect_one = old
            kept = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["items"], [{"title": "kept"}])
            self.assertEqual(log["sources"]["r_technical"]["status"], "ok")
            self.assertEqual(calls, [])

    def test_only_full_pool_logs_count_as_completed_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            complete = raw_root / "2026-08-04"
            partial = raw_root / "2026-08-05"
            complete.mkdir()
            partial.mkdir()
            (complete / "_audit_log.json").write_text(json.dumps({
                "finished_at": "done", "sources": {"r_a": {}, "r_b": {}}}), encoding="utf-8")
            (partial / "_audit_log.json").write_text(json.dumps({
                "finished_at": "done", "sources": {"r_a": {}}}), encoding="utf-8")
            self.assertEqual(
                audit.completed_attempt_days(raw_root, "2026-08-05", required_sources=2),
                ["2026-08-04"])

    def test_report_loads_latest_complete_samples_across_calendar_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp) / "audit"
            first = date(2026, 7, 10)
            for index in range(14):
                day = (first + timedelta(days=index * 2)).isoformat()
                folder = raw_root / day
                folder.mkdir(parents=True)
                (folder / "_audit_log.json").write_text(json.dumps({
                    "finished_at": "done",
                    "sources": {"r_technical": {}, "r_general": {}},
                }), encoding="utf-8")
                (folder / "r_technical.json").write_text(json.dumps({
                    "source": "r_technical", "subreddit": "Technical",
                    "category": "research", "role": "candidate", "status": "ok",
                    "fetched_at": f"{day}T12:00:00+00:00", "items": [],
                }), encoding="utf-8")

            end_day = (first + timedelta(days=26)).isoformat()
            selected = audit.audit_sample_days(
                raw_root, end_day, 14, {"r_technical", "r_general"})
            records = audit.load_audit_records(raw_root, end_day, 14, sample_days=selected)

            self.assertEqual(len(selected), 14)
            self.assertEqual(len(records), 14)
            self.assertEqual(records[0]["sample_day"], first.isoformat())

    def test_existing_index_consumes_preserved_external_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "2026-08-05"
            folder.mkdir()
            (folder / "feed.json").write_text(json.dumps({
                "fetched_at": "2026-08-05T12:00:00+00:00",
                "items": [{
                    "title": "Paper discussion",
                    "url": "https://aggregator.test/post",
                    "published": "2026-08-05T10:00:00+00:00",
                    "summary": "[link]",
                    "external_urls": ["https://arxiv.org/abs/2608.00001"],
                }],
            }), encoding="utf-8")

            index = audit.existing_index(root, "2026-08-05", 14)

            self.assertIn(
                baseline.url_signal_key("https://arxiv.org/abs/2608.00001"),
                index)

    def test_retention_covers_every_rotation_needed_for_completion(self):
        config = {
            "audit": {"duration_days": 14, "keep_days": 240},
            "subreddits": [{"name": f"S{index}"} for index in range(60)],
        }

        self.assertGreaterEqual(
            audit.retention_days(config),
            14 * len(config["subreddits"]) + 7)

    def test_pruning_preserves_minimum_attempts_across_long_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            first = date(2020, 1, 1)
            for index in range(15):
                folder = raw_root / (first + timedelta(days=index)).isoformat()
                folder.mkdir()
                (folder / "r_sparse.json").write_text(json.dumps({
                    "source": "r_sparse", "subreddit": "Sparse",
                    "status": "ok", "items": [],
                }), encoding="utf-8")

            audit.prune_audit(
                raw_root, "2026-08-06", 30, minimum_attempts=14,
                protected_sources={"r_sparse"})

            counts, _ = audit.audit_attempt_history(raw_root, "2026-08-06")
            self.assertEqual(counts["r_sparse"], 14)

    def test_report_uses_retained_attempts_across_long_calendar_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "audit"
            config = {
                "audit": dict(CONFIG["audit"], duration_days=3),
                "subreddits": [
                    {"name": f"S{index}", "category": "research",
                     "role": "candidate"}
                    for index in range(6)
                ],
            }
            first = date(2025, 1, 1)
            for run in range(18):
                day = (first + timedelta(days=run * 10)).isoformat()
                entry = audit.rotating_batch(
                    config["subreddits"], day, 1, raw_root=raw_root)[0]
                folder = raw_root / day
                folder.mkdir(parents=True)
                sid = audit.subreddit_id(entry["name"])
                (folder / f"{sid}.json").write_text(json.dumps({
                    "source": sid, "subreddit": entry["name"],
                    "category": "research", "role": "candidate",
                    "status": "ok", "items": [],
                }), encoding="utf-8")

            end_day = (first + timedelta(days=170)).isoformat()
            _, rows = audit.generate_report(
                config, end_day, raw_root=raw_root,
                existing_raw=root / "existing", report_dir=root / "reports")

            self.assertTrue(all(row["attempt_days"] == 3 for row in rows))

    def test_compact_l1_baseline_preserves_old_comparison_after_raw_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_root = root / "audit"
            existing_raw = root / "raw"
            baseline_root = raw_root / "l1_baseline"
            config = {
                "audit": dict(CONFIG["audit"], duration_days=3),
                "subreddits": [
                    {"name": "Technical", "category": "research",
                     "role": "candidate"},
                ],
            }
            days = ["2026-01-01", "2026-01-11", "2026-01-21"]
            for index, day in enumerate(days):
                folder = raw_root / day
                folder.mkdir(parents=True)
                items = ([{
                    "title": "Benchmark discussion",
                    "url": "https://reddit.com/r/Technical/comments/a/post",
                    "published": "2026-01-01T10:00:00+00:00",
                    "summary": "benchmark code https://github.com/acme/eval?id=alpha",
                }] if index == 0 else [])
                (folder / "r_technical.json").write_text(json.dumps({
                    "source": "r_technical", "subreddit": "Technical",
                    "category": "research", "role": "candidate",
                    "status": "ok", "fetched_at": f"{day}T12:00:00+00:00",
                    "items": items,
                }), encoding="utf-8")

            official = existing_raw / "2026-01-01"
            official.mkdir(parents=True)
            official_path = official / "official.json"
            official_path.write_text(json.dumps({
                "fetched_at": "2026-01-01T13:00:00+00:00",
                "items": [{
                    "title": "Independent coverage",
                    "published": "2026-01-01T11:00:00+00:00",
                    "summary": "https://github.com/acme/eval?id=alpha",
                }],
            }), encoding="utf-8")
            (official / "_fetch_log.json").write_text(json.dumps({
                "date": "2026-01-01", "run_mode": "full",
                "completed_at": "2026-01-01T13:01:00+00:00",
                "sources": {"official": {
                    "status": "ok",
                    "snapshot_hash": semantic_hash(official_path.read_bytes()),
                }},
            }), encoding="utf-8")
            refresh_l1_baseline(
                existing_raw, baseline_root, "2026-01-21", {"official"}, 900)
            compact = json.loads(
                (baseline_root / "2026-01-01.json").read_text(
                    encoding="utf-8"))
            self.assertNotIn("summary", compact["items"][0])
            self.assertEqual(
                compact["items"][0]["urls"],
                ["https://github.com/acme/eval"])
            # The live raw window no longer contains the old day; only the
            # compact URL/title/timestamp baseline can support the comparison.
            official_path.unlink()
            (official / "_fetch_log.json").unlink()
            official.rmdir()
            existing_raw.rmdir()
            refresh_l1_baseline(
                existing_raw, baseline_root, "2026-01-21", set(), 900)

            _, rows = audit.generate_report(
                config, "2026-01-21", raw_root=raw_root,
                existing_raw=existing_raw, report_dir=root / "reports")

            self.assertEqual(rows[0]["existing_matches"], 1)

    def test_compact_baseline_rejects_non_urls_userinfo_and_invalid_time(self):
        item = {
            "url": "secret prose",
            "external_url": "https://user:pass@example.com/private",
            "external_urls": ["more arbitrary content"],
            "summary": "See https://example.com/path?token=secret#part",
        }

        self.assertEqual(
            baseline._item_urls(item), ["https://example.com/path"])
        self.assertIsNone(baseline._safe_when("x" * 1000))
        query_url = "https://example.com/search?id=alpha&utm_source=test"
        self.assertEqual(
            baseline._safe_url(query_url), "https://example.com/search")
        self.assertEqual(
            baseline.url_signal_key(query_url),
            baseline.url_signal_key(audit.normalize_url(query_url)))

    def test_compact_baseline_requires_full_log_and_matching_snapshot_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = Path(tmp) / "2026-08-06"
            day_dir.mkdir()
            payload_path = day_dir / "official.json"
            payload_path.write_text(json.dumps({
                "fetched_at": "2026-08-06T12:00:00+00:00",
                "items": [{"title": "Item", "url": "https://example.com/x"}],
            }), encoding="utf-8")
            (day_dir / "_fetch_log.only.json").write_text(
                "{}", encoding="utf-8")
            self.assertEqual(baseline.compact_l1_day(day_dir, {"official"}), [])

            (day_dir / "_fetch_log.json").write_text(json.dumps({
                "date": "2026-08-06", "run_mode": "full",
                "completed_at": "2026-08-06T12:01:00+00:00",
                "sources": {"official": {"snapshot_hash": "wrong"}},
            }), encoding="utf-8")
            self.assertEqual(baseline.compact_l1_day(day_dir, {"official"}), [])

    def test_compact_baseline_retains_oldest_audit_comparison_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_root = Path(tmp) / "audit"
            baseline_root = audit_root / "l1_baseline"
            baseline_root.mkdir(parents=True)
            snapshot_dir = audit_root / "2026-01-01"
            snapshot_dir.mkdir()
            (snapshot_dir / "r_sparse.json").write_text(json.dumps({
                "source": "r_sparse", "subreddit": "Sparse",
                "status": "ok", "fetched_at": "2026-01-01T12:00:00+00:00",
                "items": [{"published": "2024-01-01T12:00:00+00:00"}],
            }), encoding="utf-8")
            old = baseline_root / "2023-12-02.json"
            old.write_text('{"version": 1, "items": []}\n', encoding="utf-8")

            refresh_l1_baseline(
                Path(tmp) / "missing-raw", baseline_root,
                "2028-08-06", set(), 900, 30, 14)

            self.assertTrue(old.exists())
            self.assertEqual(
                baseline._subtract_days_saturated(date.min, 30), date.min)


if __name__ == "__main__":
    unittest.main()
