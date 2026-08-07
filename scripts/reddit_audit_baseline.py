# -*- coding: utf-8 -*-
"""Compact long-lived non-Reddit L1 baseline for Reddit source audits."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from state_io import atomic_write_if_changed, exclusive_lock, semantic_hash

HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"&]+", re.IGNORECASE)
MAX_TITLE_CHARS = 500
MAX_URLS_PER_ITEM = 50
MAX_URL_CHARS = 4096
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid",
}


def _item_urls(item):
    urls = {_safe_url(value) for value in _url_candidates(item)}
    urls.discard(None)
    return sorted(urls)[:MAX_URLS_PER_ITEM]


def _url_candidates(item):
    values = [item.get("url", ""), item.get("external_url", "")]
    values.extend(item.get("external_urls") or [])
    values.append(item.get("summary") or "")
    return [match for value in values
            for match in HTTP_URL_RE.findall(str(value))]


def _safe_url(value):
    normalized = normalize_match_url(value)
    if not normalized:
        return None
    parts = urlsplit(normalized)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def normalize_match_url(value):
    """Normalize a URL for matching; persist only ``url_signal_key`` output."""
    value = html.unescape(str(value or "").strip()).rstrip(".,);]}")
    value = value[:MAX_URL_CHARS]
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if (scheme not in {"http", "https"} or not host
            or parts.username is not None or parts.password is not None):
        return None
    if port is not None and port != (443 if scheme == "https" else 80):
        return None
    netloc = f"[{host}]" if ":" in host else host
    query = urlencode([
        (key, item) for key, item in parse_qsl(
            parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ])
    return urlunsplit(("https", netloc, parts.path.rstrip("/") or "/", query, ""))


def url_signal_key(value):
    normalized = normalize_match_url(value)
    if not normalized:
        return None
    return "url-sha256:" + hashlib.sha256(
        normalized.encode("utf-8")).hexdigest()


def _safe_when(value):
    value = str(value or "").strip()
    if not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def compact_l1_day(day_dir, source_ids):
    """Return only comparison fields; never retain article or summary text."""
    rows = []
    try:
        log = json.loads(
            (Path(day_dir) / "_fetch_log.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rows
    if (log.get("run_mode") != "full" or not log.get("completed_at")
            or log.get("date") != Path(day_dir).name):
        return rows
    for source_id in sorted(set(source_ids)):
        path = Path(day_dir) / f"{source_id}.json"
        try:
            raw = path.read_bytes()
            source_log = log.get("sources", {}).get(source_id, {})
            if (not source_log.get("snapshot_hash")
                    or source_log["snapshot_hash"] != semantic_hash(raw)):
                continue
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            continue
        fetched_at = payload.get("fetched_at")
        for item in payload.get("items", []):
            candidates = _url_candidates(item)
            rows.append({
                "source": source_id,
                "title": str(item.get("title") or "")[:MAX_TITLE_CHARS],
                "when": _safe_when(item.get("published")) or _safe_when(fetched_at),
                "urls": sorted({
                    value for value in (_safe_url(url) for url in candidates)
                    if value
                })[:MAX_URLS_PER_ITEM],
                "url_keys": sorted({
                    value for value in (url_signal_key(url) for url in candidates)
                    if value
                })[:MAX_URLS_PER_ITEM],
            })
    return rows


def refresh_l1_baseline(raw_root, baseline_root, through_day, source_ids,
                        keep_days, signal_window_days=30,
                        audit_duration_days=14):
    """Backfill retained raw days, then keep compact daily indices longer."""
    raw_root = Path(raw_root)
    baseline_root = Path(baseline_root)
    end = date.fromisoformat(through_day)
    baseline_root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(baseline_root / ".refresh.lock"):
        _refresh_l1_baseline(
            raw_root, baseline_root, end, source_ids, keep_days,
            signal_window_days, audit_duration_days)


def _refresh_l1_baseline(raw_root, baseline_root, end, source_ids, keep_days,
                         signal_window_days, audit_duration_days):
    if raw_root.exists():
        for folder in sorted(raw_root.iterdir()):
            try:
                sample_day = date.fromisoformat(folder.name)
            except (ValueError, AttributeError):
                continue
            if sample_day > end:
                continue
            output_path = baseline_root / f"{folder.name}.json"
            prior_items = []
            try:
                prior = json.loads(output_path.read_text(encoding="utf-8"))
                prior_items = prior.get("items") or []
            except (OSError, json.JSONDecodeError):
                pass
            merged = {}
            for row in prior_items + compact_l1_day(folder, source_ids):
                key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                merged[key] = row
            payload = {
                "version": 1,
                "date": folder.name,
                # Append-only union preserves observations from sources later
                # removed from config and from partial same-day reruns.
                "items": [merged[key] for key in sorted(merged)],
            }
            atomic_write_if_changed(
                output_path,
                json.dumps(payload, ensure_ascii=False, indent=2,
                           sort_keys=True) + "\n")

    cutoff = _subtract_days_saturated(end, max(1, int(keep_days)))
    protected_days = _audit_protection_days(
        baseline_root.parent, audit_duration_days)
    if protected_days:
        cutoff = min(
            cutoff,
            _subtract_days_saturated(
                min(protected_days), max(0, int(signal_window_days))))
    for path in baseline_root.glob("*.json"):
        try:
            old = date.fromisoformat(path.stem) < cutoff
        except ValueError:
            old = False
        if old:
            path.unlink()


def _subtract_days_saturated(value, days):
    days = max(0, int(days))
    if days >= value.toordinal():
        return date.min
    return value - timedelta(days=days)


def _audit_protection_days(audit_root, duration_days):
    """Return snapshot and event days still relevant to per-source scoring."""
    by_source = {}
    for folder in sorted(Path(audit_root).iterdir()):
        try:
            sample_day = date.fromisoformat(folder.name)
        except (ValueError, AttributeError):
            continue
        for path in folder.glob("r_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            source_id = str(payload.get("source") or path.stem)
            by_source.setdefault(source_id, []).append((sample_day, payload))
    protected = []
    limit = max(1, int(duration_days))
    for snapshots in by_source.values():
        for sample_day, payload in sorted(
                snapshots, key=lambda value: value[0])[-limit:]:
            protected.append(sample_day)
            fetched = _safe_when(payload.get("fetched_at"))
            for item in payload.get("items") or []:
                when = _safe_when(item.get("published")) or fetched
                if when:
                    protected.append(datetime.fromisoformat(when).date())
    return protected


def load_l1_baseline(baseline_root, end_day):
    """Load retained compact rows through ``end_day``."""
    end = date.fromisoformat(end_day)
    rows = []
    baseline_root = Path(baseline_root)
    if not baseline_root.exists():
        return rows
    for path in sorted(baseline_root.glob("*.json")):
        try:
            sample_day = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if sample_day > end:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.extend(payload.get("items") or [])
    return rows
