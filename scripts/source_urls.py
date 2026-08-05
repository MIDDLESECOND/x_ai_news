# -*- coding: utf-8 -*-
"""Canonical public-source URL normalization shared by write boundaries."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DROP_QUERY_PREFIXES = ("utm_",)
DROP_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_url(value: str) -> str:
    value = (value or "").strip()
    parts = urlsplit(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError("url 必须是可点的 http/https 原始出处")
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port:
        netloc += f":{parts.port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in DROP_QUERY_KEYS
             and not any(k.lower().startswith(p) for p in DROP_QUERY_PREFIXES)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))
