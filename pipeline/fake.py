"""Offline stand-ins for every external API, used when XRPTERM_FAKE=1 (python -m pipeline.selftest)."""
from __future__ import annotations

import datetime as dt
import json
import math
import zlib
from urllib.parse import parse_qs, urlparse

from .util import UTC, HttpError, iso, now_utc


class FakeResponse:
    def __init__(self, body, status=200):
        self.status_code = status
        self.headers = {}
        if isinstance(body, (dict, list)):
            self.text = json.dumps(body)
        else:
            self.text = body
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


def respond(method, url, **kw):
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    q.update({k: str(v) for k, v in (kw.get("params") or {}).items()})
    if u.netloc == "api.x.ai":
        return FakeResponse(_xai(kw.get("json") or {}))
    if u.netloc == "api.exchange.coinbase.com":
        return FakeResponse(_candles(u.path, q))
    if u.netloc == "open-api-v4.coinglass.com":
        return FakeResponse(_coinglass(u.path, q))
    if "xrpl.org" in u.netloc:
        raise HttpError("HTTP 404 from fake feed", 404)
    return FakeResponse(_rss(u.netloc))


def _ago(h):
    return iso(now_utc() - dt.timedelta(hours=h))


def _usage(posts=0, web=0):
    return {"input_tokens": 9000, "input_tokens_details": {"cached_tokens": 2000}, "output_tokens": 1200,
            "server_side_tool_usage_details": {"x_posts_fetched": posts, "x_users_fetched": 0, "web_search_calls": web}}


def _wrap(obj, cites, usage):
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(obj), "annotations": []}]}],
            "citations": cites, "usage": usage}


def _xai(body):
    name = body["text"]["format"]["name"]
    tools = body.get("tools") or [{}]
    if name == "posts":
        allowed = tools[0].get("allowed_x_handles")
        if allowed and "JSeyff" in allowed:
            posts = [{"url": "https://x.com/JSeyff/status/1975000000000000001", "handle": "JSeyff", "author_name": "James Seyffart",
                      "posted_at": _ago(5), "text": "Spot XRP ETFs saw $18.2M net inflows yesterday, 4th straight day of inflows.",
                      "quoted_text": None, "links": []},
                     {"url": "https://x.com/JSeyff/status/1975000000000000099", "handle": "JSeyff", "author_name": "James Seyffart",
                      "posted_at": _ago(3), "text": "This post was invented by the model and has no citation.", "quoted_text": None, "links": []}]
            cites = ["https://x.com/JSeyff/status/1975000000000000001"]
        elif allowed and "whale_alert" in allowed:
            posts = [{"url": "https://x.com/whale_alert/status/1975000000000000002", "handle": "whale_alert", "author_name": "Whale Alert",
                      "posted_at": _ago(9), "text": "150,000,000 #XRP transferred from unknown wallet to Bitstamp", "quoted_text": None, "links": []}]
            cites = [posts[0]["url"]]
        elif allowed:
            posts, cites = [], []
        else:
            posts = [{"url": "https://x.com/i/status/1975000000000000003", "handle": "someaccount", "author_name": "Some Account",
                      "posted_at": _ago(2), "text": "XRP to $100 by Christmas, banks are loading up!!", "quoted_text": None, "links": []}]
            cites = ["https://x.com/someaccount/status/1975000000000000003"]
        return _wrap({"posts": posts}, cites, _usage(posts=12))
    if name == "articles":
        a = [{"url": "https://www.reuters.com/markets/ripple-test-article", "title": "Ripple expands payments license footprint",
              "outlet": "Reuters", "published_at": _ago(6), "description": "Ripple received an additional state money transmitter license."}]
        return _wrap({"articles": a}, [a[0]["url"]], _usage(web=2))
    if name == "ratings":
        payload = json.loads(body["input"][1]["content"])
        items = []
        for c in payload["CANDIDATES"]:
            txt = (c.get("text") or c.get("title") or "").lower()
            spec = "$100" in txt
            whale = "transferred" in txt
            etf = "etf" in txt
            items.append({
                "cid": c["cid"], "keep": True, "duplicate_of": None,
                "headline": (c.get("title") or c.get("text") or "")[:100],
                "summary": "Test summary written by the fake rater.",
                "themes": ["spec"] if spec else ["sup"] if whale else ["etf"] if etf else ["pay", "reg"],
                "status": "speculative" if spec else "confirmed" if (whale or etf) else "reported",
                "source_tier": "crowd" if spec else "t1",
                "direction": "bullish" if not whale else "bearish",
                "horizon": "intraday" if whale else "structural",
                "novelty": "new", "size": "large" if whale else "moderate" if etf else "small",
                "scope": "direct",
                "factors": [{"t": "+", "label": "test factor"}, {"t": "-", "label": "test caveat"}],
                "rationale": "Fake rationale.", "reality_check": "Fake reality check."})
        return _wrap({"items": items}, [], _usage())
    if name == "states":
        payload = json.loads(body["input"][1]["content"])
        return _wrap({"states": [{"theme": t, "state": f"Test state for {t}."} for t in payload["themes"]]}, [], _usage())
    if name == "accounts":
        txt = body["input"][1]["content"]
        h = [x.strip().lstrip("@") for x in txt.split("Handles:")[-1].split(",")]
        return _wrap({"accounts": [{"handle": x, "status": "unclear" if x == "xrpl_commons" else "found", "display_name": x,
                                    "description": "test"} for x in h]}, [], _usage())
    raise HttpError("unknown fake xai request", 400)


def _candles(path, q):
    g = int(q.get("granularity", 300))
    if q.get("start"):
        a = int(dt.datetime.fromisoformat(q["start"]).timestamp())
        b = int(dt.datetime.fromisoformat(q["end"]).timestamp())
    else:
        b = int(now_utc().timestamp()) // g * g
        a = b - 299 * g
    base = 2.3 if "XRP" in path else 62000.0
    rows = []
    t = a // g * g
    nowt = int(now_utc().timestamp())
    while t <= b and t <= nowt:
        p = base * (1 + 0.03 * math.sin(t / 20000) + (0.01 * math.sin(t / 3000) if "XRP" in path else 0))
        rows.append([t, p * 0.998, p * 1.002, p, p * 1.0005, 1.5e6 if g == 86400 else 4e3])
        t += g
    return list(reversed(rows))


def _coinglass(path, q):
    if path.endswith("/etf/xrp/flow-history"):
        rows = []
        day = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
        for i in range(45, 0, -1):
            d = day - dt.timedelta(days=i)
            if d.weekday() >= 5:
                continue
            f = [("XRPC", 2e6), ("XRPZ", 4e6 + i * 1e5), ("XRP", 3e6), ("GXRP", -1e6 if i % 7 == 0 else 1e6), ("TOXR", 0)]
            rows.append({"timestamp": int(d.timestamp() * 1000), "flow_usd": sum(v for _, v in f), "price_usd": 2.3,
                         "etf_flows": [{"etf_ticker": t, "flow_usd": v} if v else {"etf_ticker": t} for t, v in f]})
        # today, not reported yet: per-fund figures missing
        rows.append({"timestamp": int(day.timestamp() * 1000), "flow_usd": 0, "price_usd": 2.3,
                     "etf_flows": [{"etf_ticker": t} for t in ("XRPC", "XRPZ", "XRP", "GXRP", "TOXR")]})
        return {"code": "0", "data": rows}
    n = int(q.get("limit", 180))
    t0 = int(now_utc().timestamp() * 1000)
    if "funding" in path:
        return {"code": "0", "data": [{"time": t0 - i * 14400000, "open": "0.0001", "high": "0.0002", "low": "0.0", "close": str(0.0001 + (i % 9) * 1e-5)} for i in range(n)][::-1]}
    return {"code": "0", "data": [{"time": t0 - i * 14400000, "open": 1e9, "high": 1e9, "low": 1e9, "close": 1.6e9 - i * 1e6} for i in range(n)][::-1]}


def _rss(host):
    items = [("XRP Ledger adds new amendment for tokenized funds", 4), ("Bitcoin miners report record hashrate", 3)]
    body = "".join(f"<item><title>{t}</title><link>https://{host}/a/{zlib.crc32(t.encode()) % 10**6}</link>"
                   f"<description>{t}. More details.</description><pubDate>{(now_utc() - dt.timedelta(hours=h)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
                   for t, h in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>{host}</title>{body}</channel></rss>'
