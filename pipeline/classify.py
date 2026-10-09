"""Rating: Grok fills in categorical rubric fields; the impact tier is computed here, the same way for every item."""
from __future__ import annotations

import json

from .util import log
from .xai import Grok

THEME_IDS = ["etf", "reg", "tok", "stb", "pay", "col", "corp", "sup", "mac", "spec"]

THEME_GUIDE = """Themes (pick 1-3, most relevant first):
- etf: spot XRP ETF flows, filings, launches, holders, issuer news
- reg: regulation, legislation, court cases, enforcement, regulator statements
- tok: tokenization and real-world assets on the XRP Ledger
- stb: RLUSD and other stablecoins on the XRP Ledger
- pay: payments adoption, bank and remittance partnerships, payment volumes
- col: XRP used as collateral, in lending, prime brokerage, corporate or bank treasuries
- corp: Ripple the company (acquisitions, funding, executives, IPO talk, escrow policy)
- sup: supply and on-chain flows (escrow releases, large transfers, exchange balances, unlocks)
- mac: macro and whole-crypto-market moves affecting XRP through correlation
- spec: speculation, price targets, unsourced theories"""

FIELD_GUIDE = """Fields:
- keep: false if the item is spam, or is about neither XRP, the themes above, nor the theories below. Everything else true.
- duplicate_of: if the item reports the same event as an item in RECENT or an earlier candidate, that id; otherwise null.
- headline: neutral, factual, at most 110 characters, in your own words. No hype words, no emojis.
- summary: 1-2 sentences, your own words, what happened and the key number if any.
- status: confirmed = primary record or verifiable data (filing, on-chain data, official account, flow data);
  reported = established outlet or analyst reporting, not yet on a primary record;
  rumor = unsourced, anonymous or single unverified claim; speculative = opinion, prediction, price target.
- source_tier: primary = the account or publisher IS the source (company, issuer, regulator, on-chain data);
  t1 = established outlet or known analyst; t2 = smaller outlet; crowd = unvetted account.
- direction: likely effect on XRP if true: bullish, bearish, neutral, or mixed.
- horizon: intraday (hours), swing (days to weeks), structural (months or longer).
- novelty: new = not previously known; known = already reported or announced earlier; scheduled = expected event (escrow release, planned date).
- size: large / moderate / small relative to XRP's daily trading volume, ETF assets or float; na if no size applies.
- scope: direct = specifically about XRP or Ripple; indirect = affects XRP through its ledger or ecosystem; market_wide = whole market.
- factors: 2-5 short phrases (max 40 chars) that raised (+) or lowered (-) the rating.
- rationale: 1-2 sentences on why the item matters or doesn't for price.
- reality_check: 1-2 sentences on what the item does NOT show, common misreadings, or what would confirm it."""

SYSTEM = (
    "You rate news and X posts about XRP for a trading terminal that must stay objective. "
    "Rate the information, not the tone: excitement, capital letters and engagement never raise a rating. "
    "Separate what is on record from what is claimed. Be equally skeptical of bullish and bearish claims. "
    "Price targets and predictions are status 'speculative' and theme 'spec'. "
    "Write every text field in your own words; never copy more than a few words from the source.\n\n"
    + THEME_GUIDE + "\n\n" + FIELD_GUIDE
)

ENUM = lambda *v: {"type": "string", "enum": list(v)}
RATING_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "cid": {"type": "string"},
        "keep": {"type": "boolean"},
        "duplicate_of": {"type": ["string", "null"]},
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "themes": {"type": "array", "items": ENUM(*THEME_IDS)},
        "status": ENUM("confirmed", "reported", "rumor", "speculative"),
        "source_tier": ENUM("primary", "t1", "t2", "crowd"),
        "direction": ENUM("bullish", "bearish", "neutral", "mixed"),
        "horizon": ENUM("intraday", "swing", "structural"),
        "novelty": ENUM("new", "known", "scheduled"),
        "size": ENUM("large", "moderate", "small", "na"),
        "scope": ENUM("direct", "indirect", "market_wide"),
        "factors": {"type": "array", "items": {"type": "object", "properties": {
            "t": ENUM("+", "-"), "label": {"type": "string"}}, "required": ["t", "label"], "additionalProperties": False}},
        "rationale": {"type": "string"},
        "reality_check": {"type": "string"},
    }, "required": ["cid", "keep", "duplicate_of", "headline", "summary", "themes", "status", "source_tier", "direction",
                    "horizon", "novelty", "size", "scope", "factors", "rationale", "reality_check"],
       "additionalProperties": False}}},
    "required": ["items"], "additionalProperties": False,
}


def score(r: dict, rubric: dict) -> tuple[int, str]:
    s = (rubric["source"].get(r["source_tier"], 0) + rubric["status"].get(r["status"], 0) +
         rubric["novelty"].get(r["novelty"], 0) + rubric["size"].get(r["size"], 0) + rubric["scope"].get(r["scope"], 0))
    t = rubric["tiers"]
    tier = "high" if s >= t["high"] else "medium" if s >= t["medium"] else "low" if s >= t["low"] else "noise"
    if tier in ("high", "medium") and r["size"] in rubric.get("cap_low_for_size", []):
        tier = "low"          # well-sourced but immaterial: can't move price much on its own
    return s, tier


def rescore(e: dict, rubric: dict) -> None:
    """Recompute a logged event's tier from its stored fields, so rubric changes apply to the whole log."""
    r = {"source_tier": e["source"]["tier"], "status": e["status"], **e["rubric"]}
    e["score"], e["impact"]["tier"] = score(r, rubric)


def _candidate_line(cid: str, c: dict) -> dict:
    d = {"cid": cid, "type": "X post" if c["kind"] == "x" else "article", "time_utc": c["posted_at"]}
    if c["kind"] == "x":
        d["account"] = "@" + c.get("handle", "")
        d["author"] = c.get("author_name")
        if c.get("source_tier"):
            d["account_tier_from_config"] = c["source_tier"]
        d["text"] = c.get("text", "")[:1200]
        if c.get("quoted_text"):
            d["quoting"] = c["quoted_text"][:500]
        if c.get("links"):
            d["links"] = c["links"]
    else:
        d["outlet"] = c.get("outlet")
        d["outlet_tier"] = c.get("source_tier")
        d["title"] = c.get("title")
        d["excerpt"] = c.get("text", "")[:700]
    return d


def _schema_with_theories(ids: list[str]) -> dict:
    if not ids:
        return RATING_SCHEMA
    s = json.loads(json.dumps(RATING_SCHEMA))
    item = s["properties"]["items"]["items"]
    item["properties"]["theory_links"] = {"type": "array", "items": {"type": "object", "properties": {
        "theory": ENUM(*ids), "stance": ENUM("supports", "contradicts")},
        "required": ["theory", "stance"], "additionalProperties": False}}
    item["required"].append("theory_links")
    return s


def rate(grok: Grok, candidates: list[dict], recent: list[dict], positioning: str, rubric: dict,
         theories: list[dict] | None = None, batch: int = 20) -> tuple[list[dict], list[dict]]:
    """Returns (rated, processed): rated candidates with ratings (keep=false and duplicates removed),
    and every candidate Grok actually returned a verdict for, so failed batches are retried next run."""
    from .theories import rating_guide, tracked
    tids = [t["id"] for t in tracked(theories or [])]
    system = SYSTEM + ("\n\n" + rating_guide(theories) if tids else "")
    schema = _schema_with_theories(tids)
    out, processed = [], []
    recent_lines = [{"id": r["id"], "time_utc": r["published_at"], "headline": r["headline"]} for r in recent][-60:]
    for i in range(0, len(candidates), batch):
        chunk = candidates[i:i + batch]
        cmap = {f"c{i + j + 1}": c for j, c in enumerate(chunk)}
        user = json.dumps({
            "market_positioning_now": positioning or "not available",
            "RECENT": recent_lines,
            "CANDIDATES": [_candidate_line(cid, c) for cid, c in cmap.items()],
            "instructions": "Rate every candidate. Return one entry per cid. If account_tier_from_config is given, use it as source_tier.",
        }, ensure_ascii=False)
        try:
            data, _ = grok.respond(system=system, user=user, schema=schema, name="ratings", label="rate", effort="low")
        except Exception as e:
            log.warning("rating batch failed: %s", e)
            if e.__class__.__name__ == "BudgetExceeded":
                raise
            continue
        seen_in_batch = set()
        for r in data.get("items") or []:
            c = cmap.get(r.get("cid"))
            if not c or r["cid"] in seen_in_batch:
                continue
            seen_in_batch.add(r["cid"])
            processed.append(c)
            if not r.get("keep") or r.get("duplicate_of"):
                continue
            if c.get("source_tier") and c.get("pass") != "discovery":
                r["source_tier"] = c["source_tier"]        # config / feed tier wins over the model's guess
            r["themes"] = [t for t in dict.fromkeys(r.get("themes") or []) if t in THEME_IDS][:3] or ["spec"]
            links, seen_t = [], set()
            for l in r.get("theory_links") or []:
                if l.get("theory") in tids and l["theory"] not in seen_t and l.get("stance") in ("supports", "contradicts"):
                    links.append({"id": l["theory"], "stance": l["stance"]}); seen_t.add(l["theory"])
            r["theory_links"] = links
            s, tier = score(r, rubric)
            out.append({**c, "rating": r, "score": s, "tier": tier})
    return out, processed


STATES_SCHEMA = {
    "type": "object",
    "properties": {"states": {"type": "array", "items": {"type": "object", "properties": {
        "theme": ENUM(*THEME_IDS), "state": {"type": "string"}}, "required": ["theme", "state"], "additionalProperties": False}}},
    "required": ["states"], "additionalProperties": False,
}


def theme_states(grok: Grok, items: list[dict]) -> dict:
    """One neutral sentence per active theme, from the last 7 days of rated items."""
    by = {}
    for it in items:
        for t in it["themes"]:
            by.setdefault(t, []).append(f"{it['published_at'][:10]} [{it['status']}, {it['direction']}, {it['impact']['tier']}] {it['headline']}")
    if not by:
        return {}
    user = json.dumps({"instructions": "For each theme, write one neutral sentence (max 120 characters) on where it stands this week, "
                                       "based only on these items. State facts, not predictions.",
                       "themes": {k: v[-15:] for k, v in by.items()}}, ensure_ascii=False)
    try:
        data, _ = grok.respond(system="You summarize news themes for an objective trading terminal. " + THEME_GUIDE,
                               user=user, schema=STATES_SCHEMA, name="states", label="theme-states", effort="low")
    except Exception as e:
        log.warning("theme states failed: %s", e)
        return {}
    return {s["theme"]: s["state"][:160] for s in data.get("states") or [] if s["theme"] in by}
