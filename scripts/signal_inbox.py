# -*- coding: utf-8 -*-
"""Validated, deduplicated monthly inbox for weak or unassigned signals.

Fetched text is data.  The inbox stores only short analyst-authored metadata and
an original URL; it never stores full third-party content or executes anything.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from state_io import atomic_write_if_changed, exclusive_lock, semantic_hash

ROOT = Path(__file__).resolve().parent.parent
INBOX_ROOT = ROOT / "data" / "state" / "claim_inbox"
LOCK_ROOT = ROOT / "data" / "state" / "locks"
SOURCE_TYPES = {"controlled", "n1-user", "vendor", "index", "forum", "report"}
ACTIONS = {"watch_signal", "claim_candidate"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
DROP_QUERY_PREFIXES = ("utm_",)
DROP_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_url(value: str) -> str:
    value = (value or "").strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("url 必须是可点的 http/https 原始出处")
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port:
        netloc += f":{parts.port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in DROP_QUERY_KEYS
             and not any(k.lower().startswith(p) for p in DROP_QUERY_PREFIXES)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))


def _clean_text(name: str, value, limit: int, *, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{name} 不能为空")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ValueError(f"{name} 含控制字符")
    if len(text) > limit:
        raise ValueError(f"{name} 超过 {limit} 字符")
    return text


def observation_identity(record: dict) -> str:
    """Identify one observation without collapsing later snapshots of one URL."""
    fields = (
        "date", "title", "source_type", "matched_claim", "candidate_key",
        "why_it_matters", "main_alternative", "next_check", "action",
    )
    payload = {name: record.get(name) for name in fields}
    payload["url"] = canonical_url(str(record.get("url", "")))
    return semantic_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))


def validate_record(raw: dict) -> dict:
    allowed = {
        "date", "title", "url", "source_type", "matched_claim", "candidate_key",
        "why_it_matters", "main_alternative", "next_check", "action",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"未知字段：{', '.join(sorted(unknown))}")
    try:
        day = date.fromisoformat(str(raw.get("date"))).isoformat()
    except ValueError as exc:
        raise ValueError("date 必须是 YYYY-MM-DD") from exc
    source_type = str(raw.get("source_type", ""))
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"source_type 必须是：{', '.join(sorted(SOURCE_TYPES))}")
    action = str(raw.get("action", ""))
    if action not in ACTIONS:
        raise ValueError(f"action 必须是：{', '.join(sorted(ACTIONS))}")
    matched = _clean_text("matched_claim", raw.get("matched_claim"), 100, required=False) or None
    candidate = _clean_text("candidate_key", raw.get("candidate_key"), 80, required=False) or None
    if matched and candidate:
        raise ValueError("matched_claim 与 candidate_key 只能填写一个")
    if action == "claim_candidate" and not candidate:
        raise ValueError("claim_candidate 必须提供 candidate_key")
    if candidate and not SLUG_RE.match(candidate):
        raise ValueError("candidate_key 必须是 2–80 位小写字母、数字或连字符")
    if action == "watch_signal" and not (matched or candidate):
        raise ValueError("watch_signal 必须指向 matched_claim 或 candidate_key")

    url = canonical_url(str(raw.get("url", "")))
    record = {
        "version": 1,
        "date": day,
        "title": _clean_text("title", raw.get("title"), 300),
        "url": url,
        "source_type": source_type,
        "matched_claim": matched,
        "candidate_key": candidate,
        "why_it_matters": _clean_text("why_it_matters", raw.get("why_it_matters"), 1000),
        "main_alternative": _clean_text("main_alternative", raw.get("main_alternative"), 1000),
        "next_check": _clean_text("next_check", raw.get("next_check"), 1000),
        "action": action,
    }
    record["id"] = observation_identity(record)[:20]
    return record


def inbox_path(root: Path, day: str) -> Path:
    return root / "data" / "state" / "claim_inbox" / f"{day[:7]}.jsonl"


def load_month(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} 不是合法 JSONL") from exc
    return rows


def add_records(root: Path, raw_records: list[dict]) -> tuple[int, int, Path | None]:
    if not raw_records:
        return 0, 0, None
    records = [validate_record(r) for r in raw_records]
    months = {r["date"][:7] for r in records}
    if len(months) != 1:
        raise ValueError("一次写入只能包含同一个月份的记录")
    month = next(iter(months))
    path = root / "data" / "state" / "claim_inbox" / f"{month}.jsonl"
    lock = root / "data" / "state" / "locks" / f"claim-inbox-{month}.lock"
    with exclusive_lock(lock):
        existing = load_month(path)
        identities = set()
        for old in existing:
            try:
                identities.add(observation_identity(old))
            except ValueError:
                # Preserve malformed legacy rows without letting them block valid input.
                identities.add(f"legacy:{old.get('id')}")
        added = []
        for record in records:
            identity = observation_identity(record)
            if identity in identities:
                continue
            added.append(record)
            identities.add(identity)
        if not added:
            return 0, len(records), path
        merged = existing + added
        text = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in merged)
        atomic_write_if_changed(path, text)
    return len(added), len(records) - len(added), path


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add")
    add.add_argument("--input", type=Path, required=True,
                     help="含一条记录或记录数组的 JSON 文件")
    show = sub.add_parser("show")
    show.add_argument("--month", required=True, help="YYYY-MM")
    args = ap.parse_args()

    if args.command == "add":
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        records = raw if isinstance(raw, list) else [raw]
        added, duplicates, path = add_records(ROOT, records)
        print(f"候选箱：新增 {added}，重复 {duplicates} -> {path}")
    else:
        if not re.fullmatch(r"\d{4}-\d{2}", args.month):
            sys.exit("--month 必须是 YYYY-MM")
        path = INBOX_ROOT / f"{args.month}.jsonl"
        print(json.dumps(load_month(path), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
