"""Budget outlook and the heads-up before scans pause.

The outlook (shown on the page) projects when the month's Grok spend reaches budget.monthly_usd from the
average of the last 7 full days. A GitHub issue mentioning the repo owner (GitHub emails it) is opened
once per month when the projected pause is 3 days away or less, and once when scans actually pause."""
from __future__ import annotations

import datetime as dt
import os

from .util import NY, http, iso, log, now_utc

WARN_DAYS = 3


def budget_outlook(state: dict, settings: dict) -> dict:
    now = now_utc()
    month = now.strftime("%Y-%m")
    m = (state.get("spend") or {}).get(month) or {}
    cap = float(settings["budget"]["monthly_usd"])
    spent = float(m.get("usd") or 0)
    by_day = m.get("by_day") or {}
    today = now.strftime("%Y-%m-%d")
    full = [d for d in sorted(by_day) if d < today][-7:]
    if len(full) >= 2:
        rate = sum(by_day[d] for d in full) / len(full)
    else:                                                  # not enough daily history yet: month-to-date average
        rate = spent / max(1.0, (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400)
    nxt = (now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=32)).replace(day=1)
    left = cap - spent
    paused = left <= 0
    pause_on = None
    if paused:
        pause_on = now.astimezone(NY).strftime("%Y-%m-%d")
    elif rate > 0:
        t = now + dt.timedelta(days=left / rate)
        if t < nxt:
            pause_on = t.astimezone(NY).strftime("%Y-%m-%d")
    return {"month": month, "spent_usd": round(spent, 2), "cap_usd": cap, "left_usd": round(max(0.0, left), 2),
            "daily_usd": round(rate, 2), "pause_on": pause_on, "paused": paused, "resets_on": nxt.strftime("%Y-%m-%d")}


def _issue(title: str, body: str) -> bool:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        log.info("budget heads-up (not sent, no GitHub token here): %s", title)
        return False
    owner = repo.split("/")[0]
    http("POST", f"https://api.github.com/repos/{repo}/issues", timeout=20,
         headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
         json={"title": title, "body": f"{body}\n\ncc @{owner}"})
    log.info("budget heads-up sent as a GitHub issue: %s", title)
    return True


def notify(state: dict, settings: dict) -> None:
    o = budget_outlook(state, settings)
    sent = state.setdefault("alerts", {})
    when = lambda d: dt.date.fromisoformat(d).strftime("%a %b %-d")
    resets = when(o["resets_on"])
    keep = ("ETF flows, price reactions and the chart keep updating. Scans resume on their own on "
            f"{resets}, or sooner if you raise `budget.monthly_usd` in `config/settings.yaml`.")
    if o["paused"]:
        key = f"budget-paused:{o['month']}"
        title = f"Scans paused: the {o['month']} Grok budget of ${o['cap_usd']:.0f} is used up"
        body = (f"Spent ${o['spent_usd']:.2f} of ${o['cap_usd']:.0f} this month. The X scan, web news search, Theory Check "
                f"updates and Claim Tracker are paused. {keep}")
    elif o["pause_on"]:
        days = (dt.date.fromisoformat(o["pause_on"]) - now_utc().astimezone(NY).date()).days
        if days > WARN_DAYS:
            return
        key = f"budget-warn:{o['month']}"
        title = f"Heads-up: scans pause around {when(o['pause_on'])} (Grok budget)"
        body = (f"Spent ${o['spent_usd']:.2f} of the ${o['cap_usd']:.0f} monthly Grok budget; ${o['left_usd']:.2f} left. "
                f"At about ${o['daily_usd']:.2f} a day, the X scan, web news search, Theory Check updates and Claim Tracker "
                f"pause around {when(o['pause_on'])}. {keep}")
    else:
        return
    if key in sent:
        return
    if _issue(title, body):
        sent[key] = iso(now_utc())
