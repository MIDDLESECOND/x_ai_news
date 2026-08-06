# -*- coding: utf-8 -*-
"""Build a descriptive, rebuildable source-health ledger from local captures.

Metrics support human source review only.  They must not automatically promote,
demote, enable, or disable a source, and low-frequency sources must not be
ranked against high-frequency feeds by activity volume alone.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import yaml

import build_digest
import build_story_clusters
from state_io import atomic_write_if_changed, exclusive_lock


ROOT = Path(__file__).resolve().parent.parent


def load_sample_days(root: Path, end_day: str, days: int) -> list[dict]:
    end = date.fromisoformat(end_day)
    cutoff = end - timedelta(days=max(1, days) - 1)
    rows = []
    raw_root = root / "data" / "raw"
    if not raw_root.exists():
        return rows
    for folder in sorted(raw_root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            sample_day = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if sample_day < cutoff or sample_day > end:
            continue
        payloads = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(folder.glob("*.json"))
            if not path.name.startswith("_")
        ]
        log_path = folder / "_fetch_log.json"
        log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else None
        rows.append({"day": sample_day.isoformat(), "payloads": payloads, "log": log})
    return rows


def _source_id_from_item(item: dict) -> str:
    return str(item.get("source_item_id") or "").rsplit(":", 1)[0]


def collect_health(sources: list[dict], topics: dict, samples: list[dict],
                   *, window_days: int = 3) -> dict:
    health = {}
    unique_items = defaultdict(set)
    snapshot_versions = defaultdict(set)
    story_mentions = defaultdict(set)
    primary_stories = defaultdict(set)
    sole_stories = defaultdict(set)

    for source in sources:
        source_id = str(source["id"])
        health[source_id] = {
            "source_id": source_id,
            "name": source.get("name", source_id),
            "tier": source.get("tier", "unknown"),
            "type": source.get("type", "unknown"),
            "configured_enabled": bool(source.get("enabled", True)),
            "attempt_days": 0,
            "success_days": 0,
            "failure_days": 0,
            "partial_days": 0,
            "skipped_days": 0,
            "cached_days": 0,
            "stale_days": 0,
            "deferred_days": 0,
            "last_status": None,
            "last_success_at": None,
            "raw_snapshot_days": 0,
            "raw_item_observations": 0,
            "qualified_observations": 0,
        }

    snapshot_times = []
    full_run_days = 0
    for sample in samples:
        day = sample["day"]
        log = sample.get("log")
        if log:
            full_run_days += 1
            if build_digest.parse_pubdate(log.get("fetched_at")):
                snapshot_times.append(build_digest.parse_pubdate(log["fetched_at"]))
            for source_id, status_row in (log.get("sources") or {}).items():
                if source_id not in health:
                    continue
                status = status_row.get("status")
                row = health[source_id]
                row["last_status"] = status
                if status in ("ok", "partial", "error"):
                    row["attempt_days"] += 1
                if status == "ok":
                    row["success_days"] += 1
                    row["last_success_at"] = log.get("fetched_at") or day
                elif status == "error":
                    row["failure_days"] += 1
                elif status == "partial":
                    row["partial_days"] += 1
                elif status == "skipped":
                    row["skipped_days"] += 1
                elif status == "cached":
                    row["cached_days"] += 1
                elif status == "stale":
                    row["stale_days"] += 1
                elif status == "deferred":
                    row["deferred_days"] += 1

        sectioned, _ = build_digest.classify(
            sample["payloads"], topics, day, window_days, deduplicate_urls=False)
        qualified = []
        for section, items in sectioned.items():
            qualified.extend(dict(item, section=section) for item in items)
        clusters, _ = build_story_clusters.cluster_items(qualified)

        for payload in sample["payloads"]:
            source_id = str(payload.get("source") or "")
            if source_id not in health:
                continue
            row = health[source_id]
            row["raw_snapshot_days"] += 1
            row["raw_item_observations"] += len(payload.get("items") or [])
            fetched = build_digest.parse_pubdate(payload.get("fetched_at"))
            if fetched:
                snapshot_times.append(fetched)

        for item in qualified:
            source_id = _source_id_from_item(item)
            if source_id not in health:
                continue
            health[source_id]["qualified_observations"] += 1
            unique_items[source_id].add(item["source_item_id"])
            snapshot_versions[source_id].add(
                (item["source_item_id"], item["snapshot_hash"]))

        for story in clusters:
            story_key = story["story_id"]
            source_ids = {_source_id_from_item(item) for item in story.get("items") or []}
            for source_id in source_ids:
                if source_id in health:
                    story_mentions[source_id].add(story_key)
            primary = _source_id_from_item({
                "source_item_id": story.get("primary_source_item_id", "")})
            if primary in health:
                primary_stories[primary].add(story_key)
            if len(source_ids) == 1:
                source_id = next(iter(source_ids), "")
                if source_id in health:
                    sole_stories[source_id].add(story_key)

    for source_id, row in health.items():
        attempts = row["attempt_days"]
        raw_count = row["raw_item_observations"]
        row["success_rate"] = (
            round(row["success_days"] / attempts, 4) if attempts else None)
        row["qualification_rate"] = (
            round(row["qualified_observations"] / raw_count, 4) if raw_count else None)
        row["unique_qualified_items"] = len(unique_items[source_id])
        row["unique_snapshot_versions"] = len(snapshot_versions[source_id])
        row["story_mentions"] = len(story_mentions[source_id])
        row["primary_stories"] = len(primary_stories[source_id])
        row["sole_source_stories"] = len(sole_stories[source_id])

    generated_at = (
        max(snapshot_times).isoformat(timespec="seconds") if snapshot_times else
        datetime.combine(date.fromisoformat(samples[-1]["day"]), time.min,
                         tzinfo=timezone.utc).isoformat(timespec="seconds")
        if samples else ""
    )
    return {
        "date": samples[-1]["day"] if samples else "",
        "generated_at": generated_at,
        "sample_days": [sample["day"] for sample in samples],
        "full_run_days": full_run_days,
        "boundary": (
            "描述性派生账本；不得自动晋退、启停或按活跃度跨轨排名信源。"
            "低频官方源与高频聚合源必须分轨人工解释；raw 量可能包含重复历史 feed，"
            "入围率不是质量分。"
        ),
        "sources": [health[key] for key in sorted(health)],
    }


def _ratio(value) -> str:
    return "—" if value is None else f"{value:.1%}"


def render_report(payload: dict, *, days: int) -> str:
    lines = [
        f"# 信源健康账本 — {payload['date']}", "",
        f"- 请求窗口：最近 {days} 个自然日；实际 raw 样本日：{len(payload['sample_days'])}",
        f"- 含完整抓取日志的运行日：{payload['full_run_days']}",
        f"- 边界：{payload['boundary']}", "",
        "| source_id | tier | 成功/部分/尝试 | 缓存/陈旧/冷却 | raw | 入围 | 入围率 | 唯一条目 | 故事 | 主来源 | 单源故事 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in sorted(payload["sources"], key=lambda value: (
            value["tier"], value["source_id"])):
        lines.append(
            f"| `{row['source_id']}` | {row['tier']} | "
            f"{row['success_days']}/{row['partial_days']}/{row['attempt_days']} | "
            f"{row['cached_days']}/{row['stale_days']}/{row['deferred_days']} | "
            f"{row['raw_item_observations']} | {row['qualified_observations']} | "
            f"{_ratio(row['qualification_rate'])} | {row['unique_qualified_items']} | "
            f"{row['story_mentions']} | {row['primary_stories']} | "
            f"{row['sole_source_stories']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument("--output", default="")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error("--date 必须是 YYYY-MM-DD")
    if args.days < 1 or args.window < 1:
        parser.error("--days 与 --window 必须大于 0")

    sources_cfg = yaml.safe_load(
        (ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")) or {}
    topics = yaml.safe_load(
        (ROOT / "config" / "topics.yaml").read_text(encoding="utf-8")) or {}
    samples = load_sample_days(ROOT, args.date, args.days)
    if not samples:
        parser.error("窗口内没有 raw 样本")
    payload = collect_health(
        sources_cfg.get("sources") or [], topics, samples, window_days=args.window)
    output = (Path(args.output) if args.output else
              ROOT / "data" / "state" / "source_health" / f"{args.date}.json")
    report = (Path(args.report) if args.report else
              ROOT / "reports" / "source-health" / f"{args.date}.md")
    if not output.is_absolute():
        output = ROOT / output
    if not report.is_absolute():
        report = ROOT / report
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    report_text = render_report(payload, days=args.days)
    lock = ROOT / "data" / "state" / "source-health.lock"
    with exclusive_lock(lock):
        changed = atomic_write_if_changed(output, encoded)
        report_changed = atomic_write_if_changed(report, report_text)
    print(
        f"信源健康账本：{len(payload['sources'])} 个配置源、"
        f"{len(payload['sample_days'])} 个样本日（"
        f"{'已更新' if changed or report_changed else '无变化'}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
