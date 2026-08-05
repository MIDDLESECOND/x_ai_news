# -*- coding: utf-8 -*-
"""Stable capture identities and validation against the local raw archive."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from source_urls import canonical_url


def _source_id(payload: dict) -> str:
    return str(payload.get("source") or (
        "source-" + hashlib.sha256(str(payload.get("name", "unknown")).encode("utf-8"))
        .hexdigest()[:12]))


def source_item_metadata(source_id: str, item: dict) -> tuple[str, str]:
    url = canonical_url(str(item.get("url") or ""))
    identity = hashlib.sha256(f"{source_id}\0{url}".encode("utf-8")).hexdigest()[:20]
    snapshot = {
        "title": item.get("title") or "",
        "url": url,
        "published": item.get("published") or "",
        "summary": item.get("summary") or "",
        "content_sha256": item.get("content_sha256") or "",
    }
    digest = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"{source_id}:{identity}", digest


def source_collection_metadata(payload: dict) -> tuple[str, str]:
    """Identify collection content; observation time is deliberately excluded."""
    source_id = _source_id(payload)
    items = []
    for item in payload.get("items") or []:
        normalized = {
            "title": item.get("title") or "",
            "url": canonical_url(str(item.get("url") or "")),
            "published": item.get("published") or "",
            "summary": item.get("summary") or "",
            "content_sha256": item.get("content_sha256") or "",
            "external_url": item.get("external_url") or "",
            "x_links": sorted(set(item.get("x_links") or [])),
        }
        items.append(normalized)
    snapshot = {"source": source_id, "items": items}
    digest = hashlib.sha256(json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"{source_id}:collection", digest


def configured_audit_urls(root: Path) -> dict[str, set[str]]:
    path = root / "config" / "sources.yaml"
    if not path.exists():
        return {}
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: dict[str, set[str]] = {}
    for source in cfg.get("sources") or []:
        if not source.get("id"):
            continue
        values = [source.get("url")] + list(source.get("audit_urls") or [])
        for value in values:
            if value:
                result.setdefault(str(source["id"]), set()).add(
                    canonical_url(str(value)))
    return result


def build_capture_index(root: Path, day: str) -> dict[tuple[str, str], dict]:
    """Index auditable item and collection snapshots captured on *day*."""
    raw_dir = root / "data" / "raw" / day
    if not raw_dir.is_dir():
        raise ValueError(f"缺少当日原始抓取目录：data/raw/{day}")
    configured = configured_audit_urls(root)
    index: dict[tuple[str, str], dict] = {}
    for path in sorted(raw_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        source_id = _source_id(payload)
        collection_urls = set(configured.get(source_id, set()))
        for item in payload.get("items") or []:
            url = canonical_url(str(item.get("url", "")))
            item_id, digest = source_item_metadata(source_id, item)
            index[(item_id, digest)] = {"kind": "item", "urls": {url}}
        collection_id, collection_hash = source_collection_metadata(payload)
        index[(collection_id, collection_hash)] = {
            "kind": "collection", "urls": collection_urls,
        }
    return index


def validate_capture_bindings(root: Path, records: list[dict], *, url_key: str) -> None:
    """Require every asserted identity/hash/url triple to match the raw archive."""
    indexes: dict[str, dict] = {}
    for record in records:
        day = str(record.get("date", ""))
        if day not in indexes:
            indexes[day] = build_capture_index(root, day)
        index = indexes[day]
        key = (str(record.get("source_item_id", "")),
               str(record.get("snapshot_hash", "")).lower())
        binding = index.get(key)
        if not binding:
            raise ValueError(
                f"抓取身份无法在 data/raw/{day} 复核：{key[0]} / {key[1][:12]}…")
        url = canonical_url(str(record.get(url_key, "")))
        if binding["kind"] == "item" and url not in binding["urls"]:
            raise ValueError(f"证据 URL 与抓取条目不一致：{url}")
        if binding["kind"] == "collection":
            if not binding["urls"]:
                raise ValueError(
                    f"汇总抓取源未配置审计地址，拒绝绑定 collection：{key[0]}")
            if url not in binding["urls"]:
                raise ValueError(f"汇总证据 URL 不是该抓取源配置的审计地址：{url}")
