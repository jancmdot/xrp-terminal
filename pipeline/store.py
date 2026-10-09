"""Event log, run state, base rates, and the data.json the page reads."""
from __future__ import annotations

import datetime as dt
import json

from .util import DATA_DIR, ROOT, load_json, now_utc, parse_iso, save_json, iso

EVENTS = DATA_DIR / "events.jsonl"
STATE = DATA_DIR / "state.json"
ETF = DATA_DIR / "etf.json"
OUT = ROOT / "data.json"
SCHEMA = "xrp-terminal/0.1"
TIER_RANK = {"noise": 0, "low": 1, "medium": 2, "high": 3}


def load_events() -> dict[str, dict]:
    evs = {}
    try:
        with open(EVENTS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    evs[e["id"]] = e
    except FileNotFoundError:
        pass
    return evs


def save_events(evs: dict[str, dict]) -> None:
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(evs.values(), key=lambda e: e["published_at"])
    tmp = EVENTS.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for e in rows:
            f.write(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(EVENTS)


def load_state() -> dict:
    s = load_json(STATE, {})
    s.setdefault("seen", {})
    s.setdefault("slots_done", [])
    s.setdefault("spend", {})
    return s


def save_state(s: dict) -> None:
    cutoff = iso(now_utc() - dt.timedelta(days=21))
    s["seen"] = {k: v for k, v in s["seen"].items() if v >= cutoff}
    s["slots_done"] = s["slots_done"][-60:]
    save_json(STATE, s)


def event_from_rated(c: dict) -> dict:
    r = c["rating"]
    if c["kind"] == "x":
        eid = "x:" + c["sid"]
        source = {"name": c.get("author_name") or c.get("handle"), "handle": "@" + c.get("handle", ""), "url": c["url"], "tier": r["source_tier"]}
    else:
        eid = "n:" + c["uid"]
        source = {"name": c.get("outlet"), "url": c["url"], "tier": r["source_tier"]}
    return {
        "id": eid, "kind": c["kind"], "published_at": c["posted_at"], "logged_at": iso(now_utc()),
        "headline": r["headline"][:140], "summary": r["summary"], "source": source,
        "themes": r["themes"], "status": r["status"], "direction": r["direction"], "horizon": r["horizon"],
        "rubric": {k: r[k] for k in ("novelty", "size", "scope")}, "score": c["score"],
        "impact": {"tier": c["tier"], "basis": "rubric", "factors": r["factors"][:5], "rationale": r["rationale"]},
        "reality_check": r["reality_check"], "pass": c.get("pass"), "reaction": {},
    }


def base_rates(evs: dict, settings: dict) -> dict:
    br = settings["base_rates"]
    thr, min_rank = float(br["move_threshold_pct"]), TIER_RANK[br["min_tier"]]
    out = {}
    for e in evs.values():
        t = (e.get("themes") or ["spec"])[0]
        if t == "spec" or TIER_RANK.get(e["impact"]["tier"], 0) < min_rank:
            continue
        v = (e.get("reaction") or {}).get("vs_btc_24h")
        o = out.setdefault(t, {"n": 0, "moves": 0})
        if v is None:
            continue
        o["n"] += 1
        o["moves"] += abs(v) > thr
    return {t: {"n": o["n"], "p": round(o["moves"] / o["n"], 3) if o["n"] else None} for t, o in out.items()}


def has_any_data(evs: dict) -> bool:
    return bool(evs) or ETF.exists()


def build(evs: dict, state: dict, settings: dict, theories: list, theme_state: dict) -> dict:
    window = now_utc() - dt.timedelta(days=settings["feed"]["window_days"])
    rates = base_rates(evs, settings)
    unlock = int(settings["base_rates"]["unlock_n"])
    items = []
    for e in sorted(evs.values(), key=lambda e: e["published_at"], reverse=True):
        t = parse_iso(e["published_at"])
        if not t or t < window:
            continue
        it = {k: e[k] for k in ("id", "kind", "published_at", "headline", "summary", "source", "themes", "status",
                                 "direction", "horizon", "reality_check")}
        imp = dict(e["impact"])
        r = rates.get(e["themes"][0]) if e["themes"] else None
        if r and r["n"] >= unlock and r["p"] is not None:
            imp.update(basis="base_rate", p=r["p"], n=r["n"])
        it["impact"] = imp
        rx = e.get("reaction") or {}
        if any(v is not None for v in rx.values()):
            it["reaction"] = rx
        items.append(it)
    themes = {}
    for tid in ["etf", "reg", "tok", "stb", "pay", "col", "corp", "sup", "mac", "spec"]:
        themes[tid] = {"state": theme_state.get(tid), "base_rate": None if tid == "spec" else (rates.get(tid) or {"n": 0, "p": None})}
    etf = load_json(ETF, None)
    month = now_utc().strftime("%Y-%m")
    spend = state.get("spend", {}).get(month, {})
    return {
        "meta": {"schema": SCHEMA, "generated_at": iso(now_utc()), "sample": False,
                 "last_x_scan": state.get("last_x_scan"), "last_news_scan": state.get("last_news_scan"),
                 "last_etf_pull": state.get("last_etf_pull"), "feed_errors": state.get("feed_errors", []),
                 "positioning": state.get("positioning"),
                 "spend": {"month": month, "usd": round(spend.get("usd", 0), 2), "cap_usd": settings["budget"]["monthly_usd"]},
                 "spot_volume_source": settings["etf"].get("spot_volume_source"),
                 "sources": ["Grok x_search", "RSS feeds", "Grok web search", "CoinGlass ETF flows", "Coinbase prices"]},
        "etf": etf,
        "items": items,
        "themes": themes,
        "theories": theories,
        "event_log": {"logged": sum(1 for e in evs.values() if e["themes"] and e["themes"][0] != "spec"), "unlock_n": unlock,
                      "with_24h_reaction": sum(1 for e in evs.values() if (e.get("reaction") or {}).get("vs_btc_24h") is not None)},
    }


def write_output(data: dict) -> bool:
    """Write data.json unless only generated_at would change. Returns True if written."""
    old = load_json(OUT, None)
    if old and {**old, "meta": {**old.get("meta", {}), "generated_at": None}} == {**data, "meta": {**data["meta"], "generated_at": None}}:
        return False
    save_json(OUT, data, indent=None)
    return True
