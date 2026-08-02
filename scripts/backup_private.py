# -*- coding: utf-8 -*-
"""私有层备份：把记忆核心（悬案账本、账号库、简报、探针历史、剧本、报告等）
镜像到独立的私有 git 仓库并推送。公开仓库不含这些文件（见 .gitignore）。

用法：python scripts/backup_private.py
定时：FrontierRadar-Backup（每天 09:30，在探针/抓取/合成全部完成之后）。
"""
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_REPO = ROOT.parent / "x_ai_news_private"

# 备份对象：全部私有层，排除可再生的 data/raw
PATHS = [
    "config/claims.yaml",
    "config/accounts.yaml",
    "briefs",
    "data/state",
    "data/candidates_ledger.json",
    "probes",
    "docs",
    "playbooks",
    "reports",
]

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def git(*args):
    return subprocess.run(["git", "-C", str(BACKUP_REPO), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def main():
    if not (BACKUP_REPO / ".git").exists():
        sys.exit(f"{BACKUP_REPO} 不是 git 仓库——先按 README 初始化备份仓库")
    copied = 0
    for rel in PATHS:
        src = ROOT / rel
        dst = BACKUP_REPO / rel
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied += 1
    git("add", "-A")
    if not git("status", "--porcelain").stdout.strip():
        print("无变化，跳过提交")
        return
    r = git("commit", "-m", f"backup {date.today().isoformat()}")
    if r.returncode != 0:
        sys.exit(f"commit 失败：{r.stderr[-300:]}")
    p = git("push")
    if p.returncode != 0:
        sys.exit(f"push 失败（本地提交已保留）：{p.stderr[-300:]}")
    print(f"已备份 {copied} 项并推送 -> x_ai_news_private")


if __name__ == "__main__":
    main()
