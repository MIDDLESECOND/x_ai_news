# -*- coding: utf-8 -*-
"""Build deterministic story clusters without changing source evidence.

Every source observation remains addressable by ``source_item_id`` and
``snapshot_hash``.  Clusters are rebuildable reading aids, not evidence or
claim judgments; multi-source membership must not be interpreted as independent
corroboration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import yaml

import build_digest
from source_urls import canonical_url
from state_io import atomic_write_if_changed, exclusive_lock


ROOT = Path(__file__).resolve().parent.parent
TITLE_STOPWORDS = {
    "a", "an", "and", "for", "in", "is", "its", "new", "of", "on", "the", "to",
    "with", "发布", "上线", "宣布", "推出", "今日", "今天",
}
VENDOR_ALIASES = {
    "openai": "openai", "chatgpt": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
    "deepseek": "deepseek",
    "google": "google", "gemini": "google",
    "xai": "xai", "grok": "xai",
    "zhipu": "zhipu", "z.ai": "zhipu", "智谱": "zhipu", "glm": "zhipu",
    "moonshot": "moonshot", "kimi": "moonshot", "月之暗面": "moonshot",
    "alibaba": "alibaba", "qwen": "alibaba", "通义": "alibaba",
}
MODEL_RE = re.compile(
    r"\b(?:gpt[-\s]?[\w.]+|claude[-\s]?[\w.]+|deepseek[-\s]?[\w.]+|"
    r"glm[-\s]?[\w.]+|grok[-\s]?[\w.]+|kimi[-\s]?[\w.]+|qwen[-\s]?[\w.]+)\b",
    re.IGNORECASE,
)
TIER_RANK = {
    "official": 0, "radar": 1, "community": 2, "index": 3,
    "aggregator": 4, "finance": 5,
}


def canonical_story_url(value: str) -> str:
    try:
        return canonical_url(value)
    except ValueError:
        return str(value or "").strip()


def normalized_title(item: dict) -> str:
    value = str(item.get("title") or "").lower()
    return re.sub(r"\s+", " ", value).strip()


def title_tokens(value: str) -> set[str]:
    tokens = {token for token in re.findall(r"[a-z0-9][a-z0-9_.-]*", value.lower())
              if len(token) >= 2 and token not in TITLE_STOPWORDS}
    for chunk in re.findall(r"[\u4e00-\u9fff]+", value):
        if len(chunk) == 1:
            continue
        if len(chunk) == 2:
            tokens.add(chunk)
        else:
            tokens.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return tokens


def title_similarity(a: str, b: str) -> float:
    left, right = title_tokens(a), title_tokens(b)
    jaccard = len(left & right) / len(left | right) if left and right else 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    return max(sequence, sequence * 0.6 + jaccard * 0.4)


def title_is_mergeable(value: str) -> bool:
    return len(value) >= 18 and len(title_tokens(value)) >= 4


def title_entities(value: str) -> tuple[set[str], set[str]]:
    lowered = value.lower()
    vendors = {canonical for alias, canonical in VENDOR_ALIASES.items()
               if alias.lower() in lowered}
    models = {re.sub(r"\s+", "-", match.group(0).lower())
              for match in MODEL_RE.finditer(lowered)}
    return vendors, models


def titles_compatible(a: str, b: str) -> bool:
    vendors_a, models_a = title_entities(a)
    vendors_b, models_b = title_entities(b)
    if vendors_a and vendors_b and vendors_a.isdisjoint(vendors_b):
        return False
    if models_a and models_b and models_a.isdisjoint(models_b):
        return False
    return True


def event_time(item: dict) -> datetime | None:
    return build_digest.parse_pubdate(item.get("published"))


def story_id_for(item: dict) -> str:
    basis = f"{canonical_story_url(item.get('url', ''))}\0{normalized_title(item)}"
    return "story-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _public_item(item: dict) -> dict:
    return {
        "source_item_id": item["source_item_id"],
        "snapshot_hash": item["snapshot_hash"],
        "title": item["title"],
        "url": item["url"],
        "external_url": item.get("external_url", ""),
        "source": item["source"],
        "source_id": str(item["source_item_id"]).rsplit(":", 1)[0],
        "tier": item["tier"],
        "section": item.get("section", ""),
        "published": item.get("published", ""),
        "summary": item.get("summary", ""),
    }


def build_cluster(story_id: str, items: list[dict]) -> dict:
    ordered = sorted(items, key=lambda row: (
        TIER_RANK.get(row.get("tier"), 99),
        -(event_time(row).timestamp() if event_time(row) else 0),
        row.get("title", ""),
    ))
    primary = ordered[0]
    times = [event_time(row) for row in ordered if event_time(row)]
    tiers = Counter(str(row.get("tier") or "unknown") for row in ordered)
    sections = Counter(str(row.get("section") or "unknown") for row in ordered)
    source_ids = {str(row["source_item_id"]).rsplit(":", 1)[0] for row in ordered}
    return {
        "story_id": story_id,
        "title": primary["title"],
        "primary_url": primary["url"],
        "primary_source_item_id": primary["source_item_id"],
        "item_count": len(ordered),
        "source_count": len(source_ids),
        "evidence_tiers": dict(sorted(tiers.items())),
        "sections": dict(sorted(sections.items())),
        "earliest_at": min(times).isoformat() if times else None,
        "latest_at": max(times).isoformat() if times else None,
        "items": [_public_item(row) for row in ordered],
    }


def cluster_sort_key(row: dict) -> tuple:
    latest = row.get("latest_at")
    latest_rank = -datetime.fromisoformat(latest).timestamp() if latest else float("inf")
    return (-row["source_count"], latest_rank, row["title"])


def source_snapshot_at(payloads: list[dict], day: str) -> str:
    times = [build_digest.parse_pubdate(payload.get("fetched_at")) for payload in payloads]
    parsed = [value for value in times if value is not None]
    if parsed:
        return max(parsed).isoformat(timespec="seconds")
    return datetime.combine(date.fromisoformat(day), datetime.min.time(),
                            tzinfo=timezone.utc).isoformat(timespec="seconds")


def cluster_items(items: list[dict], *, now: datetime | None = None,
                  title_window_hours: float = 6.0,
                  title_threshold: float = 0.86) -> tuple[list[dict], list[dict]]:
    """Cluster by canonical URL, then guarded title similarity."""
    del now  # Reserved for future recency ranking; clustering is content/time deterministic.
    groups: dict[str, list[dict]] = {}
    titles: dict[str, str] = {}
    times: dict[str, datetime | None] = {}
    sources: dict[str, str] = {}
    url_index: dict[str, list[str]] = {}
    events = []

    ordered = sorted(items, key=lambda row: (
        event_time(row) or datetime.min.replace(tzinfo=timezone.utc),
        row.get("source_item_id", ""),
    ))
    for item in ordered:
        title = normalized_title(item)
        source_id = str(item["source_item_id"]).rsplit(":", 1)[0]
        canonical = canonical_story_url(item.get("url", ""))
        timestamp = event_time(item)
        target = None
        reason = ""
        similarity = 0.0

        for candidate in url_index.get(canonical, []):
            candidate_title = titles[candidate]
            sim = title_similarity(title, candidate_title)
            if not titles_compatible(title, candidate_title):
                continue
            if (sources[candidate] == source_id and title != candidate_title
                    and sim < title_threshold):
                continue
            target, reason, similarity = candidate, "canonical_url", 1.0
            break

        if target is None and title_is_mergeable(title):
            for candidate, candidate_title in titles.items():
                candidate_time = times[candidate]
                if timestamp is None or candidate_time is None:
                    continue
                delta = abs((timestamp - candidate_time).total_seconds()) / 3600
                if delta > title_window_hours:
                    continue
                sim = title_similarity(title, candidate_title)
                if sim >= title_threshold and titles_compatible(title, candidate_title):
                    target, reason, similarity = candidate, "title_similarity", sim
                    break

        if target is None:
            target = story_id_for(item)
            while target in groups:
                target += "x"
            groups[target] = []
            titles[target] = title
            times[target] = timestamp
            sources[target] = source_id
            url_index.setdefault(canonical, []).append(target)
        else:
            events.append({
                "story_id": target,
                "source_item_id": item["source_item_id"],
                "merged_into_source_item_id": groups[target][0]["source_item_id"],
                "reason": reason,
                "similarity": round(similarity, 4),
            })
            bucket = url_index.setdefault(canonical, [])
            if target not in bucket:
                bucket.append(target)
        groups[target].append(item)

    clusters = [build_cluster(story_id, group) for story_id, group in groups.items()]
    clusters.sort(key=cluster_sort_key)
    return clusters, events


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error("--date 必须是 YYYY-MM-DD")
    if args.window < 1:
        parser.error("--window 必须大于 0")

    topics = yaml.safe_load(
        (ROOT / "config" / "topics.yaml").read_text(encoding="utf-8")) or {}
    payloads = build_digest.load_raw(args.date)
    sectioned, _ = build_digest.classify(
        payloads, topics, args.date, args.window, deduplicate_urls=False)
    items = []
    for section, rows in sectioned.items():
        items.extend(dict(row, section=section) for row in rows)
    clusters, events = cluster_items(items)
    generated_at = source_snapshot_at(payloads, args.date)
    payload = {
        "date": args.date,
        "generated_at": generated_at,
        "source_observations": len(items),
        "story_count": len(clusters),
        "boundary": (
            "派生阅读视图；多来源不等于独立证实；底层 source_item_id 与 snapshot_hash "
            "仍是唯一可审计证据身份。"
        ),
        "stories": clusters,
    }
    merge_log = {
        "date": args.date,
        "generated_at": generated_at,
        "merge_count": len(events),
        "events": events,
    }
    output = (Path(args.output) if args.output else
              ROOT / "data" / "state" / "story_clusters" / f"{args.date}.json")
    if not output.is_absolute():
        output = ROOT / output
    log_path = output.with_name(f"{output.stem}.merge-log.json")
    lock = ROOT / "data" / "state" / "story-clusters.lock"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    log_encoded = json.dumps(merge_log, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with exclusive_lock(lock):
        changed = atomic_write_if_changed(output, encoded)
        log_changed = atomic_write_if_changed(log_path, log_encoded)
    print(
        f"故事聚类：{len(items)} 条观察 → {len(clusters)} 个故事；"
        f"merge log {len(events)} 条（{'已更新' if changed or log_changed else '无变化'}）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
