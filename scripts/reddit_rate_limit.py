# -*- coding: utf-8 -*-
"""Cross-process hard rate limit for unauthenticated Reddit requests."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from state_io import atomic_write_if_changed, exclusive_lock

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

    with exclusive_lock(lock_path, stale_after=15 * 60):
        now = now_fn().astimezone(timezone.utc)
        day = now.date().isoformat()
        state = _read_state(state_path)
        count = int(state.get("request_count", 0)) if state.get("utc_date") == day else 0
        limit = policy["max_requests_per_utc_day"]
        if count >= limit:
            raise RedditDailyBudgetExceeded(
                f"Reddit UTC 日请求预算已用尽：{count}/{limit}")

        try:
            last = datetime.fromisoformat(state.get("last_reserved_at", ""))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            last = last.astimezone(timezone.utc)
        except (TypeError, ValueError):
            last = None
        scheduled = now
        if last is not None:
            scheduled = max(now, last + timedelta(seconds=policy["min_interval_seconds"]))
        wait = max(0.0, (scheduled - now).total_seconds())
        records = list(state.get("reservations", [])) if state.get("utc_date") == day else []
        records.append({
            "reserved_at": now.isoformat(),
            "scheduled_at": scheduled.isoformat(),
            "host": (urlsplit(url).hostname or "").lower(),
        })
        new_state = {
            "utc_date": day,
            "request_count": count + 1,
            "daily_limit": limit,
            "min_interval_seconds": policy["min_interval_seconds"],
            "last_reserved_at": scheduled.isoformat(),
            "reservations": records,
        }
        atomic_write_if_changed(
            state_path, json.dumps(new_state, ensure_ascii=False, indent=2) + "\n")

    if wait:
        sleep_fn(wait)
    return wait
