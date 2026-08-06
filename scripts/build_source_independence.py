# -*- coding: utf-8 -*-
"""Derive source-carrier and estimated independent-origin groups for stories.

This is a rebuildable reading aid.  An ``independence_group`` means that two
observations resolve to the same canonical origin URL; different groups remain
only candidate independent origins and are not proof of independent reporting.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from source_urls import canonical_url
from state_io import atomic_write_if_changed, exclusive_lock


ROOT = Path(__file__).resolve().parent.parent
PLATFORM_HOSTS = {
    "news.ycombinator.com": "hn",
    "reddit.com": "reddit",
    "old.reddit.com": "reddit",
    "x.com": "x",
    "twitter.com": "x",
    "huggingface.co": "huggingface",
    "github.com": "github",
}


def safe_canonical(value: str) -> str:
    try:
        return canonical_url(str(value or ""))
    except ValueError:
        return ""


def url_host(value: str) -> str:
    return (urlsplit(value).hostname or "").lower().removeprefix("www.")


def carrier_for(item: dict, observed_url: str) -> str:
    host = url_host(observed_url)
    if host in PLATFORM_HOSTS:
        return PLATFORM_HOSTS[host]
    tier = str(item.get("tier") or "")
    if tier == "aggregator":
        return "aggregator"
    if tier in ("index", "radar", "finance"):
        return tier
    return "direct"


def origin_record(item: dict) -> dict:
    observed_url = safe_canonical(item.get("url", ""))
    external_url = safe_canonical(item.get("external_url", ""))
    origin_url = external_url or observed_url
    basis = "external_url" if external_url else "observed_url"
    group = (
        "origin-" + hashlib.sha256(origin_url.encode("utf-8")).hexdigest()[:20]
        if origin_url else "unknown"
    )
    return {
        "source_item_id": item.get("source_item_id", ""),
        "snapshot_hash": item.get("snapshot_hash", ""),
        "source_id": item.get("source_id", ""),
        "tier": item.get("tier", ""),
        "observed_url": observed_url,
        "external_url": external_url,
        "carrier": carrier_for(item, observed_url),
        "origin_url": origin_url,
        "origin_domain": url_host(origin_url),
        "independence_group": group,
        "classification_basis": basis,
    }


def analyze_story(story: dict) -> dict:
    observations = [origin_record(item) for item in story.get("items") or []]
    groups = {row["independence_group"] for row in observations
              if row["independence_group"] != "unknown"}
    unknown_count = sum(
        row["independence_group"] == "unknown" for row in observations)
    carriers = {row["carrier"] for row in observations}
    domains = {row["origin_domain"] for row in observations if row["origin_domain"]}
    source_ids = {row["source_id"] for row in observations if row["source_id"]}
    return {
        "story_id": story.get("story_id", ""),
        "title": story.get("title", ""),
        "observation_count": len(observations),
        "coverage_count": len(source_ids),
        "carrier_type_count": len(carriers),
        "carrier_types": sorted(carriers),
        "estimated_independent_origin_count": len(groups),
        "unknown_origin_count": unknown_count,
        "origin_domains": sorted(domains),
        "observations": observations,
    }


def build_independence_payload(cluster_payload: dict) -> dict:
    stories = [analyze_story(story) for story in cluster_payload.get("stories") or []]
    return {
        "date": cluster_payload.get("date", ""),
        "source_snapshot_at": cluster_payload.get("generated_at", ""),
        "story_count": len(stories),
        "boundary": (
            "派生来源关系视图；independence_group 仅表示规范化 origin URL 相同。"
            "不同 group 只是候选独立来源，不等于独立证实。"
        ),
        "stories": stories,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--clusters", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error("--date 必须是 YYYY-MM-DD")

    cluster_path = (Path(args.clusters) if args.clusters else
                    ROOT / "data" / "state" / "story_clusters" / f"{args.date}.json")
    if not cluster_path.is_absolute():
        cluster_path = ROOT / cluster_path
    if not cluster_path.exists():
        parser.error(f"缺少故事聚类：{cluster_path}；先运行 build_story_clusters.py")
    cluster_payload = json.loads(cluster_path.read_text(encoding="utf-8"))
    payload = build_independence_payload(cluster_payload)
    output = (Path(args.output) if args.output else
              ROOT / "data" / "state" / "source_independence" / f"{args.date}.json")
    if not output.is_absolute():
        output = ROOT / output
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lock = ROOT / "data" / "state" / "source-independence.lock"
    with exclusive_lock(lock):
        changed = atomic_write_if_changed(output, encoded)
    print(
        f"来源独立性图谱：{len(payload['stories'])} 个故事 -> {output}"
        f"（{'已更新' if changed else '无变化'}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
