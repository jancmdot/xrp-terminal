"""Shared helpers: paths, time, HTTP with retries, JSON/YAML IO."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(os.environ.get("XRPTERM_ROOT", Path(__file__).resolve().parent.parent))
CONFIG = ROOT / "config"
DATA_DIR = ROOT / "data"
NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
UA = "xrp-news-terminal/0.1 (+https://github.com)"

log = logging.getLogger("xrpterm")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


# ---------------------------------------------------------------- time
def now_utc() -> dt.datetime:
    fake = os.environ.get("XRPTERM_NOW")
    if fake:
        return parse_iso(fake)
    return dt.datetime.now(UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC)


# ---------------------------------------------------------------- io
def load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- http
class HttpError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, body: str = ""):
        super().__init__(msg)
        self.status = status
        self.body = body


def http(method: str, url: str, *, retries: int = 2, timeout: float = 30, **kw) -> requests.Response:
    """HTTP with retries on 429/5xx/connection errors. Raises HttpError on final failure."""
    if os.environ.get("XRPTERM_FAKE"):
        from . import fake
        return fake.respond(method, url, **kw)
    headers = {"User-Agent": UA, **kw.pop("headers", {})}
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.request(method, url, headers=headers, timeout=timeout, **kw)
        except requests.RequestException as e:
            last = e
            log.warning("HTTP %s %s failed (%s), attempt %d", method, short(url), e.__class__.__name__, attempt + 1)
        else:
            if r.status_code < 400:
                return r
            if r.status_code not in (429, 500, 502, 503, 504):
                raise HttpError(f"HTTP {r.status_code} from {short(url)}", r.status_code, r.text[:500])
            last = HttpError(f"HTTP {r.status_code} from {short(url)}", r.status_code, r.text[:500])
            wait = float(r.headers.get("Retry-After") or 0)
            log.warning("HTTP %s from %s, attempt %d", r.status_code, short(url), attempt + 1)
            if wait:
                time.sleep(min(wait, 30))
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    if isinstance(last, HttpError):
        raise last
    raise HttpError(f"{method} {short(url)} failed: {last}")


def short(url: str) -> str:
    return re.sub(r"\?.*$", "", url)[:90]


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"&nbsp;|&#160;", " ", s)
    s = re.sub(r"&amp;", "&", s)
    s = re.sub(r"&(#?\w+);", " ", s)
    return re.sub(r"\s+", " ", s).strip()
