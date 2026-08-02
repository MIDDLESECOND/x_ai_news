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
import time
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
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

if sys.stdout:  # pythonw / stdout 分离的无控制台运行下 sys.stdout 为 None
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def http_get(url, accept=None):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 429:  # 限流：按 Retry-After（上限 60s）退避一次
        wait = min(int(resp.headers.get("Retry-After", 30) or 30), 60)
        time.sleep(wait)
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


def load_state(name):
    """data/state/<name>.json —— 跨日状态（快照哈希、价格基线、雷达 IQ 等）。损坏时重建。"""
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"[warn] state/{name}.json 损坏，重建")
    return {}


def save_state(name, obj):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f"{name}.json.tmp"
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_DIR / f"{name}.json")


def fetch_html_stub(src):
    """页面快照 + 跨日哈希比对：内容未变的快照标 changed=false，digest 不收入简报。"""
    resp = http_get(src["url"])
    if resp.encoding in (None, "ISO-8859-1"):  # 无 charset 头时 requests 默认 latin-1，中文站会乱码
        resp.encoding = resp.apparent_encoding or "utf-8"
    text = re.sub(r"\s+", " ", strip_tags(SCRIPT_STYLE_RE.sub(" ", resp.text)))
    sha = hashlib.sha256(resp.content).hexdigest()
    state = load_state("html_snapshots")
    prev = state.get(src["id"])
    changed = prev is None or prev.get("sha") != sha
    now = datetime.now(timezone.utc).isoformat()
    if changed:
        state[src["id"]] = {"sha": sha, "last_changed": now}
        save_state("html_snapshots", state)
    return [{
        "title": ("[页面有更新] " if changed and prev else "[页面快照] ") + src["name"],
        "url": src["url"],
        "published": now if changed else (prev or {}).get("last_changed", now),
        "summary": text[:5000],
        "content_sha256": sha,
        "changed": changed,
    }]


def fetch_yahoo_chart(src):
    """港股/美股行情（Yahoo chart API）→ 单条行情条目。"""
    res = http_get(src["url"]).json()["chart"]["result"][0]
    meta = res["meta"]
    sym = meta.get("symbol", "")
    price = meta.get("regularMarketPrice")
    closes = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
    valid = [c for c in closes if c is not None]
    # 日变动以序列中最近两个收盘为基准（meta 的 chartPreviousClose 是区间前收，跨多日会失真）
    chg = f"{(valid[-1] - valid[-2]) / valid[-2] * 100:+.2f}%" if len(valid) >= 2 else "n/a"
    days = "，".join(
        f"{datetime.fromtimestamp(t, timezone.utc).date()}: {c:.2f}"
        for t, c in zip(res.get("timestamp") or [], closes) if c is not None)
    return [{
        "title": f"{src['name']}：{price}（较上一交易日 {chg}）",
        "url": f"https://finance.yahoo.com/quote/{sym}",
        "published": datetime.now(timezone.utc).isoformat(),
        "summary": f"symbol={sym} last={price}；近 5 日收盘：{days}",
    }]


def fetch_statuspage(src):
    """statuspage.io 标准 summary.json → 每个未解决事故一条；全绿则 0 条（不是错误）。"""
    d = http_get(src["url"]).json()
    items = []
    for inc in d.get("incidents", []):
        upd = (inc.get("incident_updates") or [{}])[0].get("body", "")
        items.append({
            "title": f"[服务事故] {src['name']}: {inc.get('name', '')}（{inc.get('impact', '')}/{inc.get('status', '')}）",
            "url": inc.get("shortlink") or d.get("page", {}).get("url", src["url"]),
            "published": inc.get("started_at") or inc.get("created_at") or "",
            "summary": strip_tags(upd)[:500],
        })
    ind = d.get("status", {}).get("indicator")
    if not items and ind not in (None, "none"):
        items.append({
            "title": f"[服务事故] {src['name']}: {d['status'].get('description', ind)}",
            "url": d.get("page", {}).get("url", src["url"]),
            "published": datetime.now(timezone.utc).isoformat(),
            "summary": f"indicator={ind}",
        })
    return items


def fetch_openrouter_prices(src):
    """OpenRouter 全模型牌价 → 与上次基线 diff，只报变动/新上架（首跑建基线）。
    只追踪命中 topics.yaml model_keywords 的模型。"""
    data = http_get(src["url"]).json().get("data", [])
    kws = [k.lower() for k in src.get("_model_keywords", [])]
    tracked = {}
    for m in data:
        mid = m.get("id") or ""
        name = m.get("name") or mid
        hay = f"{mid.lower()} {name.lower()}"
        if not any(k in hay for k in kws):
            continue
        pr = m.get("pricing") or {}
        try:
            tracked[mid] = {"name": name,
                            "prompt": round(float(pr.get("prompt") or 0) * 1e6, 4),
                            "completion": round(float(pr.get("completion") or 0) * 1e6, 4)}
        except (TypeError, ValueError):
            continue
    state = load_state("openrouter_prices")
    now = datetime.now(timezone.utc).isoformat()
    items = []
    if not state:
        items.append({
            "title": f"[OpenRouter] pricing 基线已建立，追踪 {len(tracked)} 个模型牌价",
            "url": "https://openrouter.ai/models", "published": now,
            "summary": "首日快照；此后任何被追踪模型的输入/输出牌价变动将逐条列出",
        })
    else:
        for mid, cur in tracked.items():
            old = state.get(mid)
            if old is None:
                items.append({
                    "title": f"[OpenRouter] new listing: {cur['name']}（${cur['prompt']:.2f}/${cur['completion']:.2f} per M）",
                    "url": f"https://openrouter.ai/{mid}", "published": now,
                    "summary": "新模型上架 OpenRouter（发布信号）",
                })
            elif (cur["prompt"], cur["completion"]) != (old.get("prompt"), old.get("completion")):
                items.append({
                    "title": (f"[OpenRouter] pricing change: {cur['name']} "
                              f"${old.get('prompt')}/${old.get('completion')} → "
                              f"${cur['prompt']}/${cur['completion']} per M"),
                    "url": f"https://openrouter.ai/{mid}", "published": now,
                    "summary": "输入/输出牌价（每百万 token）变动",
                })
    save_state("openrouter_prices", tracked)
    return items[:20]


def fetch_codexradar(src):
    """CodexRadar 每日固定任务集（112 任务/档）。IQ 相比上次变动超阈值的档位逐条报告；
    另出一条当日快照。数据仅私有研究引用，不再分发（见其 README 授权说明）。"""
    d = http_get(src["url"]).json()
    pts = d.get("points") or []
    updated = d.get("source_updated_at") or datetime.now(timezone.utc).isoformat()
    state = load_state("codexradar")
    threshold = src.get("iq_delta_threshold", 2.0)
    items, new_state = [], {}
    for p in pts:
        iq = p.get("iq")
        if iq is None:
            continue
        key = f"{p.get('model')}|{p.get('effort')}"
        new_state[key] = iq
        old = state.get(key)
        if old is not None and abs(iq - old) >= threshold:
            items.append({
                "title": f"[CodexRadar] {p['model']} {p['effort']}: IQ {old:.1f}→{iq:.1f}（{iq - old:+.1f}）",
                "url": "https://codexradar.com",
                "published": updated,
                "summary": (f"passed {p.get('passed')}/{p.get('valid_tasks')}；"
                            f"均价 ${p.get('average_price_usd', 0):.3f}/任务；"
                            f"均时 {p.get('average_minutes', 0):.1f} 分钟"),
            })
    def best_line(model):
        rows = [p for p in pts if p.get("model") == model and p.get("iq") is not None]
        if not rows:
            return None
        top = max(rows, key=lambda p: p["iq"])
        return f"{model} 最高档 IQ {top['iq']:.1f}（${top.get('average_price_usd', 0):.2f}/任务）"
    lines = [x for x in (best_line(m) for m in src.get("summary_models", [])) if x]
    if lines:
        items.append({
            "title": "[CodexRadar] 每日固定任务集快照：" + "；".join(lines),
            "url": "https://codexradar.com",
            "published": updated,
            "summary": f"112 任务同 harness；源更新于 {updated}",
        })
    save_state("codexradar", new_state)
    return items


FETCHERS = {
    "rss": fetch_rss,
    "reddit_rss": fetch_rss,
    "hn_algolia": fetch_hn,
    "hf_models": fetch_hf_models,
    "hf_papers": fetch_hf_papers,
    "github_repos": fetch_github_repos,
    "html_stub": fetch_html_stub,
    "yahoo_chart": fetch_yahoo_chart,
    "statuspage": fetch_statuspage,
    "openrouter_prices": fetch_openrouter_prices,
    "codexradar": fetch_codexradar,
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
        # 检索词/追踪清单从 topics.yaml 派生，模型清单只维护一处
        if src.get("queries_from") == "model_keywords":
            src["queries"] = list(dict.fromkeys(
                (topics_cfg.get("model_keywords") or []) + src.get("queries_extra", [])))
        if src["type"] == "openrouter_prices":
            src["_model_keywords"] = topics_cfg.get("model_keywords") or []
        if src.get("delay_before"):  # 同域信源限流（如 Reddit 连续请求 429）
            time.sleep(src["delay_before"])
        fetcher = FETCHERS.get(src["type"])
        if fetcher is None:
            log["sources"][src["id"]] = {"status": "error", "error": f"unknown type {src['type']}"}
            failed += 1
            continue
        try:
            items = fetcher(src)
            kif = src.get("keep_if_contains")  # 高频低信噪信源（如 llama.cpp CI 构建）按关键词过滤
            if kif:
                items = [it for it in items
                         if any(t.lower() in f"{it.get('title', '')} {it.get('summary', '')}".lower()
                                for t in kif)]
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
