"""NSE corporate announcements (unofficial JSON endpoint behind a cookie session).

robots.txt disallows /api/ -- this source is individually disable-able in sources.yaml.
Redistributing exchange data requires a licence from NSE.
"""
from __future__ import annotations

from datetime import datetime

from ..http import Fetched
from ..models import IST, Item, now_utc
from .base import PollResult, Source

HOME = "https://www.nseindia.com/"
API = "https://www.nseindia.com/api/corporate-announcements"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
API_HEADERS = {"Referer": REFERER, "Accept": "application/json, text/plain, */*",
               "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}


def needs_cookie_refresh(f: Fetched) -> bool:
    if f.response is None:
        return False
    r = f.response
    if r.status_code in (401, 403):
        return True
    return r.status_code == 200 and "resource not found" in r.text[:500].lower()


def _parse_dt(s: str | None) -> datetime | None:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s or "", fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def parse_announcements(payload, rank: int, seen_at: datetime) -> list[Item]:
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    items = []
    for r in rows or []:
        published = _parse_dt(r.get("exchdisstime")) or _parse_dt(r.get("an_dt")) or _parse_dt(r.get("sort_date"))
        title = r.get("desc") or ""
        if r.get("attchmntText"):
            title = f"{title}: {r['attchmntText']}"
        items.append(Item(
            source="nse", source_rank=rank,
            id=str(r.get("seq_id") or f"{r.get('symbol')}|{r.get('an_dt')}|{r.get('desc')}"),
            title=f"{r.get('symbol', '')} - {title}".strip(" -")[:500],
            url=r.get("attchmntFile") or REFERER,
            published_at=published, published_precision="s" if published else "none",
            first_seen_at=seen_at,
            symbol=r.get("symbol"), isin=r.get("sm_isin"), entity=r.get("sm_name"),
            category=r.get("desc"), raw=r,
        ))
    return items


class NseSource(Source):
    type = "nse"
    browser = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.warmed = False

    def endpoints(self) -> list[str]:
        return [API]

    async def warm(self) -> Fetched:
        """Fetch the home page so the client's cookie jar gets NSE's session cookies."""
        self.fetcher.client.cookies.clear()
        f = await self.fetcher.get(HOME, expect="html")
        self.warmed = f.ok
        return f

    async def poll(self) -> list[PollResult]:
        events = []
        if not self.warmed:
            w = await self.warm()
            if not w.ok:
                reason = w.blocked or w.error or f"HTTP {w.response.status_code}"
                return [PollResult(API, w, note=f"home page warm-up failed: {reason}",
                                   events=["blocked"] if w.blocked else [])]
        day = datetime.now(IST).strftime("%d-%m-%Y")
        params = {"index": "equities", "from_date": day, "to_date": day}
        f = await self.fetcher.get(API, expect="json", params=params, headers=API_HEADERS)
        if needs_cookie_refresh(f):
            events.append("cookie_refresh")
            await self.warm()
            f = await self.fetcher.get(API, expect="json", params=params, headers=API_HEADERS)
        res = PollResult(API, f, events=events)
        if f.blocked or needs_cookie_refresh(f):
            res.events.append("blocked")
            res.note = f.blocked or "still rejected after cookie refresh"
            self.warmed = False
        elif f.ok:
            try:
                res.items = parse_announcements(f.response.json(), self.rank, now_utc())
            except ValueError:
                res.note = "200 but body is not JSON"
        return [res]
