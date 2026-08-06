# -*- coding: utf-8 -*-
"""Replay two topic configurations over historical raw observations.

The default comparison is the committed ``HEAD:config/topics.yaml`` versus the
working-tree file.  This is a read-only analysis of recall and section routing;
it never edits the claim ledger or the daily brief.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import types
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import yaml

import build_digest
from state_io import atomic_write_if_changed, exclusive_lock


ROOT = Path(__file__).resolve().parent.parent


def load_raw_window(root: Path, end_day: str, days: int) -> list[tuple[str, list[dict]]]:
    """Load available raw snapshots in the inclusive calendar window."""
    end = date.fromisoformat(end_day)
    cutoff = end - timedelta(days=max(1, days) - 1)
    raw_root = root / "data" / "raw"
    observations = []
    if not raw_root.exists():
        return observations
    for folder in sorted(raw_root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            sample_day = date.fromisoformat(folder.name)
        except ValueError:
            continue
        if sample_day < cutoff or sample_day > end:
            continue
        payloads = []
        for path in sorted(folder.glob("*.json")):
            if path.name.startswith("_"):
                continue
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        observations.append((sample_day.isoformat(), payloads))
    return observations


def _classified(payloads: list[dict], topics: dict, sample_day: str,
                window_days: int, classifier) -> dict[tuple[str, str], dict]:
    sectioned, _ = classifier(payloads, topics, sample_day, window_days)
    indexed = {}
    for section, items in sectioned.items():
        for item in items:
            source_item_id = str(item["source_item_id"])
            source_id = source_item_id.rsplit(":", 1)[0]
            indexed[(sample_day, source_item_id)] = {
                "day": sample_day,
                "source_id": source_id,
                "source": item["source"],
                "title": item["title"],
                "url": item["url"],
                "tier": item["tier"],
                "section": section,
                "source_item_id": source_item_id,
            }
    return indexed


def compare_observations(observations: list[tuple[str, list[dict]]],
                         baseline_topics: dict, candidate_topics: dict,
                         window_days: int = 3, *,
                         baseline_classifier=None,
                         candidate_classifier=None) -> dict:
    baseline_classifier = baseline_classifier or build_digest.classify
    candidate_classifier = candidate_classifier or build_digest.classify
    baseline = {}
    candidate = {}
    for sample_day, payloads in observations:
        baseline.update(_classified(
            payloads, baseline_topics, sample_day, window_days, baseline_classifier))
        candidate.update(_classified(
            payloads, candidate_topics, sample_day, window_days, candidate_classifier))

    added = []
    removed = []
    moves = []
    for key in sorted(set(baseline) | set(candidate)):
        old = baseline.get(key)
        new = candidate.get(key)
        if old is None:
            added.append(dict(new, candidate_section=new["section"]))
        elif new is None:
            removed.append(dict(old, baseline_section=old["section"]))
        elif old["section"] != new["section"]:
            moves.append(dict(
                new,
                baseline_section=old["section"],
                candidate_section=new["section"],
            ))

    baseline_sections = Counter(row["section"] for row in baseline.values())
    candidate_sections = Counter(row["section"] for row in candidate.values())
    source_counts = defaultdict(lambda: {"baseline": 0, "candidate": 0})
    for row in baseline.values():
        source_counts[row["source_id"]]["baseline"] += 1
    for row in candidate.values():
        source_counts[row["source_id"]]["candidate"] += 1

    return {
        "summary": {
            "baseline_kept": len(baseline),
            "candidate_kept": len(candidate),
            "added": len(added),
            "removed": len(removed),
            "section_moves": len(moves),
        },
        "added": added,
        "removed": removed,
        "section_moves": moves,
        "per_section": {
            section: {
                "baseline": baseline_sections.get(section, 0),
                "candidate": candidate_sections.get(section, 0),
                "delta": candidate_sections.get(section, 0) - baseline_sections.get(section, 0),
            }
            for section in build_digest.SECTION_ORDER
        },
        "per_source": {
            source: {
                **counts,
                "delta": counts["candidate"] - counts["baseline"],
            }
            for source, counts in sorted(source_counts.items())
        },
        "sample_days": [day for day, _ in observations],
    }


def _sample_lines(rows: list[dict], *, move: bool = False, limit: int = 50) -> list[str]:
    if not rows:
        return ["- （无）"]
    lines = []
    for row in rows[:limit]:
        route = (
            f" `{row['baseline_section']}` → `{row['candidate_section']}`"
            if move else ""
        )
        lines.append(
            f"- `{row['day']}` `{row['source_id']}`{route} "
            f"[{row['title'][:120]}]({row['url']})"
        )
    if len(rows) > limit:
        lines.append(f"- ……另有 {len(rows) - limit} 条未展开")
    return lines


def render_report(result: dict, *, end_day: str, days: int,
                  baseline_label: str) -> str:
    summary = result["summary"]
    lines = [
        f"# 分类规则历史回放 — {end_day}",
        "",
        f"- 基线：`{baseline_label}` 的分类器与 topics 配置",
        "- 候选：工作区分类器与 `config/topics.yaml`",
        f"- 请求窗口：最近 {days} 个自然日；实际样本日：{len(result['sample_days'])}",
        f"- 基线入围 {summary['baseline_kept']}；候选入围 {summary['candidate_kept']}",
        f"- 新增 {summary['added']}；删除 {summary['removed']}；栏目迁移 {summary['section_moves']}",
        "- 边界：本报告只比较召回与归栏变化；不判断条目真假，不把多次观察当作独立证据，"
        "不修改日报、悬案或状态。",
        "",
        "## 逐栏目变化",
        "",
        "| 栏目 | 基线 | 候选 | Δ |",
        "| --- | ---: | ---: | ---: |",
    ]
    for section, row in result["per_section"].items():
        lines.append(
            f"| {section} | {row['baseline']} | {row['candidate']} | {row['delta']:+d} |"
        )
    lines.extend([
        "", "## 逐信源变化", "",
        "| source_id | 基线 | 候选 | Δ |",
        "| --- | ---: | ---: | ---: |",
    ])
    for source, row in result["per_source"].items():
        lines.append(
            f"| `{source}` | {row['baseline']} | {row['candidate']} | {row['delta']:+d} |"
        )
    lines.extend(["", "## 新增入围", ""])
    lines.extend(_sample_lines(result["added"]))
    lines.extend(["", "## 被删除", ""])
    lines.extend(_sample_lines(result["removed"]))
    lines.extend(["", "## 栏目迁移", ""])
    lines.extend(_sample_lines(result["section_moves"], move=True))
    lines.append("")
    return "\n".join(lines)


def topics_at_revision(root: Path, revision: str) -> dict:
    proc = subprocess.run(
        ["git", "show", f"{revision}:config/topics.yaml"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return yaml.safe_load(proc.stdout.decode("utf-8")) or {}


def classifier_at_revision(root: Path, revision: str):
    """Load build_digest.classify from a Git revision without running its CLI."""
    proc = subprocess.run(
        ["git", "show", f"{revision}:scripts/build_digest.py"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    module = types.ModuleType(f"_build_digest_{revision.replace('/', '_')}")
    module.__file__ = str(root / "scripts" / "build_digest.py")
    source = proc.stdout.decode("utf-8")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module.classify


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--days", type=int, default=14,
                        help="回放最近多少个自然日（默认 14）")
    parser.add_argument("--window", type=int, default=3,
                        help="沿用 build_digest 的条目时间窗（默认 3）")
    parser.add_argument("--baseline-rev", default="HEAD")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        parser.error("--date 必须是 YYYY-MM-DD")
    if args.days < 1 or args.window < 1:
        parser.error("--days 与 --window 必须大于 0")

    observations = load_raw_window(ROOT, args.date, args.days)
    if not observations:
        print(f"没有找到截至 {args.date} 的 raw 样本", file=sys.stderr)
        return 1
    try:
        baseline = topics_at_revision(ROOT, args.baseline_rev)
        baseline_classifier = classifier_at_revision(ROOT, args.baseline_rev)
    except subprocess.CalledProcessError as exc:
        print(f"无法读取基线 {args.baseline_rev!r}: {exc.stderr.decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    candidate = yaml.safe_load(
        (ROOT / "config" / "topics.yaml").read_text(encoding="utf-8")) or {}
    result = compare_observations(
        observations, baseline, candidate, args.window,
        baseline_classifier=baseline_classifier,
        candidate_classifier=build_digest.classify,
    )
    report = render_report(
        result, end_day=args.date, days=args.days, baseline_label=args.baseline_rev)
    output = (Path(args.output) if args.output else
              ROOT / "reports" / "classification-backtest" / f"{args.date}.md")
    if not output.is_absolute():
        output = ROOT / output
    lock = ROOT / "data" / "state" / "classification-backtest.lock"
    with exclusive_lock(lock):
        changed = atomic_write_if_changed(output, report)
    print(f"分类回放：{output}（{'已更新' if changed else '无变化'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
