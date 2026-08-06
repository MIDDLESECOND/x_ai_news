# -*- coding: utf-8 -*-
"""Persistent HTTP validators, bounded freshness scheduling, and failure cooldown.

The cache is private rebuildable state.  Immutable response bodies are addressed
by their content hash; small metadata records are atomically replaced under a
per-request-key lock.  Callers still parse a normal ``requests.Response`` on a
fresh cache hit or a 304 revalidation.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

from state_io import atomic_write_if_changed, exclusive_lock


DEFAULT_POLICY = {
    "enabled": True,
    "conditional": True,
    "adaptive": True,
    # Safe default: optimize duplicate same-day runs, never skip the next daily run.
    "min_interval_minutes": 30,
    "max_interval_minutes": 12 * 60,
    "failure_base_minutes": 30,
    "failure_max_minutes": 12 * 60,
    "max_stale_minutes": 12 * 60,
    "max_body_bytes": 15 * 1024 * 1024,
    # Hard transport ceiling, enforced while reading decoded response chunks.
    "max_download_bytes": 15 * 1024 * 1024,
}


@dataclass
class PreparedRequest:
    key: str
    url: str
    accept: str
    entry: dict
    headers: dict[str, str]
    cached_response: requests.Response | None = None
    deferred_until: str | None = None
    deferred_error: str = ""


class FetchCooldown(requests.RequestException):
    """A prior retryable failure is still inside its recorded cooldown."""


@dataclass
class RequestLease:
    path: Path
    token: str
    waited: bool

    def __bool__(self) -> bool:
        return self.waited

    def heartbeat(self) -> None:
        """Keep a legitimately long request from being mistaken for a dead owner."""
        try:
            if self.path.read_text(encoding="ascii") == self.token:
                os.utime(self.path, None)
        except OSError:
            pass

    def release(self) -> None:
        """Remove only this owner's lock; never unlink a successor's lease."""
        try:
            if self.path.read_text(encoding="ascii") == self.token:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def request_lease(root: Path, key: str, *, wait_seconds: float = 65,
                  stale_after: int = 5 * 60):
    """Serialize network revalidation for one request key across processes."""
    path = root / "request_locks" / f"{key}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0, wait_seconds)
    fd = None
    token = f"pid={os.getpid()} nonce={os.urandom(12).hex()}\n"
    waited = False
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            waited = True
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_after:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise FetchCooldown(f"等待同一 HTTP 请求完成超时：{path.stem}")
            time.sleep(0.05)
    try:
        os.write(fd, token.encode("ascii"))
        os.close(fd)
        fd = None
        lease = RequestLease(path=path, token=token, waited=waited)
        yield lease
    finally:
        if fd is not None:
            os.close(fd)
        if "lease" in locals():
            lease.release()
        else:
            # Acquisition failed before the owner handle was published; the
            # just-created path is still ours and must not strand a dead lease.
            path.unlink(missing_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_now(value: datetime | None) -> datetime:
    value = value or utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_policy(source: dict | None = None) -> dict:
    policy = dict(DEFAULT_POLICY)
    policy.update((source or {}).get("fetch_policy") or {})
    for key in ("min_interval_minutes", "max_interval_minutes",
                "failure_base_minutes", "failure_max_minutes",
                "max_stale_minutes", "max_body_bytes", "max_download_bytes"):
        policy[key] = max(0, int(policy[key]))
    policy["max_download_bytes"] = min(
        DEFAULT_POLICY["max_download_bytes"], policy["max_download_bytes"])
    policy["max_interval_minutes"] = max(
        policy["min_interval_minutes"], policy["max_interval_minutes"])
    policy["failure_max_minutes"] = max(
        policy["failure_base_minutes"], policy["failure_max_minutes"])
    return policy


def request_key(url: str, accept: str | None = None,
                source_id: str = "") -> str:
    # Bodies remain content-addressed/deduplicated, while validators and polling
    # state are isolated per configured source and policy.
    return hashlib.sha256(
        f"{source_id}\0{url}\0{accept or ''}".encode("utf-8")).hexdigest()


def normalized_origin(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return f"{scheme}://{host}{f':{port}' if port is not None else ''}"


def host_state_key(url: str) -> str:
    digest = hashlib.sha256(normalized_origin(url).encode("utf-8")).hexdigest()
    return f"host-{digest}"


def host_lease_key(url: str) -> str:
    return host_state_key(url)


def host_cooldown(root: Path, url: str, *, now: datetime | None = None) -> tuple[str | None, str]:
    """Return an active origin cooldown without consulting per-request cache state."""
    now = normalize_now(now)
    entry = load_entry(root, host_state_key(url))
    blocked_until = _parse_time(entry.get("blocked_until"))
    if blocked_until and now < blocked_until:
        return blocked_until.isoformat(), str(
            entry.get("last_error") or "同一源站要求暂停请求")
    return None, ""


def _entry_path(root: Path, key: str) -> Path:
    return root / "entries" / f"{key}.json"


def _body_path(root: Path, digest: str) -> Path:
    return root / "bodies" / f"{digest}.bin"


def load_entry(root: Path, key: str) -> dict:
    path = _entry_path(root, key)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _cached_body(root: Path, entry: dict) -> bytes | None:
    digest = str(entry.get("body_sha256") or "")
    if not digest:
        return None
    path = _body_path(root, digest)
    try:
        body = path.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(body).hexdigest() != digest:
        return None
    return body


def _header(headers, name: str) -> str:
    if not headers:
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _response_headers(entry: dict, extra=None) -> dict[str, str]:
    headers = dict(entry.get("response_headers") or {})
    for key, value in (extra or {}).items():
        if str(key).lower() in {
                "content-type", "content-language", "etag", "last-modified",
                "cache-control", "expires", "age", "date", "vary"}:
            headers[str(key)] = str(value)
    return headers


def synthetic_response(root: Path, entry: dict, status: str,
                       *, extra_headers=None) -> requests.Response | None:
    body = _cached_body(root, entry)
    if body is None:
        return None
    response = requests.Response()
    response.status_code = 200
    response.url = str(entry.get("url") or "")
    response.headers.update(_response_headers(entry, extra_headers))
    response._content = body
    encoding = entry.get("encoding")
    if isinstance(encoding, str) and encoding:
        response.encoding = encoding
    response.frontier_cache_status = status
    response.frontier_cached_at = entry.get("stored_at")
    response.frontier_last_network_success_at = entry.get("last_success_at")
    return response


def prepare_request(root: Path, url: str, accept: str | None = None, *,
                    source: dict | None = None, now: datetime | None = None,
                    logical_day: str | None = None,
                    force_revalidate: bool = False) -> PreparedRequest:
    now = normalize_now(now)
    logical_day = logical_day or now.date().isoformat()
    policy = resolve_policy(source)
    key = request_key(url, accept, str((source or {}).get("id") or ""))
    entry = load_entry(root, key) if policy["enabled"] else {}
    prepared = PreparedRequest(key, url, accept or "", entry, {})
    body_exists = _cached_body(root, entry) is not None

    host_entry = load_entry(root, host_state_key(url))
    host_blocked_until = _parse_time(host_entry.get("blocked_until"))
    if host_blocked_until and now < host_blocked_until:
        prepared.deferred_until = host_blocked_until.isoformat()
        prepared.deferred_error = str(
            host_entry.get("last_error") or "同一源站要求暂停请求")
        return prepared

    retry_at = _parse_time(entry.get("retry_at"))
    if retry_at and now < retry_at and not force_revalidate:
        if (body_exists and entry.get("allow_stale")
                and _may_serve_stale(entry, now, policy)):
            prepared.cached_response = synthetic_response(root, entry, "stale_backoff")
        else:
            prepared.deferred_until = retry_at.isoformat()
            prepared.deferred_error = str(entry.get("last_error") or "")
        return prepared

    same_day = str(entry.get("logical_day") or "") == logical_day
    next_check_at = _parse_time(
        entry.get("next_check_at") or entry.get("next_fetch_at"))
    last_checked_at = _parse_time(entry.get("last_checked_at"))
    if next_check_at and last_checked_at:
        # Re-evaluate with the current source policy so a configuration tightening
        # takes effect immediately instead of inheriting an old absolute deadline.
        current_cap = last_checked_at + timedelta(seconds=_schedule_interval_seconds(
            policy, int(entry.get("unchanged_streak") or 0)))
        next_check_at = min(next_check_at, current_cap)
    fresh_until = _parse_time(entry.get("fresh_until"))
    within_http_freshness = fresh_until is None or now < fresh_until
    if (body_exists and same_day and next_check_at and now < next_check_at
            and within_http_freshness and not _requires_revalidation(entry)
            and not force_revalidate):
        prepared.cached_response = synthetic_response(root, entry, "fresh")
        return prepared

    if body_exists and policy["conditional"]:
        if entry.get("etag"):
            prepared.headers["If-None-Match"] = str(entry["etag"])
        if entry.get("last_modified"):
            prepared.headers["If-Modified-Since"] = str(entry["last_modified"])
    return prepared


def _cache_control(headers) -> dict:
    directives = {}
    for token in _header(headers, "Cache-Control").split(","):
        token = token.strip()
        if not token:
            continue
        key, _, value = token.partition("=")
        directives[key.lower()] = value.strip().strip('"') if value else True
    return directives


def _explicit_freshness_seconds(headers, now: datetime) -> int | None:
    directives = _cache_control(headers)
    if "no-cache" in directives:
        return 0
    try:
        if "max-age" in directives:
            age = max(0, int(_header(headers, "Age") or 0))
            return max(0, int(directives["max-age"]) - age)
    except (TypeError, ValueError):
        pass
    expires = _header(headers, "Expires")
    if expires:
        try:
            value = parsedate_to_datetime(expires)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return max(0, int((value.astimezone(timezone.utc) - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    return None


def retry_after_seconds(headers, now: datetime | None = None) -> int:
    value = _header(headers, "Retry-After")
    if not value:
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0, int((parsed - normalize_now(now)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0


def _schedule_interval_seconds(policy: dict, unchanged_streak: int) -> int:
    minimum = policy["min_interval_minutes"] * 60
    maximum = policy["max_interval_minutes"] * 60
    if not policy["adaptive"]:
        learned = minimum
    else:
        learned = minimum * (2 ** min(max(0, unchanged_streak), 10))
    return min(maximum, max(minimum, learned))


def _fresh_until(headers, now: datetime) -> str | None:
    seconds = _explicit_freshness_seconds(headers, now)
    return ((now + timedelta(seconds=seconds)).isoformat()
            if seconds is not None else None)


def _requires_revalidation(entry: dict) -> bool:
    directives = _cache_control(entry.get("response_headers") or {})
    vary = {
        token.strip().lower()
        for token in _header(entry.get("response_headers") or {}, "Vary").split(",")
        if token.strip()
    }
    return (any(name in directives for name in (
        "no-store", "no-cache", "must-revalidate", "proxy-revalidate"))
        or not vary.issubset({"accept"}))


def _may_serve_stale(entry: dict, now: datetime, policy: dict) -> bool:
    directives = _cache_control(entry.get("response_headers") or {})
    last_success = _parse_time(entry.get("last_success_at"))
    within_age = bool(
        last_success and now - last_success <= timedelta(
            minutes=policy["max_stale_minutes"]))
    return within_age and not any(name in directives for name in (
        "no-store", "no-cache", "must-revalidate", "proxy-revalidate"))


def _record_host_cooldown(root: Path, url: str, now: datetime,
                          retry_after: int, error: str) -> None:
    if retry_after <= 0:
        return
    key = host_state_key(url)
    old = load_entry(root, key)
    blocked_until = now + timedelta(seconds=retry_after)
    previous = _parse_time(old.get("blocked_until"))
    if previous and previous > blocked_until:
        blocked_until = previous
    entry = {
        "version": 1,
        "kind": "host_cooldown",
        "origin": normalized_origin(url),
        "last_checked_at": now.isoformat(),
        "blocked_until": blocked_until.isoformat(),
        "last_error": error[:500],
    }
    _write_entry(root, key, entry)


def record_host_cooldown(root: Path, url: str, *, retry_after: int,
                         now: datetime | None = None, error: str = "") -> None:
    """Persist Retry-After while the caller still owns the origin lease."""
    _record_host_cooldown(
        root, url, normalize_now(now), max(0, int(retry_after)), error)


def _write_entry(root: Path, key: str, entry: dict, body: bytes | None = None) -> None:
    lock = root / "cache.lock"
    last_error = None
    for attempt in range(3):
        try:
            with exclusive_lock(lock, stale_after=15 * 60):
                if body is not None:
                    digest = hashlib.sha256(body).hexdigest()
                    atomic_write_if_changed(_body_path(root, digest), body)
                encoded = json.dumps(
                    entry, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                atomic_write_if_changed(_entry_path(root, key), encoded)
            return
        except RuntimeError as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.05)
    raise last_error


def prune_cache(root: Path, *, now: datetime | None = None,
                max_age_days: int = 45) -> dict[str, int]:
    """Remove expired metadata and bodies no current entry references."""
    now = normalize_now(now)
    cutoff = now - timedelta(days=max(1, int(max_age_days)))
    removed_entries = 0
    removed_bodies = 0
    referenced = set()
    with exclusive_lock(root / "cache.lock", stale_after=15 * 60):
        for path in sorted((root / "entries").glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                entry = None
            checked = _parse_time((entry or {}).get("last_checked_at"))
            if entry is None or checked is None or checked < cutoff:
                path.unlink(missing_ok=True)
                removed_entries += 1
                continue
            digest = str(entry.get("body_sha256") or "")
            if digest:
                referenced.add(digest)
        for path in sorted((root / "bodies").glob("*.bin")):
            if path.stem not in referenced:
                path.unlink(missing_ok=True)
                removed_bodies += 1
    return {"removed_entries": removed_entries, "removed_bodies": removed_bodies}


def _noncacheable_entry(prepared: PreparedRequest, now: datetime,
                        logical_day: str) -> dict:
    return {
        "version": 1,
        "url": prepared.url,
        "accept": prepared.accept,
        "cacheable": False,
        "stored_at": now.isoformat(),
        "last_checked_at": now.isoformat(),
        "last_success_at": now.isoformat(),
        "logical_day": logical_day,
        "fresh_until": now.isoformat(),
        "next_check_at": now.isoformat(),
        "failure_streak": 0,
        "retry_at": None,
        "allow_stale": False,
    }


def discard_cached_response(root: Path, prepared: PreparedRequest, *,
                            now: datetime | None = None,
                            logical_day: str | None = None,
                            error: str = "") -> None:
    """Tombstone bytes that failed caller-level format validation."""
    now = normalize_now(now)
    logical_day = logical_day or now.date().isoformat()
    entry = _noncacheable_entry(prepared, now, logical_day)
    entry["last_error"] = error[:500]
    _write_entry(root, prepared.key, entry)
    prepared.entry = entry
    prepared.cached_response = None


def store_success(root: Path, prepared: PreparedRequest, response,
                  *, source: dict | None = None, now: datetime | None = None,
                  logical_day: str | None = None):
    now = normalize_now(now)
    logical_day = logical_day or now.date().isoformat()
    policy = resolve_policy(source)
    old = prepared.entry
    if response.status_code == 304:
        cached = synthetic_response(root, old, "revalidated", extra_headers=response.headers)
        if cached is None:
            return None
        unchanged_streak = int(old.get("unchanged_streak") or 0) + 1
        merged_headers = _response_headers(old, response.headers)
        merged_directives = _cache_control(merged_headers)
        if (not policy["enabled"] or "no-store" in merged_directives
                or _header(merged_headers, "Vary").strip() == "*"):
            _write_entry(
                root, prepared.key,
                _noncacheable_entry(prepared, now, logical_day))
            cached.frontier_cache_status = "revalidated_no_store"
            cached.frontier_last_network_success_at = now.isoformat()
            return cached
        entry = dict(old)
        entry.update({
            "etag": _header(response.headers, "ETag") or old.get("etag", ""),
            "last_modified": (_header(response.headers, "Last-Modified")
                              or old.get("last_modified", "")),
            "response_headers": merged_headers,
            "last_checked_at": now.isoformat(),
            "last_success_at": now.isoformat(),
            "logical_day": logical_day,
            "fresh_until": _fresh_until(merged_headers, now),
            "next_check_at": (now + timedelta(seconds=_schedule_interval_seconds(
                policy, unchanged_streak))).isoformat(),
            "unchanged_streak": unchanged_streak,
            "failure_streak": 0,
            "retry_at": None,
            "allow_stale": False,
        })
        _write_entry(root, prepared.key, entry)
        cached.frontier_last_network_success_at = now.isoformat()
        return cached

    body = bytes(response.content)
    directives = _cache_control(response.headers)
    cacheable = (policy["enabled"] and "no-store" not in directives
                 and len(body) <= policy["max_body_bytes"])
    if _header(response.headers, "Vary").strip() == "*":
        cacheable = False
    if not cacheable:
        # Replace metadata so a prior fresh body cannot survive a later no-store,
        # Vary: *, explicit disable, or body-size rejection decision.
        _write_entry(
            root, prepared.key,
            _noncacheable_entry(prepared, now, logical_day))
        response.frontier_cache_status = "bypass"
        response.frontier_last_network_success_at = now.isoformat()
        return response

    digest = hashlib.sha256(body).hexdigest()
    changed = digest != old.get("body_sha256")
    unchanged_streak = 0 if changed else int(old.get("unchanged_streak") or 0) + 1
    stored_headers = _response_headers({}, response.headers)
    entry = {
        "version": 1,
        "url": prepared.url,
        "accept": prepared.accept,
        "etag": _header(response.headers, "ETag"),
        "last_modified": _header(response.headers, "Last-Modified"),
        "response_headers": stored_headers,
        "encoding": response.encoding if isinstance(response.encoding, str) else None,
        "body_sha256": digest,
        "stored_at": now.isoformat(),
        "last_checked_at": now.isoformat(),
        "last_success_at": now.isoformat(),
        "logical_day": logical_day,
        "fresh_until": _fresh_until(response.headers, now),
        "next_check_at": (now + timedelta(seconds=_schedule_interval_seconds(
            policy, unchanged_streak))).isoformat(),
        "unchanged_streak": unchanged_streak,
        "failure_streak": 0,
        "retry_at": None,
        "allow_stale": False,
    }
    _write_entry(root, prepared.key, entry, body)
    response.frontier_cache_status = (
        "miss" if not old.get("body_sha256") else "updated" if changed else "unchanged")
    response.frontier_cached_at = now.isoformat()
    response.frontier_last_network_success_at = now.isoformat()
    return response


def record_failure(root: Path, prepared: PreparedRequest, *,
                   source: dict | None = None, now: datetime | None = None,
                   logical_day: str | None = None, retry_after: int = 0,
                   retryable: bool = True, allow_stale: bool = True,
                   error: str = "", host_url: str | None = None) -> None:
    now = normalize_now(now)
    logical_day = logical_day or now.date().isoformat()
    policy = resolve_policy(source)
    old = prepared.entry
    if not policy["enabled"]:
        return
    streak = int(old.get("failure_streak") or 0) + 1
    if retryable:
        backoff = policy["failure_base_minutes"] * 60 * (2 ** min(streak - 1, 10))
        backoff = min(policy["failure_max_minutes"] * 60, backoff)
        # Retry-After is the origin's lower bound, not a hint our local cap may shorten.
        backoff = max(backoff, retry_after)
        retry_at = (now + timedelta(seconds=backoff)).isoformat()
    else:
        retry_at = None
    entry = dict(old)
    entry.update({
        "version": 1,
        "url": prepared.url,
        "accept": prepared.accept,
        "last_checked_at": now.isoformat(),
        "logical_day": logical_day,
        "failure_streak": streak,
        "retry_at": retry_at,
        "allow_stale": bool(allow_stale and retryable),
        "last_error": error[:500],
    })
    _write_entry(root, prepared.key, entry)
    if retryable and retry_after > 0:
        _record_host_cooldown(
            root, host_url or prepared.url, now, retry_after, error)
