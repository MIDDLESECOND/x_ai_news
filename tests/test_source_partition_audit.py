# -*- coding: utf-8 -*-
import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import audit_source_partitions as audit  # noqa: E402


CONFIG = {
    "audit": {
        "duration_days": 14, "keep_days": 21, "request_delay_seconds": 0,
        "signal_match_window_days": 30,
    },
    "partitions": {
        "release": {"section": "今日发布", "label": "发布", "profile": "release"},
        "field_test": {"section": "一线实测", "label": "实测", "profile": "field_test"},
        "research_synthesis": {
            "section": "技术综述/研究解读", "label": "技术综述/研究解读",
            "profile": "research_synthesis", "shadow_only": True,
        },
        "pricing": {"section": "定价与额度变动", "label": "定价", "profile": "pricing"},
        "degradation": {"section": "降智观察", "label": "降智", "profile": "degradation"},
        "company": {"section": "公司动态", "label": "公司", "profile": "company"},
    },
    "candidates": [{"id": "trial_price", "enabled": True}],
    "coverage": {"targets": {"Vendor": {
        "release": {"incumbent": ["official_feed"]},
        "pricing": {"trial": ["trial_price"]},
        "degradation": {"blocked": "anonymous 403"},
        "company": {},
    }}},
}

TOPICS = {
    "model_keywords": ["Qwen"],
    "topics": {"release": {"section": "今日发布", "keywords": ["release"]}},
}


class SourcePartitionAuditTest(unittest.TestCase):
    def test_collection_uses_isolated_http_state_and_logical_day(self):
        config = {
            "audit": {"keep_days": 21, "request_delay_seconds": 0},
            "candidates": [{
                "id": "candidate", "name": "Candidate", "type": "state_test",
                "tier": "community", "enabled": True,
            }],
        }
        seen = {}

        def fetcher(source):
            seen["state"] = audit.fetch_l1.STATE_DIR
            seen["cache"] = audit.fetch_l1.HTTP_CACHE_ROOT
            seen["day"] = audit.fetch_l1.HTTP_LOGICAL_DAY
            return []

        old_fetcher = audit.fetch_l1.FETCHERS.get("state_test")
        old_state = audit.fetch_l1.STATE_DIR
        old_cache = audit.fetch_l1.HTTP_CACHE_ROOT
        old_day = audit.fetch_l1.HTTP_LOGICAL_DAY
        audit.fetch_l1.FETCHERS["state_test"] = fetcher
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                state = root / "shadow-state"
                audit.collect_candidates(
                    config, "2026-08-05", delay=0, raw_root=root / "raw",
                    state_root=state, today=date(2026, 8, 5))
        finally:
            if old_fetcher is None:
                audit.fetch_l1.FETCHERS.pop("state_test", None)
            else:
                audit.fetch_l1.FETCHERS["state_test"] = old_fetcher

        self.assertEqual(seen, {
            "state": state,
            "cache": state / "http_cache",
            "day": "2026-08-05",
        })
        self.assertEqual(audit.fetch_l1.STATE_DIR, old_state)
        self.assertEqual(audit.fetch_l1.HTTP_CACHE_ROOT, old_cache)
        self.assertEqual(audit.fetch_l1.HTTP_LOGICAL_DAY, old_day)

    def test_same_day_success_is_not_fetched_again(self):
        config = {
            "audit": {"keep_days": 21, "request_delay_seconds": 0},
            "candidates": [{
                "id": "candidate", "name": "Candidate", "type": "rerun_test",
                "tier": "community", "enabled": True,
            }],
        }
        fetcher = Mock(return_value=[])
        old_fetcher = audit.fetch_l1.FETCHERS.get("rerun_test")
        audit.fetch_l1.FETCHERS["rerun_test"] = fetcher
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                kwargs = {
                    "delay": 0, "raw_root": root / "raw",
                    "state_root": root / "state", "today": date(2026, 8, 5),
                }
                audit.collect_candidates(config, "2026-08-05", **kwargs)
                audit.collect_candidates(config, "2026-08-05", **kwargs)
        finally:
            if old_fetcher is None:
                audit.fetch_l1.FETCHERS.pop("rerun_test", None)
            else:
                audit.fetch_l1.FETCHERS["rerun_test"] = old_fetcher

        fetcher.assert_called_once()

    def test_collection_rejects_historical_sample_day_before_fetch(self):
        config = {
            "audit": {"keep_days": 21, "request_delay_seconds": 0},
            "candidates": [{
                "id": "candidate", "name": "Candidate", "type": "history_test",
                "tier": "community", "enabled": True,
            }],
        }
        fetcher = Mock(return_value=[])
        old_fetcher = audit.fetch_l1.FETCHERS.get("history_test")
        audit.fetch_l1.FETCHERS["history_test"] = fetcher
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ValueError, "历史日期"):
                    audit.collect_candidates(
                        config, "2026-08-04", delay=0,
                        raw_root=Path(tmp) / "raw", state_root=Path(tmp) / "state",
                        today=date(2026, 8, 5))
        finally:
            if old_fetcher is None:
                audit.fetch_l1.FETCHERS.pop("history_test", None)
            else:
                audit.fetch_l1.FETCHERS["history_test"] = old_fetcher

        fetcher.assert_not_called()

    def test_named_low_frequency_author_has_auditable_policy(self):
        config = audit.load_config()
        source = next(s for s in config["candidates"] if s["id"] == "trial_lilian_weng")

        self.assertEqual(source["url"], "https://lilianweng.github.io/index.xml")
        self.assertEqual(source["cadence"], "low_frequency")
        self.assertEqual(source["evidence_role"], "original_author")
        self.assertGreater(source["lookback_days"], config["audit"]["duration_days"])

    def test_expert_author_catalog_covers_sampling_rotation_and_archive_groups(self):
        config = audit.load_config()
        authors = [source for source in config["candidates"]
                   if source.get("track") == "expert_author"]
        by_group = {}
        for source in authors:
            by_group.setdefault(source["audit_group"], []).append(source)

        self.assertEqual(len(by_group["first_batch"]), 10)
        self.assertEqual(len(by_group["rotation"]), 3)
        self.assertEqual(len(by_group["historical_archive"]), 3)
        jay = next(source for source in authors if source["name"] == "Jay Alammar")
        shreya = next(source for source in authors if source["name"] == "Shreya Shankar")
        self.assertEqual(jay["url"], "https://newsletter.languagemodels.co/feed")
        self.assertEqual(shreya["url"], "https://www.sh-reya.com/rss.xml")
        self.assertEqual(audit.expert_author_config_errors(config), [])

    def test_explicit_only_selects_rotation_but_never_archive(self):
        config = {"candidates": [
            {"id": "daily", "enabled": True},
            {"id": "rotation", "enabled": False, "audit_group": "rotation"},
            {"id": "archive", "enabled": False, "audit_group": "historical_archive"},
        ]}

        selected = audit.candidates_for_collection(config, {"rotation", "archive"})

        self.assertEqual([source["id"] for source in selected], ["rotation"])

    def test_expert_author_guard_rejects_affiliation_without_conflict_keywords(self):
        config = audit.load_config()
        broken = dict(config)
        broken["candidates"] = [dict(source) for source in config["candidates"]]
        eugene = next(source for source in broken["candidates"]
                      if source["id"] == "trial_eugene_yan")
        eugene.pop("conflict_keywords")

        self.assertIn(
            "trial_eugene_yan: affiliation requires conflict_keywords",
            audit.expert_author_config_errors(broken))

    def test_expert_features_use_transient_fulltext_and_record_conflicts(self):
        source = {
            "track": "expert_author", "affiliation": "Anthropic",
            "conflict_keywords": ["Anthropic", "Claude"],
        }
        items = [{
            "title": "Evaluation notes", "summary": "short summary",
            "url": "https://author.test/eval",
            "external_urls": ["https://github.com/acme/eval"],
            "_audit_fulltext": (
                "We ran a controlled experiment with a benchmark baseline, measured latency, "
                "and evaluated Claude at Anthropic."),
        }]

        audit.annotate_candidate_items(source, items)

        self.assertNotIn("_audit_fulltext", items[0])
        features = items[0]["expert_features"]
        self.assertGreaterEqual(len(features["method_hits"]), 2)
        self.assertIn("we ran", features["firsthand_hits"])
        self.assertIn("Anthropic", features["conflict_keywords"])
        self.assertTrue(features["fulltext_used"])

    def test_expert_snapshot_persists_features_but_not_third_party_fulltext(self):
        config = {
            "audit": {"keep_days": 21, "request_delay_seconds": 0},
            "candidates": [{
                "id": "author", "name": "Author", "type": "expert_test",
                "tier": "community", "track": "expert_author",
                "audit_group": "first_batch", "audit_status": "shadow_sampling",
                "evidence_role": "original_author", "routing": "expert_author",
                "partitions": ["field_test", "research_synthesis"], "enabled": True,
            }],
        }
        item = {
            "title": "Evaluation", "summary": "short", "url": "https://author.test/post",
            "_audit_fulltext": "secret-tail controlled experiment benchmark baseline",
            "external_urls": ["https://github.com/acme/eval"],
        }
        old = audit.fetch_l1.FETCHERS.get("expert_test")
        audit.fetch_l1.FETCHERS["expert_test"] = lambda source: [dict(item)]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                audit.collect_candidates(
                    config, "2026-08-05", delay=0, raw_root=root,
                    state_root=root / "state", today=date(2026, 8, 5))
                payload = json.loads(
                    (root / "2026-08-05" / "author.json").read_text(encoding="utf-8"))
        finally:
            if old is None:
                audit.fetch_l1.FETCHERS.pop("expert_test", None)
            else:
                audit.fetch_l1.FETCHERS["expert_test"] = old

        serialized = json.dumps(payload)
        self.assertNotIn("_audit_fulltext", serialized)
        self.assertNotIn("secret-tail", serialized)
        self.assertIn("expert_features", serialized)

    def test_expert_body_fallback_is_bounded_and_ephemeral(self):
        source = {
            "id": "author", "url": "https://author.test/feed.xml",
            "track": "expert_author", "audit_body_fallback_items": 1,
            "audit_body_fallback_delay_seconds": 0,
        }
        items = [{
            "title": "Post", "url": "https://author.test/post", "summary": "short",
            "_audit_fulltext": "short",
        }]
        response = Mock(
            text=("<html><body>controlled experiment benchmark baseline "
                  '<a href="https://arxiv.org/abs/2608.00001">paper</a></body></html>'),
            encoding="utf-8", apparent_encoding="utf-8")
        with patch.object(
                audit.fetch_l1, "http_get", return_value=response) as get:
            audit.hydrate_expert_fulltext(source, items)
        get.assert_called_once_with(
            "https://author.test/post",
            accept="text/html,application/xhtml+xml",
            source=source,
            trusted_parent_url="https://author.test/feed.xml")
        audit.annotate_candidate_items(source, items)

        self.assertNotIn("_audit_fulltext", items[0])
        self.assertIn("https://arxiv.org/abs/2608.00001", items[0]["external_urls"])
        self.assertGreaterEqual(len(items[0]["expert_features"]["method_hits"]), 2)

    def test_expert_body_fallback_rejects_private_item_url_before_network(self):
        source = {
            "id": "author", "url": "https://author.test/feed.xml",
            "track": "expert_author", "audit_body_fallback_items": 1,
            "audit_body_fallback_delay_seconds": 0,
        }
        items = [{"url": "http://127.0.0.1/private", "_audit_fulltext": "short"}]

        with tempfile.TemporaryDirectory() as tmp:
            with (patch.object(
                    audit.fetch_l1, "HTTP_CACHE_ROOT", Path(tmp) / "http-cache"),
                  patch.object(audit.fetch_l1.requests, "get") as get):
                audit.hydrate_expert_fulltext(source, items)

        get.assert_not_called()
        self.assertIn("UnsafeRedirectTarget", items[0]["_audit_body_error"])

    def test_expert_router_separates_firsthand_test_from_synthesis(self):
        def payload(item):
            return {
                "source": "author", "name": "Author", "tier": "community",
                "track": "expert_author", "evidence_role": "original_author",
                "role": "candidate", "routing": "expert_author",
                "partitions": ["field_test", "research_synthesis"], "status": "ok",
                "sample_day": "2026-08-05", "lookback_days": 400, "items": [item],
            }

        experiment = {
            "title": "Evaluation", "url": "https://author.test/eval",
            "published": "2026-08-01T10:00:00+00:00", "summary": "short",
            "external_urls": ["https://github.com/acme/eval"],
            "expert_features": {
                "technical_groups": ["evaluation", "engineering"],
                "method_hits": ["controlled", "baseline"], "synthesis_hits": [],
                "firsthand_hits": ["we tested"],
                "artifact_urls": ["https://github.com/acme/eval"],
                "conflict_keywords": [], "fulltext_used": True,
            },
        }
        synthesis = {
            "title": "Transformer guide", "url": "https://author.test/guide",
            "published": "2026-08-01T10:00:00+00:00", "summary": "short",
            "external_urls": ["https://arxiv.org/abs/2608.00001"],
            "expert_features": {
                "technical_groups": ["model_systems", "research"],
                "method_hits": [], "synthesis_hits": ["guide", "overview"],
                "firsthand_hits": [],
                "artifact_urls": ["https://arxiv.org/abs/2608.00001"],
                "conflict_keywords": [], "fulltext_used": True,
            },
        }

        self.assertEqual(audit.route_payload(payload(experiment), CONFIG, TOPICS)[0][0],
                         "field_test")
        self.assertEqual(audit.route_payload(payload(synthesis), CONFIG, TOPICS)[0][0],
                         "research_synthesis")

    def test_affiliated_product_post_is_visible_with_conflict_label(self):
        item = {
            "title": "Codex workflow", "url": "https://author.test/codex",
            "published": "2026-08-01T10:00:00+00:00", "summary": "short",
            "expert_features": {
                "technical_groups": ["model_systems"], "method_hits": [],
                "firsthand_hits": [], "synthesis_hits": [], "artifact_urls": [],
                "conflict_keywords": ["Codex"], "fulltext_used": True,
                "fulltext_chars": 500,
            },
        }
        payload = {
            "source": "author", "name": "Author", "tier": "community",
            "track": "expert_author", "evidence_role": "original_author",
            "affiliation": "OpenAI (Codex team)", "routing": "expert_author",
            "partitions": ["field_test", "research_synthesis"], "status": "ok",
            "sample_day": "2026-08-05", "lookback_days": 400, "items": [item],
        }

        routed = audit.route_payload(payload, CONFIG, TOPICS)
        quality = audit.item_quality(
            "research_synthesis", item, "community", evidence_role="original_author")

        self.assertEqual(routed[0][0], "research_synthesis")
        self.assertTrue(quality["quality"])
        self.assertTrue(quality["conflict"])

    def test_expert_report_exposes_affiliation_and_conflict_column(self):
        config = audit.load_config()
        report = audit.render_report(config, [], "2026-08-05", 0)

        self.assertIn("技术综述/研究解读信源", report)
        self.assertIn("利益关系命中", report)
        self.assertIn("独家率", report)
        self.assertIn("Eugene Yan", report)
        self.assertIn("Anthropic", report)
        self.assertIn("historical_archive", report)

    def test_forced_route_does_not_depend_on_topic_keywords(self):
        payload = {"status": "ok", "routing": "forced", "partitions": ["pricing"],
                   "items": [{"title": "price table", "url": "https://vendor.test/pricing"}]}
        self.assertEqual(audit.route_payload(payload, CONFIG, TOPICS)[0][0], "pricing")

    def test_forced_recent_route_excludes_old_feed_items(self):
        payload = {
            "status": "ok", "routing": "forced_recent", "partitions": ["degradation"],
            "sample_day": "2026-08-05",
            "items": [
                {"title": "old", "url": "https://status.test/old",
                 "published": "Tue, 01 Jul 2026 10:00:00 GMT"},
                {"title": "recent", "url": "https://status.test/recent",
                 "published": "Tue, 04 Aug 2026 10:00:00 GMT"},
            ],
        }
        routed = audit.route_payload(payload, CONFIG, TOPICS)
        self.assertEqual([item["title"] for _, item in routed], ["recent"])

    def test_classified_route_uses_digest_section_rules(self):
        payload = {
            "source": "trial", "name": "Trial", "tier": "community", "status": "ok",
            "routing": "classify", "partitions": ["release"], "sample_day": "2026-08-05",
            "items": [{"title": "Qwen release", "url": "https://example.test/qwen",
                       "published": "2026-08-05T10:00:00+00:00", "summary": "new weights"}],
        }
        routed = audit.route_payload(payload, CONFIG, TOPICS)
        self.assertEqual([key for key, _ in routed], ["release"])

    def test_disallowed_topic_cannot_eclipse_an_allowed_partition(self):
        payload = {
            "source": "author", "name": "Author", "tier": "community", "status": "ok",
            "routing": "classify", "partitions": ["field_test"], "sample_day": "2026-08-05",
            "items": [{"title": "Harness engineering", "url": "https://author.test/post",
                       "published": "2026-08-05T10:00:00+00:00",
                       "summary": "OpenAI OpenAI OpenAI controlled harness",
                       "external_urls": ["https://github.com/acme/harness"]}],
        }
        topics = {
            "model_keywords": [],
            "topics": {
                "field": {"section": "一线实测", "keywords": ["harness"]},
                "company": {"section": "公司动态", "keywords": ["OpenAI"]},
            },
        }

        routed = audit.route_payload(payload, CONFIG, topics)

        self.assertEqual([key for key, _ in routed], ["field_test"])
        self.assertEqual(routed[0][1]["external_urls"], ["https://github.com/acme/harness"])

    def test_audit_discovery_keywords_do_not_mutate_formal_topics(self):
        base = {"model_keywords": ["Qwen"], "topics": {}}
        config = {"audit": {"discovery_keywords": ["Gemini"]}}
        expanded = audit.topics_for_audit(base, config)
        self.assertEqual(expanded["model_keywords"], ["Qwen", "Gemini"])
        self.assertEqual(base["model_keywords"], ["Qwen"])

    def test_partition_quality_profiles_do_not_share_one_metric(self):
        price = {"title": "API pricing", "summary": "$2 per million tokens", "url": "https://v.test"}
        field = {"title": "benchmark", "summary": "controlled A/B latency benchmark with baseline",
                 "url": "https://github.com/acme/eval"}
        self.assertTrue(audit.item_quality("pricing", price, "official")["quality"])
        self.assertFalse(audit.item_quality("field_test", price, "official")["quality"])
        self.assertTrue(audit.item_quality("field_test", field, "community")["quality"])

    def test_field_test_artifact_link_does_not_promote_synthesis_to_primary(self):
        item = {
            "title": "Benchmark summary",
            "summary": "controlled benchmark methodology https://arxiv.org/abs/2608.00001",
            "url": "https://aggregator.test/summary",
        }
        synthesis = audit.item_quality(
            "field_test", item, "aggregator", evidence_role="synthesis")
        original = audit.item_quality(
            "field_test", item, "community", evidence_role="original_author")

        self.assertTrue(synthesis["quality"])
        self.assertFalse(synthesis["primary"])
        self.assertTrue(original["primary"])

    def test_unreadable_probe_never_gets_specialized_quality_credit(self):
        item = {"title": "[页面不可读] status", "summary": "service status",
                "url": "https://status.test", "readable": False}
        features = audit.item_quality("degradation", item, "official")
        self.assertFalse(features["quality"])
        self.assertTrue(features["noise"])

    def test_only_complete_candidate_pool_days_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for day, count in (("2026-08-04", 2), ("2026-08-05", 1)):
                folder = root / day
                folder.mkdir()
                (folder / "_audit_log.json").write_text(json.dumps({
                    "finished_at": "done", "sources": {f"s{i}": {} for i in range(count)}}),
                    encoding="utf-8")
            self.assertEqual(audit.completed_days(root, "2026-08-05", 2), ["2026-08-04"])

    def test_coverage_report_distinguishes_current_trial_and_blocked(self):
        report = audit.render_report(CONFIG, [], "2026-08-05", 0)
        self.assertIn("✅ official_feed", report)
        self.assertIn("🧪 trial_price", report)
        self.assertIn("⛔ anonymous 403", report)
        self.assertIn("不自动修改 config/sources.yaml", report)

    def test_coverage_cells_distinguish_manual_and_not_applicable(self):
        self.assertEqual(audit.cell_text({"manual": "Chrome only"}), "👁 Chrome only")
        self.assertEqual(audit.cell_text({"not_applicable": "no hosted API"}), "➖ no hosted API")

    def test_coverage_references_must_resolve_to_real_source_ids(self):
        self.assertEqual(audit.coverage_reference_errors(CONFIG, {"official_feed"}), [])
        self.assertEqual(
            audit.coverage_reference_errors(CONFIG, set()),
            ["Vendor.release: unknown incumbent official_feed"])

    def test_model_keywords_cannot_silently_fall_out_of_target_matrix(self):
        config = {"coverage": {
            "targets": {"Meta / Llama": {}},
            "target_aliases": {"Meta / Llama": ["Llama"]},
        }}
        self.assertEqual(audit.coverage_target_errors(config, ["Llama"]), [])
        self.assertEqual(
            audit.coverage_target_errors(config, ["Llama", "Mistral"]),
            ["model keyword missing coverage target: Mistral"])

    def test_candidate_with_zero_routed_items_remains_visible(self):
        payload = {
            "source": "trial_price", "name": "Trial", "tier": "official", "role": "candidate",
            "origin": "candidate", "routing": "classify", "partitions": ["pricing"],
            "status": "ok", "sample_day": "2026-08-05", "fetched_at": "2026-08-05T12:00:00+00:00",
            "items": [{"title": "unrelated", "url": "https://example.test/unrelated",
                       "published": "2026-08-05T11:00:00+00:00", "summary": "nothing"}],
        }
        rows = audit.build_rows(CONFIG, [payload], TOPICS)
        row = next(row for row in rows if row["source_id"] == "trial_price")
        self.assertEqual(row["qualified"], 0)
        self.assertEqual(row["decision"], "采集中 1/14")

    def test_configured_candidate_without_snapshot_remains_visible(self):
        rows = audit.build_rows(CONFIG, [], TOPICS)

        row = next(row for row in rows if row["source_id"] == "trial_price")
        self.assertEqual(row["sample_days"], 0)
        self.assertEqual(row["decision"], "未采集")

    def test_later_unreadable_probe_reduces_cross_day_reliability(self):
        base = {
            "source": "trial_price", "name": "Price", "tier": "official", "role": "candidate",
            "origin": "candidate", "routing": "forced", "partitions": ["pricing"], "status": "ok",
        }
        first = dict(base, sample_day="2026-08-04", fetched_at="2026-08-04T12:00:00+00:00",
                     items=[{"title": "API pricing", "url": "https://v.test/pricing",
                             "summary": "$2 per million tokens", "readable": True}])
        second = dict(base, sample_day="2026-08-05", fetched_at="2026-08-05T12:00:00+00:00",
                      items=[{"title": "[页面不可读] API pricing", "url": "https://v.test/pricing",
                              "summary": "challenge", "readable": False}])
        row = audit.build_rows(CONFIG, [first, second], TOPICS)[0]
        self.assertEqual(row["readability"], 0.5)
        self.assertLess(row["score"], 100)

    def test_lag_requires_two_distinct_sources_for_same_signal(self):
        def payload(source, published):
            return {
                "source": source, "name": source, "tier": "official", "role": "candidate",
                "origin": "candidate", "routing": "forced", "partitions": ["degradation"],
                "status": "ok", "sample_day": "2026-08-05",
                "fetched_at": "2026-08-05T15:00:00+00:00",
                "items": [{"title": "API incident", "url": "https://status.test/incidents/1",
                           "published": published, "summary": "incident lasted 30 minutes"}],
            }

        single = audit.build_rows(CONFIG, [payload("only", "2026-08-05T10:00:00+00:00")], TOPICS)
        self.assertTrue(math.isnan(single[0]["lag"]))

        rows = audit.build_rows(CONFIG, [
            payload("early", "Wed, 05 Aug 2026 10:00:00 GMT"),
            payload("late", "2026-08-05T12:00:00+00:00"),
        ], TOPICS)
        by_source = {row["source_id"]: row for row in rows}
        self.assertEqual(by_source["early"]["lag"], 0.0)
        self.assertEqual(by_source["late"]["lag"], 2.0)

    def test_lag_uses_current_item_time_when_source_repeats_signal(self):
        artifact = "https://arxiv.org/abs/2608.00001"

        def item(url, published):
            return {
                "title": "Evaluation", "url": url, "published": published,
                "summary": "controlled benchmark baseline",
                "expert_features": {
                    "artifact_urls": [artifact], "method_hits": ["benchmark"],
                    "technical_groups": ["evaluation"],
                },
            }

        def payload(source, items):
            return {
                "source": source, "name": source, "tier": "community", "role": "candidate",
                "origin": "candidate", "routing": "forced", "partitions": ["field_test"],
                "status": "ok", "sample_day": "2026-08-10",
                "fetched_at": "2026-08-10T12:00:00+00:00", "items": items,
            }

        rows = audit.build_rows(CONFIG, [
            payload("author", [
                item("https://author.test/first", "2026-08-01T00:00:00+00:00"),
                item("https://author.test/followup", "2026-08-10T00:00:00+00:00"),
            ]),
            payload("other", [
                item("https://other.test/post", "2026-08-05T00:00:00+00:00"),
            ]),
        ], TOPICS)

        by_source = {row["source_id"]: row for row in rows}
        self.assertEqual(by_source["author"]["lag"], 108.0)
        self.assertEqual(by_source["other"]["lag"], 96.0)

    def test_undated_items_use_fetched_at_for_signal_window(self):
        artifact = "https://arxiv.org/abs/2608.00001"

        def payload(source, fetched_at):
            return {
                "source": source, "name": source, "tier": "community", "role": "candidate",
                "origin": "candidate", "routing": "forced", "partitions": ["field_test"],
                "status": "ok", "sample_day": fetched_at[:10], "fetched_at": fetched_at,
                "items": [{
                    "title": "Undated evaluation", "url": f"https://{source}.test/post",
                    "summary": "controlled benchmark baseline",
                    "expert_features": {
                        "artifact_urls": [artifact], "method_hits": ["benchmark"],
                        "technical_groups": ["evaluation"],
                    },
                }],
            }

        rows = audit.build_rows(CONFIG, [
            payload("old", "2026-01-01T00:00:00+00:00"),
            payload("new", "2026-08-01T00:00:00+00:00"),
        ], TOPICS)

        selected = [row for row in rows if row["source_id"] in {"old", "new"}]
        self.assertTrue(all(row["duplicate"] == 0.0 for row in selected))
        self.assertTrue(all(math.isnan(row["lag"]) for row in selected))

    def test_shared_citation_months_apart_is_not_a_duplicate_or_lag_signal(self):
        def payload(source, published):
            return {
                "source": source, "name": source, "tier": "community", "role": "candidate",
                "origin": "candidate", "routing": "forced", "partitions": ["field_test"],
                "status": "ok", "sample_day": "2026-08-05",
                "fetched_at": "2026-08-05T15:00:00+00:00",
                "items": [{
                    "title": "Evaluation", "url": f"https://{source}.test/post",
                    "published": published, "summary": "controlled benchmark baseline",
                    "external_urls": ["https://arxiv.org/abs/2608.00001"],
                }],
            }

        rows = audit.build_rows(CONFIG, [
            payload("early", "2026-01-01T10:00:00+00:00"),
            payload("late", "2026-08-01T10:00:00+00:00"),
        ], TOPICS)

        self.assertTrue(all(row["duplicate"] == 0.0 for row in rows))
        self.assertTrue(all(math.isnan(row["lag"]) for row in rows))

    def test_report_window_uses_complete_samples_not_contiguous_calendar_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = date(2026, 7, 10)
            for index in range(14):
                day = (first + timedelta(days=index * 2)).isoformat()
                folder = root / day
                folder.mkdir()
                (folder / "_audit_log.json").write_text(json.dumps({
                    "finished_at": "done", "sources": {"trial_price": {}}
                }), encoding="utf-8")
                (folder / "trial_price.json").write_text(json.dumps({
                    "source": "trial_price", "name": "Price", "tier": "official",
                    "role": "candidate", "routing": "forced", "partitions": ["pricing"],
                    "status": "ok", "fetched_at": f"{day}T12:00:00+00:00",
                    "items": [{"title": "API pricing", "url": "https://v.test/pricing",
                               "summary": "$2 per million tokens"}],
                }), encoding="utf-8")

            end_day = (first + timedelta(days=26)).isoformat()
            selected = audit.audit_sample_days(root, end_day, 14, {"trial_price"})
            payloads = audit.load_payloads(
                root, end_day, 14, "candidate", sample_days=selected)

            self.assertEqual(len(selected), 14)
            self.assertEqual(len(payloads), 14)

    def test_explicit_rotation_sample_remains_visible_after_full_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = date(2026, 7, 1)
            for index in range(14):
                day = (first + timedelta(days=index)).isoformat()
                folder = root / day
                folder.mkdir()
                (folder / "_audit_log.json").write_text(json.dumps({
                    "finished_at": "done", "sources": {"trial_price": {}}
                }), encoding="utf-8")
                (folder / "trial_price.json").write_text(json.dumps({
                    "source": "trial_price", "name": "Price", "tier": "official",
                    "role": "candidate", "routing": "forced", "partitions": ["pricing"],
                    "status": "ok", "fetched_at": f"{day}T12:00:00+00:00", "items": [],
                }), encoding="utf-8")

            current_day = "2026-08-05"
            current = root / current_day
            current.mkdir()
            (current / "_audit_log.json").write_text(json.dumps({
                "finished_at": "done", "sources": {"rotation": {}}
            }), encoding="utf-8")
            (current / "rotation.json").write_text(json.dumps({
                "source": "rotation", "name": "Rotation", "tier": "community",
                "role": "candidate", "routing": "forced", "partitions": ["pricing"],
                "status": "ok", "fetched_at": "2026-08-05T12:00:00+00:00",
                "items": [{
                    "title": "API pricing", "url": "https://rotation.test/pricing",
                    "summary": "$2 per million tokens",
                }],
            }), encoding="utf-8")
            config = dict(CONFIG)
            config["candidates"] = [
                {"id": "trial_price", "enabled": True},
                {"id": "rotation", "enabled": False, "audit_group": "rotation"},
            ]

            _, rows = audit.generate_report(
                config, current_day, shadow_root=root, formal_root=root / "formal",
                report_dir=root / "reports", include_current_partial=True)

            rotation = next(row for row in rows if row["source_id"] == "rotation")
            price = next(row for row in rows
                         if row["source_id"] == "trial_price" and row["partition"] == "pricing")
            self.assertEqual(rotation["qualified"], 1)
            self.assertEqual(rotation["attempt_days"], 1)
            self.assertEqual(price["attempt_days"], 14)

    def test_qualification_denominator_uses_same_recent_window_as_classifier(self):
        payload = {
            "source": "trial_price", "name": "Trial", "tier": "community",
            "role": "candidate", "origin": "candidate", "routing": "classify",
            "partitions": ["release"], "status": "ok", "sample_day": "2026-08-05",
            "fetched_at": "2026-08-05T12:00:00+00:00",
            "items": [
                {"title": "Qwen release", "url": "https://example.test/recent",
                 "published": "2026-08-05T10:00:00+00:00", "summary": "new weights"},
                {"title": "Qwen release", "url": "https://example.test/old",
                 "published": "2025-01-01T10:00:00+00:00", "summary": "old weights"},
            ],
        }
        row = audit.build_rows(CONFIG, [payload], TOPICS)[0]

        self.assertEqual(row["raw"], 1)
        self.assertEqual(row["qualified"], 1)
        self.assertEqual(row["qualification"], 1.0)

    def test_signal_key_uses_preserved_external_urls(self):
        item = {
            "title": "Paper discussion",
            "url": "https://aggregator.test/post",
            "summary": "[link]",
            "external_urls": ["https://arxiv.org/abs/2608.00001"],
        }
        self.assertEqual(
            audit.signal_key(item), "url:https://arxiv.org/abs/2608.00001")

    def test_expert_signal_keys_ignore_navigation_and_generic_artifact_roots(self):
        item = {
            "title": "Evaluation", "url": "https://author.test/post",
            "external_urls": ["https://github.com/", "https://x.com/share"],
            "expert_features": {
                "artifact_urls": ["https://github.com/", "https://github.com/acme/eval"]},
        }

        self.assertEqual(audit.signal_keys(item), ["url:https://github.com/acme/eval"])

    def test_expert_signal_keys_include_title_fallback_without_artifacts(self):
        item = {
            "title": "Same release analysis", "url": "https://author.test/post",
            "expert_features": {"artifact_urls": [], "technical_groups": ["evaluation"]},
        }

        self.assertEqual(audit.signal_keys(item), [
            "url:https://author.test/post", "title:same release analysis",
        ])


if __name__ == "__main__":
    unittest.main()
