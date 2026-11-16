"""Polite HTTP: one shared limiter per host, conditional GETs, block/captcha detection.

No proxy rotation, no CAPTCHA solving. If a site blocks us we record it and back off.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

# Used only for the exchange/regulator sites that reject non-browser clients outright.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Markers of a bot wall / challenge page. Only checked when we did not get what we expected.
CHALLENGE_MARKERS = (
    "captcha", "are you a robot", "access denied", "request unsuccessful",
    "_incapsula_resource", "cf-chl", "attention required", "bot detection",
)


class HostLimiter:
    """Serialises requests per host and enforces a minimum gap between them."""

    def __init__(self, min_interval: dict[str, float], default: float = 1.0):
        self._min = min_interval
        self._default = default
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self._min.get(host, self._default)
            delay = self._last.get(host, 0.0) + gap - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


@dataclass
class Fetched:
    response: httpx.Response | None
    elapsed_ms: float
    not_modified: bool = False
    blocked: str | None = None   # reason, if this looks like a block/captcha
    error: str | None = None     # transport error text

    @property
    def ok(self) -> bool:
        return self.response is not None and self.response.status_code == 200 and not self.blocked


def detect_block(resp: httpx.Response, expect: str) -> str | None:
    """expect: 'json' | 'xml' | 'html'. Returns a short reason or None."""
    code = resp.status_code
    if code in (401, 403, 429, 503):
        body = resp.text[:4000].lower()
        hit = next((m for m in CHALLENGE_MARKERS if m in body), None)
        return f"HTTP {code}" + (f" ({hit})" if hit else "")
    if code != 200:
        return None
    ctype = resp.headers.get("content-type", "").lower()
    if expect in ("json", "xml") and "html" in ctype:
        body = resp.text[:4000].lower()
        hit = next((m for m in CHALLENGE_MARKERS if m in body), None)
        if hit:
            return f"challenge page ({hit})"
    return None


def backoff_delay(failures: int, base: float, cap: float = 600.0) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(cap, base * (2 ** min(failures, 10))))


class Fetcher:
    """Wraps an httpx.AsyncClient with the limiter and an ETag/Last-Modified cache."""

    def __init__(self, client: httpx.AsyncClient, limiter: HostLimiter):
        self.client = client
        self.limiter = limiter
        self.validators: dict[str, dict[str, str]] = {}

    async def get(self, url: str, *, expect: str, params: dict | None = None,
                  headers: dict | None = None, conditional: bool = False) -> Fetched:
        hdrs = dict(headers or {})
        key = str(httpx.URL(url, params=params))
        if conditional and key in self.validators:
            v = self.validators[key]
            if "etag" in v:
                hdrs["If-None-Match"] = v["etag"]
            if "last-modified" in v:
                hdrs["If-Modified-Since"] = v["last-modified"]
        await self.limiter.wait(urlsplit(url).hostname or "")
        t0 = time.perf_counter()
        try:
            resp = await self.client.get(url, params=params, headers=hdrs)
        except httpx.HTTPError as e:
            return Fetched(None, (time.perf_counter() - t0) * 1000, error=f"{type(e).__name__}: {e}"[:160])
        elapsed = (time.perf_counter() - t0) * 1000
        if resp.status_code == 304:
            return Fetched(resp, elapsed, not_modified=True)
        if conditional and resp.status_code == 200:
            v = {k: resp.headers[k] for k in ("etag", "last-modified") if k in resp.headers}
            if v:
                self.validators[key] = v
        return Fetched(resp, elapsed, blocked=detect_block(resp, expect))


def make_client(*, browser: bool = False, user_agent: str | None = None, timeout: float = 15.0,
                transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    headers = dict(BROWSER_HEADERS) if browser else {"User-Agent": user_agent or "marketmon"}
    return httpx.AsyncClient(
        http2=transport is None, headers=headers, follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=10.0), transport=transport,
    )
