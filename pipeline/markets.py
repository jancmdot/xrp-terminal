"""Market data: Coinbase candles (price reactions, spot volume) and CoinGlass (ETF flows, positioning)."""
from __future__ import annotations

import datetime as dt
import os
from bisect import bisect_right

from .util import UTC, HttpError, http, iso, log, now_utc

CB = "https://api.exchange.coinbase.com/products/{pid}/candles"
CG = "https://open-api-v4.coinglass.com"
ISSUERS = {"XRPC": "Canary Capital", "XRPZ": "Franklin Templeton", "XRP": "Bitwise", "GXRP": "Grayscale",
           "TOXR": "21Shares", "XRPR": "REX-Osprey"}


# ---------------------------------------------------------------- Coinbase
class Candles:
    """5-minute candles fetched in 25-hour windows and cached for the run."""
    G = 300
    SPAN = 300 * 300

    def __init__(self):
        self.cache: dict[str, dict[int, tuple]] = {}
        self.fetched: dict[str, set] = {}

    def _fetch_window(self, pid: str, start: int) -> None:
        w = start // self.SPAN
        if w in self.fetched.setdefault(pid, set()):
            return
        a, b = w * self.SPAN, (w + 1) * self.SPAN - self.G     # 300 candles, Coinbase's per-request maximum
        r = http("GET", CB.format(pid=pid), params={"granularity": self.G,
                 "start": dt.datetime.fromtimestamp(a, UTC).isoformat(), "end": dt.datetime.fromtimestamp(b, UTC).isoformat()},
                 timeout=20)
        rows = r.json()
        store = self.cache.setdefault(pid, {})
        for x in rows if isinstance(rows, list) else []:
            store[int(x[0])] = (float(x[3]), float(x[4]))   # open, close
        self.fetched[pid].add(w)

    def price_at(self, pid: str, ts: int) -> float | None:
        """Open of the 5-minute candle containing ts (or the nearest earlier one within 30 minutes)."""
        self._fetch_window(pid, ts)
        store = self.cache.get(pid, {})
        if not store:
            return None
        keys = sorted(store)
        i = bisect_right(keys, ts) - 1
        if i < 0 or ts - keys[i] > 1800:
            return None
        return store[keys[i]][0]


HORIZONS = {"1h": 3600, "4h": 4 * 3600, "24h": 24 * 3600}


def fill_reactions(events: dict, candles: Candles) -> int:
    """Fill XRP and XRP-minus-BTC returns for every window that has fully elapsed. Returns how many cells were filled."""
    now = int(now_utc().timestamp()) - 600      # leave 10 minutes for the last candle to settle
    filled = 0
    for ev in events.values():
        rx = ev.setdefault("reaction", {})
        t0 = int(dt.datetime.fromisoformat(ev["published_at"].replace("Z", "+00:00")).timestamp())
        todo = [h for h, s in HORIZONS.items() if f"xrp_{h}" not in rx and t0 + s <= now]
        if not todo:
            continue
        if now - t0 > 14 * 86400:      # too old to backfill sensibly; mark as unavailable
            for h in todo:
                rx[f"xrp_{h}"] = rx[f"vs_btc_{h}"] = None
            continue
        try:
            x0, b0 = candles.price_at("XRP-USD", t0), candles.price_at("BTC-USD", t0)
            for h in todo:
                t1 = t0 + HORIZONS[h]
                x1, b1 = candles.price_at("XRP-USD", t1), candles.price_at("BTC-USD", t1)
                if None in (x0, x1):
                    continue
                xr = (x1 / x0 - 1) * 100
                rx[f"xrp_{h}"] = round(xr, 2)
                rx[f"vs_btc_{h}"] = round(xr - (b1 / b0 - 1) * 100, 2) if None not in (b0, b1) else None
                filled += 1
        except HttpError as e:
            log.warning("price fetch failed for %s: %s", ev["id"], e)
    return filled


def daily_spot_volume() -> dict[str, float]:
    """Coinbase XRP-USD daily volume in USD by UTC date (last ~300 days)."""
    r = http("GET", CB.format(pid="XRP-USD"), params={"granularity": 86400}, timeout=20)
    out = {}
    for x in r.json():
        d = dt.datetime.fromtimestamp(int(x[0]), UTC).strftime("%Y-%m-%d")
        out[d] = float(x[5]) * float(x[4])
    return out


# ---------------------------------------------------------------- CoinGlass
def _cg(path: str, **params) -> list:
    key = os.environ.get("COINGLASS_API_KEY", "")
    r = http("GET", CG + path, params=params, headers={"CG-API-KEY": key, "accept": "application/json"}, timeout=30)
    data = r.json()
    if str(data.get("code")) not in ("0", "200"):
        raise HttpError(f"CoinGlass {path}: {data.get('msg') or data.get('code')}")
    return data.get("data") or []


def coinglass_available() -> bool:
    return bool(os.environ.get("COINGLASS_API_KEY"))


def etf_flows() -> dict:
    """Daily US spot XRP ETF flows, oldest first, in the data.json etf shape."""
    rows = _cg("/api/etf/xrp/flow-history")
    rows = sorted(rows, key=lambda r: r.get("timestamp") or 0)
    try:
        vol = daily_spot_volume()
    except HttpError as e:
        log.warning("spot volume unavailable: %s", e)
        vol = {}
    daily, cum, tickers, fund_cum = [], 0.0, [], {}
    for r in rows:
        d = dt.datetime.fromtimestamp((r.get("timestamp") or 0) / 1000, UTC).strftime("%Y-%m-%d")
        net = float(r.get("flow_usd") or 0)
        by = {}
        for f in r.get("etf_flows") or []:
            t = f.get("etf_ticker")
            if not t:
                continue
            if t not in tickers:
                tickers.append(t)
            v = float(f.get("flow_usd") or 0)
            by[t] = round(v)
            fund_cum[t] = fund_cum.get(t, 0) + v
        cum += net
        daily.append({"date": d, "net_flow_usd": round(net), "cum_flow_usd": round(cum), "total_aum_usd": None,
                      "price_usd": r.get("price_usd"), "spot_volume_usd": round(vol[d]) if d in vol else None, "by_fund": by})
    funds = [{"ticker": t, "issuer": ISSUERS.get(t, ""), "aum_usd": None, "cum_flow_usd": round(fund_cum.get(t, 0))} for t in tickers]
    return {"as_of": daily[-1]["date"] if daily else None, "funds": funds, "daily": daily[-60:],
            "aum_available": False, "flows_since": daily[0]["date"] if daily else None}


def positioning() -> dict | None:
    """XRP funding rate and open interest context (4h bars, the shortest the entry plan allows)."""
    try:
        fr = _cg("/api/futures/funding-rate/oi-weight-history", symbol="XRP", interval="4h", limit=180)
        oi = _cg("/api/futures/open-interest/aggregated-history", symbol="XRP", interval="4h", limit=180)
    except HttpError as e:
        log.warning("positioning unavailable: %s", e)
        return None
    if not fr or not oi:
        return None
    rates = [float(x["close"]) for x in fr if x.get("close") is not None]
    ois = [float(x["close"]) for x in oi if x.get("close") is not None]
    if not rates or len(ois) < 7:
        return None
    cur = rates[-1]
    pct = sum(1 for r in rates if r <= cur) / len(rates)
    oi_chg = (ois[-1] / ois[-7] - 1) * 100
    return {"as_of": iso(now_utc()), "funding_rate": cur, "funding_pctile_30d": round(pct, 2),
            "open_interest_usd": round(ois[-1]), "oi_change_24h_pct": round(oi_chg, 2)}


def positioning_text(p: dict | None) -> str:
    if not p:
        return ""
    lvl = "high" if p["funding_pctile_30d"] >= 0.8 else "low" if p["funding_pctile_30d"] <= 0.2 else "normal"
    return (f"XRP perpetual funding is {lvl} for the past 30 days ({p['funding_pctile_30d']:.0%} percentile); "
            f"open interest {p['oi_change_24h_pct']:+.1f}% over 24h.")
