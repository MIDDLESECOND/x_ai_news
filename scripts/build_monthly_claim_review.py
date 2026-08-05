# -*- coding: utf-8 -*-
"""Render a deterministic monthly claim review from the signal inbox.

Quantitative gates nominate candidates only.  This script never edits the claims
ledger, creates a claim, or changes a claim status.
"""
from __future__ import annotations

import argparse
import calendar
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import build_digest as bd
from signal_inbox import load_month
from state_io import atomic_write_if_changed

ROOT = Path(__file__).resolve().parent.parent
INDEPENDENT_TYPES = {"controlled", "n1-user", "forum", "report"}
DEFAULT_WINDOW_DAYS = 60


def domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def candidate_gate(rows: list[dict]) -> tuple[bool, dict]:
    stats = {
        "days": len({r["date"] for r in rows}),
        "domains": len({domain(r["url"]) for r in rows if domain(r["url"])}),
        "source_types": len({r["source_type"] for r in rows}),
        "has_non_vendor_or_index": any(r["source_type"] in INDEPENDENT_TYPES for r in rows),
    }
    passed = (stats["days"] >= 3 and stats["domains"] >= 2
              and stats["source_types"] >= 2 and stats["has_non_vendor_or_index"])
    return passed, stats


def load_review_window(inbox_root: Path, month: str,
                       window_days: int = DEFAULT_WINDOW_DAYS) -> tuple[list[dict], date, date]:
    """Load calendar shards touched by a rolling window ending in *month*."""
    year, month_number = map(int, month.split("-"))
    end = date(year, month_number, calendar.monthrange(year, month_number)[1])
    start = end - timedelta(days=window_days - 1)
    cursor = date(start.year, start.month, 1)
    rows = []
    while cursor <= end:
        rows.extend(load_month(inbox_root / f"{cursor:%Y-%m}.jsonl"))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    filtered = [row for row in rows
                if start <= date.fromisoformat(str(row.get("date", ""))) <= end]
    filtered.sort(key=lambda row: (row.get("date", ""), row.get("id", "")))
    return filtered, start, end


def render_review(month: str, rows: list[dict], claims: list[dict],
                  *, window_start: date | None = None,
                  window_end: date | None = None) -> str:
    by_id = {c.get("id"): c for c in claims}
    matched = defaultdict(list)
    candidates = defaultdict(list)
    for row in rows:
        if row.get("matched_claim"):
            matched[row["matched_claim"]].append(row)
        else:
            candidates[row.get("candidate_key") or "unclassified"].append(row)

    window_line = (f"- 观察窗口：{window_start.isoformat()} 至 {window_end.isoformat()}（滚动窗口）"
                   if window_start and window_end else None)
    lines = [f"# Frontier Radar 悬案月度复盘候选 — {month}", "",
             "> 自动生成的候选视图，不是立案或改判结果。claims.yaml 是唯一权威来源。", "",
             *([window_line] if window_line else []),
             f"- 候选箱信号：{len(rows)} 条", f"- 指向现有悬案：{sum(map(len, matched.values()))} 条",
             f"- 新悬案候选：{sum(map(len, candidates.values()))} 条", ""]

    lines += ["## 应并入现有悬案", ""]
    if not matched:
        lines.append("（无）")
    for claim_id in sorted(matched):
        claim = by_id.get(claim_id)
        boundary = "；**职业边界：只建议复查正典，不得自动改判**" if claim and "ledger_ref" in claim else ""
        lines.append(f"### {claim_id}（{len(matched[claim_id])} 条）{boundary}")
        lines.append("")
        if claim:
            lines.append(f"> {claim.get('claim', '')}；当前状态：{claim.get('status', 'open')}")
            lines.append("")
        else:
            lines.append("> 警告：账本中找不到该 claim id，必须人工处理。")
            lines.append("")
        for row in matched[claim_id]:
            lines.append(f"- [{row['title']}]({row['url']})（{row['date']}；{row['source_type']}）")
            lines.append(f"  - 影响：{row['why_it_matters']}")
            lines.append(f"  - 替代解释：{row['main_alternative']}")
            lines.append(f"  - 下一验证：{row['next_check']}")
        lines.append("")

    lines += ["## 新悬案提名", ""]
    if not candidates:
        lines.append("（无）")
    for key in sorted(candidates):
        group = candidates[key]
        passed, stats = candidate_gate(group)
        verdict = "达到定量提名门槛，仍需人工确认可证伪性、非重复性与现实价值" if passed else "证据不足，继续观察"
        lines.append(f"### {key} — {verdict}")
        lines.append("")
        lines.append(f"- 独立日期：{stats['days']}；来源域名：{stats['domains']}；证据类型：{stats['source_types']}；"
                     f"含非厂商/指数材料：{'是' if stats['has_non_vendor_or_index'] else '否'}")
        for row in group:
            lines.append(f"- [{row['title']}]({row['url']})（{row['date']}；{row['source_type']}）")
            lines.append(f"  - 影响：{row['why_it_matters']}")
            lines.append(f"  - 替代解释：{row['main_alternative']}")
            lines.append(f"  - 下一验证：{row['next_check']}")
        lines.append("")

    lines += ["## 人工复核清单", "",
              "- 问题能否被明确证伪？", "- 是否可被现有悬案吸收？",
              "- 真/假会不会改变选择、测试或观察优先级？", "- 是否存在可执行的下一验证？",
              "- 若涉及职业含义，只能建议复查外部正典，不得在本项目内立战略结论。", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}", args.month):
        raise SystemExit("--month 必须是 YYYY-MM")
    if not 30 <= args.window_days <= 60:
        raise SystemExit("--window-days 必须在 30–60 之间")
    inbox_root = ROOT / "data" / "state" / "claim_inbox"
    output = args.output or ROOT / "reports" / "monthly" / f"{args.month}-claim-review.md"
    rows, window_start, window_end = load_review_window(
        inbox_root, args.month, args.window_days)
    claims = bd.load_yaml("claims.yaml").get("claims", [])
    text = render_review(args.month, rows, claims,
                         window_start=window_start, window_end=window_end)
    changed = atomic_write_if_changed(output, text)
    print(f"月度复盘{'已更新' if changed else '未变化'}：{output}（{len(rows)} 条信号）")


if __name__ == "__main__":
    main()
