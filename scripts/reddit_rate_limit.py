# -*- coding: utf-8 -*-
"""Cross-process hard rate limit for unauthenticated Reddit requests."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from state_io import atomic_write_if_changed

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "config" / "reddit_access.yaml"
STATE_PATH = ROOT / "data" / "state" / "reddit_request_budget.json"
LOCK_PATH = ROOT / "data" / "state" / "locks" / "reddit-request-budget.lock"


class RedditDailyBudgetExceeded(RuntimeError):
    pass


def is_reddit_url(url):
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host == "reddit.com" or host.endswith(".reddit.com")


def load_policy(path=POLICY_PATH):
    policy = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return normalize_policy(policy)


def normalize_policy(policy):
    """Configuration may tighten these limits, but may never loosen them."""
    return {
        "enabled": bool(policy.get("enabled", True)),
        "min_interval_seconds": max(1800, int(policy.get("min_interval_seconds", 1800))),
        "max_requests_per_utc_day": min(
            2, max(1, int(policy.get("max_requests_per_utc_day", 2)))),
    }


def _read_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _parse_utc(value):
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


@contextmanager
def _budget_lock(path):
    """Acquire a non-blocking kernel lock; crashes release it without takeover."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0))
    token = f"pid={os.getpid()} nonce={os.urandom(12).hex()}"
    locked = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RedditDailyBudgetExceeded(
                f"另一个进程持有 Reddit 预算锁，拒绝并行请求：{path}") from exc
        # Byte 0 is reserved for the kernel lock.  Windows treats that region
        # as mandatory even for this descriptor, so diagnostics live after it.
        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, token.encode("ascii"))
        os.ftruncate(fd, 1 + len(token))
        os.fsync(fd)
        yield token, fd
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _assert_budget_lock_owned(path, fd, token):
    try:
        opened = os.fstat(fd)
        current = Path(path).stat()
        os.lseek(fd, 1, os.SEEK_SET)
        owned = (
            opened.st_dev == current.st_dev
            and opened.st_ino == current.st_ino
            and os.read(fd, max(256, len(token))).decode("ascii") == token
        )
    except OSError:
        owned = False
    if not owned:
        raise RedditDailyBudgetExceeded(
            "Reddit 预算锁所有权已变化；为防止额度丢失，本次请求已停止")


def reserve_request(url, *, policy=None, state_path=STATE_PATH, lock_path=LOCK_PATH,
                    now_fn=None, sleep_fn=None):
    """Reserve one Reddit request slot, sleeping until its globally scheduled time.

    The slot is persisted before sleeping. A crashed process may waste a slot, but
    cannot create a burst. Failed network attempts and retries each reserve a slot.
    """
    if not is_reddit_url(url):
        return 0.0
    policy = normalize_policy(policy) if policy is not None else load_policy()
    if not policy["enabled"]:
        raise RedditDailyBudgetExceeded("Reddit 直接访问已禁用")
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    sleep_fn = sleep_fn or time.sleep
    state_path = Path(state_path)
    lock_path = Path(lock_path)

    with _budget_lock(lock_path) as (lock_token, lock_fd):
        now = now_fn().astimezone(timezone.utc)
        day = now.date().isoformat()
        state = _read_state(state_path)
        limit = policy["max_requests_per_utc_day"]
        records = []
        record_counts = {}
        for raw in state.get("reservations", []):
            if not isinstance(raw, dict):
                continue
            scheduled_at = _parse_utc(raw.get("scheduled_at"))
            if scheduled_at is None or scheduled_at.date() < now.date():
                continue
            record = dict(raw, scheduled_at=scheduled_at.isoformat())
            records.append(record)
            scheduled_day = scheduled_at.date().isoformat()
            record_counts[scheduled_day] = record_counts.get(scheduled_day, 0) + 1

        counts = {}
        for key, value in (state.get("scheduled_counts") or {}).items():
            try:
                if date.fromisoformat(str(key)) >= now.date():
                    counts[str(key)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        for key, value in record_counts.items():
            counts[key] = max(counts.get(key, 0), value)
        legacy_day = str(state.get("utc_date") or "")
        try:
            if date.fromisoformat(legacy_day) >= now.date():
                counts[legacy_day] = max(
                    counts.get(legacy_day, 0),
                    max(0, int(state.get("request_count", 0))))
        except (TypeError, ValueError):
            pass

        last = _parse_utc(state.get("last_reserved_at"))
        record_times = [
            _parse_utc(record.get("scheduled_at")) for record in records]
        record_times = [value for value in record_times if value is not None]
        if record_times:
            last = max([last] + record_times) if last is not None else max(record_times)
        scheduled = now
        if last is not None:
            scheduled = max(now, last + timedelta(seconds=policy["min_interval_seconds"]))
        scheduled_day = scheduled.date().isoformat()
        count = counts.get(scheduled_day, 0)
        if count >= limit:
            raise RedditDailyBudgetExceeded(
                f"Reddit UTC 日请求预算已用尽（{scheduled_day}）：{count}/{limit}")
        wait = max(0.0, (scheduled - now).total_seconds())
        records.append({
            "reserved_at": now.isoformat(),
            "scheduled_at": scheduled.isoformat(),
            "host": (urlsplit(url).hostname or "").lower(),
        })
        counts[scheduled_day] = count + 1
        new_state = {
            "version": 2,
            "utc_date": day,
            "request_count": counts.get(day, 0),
            "daily_limit": limit,
            "min_interval_seconds": policy["min_interval_seconds"],
            "last_reserved_at": scheduled.isoformat(),
            "scheduled_counts": dict(sorted(counts.items())),
            "reservations": records,
        }
        _assert_budget_lock_owned(lock_path, lock_fd, lock_token)
        atomic_write_if_changed(
            state_path, json.dumps(new_state, ensure_ascii=False, indent=2) + "\n")
        # A lost owner may already have written a conservative reservation, but
        # must never proceed to the network.  The extra slot can only reduce use.
        _assert_budget_lock_owned(lock_path, lock_fd, lock_token)

    if wait:
        sleep_fn(wait)
    return wait
