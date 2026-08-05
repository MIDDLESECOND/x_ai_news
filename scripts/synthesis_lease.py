# -*- coding: utf-8 -*-
"""Shared synthesis lease for the App task and orchestrator fallback."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from state_io import atomic_write_if_changed

ROOT = Path(__file__).resolve().parent.parent
OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
DEFAULT_STALE_AFTER = 90 * 60


def _validate(day: str, owner: str) -> tuple[str, str]:
    try:
        day = date.fromisoformat(day).isoformat()
    except ValueError as exc:
        raise ValueError("date 必须是 YYYY-MM-DD") from exc
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("owner 只能包含小写字母、数字、下划线和连字符")
    return day, owner


def _paths(root: Path, day: str, owner: str) -> tuple[Path, Path]:
    lock_root = root / "data" / "state" / "locks"
    return (lock_root / f"synthesis-{day}.lease",
            lock_root / f"synthesis-{day}-{owner}.token")


def _remove_stale_receipt(root: Path, day: str, record: dict) -> None:
    """Remove only the receipt that matches the reclaimed lease token."""
    owner = record.get("owner")
    token = record.get("token")
    if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
        return
    if not isinstance(token, str) or not token:
        return
    _, receipt_path = _paths(root, day, owner)
    try:
        receipt_token = receipt_path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    # A same-owner process may already have written a new receipt after the
    # stale lease was unlinked. Never remove a receipt with a different token.
    if receipt_token == token:
        receipt_path.unlink(missing_ok=True)


def acquire_lease(root: Path, day: str, owner: str,
                  *, stale_after: int = DEFAULT_STALE_AFTER) -> str | None:
    """Acquire the day's lease, returning its token or None when it is busy."""
    day, owner = _validate(day, owner)
    lease_path, receipt_path = _paths(root, day, owner)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - lease_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age <= stale_after:
                return None
            try:
                stale_record = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stale_record = {}
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
            else:
                _remove_stale_receipt(root, day, stale_record)
    token = uuid.uuid4().hex
    record = {
        "date": day,
        "owner": owner,
        "pid": os.getpid(),
        "token": token,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        atomic_write_if_changed(receipt_path, token + "\n")
    except Exception:
        # Never strand a fresh lease when its ownership receipt cannot be saved.
        lease_path.unlink(missing_ok=True)
        raise
    return token


def release_lease(root: Path, day: str, owner: str) -> bool:
    """Release only the lease matching this owner's private receipt token."""
    day, owner = _validate(day, owner)
    lease_path, receipt_path = _paths(root, day, owner)
    if not lease_path.exists() or not receipt_path.exists():
        return False
    token = receipt_path.read_text(encoding="utf-8").strip()
    try:
        record = json.loads(lease_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if record.get("owner") != owner or record.get("token") != token:
        return False
    lease_path.unlink(missing_ok=True)
    receipt_path.unlink(missing_ok=True)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="command", required=True)
    for command in ("acquire", "release"):
        parser = sub.add_parser(command)
        parser.add_argument("--date", required=True)
        parser.add_argument("--owner", required=True)
    args = ap.parse_args()
    try:
        if args.command == "acquire":
            if acquire_lease(ROOT, args.date, args.owner) is None:
                print("SYNTHESIS_LEASE_BUSY")
                sys.exit(2)
            print("SYNTHESIS_LEASE_ACQUIRED")
        else:
            if not release_lease(ROOT, args.date, args.owner):
                print("SYNTHESIS_LEASE_NOT_OWNED")
                sys.exit(1)
            print("SYNTHESIS_LEASE_RELEASED")
    except ValueError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
