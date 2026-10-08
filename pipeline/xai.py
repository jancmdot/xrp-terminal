"""Grok via the xAI Responses API, with spend tracking against a monthly cap."""
from __future__ import annotations

import json
import os
import re

from .util import http, log, now_utc


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Estimated spend per calendar month, stored in state['spend']."""

    def __init__(self, state: dict, settings: dict):
        self.state = state
        self.cfg = settings["budget"]
        self.month = now_utc().strftime("%Y-%m")
        self.state.setdefault("spend", {}).setdefault(self.month, {"usd": 0.0, "calls": 0, "x_posts": 0, "x_users": 0, "web_searches": 0})

    @property
    def m(self) -> dict:
        return self.state["spend"][self.month]

    def remaining(self) -> float:
        return float(self.cfg["monthly_usd"]) - self.m["usd"]

    def check(self) -> None:
        if self.remaining() <= 0:
            raise BudgetExceeded(f"Monthly Grok budget of ${self.cfg['monthly_usd']} reached (spent ${self.m['usd']:.2f})")

    def add(self, usage: dict | None, label: str) -> float:
        u = usage or {}
        c = self.cfg
        inp = int(u.get("input_tokens") or 0)
        cached = int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        tools = u.get("server_side_tool_usage_details") or {}
        posts = int(tools.get("x_posts_fetched") or 0)
        users = int(tools.get("x_users_fetched") or 0)
        webs = int(tools.get("web_search_calls") or 0)
        usd = ((inp - cached) * c["price_input_per_m"] + cached * c["price_cached_input_per_m"] + out * c["price_output_per_m"]) / 1e6
        usd += posts * c["price_x_post"] + users * c["price_x_user"] + webs * c["price_web_search"]
        m = self.m
        m["usd"] = round(m["usd"] + usd, 4)
        m["calls"] += 1
        m["x_posts"] += posts
        m["x_users"] += users
        m["web_searches"] += webs
        log.info("grok %-22s in=%d out=%d posts=%d users=%d web=%d  ≈$%.3f  (month $%.2f / $%s)",
                 label, inp, out, posts, users, webs, usd, m["usd"], c["monthly_usd"])
        return usd


class Grok:
    def __init__(self, settings: dict, budget: Budget):
        self.key = os.environ.get("XAI_API_KEY", "")
        self.cfg = settings["xai"]
        self.budget = budget

    @property
    def available(self) -> bool:
        return bool(self.key)

    def respond(self, *, system: str, user: str, schema: dict, name: str, tools: list | None = None, label: str = "") -> tuple[dict, dict]:
        """One Responses API call with a JSON schema. Returns (parsed, info) where info has citations and cost."""
        self.budget.check()
        body = {
            "model": self.cfg["model"],
            "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            "include": ["no_inline_citations"],
        }
        if tools:
            body["tools"] = tools
        r = http("POST", f"{self.cfg['base_url'].rstrip('/')}/responses",
                 headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                 json=body, timeout=self.cfg.get("timeout_s", 240), retries=1)
        data = r.json()
        cost = self.budget.add(data.get("usage"), label or name)
        for it in data.get("output") or []:
            if it.get("type") == "message":
                continue
            brief = {k: v for k, v in it.items() if k not in ("id", "status", "results", "output", "content")}
            log.info("    tool %s", json.dumps(brief, ensure_ascii=False)[:300])
        text = output_text(data)
        parsed = parse_json(text)
        cites = set(data.get("citations") or [])
        for item in data.get("output") or []:
            for c in item.get("content") or []:
                for a in c.get("annotations") or []:
                    if a.get("url"):
                        cites.add(a["url"])
        return parsed, {"citations": sorted(cites), "cost": cost}


def output_text(data: dict) -> str:
    parts = []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(c["text"])
    if not parts and data.get("output_text"):
        parts.append(data["output_text"])
    return "\n".join(parts)


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("Grok returned no JSON")
