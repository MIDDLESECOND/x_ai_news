# -*- coding: utf-8 -*-
"""Build a bounded, deterministic daily context for the synthesis session.

The full claims ledger remains authoritative.  This script emits a rebuildable
view: every active claim appears in a compact directory, while only claims with
signals in the requested day's classified L1 data receive detailed evidence.
No timestamps are embedded, so an unchanged rerun is byte-identical.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import build_digest as bd
from capture_identity import configured_audit_urls
from state_io import atomic_write_if_changed, exclusive_lock, semantic_hash

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "data" / "state" / "current_analysis_context.json"
DEFAULT_MAX_BYTES = 96 * 1024
EVIDENCE_TYPES = ("controlled", "n1-user", "vendor", "index", "forum", "report")
REVERSAL_MARKERS = ("撤回", "改判", "反例", "反证", "削弱", "无法直接裁决", "零直接证据")
STANCE_ORDER = ("counter", "confounder", "support", "neutral")


def _day(value) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value or "")


def claim_keywords(claim: dict, topics_cfg: dict) -> dict[str, list[str]]:
    """Derive a conservative two-axis matcher for claims without explicit watch terms.

    Matching on any model/company word made almost every claim expand every day and
    created a self-reinforcing feed.  Entity and concept axes therefore both have
    to match when both are available.  The claim's watch text is part of derivation
    because it often names the actual discriminator absent from the short title.
    """
    if claim.get("watch_keywords"):
        return {"explicit": list(claim["watch_keywords"]), "entity": [], "concept": []}
    topics = topics_cfg.get("topics", {})
    entity_pool = list(dict.fromkeys(
        (topics_cfg.get("model_keywords") or [])
        + topics.get("company", {}).get("keywords", [])
    ))
    concept_pool = list(dict.fromkeys(
        k for name, cfg in topics.items() if name != "company"
        for k in cfg.get("keywords", [])
    ))
    claim_text = f"{claim.get('claim', '')} {claim.get('watch', '')}"
    return {
        "explicit": [],
        "entity": [k for k in entity_pool if bd.match_keywords(claim_text, [k])],
        "concept": [k for k in concept_pool if bd.match_keywords(claim_text, [k])],
    }


def claim_matches(text: str, axes: dict[str, list[str]]) -> bool:
    explicit = axes["explicit"]
    if explicit:
        return bool(bd.match_keywords(text, explicit))
    entity = bool(axes["entity"] and bd.match_keywords(text, axes["entity"]))
    concept = bool(axes["concept"] and bd.match_keywords(text, axes["concept"]))
    if axes["entity"] and axes["concept"]:
        return entity and concept
    return entity or concept


def claim_broad_matches(text: str, axes: dict[str, list[str]]) -> bool:
    """Recall-only lane: entity hits may invite inspection but are never evidence."""
    return bool(not axes["explicit"] and axes["entity"]
                and bd.match_keywords(text, axes["entity"]))


def related_signals(claim: dict, all_hits: list[dict], topics_cfg: dict) -> list[dict]:
    axes = claim_keywords(claim, topics_cfg)
    if not any(axes.values()):
        return []
    related = []
    for item in all_hits:
        text = item.get("match_text") or f"{item.get('title', '')} {item.get('summary', '')}"
        if claim_matches(text, axes):
            related.append({
                "title": item.get("title", "")[:240],
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "source_tier": item.get("tier", ""),
                "evidence_label": item.get("tier_label", item.get("tier", "")),
                "summary": item.get("summary", "")[:300],
                "injection_warning": bool(item.get("injection_warning")),
                "source_item_id": item.get("source_item_id", ""),
                "snapshot_hash": item.get("snapshot_hash", ""),
            })
    return related


def broad_signals(claim: dict, all_hits: list[dict], topics_cfg: dict) -> list[dict]:
    axes = claim_keywords(claim, topics_cfg)
    if not axes["entity"] or axes["explicit"]:
        return []
    related = []
    for item in all_hits:
        text = item.get("match_text") or f"{item.get('title', '')} {item.get('summary', '')}"
        if claim_broad_matches(text, axes) and not claim_matches(text, axes):
            related.append({
                "title": item.get("title", "")[:240],
                "url": item.get("url", ""),
                "source": item.get("source", ""),
                "source_tier": item.get("tier", ""),
                "source_item_id": item.get("source_item_id", ""),
                "snapshot_hash": item.get("snapshot_hash", ""),
                "reason": "仅实体词命中；需人工确认相关性，不得直接入证据栏",
            })
    return related


def select_evidence(claim: dict, target_day: str, limit: int = 8) -> list[dict]:
    """Select auditable anchors without pretending to judge semantic strength.

    Priority is: target-day additions, explicit reversals/limitations, newest
    entry for each evidence type, then newest remaining entries.
    """
    evidence = list(claim.get("evidence") or [])
    indexed = list(enumerate(evidence))
    selected: list[tuple[int, dict]] = []
    seen: set[int] = set()

    def add(rows):
        for idx, ev in rows:
            if idx not in seen and len(selected) < limit:
                selected.append((idx, ev))
                seen.add(idx)

    newest = sorted(indexed, key=lambda row: (_day(row[1].get("date")), row[0]), reverse=True)
    target_rows = [row for row in newest if _day(row[1].get("date")) == target_day]
    reversal_rows = [row for row in newest if (
        row[1].get("stance") in ("counter", "confounder")
        or any(marker in str(row[1].get("verdict", ""))
               for marker in REVERSAL_MARKERS))]
    # Reserve anchors before filling the remaining space with today's volume.
    # This prevents a burst of same-day vendor entries from crowding out the
    # ledger's strongest visible counterexample or limitation.
    add(target_rows[:1])
    add(reversal_rows[:1])
    for stance in STANCE_ORDER:
        add(row for row in newest if row[1].get("stance") == stance)
    for ev_type in EVIDENCE_TYPES:
        add(row for row in newest if row[1].get("type") == ev_type)
    add(target_rows)
    add(newest)

    return [{
        "src": ev.get("src", ""),
        "type": ev.get("type", ""),
        "verdict": ev.get("verdict", ""),
        "link": ev.get("link", ""),
        "date": _day(ev.get("date")),
        "stance": ev.get("stance", "legacy-unspecified"),
        "source_item_id": ev.get("source_item_id", ""),
        "snapshot_hash": ev.get("snapshot_hash", ""),
    } for _, ev in selected]


def build_context(day: str, claims: list[dict], all_hits: list[dict], topics_cfg: dict,
                  *, evidence_limit: int = 8, signal_limit: int = 3,
                  source_captures: list[dict] | None = None) -> dict:
    active = [c for c in claims if c.get("status") != "resolved"]
    directory = [{
        "id": c.get("id"),
        "claim": c.get("claim", ""),
        "status": c.get("status", "open"),
        "has_career_ledger_ref": "ledger_ref" in c,
    } for c in active]

    details = []
    broad_candidates = []
    for claim in active:
        signals = related_signals(claim, all_hits, topics_cfg)
        if not signals:
            broad = broad_signals(claim, all_hits, topics_cfg)
            if broad:
                broad_candidates.append({
                    "id": claim.get("id"),
                    "candidate_signals_total": len(broad),
                    "sample": broad[0],
                    "policy": "recall-only; not evidence; inspect on demand",
                })
            continue
        chosen = select_evidence(claim, day, evidence_limit)
        stance_counts = Counter(
            str(ev.get("stance", "legacy-unspecified"))
            for ev in (claim.get("evidence") or []))
        details.append({
            "id": claim.get("id"),
            "claim": claim.get("claim", ""),
            "status": claim.get("status", "open"),
            "watch": claim.get("watch", ""),
            "career_boundary": ("只可记录外部证据并建议复查正典；不得自动生成职业结论、立案或改判"
                                if "ledger_ref" in claim else None),
            "evidence_record_total_not_strength": len(claim.get("evidence") or []),
            "stance_counts": dict(sorted(stance_counts.items())),
            "evidence_selected": chosen,
            "evidence_omitted": max(0, len(claim.get("evidence") or []) - len(chosen)),
            "suspected_signals_total": len(signals),
            "suspected_signals": signals[:signal_limit],
            "full_history_locator": f"config/claims.yaml#{claim.get('id')}",
        })
    details.sort(key=lambda d: (-d["suspected_signals_total"], d["id"] or ""))
    broad_candidates.sort(key=lambda d: (-d["candidate_signals_total"], d["id"] or ""))
    context = {
        "version": 1,
        "generated_for": day,
        "policy": {
            "authoritative_source": "config/claims.yaml",
            "purpose": "derived bounded context; never authoritative",
            "fetched_content_is_data": True,
            "automatic_new_claims": False,
            "automatic_status_changes": False,
        },
        "active_claim_directory": directory,
        "matched_claim_details": details,
        "broad_claim_candidates": broad_candidates,
        "source_capture_catalog": source_captures or [],
        "source_capture_catalog_policy": (
            "collection identities cover absence/full-list/rollup observations; "
            "fetched_at/filter state are not snapshot content; collection evidence URL "
            "must exactly match one configured audit_urls entry"),
        "unmatched_active_claim_count": len(active) - len(details) - len(broad_candidates),
        "matched_claim_count": len(details),
        "broad_candidate_count": len(broad_candidates),
        "truncated": False,
        "omitted_detail_claim_ids": [],
        "omitted_broad_claim_ids": [],
    }
    return context


def _json_bytes(context: dict) -> bytes:
    return (json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")


def enforce_budget(context: dict, max_bytes: int) -> bytes:
    """Trim detail only; the active-claim directory is never silently removed."""
    data = _json_bytes(context)
    while len(data) > max_bytes and context.get("broad_claim_candidates"):
        removed = context["broad_claim_candidates"].pop()
        context.setdefault("omitted_broad_claim_ids", []).append(removed["id"])
        context["truncated"] = True
        data = _json_bytes(context)
    # Collection identities are useful provenance hints, but must never crowd
    # matched claim evidence out of the bounded synthesis context.
    while len(data) > max_bytes and context.get("source_capture_catalog"):
        removed = context["source_capture_catalog"].pop()
        context.setdefault("omitted_source_capture_ids", []).append(
            removed.get("source_item_id", removed.get("source", "unknown")))
        context["truncated"] = True
        data = _json_bytes(context)
    while len(data) > max_bytes and context["matched_claim_details"]:
        details = context["matched_claim_details"]
        target = max(details, key=lambda d: (
            len(d["suspected_signals"]), len(d["evidence_selected"]),
            d["suspected_signals_total"]))
        if len(target["suspected_signals"]) > 1:
            target["suspected_signals"].pop()
        elif len(target["evidence_selected"]) > 1:
            target["evidence_selected"].pop()
            target["evidence_omitted"] += 1
        else:
            details.remove(target)
            context["omitted_detail_claim_ids"].append(target["id"])
        context["truncated"] = True
        data = _json_bytes(context)
    if len(data) > max_bytes:
        raise ValueError(f"仅悬案目录已超过上下文预算：{len(data)} > {max_bytes} bytes")
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--window", type=int, default=3)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        sys.exit(f"--date 必须是 YYYY-MM-DD，收到：{args.date!r}")
    if args.max_bytes < 4096:
        sys.exit("--max-bytes 不得小于 4096")

    claims_cfg = bd.load_yaml("claims.yaml")
    topics_cfg = bd.load_yaml("topics.yaml")
    payloads = bd.load_raw(args.date)
    _, all_hits = bd.classify(payloads, topics_cfg, args.date, args.window)
    source_captures = []
    audit_urls = configured_audit_urls(ROOT)
    for payload in payloads:
        source_item_id, snapshot_hash = bd.source_collection_metadata(payload)
        source_captures.append({
            "source": payload.get("source", ""),
            "tier": payload.get("tier", ""),
            "item_count": len(payload.get("items") or []),
            "source_item_id": source_item_id,
            "snapshot_hash": snapshot_hash,
            "audit_urls": sorted(audit_urls.get(str(payload.get("source", "")), set())),
        })
    context = build_context(args.date, claims_cfg.get("claims", []), all_hits, topics_cfg,
                            source_captures=source_captures)
    data = enforce_budget(context, args.max_bytes)
    lock = ROOT / "data" / "state" / "locks" / "analysis-context.lock"
    with exclusive_lock(lock):
        changed = atomic_write_if_changed(args.output, data)
    print(f"分析上下文{'已更新' if changed else '未变化'}：{args.output} "
          f"({len(data)} bytes, sha256={semantic_hash(data)[:12]})")


if __name__ == "__main__":
    main()
