"""Offline end-to-end test with fake APIs: python -m pipeline.selftest

Runs scan → ETF → a later reactions pass in a temporary copy of the repo and checks data.json.
Costs nothing and needs no keys."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO = Path(__file__).resolve().parent.parent


def run(root: Path, task: str, now: dt.datetime) -> str:
    env = {**os.environ, "XRPTERM_ROOT": str(root), "XRPTERM_FAKE": "1", "XAI_API_KEY": "fake", "COINGLASS_API_KEY": "fake",
           "XRPTERM_NOW": now.isoformat()}
    p = subprocess.run([sys.executable, "-m", "pipeline", "--task", task], cwd=REPO, env=env, capture_output=True, text=True)
    if p.returncode:
        print(p.stdout, p.stderr)
        raise SystemExit(f"task {task} failed")
    return p.stderr + p.stdout


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="xrpterm-"))
    shutil.copytree(REPO / "config", tmp / "config")
    # the real account list is swapped for two example accounts (the claim tracker also runs inside a manual scan)
    (tmp / "config" / "influencers.yaml").write_text(
        "hours_et: [17]\nmax_posts: 15\nmax_web_searches: 4\naccounts:\n"
        "  - {handle: ExInfluencerA, name: Influencer A}\n  - {handle: ExInfluencerB, name: Influencer B}\n")
    with open(tmp / "config" / "feeds.yaml", "a") as f:     # a feed that 404s, to test error reporting
        f.write('  - {name: XRPL Blog, url: "https://xrpl.org/blog/rss.xml", tier: primary}\n')
    t0 = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    checks = []

    def check(name, ok):
        checks.append((name, bool(ok)))

    log1 = run(tmp, "scan", t0)
    check("uncited invented post dropped", "dropped 1 posts without a matching search citation" in log1)
    run(tmp, "etf", t0)
    run(tmp, "reactions", t0 + dt.timedelta(hours=30))
    data = json.loads((tmp / "data.json").read_text())

    ids = {i["id"] for i in data["items"]}
    check("signal post kept", "x:1975000000000000001" in ids)
    check("invented post absent", "x:1975000000000000099" not in ids)
    check("discovery post kept", "x:1975000000000000003" in ids)
    check("RSS items kept (XRP only)", sum(1 for i in data["items"] if i["kind"] == "news") >= 2)
    check("non-XRP RSS item filtered", not any("hashrate" in i["headline"].lower() for i in data["items"]))
    check("failing feed reported", any("XRPL Blog" in e for e in data["meta"]["feed_errors"]))
    spec = next(i for i in data["items"] if i["id"] == "x:1975000000000000003")
    check("price-target post rated noise", spec["impact"]["tier"] == "noise")
    whale = next(i for i in data["items"] if i["id"] == "x:1975000000000000002")
    check("whale post has source tier from config", whale["source"]["tier"] == "primary")
    check("24h reactions filled", all((i.get("reaction") or {}).get("vs_btc_24h") is not None for i in data["items"]))
    check("ETF daily rows present", data["etf"] and len(data["etf"]["daily"]) > 20)
    check("ETF per-fund flows present", "XRPZ" in data["etf"]["daily"][-1]["by_fund"])
    check("unreported ETF day kept apart from $0", data["etf"]["daily"][-1]["reported"] is False
          and data["etf"]["daily"][-1]["net_flow_usd"] is None and data["etf"]["as_of"] == data["etf"]["daily"][-2]["date"])
    check("$0 fund flow kept as 0, missing as null", data["etf"]["daily"][-2]["by_fund"].get("TOXR") is None)
    check("immaterial items capped at low", all(i["impact"]["tier"] in ("low", "noise") for i in data["items"] if "amendment" in i["headline"].lower()))
    check("spend recorded", data["meta"]["spend"]["usd"] > 0)
    check("theme states written", any(v["state"] for v in data["themes"].values()))
    th = {t["id"]: t for t in data["theories"]}
    check("theories copied", len(th) == 5 and th["price-target"]["tracked"] is False and "week" not in th["price-target"])
    check("items linked to theories", th["supply-shock"]["week"]["supports"] >= 1 and th["tokenization"]["week"]["supports"] >= 1)
    check("rumors don't count as evidence", th["collateral"]["week"]["claims"] >= 1)
    check("unknown theory ids dropped", all(l["id"] != "nonexistent" for i in data["items"] for l in i.get("theories", [])))
    check("theory evidence search ran", any("basel" in i["headline"].lower() for i in data["items"]))
    check("weekly note written", th["supply-shock"]["week"]["note"] == "Test note for supply-shock.")
    check("verdict step allowed with confirmed evidence", th["supply-shock"]["verdict"] == "supported" and th["supply-shock"]["changed"]["from"] == "partly")
    check("verdict change refused without confirmed evidence", th["tokenization"]["verdict"] == "partly")
    h = th["supply-shock"]["history"]
    check("theory history kept", h["supports"] >= 1 and h["items"] and h["weeks"] and h["weeks"][0]["note"] and h["changes"])

    # schedule: a second scan in the same slot must not run
    first = yaml.safe_load((REPO / "config" / "settings.yaml").read_text())["schedule"]["scan_hours_et"][0]
    ny7 = t0.astimezone(ZoneInfo("America/New_York")).replace(hour=first, minute=10, second=0)
    run(tmp, "auto", ny7.astimezone(dt.timezone.utc))
    log_b = run(tmp, "auto", (ny7 + dt.timedelta(minutes=50)).astimezone(dt.timezone.utc))
    tasks_line = next((l for l in log_b.splitlines() if "tasks:" in l), "")
    check("slot runs only once", "scan" not in tasks_line.split("tasks:")[-1])
    late = run(tmp, "auto", (ny7.replace(minute=5) + dt.timedelta(days=1, hours=2)).astimezone(dt.timezone.utc))
    check("missed slot caught up within the window", "missed its trigger" in late)

    # ten days later the weekly window is empty but the history is still there
    run(tmp, "rebuild", t0 + dt.timedelta(days=10))
    later = {t["id"]: t for t in json.loads((tmp / "data.json").read_text())["theories"]}["supply-shock"]
    check("history survives past the 7-day window", later["week"]["supports"] == 0 and later["history"]["supports"] >= 1)

    # claim tracker: claims from the scan above, then price calls scored after their deadline
    run(tmp, "claims", t0)
    cl = json.loads((tmp / "data.json").read_text())["claims"]
    by = {c["claim"]: c for c in cl["items"]}
    check("claims logged, no-claim posts dropped", len(cl["items"]) == 5)
    check("'false' without a cited source becomes unverified", by["A top-5 bank settles all payments in XRP"]["verdict"] == "unverified")
    check("'supported' without a cited source becomes unverified", by["RLUSD is the largest stablecoin by volume"]["verdict"] == "unverified")
    ath = by["XRP will make a new all-time high"]["prediction"]
    check("all-time-high call gets the Coinbase record as its target", ath["target_price"] and ath["target_price"] > 2.3
          and ath["direction"] == "above" and ath["outcome"] == "pending" and "record high" in ath["basis"])
    check("scorecard per account", {a["handle"] for a in cl["accounts"]} == {"@ExInfluencerA", "@ExInfluencerB"})
    run(tmp, "claims", t0 + dt.timedelta(days=5))
    cl = json.loads((tmp / "data.json").read_text())["claims"]
    by = {c["claim"]: c for c in cl["items"]}
    check("dated price call scored hit", by["XRP will trade at $2 or higher within a week"]["prediction"]["outcome"] == "hit")
    check("dated price call scored missed", by["XRP will trade at $10 within a month"]["prediction"]["outcome"] == "missed")
    a = next(x for x in cl["accounts"] if x["handle"] == "@ExInfluencerB")
    check("scorecard counts outcomes", a["calls"]["missed"] == 1 and a["calls"]["pending"] == 1 and a["total"] == 2)

    # handle check: unclear handles are reported, found ones confirmed
    run(tmp, "verify_handles", t0)
    hc = json.loads((tmp / "data" / "handle_check.json").read_text())["accounts"]
    st = {a["handle"]: a["status"] for a in hc}
    check("handle check covers every account", len(hc) >= 25 and st.get("Ripple") == "found")
    check("handle check flags an unclear account", st.get("xrpl_commons") == "unclear")

    width = max(len(n) for n, _ in checks)
    for n, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {n.ljust(width)}")
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed · output in {tmp}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
