from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..http import Fetched, Fetcher, HostLimiter, make_client
from ..models import TIER_RANK, Item


@dataclass
class PollResult:
    """Outcome of hitting one endpoint once. A source may hit several endpoints per poll."""
    endpoint: str
    fetched: Fetched | None
    items: list[Item] = field(default_factory=list)
    note: str = ""
    events: list[str] = field(default_factory=list)  # e.g. "cookie_refresh", "blocked"


def status_of(r: PollResult) -> str:
    """OK / EMPTY / NOT_MODIFIED / BLOCKED / ERROR / HTTP nnn for one endpoint hit."""
    f = r.fetched
    if f is None or f.error:
        return "ERROR"
    if f.not_modified:
        return "NOT_MODIFIED"
    if f.blocked or "blocked" in r.events:
        return "BLOCKED"
    code = f.response.status_code
    if code == 200:
        return "OK" if r.items else "EMPTY"
    return f"HTTP {code}"


class Source:
    type = "base"
    browser = False   # needs browser-like headers (exchange sites reject anything else)

    def __init__(self, name: str, cfg: dict, limiter: HostLimiter,
                 user_agent: str, transport: httpx.AsyncBaseTransport | None = None):
        self.name = name
        self.cfg = cfg
        self.tier = cfg.get("tier", "media")
        self.rank = TIER_RANK[self.tier]
        self.enabled = cfg.get("enabled", True)
        self.burst = False   # set by the runner when burst mode is switched on
        browser = cfg.get("browser_headers", self.browser)
        self.fetcher = Fetcher(make_client(browser=browser, user_agent=user_agent,
                                           timeout=cfg.get("timeout", 15.0),
                                           transport=transport), limiter)

    def endpoints(self) -> list[str]:
        return [u["url"] if isinstance(u, dict) else u for u in self.cfg.get("urls", [])]

    async def poll(self) -> list[PollResult]:
        raise NotImplementedError

    async def aclose(self) -> None:
        await self.fetcher.client.aclose()


def canonical_url(url: str) -> str:
    """Strip tracking params and fragments so the same story dedupes across feeds."""
    p = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in {"ref", "fbclid", "gclid", "from"}]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, urlencode(q), ""))
