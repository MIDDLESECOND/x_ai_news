# -*- coding: utf-8 -*-
"""L1 全自动抓取：读 config/sources.yaml，逐个信源拉取并归一化，落盘 data/raw/YYYY-MM-DD/。

零 X 风险：全部免登录公开端点。单个信源失败只记录日志，不影响其他信源。
页面快照（html_stub）带跨日内容哈希状态（data/state/html_snapshots.json）——
只有内容变化的快照才会被 build_digest 收入简报。
data/raw 按日期目录自动保留最近 N 天（--keep-days，默认 45）。

用法：python scripts/fetch_l1.py [--date YYYY-MM-DD] [--keep-days N]
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
UA = "frontier-radar/0.1 (personal news pipeline; +https://github.com/MIDDLESECOND/x_ai_news)"
TIMEOUT = 30

X_LINK_RE = re.compile(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")
# X 的保留路径段（x.com/i/status/... 等非用户名路径），挖掘时即过滤
X_RESERVED_PATHS = {"i", "search", "home", "intent", "hashtag", "explore", "share"}
TAG_RE = re.compile(r"<[^>]+>")

if sys.stdout:  # pythonw / stdout 分离的无控制台运行下 sys.stdout 为 None
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def http_get(url, accept=None):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp


def strip_tags(html):
    return TAG_RE.sub(" ", html or "").strip()


def mine_x_links(text):
    """从任意文本提取推文链接（归一化为 x.com/<handle>/status/<id>），过滤保留路径段。"""
    return sorted({
        f"x.com/{m[0]}/status/{m[1]}"
        for m in X_LINK_RE.findall(text or "")
        if m[0].lower() not in X_RESERVED_PATHS
    })


def _first_text(elem, *paths):
    for p in paths:
        node = elem.find(p)
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    return ""


def parse_feed(content):
    """兼容 RSS 2.0 与 Atom。返回 [{title, url, published, summary, _fulltext}]。
    _fulltext 是 description + content:encoded（或 summary + content）的合并，供链接挖掘用——
    RSS 的 description 常只是摘要，推文链接在全文字段里。"""
    # 去掉所有默认命名空间声明，统一处理（带前缀的声明如 xmlns:atom 不受影响）
    content = re.sub(rb'xmlns="[^"]+"', b"", content)
    root = ET.fromstring(content)
    items = []
    entries = root.findall(".//item")
    if entries:  # RSS 2.0
        for it in entries:
            desc = _first_text(it, "description")
            enc = _first_text(it, "{http://purl.org/rss/1.0/modules/content/}encoded")
            items.append({
                "title": _first_text(it, "title"),
                "url": _first_text(it, "link"),
                "published": _first_text(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date"),
                "summary": desc or enc,
                "_fulltext": " ".join(t for t in (desc, enc) if t),
            })
    else:  # Atom
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for it in root.findall(".//a:entry", ns) or root.findall(".//entry"):
            link_el = it.find("a:link", ns) if it.find("a:link", ns) is not None else it.find("link")
            href = link_el.get("href", "") if link_el is not None else ""
            def t(tag):
                el = it.find(f"a:{tag}", ns)
                if el is None:
                    el = it.find(tag)
                return (el.text or "").strip() if el is not None and el.text else ""
            summ, cont = t("summary"), t("content")
            items.append({
                "title": t("title"),
                "url": href,
                "published": t("updated") or t("published"),
                "summary": summ or cont,
                "_fulltext": " ".join(x for x in (summ, cont) if x),
            })
    return items


def fetch_rss(src):
    resp = http_get(src["url"], accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
    items = parse_feed(resp.content)
    for it in items:
        full = it.pop("_fulltext", "") or it.get("summary") or ""
        if src.get("mine_x_links"):
            it["x_links"] = mine_x_links(full)
        it["summary"] = strip_tags(it["summary"])[:2000]
    # 兜底：全文字段也没有链接时，对最近 3 条抓正文页提取（发布后的页面内容不变，正常情况下用不到）
    if src.get("mine_x_links"):
        for it in items[:3]:
            if it.get("x_links") or not it.get("url"):
                continue
            try:
                it["x_links"] = mine_x_links(http_get(it["url"]).text)
            except Exception:
                pass  # 单页失败不影响信源整体
    return items


def fetch_hn(src):
    since = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    min_points = src.get("min_points", 30)
    seen, items = set(), []
    for q in src.get("queries", []):
        resp = http_get(
            f"{src['url']}?query={requests.utils.quote(q)}&tags=story"
            f"&numericFilters=points>={min_points},created_at_i>{since}&hitsPerPage=20"
        )
        for hit in resp.json().get("hits", []):
            oid = hit.get("objectID")
            if oid in seen:
                continue
            seen.add(oid)
            items.append({
                "title": hit.get("title") or "",
                "url": f"https://news.ycombinator.com/item?id={oid}",
                "external_url": hit.get("url") or "",
                "published": hit.get("created_at") or "",
                "summary": f"{hit.get('points', 0)} points, {hit.get('num_comments', 0)} comments",
                "matched_query": q,
            })
    return items


def fetch_hf_models(src):
    resp = http_get(src["url"])
    items = []
    for m in resp.json():
        mid = m.get("id") or m.get("modelId") or ""
        items.append({
            "title": mid,
            "url": f"https://huggingface.co/{mid}",
            "published": m.get("lastModified") or m.get("createdAt") or "",
            "summary": f"likes={m.get('likes')} downloads={m.get('downloads')} trending={m.get('trendingScore')}",
        })
    return items


def fetch_hf_papers(src):
    resp = http_get(src["url"])
    items = []
    for p in resp.json():
        paper = p.get("paper", p)
        pid = paper.get("id", "")
        items.append({
            "title": paper.get("title") or "",
            "url": f"https://huggingface.co/papers/{pid}",
            "published": p.get("publishedAt") or paper.get("publishedAt") or "",
            "summary": strip_tags(paper.get("summary") or "")[:1000],
        })
    return items


def fetch_github_repos(src):
    resp = http_get(src["url"], accept="application/vnd.github+json")
    items = []
    for r in resp.json():
        items.append({
            "title": r.get("full_name") or "",
            "url": r.get("html_url") or "",
            "published": r.get("pushed_at") or "",
            "summary": r.get("description") or "",
        })
    return items


def _load_snapshot_state():
    p = STATE_DIR / "html_snapshots.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] html_snapshots.json 损坏，重建快照状态")
    return {}


def _save_snapshot_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "html_snapshots.json.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_DIR / "html_snapshots.json")


def fetch_html_stub(src):
    """页面快照 + 跨日哈希比对：内容未变的快照标 changed=false，digest 不收入简报。"""
    resp = http_get(src["url"])
    text = re.sub(r"\s+", " ", strip_tags(resp.text))
    sha = hashlib.sha256(resp.content).hexdigest()
    state = _load_snapshot_state()
    prev = state.get(src["id"])
    changed = prev is None or prev.get("sha") != sha
    now = datetime.now(timezone.utc).isoformat()
    if changed:
        state[src["id"]] = {"sha": sha, "last_changed": now}
        _save_snapshot_state(state)
    return [{
        "title": ("[页面有更新] " if changed and prev else "[页面快照] ") + src["name"],
        "url": src["url"],
        "published": now if changed else (prev or {}).get("last_changed", now),
        "summary": text[:5000],
        "content_sha256": sha,
        "changed": changed,
    }]


FETCHERS = {
    "rss": fetch_rss,
    "reddit_rss": fetch_rss,
    "hn_algolia": fetch_hn,
    "hf_models": fetch_hf_models,
    "hf_papers": fetch_hf_papers,
    "github_repos": fetch_github_repos,
    "html_stub": fetch_html_stub,
}


def prune_raw(keep_days, today):
    """data/raw 只保留最近 keep_days 天的日期目录。"""
    raw_root = ROOT / "data" / "raw"
    if not raw_root.exists():
        return []
    cutoff = date.fromisoformat(today) - timedelta(days=keep_days)
    removed = []
    for d in raw_root.iterdir():
        if not d.is_dir():
            continue
        try:
            ddate = date.fromisoformat(d.name)
        except ValueError:
            continue
        if ddate < cutoff:
            shutil.rmtree(d)
            removed.append(d.name)
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--keep-days", type=int, default=45, help="data/raw 保留天数（默认 45）")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    topics_path = ROOT / "config" / "topics.yaml"
    topics_cfg = yaml.safe_load(topics_path.read_text(encoding="utf-8")) if topics_path.exists() else {}

    out_dir = ROOT / "data" / "raw" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)

    log = {"date": args.date, "fetched_at": datetime.now(timezone.utc).isoformat(), "sources": {}}
    ok = failed = 0
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            log["sources"][src["id"]] = {"status": "skipped"}
            continue
        # 检索词从 topics.yaml 派生，模型清单只维护一处
        if src.get("queries_from") == "model_keywords":
            src["queries"] = list(dict.fromkeys(
                (topics_cfg.get("model_keywords") or []) + src.get("queries_extra", [])))
        fetcher = FETCHERS.get(src["type"])
        if fetcher is None:
            log["sources"][src["id"]] = {"status": "error", "error": f"unknown type {src['type']}"}
            failed += 1
            continue
        try:
            items = fetcher(src)
            payload = {
                "source": src["id"], "name": src["name"], "tier": src["tier"],
                "injection_warning": src.get("injection_warning", False),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "items": items,
            }
            (out_dir / f"{src['id']}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            log["sources"][src["id"]] = {"status": "ok", "items": len(items)}
            print(f"[ok]   {src['id']}: {len(items)} items")
            ok += 1
        except Exception as e:
            log["sources"][src["id"]] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            print(f"[fail] {src['id']}: {type(e).__name__}: {e}")
            failed += 1

    (out_dir / "_fetch_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    pruned = prune_raw(args.keep_days, args.date)
    if pruned:
        print(f"已清理 {len(pruned)} 个过期 raw 目录：{', '.join(pruned)}")
    print(f"\n{ok} ok, {failed} failed -> {out_dir}")
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
