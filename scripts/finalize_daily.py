# -*- coding: utf-8 -*-
"""Finalize one synthesized day and back up one coherent artifact snapshot.

The synthesis session may update the brief, claims ledger, signal inbox, and
derived dossiers at different moments.  This module defines the transaction
boundary: refresh derived views, fingerprint the authoritative outputs, run the
private backup, then verify that the source fingerprint did not change while
the backup was in flight.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from backup_scope import snapshot_manifest
from brief_marker import brief_synthesized
from state_io import atomic_write_if_changed, exclusive_lock, semantic_hash

ROOT = Path(__file__).resolve().parent.parent


def _run(root: Path, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=root, timeout=timeout, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL)


def artifact_manifest(root: Path, day: str) -> tuple[dict[str, str], str]:
    return snapshot_manifest(root, day)


def _step_record(result: subprocess.CompletedProcess) -> dict:
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    return {"ok": result.returncode == 0, "returncode": result.returncode,
            "note": output[-500:]}


def _run_step(runner, command: list[str], timeout: int) -> dict:
    """Convert command exceptions into an explicit failed derived step."""
    try:
        return _step_record(runner(command, timeout))
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "note": f"{type(exc).__name__}: {exc}"[-500:],
        }


def _load_receipt(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _sync_confirmed(receipt_path: Path, marker_path: Path) -> bool:
    if not receipt_path.is_file() or not marker_path.is_file():
        return False
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return marker.get("receipt_sha256") == semantic_hash(receipt_path.read_bytes())


def finalize_day(root: Path, day: str, *, run_command=None,
                 force: bool = False, today: date | None = None) -> dict:
    day = date.fromisoformat(day).isoformat()
    brief = root / "briefs" / f"{day}.md"
    if not brief_synthesized(brief):
        raise RuntimeError(f"{brief} 不是合成版，拒绝标记本日事务完成")
    runner = run_command or (lambda cmd, timeout: _run(root, cmd, timeout))
    receipt_path = root / "data" / "state" / "daily_runs" / f"{day}.json"
    marker_path = root / "data" / "state" / "daily_runs" / f"{day}.sync.json"
    lock = root / "data" / "state" / "locks" / f"daily-finalize-{day}.lock"

    with exclusive_lock(lock, stale_after=90 * 60):
        current_day = (today or date.today()).isoformat()
        previous = _load_receipt(receipt_path)
        if (not force and day < current_day
                and previous.get("status") == "complete"
                and _sync_confirmed(receipt_path, marker_path)):
            return dict(previous, skipped=True)

        previous_sync_confirmed = (
            previous.get("status") == "complete"
            and _sync_confirmed(receipt_path, marker_path))
        previous_marker = (
            marker_path.read_bytes() if previous_sync_confirmed else None)
        # Any derived writer may change the managed snapshot or raise midway.
        # Invalidate the old proof before the first such command runs.  If every
        # output remains byte-identical, the exact marker is restored below.
        marker_path.unlink(missing_ok=True)

        steps = {}
        context_cmd = [sys.executable, "scripts/build_analysis_context.py", "--date", day]
        if day != current_day:
            context_cmd += [
                "--output", str(root / "data" / "state" / "analysis_context"
                                  / f"{day}.json")]
        steps["analysis_context"] = _run_step(runner, context_cmd, 120)

        derived_commands = [
            ("story_clusters", [
                sys.executable, "scripts/build_story_clusters.py", "--date", day], 120),
            ("source_independence", [
                sys.executable, "scripts/build_source_independence.py", "--date", day], 120),
            ("source_health", [
                sys.executable, "scripts/build_source_health.py", "--date", day,
                "--days", "30"], 180),
            ("story_lineage", [
                sys.executable, "scripts/build_story_lineage.py", "--date", day,
                "--days", "30"], 180),
            ("dossiers", [sys.executable, "scripts/build_report_dossiers.py"], 120),
            ("monthly_review", [
                sys.executable, "scripts/build_monthly_claim_review.py", "--as-of", day], 120),
        ]
        for name, command, timeout in derived_commands:
            steps[name] = _run_step(runner, command, timeout)
        required_steps = ("analysis_context",) + tuple(
            name for name, _, _ in derived_commands)
        views_ok = all(steps[name]["ok"] for name in required_steps)

        manifest, fingerprint = artifact_manifest(root, day)
        previous = _load_receipt(receipt_path)
        if (not force and views_ok
                and previous.get("status") == "complete"
                and previous.get("artifact_fingerprint") == fingerprint
                and previous_sync_confirmed
                and previous_marker is not None):
            atomic_write_if_changed(marker_path, previous_marker)
            return dict(previous, skipped=True)

        pending_receipt = {
            "version": 2, "date": day, "status": "backup_pending",
            "artifact_fingerprint": fingerprint, "artifacts": manifest,
            "steps": steps, "skipped": False,
        }
        atomic_write_if_changed(
            receipt_path,
            json.dumps(pending_receipt, ensure_ascii=False, indent=2,
                       sort_keys=True) + "\n")

        backup = runner([
            sys.executable, "scripts/backup_private.py",
            "--finalize-date", day,
            "--artifact-fingerprint", fingerprint,
        ], 900)
        steps["backup"] = _step_record(backup)
        after_manifest, after_fingerprint = artifact_manifest(root, day)
        stable = fingerprint == after_fingerprint
        steps["snapshot_stable_during_backup"] = {"ok": stable}

        if backup.returncode != 0:
            status = "backup_failed"
        elif not stable:
            status = "changed_during_backup"
        elif not views_ok:
            status = "complete_with_warnings"
        else:
            status = "complete"
        receipt = {
            "version": 2,
            "date": day,
            "status": status,
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "artifact_fingerprint": after_fingerprint,
            "artifacts": after_manifest,
            "steps": steps,
            "skipped": False,
        }
        atomic_write_if_changed(
            receipt_path,
            json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if status in {"complete", "complete_with_warnings"}:
            try:
                receipt_sync = runner([
                    sys.executable, "scripts/backup_private.py",
                    "--finalize-date", day,
                    "--artifact-fingerprint", after_fingerprint,
                    "--receipt-sync-only",
                ], 300)
                steps["receipt_backup"] = _step_record(receipt_sync)
            except Exception as exc:
                steps["receipt_backup"] = {
                    "ok": False, "returncode": None,
                    "note": f"{type(exc).__name__}: {exc}"[-500:],
                }
                receipt_sync = None
            if receipt_sync is None or receipt_sync.returncode != 0:
                receipt["status"] = "receipt_backup_failed"
                receipt["steps"] = steps
                atomic_write_if_changed(
                    receipt_path,
                    json.dumps(receipt, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n")
            else:
                marker = {
                    "version": 1,
                    "date": day,
                    "artifact_fingerprint": after_fingerprint,
                    "receipt_sha256": semantic_hash(receipt_path.read_bytes()),
                    "synced_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
                atomic_write_if_changed(
                    marker_path,
                    json.dumps(marker, ensure_ascii=False, indent=2,
                               sort_keys=True) + "\n")
        return receipt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--force", action="store_true",
                    help="强制重跑已完成日期；历史日期使用分片上下文，不覆盖 current")
    args = ap.parse_args()
    try:
        result = finalize_day(ROOT, args.date, force=args.force)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"本日事务：{result['status']}；"
          f"fingerprint={result['artifact_fingerprint'][:12]}"
          f"{'；无变化，跳过重复备份' if result.get('skipped') else ''}")
    if result["status"] in {"backup_failed", "changed_during_backup",
                            "receipt_backup_failed"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
