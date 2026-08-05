# -*- coding: utf-8 -*-
"""Render rebuildable per-claim evidence dossiers without touching final reports."""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import build_digest as bd
from state_io import atomic_write_if_changed, exclusive_lock

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "reports" / "dossiers"
TYPE_ORDER = ("controlled", "n1-user", "vendor", "index", "forum", "report")
TYPE_LABEL = {
    "controlled": "受控/可复核测试",
    "n1-user": "个体一手实测（n=1）",
    "vendor": "厂商口径",
    "index": "聚合指数",
    "forum": "论坛/社区材料",
    "report": "媒体或研究报告",
}
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,99}$")
STANCE_ORDER = ("support", "counter", "confounder", "neutral", "legacy-unspecified")
STANCE_LABEL = {
    "support": "支持",
    "counter": "反证/削弱",
    "confounder": "混杂/替代解释",
    "neutral": "中立背景",
    "legacy-unspecified": "旧记录未标注",
}


def _day(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def render_dossier(claim: dict) -> str:
    groups = defaultdict(list)
    evidence = claim.get("evidence") or []
    stance_counts = defaultdict(int)
    for ev in evidence:
        groups[ev.get("type", "unknown")].append(ev)
        stance_counts[ev.get("stance", "legacy-unspecified")] += 1
    order = list(TYPE_ORDER) + sorted(set(groups) - set(TYPE_ORDER))
    stance_summary = "；".join(
        f"{STANCE_LABEL[stance]} {stance_counts.get(stance, 0)}"
        for stance in STANCE_ORDER if stance_counts.get(stance, 0)) or "无"
    lines = [f"# 悬案证据档案：{claim.get('claim', claim.get('id', ''))}", "",
             "> **自动派生档案，不是 L2 专题裁决报告。** `config/claims.yaml` 是唯一权威来源；"
             "本文件不自动立案、不改判，也不覆盖任何正式报告。", "",
             f"- Claim ID：`{claim.get('id', '')}`",
             f"- 当前状态：`{claim.get('status', 'open')}`",
             f"- 建立日期：{_day(claim.get('opened')) or '—'}",
             f"- 证据记录总数（不代表支持强度）：{len(evidence)}",
             f"- 立场统计：{stance_summary}", "",
             "## 当前观察点", "", claim.get("watch", "—"), "",
             "## 分层证据", ""]
    if not groups:
        lines += ["（暂无证据）", ""]
    for ev_type in order:
        entries = groups.get(ev_type)
        if not entries:
            continue
        lines += [f"### {TYPE_LABEL.get(ev_type, ev_type)}（{len(entries)}）", ""]
        for ev in sorted(entries, key=lambda x: (_day(x.get("date")), str(x.get("src", "")))):
            src = str(ev.get("src", "来源"))
            url = bd._linkify(ev.get("link"))
            source = f"[{src}]({url})" if url else f"{src}（不可点本地材料或待补原始出处）"
            lines.append(f"- **{_day(ev.get('date')) or '日期不明'}｜{source}**")
            stance = ev.get("stance", "legacy-unspecified")
            lines.append(f"  - 立场：**{STANCE_LABEL.get(stance, stance)}**")
            lines.append(f"  - 判断：{ev.get('verdict', '')}")
            if ev.get("source_item_id"):
                lines.append(f"  - 抓取身份：`{ev['source_item_id']}` / "
                             f"`{str(ev.get('snapshot_hash', ''))[:12]}…`")
        lines.append("")
    lines += ["## 使用边界", "",
              "- 相同来源的多条记录不自动算作独立复现。",
              "- 支持、反证、混杂与中立记录分别计数；记录变多不等于主张变强。",
              "- `legacy-unspecified` 表示旧账本尚未迁移，派生档案不会从措辞猜测其立场。",
              "- 厂商口径只能证明厂商声明；聚合指数不能替代生产环境可靠性。",
              "- n=1、受控测试和报道材料必须保持分层，不能因为被写进同一档案就等权。",
              "- 状态历史与改判理由保留在 `config/claims.yaml` 注释中；本派生文件不重写这些注释。", ""]
    return "\n".join(lines)


def render_index(generated: list[dict], skipped: list[dict]) -> str:
    lines = ["# 悬案证据档案索引", "",
             "> 此目录全部为可重建派生视图；正式专题报告位于 `reports/` 根目录。", "",
             "## 已生成", ""]
    for claim in generated:
        lines.append(f"- [{claim['id']}](./{claim['id']}.md) — {claim.get('claim', '')}"
                     f"（{claim.get('status', 'open')}）")
    if not generated:
        lines.append("（无）")
    lines += ["", "## 因职业正典边界跳过", ""]
    for claim in skipped:
        lines.append(f"- `{claim['id']}` — 只可建议复查外部正典，不进行无人值守派生")
    if not skipped:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines)


def build_dossiers(root: Path, claims: list[dict]) -> tuple[int, int, int]:
    output_dir = root / "reports" / "dossiers"
    lock = root / "data" / "state" / "locks" / "report-dossiers.lock"
    with exclusive_lock(lock):
        generated = []
        skipped = []
        changed = 0
        for claim in claims:
            claim_id = str(claim.get("id", ""))
            if not SAFE_ID.match(claim_id):
                raise ValueError(f"不安全的 claim id，拒绝生成文件名：{claim_id!r}")
            if "ledger_ref" in claim:
                skipped.append(claim)
                continue
            generated.append(claim)
            if atomic_write_if_changed(output_dir / f"{claim_id}.md", render_dossier(claim)):
                changed += 1
        if atomic_write_if_changed(output_dir / "index.md", render_index(generated, skipped)):
            changed += 1
    return changed, len(generated), len(skipped)


def main() -> None:
    argparse.ArgumentParser().parse_args()
    claims = bd.load_yaml("claims.yaml").get("claims", [])
    changed, generated, skipped = build_dossiers(ROOT, claims)
    print(f"dossier：{generated} 条可生成，跳过职业边界 {skipped} 条，实际改写 {changed} 个文件")


if __name__ == "__main__":
    main()
