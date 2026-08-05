# -*- coding: utf-8 -*-
"""开机即查的日常总调度：探针 → 机械日报 → 有界上下文 → 合成 → finalize。

设计：幂等 + 有序。每个环节先查当天是否已完成，未完成才跑；全齐则秒退。
触发（Windows 任务）：FrontierRadar-Daily 每天 09:00（WakeToRun）+ 每次登录；
FrontierRadar-SynthesisFallback 在 09:45–10:45 每 15 分钟重试一次本地合成兜底。
电脑几点开机都行——登录触发会把当天缺的环节补齐。

合成兜底：过了当天 09:45 日报仍是机械版（无「人工合成」标记）时，
读取仓库私有 playbook 作为提示词，用 `claude -p` 无头补跑。
正常情况下 App 的定时会话（09:30）会先完成合成，兜底不触发。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from brief_marker import brief_synthesized  # 只依赖标准库，不会拖进 yaml/requests
from synthesis_lease import acquire_lease, release_lease

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "data" / "state" / "orchestrator.lock"
LOG = ROOT / "data" / "state" / "orchestrator_log.jsonl"
SYNTH_PROMPT = ROOT / "playbooks" / "daily-brief-synthesis.md"
SYNTH_FALLBACK_AFTER = (9, 45)     # 当天此时刻后日报仍为机械版才兜底
APP_LEASE_STALE_AFTER = 30 * 60    # App 异常退出后，最多阻塞兜底 30 分钟

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(step, status, note=""):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "step": step, "status": status, "note": note},
                           ensure_ascii=False) + "\n")
    print(f"[{step}] {status}{'：' + note if note else ''}", flush=True)


def run(cmd, timeout, *, env=None):
    return subprocess.run(cmd, cwd=ROOT, timeout=timeout, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, env=env)


def build_context(day):
    result = run([sys.executable, "scripts/build_analysis_context.py", "--date", day],
                 timeout=120)
    log("context", "ok" if result.returncode == 0 else f"退出码 {result.returncode}",
        ((result.stdout or "") + (result.stderr or "")).strip()[-200:])
    return result


def run_synthesis(day, brief, context_result):
    """Run bounded synthesis; return True when finalized under its lease."""
    h, m = SYNTH_FALLBACK_AFTER
    current = datetime.now()  # 前面的探针可能跑很久，不能继续用进程启动时刻判断
    past_cutoff = (current.hour, current.minute) >= (h, m)
    if brief_synthesized(brief):
        log("synthesis", "已是合成版，跳过")
    elif not past_cutoff:
        log("synthesis", f"未到兜底时刻（{h:02d}:{m:02d}），留给 App 定时会话")
    elif context_result.returncode != 0:
        log("synthesis", "skip", "当日有界上下文生成失败，拒绝使用旧上下文")
    elif not SYNTH_PROMPT.exists():
        log("synthesis", "skip", f"未找到 {SYNTH_PROMPT}")
    elif not shutil.which("claude"):
        log("synthesis", "skip", "claude CLI 不在 PATH")
    else:
        lease_token = acquire_lease(
            ROOT, day, "orchestrator", stale_after=APP_LEASE_STALE_AFTER)
        if lease_token is None:
            log("synthesis", "skip", "App 合成会话持有共享 lease，本次不并发兜底")
        else:
            try:
                text = SYNTH_PROMPT.read_text(encoding="utf-8")
                if text.startswith("---"):  # 去掉 frontmatter
                    text = text.split("---", 2)[-1]
                prompt = (text.strip()
                          + f"\n\n（本次为无人值守兜底运行，今天的日期是 {day}；"
                            "共享 synthesis lease 已由 orchestrator 持有，"
                            "跳过 playbook 中的 acquire/release；"
                            "数据与机械版日报应已就绪，若缺失按提示词步骤 1 处理。）")
                # 本机可能残留失效的 API key；Claude CLI 会优先于已登录的
                # claude.ai 会话读取它，导致兜底稳定报 401。只对子进程移除，
                # 不修改用户的持久环境。
                synthesis_env = os.environ.copy()
                synthesis_env.pop("ANTHROPIC_API_KEY", None)
                synthesis_result = run(
                    [shutil.which("claude"), "-p", prompt,
                     "--permission-mode", "acceptEdits",
                     "--tools", "Read,Write,Edit,Bash",
                     "--allowedTools", "Read", "Write", "Edit",
                     "Bash(python scripts/build_analysis_context.py *)",
                     "Bash(python scripts/signal_inbox.py add --input data/state/pending_inbox.json)",
                     "Bash(python scripts/apply_triage.py --input data/state/pending_evidence.json)",
                     "Bash(python scripts/build_report_dossiers.py)",
                     "Bash(python scripts/finalize_daily.py *)",
                     "--disallowedTools", "PowerShell", "WebFetch", "WebSearch"],
                    timeout=2400, env=synthesis_env)
                log("synthesis", "ok" if brief_synthesized(brief) else "兜底后仍非合成版",
                    (synthesis_result.stdout or "").strip()[-200:])
                # The lease covers the whole fallback transaction, including
                # triage-derived views and the final backup.  Releasing it
                # between synthesis and finalization would reopen the App race.
                run_finalization(day, brief, lease_held=True)
                return True
            finally:
                release_lease(ROOT, day, "orchestrator")
    return False


def run_finalization(day, brief, *, lease_held=False):
    """Finalize only a synthesized brief; never back up a provisional snapshot."""
    if not brief_synthesized(brief):
        log("finalize", "skip", "日报仍是机械版，不创建误导性的最终备份")
        return None
    owner = "orchestrator-finalize"
    token = True
    if not lease_held:
        token = acquire_lease(
            ROOT, day, owner, stale_after=APP_LEASE_STALE_AFTER)
        if token is None:
            log("finalize", "skip", "App 或另一合成器仍持有共享 lease")
            return None
    try:
        result = run([sys.executable, "scripts/finalize_daily.py", "--date", day],
                     timeout=1200)
        log("finalize", "ok" if result.returncode == 0 else f"退出码 {result.returncode}",
            ((result.stdout or "") + (result.stderr or "")).strip()[-300:])
        return result
    finally:
        if not lease_held:
            release_lease(ROOT, day, owner)


def main(*, synthesis_only=False):
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    raw = ROOT / "data" / "raw" / day
    brief = ROOT / "briefs" / f"{day}.md"

    # 并发锁：登录触发与 09:00 定时可能同时开跑
    if LOCK.exists() and (now.timestamp() - LOCK.stat().st_mtime) < 90 * 60:
        print("[lock] 另一实例正在运行（或 90 分钟内异常退出过），本次跳过")
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(day, encoding="utf-8")

    try:
        if synthesis_only:
            if not (raw / "_fetch_log.json").exists() or not brief.exists():
                log("synthesis", "skip", "当日原始数据或机械日报未就绪")
                return
            finalized = run_synthesis(day, brief, build_context(day))
            if not finalized:
                run_finalization(day, brief)
            return

        # 1. 账号探针（每天一次；auth 失败时 run_probe 自身会整臂跳过）
        if (raw / "probe.json").exists():
            log("probe", "已完成，跳过")
        else:
            p = run([sys.executable, "probes/run_probe.py"], timeout=3600)
            log("probe", "ok" if p.returncode == 0 else f"退出码 {p.returncode}",
                (p.stdout or "").strip()[-200:])

        # 2. 抓取 + 机械日报
        fetched_now = False
        if (raw / "_fetch_log.json").exists():
            log("fetch", "已完成，跳过")
        else:
            p = run([sys.executable, "scripts/fetch_l1.py"], timeout=1800)
            fetched_now = p.returncode == 0
            log("fetch", "ok" if fetched_now else f"退出码 {p.returncode}",
                (p.stderr or "").strip()[-200:])
        # 机械版只在日报缺失或本次有新数据、且尚未被合成版覆盖时重建
        if not brief_synthesized(brief) and (fetched_now or not brief.exists()):
            p = run([sys.executable, "scripts/build_digest.py"], timeout=600)
            log("digest", "ok" if p.returncode == 0 else f"退出码 {p.returncode}")
        else:
            log("digest", "已完成，跳过")

        # 3. 生成有界分析上下文。失败不破坏机械日报，但不允许静默使用旧日期上下文。
        context_result = build_context(day)

        # 4. 合成兜底（App 定时会话没跑成时才出手）
        finalized = run_synthesis(day, brief, context_result)

        # 5. 统一收尾：dossier → 上一完整月复盘 → 快照指纹 → 私有备份。
        if not finalized:
            run_finalization(day, brief)
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthesis-only", action="store_true",
                    help="只运行本地合成兜底；不抓取、不执行独立备份或推送")
    main(synthesis_only=ap.parse_args().synthesis_only)
