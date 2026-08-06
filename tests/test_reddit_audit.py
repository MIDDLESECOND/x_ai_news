# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import audit_reddit_sources as audit  # noqa: E402


CONFIG = {
    "audit": {
        "duration_days": 14,
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

    def test_partial_collection_preserves_existing_log_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            day_dir = raw_root / "2026-08-05"
            day_dir.mkdir()
            (day_dir / "_audit_log.json").write_text(
                json.dumps({"date": "2026-08-05", "started_at": "earlier",
                            "sources": {"r_previous": {"status": "ok", "items": 3}}}),
                encoding="utf-8")
            old = audit.collect_one
            audit.collect_one = lambda entry, cfg, limit: {
                "source": "r_technical", "subreddit": "Technical", "category": "research",
                "role": "candidate", "fetched_at": "2026-08-05T12:00:00+00:00", "items": []}
            try:
                log = audit.collect(CONFIG, "2026-08-05", only={"technical"}, delay=0,
                                    raw_root=raw_root)
            finally:
                audit.collect_one = old
            self.assertIn("r_previous", log["sources"])
            self.assertIn("r_technical", log["sources"])
            self.assertEqual(log["started_at"], "earlier")

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

    def test_retry_failure_does_not_replace_same_day_success(self):
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
            audit.collect_one = lambda *args: (_ for _ in ()).throw(RuntimeError("retry failed"))
            try:
                log = audit.collect(CONFIG, "2026-08-05", only={"technical"}, delay=0,
                                    raw_root=raw_root)
            finally:
                audit.collect_one = old
            kept = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(kept["items"], [{"title": "kept"}])
            self.assertEqual(log["sources"]["r_technical"]["status"], "ok")
            self.assertIn("retry_error", log["sources"]["r_technical"])

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

            self.assertIn("url:https://arxiv.org/abs/2608.00001", index)


if __name__ == "__main__":
    unittest.main()
