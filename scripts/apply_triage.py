# -*- coding: utf-8 -*-
"""Apply validated evidence additions to claims.yaml without rewriting YAML.

The automatic path deliberately cannot create claims or change status.  It only
inserts JSON-compatible inline YAML evidence records before the target claim's
watch field, preserving all hand-written comments and status history.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

from signal_inbox import SOURCE_TYPES, canonical_url
from state_io import atomic_write_if_changed, exclusive_lock, semantic_hash

ROOT = Path(__file__).resolve().parent.parent
CLAIM_ID_RE = re.compile(r"^  - id:\s*([^\s#]+)")
WATCH_RE = re.compile(r"^    watch:")
ALLOWED_TOP = {"evidence_additions"}
ALLOWED_EVIDENCE = {"claim_id", "src", "type", "verdict", "link", "date"}


def validate_addition(raw: dict) -> dict:
    unknown = set(raw) - ALLOWED_EVIDENCE
    if unknown:
        raise ValueError(f"证据出现未知字段：{', '.join(sorted(unknown))}")
    claim_id = str(raw.get("claim_id", "")).strip()
    src = str(raw.get("src", "")).strip()
    verdict = str(raw.get("verdict", "")).strip()
    ev_type = str(raw.get("type", "")).strip()
    if not claim_id or len(claim_id) > 100:
        raise ValueError("claim_id 不能为空且不得超过 100 字符")
    if not src or len(src) > 300:
        raise ValueError("src 不能为空且不得超过 300 字符")
    if not verdict or len(verdict) > 3000:
        raise ValueError("verdict 不能为空且不得超过 3000 字符")
    if ev_type not in SOURCE_TYPES:
        raise ValueError(f"type 必须是：{', '.join(sorted(SOURCE_TYPES))}")
    try:
        day = date.fromisoformat(str(raw.get("date"))).isoformat()
    except ValueError as exc:
        raise ValueError("date 必须是 YYYY-MM-DD") from exc
    link = canonical_url(str(raw.get("link", "")))
    return {"claim_id": claim_id, "src": src, "type": ev_type,
            "verdict": verdict, "link": link, "date": day}


def validate_proposal(raw: dict) -> list[dict]:
    if not isinstance(raw, dict):
        raise ValueError("提案必须是 JSON 对象")
    unknown = set(raw) - ALLOWED_TOP
    if unknown:
        raise ValueError("自动路径禁止这些字段（不得立案或改判）："
                         + ", ".join(sorted(unknown)))
    additions = raw.get("evidence_additions")
    if not isinstance(additions, list):
        raise ValueError("evidence_additions 必须是数组")
    return [validate_addition(item) for item in additions]


def _claim_ranges(lines: list[str]) -> dict[str, tuple[int, int]]:
    starts = []
    for idx, line in enumerate(lines):
        match = CLAIM_ID_RE.match(line)
        if match:
            starts.append((match.group(1).strip('"\''), idx))
    ranges = {}
    for pos, (claim_id, start) in enumerate(starts):
        end = starts[pos + 1][1] if pos + 1 < len(starts) else len(lines)
        ranges[claim_id] = (start, end)
    return ranges


def _evidence_identity(evidence: dict) -> str:
    raw_day = evidence.get("date", "")
    day = raw_day.isoformat() if hasattr(raw_day, "isoformat") else str(raw_day).strip()
    payload = {
        "src": str(evidence.get("src", "")).strip(),
        "type": str(evidence.get("type", "")).strip(),
        "verdict": str(evidence.get("verdict", "")).strip(),
        "link": canonical_url(str(evidence.get("link", ""))),
        "date": day,
    }
    return semantic_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")))


def _existing_evidence_ids(claims: list[dict]) -> dict[str, set[str]]:
    result = {}
    for claim in claims:
        identities = set()
        for ev in claim.get("evidence") or []:
            try:
                identities.add(_evidence_identity(ev))
            except ValueError:
                pass
        result[str(claim.get("id"))] = identities
    return result


def apply_additions_text(original: str, additions: list[dict]) -> tuple[str, int, int]:
    parsed = yaml.safe_load(original) or {}
    claims = parsed.get("claims") or []
    by_id = {str(c.get("id")): c for c in claims}
    existing = _existing_evidence_ids(claims)
    accepted = []
    duplicates = 0
    seen_batch = defaultdict(set)
    for addition in additions:
        # Defense in depth: callers normally pass validate_proposal(), but the
        # mutation boundary must not rely on that ordering for deduplication.
        addition = dict(addition)
        addition["link"] = canonical_url(str(addition.get("link", "")))
        claim_id = addition["claim_id"]
        if claim_id not in by_id:
            raise ValueError(f"账本中不存在 claim：{claim_id}")
        identity = _evidence_identity(addition)
        if (identity in existing.get(claim_id, set())
                or identity in seen_batch[claim_id]):
            duplicates += 1
            continue
        accepted.append(addition)
        seen_batch[claim_id].add(identity)
    if not accepted:
        return original, 0, duplicates

    lines = original.splitlines(keepends=True)
    ranges = _claim_ranges([line.rstrip("\r\n") for line in lines])
    grouped = defaultdict(list)
    for addition in accepted:
        grouped[addition["claim_id"]].append(addition)

    insertions = []
    newline = "\r\n" if "\r\n" in original else "\n"
    for claim_id, records in grouped.items():
        start, end = ranges[claim_id]
        watch_idx = next((i for i in range(start, end)
                          if WATCH_RE.match(lines[i].rstrip("\r\n"))), None)
        if watch_idx is None:
            raise ValueError(f"claim {claim_id} 缺少 watch 字段，拒绝猜测插入位置")
        rendered = []
        for record in records:
            payload = {k: record[k] for k in ("src", "type", "verdict", "link", "date")}
            rendered.append("      - " + json.dumps(payload, ensure_ascii=False,
                                                       separators=(", ", ": ")) + newline)
        insertions.append((watch_idx, rendered))
    for idx, rendered in sorted(insertions, reverse=True):
        lines[idx:idx] = rendered
    updated = "".join(lines)

    reparsed = yaml.safe_load(updated) or {}
    after = {str(c.get("id")): c for c in reparsed.get("claims") or []}
    if set(after) != set(by_id):
        raise ValueError("写入后 claim 集合发生变化，拒绝保存")
    for claim_id in by_id:
        if after[claim_id].get("status") != by_id[claim_id].get("status"):
            raise ValueError(f"写入后 status 意外变化：{claim_id}")
    return updated, len(accepted), duplicates


def apply_proposal(root: Path, proposal: dict, *, run_tests: bool = False) -> tuple[int, int]:
    additions = validate_proposal(proposal)
    claims_path = root / "config" / "claims.yaml"
    lock = root / "data" / "state" / "locks" / "claims-writer.lock"
    with exclusive_lock(lock):
        original = claims_path.read_text(encoding="utf-8")
        updated, added, duplicates = apply_additions_text(original, additions)
        if not added:
            return 0, duplicates
        atomic_write_if_changed(claims_path, updated)
        if run_tests:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode != 0:
                atomic_write_if_changed(claims_path, original)
                raise RuntimeError("回归测试失败，claims.yaml 已原子回滚：\n"
                                   + (result.stdout + result.stderr)[-2000:])
    return added, duplicates


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True,
                    help="只允许 evidence_additions 的 JSON 提案")
    args = ap.parse_args()
    proposal = json.loads(args.input.read_text(encoding="utf-8"))
    added, duplicates = apply_proposal(ROOT, proposal, run_tests=True)
    print(f"claims 证据写入：新增 {added}，重复 {duplicates}；未立案、未改判")


if __name__ == "__main__":
    main()
