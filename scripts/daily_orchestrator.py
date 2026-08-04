# -*- coding: utf-8 -*-
"""开机即查的日常总调度：探针 → 抓取+机械日报 → 合成兜底 → 私有备份。

设计：幂等 + 有序。每个环节先查当天是否已完成，未完成才跑；全齐则秒退。
触发（Windows 任务 FrontierRadar-Daily）：每天 09:00（WakeToRun）+ 每次登录。
电脑几点开机都行——登录触发会把当天缺的环节补齐。

合成兜底：过了当天 09:45 日报仍是机械版（无「人工合成」标记）时，
读取 daily-brief-synthesis 的 SKILL.md 作为提示词，用 `claude -p` 无头补跑。
正常情况下 App 的定时会话（09:30）会先完成合成，兜底不触发。
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from brief_marker import brief_synthesized  # 只依赖标准库，不会拖进 yaml/requests

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "data" / "state" / "orchestrator.lock"
LOG = ROOT / "data" / "state" / "orchestrator_log.jsonl"
SKILL = Path.home() / ".claude" / "scheduled-tasks" / "daily-brief-synthesis" / "SKILL.md"
SYNTH_FALLBACK_AFTER = (9, 45)     # 当天此时刻后日报仍为机械版才兜底

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def log(step, status, note=""):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "step": step, "status": status, "note": note},
                           ensure_ascii=False) + "\n")
    print(f"[{step}] {status}{'：' + note if note else ''}", flush=True)


def run(cmd, timeout):
    return subprocess.run(cmd, cwd=ROOT, timeout=timeout, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL)


def main():
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

        # 3. 合成兜底（App 定时会话没跑成时才出手）
        h, m = SYNTH_FALLBACK_AFTER
        past_cutoff = (now.hour, now.minute) >= (h, m)
        if brief_synthesized(brief):
            log("synthesis", "已是合成版，跳过")
        elif not past_cutoff:
            log("synthesis", f"未到兜底时刻（{h:02d}:{m:02d}），留给 App 定时会话")
        elif not SKILL.exists():
            log("synthesis", "skip", f"未找到 {SKILL}")
        elif not shutil.which("claude"):
            log("synthesis", "skip", "claude CLI 不在 PATH")
        else:
            text = SKILL.read_text(encoding="utf-8")
            if text.startswith("---"):  # 去掉 frontmatter
                text = text.split("---", 2)[-1]
            prompt = (text.strip()
                      + f"\n\n（本次为无人值守兜底运行，今天的日期是 {day}；"
                        f"数据与机械版日报应已就绪，若缺失按提示词步骤 1 处理。）")
            p = run([shutil.which("claude"), "-p", prompt,
                     "--permission-mode", "acceptEdits"], timeout=2400)
            log("synthesis", "ok" if brief_synthesized(brief) else "兜底后仍非合成版",
                (p.stdout or "").strip()[-200:])

        # 4. 私有备份（脚本自身无变更不提交；把当天已产出的东西尽早送出去）
        p = run([sys.executable, "scripts/backup_private.py"], timeout=900)
        log("backup", "ok" if p.returncode == 0 else f"退出码 {p.returncode}",
            (p.stdout or "").strip()[-200:])
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
