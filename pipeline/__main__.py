"""Entry point: python -m pipeline --task auto|scan|etf|reactions|rebuild|verify_handles"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor

from . import classify, markets, news, store, xscan
from .util import DATA_DIR, NY, iso, load_yaml, log, now_utc, parse_iso, save_json, setup_logging
from .xai import Budget, BudgetExceeded, Grok

TASKS = ["auto", "scan", "etf", "reactions", "rebuild", "verify_handles"]


def plan(task: str, state: dict, settings: dict) -> tuple[list[str], list[str]]:
    """Which tasks to run now, and the schedule slots they consume."""
    if task != "auto":
        return [task], []
    ny = now_utc().astimezone(NY)
    h, d = ny.hour, ny.strftime("%Y-%m-%d")
    late = int(settings["schedule"].get("late_window_hours", 1))
    tasks, slots = [], []
    for target in settings["schedule"]["scan_hours_et"]:
        key = f"scan:{d}:{target}"
        if target <= h <= target + late and key not in state["slots_done"]:
            tasks.append("scan"); slots.append(key); break
    target = settings["schedule"]["etf_hour_et"]
    key = f"etf:{d}:{target}"
    if target <= h <= target + late and key not in state["slots_done"]:
        tasks.append("etf"); slots.append(key)
    return tasks, slots


def window_start(last: str | None, default_h: int = 12, max_h: int = 26) -> dt.datetime:
    now = now_utc()
    t = parse_iso(last) or now - dt.timedelta(hours=default_h)
    return max(t, now - dt.timedelta(hours=max_h))


def run_scan(grok: Grok, state: dict, evs: dict, settings: dict, web: bool = True) -> None:
    if not grok.available:
        log.warning("XAI_API_KEY not set: skipping the X and news scan")
        return
    handles, feeds = load_yaml("handles.yaml"), load_yaml("feeds.yaml")
    started = now_utc()
    if markets.coinglass_available():
        state["positioning"] = markets.positioning() or state.get("positioning")

    cands, x_ok = [], False
    x_since = window_start(state.get("last_x_scan"))
    n_since = window_start(state.get("last_news_scan"))
    # The account groups, the discovery search and the web news search are independent: run them together
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_sig = ex.submit(xscan.signal_scan, grok, handles, x_since)
        f_dis = ex.submit(xscan.discovery_scan, grok, handles, x_since, settings["xai"]["discovery_max_posts"])
        f_web = ex.submit(news.web_items, grok, n_since, settings["xai"].get("web_news_max_searches", 4)) if web else None
        rss, errors = news.rss_items(feeds, settings["feed"]["keywords"], n_since)
        try:
            cands += f_sig.result() + f_dis.result()
            x_ok = True
        except BudgetExceeded as e:
            log.warning("%s", e)
        if f_web:
            try:
                cands += f_web.result()
            except BudgetExceeded as e:
                log.warning("%s", e)
    cands += rss
    state["feed_errors"] = errors

    # de-duplicate against this batch, earlier runs and the event log
    fresh, keys = [], set()
    for c in cands:
        k = ("x:" + c["sid"]) if c["kind"] == "x" else ("n:" + c["uid"])
        if k in keys or k in state["seen"] or k in evs:
            continue
        keys.add(k)
        c["_key"] = k
        fresh.append(c)
    log.info("candidates: %d new of %d found", len(fresh), len(cands))

    recent_cut = iso(now_utc() - dt.timedelta(hours=48))
    recent = [e for e in evs.values() if e["published_at"] >= recent_cut]
    try:
        rated, processed = classify.rate(grok, fresh, recent, markets.positioning_text(state.get("positioning")), settings["rubric"])
    except BudgetExceeded as e:
        log.warning("%s", e)
        rated, processed = [], []
    for c in processed:
        state["seen"][c["_key"]] = iso(now_utc())
    for c in rated:
        ev = store.event_from_rated(c)
        evs[ev["id"]] = ev
    log.info("rated %d, kept %d", len(processed), len(rated))

    if x_ok:
        state["last_x_scan"] = iso(started)
    state["last_news_scan"] = iso(started)

    week = store.build(evs, state, settings, [], {})["items"]
    try:
        states = classify.theme_states(grok, week)
        if states:
            state["theme_states"] = states
    except BudgetExceeded as e:
        log.warning("%s", e)


def run_etf(state: dict) -> None:
    if not markets.coinglass_available():
        log.warning("COINGLASS_API_KEY not set: skipping the ETF pull")
        return
    etf = markets.etf_flows()
    if etf["daily"]:
        save_json(store.ETF, etf)
        state["last_etf_pull"] = iso(now_utc())
        log.info("ETF flows through %s (%d days)", etf["as_of"], len(etf["daily"]))
    if not state.get("positioning") or (now_utc() - (parse_iso(state["positioning"].get("as_of")) or now_utc())).total_seconds() > 6 * 3600:
        state["positioning"] = markets.positioning() or state.get("positioning")


def run_verify(grok: Grok) -> None:
    if not grok.available:
        log.error("XAI_API_KEY not set: can't verify handles")
        return
    rows = xscan.verify_handles(grok, load_yaml("handles.yaml"))
    save_json(DATA_DIR / "handle_check.json", {"checked_at": iso(now_utc()), "accounts": rows})
    print("\nHANDLE CHECK")
    for r in rows:
        mark = {"found": "OK ", "not_found": "NO "}.get(r["status"], "?? ")
        print(f"  {mark}@{r['handle']:<18} {r['display_name'] or '-':<28} {r['description'] or ''}")


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    ap = argparse.ArgumentParser(prog="pipeline")
    ap.add_argument("--task", default="auto", choices=TASKS)
    args = ap.parse_args(argv)

    settings = load_yaml("settings.yaml")
    theories = (load_yaml("theories.yaml") or {}).get("theories", [])
    state, evs = store.load_state(), store.load_events()
    grok = Grok(settings, Budget(state, settings))

    tasks, slots = plan(args.task, state, settings)
    log.info("New York %s · tasks: %s", now_utc().astimezone(NY).strftime("%a %H:%M"), ", ".join(tasks) or "reactions only")
    try:
        if "verify_handles" in tasks:
            run_verify(grok)
        if "scan" in tasks:
            ny_h = now_utc().astimezone(NY).hour
            web = args.task == "scan" or any(h <= ny_h <= h + 1 for h in settings["xai"].get("web_news_hours_et", []))
            run_scan(grok, state, evs, settings, web=web)
        if "etf" in tasks:
            run_etf(state)
    finally:
        state["slots_done"] += slots
        for e in evs.values():
            classify.rescore(e, settings["rubric"])
        try:
            filled = markets.fill_reactions(evs, markets.Candles())
            if filled:
                log.info("filled %d price-reaction cells", filled)
        except Exception as e:  # prices are retried next run
            log.warning("price reactions skipped: %s", e)
        if store.has_any_data(evs) and args.task != "verify_handles":
            store.write_output(store.build(evs, state, settings, theories, state.get("theme_states", {})))
            log.info("wrote data.json (%d events logged)", len(evs))
        store.save_events(evs)
        store.save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
