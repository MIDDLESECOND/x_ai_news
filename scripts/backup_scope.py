# -*- coding: utf-8 -*-
"""Shared definition of the stable private-backup snapshot."""
from __future__ import annotations

import json
from pathlib import Path

from state_io import semantic_hash

PATHS = (
    "config/claims.yaml",
    "config/accounts.yaml",
    "briefs",
    "data/state",
    "data/candidates_ledger.json",
    # Compact URL/title/time signals are the only durable comparison source
    # after full raw snapshots expire; unlike HTTP cache bodies, they are safe
    # and necessary to preserve in the private backup.
    "data/reddit_audit/l1_baseline",
    "probes",
    "docs",
    "playbooks",
    "reports",
)


def _excluded(rel: str, day: str) -> bool:
    return (
        rel.startswith("data/state/locks/")
        # Rebuildable HTTP response cache may contain third-party full bodies;
        # never copy it into the private Git backup or final artifact fingerprint.
        or rel.startswith("data/state/http_cache/")
        or rel == "data/state/orchestrator_log.jsonl"
        or rel == f"data/state/daily_runs/{day}.json"
        or (rel.startswith("data/state/daily_runs/") and rel.endswith(".sync.json"))
        # The baseline writer's refresh lock and atomic temp files are process
        # state, not durable comparison observations.
        or rel.startswith("data/reddit_audit/l1_baseline/.")
        or "/.tmp-" in rel
    )


def iter_managed_files(root: Path):
    for name in PATHS:
        path = root / name
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = sorted(p for p in path.rglob("*") if p.is_file())
        else:
            continue
        for candidate in candidates:
            rel = candidate.relative_to(root).as_posix()
            yield rel, candidate


def iter_snapshot_files(root: Path, day: str):
    for rel, candidate in iter_managed_files(root):
        if not _excluded(rel, day):
            yield rel, candidate


def snapshot_manifest(root: Path, day: str) -> tuple[dict[str, str], str]:
    manifest = {rel: semantic_hash(path.read_bytes())
                for rel, path in iter_snapshot_files(root, day)}
    fingerprint = semantic_hash(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")))
    return manifest, fingerprint


def verify_target_manifest(target_root: Path, manifest: dict[str, str]) -> list[str]:
    failures = []
    for rel, expected in manifest.items():
        path = target_root / rel
        if not path.is_file():
            failures.append(f"missing:{rel}")
        elif semantic_hash(path.read_bytes()) != expected:
            failures.append(f"hash:{rel}")
    return failures


def expected_backup_manifest(root: Path, day: str,
                             manifest: dict[str, str]) -> dict[str, str]:
    expected = dict(manifest)
    receipt_rel = f"data/state/daily_runs/{day}.json"
    receipt = root / receipt_rel
    if not receipt.is_file():
        raise ValueError(f"完成回执缺失：{receipt_rel}")
    expected[receipt_rel] = semantic_hash(receipt.read_bytes())
    return expected


def target_extra_files(target_root: Path, expected: dict[str, str]) -> list[str]:
    actual = {rel for rel, _ in iter_managed_files(target_root)}
    return sorted(actual - set(expected))


def verify_target_snapshot(target_root: Path,
                           expected: dict[str, str]) -> list[str]:
    failures = verify_target_manifest(target_root, expected)
    failures.extend(f"extra:{rel}" for rel in target_extra_files(target_root, expected))
    return failures
