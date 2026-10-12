"""Links between Claim Tracker claims and feed items (news and X) about the same specific thing.

Three kinds, all stored on the claim as {"item", "rel", "via"}:
- cited: the item is the source the claim check cited (rel follows the verdict)
- same_post: the claim's post is itself in the feed
- match: Grok matched them as the same event, announcement or figure (rel: supports / contradicts / context)
Links are only added, never rewritten, so a later run can't flip an earlier one."""
from __future__ import annotations

import datetime as dt
import json
import re

from . import claims as claims_mod
from .news import norm_url
from .util import iso, log, now_utc, parse_iso
from .xai import BudgetExceeded, Grok

RELS = ["supports", "contradicts", "context"]
WINDOW_DAYS = 7
STATUS = re.compile(r"/status(?:es)?/(\d+)")

SCHEMA = {
    "type": "object",
    "properties": {"links": {"type": "array", "items": {"type": "object", "properties": {
        "claim": {"type": "string"}, "item": {"type": "string"}, "rel": {"type": "string", "enum": RELS}},
        "required": ["claim", "item", "rel"], "additionalProperties": False}}},
    "required": ["links"], "additionalProperties": False,
}

SYSTEM = (
    "You connect claims that X accounts made about XRP to items in a trading terminal's news and X feed. Link a claim and an item "
    "only when both are about the same specific event, announcement, filing, figure or development (the same listing, the same "
    "bug, the same partnership, the same flow number). Sharing a broad topic like 'XRP price', 'adoption' or 'ETFs' is not enough.\n"
    "rel: supports = the item reports facts that back the claim; contradicts = the item reports facts that conflict with it; "
    "context = the same event, but the item neither backs nor refutes the claim (for example a prediction and news about the event "
    "it builds on).\n"
    "contradicts needs facts that can't both be true with the claim. An item that is older, less complete, or calls something planned "
    "or unconfirmed does not contradict a later claim that it happened; that is context. Check the times: each entry has one (UTC).\n"
    "Only return pairs where the claim or the item is marked new. Use the ids exactly as given. Most claims link to 0-3 items; "
    "return an empty list if nothing fits."
)


def _status_id(url: str | None) -> str | None:
    m = STATUS.search(url or "")
    return m.group(1) if m else None


def _rel_for_verdict(v: str) -> str:
    return {"supported": "supports", "false": "contradicts", "misleading": "contradicts"}.get(v, "context")


def _add(c: dict, item: str, rel: str, via: str) -> bool:
    links = c.setdefault("links", [])
    if any(l["item"] == item for l in links):
        return False
    links.append({"item": item, "rel": rel, "via": via})
    return True


def deterministic(claims: dict, items: list[dict]) -> int:
    """Cited sources and same posts, matched by URL or X status id."""
    by_url = {norm_url(i["source"]["url"]): i["id"] for i in items if (i.get("source") or {}).get("url")}
    ids = {i["id"] for i in items}
    n = 0
    for c in claims.values():
        same = "x:" + c["id"].split(":", 1)[1]
        if same in ids:
            n += _add(c, same, "context", "same_post")
        ev = c.get("evidence_url")
        if ev:
            hit = by_url.get(norm_url(ev)) or (("x:" + _status_id(ev)) if _status_id(ev) and ("x:" + _status_id(ev)) in ids else None)
            if hit and hit != same:
                n += _add(c, hit, _rel_for_verdict(c["verdict"]), "cited")
    return n


def run(grok: Grok, state: dict, evs: dict) -> int:
    """Match new claims and new feed items from the last 7 days. Returns the number of links added."""
    cut = now_utc() - dt.timedelta(days=WINDOW_DAYS)
    last = parse_iso(state.get("last_link_pass")) or cut
    started = now_utc()
    claims = claims_mod.load()
    recent_c = {k: c for k, c in claims.items() if (parse_iso(c["published_at"]) or cut) > cut}
    items = [e for e in evs.values() if (parse_iso(e["published_at"]) or cut) > cut]
    added = deterministic(recent_c, items)

    new_c = [c for c in recent_c.values() if (parse_iso(c.get("logged_at")) or cut) > last]
    new_i = [e for e in items if (parse_iso(e.get("logged_at")) or cut) > last]
    if recent_c and items and (new_c or new_i) and grok.available:
        new_ids = {c["id"] for c in new_c} | {e["id"] for e in new_i}
        payload = {
            "claims": [{"id": c["id"], **({"new": True} if c["id"] in new_ids else {}), "time": c["published_at"][:16].replace("T", " "),
                        "claim": c["claim"], "verdict": c["verdict"]} for c in sorted(recent_c.values(), key=lambda c: c["published_at"])],
            "items": [{"id": e["id"], **({"new": True} if e["id"] in new_ids else {}), "time": e["published_at"][:16].replace("T", " "),
                       "headline": e["headline"], "summary": (e.get("summary") or "")[:220]}
                      for e in sorted(items, key=lambda e: e["published_at"])],
        }
        try:
            data, _ = grok.respond(system=SYSTEM, user=json.dumps(payload, ensure_ascii=False), schema=SCHEMA, name="links",
                                   label="claim-links", effort="low")
        except BudgetExceeded:
            raise
        except Exception as e:
            log.warning("claim linking failed: %s", e)
            data = None
        if data is not None:
            item_ids = {e["id"] for e in items}
            dropped = 0
            for l in data.get("links") or []:
                c = recent_c.get(l.get("claim"))
                if not c or l.get("item") not in item_ids or l.get("rel") not in RELS:
                    dropped += 1
                    continue
                if l["claim"] not in new_ids and l["item"] not in new_ids:
                    continue                              # pairs of two old entries were already considered
                added += _add(c, l["item"], l["rel"], "match")
            if dropped:
                log.info("claim links: dropped %d with unknown ids", dropped)
            state["last_link_pass"] = iso(started)
    elif not (new_c or new_i):
        state["last_link_pass"] = iso(started)
    if added:
        claims.update(recent_c)
        claims_mod.save(claims)
    log.info("claim links: %d added (%d new claims, %d new items considered)", added, len(new_c), len(new_i))
    return added
