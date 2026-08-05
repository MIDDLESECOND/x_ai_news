# -*- coding: utf-8 -*-
"""Small, dependency-free helpers for rebuildable private state.

Derived state must be idempotent: unchanged input must not touch mtimes.  Writers
also use lock files under data/state so two schedulers cannot partially overwrite
the same artifact.
"""
from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path


def semantic_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def atomic_write_if_changed(path: Path, data: bytes | str) -> bool:
    """Atomically replace *path* only when bytes differ. Return True if written."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return True


@contextmanager
def exclusive_lock(path: Path, *, stale_after: int = 15 * 60):
    """Acquire a simple cross-process lock, clearing only demonstrably stale locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age <= stale_after:
                raise RuntimeError(f"另一个写入器持有锁：{path}")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        path.unlink(missing_ok=True)
