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
    check("spend recorded", data["meta"]["spend"]["usd"] > 0)
    check("theme states written", any(v["state"] for v in data["themes"].values()))
    check("theories copied", len(data["theories"]) == 5)

    # schedule: a second scan in the same slot must not run
    ny7 = t0.astimezone(ZoneInfo("America/New_York")).replace(hour=7, minute=10, second=0)
    run(tmp, "auto", ny7.astimezone(dt.timezone.utc))
    log_b = run(tmp, "auto", (ny7 + dt.timedelta(minutes=50)).astimezone(dt.timezone.utc))
    check("slot runs only once", "tasks: reactions only" in log_b)

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
