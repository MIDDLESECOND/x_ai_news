# -*- coding: utf-8 -*-
"""独立的 Reddit 信源影子审计；不写正式 L1 raw，也不进入日报。

默认抓取 config/reddit_audit.yaml 中的社区，并把当日快照写入
data/reddit_audit/raw/YYYY-MM-DD/，随后生成私有报告
reports/reddit-source-audit/YYYY-MM-DD.md。

用法：
  python scripts/audit_reddit_sources.py
  python scripts/audit_reddit_sources.py --report-only
  python scripts/audit_reddit_sources.py --only MachineLearning,mlops
"""
import argparse
import html
import json
import math
import re
import shutil
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from fetch_l1 import fetch_rss
from state_io import atomic_write_if_changed, exclusive_lock

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "reddit_audit.yaml"
AUDIT_RAW = ROOT / "data" / "reddit_audit" / "raw"
REPORT_DIR = ROOT / "reports" / "reddit-source-audit"
AUDIT_LOCK = ROOT / "data" / "state" / "locks" / "reddit-source-audit.lock"
URL_RE = re.compile(r"https?://[^\s<>'\"&]+", re.IGNORECASE)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+.-]*", re.IGNORECASE)
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source"}
SOCIAL_OR_AGGREGATOR = {
    "reddit.com", "www.reddit.com", "redd.it", "x.com", "twitter.com", "xcancel.com",
    "youtube.com", "www.youtube.com", "youtu.be", "medium.com", "news.ycombinator.com",
}

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def atomic_write_json(path, value):
    atomic_write_if_changed(path, json.dumps(value, ensure_ascii=False, indent=1))


def atomic_write_text(path, value):
    atomic_write_if_changed(path, value)


def subreddit_id(name):
    return "r_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def normalize_url(value):
    value = html.unescape((value or "").strip()).rstrip(".,);]}")
    if not value.startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in TRACKING_PARAMS])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def extract_urls(text):
    return sorted({u for u in (normalize_url(m) for m in URL_RE.findall(html.unescape(text or ""))) if u})


def host_matches(host, configured):
    host = host.lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in configured)


def direct_evidence_urls(item, configured_domains):
    text_values = [item.get("summary", ""), item.get("external_url", "")]
    text_values.extend(item.get("external_urls") or [])
    urls = set(extract_urls(" ".join(str(value) for value in text_values)))
    result = []
    for url in urls:
        host = (urlsplit(url).hostname or "").lower()
        if host in SOCIAL_OR_AGGREGATOR or host.endswith(".reddit.com"):
            continue
        if host_matches(host, configured_domains):
            result.append(url)
    return sorted(result)


def marker_groups(text, groups):
    lowered = text.lower()
    return sorted(name for name, markers in groups.items()
                  if any(marker.lower() in lowered for marker in markers))


def item_features(item, audit_cfg):
    text = f"{item.get('title', '')} {html.unescape(item.get('summary', ''))}"
    groups = marker_groups(text, audit_cfg.get("technical_marker_groups", {}))
    direct = direct_evidence_urls(item, audit_cfg.get("direct_evidence_domains", []))
    noisy = any(marker.lower() in text.lower() for marker in audit_cfg.get("noise_markers", []))
    technical = len(groups) >= 2 or (bool(direct) and bool(groups))
    concrete = bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:ms|gb|mb|%|tok(?:en)?s?/s|x)\b", text, re.I))
    evidence = bool(direct) or (concrete and technical)
    return {
        "technical_proxy": technical,
        "evidence_proxy": evidence,
        "direct_evidence": bool(direct),
        "direct_urls": direct,
        "noise_proxy": noisy,
        "marker_groups": groups,
    }


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def collect_one(entry, audit_cfg, max_items):
    name = entry["name"]
    src = {
        "id": subreddit_id(name),
        "name": f"r/{name}",
        "url": f"https://www.reddit.com/r/{name}/new/.rss",
    }
    items = fetch_rss(src)[:max_items]
    for item in items:
        preserved = item.get("external_urls") or extract_urls(item.get("summary", ""))
        item["external_urls"] = [u for u in preserved
                                 if "reddit.com" not in (urlsplit(u).hostname or "")]
        item["audit_features"] = item_features(item, audit_cfg)
    return {
        "source": src["id"],
        "subreddit": name,
        "category": entry.get("category", "uncategorized"),
        "role": entry.get("role", "candidate"),
        "status": "ok",
        "feed_url": src["url"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }


def rotating_batch(entries, day, limit):
    """Select one deterministic non-overlapping batch for a calendar day."""
    entries = list(entries)
    if not entries:
        return []
    limit = max(1, min(int(limit), len(entries)))
    batches = math.ceil(len(entries) / limit)
    batch_index = date.fromisoformat(day).toordinal() % batches
    start = batch_index * limit
    return entries[start:start + limit]


def collect(config, day, only=None, delay=None, raw_root=AUDIT_RAW):
    audit_cfg = config["audit"]
    candidates = [s for s in config["subreddits"]
                  if not only or s["name"].lower() in only]
    batch_limit = 1
    selected = (candidates[:batch_limit] if only
                else rotating_batch(candidates, day, batch_limit))
    day_dir = raw_root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    configured_wait = max(0.0, float(audit_cfg.get("request_delay_seconds", 300)))
    wait = configured_wait if delay is None else max(configured_wait, delay)
    daily_limit = 1
    max_items = audit_cfg.get("max_items_per_subreddit", 25)
    log_path = day_dir / "_audit_log.json"
    try:
        log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    except json.JSONDecodeError:
        log = {}
    log = {
        "date": day,
        "started_at": log.get("started_at", datetime.now(timezone.utc).isoformat()),
        "sources": log.get("sources", {}),
        "policy": {
            "selection": "explicit-capped" if only else "daily-rotation",
            "max_subreddits_per_run": batch_limit,
            "max_requests_per_day": daily_limit,
            "request_delay_seconds": wait,
        },
    }
    # 进程可能在逐社区原子落盘后、写汇总日志前被终止；续跑时由快照恢复成功项。
    for path in day_dir.glob("r_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("status", "ok") == "ok":
            # 当天已有成功快照时，后续瞬时 429/网络失败不能把日级结果降格为失败。
            log["sources"][payload.get("source", path.stem)] = {
                "status": "ok", "items": len(payload.get("items", []))}
    attempted_today = set(log["sources"])
    remaining = max(0, daily_limit - len(attempted_today))
    selected = [entry for entry in selected
                if subreddit_id(entry["name"]) not in attempted_today][:remaining]
    log["selected_sources"] = [subreddit_id(entry["name"]) for entry in selected]
    for index, entry in enumerate(selected):
        if index and wait:
            time.sleep(wait)
        sid = subreddit_id(entry["name"])
        try:
            payload = collect_one(entry, audit_cfg, max_items)
            atomic_write_json(day_dir / f"{sid}.json", payload)
            log["sources"][sid] = {"status": "ok", "items": len(payload["items"])}
            print(f"[ok]   r/{entry['name']}: {len(payload['items'])} items", flush=True)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            existing_path = day_dir / f"{sid}.json"
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("status", "ok") == "ok" and existing.get("items") is not None:
                log["sources"][sid] = {
                    "status": "ok", "items": len(existing.get("items", [])), "retry_error": error}
            else:
                atomic_write_json(existing_path, {
                    "source": sid,
                    "subreddit": entry["name"],
                    "category": entry.get("category", "uncategorized"),
                    "role": entry.get("role", "candidate"),
                    "status": "error",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "error": error,
                    "items": [],
                })
                log["sources"][sid] = {"status": "error", "error": error}
            print(f"[fail] r/{entry['name']}: {type(exc).__name__}: {exc}", flush=True)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(log_path, log)
    prune_audit(raw_root, day, audit_cfg.get("keep_days", 21))
    return log


def prune_audit(raw_root, day, keep_days):
    cutoff = date.fromisoformat(day) - timedelta(days=keep_days)
    if not raw_root.exists():
        return
    for path in raw_root.iterdir():
        if not path.is_dir():
            continue
        try:
            old = date.fromisoformat(path.name) < cutoff
        except ValueError:
            old = False
        if old:
            shutil.rmtree(path)


def title_key(title):
    tokens = [t for t in WORD_RE.findall((title or "").lower()) if len(t) > 2]
    return " ".join(tokens[:20])


def item_signal_keys(item, direct_domains):
    direct = direct_evidence_urls(item, direct_domains)
    if direct:
        return ["url:" + u for u in direct]
    key = title_key(item.get("title", ""))
    return ["title:" + key] if key else []


def load_audit_records(raw_root, end_day, duration_days, sample_days=None):
    start = date.fromisoformat(end_day) - timedelta(days=duration_days - 1)
    selected = set(sample_days) if sample_days is not None else None
    records = []
    if not raw_root.exists():
        return records
    for folder in sorted(raw_root.iterdir()):
        try:
            sample_day = date.fromisoformat(folder.name)
        except (ValueError, AttributeError):
            continue
        if selected is not None:
            if folder.name not in selected:
                continue
        elif not (start <= sample_day <= date.fromisoformat(end_day)):
            continue
        for path in folder.glob("r_*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            payload["sample_day"] = folder.name
            records.append(payload)
    return records


def completed_attempt_days(raw_root, end_day, required_sources):
    """返回真正完成整池尝试的日期；中途终止或 --only 局部运行不计入 14 天。"""
    completed = []
    if not raw_root.exists():
        return completed
    end = date.fromisoformat(end_day)
    for folder in sorted(raw_root.iterdir()):
        try:
            sample_day = date.fromisoformat(folder.name)
        except (ValueError, AttributeError):
            continue
        if sample_day > end:
            continue
        log_path = folder / "_audit_log.json"
        try:
            log = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        actual = set(log.get("sources", {}))
        complete = (len(actual) >= required_sources if isinstance(required_sources, int)
                    else set(required_sources).issubset(actual))
        if log.get("finished_at") and complete:
            completed.append(folder.name)
    return completed


def audit_sample_days(raw_root, end_day, duration_days, required_sources):
    """选最近 N 个完整采样日；未满窗口时可附带当天的局部快照供检查。"""
    completed = completed_attempt_days(raw_root, end_day, required_sources)
    selected = completed[-duration_days:]
    current = raw_root / end_day
    if len(selected) < duration_days and current.is_dir() and end_day not in selected:
        selected.append(end_day)
    return selected


def existing_index(existing_raw, end_day, duration_days):
    """索引当前非 Reddit L1；避免用已在正式池的同一帖子制造零领先时间。"""
    start = date.fromisoformat(end_day) - timedelta(days=duration_days + 3)
    index = defaultdict(list)
    if not existing_raw.exists():
        return index
    for folder in existing_raw.iterdir():
        try:
            folder_day = date.fromisoformat(folder.name)
        except (ValueError, AttributeError):
            continue
        if not (start <= folder_day <= date.fromisoformat(end_day)):
            continue
        for path in folder.glob("*.json"):
            if path.name.startswith("_") or path.stem.startswith("r_"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in payload.get("items", []):
                when = parse_dt(item.get("published")) or parse_dt(payload.get("fetched_at"))
                if not when:
                    continue
                values = [item.get(k, "") for k in ("url", "external_url", "summary")]
                values.extend(item.get("external_urls") or [])
                urls = extract_urls(" ".join(str(value) for value in values))
                for url in urls:
                    index["url:" + url].append(when)
                key = title_key(item.get("title", ""))
                if key:
                    index["title:" + key].append(when)
    return index


def median(values, default=0.0):
    return float(statistics.median(values)) if values else default


def score_rows(config, records, existing):
    audit_cfg = config["audit"]
    direct_domains = audit_cfg.get("direct_evidence_domains", [])
    duration = audit_cfg.get("duration_days", 14)
    by_sub = defaultdict(list)
    meta = {}
    signal_times = defaultdict(list)
    signal_window = timedelta(days=audit_cfg.get("signal_match_window_days", 30))

    for payload in records:
        name = payload["subreddit"]
        meta[name] = payload
        fetched = parse_dt(payload.get("fetched_at"))
        for item in payload.get("items", []):
            when = parse_dt(item.get("published")) or fetched
            for key in item_signal_keys(item, direct_domains):
                if when:
                    signal_times[key].append((name.lower(), when))

    def nearby_signal_sources(key, current_time):
        if current_time is None:
            return set()
        return {name for name, when in signal_times.get(key, [])
                if abs(when - current_time) <= signal_window}

    for payload in records:
        name = payload["subreddit"]
        by_sub[name].append(payload)

    rows = []
    for entry in config["subreddits"]:
        name = entry["name"]
        snapshots = sorted(by_sub.get(name, []), key=lambda p: p["sample_day"])[-duration:]
        unique = {}
        unique_times = {}
        fresh_daily = []
        for payload in snapshots:
            fetched = parse_dt(payload.get("fetched_at"))
            fresh = 0
            for item in payload.get("items", []):
                key = normalize_url(item.get("url")) or "title:" + title_key(item.get("title", ""))
                unique.setdefault(key, item)
                published = parse_dt(item.get("published"))
                when = published or fetched
                if when and (key not in unique_times or when < unique_times[key]):
                    unique_times[key] = when
                if fetched and published and timedelta(0) <= fetched - published <= timedelta(hours=24):
                    fresh += 1
            fresh_daily.append(fresh)

        items = list(unique.values())
        features = [item_features(item, audit_cfg) for item in items]
        count = len(items)
        rate = lambda key: (sum(bool(f[key]) for f in features) / count) if count else 0.0
        duplicate_items = 0
        matched = 0
        lead_hours = []
        for item_key, item in unique.items():
            keys = item_signal_keys(item, direct_domains)
            audit_time = unique_times.get(item_key)
            if any(len(nearby_signal_sources(key, audit_time)) > 1 for key in keys):
                duplicate_items += 1
            matches = [dt for key in keys for dt in existing.get(key, [])
                       if audit_time is not None and abs(dt - audit_time) <= signal_window]
            if matches:
                matched += 1
                if audit_time:
                    lead_hours.append((min(matches) - audit_time).total_seconds() / 3600)

        technical = rate("technical_proxy")
        evidence = rate("evidence_proxy")
        direct = rate("direct_evidence")
        noise = rate("noise_proxy")
        duplicate = duplicate_items / count if count else 0.0
        novelty = 1.0 - duplicate
        lead_bonus = max(0.0, min(1.0, median(lead_hours) / 24.0)) if lead_hours else 0.0
        activity = min(1.0, median(fresh_daily) / 5.0)
        score = (round(100 * (0.30 * technical + 0.22 * evidence + 0.13 * direct
                              + 0.12 * novelty + 0.08 * lead_bonus + 0.15 * activity
                              - 0.20 * noise)) if count else 0)
        attempt_days = len({p["sample_day"] for p in snapshots})
        sample_days = len({p["sample_day"] for p in snapshots
                           if p.get("status", "ok") == "ok"})
        if entry.get("role") == "control":
            decision = "对照组"
        elif attempt_days < duration:
            decision = f"采集中 {attempt_days}/{duration}"
        elif count == 0:
            decision = "退役/不可达"
        elif score >= 60 and technical >= 0.35 and median(fresh_daily) >= 2:
            decision = "核心候选"
        elif score >= 42 and technical >= 0.22:
            decision = "轮换候选"
        else:
            decision = "不纳入日更"
        rows.append({
            "name": name, "category": entry.get("category", ""), "role": entry.get("role", "candidate"),
            "sample_days": sample_days, "attempt_days": attempt_days,
            "unique_posts": count, "fresh_median": median(fresh_daily),
            "technical_rate": technical, "evidence_rate": evidence, "direct_rate": direct,
            "duplicate_rate": duplicate, "noise_rate": noise, "existing_matches": matched,
            "lead_hours": median(lead_hours, default=float("nan")), "score": score, "decision": decision,
        })
    return sorted(rows, key=lambda r: (r["role"] == "control", -r["score"], r["name"].lower()))


def pct(value):
    return f"{value * 100:.0f}%"


def render_report(config, rows, day):
    duration = config["audit"].get("duration_days", 14)
    covered = sum(r["attempt_days"] > 0 for r in rows)
    complete = sum(r["attempt_days"] >= duration for r in rows)
    sampled = max((r["attempt_days"] for r in rows), default=0)
    lines = [
        f"# Reddit 技术信源影子审计 — {day}", "",
        f"> 状态：低频轮换；{covered}/{len(rows)} 个社区已有样本，{complete}/{len(rows)} 个达到 {duration} 次，单社区最多 {sampled}/{duration} 次。未满窗口的排名只用于检查采集与代理指标，**不改变正式日报信源**。", "",
        "## 排名", "",
        "| subreddit | 类别 | 成功/尝试 | 24h 新帖中位数 | 唯一帖 | 技术代理 | 证据代理 | 直接证据链接 | 跨社区重复 | 噪声 | 对现有非 Reddit 命中 | 领先中位数 | 分数 | 建议 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        lead = "—" if row["lead_hours"] != row["lead_hours"] else f"{row['lead_hours']:+.1f}h"
        lines.append(
            f"| r/{row['name']} | {row['category']} | {row['sample_days']}/{row['attempt_days']} | {row['fresh_median']:.1f} | "
            f"{row['unique_posts']} | {pct(row['technical_rate'])} | {pct(row['evidence_rate'])} | "
            f"{pct(row['direct_rate'])} | {pct(row['duplicate_rate'])} | {pct(row['noise_rate'])} | "
            f"{row['existing_matches']} | {lead} | {row['score']} | {row['decision']} |")
    lines += [
        "", "## 口径与边界", "",
        f"- 审计每天最多轮换 {config['audit'].get('max_subreddits_per_run', 4)} 个社区；所有直接 Reddit 请求还受 `config/reddit_access.yaml` 的跨进程分钟级间隔与 UTC 日预算约束。失败和重试同样占预算。",
        "- 每个社区每次最多保留 25 条 `/new/.rss` 条目；跨日按 Reddit permalink 去重。`24h 新帖中位数`按抓取时刻前 24 小时估算，不等于 Reddit 的完整发帖量。",
        "- `技术代理`要求命中至少两个不同技术词组，或同时含直接证据链接与一个技术词组；`证据代理`要求直接证据链接，或技术代理同时带具体量化值。它们是可复算筛查信号，不是人工语义裁决。",
        "- `直接证据链接`只表示正文链接到配置中的代码、论文、模型或工程文档域名；尚未核验链接内容，也不能证明发帖人就是原作者。",
        f"- `跨社区重复`按直接证据 URL 优先、标题次之估算，并要求两个条目的有效时间相距不超过 {config['audit'].get('signal_match_window_days', 30)} 天；无发布时间时以首次抓取时间兜底。`领先中位数`只比较同一窗口内现有 L1 中的非 Reddit 信源，正数表示 Reddit 更早；只对成功匹配的条目计算。",
        "- 分数用于排序，不作为证据强度。每个社区满 14 次样本后仍需人工抽查高分与低分社区各一组，尤其核对 flair、正文方法学和评论区补证。",
        "", "## 晋级规则", "",
        "- 核心候选：满 14 次样本、分数 ≥60、技术代理 ≥35%，且 24h 新帖中位数 ≥2。",
        "- 轮换候选：满 14 次样本、分数 ≥42、技术代理 ≥22%。",
        "- `general-control` 永远只作对照，不会仅凭活跃度自动晋级。",
        "",
    ]
    return "\n".join(lines)


def audit_window_days(config):
    audit_cfg = config["audit"]
    duration = audit_cfg.get("duration_days", 14)
    batch = 1
    sweeps = math.ceil(len(config["subreddits"]) / batch)
    return duration * sweeps + 7


def generate_report(config, day, raw_root=AUDIT_RAW, existing_raw=None, report_dir=REPORT_DIR):
    duration = config["audit"].get("duration_days", 14)
    records = load_audit_records(raw_root, day, audit_window_days(config))
    existing = existing_index(existing_raw or ROOT / "data" / "raw", day, duration)
    rows = score_rows(config, records, existing)
    report = render_report(config, rows, day)
    path = report_dir / f"{day}.md"
    atomic_write_text(path, report)
    return path, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--only", default="", help="仅抓指定 subreddit，逗号分隔；仍生成全池报告")
    parser.add_argument("--delay", type=float, default=None,
                        help="增加审计层请求间隔；不能绕过全仓库 Reddit 硬限速")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    with exclusive_lock(AUDIT_LOCK, stale_after=2 * 60 * 60):
        only = {x.strip().lower() for x in args.only.split(",") if x.strip()}
        duration = config["audit"].get("duration_days", 14)
        path, rows = generate_report(config, args.date)
        if (not args.report_only and not only and rows
                and all(row["attempt_days"] >= duration for row in rows)):
            ok = sum(r["sample_days"] > 0 for r in rows)
            print(f"审计已完成：每个社区至少 {duration} 次尝试；停止抓取。")
            print(f"最终报告：{path}（{ok}/{len(rows)} 个社区有成功样本）")
            return 0
        if not args.report_only:
            collect(config, args.date, only=only, delay=args.delay)
        path, rows = generate_report(config, args.date)
        ok = sum(r["sample_days"] > 0 for r in rows)
        print(f"\n审计报告：{path}（{ok}/{len(rows)} 个社区已有样本）")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
