"""Claim Tracker: what widely followed XRP accounts claim, checked against the record.

Retrieval and checking are separate calls. Every verdict that says a claim is wrong must cite a source
that came back from an actual search; otherwise it's downgraded to 'unverified'. Dated price calls are
scored automatically from Coinbase prices once their deadline passes."""
from __future__ import annotations

import datetime as dt
import json

from .markets import CB
from .util import DATA_DIR, http, iso, log, now_utc, parse_iso
from .xai import BudgetExceeded, Grok
from .xscan import POSTS_SCHEMA, RETRIEVE_SYSTEM, _clean, _window
from .news import norm_url

CLAIMS = DATA_DIR / "claims.jsonl"
TYPES = ["price_prediction", "adoption", "regulation_legal", "etf_flows", "supply_onchain", "insider_rumor", "macro", "other"]
VERDICTS = ["supported", "misleading", "false", "unverified", "opinion", "prediction"]
ENUM = lambda *v: {"type": "string", "enum": list(v)}
NULLABLE = lambda s: {"anyOf": [s, {"type": "null"}]}


# ------------------------------------------------------------------ storage
def load() -> dict[str, dict]:
    out = {}
    try:
        with open(CLAIMS, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    c = json.loads(line)
                    out[c["id"]] = c
    except FileNotFoundError:
        pass
    return out


def save(claims: dict[str, dict]) -> None:
    CLAIMS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAIMS.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for c in sorted(claims.values(), key=lambda c: c["published_at"]):
            f.write(json.dumps(c, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(CLAIMS)


def accounts(cfg: dict) -> list[dict]:
    out = []
    for a in cfg.get("accounts") or []:
        if a.get("handle"):
            out.append({"handle": a["handle"].lstrip("@"), "name": a.get("name") or a["handle"]})
    if len(out) > 20:
        raise ValueError("Claim Tracker allows 20 accounts (one X search)")
    return out


# ------------------------------------------------------------------ retrieval
def scan(grok: Grok, accts: list[dict], since: dt.datetime, max_posts: int) -> list[dict]:
    handles = [a["handle"] for a in accts]
    # one search per slice of accounts, so a few very active accounts can't fill every result
    n = min(3, len(handles))
    slices = [handles[i::n] for i in range(n)]
    day = since.strftime("%Y-%m-%d")
    queries = "\n".join(f"  `(XRP OR Ripple OR XRPL OR RLUSD) ({' OR '.join('from:' + h for h in sl)}) since:{day}`" for sl in slices)
    user = (f"Find up to {max_posts} posts from these accounts posted after {iso(since)} that make a claim or a prediction about XRP, "
            "Ripple, the XRP Ledger, RLUSD or XRP ETFs: a stated fact, a number, insider or 'coming soon' information, a partnership, "
            "a regulatory or legal statement, or a price target. Skip greetings, memes, polls, giveaways, and chart commentary that "
            "makes no claim. Prefer posts that state something specific. Take at most 3 posts per account. Keep each post's text "
            "exactly as written, and fill quoted_text when the post quotes another post.\n"
            f"Accounts: {', '.join('@' + h for h in handles)}\n"
            f"Run exactly these {n} searches in Latest mode with limit 10, one per group of accounts:\n{queries}\n"
            "Don't open threads. If there are none, return an empty list.")
    try:
        data, info = grok.respond(system=RETRIEVE_SYSTEM, user=user, schema=POSTS_SCHEMA, name="posts",
                                  tools=[{"type": "x_search", "allowed_x_handles": handles, **_window(since)}], label="x:claims")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("claim scan failed: %s", e)
        return []
    names = {a["handle"].lower(): a["name"] for a in accts}
    out = []
    for p in _clean(data.get("posts") or [], info["citations"], since, "claims"):
        if p["handle"].lower() not in names:
            continue
        out.append({**p, "name": names[p["handle"].lower()]})
    return out


# ------------------------------------------------------------------ checking
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {"type": "object", "properties": {
        "cid": {"type": "string"},
        "has_claim": {"type": "boolean"},
        "claim": {"type": "string"},
        "type": ENUM(*TYPES),
        "stance": ENUM("bullish", "bearish", "neutral"),
        "verdict": ENUM(*VERDICTS),
        "on_record": {"type": "string"},
        "evidence_url": NULLABLE({"type": "string"}),
        "target_price": NULLABLE({"type": "number"}),
        "target_direction": NULLABLE(ENUM("above", "below")),
        "deadline": NULLABLE({"type": "string"}),
        "all_time_high": {"type": "boolean"},
    }, "required": ["cid", "has_claim", "claim", "type", "stance", "verdict", "on_record", "evidence_url",
                    "target_price", "target_direction", "deadline", "all_time_high"], "additionalProperties": False}}},
    "required": ["claims"], "additionalProperties": False,
}

CHECK_SYSTEM = (
    "You check claims about XRP made by widely followed X accounts, for an objective trading terminal. Assess the claim, not the person. "
    "Be equally strict with bullish and bearish claims, and fair: state the claim's meaning accurately and don't stretch it.\n"
    "For each post: the claim must come from the post's text or the text it quotes, nothing else. has_claim=false if neither makes a "
    "checkable claim or prediction about XRP (for example a reaction like 'this is true' with no quoted text); then fill the other "
    "fields minimally.\n"
    "claim: the claim in neutral words, max 160 characters, attributed to nobody (e.g. 'Bank X will settle payments in XRP this month').\n"
    "Judge what the post asserts as fact, not its tone: caps, emojis or hype alone don't make a true claim misleading. Misleading means "
    "a reader would come away believing something that isn't so.\n"
    "verdict:\n"
    "- supported: a source you found (filing, official statement, on-chain or flow data, established reporting) backs it.\n"
    "- misleading: partly true, but missing context changes the meaning (cherry-picked numbers, wrong timeframe, partnership that doesn't use XRP).\n"
    "- false: a source you found contradicts it.\n"
    "- unverified: a factual claim with no record either way, including insider or 'sources say' claims.\n"
    "- opinion: a value judgment or interpretation, not checkable.\n"
    "- prediction: about the future. For price targets fill target_price (USD) and target_direction (above/below). deadline "
    "(YYYY-MM-DD) only if the post gives a time frame: a year alone ('in 2026') means Dec 31 of that year, 'this month' the month's "
    "last day, 'by Q1' the quarter's last day; otherwise null. all_time_high=true if the prediction is a new all-time high for XRP "
    "(leave target_price null then). Non-price predictions with a time frame also get a deadline.\n"
    "supported, misleading and false need evidence_url: the most relevant source from your searches. Otherwise evidence_url may be "
    "null. on_record: 1-2 sentences on what the record shows and why this verdict. Use the web search tool only as needed, at most "
    "{max_searches} searches in total."
)


def check(grok: Grok, posts: list[dict], context: str, max_searches: int, state: dict | None = None) -> list[dict]:
    if not posts:
        return []
    cmap = {f"p{i + 1}": p for i, p in enumerate(posts)}
    user = json.dumps({
        "context_from_terminal": context or "none",
        "posts": [{"cid": cid, "account": "@" + p["handle"], "time_utc": p["posted_at"], "text": p["text"][:1200],
                   **({"quoting": p["quoted_text"][:500]} if p.get("quoted_text") else {}),
                   **({"links": p["links"]} if p.get("links") else {})} for cid, p in cmap.items()],
    }, ensure_ascii=False)
    try:
        data, info = grok.respond(system=CHECK_SYSTEM.replace("{max_searches}", str(max_searches)), user=user,
                                  schema=CHECK_SCHEMA, name="claim_check", tools=[{"type": "web_search"}], label="claim-check")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("claim check failed: %s", e)
        return []
    cited = {norm_url(u) for u in info["citations"]}
    out = []
    for r in data.get("claims") or []:
        p = cmap.get(r.get("cid"))
        if not p:
            continue
        p["_checked"] = True
        if not r.get("has_claim"):
            continue
        url = r.get("evidence_url")
        if url and (norm_url(url) not in cited) and not any(norm_url(url) == norm_url(l) for l in p.get("links") or []):
            url = None                                   # a source has to come from an actual search result
        verdict = r["verdict"]
        if verdict in ("supported", "false", "misleading") and not url:
            verdict = "unverified"                       # no cited source: neither confirmed nor called wrong
        deadline = r.get("deadline")
        if deadline and not parse_iso(deadline + "T00:00:00Z"):
            deadline = None
        target, direction, basis, ctype = r.get("target_price"), r.get("target_direction"), None, r["type"]
        if verdict == "prediction" and r.get("all_time_high"):
            rec = record_high(state) if state is not None else None
            target, direction, ctype = (rec["price"] if rec else None), "above", "price_prediction"
            if rec:
                basis = f"Coinbase record high of {rec['date']}"
        out.append({
            "id": "c:" + p["sid"], "handle": "@" + p["handle"], "name": p["name"], "url": p["url"],
            "published_at": p["posted_at"], "logged_at": iso(now_utc()),
            "text": p["text"][:600], "quoted_text": (p.get("quoted_text") or "")[:300] or None,
            "claim": r["claim"][:200], "type": ctype, "stance": r["stance"],
            "verdict": verdict, "on_record": r["on_record"][:400], "evidence_url": url,
            "prediction": ({"target_price": target, "direction": direction, "deadline": deadline, "basis": basis,
                            "outcome": "pending" if deadline else "open-ended"} if verdict == "prediction" else None),
        })
    return out


# ------------------------------------------------------------------ prediction scoring
def _daily(start: dt.datetime, end: dt.datetime) -> list[list]:
    r = http("GET", CB.format(pid="XRP-USD"), params={"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()}, timeout=20)
    rows = r.json()
    return rows if isinstance(rows, list) else []


COINBASE_LISTED = dt.datetime(2019, 2, 25, tzinfo=dt.timezone.utc)


def record_high(state: dict) -> dict | None:
    """XRP's highest daily high on Coinbase since it listed there (Feb 2019): the bar for 'new all-time high' calls.
    Cached in state and extended with new days, so the full history is read once."""
    rec = state.get("xrp_record_high")
    now = now_utc()
    start = (parse_iso(rec["through"]) - dt.timedelta(days=1)) if rec else COINBASE_LISTED
    if rec and now - start < dt.timedelta(hours=36):
        return rec
    rows, t = [], start
    try:
        while t < now:
            t2 = min(now, t + dt.timedelta(days=290))
            rows += _daily(t, t2)
            t = t2
    except Exception as e:
        log.warning("record-high lookup failed: %s", e)
        return rec
    best = rec
    for x in rows:
        if not best or float(x[2]) > best["price"]:
            best = {"price": round(float(x[2]), 4), "date": dt.datetime.fromtimestamp(int(x[0]), dt.timezone.utc).strftime("%Y-%m-%d")}
    if best:
        best = {**best, "through": iso(now)}
        state["xrp_record_high"] = best
    return best


def score_price_calls(claims: dict[str, dict]) -> int:
    """Dated price targets: after the deadline, hit if XRP traded at or beyond the target at any point in between."""
    now = now_utc()
    n = 0
    for c in claims.values():
        pr = c.get("prediction") or {}
        if c.get("type") != "price_prediction" or pr.get("outcome") != "pending" or not pr.get("target_price") or not pr.get("direction"):
            continue
        end = parse_iso(pr["deadline"] + "T23:59:59Z")
        if not end or end > now - dt.timedelta(hours=2):
            continue
        start = parse_iso(c["published_at"])
        try:
            rows = []
            t = start
            while t < end:
                t2 = min(end, t + dt.timedelta(days=290))
                rows += _daily(t, t2)
                t = t2
        except Exception as e:
            log.warning("price check for %s failed: %s", c["id"], e)
            continue
        if not rows:
            continue
        hi, lo = max(float(x[2]) for x in rows), min(float(x[1]) for x in rows)
        hit = hi >= pr["target_price"] if pr["direction"] == "above" else lo <= pr["target_price"]
        pr.update(outcome="hit" if hit else "missed", resolved_at=iso(now), peak=round(hi if pr["direction"] == "above" else lo, 4))
        n += 1
    return n


RESOLVE_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "outcome": ENUM("came_true", "did_not_happen", "unclear"),
        "note": {"type": "string"}, "evidence_url": NULLABLE({"type": "string"})},
        "required": ["id", "outcome", "note", "evidence_url"], "additionalProperties": False}}},
    "required": ["results"], "additionalProperties": False,
}


def resolve_other(grok: Grok, claims: dict[str, dict], limit: int = 5) -> int:
    """Dated non-price predictions whose deadline has passed: check whether they happened."""
    now = now_utc()
    due = [c for c in claims.values() if (c.get("prediction") or {}).get("outcome") == "pending" and c.get("type") != "price_prediction"
           and parse_iso(c["prediction"]["deadline"] + "T23:59:59Z") and parse_iso(c["prediction"]["deadline"] + "T23:59:59Z") < now]
    due = sorted(due, key=lambda c: c["prediction"]["deadline"])[:limit]
    if not due:
        return 0
    user = json.dumps({"predictions": [{"id": c["id"], "made_on": c["published_at"][:10], "deadline": c["prediction"]["deadline"],
                                        "prediction": c["claim"]} for c in due]}, ensure_ascii=False)
    try:
        data, info = grok.respond(system="For each prediction about XRP, check with web search whether it happened by its deadline. "
                                         "came_true only with a source; did_not_happen when the record shows it didn't; unclear otherwise. "
                                         "One sentence note. At most 3 searches in total.",
                                  user=user, schema=RESOLVE_SCHEMA, name="resolve", tools=[{"type": "web_search"}], label="claim-resolve")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("prediction check failed: %s", e)
        return 0
    cited = {norm_url(u) for u in info["citations"]}
    n = 0
    for r in data.get("results") or []:
        c = claims.get(r.get("id"))
        if not c:
            continue
        url = r.get("evidence_url") if r.get("evidence_url") and norm_url(r["evidence_url"]) in cited else None
        outcome = r["outcome"] if (r["outcome"] != "came_true" or url) else "unclear"
        c["prediction"].update(outcome=outcome, resolved_at=iso(now), note=r["note"][:240], evidence_url=url)
        n += 1
    return n


# ------------------------------------------------------------------ run + output
def run(grok: Grok, cfg: dict, state: dict, context: str = "") -> None:
    accts = accounts(cfg)
    if not accts:
        log.info("claim tracker: no accounts with handles yet")
        return
    claims = load()
    since = parse_iso(state.get("last_claims_scan")) or now_utc() - dt.timedelta(hours=24)
    since = max(since, now_utc() - dt.timedelta(hours=30))
    started = now_utc()
    posts = [p for p in scan(grok, accts, since, int(cfg.get("max_posts", 15)))
             if ("c:" + p["sid"]) not in claims and ("c:" + p["sid"]) not in state["seen"]]
    log.info("claim tracker: %d new posts to check", len(posts))
    checked = check(grok, posts, context, int(cfg.get("max_web_searches", 4)), state)
    for p in posts:
        if p.get("_checked"):
            state["seen"]["c:" + p["sid"]] = iso(now_utc())
    for c in checked:
        claims[c["id"]] = c
    log.info("claim tracker: %d claims logged (%d posts had none)", len(checked), sum(1 for p in posts if p.get("_checked")) - len(checked))
    try:
        scored = score_price_calls(claims) + resolve_other(grok, claims)
        if scored:
            log.info("claim tracker: %d predictions resolved", scored)
    finally:
        save(claims)
    state["last_claims_scan"] = iso(started)


def build_output(cfg: dict, state: dict) -> dict | None:
    accts = accounts(cfg)
    claims = load()
    if not accts and not claims:
        return None
    rows = {a["handle"].lower(): {"handle": "@" + a["handle"], "name": a["name"], "total": 0,
                                  **{v: 0 for v in VERDICTS}, "calls": {"hit": 0, "missed": 0, "pending": 0, "came_true": 0,
                                                                         "did_not_happen": 0, "open-ended": 0, "unclear": 0}} for a in accts}
    for c in claims.values():
        r = rows.get(c["handle"].lstrip("@").lower())
        if not r:
            continue
        r["total"] += 1
        r[c["verdict"]] += 1
        if c.get("prediction"):
            r["calls"][c["prediction"]["outcome"]] = r["calls"].get(c["prediction"]["outcome"], 0) + 1
    cut = iso(now_utc() - dt.timedelta(days=30))
    recent = [c for c in claims.values() if c["published_at"] >= cut or (c.get("prediction") or {}).get("outcome") == "pending"]
    recent.sort(key=lambda c: c["published_at"], reverse=True)
    return {"since": min((c["logged_at"] for c in claims.values()), default=None), "last_scan": state.get("last_claims_scan"),
            "accounts": sorted(rows.values(), key=lambda r: -r["total"]), "items": recent[:120]}
