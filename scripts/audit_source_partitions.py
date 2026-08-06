# -*- coding: utf-8 -*-
"""对日报五个内容分区做通用信源影子审计，不改正式 sources.yaml。

候选采集写入 data/source_audit/raw/YYYY-MM-DD；报告写入
reports/source-audit/YYYY-MM-DD.md。正式 data/raw 只读，用作 incumbent 基线。
"""
import argparse
import hashlib
import html
import json
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

import build_digest
import fetch_l1
from state_io import atomic_write_if_changed, exclusive_lock

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "source_audit.yaml"
SHADOW_RAW = ROOT / "data" / "source_audit" / "raw"
SHADOW_STATE = ROOT / "data" / "source_audit" / "state"
REPORT_DIR = ROOT / "reports" / "source-audit"
AUDIT_LOCK = ROOT / "data" / "state" / "locks" / "source-partition-audit.lock"
URL_RE = re.compile(r"https?://[^\s<>'\"&]+", re.I)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9_+.-]*", re.I)
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "source"}
AGGREGATOR_HOSTS = {"news.smol.ai", "latent.space", "www.latent.space", "interconnects.ai",
                    "www.interconnects.ai", "jack-clark.net", "simonwillison.net"}
METHOD_MARKERS = ["benchmark", "eval", "ablation", "controlled", "reproduc", "methodology",
                  "baseline", "latency", "throughput", "tokens/s", "tok/s", "vram", "harness",
                  "experiment", "sample size", "same model", "same prompt", "a/b"]
TECH_MARKERS = ["cuda", "pytorch", "inference", "quantiz", "fine-tun", "deployment", "kernel",
                "agent", "tool call", "context", "model", "api", "github", "paper", "arxiv"]
NOISE_MARKERS = ["beginner", "career", "resume", "meme", "funny", "which model", "worth it"]
PRICE_MARKERS = ["pricing", "price", "subscription", "quota", "limit", "credits", "per million",
                 "per 1m", "token", "套餐", "额度", "限额", "积分"]
DIAGNOSTIC_MARKERS = ["incident", "outage", "degraded", "latency", "error rate", "regression",
                      "harness", "same prompt", "same task", "status", "iq ", "故障", "降智"]
CORPORATE_MARKERS = ["earnings", "filing", "investor", "acquisition", "funding", "partnership",
                     "revenue", "regulation", "lawsuit", "layoff", "hire", "财报", "公告", "配售", "解禁"]
ARTIFACT_HOSTS = {"github.com", "huggingface.co", "arxiv.org", "openreview.net"}
EXPERT_TECH_GROUPS = {
    "model_systems": ["llm", "language model", "transformer", "attention", "embedding",
                      "inference", "agent", "coding agent", "codex", "reasoning", "context window"],
    "evaluation": ["benchmark", "eval", "evaluation", "experiment", "ablation", "baseline",
                   "measurement", "dataset", "sample size"],
    "engineering": ["production", "deployment", "latency", "throughput", "serving", "vram",
                    "retrieval", "rag", "fine-tun", "pipeline", "observability", "harness"],
    "research": ["paper", "study", "scaling law", "reinforcement learning", "interpretability",
                 "representation", "optimization", "gradient", "theorem", "research"],
}
EXPERT_METHOD_MARKERS = list(dict.fromkeys(METHOD_MARKERS + [
    "we tested", "i tested", "we ran", "i ran", "we built", "i built", "case study",
    "implementation", "dataset", "results", "error analysis", "failure analysis",
]))
EXPERT_FIRSTHAND_MARKERS = [
    "we tested", "i tested", "we ran", "i ran", "we built", "i built", "we evaluated",
    "i evaluated", "we benchmarked", "i benchmarked", "our experiment", "my experiment",
    "our evaluation", "my evaluation", "our tests", "my tests", "in our test", "in my test",
    "we implemented", "i implemented", "our implementation", "my implementation",
]
EXPERT_SYNTHESIS_MARKERS = [
    "overview", "survey", "guide", "tutorial", "explainer", "introduction", "review",
    "primer", "literature", "research roundup", "deep dive", "notes on", "how it works",
]

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def atomic_json(path, value):
    atomic_write_if_changed(path, json.dumps(value, ensure_ascii=False, indent=1))


def atomic_text(path, value):
    atomic_write_if_changed(path, value)


def normalize_url(value):
    value = html.unescape(str(value or "").strip()).rstrip(".,);]}")
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
                       if k.lower() not in TRACKING])
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))


def extract_urls(text):
    return sorted({u for u in (normalize_url(v) for v in URL_RE.findall(html.unescape(text or ""))) if u})


def meaningful_artifact_url(url):
    parts = urlsplit(normalize_url(url))
    host = (parts.hostname or "").lower()
    segments = [segment for segment in parts.path.split("/") if segment]
    if host == "github.com":
        return len(segments) >= 2
    if host == "huggingface.co":
        return len(segments) >= 2 and segments[0] not in {"blog", "docs", "papers"}
    if host == "arxiv.org":
        return bool(segments) and segments[0] in {"abs", "pdf", "html"}
    if host == "openreview.net":
        return bool(segments) and segments[0] in {"forum", "pdf"}
    return False


def parse_dt(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        parsed = build_digest.parse_pubdate(value)
        return parsed.astimezone(timezone.utc) if parsed is not None else None


def title_key(title):
    tokens = [t for t in WORD_RE.findall(str(title or "").lower()) if len(t) > 2]
    return " ".join(tokens[:20])


def signal_keys(item):
    own = normalize_url(item.get("url"))
    expert = item.get("expert_features") or {}
    if expert:
        artifacts = sorted({normalize_url(url) for url in expert.get("artifact_urls") or []
                            if meaningful_artifact_url(url)})
        if artifacts:
            return ["url:" + url for url in artifacts]
        keys = ["url:" + own] if own else []
        title = title_key(item.get("title"))
        if title:
            keys.append("title:" + title)
        return keys
    values = [item.get(k, "") for k in ("summary", "match_text", "external_url")]
    values.extend(item.get("external_urls") or [])
    external = extract_urls(" ".join(str(value) for value in values))
    keys = ["url:" + url for url in external
            if (urlsplit(url).hostname or "").lower() not in AGGREGATOR_HOSTS]
    if keys:
        return keys
    return ["url:" + own] if own else ["title:" + title_key(item.get("title"))]


def signal_key(item):
    """兼容单键调用；评分路径使用 signal_keys 保留一篇文章的全部原始材料链接。"""
    return signal_keys(item)[0]


def load_config(path=CONFIG_PATH):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def topics_for_audit(base_topics, config):
    result = dict(base_topics)
    result["model_keywords"] = list(dict.fromkeys(
        list(base_topics.get("model_keywords", []))
        + list(config.get("audit", {}).get("discovery_keywords", []))))
    return result


def coverage_reference_errors(config, formal_source_ids):
    candidate_ids = {source["id"] for source in config.get("candidates", [])}
    errors = []
    for target, surfaces in config.get("coverage", {}).get("targets", {}).items():
        for surface, cell in surfaces.items():
            for sid in (cell or {}).get("incumbent", []):
                if sid not in formal_source_ids:
                    errors.append(f"{target}.{surface}: unknown incumbent {sid}")
            for sid in (cell or {}).get("trial", []):
                if sid not in candidate_ids:
                    errors.append(f"{target}.{surface}: unknown trial {sid}")
    return errors


def coverage_target_errors(config, model_keywords):
    """确保正式追踪的模型族不会再次从覆盖矩阵中静默漏列。"""
    coverage = config.get("coverage", {})
    aliases = coverage.get("target_aliases")
    if aliases is None:  # 兼容最小测试配置；正式配置显式启用该守卫。
        return []
    targets = set(coverage.get("targets", {}))
    errors = [f"coverage alias target missing: {target}" for target in aliases if target not in targets]
    declared = {alias for values in aliases.values() for alias in values}
    errors.extend(f"model keyword missing coverage target: {keyword}"
                  for keyword in model_keywords if keyword not in declared)
    return errors


def expert_author_config_errors(config):
    authors = [source for source in config.get("candidates", [])
               if source.get("track") == "expert_author"]
    groups = defaultdict(list)
    for source in authors:
        groups[source.get("audit_group", "")].append(source)
    errors = []
    if len(groups["first_batch"]) != 10:
        errors.append(f"expert_author first_batch must contain 10 sources, got {len(groups['first_batch'])}")
    if len(groups["rotation"]) != 3:
        errors.append(f"expert_author rotation must contain 3 sources, got {len(groups['rotation'])}")
    if len(groups["historical_archive"]) != 3:
        errors.append("expert_author historical_archive must contain 3 sources, "
                      f"got {len(groups['historical_archive'])}")
    partition = config.get("partitions", {}).get("research_synthesis", {})
    if not partition.get("shadow_only"):
        errors.append("research_synthesis must remain shadow_only")
    duration = config.get("audit", {}).get("duration_days", 14)
    for source in authors:
        sid = source.get("id", "unknown")
        group = source.get("audit_group")
        if source.get("type") != "rss" or not source.get("url"):
            errors.append(f"{sid}: expert_author requires an RSS URL")
        if group == "first_batch":
            if not source.get("enabled", True):
                errors.append(f"{sid}: first_batch must be enabled")
            if source.get("routing") != "expert_author":
                errors.append(f"{sid}: first_batch must use expert_author routing")
            if not source.get("audit_fulltext"):
                errors.append(f"{sid}: first_batch must derive fulltext features")
            if int(source.get("lookback_days") or 0) <= duration:
                errors.append(f"{sid}: first_batch lookback must exceed sampling duration")
            if set(source.get("partitions", [])) != {"field_test", "research_synthesis"}:
                errors.append(f"{sid}: first_batch partitions must be field_test + research_synthesis")
        elif group in ("rotation", "historical_archive") and source.get("enabled", True):
            errors.append(f"{sid}: {group} must not join unattended sampling")
        if source.get("affiliation") and not source.get("conflict_keywords"):
            errors.append(f"{sid}: affiliation requires conflict_keywords")
    return errors


def matched_markers(text, markers):
    lowered = text.lower()
    return sorted({marker for marker in markers if marker.lower() in lowered})


def annotate_candidate_items(source, items):
    """从临时 RSS 全文派生可复算特征；第三方全文在返回前即删除，不进入快照。"""
    for item in items:
        fulltext = item.pop("_audit_fulltext", "")
        body_error = item.pop("_audit_body_error", "")
        if source.get("track") != "expert_author":
            continue
        text = html.unescape(" ".join(str(value) for value in (
            item.get("title", ""), item.get("summary", ""), fulltext)))
        technical_groups = sorted(
            group for group, markers in EXPERT_TECH_GROUPS.items()
            if matched_markers(text, markers))
        urls = extract_urls(" ".join(
            [str(item.get("url", "")), *(item.get("external_urls") or [])]))
        artifact_urls = [url for url in urls if meaningful_artifact_url(url)]
        conflicts = matched_markers(text, source.get("conflict_keywords", []))
        item["expert_features"] = {
            "technical_groups": technical_groups,
            "method_hits": matched_markers(text, EXPERT_METHOD_MARKERS),
            "firsthand_hits": matched_markers(text, EXPERT_FIRSTHAND_MARKERS),
            "synthesis_hits": matched_markers(text, EXPERT_SYNTHESIS_MARKERS),
            "artifact_urls": artifact_urls,
            "conflict_keywords": conflicts,
            "fulltext_used": bool(fulltext),
            "fulltext_chars": len(fulltext),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "body_fallback_error": body_error,
        }
    return items


def hydrate_expert_fulltext(source, items):
    """对 RSS 正文缺失/过短的少量最新条目抓文章页；正文仍只在内存中存在。"""
    if source.get("track") != "expert_author":
        return items
    maximum = int(source.get("audit_body_fallback_items") or 0)
    if maximum <= 0:
        return items
    minimum = int(source.get("audit_body_fallback_min_chars") or 1500)
    delay = float(source.get("audit_body_fallback_delay_seconds") or 1)
    attempted = 0
    for item in items:
        if attempted >= maximum:
            break
        if len(item.get("_audit_fulltext") or "") >= minimum or not item.get("url"):
            continue
        if attempted and delay:
            time.sleep(delay)
        attempted += 1
        try:
            response = fetch_l1.http_get(item["url"], accept="text/html,application/xhtml+xml")
            if response.encoding in (None, "ISO-8859-1"):
                response.encoding = response.apparent_encoding or "utf-8"
            text = fetch_l1.visible_html_text(response.text)
            limit = int(source.get("audit_fulltext_char_limit", 100_000))
            item["_audit_fulltext"] = text[:limit]
            item["external_urls"] = sorted(set(item.get("external_urls") or [])
                                           | set(fetch_l1.extract_http_links(response.text, item["url"])))
        except Exception as exc:
            item["_audit_body_error"] = f"{type(exc).__name__}: {exc}"
    return items


def candidates_for_collection(config, only=None):
    candidates = config.get("candidates", [])
    if not only:
        return [source for source in candidates if source.get("enabled", True)]
    return [source for source in candidates
            if source["id"].lower() in only
            and (source.get("enabled", True) or source.get("audit_group") == "rotation")]


def collect_candidates(config, day, only=None, delay=None, raw_root=SHADOW_RAW):
    selected = candidates_for_collection(config, only)
    day_dir = raw_root / day
    day_dir.mkdir(parents=True, exist_ok=True)
    log_path = day_dir / "_audit_log.json"
    try:
        log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {}
    except json.JSONDecodeError:
        log = {}
    log = {"date": day, "started_at": log.get("started_at", datetime.now(timezone.utc).isoformat()),
           "sources": log.get("sources", {})}
    for path in day_dir.glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if old.get("status", "ok") == "ok":
            log["sources"][old.get("source", path.stem)] = {
                "status": "ok", "items": len(old.get("items", []))}

    wait = config["audit"].get("request_delay_seconds", 2) if delay is None else delay
    old_state = fetch_l1.STATE_DIR
    fetch_l1.STATE_DIR = SHADOW_STATE
    try:
        for index, source in enumerate(selected):
            if index and wait:
                time.sleep(wait)
            sid = source["id"]
            output = day_dir / f"{sid}.json"
            try:
                fetcher = fetch_l1.FETCHERS[source["type"]]
                items = fetcher(dict(source))
                if source.get("audit_max_items"):
                    items = items[:int(source["audit_max_items"])]
                hydrate_expert_fulltext(source, items)
                annotate_candidate_items(source, items)
                payload = {
                    "source": sid, "name": source["name"], "tier": source["tier"],
                    "role": source.get("role", "candidate"), "routing": source.get("routing", "classify"),
                    "track": source.get("track", ""),
                    "audit_group": source.get("audit_group", ""),
                    "audit_status": source.get("audit_status", ""),
                    "evidence_role": source.get("evidence_role", ""),
                    "cadence": source.get("cadence", "daily"),
                    "lookback_days": source.get("lookback_days"),
                    "affiliation": source.get("affiliation", ""),
                    "affiliation_role": source.get("affiliation_role", ""),
                    "conflict_keywords": source.get("conflict_keywords", []),
                    "partitions": source.get("partitions", []), "status": "ok",
                    "fetched_at": datetime.now(timezone.utc).isoformat(), "items": items,
                }
                atomic_json(output, payload)
                log["sources"][sid] = {"status": "ok", "items": len(items)}
                print(f"[ok]   {sid}: {len(items)} items", flush=True)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                try:
                    old = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    old = {}
                if old.get("status") == "ok":
                    log["sources"][sid] = {"status": "ok", "items": len(old.get("items", [])),
                                           "retry_error": error}
                else:
                    atomic_json(output, {
                        "source": sid, "name": source["name"], "tier": source["tier"],
                        "role": source.get("role", "candidate"), "routing": source.get("routing", "classify"),
                        "track": source.get("track", ""),
                        "audit_group": source.get("audit_group", ""),
                        "audit_status": source.get("audit_status", ""),
                        "evidence_role": source.get("evidence_role", ""),
                        "cadence": source.get("cadence", "daily"),
                        "lookback_days": source.get("lookback_days"),
                        "affiliation": source.get("affiliation", ""),
                        "affiliation_role": source.get("affiliation_role", ""),
                        "conflict_keywords": source.get("conflict_keywords", []),
                        "partitions": source.get("partitions", []), "status": "error",
                        "fetched_at": datetime.now(timezone.utc).isoformat(), "error": error, "items": [],
                    })
                    log["sources"][sid] = {"status": "error", "error": error}
                print(f"[fail] {sid}: {error}", flush=True)
    finally:
        fetch_l1.STATE_DIR = old_state
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_json(log_path, log)
    prune(raw_root, day, config["audit"].get("keep_days", 21))
    return log


def prune(raw_root, day, keep_days):
    cutoff = date.fromisoformat(day) - timedelta(days=keep_days)
    if not raw_root.exists():
        return
    for folder in raw_root.iterdir():
        if not folder.is_dir():
            continue
        try:
            expired = date.fromisoformat(folder.name) < cutoff
        except ValueError:
            expired = False
        if expired:
            shutil.rmtree(folder)


def completed_days(raw_root, end_day, required):
    result = []
    if not raw_root.exists():
        return result
    end = date.fromisoformat(end_day)
    for folder in sorted(raw_root.iterdir()):
        try:
            sample = date.fromisoformat(folder.name)
        except (ValueError, AttributeError):
            continue
        if sample > end:
            continue
        try:
            log = json.loads((folder / "_audit_log.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        actual = set(log.get("sources", {}))
        complete = (len(actual) >= required if isinstance(required, int)
                    else set(required).issubset(actual))
        if log.get("finished_at") and complete:
            result.append(folder.name)
    return result


def audit_sample_days(raw_root, end_day, duration, required):
    """选最近 N 个完整候选采样日；窗口未满时允许展示当天局部结果。"""
    completed = completed_days(raw_root, end_day, required)
    selected = completed[-duration:]
    current = raw_root / end_day
    if len(selected) < duration and current.is_dir() and end_day not in selected:
        selected.append(end_day)
    return selected


def load_payloads(root, end_day, duration, origin, sample_days=None):
    start = date.fromisoformat(end_day) - timedelta(days=duration - 1)
    selected = set(sample_days) if sample_days is not None else None
    result = []
    if not root.exists():
        return result
    for folder in sorted(root.iterdir()):
        try:
            sample = date.fromisoformat(folder.name)
        except (ValueError, AttributeError):
            continue
        if selected is not None:
            if folder.name not in selected:
                continue
        elif not (start <= sample <= date.fromisoformat(end_day)):
            continue
        for path in folder.glob("*.json"):
            if path.name.startswith("_"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            payload["sample_day"] = folder.name
            payload["origin"] = origin
            payload.setdefault("status", "ok")
            payload.setdefault("role", "incumbent" if origin == "incumbent" else "candidate")
            payload.setdefault("routing", "classify")
            result.append(payload)
    return result


def route_expert_author(payload, config, allowed):
    """作者轨道互斥归类：有原作者方法证据才进实测，否则技术深度内容进综述。"""
    routed = []
    for item in eligible_raw_items(payload, config):
        features = item.get("expert_features") or {}
        methods = features.get("method_hits") or []
        firsthand_hits = features.get("firsthand_hits") or []
        artifacts = features.get("artifact_urls") or []
        technical = features.get("technical_groups") or []
        synthesis = features.get("synthesis_hits") or []
        conflicts = features.get("conflict_keywords") or []
        firsthand = bool(firsthand_hits) and (
            len(methods) >= 2 or (bool(methods) and bool(artifacts)))
        analytical = len(technical) >= 2 or (bool(conflicts) and bool(technical))
        if firsthand and "field_test" in allowed:
            routed.append(("field_test", item))
        elif analytical and "research_synthesis" in allowed:
            routed.append(("research_synthesis", item))
    return routed


def route_payload(payload, config, topics):
    if payload.get("status") != "ok":
        return []
    section_to_key = {v["section"]: k for k, v in config["partitions"].items()}
    allowed = set(payload.get("partitions") or config["partitions"])
    routed = []
    if payload.get("routing") == "expert_author" or payload.get("track") == "expert_author":
        return route_expert_author(payload, config, allowed)
    lookback = int(payload.get("lookback_days") or config["audit"].get("duration_days", 14))
    if payload.get("routing") in ("forced", "forced_recent"):
        items = payload.get("items", [])
        if payload.get("routing") == "forced_recent":
            cutoff = (datetime.fromisoformat(payload["sample_day"]).replace(tzinfo=timezone.utc)
                      - timedelta(days=lookback))
            items = [item for item in items
                     if (build_digest.parse_pubdate(item.get("published")) or cutoff) >= cutoff]
        for key in allowed:
            for item in items:
                routed.append((key, item))
        return routed
    # 先收窄到该候选被授权评估的分区；否则一个更高分的禁用栏目会让本可用信号被归栏后丢弃。
    audit_topics = dict(topics)
    audit_topics["topics"] = {
        name: topic for name, topic in topics.get("topics", {}).items()
        if section_to_key.get(topic.get("section")) in allowed
    }
    sectioned, _ = build_digest.classify([payload], audit_topics, payload["sample_day"], lookback)
    originals = {normalize_url(item.get("url")): item for item in payload.get("items", [])}
    for section, items in sectioned.items():
        key = section_to_key.get(section)
        if key in allowed:
            for item in items:
                original = originals.get(normalize_url(item.get("url")), {})
                item["external_urls"] = list(original.get("external_urls") or [])
                routed.append((key, item))
    return routed


def eligible_raw_items(payload, config):
    """返回与路由器使用相同日期/可变更边界的原始分母。"""
    items = payload.get("items", [])
    routing = payload.get("routing", "classify")
    if routing == "forced":
        return items
    lookback = int(payload.get("lookback_days") or config["audit"].get("duration_days", 14))
    cutoff = (datetime.fromisoformat(payload["sample_day"]).replace(tzinfo=timezone.utc)
              - timedelta(days=lookback))
    eligible = []
    for item in items:
        if routing == "classify" and (not item.get("url") or item.get("changed") is False):
            continue
        published = build_digest.parse_pubdate(item.get("published"))
        if published is not None and published < cutoff:
            continue
        eligible.append(item)
    return eligible


def contains_any(text, markers):
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def item_quality(partition, item, tier, evidence_role=""):
    text = html.unescape(f"{item.get('title', '')} {item.get('summary', '')} {item.get('match_text', '')}")
    url_values = [text, item.get("url", ""), item.get("external_url", "")]
    url_values.extend(item.get("external_urls") or [])
    urls = extract_urls(" ".join(str(value) for value in url_values))
    hosts = {(urlsplit(url).hostname or "").lower() for url in urls}
    own_host = (urlsplit(normalize_url(item.get("url"))).hostname or "").lower()
    expert = item.get("expert_features") or {}
    conflict = bool(expert.get("conflict_keywords"))
    primary = tier in ("official", "finance", "radar")
    # 官方整页快照常含 Careers 等导航词；社区噪声词不能反向惩罚官方/财经/雷达页。
    noise = ((contains_any(text, NOISE_MARKERS) if tier not in ("official", "finance", "radar") else False)
             or item.get("readable") is False)
    if partition == "release":
        quality = bool(hosts & ARTIFACT_HOSTS) or contains_any(text, ["model card", "weights", "release", "launch"])
        noise = noise or item.get("title", "").startswith("[页面不可读]")
    elif partition == "field_test":
        if expert:
            methods = len(expert.get("method_hits") or [])
            artifacts = expert.get("artifact_urls") or []
            firsthand = expert.get("firsthand_hits") or []
            quality = bool(firsthand) and (methods >= 2 or (methods >= 1 and bool(artifacts)))
        else:
            methods = sum(1 for marker in METHOD_MARKERS if marker in text.lower())
            quality = methods >= 2 or (methods >= 1 and bool(hosts & ARTIFACT_HOSTS))
        original = (own_host in ARTIFACT_HOSTS or tier == "radar"
                    or evidence_role in ("original_author", "independent_test"))
        primary = quality and original
    elif partition == "research_synthesis":
        technical = expert.get("technical_groups") or []
        synthesis = expert.get("synthesis_hits") or []
        artifacts = expert.get("artifact_urls") or []
        conflict_related = bool(expert.get("conflict_keywords")) and bool(technical)
        quality = (len(technical) >= 2 or conflict_related) and (
            conflict_related or bool(synthesis) or bool(artifacts)
            or int(expert.get("fulltext_chars") or 0) >= 300)
        primary = False
        noise = noise or len(technical) < 2
    elif partition == "pricing":
        has_currency = bool(re.search(r"[$￥¥]|\b(?:USD|CNY)\b|\d+(?:\.\d+)?\s*元", text, re.I))
        quality = has_currency and contains_any(text, PRICE_MARKERS)
        noise = noise or item.get("readable") is False
    elif partition == "degradation":
        quality = contains_any(text, DIAGNOSTIC_MARKERS) and (
            primary or bool(re.search(r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|minutes?|hours?)\b", text, re.I)))
    else:
        quality = contains_any(text, CORPORATE_MARKERS) or tier in ("finance",)
        tech_only = contains_any(text, TECH_MARKERS) and not contains_any(text, CORPORATE_MARKERS)
        noise = noise or (tech_only and tier not in ("official", "finance"))
    if item.get("readable") is False:
        quality = False
    return {"primary": bool(primary), "quality": bool(quality), "noise": bool(noise),
            "conflict": conflict}


def build_rows(config, payloads, topics):
    raw_by_source = defaultdict(dict)
    meta = {}
    routed = defaultdict(dict)
    sample_days = defaultdict(set)
    attempt_days = defaultdict(set)
    probe_readability = defaultdict(list)
    signal_times = defaultdict(list)
    routed_times = {}
    signal_window = timedelta(days=config.get("audit", {}).get("signal_match_window_days", 30))

    def nearby_signal_times(partition, sig, current_time):
        times = signal_times.get((partition, sig), [])
        if current_time is None:
            return []
        return [(source, when) for source, when in times
                if abs(when - current_time) <= signal_window]

    for payload in payloads:
        sid = payload.get("source", payload.get("name", "unknown"))
        meta[sid] = payload
        attempt_days[sid].add(payload["sample_day"])
        if payload.get("status") != "ok":
            continue
        sample_days[sid].add(payload["sample_day"])
        for item in eligible_raw_items(payload, config):
            if "readable" in item:
                probe_readability[sid].append(item.get("readable") is True)
            key = normalize_url(item.get("url")) or "title:" + title_key(item.get("title"))
            raw_by_source[sid].setdefault(key, item)
        for partition, item in route_payload(payload, config, topics):
            key = normalize_url(item.get("url")) or "title:" + title_key(item.get("title"))
            routed[(partition, sid)].setdefault(key, item)
            when = parse_dt(item.get("published")) or parse_dt(payload.get("fetched_at"))
            time_key = (partition, sid, key)
            if when and (time_key not in routed_times or when < routed_times[time_key]):
                routed_times[time_key] = when
            for sig in signal_keys(item):
                if when:
                    signal_times[(partition, sig)].append((sid, when))

    rows = []
    duration = config["audit"].get("duration_days", 14)
    for (partition, sid), item_map in routed.items():
        payload = meta[sid]
        item_entries = list(item_map.items())
        items = [item for _, item in item_entries]
        features = [item_quality(partition, item, payload.get("tier", ""),
                                 payload.get("evidence_role", "")) for item in items]
        count = len(items)
        rate = lambda key: sum(f[key] for f in features) / count if count else 0.0
        duplicate = sum(any(len({source for source, _ in nearby_signal_times(
                                    partition, sig, routed_times.get((partition, sid, key)))}) > 1
                            for sig in signal_keys(item))
                        for key, item in item_entries) / count if count else 0.0
        lags = []
        for key, item in item_entries:
            item_lags = []
            current_time = routed_times.get((partition, sid, key))
            for sig in signal_keys(item):
                times = nearby_signal_times(partition, sig, current_time)
                if (current_time is not None
                        and any(source == sid and when == current_time for source, when in times)
                        and len({source for source, _ in times}) >= 2):
                    item_lags.append(
                        (current_time - min(t for _, t in times)).total_seconds() / 3600)
            if item_lags:
                lags.append(min(item_lags, key=abs))
        primary, quality, noise = rate("primary"), rate("quality"), rate("noise")
        conflict = rate("conflict")
        readability = (sum(probe_readability[sid]) / len(probe_readability[sid])
                       if probe_readability[sid] else 1.0)
        effective_quality = quality * readability
        effective_noise = max(noise, 1.0 - readability)
        qualification = count / max(1, len(raw_by_source[sid]))
        novelty = 1.0 - duplicate
        # 没有第二个来源命中同一信号时，时延不可观测，也不能按“最快”奖励。
        lag_score = 0.0 if not lags else max(0.0, 1.0 - statistics.median(lags) / 72.0)
        if partition == "field_test":
            score = 100 * (0.35 * effective_quality + 0.20 * primary + 0.15 * qualification
                           + 0.20 * novelty + 0.10 * lag_score - 0.20 * effective_noise)
        elif partition == "research_synthesis":
            score = 100 * (0.55 * effective_quality + 0.15 * qualification
                           + 0.20 * novelty + 0.10 * lag_score - 0.20 * effective_noise)
        elif partition == "pricing":
            score = 100 * (0.40 * primary + 0.35 * effective_quality + 0.10 * qualification
                           + 0.10 * novelty + 0.05 * lag_score - 0.30 * effective_noise)
        elif partition == "degradation":
            corroborated = sum(any(
                len({source for source, _ in nearby_signal_times(
                    partition, sig, routed_times.get((partition, sid, key)))}) > 1
                for sig in signal_keys(item))
                               for key, item in item_entries) / count
            score = 100 * (0.30 * primary + 0.30 * effective_quality + 0.15 * corroborated
                           + 0.15 * novelty + 0.10 * lag_score - 0.25 * effective_noise)
        elif partition == "company":
            score = 100 * (0.40 * primary + 0.25 * effective_quality + 0.10 * qualification
                           + 0.15 * novelty + 0.10 * lag_score - 0.30 * effective_noise)
        else:
            score = 100 * (0.35 * primary + 0.25 * effective_quality + 0.15 * qualification
                           + 0.15 * novelty + 0.10 * lag_score - 0.25 * effective_noise)
        role = payload.get("role", "incumbent")
        cadence = payload.get("cadence", "daily")
        attempts = len(attempt_days[sid])
        if role == "incumbent":
            decision = "正式池基线"
        elif attempts < duration:
            decision = f"采集中 {attempts}/{duration}"
        elif score >= 60 and quality >= 0.30 and readability >= 0.80:
            decision = "低频轮换候选" if cadence == "low_frequency" else "核心候选"
        elif score >= 42:
            decision = "低频轮换候选" if cadence == "low_frequency" else "轮换候选"
        else:
            decision = "不纳入"
        rows.append({
            "partition": partition, "source_id": sid, "name": payload.get("name", sid), "role": role,
            "cadence": cadence, "track": payload.get("track", ""),
            "affiliation": payload.get("affiliation", ""),
            "sample_days": len(sample_days[sid]), "attempt_days": attempts,
            "raw": len(raw_by_source[sid]), "qualified": count, "qualification": qualification,
            "primary": primary, "quality": quality, "duplicate": duplicate, "noise": noise,
            "novelty": novelty, "readability": readability, "conflict": conflict,
            "lag": statistics.median(lags) if lags else float("nan"),
            "score": round(max(0.0, min(100.0, score))), "decision": decision,
        })

    # “成功抓取但零入栏”与失败尝试同样是审计结果，不能因为没有 routed item 而消失。
    present = {(row["partition"], row["source_id"]) for row in rows}
    for sid, payload in meta.items():
        if payload.get("role") != "candidate":
            continue
        for partition in payload.get("partitions", []):
            if (partition, sid) in present:
                continue
            attempts = len(attempt_days[sid])
            decision = (f"采集中 {attempts}/{duration}" if attempts < duration else "不纳入")
            rows.append({
                "partition": partition, "source_id": sid, "name": payload.get("name", sid),
                "role": "candidate", "cadence": payload.get("cadence", "daily"),
                "track": payload.get("track", ""),
                "affiliation": payload.get("affiliation", ""),
                "sample_days": len(sample_days[sid]), "attempt_days": attempts,
                "raw": len(raw_by_source[sid]), "qualified": 0, "qualification": 0.0,
                "primary": 0.0, "quality": 0.0, "duplicate": 0.0, "noise": 0.0,
                "novelty": 0.0, "conflict": 0.0,
                "readability": (sum(probe_readability[sid]) / len(probe_readability[sid])
                                if probe_readability[sid] else 1.0),
                "lag": float("nan"), "score": 0, "decision": decision,
            })
    # 新增或持续失败、尚未形成任何快照的候选也必须显式出现，避免配置项静默消失。
    for source in config.get("candidates", []):
        sid = source["id"]
        if not source.get("enabled", True) or sid in meta:
            continue
        for partition in (source.get("partitions") or config["partitions"]):
            rows.append({
                "partition": partition, "source_id": sid, "name": source.get("name", sid),
                "role": "candidate", "cadence": source.get("cadence", "daily"),
                "track": source.get("track", ""), "affiliation": source.get("affiliation", ""),
                "sample_days": 0, "attempt_days": 0, "raw": 0, "qualified": 0,
                "qualification": 0.0, "primary": 0.0, "quality": 0.0,
                "duplicate": 0.0, "novelty": 0.0, "noise": 0.0,
                "readability": 0.0, "conflict": 0.0,
                "lag": float("nan"), "score": 0, "decision": "未采集",
            })
    return sorted(rows, key=lambda r: (list(config["partitions"]).index(r["partition"]),
                                       r["role"] == "incumbent", -r["score"], r["source_id"]))


def cell_text(value):
    if not value:
        return "—"
    parts = []
    if value.get("incumbent"):
        parts.append("✅ " + ", ".join(value["incumbent"]))
    if value.get("trial"):
        parts.append("🧪 " + ", ".join(value["trial"]))
    if value.get("blocked"):
        parts.append("⛔ " + value["blocked"])
    if value.get("gap"):
        parts.append("⚠️ " + value["gap"])
    if value.get("manual"):
        parts.append("👁 " + value["manual"])
    if value.get("not_applicable"):
        parts.append("➖ " + value["not_applicable"])
    return "<br>".join(parts) or "—"


def pct(value):
    return f"{value * 100:.0f}%"


def render_expert_catalog(config):
    authors = [source for source in config.get("candidates", [])
               if source.get("track") == "expert_author"]
    if not authors:
        return []
    lines = [
        "", "## 专家作者轨道目录", "",
        "> `first_batch` 参与本轮影子采样；`rotation` 只低频轮换或专项调用；"
        "`historical_archive` 只保留历史检索入口。", "",
        "| 作者/出版物 | 组别 | 状态 | 最近条目核验 | 归属披露 | 入口 |",
        "|---|---|---|---:|---|---|",
    ]
    for source in authors:
        affiliation = source.get("affiliation") or "—"
        lines.append(
            f"| {source['name']} | {source.get('audit_group', '—')} | "
            f"{source.get('audit_status', '—')} | {source.get('latest_seen', '—')} | "
            f"{affiliation} | [RSS]({source['url']}) |")
    return lines


def render_report(config, rows, day, complete_count):
    lines = [
        f"# 全分区信源影子审计 — {day}", "",
        f"> 完整候选采样日：{complete_count}/{config['audit']['duration_days']}。候选源不进入正式日报；未满窗口只显示临时排序。", "",
        "## 直接信源覆盖矩阵", "",
        "✅ 正式直接源；🧪 影子候选；⛔ 已验证阻断；⚠️ 尚无已验证候选；👁 浏览器可见但未自动化；➖ 不适用。聚合器与 OpenRouter 不算厂商直接覆盖。", "",
        "| 对象 | 发布 | 定价 | 降智/状态 | 公司材料 |", "|---|---|---|---|---|",
    ]
    for target, cells in config["coverage"]["targets"].items():
        lines.append(f"| {target} | {cell_text(cells.get('release'))} | {cell_text(cells.get('pricing'))} | "
                     f"{cell_text(cells.get('degradation'))} | {cell_text(cells.get('company'))} |")
    lines += render_expert_catalog(config)
    for key, partition in config["partitions"].items():
        lines += ["", f"## {partition['label']}信源", "",
                  "| 信源 | 身份 | 归属披露 | 成功/尝试 | 原始唯一条目 | 入栏 | 入栏率 | 主证据率 | 专用质量率 | 利益关系命中 | 页面可读率 | 独家率 | 跨源重复 | 噪声 | 相对最早滞后 | 分数 | 建议 |",
                  "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
        selected = [row for row in rows if row["partition"] == key]
        if not selected:
            lines.append("| （当前窗口无命中） | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |")
        for row in selected:
            lag = "—" if row["lag"] != row["lag"] else f"{row['lag']:.1f}h"
            identity = row["role"]
            if row.get("track") == "expert_author":
                identity += " / expert-author"
            if row.get("cadence") == "low_frequency":
                identity += " / low-frequency"
            affiliation = row.get("affiliation") or "—"
            lines.append(f"| {row['name']} (`{row['source_id']}`) | {identity} | {affiliation} | "
                         f"{row['sample_days']}/{row['attempt_days']} | {row['raw']} | {row['qualified']} | "
                         f"{pct(row['qualification'])} | {pct(row['primary'])} | {pct(row['quality'])} | "
                         f"{pct(row.get('conflict', 0.0))} | {pct(row['readability'])} | "
                         f"{pct(row.get('novelty', 0.0))} | {pct(row['duplicate'])} | "
                         f"{pct(row['noise'])} | "
                         f"{lag} | {row['score']} | {row['decision']} |")
    lines += [
        "", "## 指标边界", "",
        "- `入栏率`只在与路由器相同的内容时间窗内计算；它同时受 topics.yaml 关键词影响，低分可能是信源不相关，也可能是分类召回不足。",
        "- `主证据率`按栏目区分：发布/定价/公司优先官方或财经层；实测必须来自原始论文/代码页、明确标注的原作者或独立雷达，聚合文链接到材料本身不升级为主证据；降智优先状态页、雷达或带量化诊断的记录。",
        "- `技术综述/研究解读`是影子专用类别：要求至少两个技术主题组，并带综述语义、原始材料链接或三个以上技术主题组；它不会写入正式日报栏目。",
        "- expert_author 的方法/综述代理从 RSS 全文在内存中派生，快照只保存命中词组、字符数与哈希，不保存第三方全文。`利益关系命中`表示文章涉及配置中的现任雇主或产品，必须披露，但不自动否定其一线价值。",
        "- `专用质量率`是文本代理：发布看模型卡/权重/发布语义，实测看对照与可复现细节，定价看币种与计价语义，降智看诊断，企业动态看公告/财报/监管语义。",
        "- `页面可读率`按每天的 HTML 探针独立计算；抓取成功但正文缺少必要模式仍算不可读，并直接折损质量分。非页面型 feed 记为 100%。",
        f"- `独家率`为 1−跨源重复率；重复和滞后会检查文章保存的全部原始外链，再以规范化标题/URL 兜底，并要求发布时间相距不超过 {config.get('audit', {}).get('signal_match_window_days', 30)} 天。只有至少两个不同来源在该窗口命中同一信号才计算滞后；否则显示为 —、且时延项不加分，不能解释为领先。",
        "- 所有评分仅用于信源晋退排序；候选满 14 个完整采样日后仍需人工抽查，不自动修改 config/sources.yaml。",
        "",
    ]
    return "\n".join(lines)


def generate_report(config, day, shadow_root=SHADOW_RAW, formal_root=None, report_dir=REPORT_DIR,
                    include_current_partial=False):
    duration = config["audit"].get("duration_days", 14)
    required = {source["id"] for source in config["candidates"] if source.get("enabled", True)}
    sample_days = audit_sample_days(shadow_root, day, duration, required)
    shadow_days = list(sample_days)
    if (include_current_partial and (shadow_root / day).is_dir()
            and day not in shadow_days):
        shadow_days.append(day)
    formal = load_payloads(formal_root or ROOT / "data" / "raw", day, duration, "incumbent",
                           sample_days=sample_days)
    shadow = load_payloads(shadow_root, day, duration, "candidate", sample_days=shadow_days)
    topics = topics_for_audit(
        yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8")), config)
    rows = build_rows(config, formal + shadow, topics)
    complete = completed_days(shadow_root, day, required)
    path = report_dir / f"{day}.md"
    atomic_text(path, render_report(config, rows, day, min(duration, len(complete))))
    return path, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--only", default="", help="只抓指定候选 id，逗号分隔")
    parser.add_argument("--delay", type=float, default=None)
    args = parser.parse_args()
    try:
        args.date = date.fromisoformat(args.date).isoformat()
    except ValueError:
        sys.exit(f"--date 必须是 YYYY-MM-DD，收到：{args.date!r}")
    config = load_config(args.config)
    formal_cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    reference_errors = coverage_reference_errors(
        config, {source["id"] for source in formal_cfg.get("sources", [])})
    target_errors = coverage_target_errors(
        config, yaml.safe_load((ROOT / "config" / "topics.yaml").read_text(encoding="utf-8"))
        .get("model_keywords", []))
    expert_errors = expert_author_config_errors(config)
    if reference_errors or target_errors or expert_errors:
        sys.exit("source_audit.yaml 无效：\n- "
                 + "\n- ".join(reference_errors + target_errors + expert_errors))
    enabled = [s for s in config["candidates"] if s.get("enabled", True)]
    enabled_ids = {source["id"] for source in enabled}
    with exclusive_lock(AUDIT_LOCK, stale_after=2 * 60 * 60):
        only = {s.strip().lower() for s in args.only.split(",") if s.strip()}
        complete = completed_days(SHADOW_RAW, args.date, enabled_ids)
        if not args.report_only and not only and len(complete) >= config["audit"]["duration_days"]:
            path, _ = generate_report(config, complete[-1])
            print(f"审计已完成 {len(complete)} 个完整采样日；停止抓取。最终报告：{path}")
            return 0
        if not args.report_only:
            collect_candidates(config, args.date, only=only, delay=args.delay)
        path, rows = generate_report(
            config, args.date, include_current_partial=bool(only))
        trials = {row["source_id"] for row in rows
                  if row["role"] == "candidate" and row["qualified"] > 0}
        print(f"\n分区审计报告：{path}（{len(trials)}/{len(enabled)} 个候选已有入栏信号）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
