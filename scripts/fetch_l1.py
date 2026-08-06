# -*- coding: utf-8 -*-
"""L1 全自动抓取：读 config/sources.yaml，逐个信源拉取并归一化，落盘 data/raw/YYYY-MM-DD/。

零 X 风险：全部免登录公开端点。单个信源失败只记录日志，不影响其他信源。
页面快照（html_stub）带跨日可见正文哈希与可读性状态（data/state/html_snapshots.json）——
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
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
import yaml

from http_fetch_state import (FetchCooldown, discard_cached_response,
                              host_cooldown, host_lease_key, prepare_request,
                              prune_cache, record_failure, record_host_cooldown,
                              request_lease, resolve_policy,
                              retry_after_seconds, store_success)
from reddit_rate_limit import is_reddit_url, reserve_request

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "data" / "state"
HTTP_CACHE_ROOT = STATE_DIR / "http_cache"
UA = "frontier-radar/0.1 (personal news pipeline; +https://github.com/MIDDLESECOND/x_ai_news)"
TIMEOUT = 30
HTTP_FORCE_REVALIDATE = False
HTTP_LOGICAL_DAY = None
_HTTP_EVENTS = []

X_LINK_RE = re.compile(r"https?://(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")
HTTP_URL_RE = re.compile(r"https?://[^\s<>'\"&]+", re.IGNORECASE)
HREF_RE = re.compile(r"\bhref\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
# X 的保留路径段（x.com/i/status/... 等非用户名路径），挖掘时即过滤
X_RESERVED_PATHS = {"i", "search", "home", "intent", "hashtag", "explore", "share"}
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

if sys.stdout:  # pythonw / stdout 分离的无控制台运行下 sys.stdout 为 None
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def take_http_events():
    """Return and clear request-level observations for the current source."""
    events = list(_HTTP_EVENTS)
    _HTTP_EVENTS.clear()
    return events


def summarize_http_events(events):
    counts = {}
    for event in events:
        status = str(event.get("cache_status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    attempts = sum(int(event.get("network_attempts") or 0) for event in events)
    network_successes = sum(
        1 for event in events
        if int(event.get("network_attempts") or 0) > 0
        and event.get("cache_status") != "error")
    errors = counts.get("error", 0)
    deferred = counts.get("deferred", 0)
    usable_responses = sum(
        count for status, count in counts.items()
        if status not in {"error", "deferred", "unknown"})
    successes = sorted(
        str(event["last_network_success_at"])
        for event in events if event.get("last_network_success_at"))
    if (errors or deferred) and usable_responses:
        source_status = "partial"
    elif errors:
        source_status = "error"
    elif network_successes:
        source_status = "ok"
    elif counts.get("stale_backoff"):
        source_status = "stale"
    elif counts.get("deferred"):
        source_status = "deferred"
    elif events:
        source_status = "cached"
    else:
        source_status = "ok"
    return {
        "source_status": source_status,
        "network_attempts": attempts,
        "network_successes": network_successes,
        "network_errors": errors,
        "usable_responses": usable_responses,
        "cache_statuses": counts,
        "last_network_success_at": successes[-1] if successes else None,
    }


class ResponseValidationError(requests.RequestException):
    """A nominally successful response is not parseable as the requested format."""


class ResponseTooLarge(requests.RequestException):
    """A response exceeded the configured decoded-body transport ceiling."""


def close_response(response):
    """Release a streamed response, tolerating test/custom adapters without raw."""
    try:
        response.close()
    except (AttributeError, OSError):
        pass


def validate_http_response(response, response_kind):
    if response_kind == "json":
        try:
            response.json()
        except ValueError as error:
            raise ResponseValidationError("HTTP 200 正文不是有效 JSON") from error
    elif response_kind == "xml":
        try:
            ET.fromstring(response.content)
        except ET.ParseError as error:
            raise ResponseValidationError("HTTP 200 正文不是有效 XML") from error


def materialize_response_body(response, max_bytes):
    """Read a streamed response into memory without ever crossing *max_bytes*."""
    limit = max(0, int(max_bytes))
    preloaded = getattr(response, "_content", False)
    if not isinstance(response, requests.Response):
        candidate = getattr(response, "content", b"")
        preloaded = candidate if isinstance(candidate, (bytes, bytearray)) else preloaded
    if isinstance(preloaded, (bytes, bytearray)):
        body = bytes(preloaded)
        if len(body) > limit:
            close_response(response)
            raise ResponseTooLarge(
                f"响应正文 {len(body)} 字节超过上限 {limit} 字节",
                response=response)
        response._content = body
        return response

    declared = response.headers.get("Content-Length")
    try:
        declared_size = int(declared) if declared is not None else None
    except (TypeError, ValueError):
        declared_size = None
    if declared_size is not None and declared_size > limit:
        close_response(response)
        raise ResponseTooLarge(
            f"Content-Length {declared_size} 字节超过上限 {limit} 字节",
            response=response)

    chunks = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise ResponseTooLarge(
                    f"流式正文超过上限 {limit} 字节", response=response)
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response._content_consumed = True
        return response
    finally:
        close_response(response)


def http_get(url, accept=None, *, source=None, cache_root=None, now=None,
             logical_day=None, force_revalidate=None, response_kind=None):
    """GET with persistent validators, bounded freshness reuse, and hard Reddit gate.

    Cached responses are normal ``requests.Response`` objects, so fetchers keep
    parsing exactly the same bytes.  Every actual network attempt still calls
    ``reserve_request``; a cache hit performs no Reddit request and consumes no slot.
    """
    cache_root = Path(cache_root) if cache_root is not None else HTTP_CACHE_ROOT
    max_download_bytes = resolve_policy(source)["max_download_bytes"]
    force_revalidate = (HTTP_FORCE_REVALIDATE if force_revalidate is None
                        else force_revalidate)
    logical_day = logical_day or HTTP_LOGICAL_DAY
    prepare_kwargs = {
        "source": source, "now": now, "logical_day": logical_day,
        "force_revalidate": force_revalidate,
    }
    prepared = prepare_request(cache_root, url, accept, **prepare_kwargs)
    ready = _prepared_response(
        url, prepared, cache_root, now, logical_day, response_kind,
        max_download_bytes)
    if ready is not None:
        return ready
    try:
        with request_lease(cache_root, prepared.key) as key_lease:
            # Another process may have completed the same request while this one
            # waited for the lease.  Re-read state before touching the network.
            refreshed_kwargs = dict(prepare_kwargs)
            if key_lease:
                # A completed identical forced revalidation satisfies this waiter.
                refreshed_kwargs["force_revalidate"] = False
            prepared = prepare_request(cache_root, url, accept, **refreshed_kwargs)
            ready = _prepared_response(
                url, prepared, cache_root, now, logical_day, response_kind,
                max_download_bytes)
            if ready is not None:
                return ready
            return _network_http_get(
                url, accept, source, cache_root, now, logical_day, prepared,
                key_lease, response_kind)
    except FetchCooldown as error:
        if not (_HTTP_EVENTS and _HTTP_EVENTS[-1].get("url") == url
                and _HTTP_EVENTS[-1].get("cache_status") == "deferred"):
            _HTTP_EVENTS.append({
                "url": url, "cache_status": "deferred", "network_attempts": 0,
                "error": type(error).__name__,
            })
        raise


def _prepared_response(url, prepared, cache_root, now, logical_day, response_kind,
                       max_download_bytes):
    if prepared.cached_response is not None:
        try:
            materialize_response_body(
                prepared.cached_response, max_download_bytes)
            validate_http_response(prepared.cached_response, response_kind)
        except (ResponseValidationError, ResponseTooLarge) as error:
            discard_cached_response(
                cache_root, prepared, now=now, logical_day=logical_day,
                error=f"{type(error).__name__}: {error}")
            return None
        _HTTP_EVENTS.append({
            "url": url,
            "cache_status": prepared.cached_response.frontier_cache_status,
            "network_attempts": 0,
            "last_network_success_at": getattr(
                prepared.cached_response, "frontier_last_network_success_at", None),
        })
        return prepared.cached_response
    if prepared.deferred_until:
        _HTTP_EVENTS.append({
            "url": url, "cache_status": "deferred", "network_attempts": 0,
            "retry_at": prepared.deferred_until,
        })
        raise FetchCooldown(
            f"请求仍在失败冷却期（至 {prepared.deferred_until}）"
            + (f"：{prepared.deferred_error}" if prepared.deferred_error else ""))
    return None


def _sleep_with_lease_heartbeat(seconds, *leases):
    remaining = max(0.0, float(seconds))
    while remaining:
        for lease in leases:
            lease.heartbeat()
        chunk = min(60.0, remaining)
        time.sleep(chunk)
        remaining -= chunk
    for lease in leases:
        lease.heartbeat()


def _network_http_get(url, accept, source, cache_root, now, logical_day, prepared,
                      key_lease, response_kind):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    headers.update(prepared.headers)
    attempts = 0
    retry_after_observed = 0
    retry_after_waited = 0
    resp = None
    retried_429 = False
    max_download_bytes = resolve_policy(source)["max_download_bytes"]

    delay_before = max(0.0, float((source or {}).get("delay_before") or 0))
    if delay_before and not is_reddit_url(url):
        # Apply source pacing only after cache/cooldown checks prove that this
        # invocation will really touch the network. Reddit uses only its shared
        # hard gate, so a disabled policy can never be preceded by this delay.
        _sleep_with_lease_heartbeat(delay_before, key_lease)

    def request_hop(request_url, request_headers):
        nonlocal attempts, retry_after_observed, retry_after_waited, retried_429
        with request_lease(cache_root, host_lease_key(request_url)) as host_lease:
            blocked_until, blocked_error = host_cooldown(
                cache_root, request_url, now=now)
            if blocked_until:
                raise FetchCooldown(
                    f"同一源站仍在冷却期（至 {blocked_until}）：{blocked_error}")

            def gated_request():
                nonlocal attempts
                reserve_request(
                    request_url,
                    sleep_fn=lambda seconds: _sleep_with_lease_heartbeat(
                        seconds, key_lease, host_lease))
                attempts += 1
                return requests.get(
                    request_url, headers=request_headers, timeout=TIMEOUT,
                    allow_redirects=False, stream=True)

            try:
                response = gated_request()
            except (requests.ConnectionError, requests.Timeout):
                # 部分公开 feed 偶发在 TLS 握手或首包阶段断开；一次短重试即可区分瞬时故障与持续阻断。
                _sleep_with_lease_heartbeat(1, key_lease, host_lease)
                response = gated_request()
            if response.status_code == 429 and not retried_429:
                wait = retry_after_seconds(response.headers, now) or 30
                retry_after_observed = max(retry_after_observed, wait)
                retried_429 = True
                if wait <= 60:
                    close_response(response)
                    _sleep_with_lease_heartbeat(wait, key_lease, host_lease)
                    retry_after_waited += wait
                    response = gated_request()
                    if response.status_code == 429:
                        retry_after_observed = max(
                            retry_after_observed,
                            retry_after_waited
                            + (retry_after_seconds(response.headers, now) or 30))
            if response.status_code in {429, 503}:
                final_retry_after = max(
                    retry_after_observed,
                    retry_after_waited
                    + retry_after_seconds(response.headers, now))
                if final_retry_after:
                    record_host_cooldown(
                        cache_root, request_url,
                        retry_after=final_retry_after, now=now,
                        error=f"HTTP {response.status_code}")
            return response

    def request_chain(start_url, initial_headers):
        current_url = start_url
        current_headers = dict(initial_headers)
        history = []
        for redirect_count in range(6):
            response = request_hop(current_url, current_headers)
            location = response.headers.get("Location")
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.history = history
                return response
            if not location:
                close_response(response)
                raise requests.HTTPError(
                    f"重定向响应缺少 Location：{current_url}", response=response)
            if redirect_count >= 5:
                close_response(response)
                raise requests.TooManyRedirects(
                    f"重定向超过 5 跳：{url}", response=response)
            history.append(response)
            current_url = urljoin(response.url or current_url, location)
            close_response(response)
            if urlsplit(current_url).scheme.lower() not in {"http", "https"}:
                raise requests.InvalidURL(f"不支持的重定向目标：{current_url}")
            # Validators describe the original selected representation and must
            # not leak to or incorrectly validate a redirect target.
            current_headers.pop("If-None-Match", None)
            current_headers.pop("If-Modified-Since", None)
        raise requests.TooManyRedirects(f"重定向超过 5 跳：{url}")

    try:
        resp = request_chain(url, headers)
        # 元数据尚在但不可变 body 被清理时，304 无法还原响应；只在这种损坏场景无条件补取一次。
        if resp.status_code == 304:
            not_modified = resp
            restored = store_success(
                cache_root, prepared, resp, source=source, now=now,
                logical_day=logical_day)
            close_response(not_modified)
            if restored is None:
                headers.pop("If-None-Match", None)
                headers.pop("If-Modified-Since", None)
                resp = request_chain(url, headers)
            else:
                resp = restored
        if getattr(resp, "frontier_cache_status", "") != "revalidated":
            resp.raise_for_status()
            materialize_response_body(resp, max_download_bytes)
            try:
                validate_http_response(resp, response_kind)
            except ResponseValidationError as error:
                discard_cached_response(
                    cache_root, prepared, now=now, logical_day=logical_day,
                    error=f"{type(error).__name__}: {error}")
                raise
            resp = store_success(
                cache_root, prepared, resp, source=source, now=now,
                logical_day=logical_day)
        else:
            try:
                materialize_response_body(resp, max_download_bytes)
                validate_http_response(resp, response_kind)
            except ResponseTooLarge as error:
                # A 304 proves the oversized cached representation is unchanged;
                # an unconditional second download would only waste bandwidth.
                discard_cached_response(
                    cache_root, prepared, now=now, logical_day=logical_day,
                    error=f"{type(error).__name__}: {error}")
                raise
            except ResponseValidationError as error:
                discard_cached_response(
                    cache_root, prepared, now=now, logical_day=logical_day,
                    error=f"{type(error).__name__}: {error}")
                headers.pop("If-None-Match", None)
                headers.pop("If-Modified-Since", None)
                resp = request_chain(url, headers)
                resp.raise_for_status()
                materialize_response_body(resp, max_download_bytes)
                validate_http_response(resp, response_kind)
                resp = store_success(
                    cache_root, prepared, resp, source=source, now=now,
                    logical_day=logical_day)
        _HTTP_EVENTS.append({
            "url": url,
            "cache_status": getattr(resp, "frontier_cache_status", "network"),
            "network_attempts": attempts,
            "last_network_success_at": getattr(
                resp, "frontier_last_network_success_at", None),
        })
        return resp
    except FetchCooldown:
        raise
    except Exception as error:
        status_code = getattr(resp, "status_code", None)
        validation_error = isinstance(error, ResponseValidationError)
        oversized = isinstance(error, ResponseTooLarge)
        transport_error = isinstance(error, (
            requests.ConnectionError, requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError))
        if oversized:
            discard_cached_response(
                cache_root, prepared, now=now, logical_day=logical_day,
                error=f"{type(error).__name__}: {error}")
        retryable = (validation_error or oversized or transport_error
                     or status_code is None or status_code in {
            408, 425, 429, 500, 502, 503, 504})
        allow_stale = (not validation_error and not oversized and (
            transport_error or status_code is None
            or status_code in {500, 502, 503, 504}))
        retry_after = max(
            retry_after_observed,
            (retry_after_waited
             + retry_after_seconds(getattr(resp, "headers", {}), now))
            if resp is not None else 0)
        record_failure(
            cache_root, prepared, source=source, now=now,
            logical_day=logical_day, retry_after=retry_after,
            retryable=retryable, allow_stale=allow_stale,
            error=f"{type(error).__name__}: {error}",
            host_url=getattr(resp, "url", None) or url)
        _HTTP_EVENTS.append({
            "url": url, "cache_status": "error",
            "network_attempts": attempts, "error": type(error).__name__,
        })
        if resp is not None:
            close_response(resp)
        raise


def strip_tags(html):
    return TAG_RE.sub(" ", html or "").strip()


def visible_html_text(value):
    """移除脚本、样式与标签，供页面探针和只在内存使用的审计正文共用。"""
    return re.sub(r"\s+", " ", strip_tags(SCRIPT_STYLE_RE.sub(" ", value or ""))).strip()


def mine_x_links(text):
    """从任意文本提取推文链接（归一化为 x.com/<handle>/status/<id>），过滤保留路径段。"""
    return sorted({
        f"x.com/{m[0]}/status/{m[1]}"
        for m in X_LINK_RE.findall(text or "")
        if m[0].lower() not in X_RESERVED_PATHS
    })


def extract_http_links(text, base_url=""):
    """在清理 RSS HTML 前保留正文外链；页面内容只作为数据解析。"""
    decoded = unescape(text or "")
    candidates = list(HREF_RE.findall(decoded)) + list(HTTP_URL_RE.findall(decoded))
    links = set()
    for candidate in candidates:
        resolved = urljoin(base_url, candidate.strip()).rstrip(".,);]}")
        try:
            parts = urlsplit(resolved)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and parts.hostname:
            links.add(resolved)
    return sorted(links)


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
    resp = http_get(src["url"], accept="application/rss+xml, application/atom+xml, application/xml, text/xml",
                    source=src, response_kind="xml")
    items = parse_feed(resp.content)
    for it in items:
        full = it.pop("_fulltext", "") or it.get("summary") or ""
        it["external_urls"] = extract_http_links(full, it.get("url", ""))
        if src.get("audit_fulltext"):
            # 仅供影子审计在本次进程内派生特征；调用方落盘前必须删除该临时字段。
            limit = int(src.get("audit_fulltext_char_limit", 100_000))
            it["_audit_fulltext"] = strip_tags(full)[:limit]
        if src.get("mine_x_links"):
            it["x_links"] = mine_x_links(full)
        it["summary"] = strip_tags(it["summary"])[:2000]
    # 兜底：全文字段也没有链接时，对最近 3 条抓正文页提取（发布后的页面内容不变，正常情况下用不到）
    if src.get("mine_x_links"):
        for it in items[:3]:
            if it.get("x_links") or not it.get("url"):
                continue
            try:
                it["x_links"] = mine_x_links(http_get(it["url"], source=src).text)
            except Exception:
                pass  # 单页失败不影响信源整体
    return items


def iter_query_json(src, build_url, accept=None):
    """按检索词逐个请求并解析 JSON —— 两个「抓取阶段就按关键词收敛」的信源共用此形状。

    两条纪律：①词间留间隔，无间隔连打十余次会撞限流（Algolia 无鉴权、GitHub 搜索
    未鉴权仅 10 次/分）；②单个检索词失败只跳过该词。http_get 对 429 只退避重试一次，
    再失败就抛；解析失败（限流页/挑战页返回 200 但正文非 JSON）同样会抛。
    过去这两种异常都会穿出整个 fetcher，让当天该信源的全部检索词一起归零。
    """
    delay = src.get("query_delay", 1)
    for i, q in enumerate(src.get("queries", [])):
        if i:
            time.sleep(delay)
        try:
            # .json() 必须在 try 内：requests 的 JSONDecodeError 同时继承 ValueError
            yield q, http_get(
                build_url(q), accept=accept, source=src,
                response_kind="json").json()
        except (requests.RequestException, ValueError) as e:
            print(f"[warn] {src.get('id', '?')} 检索词 {q!r} 失败"
                  f"（{type(e).__name__}），跳过该词", flush=True)
            continue


def fetch_hn(src):
    since = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    min_points = src.get("min_points", 30)
    seen, items = set(), []

    def url(q):
        return (f"{src['url']}?query={requests.utils.quote(q)}&tags=story"
                f"&numericFilters=points>={min_points},created_at_i>{since}&hitsPerPage=20")

    for q, data in iter_query_json(src, url):
        for hit in data.get("hits", []):
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
    resp = http_get(src["url"], source=src, response_kind="json")
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
    resp = http_get(src["url"], source=src, response_kind="json")
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
    resp = http_get(
        src["url"], accept="application/vnd.github+json", source=src,
        response_kind="json")
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


def content_excerpt(text, required_patterns, limit=5000):
    """保留页首语境，并优先收录必要内容模式周围的正文。"""
    if len(text) <= limit:
        return text
    if not required_patterns:
        return text[:limit]

    matches = sorted(
        (match.start(), match.end())
        for pattern in required_patterns
        for match in re.finditer(pattern, text, re.IGNORECASE)
    )
    if not matches:
        return text[:limit]

    # 价格符号通常跟在套餐/模型名称之后，向前保留更多上下文；相邻价格行合并成一段。
    intervals = []
    for start, end in matches:
        span = (max(0, start - 320), min(len(text), end + 560))
        if intervals and span[0] <= intervals[-1][1] + 80:
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], span[1]))
        else:
            intervals.append(span)

    head = text[:600].rstrip()
    output = head
    for start, end in intervals:
        snippet = text[start:end].strip()
        if not snippet or snippet in output:
            continue
        separator = "\n… [必要内容片段] …\n"
        available = limit - len(output) - len(separator)
        if available <= 0:
            break
        output += separator + snippet[:available]
        if len(snippet) > available:
            break
    return output[:limit]


def fetch_html_stub(src):
    """对稳定的可见正文做跨日比对，并显式标记抓取盲区。

    原始 HTML 包含构建号、脚本与 hydration 数据，直接哈希会把页面骨架变化误报为
    内容更新。可选的 content_required_patterns 用于确认关键正文确实抓到；不可读状态
    只在首次发现或恢复时进入简报，不能被表述成价格/发布变化。官方 Markdown 端点
    可声明 content_format=markdown，保留组件属性中的表格数据，不按 HTML 标签剥离。
    """
    resp = http_get(src["url"], source=src)
    if resp.encoding in (None, "ISO-8859-1"):  # 无 charset 头时 requests 默认 latin-1，中文站会乱码
        resp.encoding = resp.apparent_encoding or "utf-8"
    if src.get("content_format") == "markdown":
        text = re.sub(r"\s+", " ", resp.text).strip()
    else:
        text = visible_html_text(resp.text)
    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    response_sha = hashlib.sha256(resp.content).hexdigest()
    required = src.get("content_required_patterns") or []
    readable = all(re.search(pattern, text, re.IGNORECASE) for pattern in required)
    state = load_state("html_snapshots")
    prev = state.get(src["id"])
    prev_readable = prev.get("readable") if prev else None
    prev_content_sha = prev.get("content_sha") if prev else None
    if prev is None:
        changed = True
    elif not readable:
        changed = prev_readable is not False
    elif prev_readable is False:
        changed = True
    elif prev_content_sha is None:
        # 旧状态只保存原始 HTML 哈希。升级当天静默建立正文基线，避免迁移伪更新。
        changed = False
    else:
        changed = prev_content_sha != content_sha
    now = datetime.now(timezone.utc).isoformat()
    if prev is None or changed or prev_content_sha is None:
        state[src["id"]] = {
            "sha": content_sha,  # 兼容仍读取旧字段的本地工具
            "content_sha": content_sha,
            "response_sha": response_sha,
            "readable": readable,
            "last_changed": now if changed else (prev or {}).get("last_changed", now),
        }
        save_state("html_snapshots", state)
    if not readable:
        title = "[页面不可读] " + src["name"]
        summary = "抓取正文未命中必要内容模式；本条只表示探针盲区，不表示页面内容发生变化。 " + text
    elif prev_readable is False:
        title = "[页面恢复可读] " + src["name"]
        summary = text
    else:
        title = (("[页面有更新] " if changed and prev else "[页面快照] ")
                 + src["name"])
        summary = text
    summary = content_excerpt(summary, required)
    return [{
        "title": title,
        "url": src["url"],
        "published": now if changed else (prev or {}).get("last_changed", now),
        "summary": summary,
        "content_sha256": content_sha,
        "response_sha256": response_sha,
        "changed": changed,
        "readable": readable,
    }]


def fetch_github_search(src):
    """GitHub 仓库检索：只报最近 N 天新建的匹配仓库（发现新审计/雷达/基准项目）。
    活跃老仓库不重复报——发现管道要的是'新出现'，不是'又更新'。"""
    days = src.get("created_within_days", 14)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    reported = load_state("gh_audit_seen")  # 每个仓库只报第一次发现，不连报 14 天
    seen, items = set(), []

    def url(q):
        return (f"{src['url']}?q={requests.utils.quote(f'{q} created:>={since}')}"
                f"&sort=updated&per_page=5")

    for q, data in iter_query_json(src, url, accept="application/vnd.github+json"):
        for r in data.get("items", []):
            if r["full_name"] in seen or r["full_name"] in reported:
                continue
            seen.add(r["full_name"])
            reported[r["full_name"]] = datetime.now(timezone.utc).date().isoformat()
            items.append({
                "title": f"[新审计/基准项目] {r['full_name']}（★{r['stargazers_count']}）",
                "url": r["html_url"],
                "published": r.get("created_at", ""),
                "summary": (r.get("description") or "")[:300] + f"（检索词：{q}）",
            })
    save_state("gh_audit_seen", reported)
    return items


def fetch_yahoo_chart(src):
    """港股/美股行情（Yahoo chart API）→ 单条行情条目。"""
    res = http_get(
        src["url"], source=src, response_kind="json").json()["chart"]["result"][0]
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
    d = http_get(src["url"], source=src, response_kind="json").json()
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
    data = http_get(
        src["url"], source=src, response_kind="json").json().get("data", [])
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
    validate_price_snapshot("OpenRouter", state, tracked)
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
    return items


def _price_match_text(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def validate_price_snapshot(name, previous, current, *, min_retained_ratio=0.5):
    """Fail closed before an empty or structurally collapsed snapshot is saved."""
    if not current:
        raise RuntimeError(f"{name} 返回空价格快照，拒绝覆盖既有基线")
    if not previous:
        return
    retained = len(set(previous) & set(current))
    ratio = retained / len(previous)
    if ratio < min_retained_ratio:
        raise RuntimeError(
            f"{name} 价格快照仅保留旧基线 {retained}/{len(previous)} 个模型 "
            f"({ratio:.1%})，疑似上游或关键词异常，拒绝覆盖"
        )


def extract_genai_price_snapshot(data, model_keywords):
    """Extract tracked model price records from pydantic/genai-prices v2.

    The returned snapshot deliberately preserves conditional/tiered price
    structures.  It is an aggregator observation, never a replacement for the
    provider's official pricing page.
    """
    if not isinstance(data, list):
        raise ValueError("genai-prices v2 顶层必须是 provider 数组")
    keywords = [_price_match_text(keyword) for keyword in model_keywords if str(keyword).strip()]
    snapshot = {}
    for provider in data:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "").strip()
        provider_name = str(provider.get("name") or provider_id).strip()
        if not provider_id:
            continue
        pricing_urls = provider.get("pricing_urls") or []
        pricing_url = next((str(value) for value in pricing_urls if value), "")
        for model in provider.get("models") or []:
            if not isinstance(model, dict) or model.get("prices") is None:
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            model_name = str(model.get("name") or model_id).strip()
            haystack = _price_match_text(
                f"{provider_id} {provider_name} {model_id} {model_name}")
            if keywords and not any(keyword in haystack for keyword in keywords):
                continue
            key = f"{provider_id}/{model_id}"
            snapshot[key] = {
                "provider_id": provider_id,
                "provider_name": provider_name,
                "model_id": model_id,
                "model_name": model_name,
                "context_window": model.get("context_window"),
                "prices": model["prices"],
                "pricing_url": pricing_url,
            }
    return snapshot


def _compact_prices(prices):
    if isinstance(prices, list):
        groups = []
        for index, entry in enumerate(prices, start=1):
            if not isinstance(entry, dict):
                groups.append(f"组{index}: {entry}")
                continue
            constraint = entry.get("constraint") or {}
            label = (json.dumps(constraint, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":"))
                     if constraint else "default")
            groups.append(f"{label}: {_compact_prices(entry.get('prices'))}")
        return " | ".join(groups)
    if not isinstance(prices, dict):
        return str(prices)
    preferred = ["input_mtok", "cache_read_mtok", "cache_write_mtok", "output_mtok"]
    keys = [key for key in preferred if key in prices]
    keys.extend(sorted(key for key in prices if key not in preferred))
    parts = [
        f"{key}=${prices[key]}/M" if key.endswith("_mtok") else f"{key}={prices[key]}"
        for key in keys
    ]
    return ", ".join(parts)


def _genai_record_url(provider_id, model_id):
    quoted = requests.utils.quote(str(provider_id), safe="")
    model = requests.utils.quote(str(model_id), safe="")
    return ("https://github.com/pydantic/genai-prices/blob/main/"
            f"prices/providers/{quoted}.yml?model={model}&plain=1")


def diff_genai_price_snapshots(previous, current, published):
    """Return only new models and exact structured price changes."""
    items = []
    for key, record in sorted(current.items()):
        old = previous.get(key)
        url = _genai_record_url(record["provider_id"], record["model_id"])
        boundary = "聚合价格索引的结构化记录；不得替代厂商官方价格页"
        if old is None:
            items.append({
                "title": (f"[genai-prices] new model: {record['model_name']} "
                          f"（{_compact_prices(record['prices'])}）"),
                "url": url,
                "published": published,
                "summary": f"{boundary}；厂商价目参考：{record.get('pricing_url') or '未提供'}",
            })
        elif old.get("prices") != record.get("prices"):
            items.append({
                "title": (f"[genai-prices] price change: {record['model_name']} "
                          f"{_compact_prices(old.get('prices'))} → "
                          f"{_compact_prices(record.get('prices'))}"),
                "url": url,
                "published": published,
                "summary": f"{boundary}；厂商价目参考：{record.get('pricing_url') or '未提供'}",
            })
    return items


def fetch_genai_prices(src):
    """pydantic/genai-prices v2 → tracked structured-price diff."""
    data = http_get(
        src["url"], accept="application/json", source=src,
        response_kind="json").json()
    snapshot = extract_genai_price_snapshot(
        data, src.get("_model_keywords") or [])
    state = load_state("genai_prices")
    validate_price_snapshot("genai-prices", state, snapshot)
    now = datetime.now(timezone.utc).isoformat()
    if state:
        items = diff_genai_price_snapshots(state, snapshot, now)
    else:
        items = [{
            "title": f"[genai-prices] pricing 基线已建立，追踪 {len(snapshot)} 个模型",
            "url": "https://github.com/pydantic/genai-prices",
            "published": now,
            "summary": "聚合价格索引基线；后续只报新模型与结构化价格变化，不替代厂商官方价目",
        }]
    save_state("genai_prices", snapshot)
    return items


def fetch_codexradar(src):
    """CodexRadar 每日固定任务集（112 任务/档）。IQ 相比上次变动超阈值的档位逐条报告；
    另出一条当日快照。数据仅私有研究引用，不再分发（见其 README 授权说明）。"""
    d = http_get(src["url"], source=src, response_kind="json").json()
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
    "github_search": fetch_github_search,
    "yahoo_chart": fetch_yahoo_chart,
    "statuspage": fetch_statuspage,
    "openrouter_prices": fetch_openrouter_prices,
    "genai_prices": fetch_genai_prices,
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
    global HTTP_FORCE_REVALIDATE, HTTP_LOGICAL_DAY
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--keep-days", type=int, default=45, help="data/raw 保留天数（默认 45）")
    ap.add_argument("--only", default="", help="只抓这些信源（逗号分隔的 id，测试用）")
    ap.add_argument(
        "--refresh-http", action="store_true",
        help="忽略本地新鲜期并立即向源站复核；仍发送条件头，且不能绕过 Reddit 硬闸门")
    args = ap.parse_args()
    HTTP_FORCE_REVALIDATE = args.refresh_http
    HTTP_LOGICAL_DAY = args.date
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    topics_path = ROOT / "config" / "topics.yaml"
    topics_cfg = yaml.safe_load(topics_path.read_text(encoding="utf-8")) if topics_path.exists() else {}

    out_dir = ROOT / "data" / "raw" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)

    log = {"date": args.date, "fetched_at": datetime.now(timezone.utc).isoformat(), "sources": {}}
    ok = partial = failed = 0
    for src in cfg["sources"]:
        if only and src["id"] not in only:
            continue
        if not src.get("enabled", True):
            log["sources"][src["id"]] = {"status": "skipped"}
            continue
        # 检索词/追踪清单从 topics.yaml 派生，模型清单只维护一处
        if src.get("queries_from") == "model_keywords":
            src["queries"] = list(dict.fromkeys(
                (topics_cfg.get("model_keywords") or []) + src.get("queries_extra", [])))
        if src["type"] in ("openrouter_prices", "genai_prices"):
            src["_model_keywords"] = topics_cfg.get("model_keywords") or []
        fetcher = FETCHERS.get(src["type"])
        if fetcher is None:
            log["sources"][src["id"]] = {"status": "error", "error": f"unknown type {src['type']}"}
            failed += 1
            continue
        try:
            take_http_events()  # 清除上一个信源的请求观察
            items = fetcher(src)
            http_summary = summarize_http_events(take_http_events())
            kif = src.get("keep_if_contains")  # 高频低信噪信源（如 llama.cpp CI 构建）按关键词过滤
            if kif:
                items = [it for it in items
                         if any(t.lower() in f"{it.get('title', '')} {it.get('summary', '')}".lower()
                                for t in kif)]
            payload = {
                "source": src["id"], "name": src["name"], "tier": src["tier"],
                "injection_warning": src.get("injection_warning", False),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "retrieval": {
                    key: value for key, value in http_summary.items()
                    if key != "source_status"
                },
                "items": items,
            }
            (out_dir / f"{src['id']}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            source_status = http_summary["source_status"]
            log["sources"][src["id"]] = {
                "status": source_status, "items": len(items),
                "http": payload["retrieval"],
            }
            label = {
                "cached": "cache", "stale": "stale", "deferred": "defer",
                "partial": "part", "error": "fail",
            }.get(source_status, "ok")
            print(f"[{label:<5}] {src['id']}: {len(items)} items")
            if source_status == "error":
                failed += 1
            elif source_status == "partial":
                partial += 1
            else:
                ok += 1
        except Exception as e:
            events = take_http_events()
            log["sources"][src["id"]] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            if events:
                log["sources"][src["id"]]["http"] = summarize_http_events(events)
            print(f"[fail] {src['id']}: {type(e).__name__}: {e}")
            failed += 1

    if not only:  # --only 是局部测试，不覆盖全量日志、不触发保留清理
        (out_dir / "_fetch_log.json").write_text(
            json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        pruned = prune_raw(args.keep_days, args.date)
        if pruned:
            print(f"已清理 {len(pruned)} 个过期 raw 目录：{', '.join(pruned)}")
        cache_pruned = prune_cache(HTTP_CACHE_ROOT, max_age_days=args.keep_days)
        if any(cache_pruned.values()):
            print("已清理 HTTP 缓存："
                  f"{cache_pruned['removed_entries']} 个过期索引、"
                  f"{cache_pruned['removed_bodies']} 个孤儿正文")
    print(f"\n{ok} ok, {partial} partial, {failed} failed -> {out_dir}")
    return 0 if ok + partial > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
