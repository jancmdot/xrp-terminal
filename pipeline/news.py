"""Articles: RSS feeds plus one Grok web search per scan."""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import re
from urllib.parse import urlparse, urlunparse

import feedparser

from .util import UTC, http, iso, log, now_utc, parse_iso, strip_html
from .xai import BudgetExceeded, Grok

PRIMARY_DOMAINS = {
    "sec.gov", "cftc.gov", "federalreserve.gov", "treasury.gov", "occ.gov", "fdic.gov", "congress.gov", "uscourts.gov",
    "ripple.com", "xrpl.org", "xrplf.org", "bitwiseinvestments.com", "grayscale.com", "21shares.com",
    "franklintempleton.com", "canary.capital", "businesswire.com", "prnewswire.com", "globenewswire.com",
}
T1_DOMAINS = {
    "bloomberg.com", "reuters.com", "wsj.com", "ft.com", "cnbc.com", "apnews.com", "axios.com", "fortune.com",
    "coindesk.com", "theblock.co", "cointelegraph.com", "decrypt.co", "dlnews.com", "blockworks.co", "barrons.com",
}


def domain(url: str) -> str:
    try:
        h = urlparse(url).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def domain_tier(url: str) -> str:
    d = domain(url)
    if any(d == p or d.endswith("." + p) for p in PRIMARY_DOMAINS):
        return "primary"
    if any(d == p or d.endswith("." + p) for p in T1_DOMAINS):
        return "t1"
    return "t2"


def norm_url(url: str) -> str:
    try:
        p = urlparse(url)
    except ValueError:
        return url
    q = "&".join(x for x in (p.query or "").split("&") if x and not x.lower().startswith(("utm_", "ref=", "src=")))
    return urlunparse((p.scheme or "https", (p.netloc or "").lower(), p.path.rstrip("/"), "", q, ""))


def url_id(url: str) -> str:
    return hashlib.sha1(norm_url(url).encode()).hexdigest()[:12]


def matches(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", t) for k in keywords)


def rss_items(feeds_cfg: dict, keywords: list[str], since: dt.datetime) -> tuple[list[dict], list[str]]:
    items, errors = [], []
    for f in feeds_cfg.get("feeds") or []:
        try:
            r = http("GET", f["url"], timeout=20, retries=1)
            parsed = feedparser.parse(r.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError("not a readable feed")
        except Exception as e:
            errors.append(f"{f['name']}: {e}")
            log.warning("feed %s failed: %s", f["name"], e)
            continue
        n = 0
        for e in parsed.entries:
            title = strip_html(e.get("title", ""))
            summary = strip_html(e.get("summary", ""))[:700]
            link = e.get("link") or ""
            if not link or not matches(f"{title} {summary}", keywords):
                continue
            tp = e.get("published_parsed") or e.get("updated_parsed")
            t = dt.datetime.fromtimestamp(calendar.timegm(tp), UTC) if tp else None
            if t and t < since:
                continue
            items.append({"kind": "news", "url": norm_url(link), "uid": url_id(link), "title": title, "text": summary,
                          "outlet": f["name"], "posted_at": iso(t or now_utc()), "source_tier": f.get("tier") or domain_tier(link),
                          "pass": "rss"})
            n += 1
        log.info("feed %-20s %d entries, %d about XRP in the window", f["name"], len(parsed.entries), n)
    return items, errors


ARTICLES_SCHEMA = {
    "type": "object",
    "properties": {"articles": {"type": "array", "items": {"type": "object", "properties": {
        "url": {"type": "string"}, "title": {"type": "string"}, "outlet": {"type": "string"},
        "published_at": {"type": "string"}, "description": {"type": "string"}},
        "required": ["url", "title", "outlet", "published_at", "description"], "additionalProperties": False}}},
    "required": ["articles"], "additionalProperties": False,
}


def web_items(grok: Grok, since: dt.datetime, max_searches: int = 4) -> list[dict]:
    user = (f"Find news articles published after {iso(since)} about XRP, Ripple, the XRP Ledger, RLUSD or spot XRP ETFs, "
            "from established outlets or primary sources such as press releases, filings and regulator sites. "
            f"Run at most {max_searches} web searches and don't open pages unless a result's date or content is unclear. "
            "Skip price-prediction and technical-analysis articles. Return at most 12. For each give the article URL, its title, "
            "the outlet, the publish time in ISO 8601 UTC, and one factual sentence in your own words about what it reports.")
    try:
        data, info = grok.respond(system="You find news articles for a market-data pipeline. Only return articles that appear in your search results. Never invent URLs.",
                                  user=user, schema=ARTICLES_SCHEMA, name="articles",
                                  tools=[{"type": "web_search"}], label="web:news")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("web news search failed: %s", e)
        return []
    cited = {norm_url(u) for u in info["citations"]}
    out = []
    for a in data.get("articles") or []:
        u = norm_url(a.get("url", ""))
        if not u.startswith("http") or (cited and u not in cited):
            continue
        t = parse_iso(a.get("published_at"))
        if t and t < since - dt.timedelta(hours=2):
            continue
        out.append({"kind": "news", "url": u, "uid": url_id(u), "title": a.get("title", ""), "text": a.get("description", ""),
                    "outlet": a.get("outlet") or domain(u), "posted_at": iso(t or now_utc()), "source_tier": domain_tier(u),
                    "pass": "web"})
    return out


def theory_items(grok: Grok, since: dt.datetime, theories: list[dict], max_searches: int = 4) -> list[dict]:
    """Daily search for evidence about the tracked theories, including news that doesn't mention XRP."""
    tt = [t for t in theories if t.get("tracked") and t.get("search")]
    if not tt:
        return []
    lines = "\n".join(f"- {t['claim']} Look for: {t['search']}" for t in tt)
    user = (f"Find news published after {iso(since)} that is evidence for or against these claims about XRP:\n{lines}\n"
            "Prefer primary sources (regulators, banks, SWIFT, Ripple, fund filings, on-chain data providers) and established outlets. "
            f"Run one web search per claim ({max_searches} searches in total), then stop; don't open pages. "
            "Skip opinion pieces and price predictions. Return at most 8 articles. For each give the URL, title, outlet, "
            "publish time in ISO 8601 UTC, and one factual sentence in your own words about what it reports.")
    try:
        data, info = grok.respond(system="You find news articles for a market-data pipeline. Only return articles that appear in your search results. Never invent URLs.",
                                  user=user, schema=ARTICLES_SCHEMA, name="articles",
                                  tools=[{"type": "web_search"}], label="web:theories", effort="low")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("theory evidence search failed: %s", e)
        return []
    cited = {norm_url(u) for u in info["citations"]}
    out = []
    for a in data.get("articles") or []:
        u = norm_url(a.get("url", ""))
        if not u.startswith("http") or (cited and u not in cited):
            continue
        t = parse_iso(a.get("published_at"))
        if t and t < since - dt.timedelta(hours=2):
            continue
        out.append({"kind": "news", "url": u, "uid": url_id(u), "title": a.get("title", ""), "text": a.get("description", ""),
                    "outlet": a.get("outlet") or domain(u), "posted_at": iso(t or now_utc()), "source_tier": domain_tier(u),
                    "pass": "theory"})
    log.info("theory evidence search: %d articles", len(out))
    return out
