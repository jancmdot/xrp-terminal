"""Theory Check: link items to theories, write a weekly evidence note, and move verdicts only under strict rules."""
from __future__ import annotations

import datetime as dt
import json

from .util import iso, log, now_utc, parse_iso
from .xai import BudgetExceeded, Grok

ORDER = ["unsupported", "speculative", "partly", "supported"]      # levels of support, low to high
EVIDENCE_STATUS = {"confirmed", "reported"}                         # rumors and speculation are claims, not evidence
STRONG_TIERS = {"primary", "t1"}
WINDOW_DAYS = 7
COOLDOWN_DAYS = 7


def load(cfg: dict) -> list[dict]:
    out = []
    for t in cfg.get("theories") or []:
        if not t.get("id"):
            continue
        out.append({**t, "tracked": bool(t.get("tracked", True)) and t.get("verdict") != "untestable"})
    return out


def tracked(theories: list[dict]) -> list[dict]:
    return [t for t in theories if t["tracked"]]


def current_verdict(t: dict, st: dict) -> str:
    """The automatic verdict if one is set and the config verdict hasn't been edited since; else the config verdict."""
    if st.get("verdict") and st.get("base") == t["verdict"]:
        return st["verdict"]
    return t["verdict"]


def evidence(evs: dict, tid: str, days: int = WINDOW_DAYS) -> list[dict]:
    cut = iso(now_utc() - dt.timedelta(days=days))
    rows = []
    for e in evs.values():
        if e["published_at"] < cut:
            continue
        for link in e.get("theories") or []:
            if link.get("id") == tid:
                rows.append({"id": e["id"], "stance": link["stance"], "date": e["published_at"], "headline": e["headline"],
                             "status": e["status"], "tier": e["source"]["tier"], "counts": e["status"] in EVIDENCE_STATUS})
    return sorted(rows, key=lambda r: r["date"], reverse=True)


UPDATE_SCHEMA = {
    "type": "object",
    "properties": {"theories": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"},
        "note": {"type": "string"},
        "net": {"type": "string", "enum": ["strengthens", "weakens", "mixed", "none"]},
        "propose_verdict": {"anyOf": [{"type": "string", "enum": ORDER}, {"type": "null"}]},
        "why": {"type": ["string", "null"]},
    }, "required": ["id", "note", "net", "propose_verdict", "why"], "additionalProperties": False}}},
    "required": ["theories"], "additionalProperties": False,
}

UPDATE_SYSTEM = (
    "You maintain the Theory Check for an objective XRP trading terminal. For each theory you get its claim, current verdict, "
    "what is on record, the condition that would change the verdict, and this week's linked evidence. "
    "Write 'note': one neutral sentence (max 160 characters) on what this week's evidence shows for the claim, naming the strongest item. "
    "'net': strengthens, weakens, mixed, or none. Judge the evidence, not how often a claim is repeated; reported items count less than confirmed ones. "
    "'propose_verdict': only if this week's confirmed evidence actually meets the would-change condition (or clearly breaks the claim), "
    "the new verdict, one step from the current one; otherwise null. Most weeks it should be null. 'why': one sentence if you propose a change, else null."
)


def _allowed_change(cur: str, new: str | None, rows: list[dict], st: dict) -> tuple[bool, str]:
    if not new or new == cur or cur not in ORDER or new not in ORDER:
        return False, "no change proposed"
    step = ORDER.index(new) - ORDER.index(cur)
    if abs(step) != 1:
        return False, f"{cur} to {new} skips a step"
    need = "supports" if step > 0 else "contradicts"
    if not any(r["stance"] == need and r["status"] == "confirmed" and r["tier"] in STRONG_TIERS for r in rows):
        return False, f"no confirmed {need} item from a primary or established source"
    last = parse_iso((st.get("history") or [{}])[-1].get("on"))
    if last and now_utc() - last < dt.timedelta(days=COOLDOWN_DAYS):
        return False, "verdict changed less than a week ago"
    return True, ""


def update(grok: Grok, theories: list[dict], state: dict, evs: dict, etf_context: str = "") -> None:
    """Refresh each tracked theory's weekly note, and apply verdict changes that pass the rules."""
    tstate = state.setdefault("theories", {})
    ctx = []
    for t in tracked(theories):
        st = tstate.setdefault(t["id"], {})
        if st.get("base") and st["base"] != t["verdict"]:        # verdict edited in the config: drop the automatic one
            st.pop("verdict", None); st.pop("base", None)
        rows = evidence(evs, t["id"])
        counted = [r for r in rows if r["counts"]]
        if not counted:
            st.update(note="No confirmed or reported evidence in the last 7 days.", net="none", updated_at=iso(now_utc()))
            continue
        ctx.append({"id": t["id"], "claim": t["claim"], "current_verdict": current_verdict(t, st), "on_record": t["true_part"],
                    "would_change_if": t["would_change"],
                    "evidence_this_week": [{"date": r["date"][:10], "stance": r["stance"], "status": r["status"],
                                            "source_tier": r["tier"], "headline": r["headline"]} for r in counted[:15]]})
    if not ctx:
        return
    payload = {"theories": ctx}
    if etf_context:
        payload["etf_flows"] = etf_context
    try:
        data, _ = grok.respond(system=UPDATE_SYSTEM, user=json.dumps(payload, ensure_ascii=False), schema=UPDATE_SCHEMA,
                               name="theory_update", label="theory-update", effort="low")
    except BudgetExceeded:
        raise
    except Exception as e:
        log.warning("theory update failed: %s", e)
        return
    by_id = {t["id"]: t for t in theories}
    for r in data.get("theories") or []:
        t = by_id.get(r.get("id"))
        if not t or not t["tracked"]:
            continue
        st = tstate.setdefault(t["id"], {})
        st.update(note=(r.get("note") or "")[:220], net=r.get("net") or "none", updated_at=iso(now_utc()))
        cur = current_verdict(t, st)
        ok, reason = _allowed_change(cur, r.get("propose_verdict"), evidence(evs, t["id"]), st)
        if ok:
            st["verdict"], st["base"] = r["propose_verdict"], t["verdict"]
            st.setdefault("history", []).append({"on": iso(now_utc()), "from": cur, "to": r["propose_verdict"], "why": (r.get("why") or "")[:240]})
            log.info("theory %s: verdict %s -> %s", t["id"], cur, r["propose_verdict"])
        elif r.get("propose_verdict") and r["propose_verdict"] != cur:
            log.info("theory %s: proposed %s -> %s not applied (%s)", t["id"], cur, r["propose_verdict"], reason)


def build_output(theories: list[dict], state: dict, evs: dict) -> list[dict]:
    tstate = state.get("theories", {})
    out = []
    for t in theories:
        st = tstate.get(t["id"], {})
        row = {"id": t["id"], "short": t.get("short") or t["id"], "claim": t["claim"], "verdict": current_verdict(t, st), "base_verdict": t["verdict"],
               "tracked": t["tracked"], "true_part": t["true_part"], "would_change": t["would_change"]}
        if t["tracked"]:
            rows = evidence(evs, t["id"])
            counted = [r for r in rows if r["counts"]]
            row["week"] = {
                "supports": sum(r["stance"] == "supports" for r in counted),
                "contradicts": sum(r["stance"] == "contradicts" for r in counted),
                "claims": sum(not r["counts"] for r in rows),
                "net": st.get("net", "none"), "note": st.get("note"), "updated_at": st.get("updated_at"),
                "items": [{"id": r["id"], "stance": r["stance"], "counts": r["counts"]} for r in rows[:6]],
            }
            if st.get("verdict") and st.get("base") == t["verdict"] and st.get("history"):
                row["changed"] = st["history"][-1]
        out.append(row)
    return out


def rating_guide(theories: list[dict]) -> str:
    tt = tracked(theories)
    if not tt:
        return ""
    lines = "\n".join(f"- {t['id']}: \"{t['claim']}\" Would change if: {t['would_change']}" for t in tt)
    return ("Theories (fill theory_links):\n" + lines + "\n"
            "theory_links: for each theory the item is direct evidence about, its stance: supports (makes the claim more likely) "
            "or contradicts (less likely). Leave it empty unless the item bears on the claim's mechanism; most items link to none. "
            "A partnership that doesn't use XRP is not evidence that XRP replaces SWIFT. Repeating a claim is not evidence for it.")
