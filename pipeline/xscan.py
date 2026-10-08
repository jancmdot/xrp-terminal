"""X retrieval through Grok's x_search tool. Retrieval only: no rating happens here."""
from __future__ import annotations

import datetime as dt
import re

from .util import iso, log, now_utc, parse_iso
from .xai import BudgetExceeded, Grok

STATUS_RE = re.compile(r"(?:x|twitter)\.com/(?:i/web/|i/)?(?:([A-Za-z0-9_]{1,15})/)?status(?:es)?/(\d+)", re.I)

POSTS_SCHEMA = {
    "type": "object",
    "properties": {
        "posts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "handle": {"type": "string"},
                    "author_name": {"type": "string"},
                    "posted_at": {"type": "string"},
                    "text": {"type": "string"},
                    "quoted_text": {"type": ["string", "null"]},
                    "links": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["url", "handle", "author_name", "posted_at", "text", "quoted_text", "links"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["posts"],
    "additionalProperties": False,
}

RETRIEVE_SYSTEM = (
    "You retrieve X posts for a market-data pipeline. Use the x_search tool. "
    "Return posts exactly as written: do not summarize, rate, or add opinions. "
    "Only return posts you actually found in search results. Never invent posts, handles, times or URLs. "
    "Post URLs must have the form https://x.com/<handle>/status/<id>. Times are ISO 8601 in UTC."
)

TOPIC = ("XRP, Ripple, the XRP Ledger (XRPL), RLUSD, spot XRP ETFs, or regulation, court, exchange "
         "or market events that directly affect XRP")


def status_id(url: str) -> str | None:
    m = STATUS_RE.search(url or "")
    return m.group(2) if m else None


def groups_from_config(handles_cfg: dict) -> list[dict]:
    out = []
    for g in handles_cfg.get("groups") or []:
        tiers = {}
        for h in g.get("handles") or []:
            tiers[h.lstrip("@")] = g.get("tier", "t1")
        for h in g.get("t1_handles") or []:
            tiers[h.lstrip("@")] = "t1"
        for h in g.get("primary_handles") or []:
            tiers[h.lstrip("@")] = "primary"
        if len(tiers) > 20:
            raise ValueError(f"Handle group '{g.get('name')}' has {len(tiers)} accounts; x_search allows 20 per request")
        if tiers:
            out.append({"name": g.get("name", "group"), "tiers": tiers})
    return out


def _window(since: dt.datetime) -> dict:
    # x_search date filters are whole UTC days. Setting from_date and to_date to the same day returned no
    # posts at all (Oct 8 run), so start a day early and leave the end open; posted_at is filtered below.
    return {"from_date": (since - dt.timedelta(days=1)).strftime("%Y-%m-%d")}


def _clean(posts: list[dict], cites: list[str], since: dt.datetime, label: str) -> list[dict]:
    cited_ids = {status_id(u) for u in cites} - {None}
    kept, dropped = [], 0
    for p in posts:
        sid = status_id(p.get("url", ""))
        t = parse_iso(p.get("posted_at"))
        if not sid:
            dropped += 1
            continue
        if cited_ids and sid not in cited_ids:
            dropped += 1          # not backed by an actual search result
            continue
        if t and t < since - dt.timedelta(hours=1):
            continue
        handle = (p.get("handle") or "").lstrip("@")
        m = STATUS_RE.search(p["url"])
        if m and m.group(1) and m.group(1).lower() != "i":
            handle = m.group(1)
        kept.append({
            "sid": sid, "url": f"https://x.com/{handle or 'i'}/status/{sid}", "handle": handle,
            "author_name": p.get("author_name") or handle, "posted_at": iso(t) if t else iso(now_utc()),
            "text": (p.get("text") or "").strip(), "quoted_text": p.get("quoted_text"),
            "links": [l for l in (p.get("links") or []) if isinstance(l, str)][:5],
            "verified_citation": bool(cited_ids),
        })
    if dropped:
        log.info("%s: dropped %d posts without a matching search citation", label, dropped)
    log.info("%s: %d posts returned, %d kept, %d citations", label, len(posts), len(kept), len(cites))
    return kept


def signal_scan(grok: Grok, handles_cfg: dict, since: dt.datetime) -> list[dict]:
    """Every relevant post from the curated account groups."""
    out = []
    for g in groups_from_config(handles_cfg):
        handles = list(g["tiers"])
        froms = " OR ".join(f"from:{h}" for h in handles)
        user = (f"Find every post from these accounts posted after {iso(since)} that is about {TOPIC}. "
                f"Accounts: {', '.join('@' + h for h in handles)}. "
                "Use x_keyword_search with X search operators, for example: "
                f"`(XRP OR Ripple OR XRPL OR RLUSD OR ETF) ({froms}) since:{since.strftime('%Y-%m-%d')}`, in Latest mode. "
                "Split the accounts across a few searches if a query gets too long. "
                "Skip replies that add no information and plain reposts. If there are none, return an empty list.")
        try:
            data, info = grok.respond(system=RETRIEVE_SYSTEM, user=user, schema=POSTS_SCHEMA, name="posts",
                                      tools=[{"type": "x_search", "allowed_x_handles": handles, **_window(since)}],
                                      label=f"x:{g['name'][:16]}")
        except BudgetExceeded:
            raise
        except Exception as e:  # one failed group shouldn't stop the scan
            log.warning("signal group %s failed: %s", g["name"], e)
            continue
        for p in _clean(data.get("posts") or [], info["citations"], since, g["name"]):
            tier = g["tiers"].get(p["handle"]) or next((t for h, t in g["tiers"].items() if h.lower() == p["handle"].lower()), "t1")
            out.append({**p, "kind": "x", "source_tier": tier, "pass": "signal", "group": g["name"]})
    return out


def discovery_scan(grok: Grok, handles_cfg: dict, since: dt.datetime, max_posts: int) -> list[dict]:
    """Open search for XRP news from accounts outside the curated list."""
    hours = max(1, round((now_utc() - since).total_seconds() / 3600))
    blocked = [h.lstrip("@") for h in (handles_cfg.get("blocked") or [])][:20]
    tool = {"type": "x_search", **_window(since)}
    if blocked:
        tool["excluded_x_handles"] = blocked
    user = (f"Find up to {max_posts} posts from the last {hours} hours that report new, checkable information affecting XRP: "
            "ETF flows or filings, regulation or court actions, Ripple corporate news, XRP Ledger adoption, RLUSD, "
            "large on-chain movements, exchange listings or delistings, bank or payment partnerships. "
            "Prefer posts from the organization involved or posts that link to a primary source. "
            "Skip price predictions, chart analysis, giveaways and engagement bait. "
            "Use x_keyword_search in Latest mode with queries such as `XRP ETF`, `Ripple partnership`, `XRPL`, `RLUSD`, "
            f"`XRP SEC`, each with `min_faves:20 since:{since.strftime('%Y-%m-%d')}`, and x_semantic_search for XRP news. "
            "If there are none, return an empty list.")
    try:
        data, info = grok.respond(system=RETRIEVE_SYSTEM, user=user, schema=POSTS_SCHEMA, name="posts",
                                  tools=[tool], label="x:discovery")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("discovery scan failed: %s", e)
        return []
    return [{**p, "kind": "x", "source_tier": None, "pass": "discovery"}
            for p in _clean(data.get("posts") or [], info["citations"], since, "discovery")]


ACCOUNTS_SCHEMA = {
    "type": "object",
    "properties": {"accounts": {"type": "array", "items": {"type": "object", "properties": {
        "handle": {"type": "string"},
        "status": {"type": "string", "enum": ["found", "not_found", "unclear"]},
        "display_name": {"type": ["string", "null"]}, "description": {"type": ["string", "null"]}},
        "required": ["handle", "status", "display_name", "description"], "additionalProperties": False}}},
    "required": ["accounts"], "additionalProperties": False,
}

VERIFY_SYSTEM = ("You check X account identities for a data pipeline. Look each handle up with an X user search "
                 "(not a post search). Only report what the profile results show. "
                 "status: found = the profile came back; not_found = you searched and no such account exists; "
                 "unclear = you couldn't check it.")


def _verify_batch(grok: Grok, handles: list[str], label: str) -> dict:
    user = ("Look up each of these X handles with a user search and report its display name and a one-line description "
            f"of who runs it, from the profile bio. Handles: {', '.join('@' + h for h in handles)}")
    data, _ = grok.respond(system=VERIFY_SYSTEM, user=user, schema=ACCOUNTS_SCHEMA, name="accounts",
                           tools=[{"type": "x_search"}], label=label)
    return {a["handle"].lstrip("@").lower(): a for a in data.get("accounts") or []}


def verify_handles(grok: Grok, handles_cfg: dict) -> list[dict]:
    """Confirm each configured handle exists and who it belongs to: small batches, then a one-by-one retry."""
    order = [(g["name"], h) for g in groups_from_config(handles_cfg) for h in g["tiers"]]
    found: dict[str, dict] = {}
    handles = [h for _, h in order]
    for i in range(0, len(handles), 6):
        try:
            found.update(_verify_batch(grok, handles[i:i + 6], f"verify:{i // 6 + 1}"))
        except BudgetExceeded:
            raise
        except Exception as e:
            log.warning("verify batch %d failed: %s", i // 6 + 1, e)
    retry = [h for h in handles if (found.get(h.lower()) or {}).get("status") != "found"]
    if len(retry) <= 15:
        for h in retry:
            try:
                found.update(_verify_batch(grok, [h], f"verify:@{h[:12]}"))
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning("verify @%s failed: %s", h, e)
    rows = []
    for group, h in order:
        a = found.get(h.lower()) or {"status": "unclear", "display_name": None, "description": "no answer"}
        rows.append({"group": group, "handle": h, "status": a.get("status"), "exists": a.get("status") == "found",
                     "display_name": a.get("display_name"), "description": a.get("description")})
    return rows
