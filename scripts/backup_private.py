# -*- coding: utf-8 -*-
"""私有层备份：把记忆核心（悬案账本、账号库、简报、探针历史、剧本、报告等）
镜像到独立的私有 git 仓库并推送。公开仓库不含这些文件（见 .gitignore）。

仅由 finalize_daily.py 在合成、分诊与派生刷新后调用。旧的独立定时任务
若直接运行本脚本会明确失败，避免把早期机械版误当成最终备份。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from backup_scope import (PATHS, expected_backup_manifest, snapshot_manifest,
                          target_extra_files, verify_target_snapshot)
from state_io import exclusive_lock

ROOT = Path(__file__).resolve().parent.parent
BACKUP_REPO = ROOT.parent / "x_ai_news_private"

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def git(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def git_bytes(repo: Path, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def gh(*args):
    exe = shutil.which("gh")
    if not exe:
        return None
    return subprocess.run([exe, *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


class GhAccount:
    """gh 凭据助手只认当前活跃账号（不按 URL 用户名路由，2026-08-02 实测）。
    推送前切到 MIDDLESECOND，完毕后还原，避免干扰用户日常使用的另一账号。"""
    NEEDED = "MIDDLESECOND"

    def __enter__(self):
        self.prev = None
        r = gh("api", "user", "--jq", ".login")
        if r and r.returncode == 0:
            active = r.stdout.strip()
            if active and active != self.NEEDED:
                self.prev = active
                switched = gh("auth", "switch", "-u", self.NEEDED)
                if not switched or switched.returncode != 0:
                    raise RuntimeError(f"无法切换 gh 账号到 {self.NEEDED}")
        return self

    def __exit__(self, *exc):
        if self.prev:
            gh("auth", "switch", "-u", self.prev)


def _git_checked(repo: Path, *args):
    result = git(repo, *args)
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败：{(result.stdout + result.stderr)[-500:]}")
    return result


def _head_paths(repo: Path) -> set[str]:
    result = git_bytes(repo, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *PATHS)
    if result.returncode != 0:
        raise RuntimeError(f"无法读取备份提交树：{result.stderr[-500:]!r}")
    return {value.decode("utf-8") for value in result.stdout.split(b"\0") if value}


def _verify_head(repo: Path, source_root: Path,
                 expected: dict[str, str]) -> list[str]:
    actual = _head_paths(repo)
    failures = []
    for rel in expected:
        if rel not in actual:
            failures.append(f"git-missing:{rel}")
            continue
        expected_blob = git(
            repo, "hash-object", f"--path={rel}", str(source_root / rel))
        actual_blob = git(repo, "rev-parse", f"HEAD:{rel}")
        if expected_blob.returncode != 0 or actual_blob.returncode != 0:
            failures.append(f"git-read:{rel}")
        elif expected_blob.stdout.strip() != actual_blob.stdout.strip():
            failures.append(f"git-blob:{rel}")
    failures.extend(f"git-extra:{rel}" for rel in sorted(set(actual) - set(expected)))
    return failures


def _remove_target_extras(repo: Path, expected: dict[str, str]) -> int:
    base = repo.resolve()
    removed = 0
    for rel in target_extra_files(repo, expected):
        path = (repo / rel).resolve()
        if base not in path.parents or not path.is_file():
            raise RuntimeError(f"拒绝删除备份范围之外的路径：{path}")
        path.unlink()
        removed += 1
    return removed


def _managed_pathspecs(repo: Path) -> list[str]:
    pathspecs = []
    for rel in PATHS:
        tracked = git(repo, "ls-files", "--", rel)
        if tracked.returncode != 0:
            raise RuntimeError(f"无法检查备份跟踪路径 {rel}：{tracked.stderr[-300:]}")
        if (repo / rel).exists() or tracked.stdout.strip():
            pathspecs.append(rel)
    return pathspecs


def perform_backup(args, *, root: Path = ROOT,
                   backup_repo: Path = BACKUP_REPO, push: bool = True) -> None:
    manifest, actual_fingerprint = snapshot_manifest(root, args.finalize_date)
    if actual_fingerprint != args.artifact_fingerprint:
        sys.exit("产物指纹已变化，拒绝备份非 finalize 快照")
    if not (backup_repo / ".git").exists():
        sys.exit(f"{backup_repo} 不是 git 仓库——先按 README 初始化备份仓库")
    receipt_rel = f"data/state/daily_runs/{args.finalize_date}.json"
    expected = expected_backup_manifest(root, args.finalize_date, manifest)
    if args.receipt_sync_only:
        copy_rows = [(receipt_rel, root / receipt_rel)]
    else:
        copy_rows = [(rel, root / rel) for rel in manifest]
        copy_rows.append((receipt_rel, root / receipt_rel))
    copied = 0
    for rel, src in copy_rows:
        if not src.is_file():
            sys.exit(f"备份源文件缺失：{rel}")
        dst = backup_repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    removed = _remove_target_extras(backup_repo, expected)
    after_manifest, after_fingerprint = snapshot_manifest(root, args.finalize_date)
    if after_fingerprint != args.artifact_fingerprint:
        sys.exit("复制期间源产物发生变化，拒绝提交混合时间点快照")
    after_expected = expected_backup_manifest(root, args.finalize_date, after_manifest)
    failures = verify_target_snapshot(backup_repo, after_expected)
    if failures:
        sys.exit("备份目标校验失败：" + ", ".join(failures[:10]))
    pathspecs = _managed_pathspecs(backup_repo)
    _git_checked(backup_repo, "add", "-A", "-f", "--", *pathspecs)
    staged = git(backup_repo, "diff", "--cached", "--quiet")
    if staged.returncode == 1:
        _git_checked(backup_repo, "commit", "-m", f"backup {args.finalize_date}")
    elif staged.returncode != 0:
        raise RuntimeError(f"无法检查备份暂存区：{staged.stderr[-500:]}")
    git_failures = _verify_head(backup_repo, root, after_expected)
    if git_failures:
        raise RuntimeError("备份 Git 提交树校验失败：" + ", ".join(git_failures[:10]))
    if push:
        with GhAccount():
            _git_checked(backup_repo, "push")
        head = _git_checked(backup_repo, "rev-parse", "HEAD").stdout.strip()
        upstream = _git_checked(
            backup_repo, "rev-parse", "@{upstream}").stdout.strip()
        if head != upstream:
            raise RuntimeError(f"push 后远端跟踪分支未对齐：{head} != {upstream}")
    print(f"已镜像并校验 {copied} 个文件、删除目标残留 {removed} 个"
          f"{'后推送' if push else ''} -> {backup_repo.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--finalize-date", required=True)
    ap.add_argument("--artifact-fingerprint", required=True)
    ap.add_argument("--receipt-sync-only", action="store_true")
    args = ap.parse_args()
    lock = ROOT / "data" / "state" / "locks" / "private-backup.lock"
    with exclusive_lock(lock, stale_after=30 * 60):
        perform_backup(args)


if __name__ == "__main__":
    main()
