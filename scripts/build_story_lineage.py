# -*- coding: utf-8 -*-
"""Link daily story clusters into deterministic, window-bounded lineages.

Lineages are reading aids, not claims.  The script never infers correction,
resolution, truth, or career meaning; it records only observable repetition,
capture changes, and conservative follow-up candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

import build_digest
import build_source_health
import build_source_independence
import build_story_clusters
from state_io import atomic_write_if_changed, exclusive_lock


ROOT = Path(__file__).resolve().parent.parent
REASON_RANK = {"shared_source_item": 3, "shared_origin_url": 2, "title_similarity": 1}


def story_features(story: dict) -> dict:
    items = story.get("items") or []
    origins = {
        row["origin_url"]
        for row in (build_source_independence.origin_record(item) for item in items)
        if row["origin_url"]
    }
    source_item_ids = {str(item.get("source_item_id") or "") for item in items
                       if item.get("source_item_id")}
    versions = {
        (str(item.get("source_item_id") or ""), str(item.get("snapshot_hash") or ""))
        for item in items if item.get("source_item_id")
    }
    title = build_story_clusters.normalized_title(story)
    return {
        "origins": origins,
        "source_item_ids": source_item_ids,
        "versions": versions,
        "title": title,
    }


def new_lineage_id(features: dict, story: dict) -> str:
    if features["origins"]:
        basis = "origin\0" + min(features["origins"]) + "\0" + features["title"]
    elif features["source_item_ids"]:
        basis = "item\0" + min(features["source_item_ids"])
    else:
        basis = "title\0" + features["title"] + "\0" + str(story.get("story_id", ""))
    return "lineage-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def match_candidate(current: dict, prior: dict, *, title_threshold: float) -> tuple | None:
    left, right = current["features"], prior["features"]
    if left["source_item_ids"] & right["source_item_ids"]:
        return (REASON_RANK["shared_source_item"], 1.0, "shared_source_item")
    if left["origins"] & right["origins"]:
        similarity = build_story_clusters.title_similarity(left["title"], right["title"])
        if (build_story_clusters.titles_compatible(left["title"], right["title"])
                and similarity >= 0.6):
            return (REASON_RANK["shared_origin_url"], similarity, "shared_origin_url")
    if not (build_story_clusters.title_is_mergeable(left["title"])
            and build_story_clusters.title_is_mergeable(right["title"])):
        return None
    if not build_story_clusters.titles_compatible(left["title"], right["title"]):
        return None
    left_vendors, left_models = build_story_clusters.title_entities(left["title"])
    right_vendors, right_models = build_story_clusters.title_entities(right["title"])
    if not ((left_vendors & right_vendors) or (left_models & right_models)):
        return None
    similarity = build_story_clusters.title_similarity(left["title"], right["title"])
    if similarity < title_threshold:
        return None
    return (REASON_RANK["title_similarity"], similarity, "title_similarity")


def update_type(current_features: dict, prior_features: dict | None) -> str:
    if prior_features is None:
        return "new"
    if current_features["versions"] == prior_features["versions"]:
        return "repeat"
    current_hashes = {}
    prior_hashes = {}
    for item_id, snapshot_hash in current_features["versions"]:
        current_hashes.setdefault(item_id, set()).add(snapshot_hash)
    for item_id, snapshot_hash in prior_features["versions"]:
        prior_hashes.setdefault(item_id, set()).add(snapshot_hash)
    shared_ids = set(current_hashes) & set(prior_hashes)
    if any(current_hashes[item_id] != prior_hashes[item_id] for item_id in shared_ids):
        return "updated-capture"
    return "follow-up"


def build_lineages(daily_stories: list[dict], *, lookback_days: int = 7,
                   title_threshold: float = 0.9) -> dict:
    entries = []
    active = []
    lineage_groups = {}

    for daily in sorted(daily_stories, key=lambda row: row["day"]):
        current_day = date.fromisoformat(daily["day"])
        active = [row for row in active
                  if (current_day - date.fromisoformat(row["date"])).days <= lookback_days]
        for story in daily.get("stories") or []:
            current = {"story": story, "features": story_features(story)}
            candidates = []
            for prior in active:
                if prior["date"] == daily["day"]:
                    continue
                match = match_candidate(current, prior, title_threshold=title_threshold)
                if match:
                    gap = (current_day - date.fromisoformat(prior["date"])).days
                    candidates.append((*match, -gap, prior))
            if candidates:
                _, similarity, reason, _, prior = max(
                    candidates, key=lambda value: (value[0], value[1], value[3],
                                                    value[4]["story_id"]))
                lineage_id = prior["lineage_id"]
                prior_features = prior["features"]
                prior_story_id = prior["story_id"]
                prior_date = prior["date"]
            else:
                similarity, reason = 0.0, "new"
                lineage_id = new_lineage_id(current["features"], story)
                if lineage_id in lineage_groups:
                    collision_basis = (
                        f"{lineage_id}\0{daily['day']}\0{story.get('story_id', '')}")
                    lineage_id = "lineage-" + hashlib.sha256(
                        collision_basis.encode("utf-8")).hexdigest()[:20]
                prior_features = None
                prior_story_id = None
                prior_date = None

            entry = {
                "date": daily["day"],
                "story_id": story.get("story_id", ""),
                "lineage_id": lineage_id,
                "title": story.get("title", ""),
                "update_type": update_type(current["features"], prior_features),
                "link_reason": reason,
                "similarity": round(similarity, 4),
                "prior_story_id": prior_story_id,
                "prior_date": prior_date,
                "source_item_ids": sorted(current["features"]["source_item_ids"]),
                "origin_urls": sorted(current["features"]["origins"]),
            }
            entries.append(entry)
            active.append({
                **entry,
                "features": current["features"],
            })
            group = lineage_groups.setdefault(lineage_id, {
                "lineage_id": lineage_id,
                "window_first_seen": daily["day"],
                "window_latest_seen": daily["day"],
                "event_count": 0,
                "days": set(),
                "latest_title": story.get("title", ""),
            })
            group["window_latest_seen"] = daily["day"]
            group["event_count"] += 1
            group["days"].add(daily["day"])
            group["latest_title"] = story.get("title", "")

    groups = []
    for group in lineage_groups.values():
        groups.append({**group, "days": sorted(group["days"])})
    groups.sort(key=lambda row: (
        row["window_latest_seen"], row["event_count"], row["lineage_id"]), reverse=True)
    return {
        "lineage_count": len(groups),
        "entry_count": len(entries),
        "boundary": (
            "窗口内确定性连续性候选；只表达重复抓取、快照变化或可能后续。"
            "不得据此推断纠正、解决、事实成立或改写 claims.yaml。"
        ),
        "lineages": groups,
        "entries": entries,
    }


def stories_from_samples(samples: list[dict], topics: dict,
                         *, window_days: int) -> list[dict]:
    rows = []
    for sample in samples:
        sectioned, _ = build_digest.classify(
            sample["payloads"], topics, sample["day"], window_days,
            deduplicate_urls=False)
        items = []
        for section, section_items in sectioned.items():
            items.extend(dict(item, section=section) for item in section_items)
        stories, _ = build_story_clusters.cluster_items(items)
        rows.append({"day": sample["day"], "stories": stories})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument("--lookback", type=int, default=7)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error("--date 必须是 YYYY-MM-DD")
    if min(args.days, args.window, args.lookback) < 1:
        parser.error("--days、--window 与 --lookback 必须大于 0")

    topics = yaml.safe_load(
        (ROOT / "config" / "topics.yaml").read_text(encoding="utf-8")) or {}
    samples = build_source_health.load_sample_days(ROOT, args.date, args.days)
    if not samples:
        parser.error("窗口内没有 raw 样本")
    daily_stories = stories_from_samples(samples, topics, window_days=args.window)
    payload = build_lineages(daily_stories, lookback_days=args.lookback)
    times = [
        build_digest.parse_pubdate(item.get("fetched_at"))
        for sample in samples for item in sample["payloads"]
    ]
    parsed_times = [value for value in times if value]
    payload.update({
        "date": args.date,
        "generated_at": (
            max(parsed_times).isoformat(timespec="seconds") if parsed_times else
            datetime.combine(date.fromisoformat(args.date), datetime.min.time(),
                             tzinfo=timezone.utc).isoformat(timespec="seconds")
        ),
        "sample_days": [sample["day"] for sample in samples],
        "lookback_days": args.lookback,
    })
    output = (Path(args.output) if args.output else
              ROOT / "data" / "state" / "story_lineage" / f"{args.date}.json")
    if not output.is_absolute():
        output = ROOT / output
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lock = ROOT / "data" / "state" / "story-lineage.lock"
    with exclusive_lock(lock):
        changed = atomic_write_if_changed(output, encoded)
    print(
        f"跨日故事连续性：{payload['entry_count']} 个日故事 -> "
        f"{payload['lineage_count']} 条 lineage（{'已更新' if changed else '无变化'}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
