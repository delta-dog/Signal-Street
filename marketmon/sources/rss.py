"""Generic RSS/Atom source (RBI, SEBI RSS, media, Fed, Google News)."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone, tzinfo
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser

from ..models import IST, Item, now_utc
from .base import PollResult, Source, canonical_url

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"


def _feeds(cfg: dict, key: str = "urls") -> list[tuple[str, str | None]]:
    """[(url, category)]. A url entry may be a string or {url, category}."""
    out = [(u["url"], u.get("category")) if isinstance(u, dict) else (u, None) for u in cfg.get(key, [])]
    if key == "urls":
        out += [(GOOGLE_NEWS.format(q=quote_plus(q)), q) for q in cfg.get("google_news_queries", [])]
    return out


def parse_loose_date(raw: str, naive_tz: tzinfo = IST) -> datetime | None:
    """Dates feedparser can't read: RFC 822 without a zone (RBI: 'Mon, 16 Nov 2026 12:05:00') or the
    exchanges' '16-Nov-2026 14:16:53'. A missing zone means the source's local time."""
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        d = None
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                d = datetime.strptime(raw.strip(), fmt)
                break
            except ValueError:
                pass
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=naive_tz)
    return d


def parse_feed(source: str, rank: int, content: bytes, seen_at: datetime,
               naive_tz: tzinfo = IST, category: str | None = None) -> list[Item]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        link = e.get("link") or ""
        published, precision = None, "none"
        raw_date = (e.get("published") or e.get("updated") or "").strip()
        st = e.get("published_parsed") or e.get("updated_parsed")
        if st:
            # feedparser normalises to UTC struct_time
            published = datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
        elif raw_date:
            published = parse_loose_date(raw_date, naive_tz)
        if published:
            precision = "s" if ":" in raw_date else "day"
        tags = [t.get("term") for t in e.get("tags", []) if t.get("term")]
        # Hard key: media -> canonical URL (utm_* stripped); others -> feed guid.
        item_id = (canonical_url(link) if rank >= 3 and link else None) or e.get("id") or canonical_url(link)
        items.append(Item(
            source=source, source_rank=rank,
            id=item_id or e.get("title", ""),
            title=" ".join((e.get("title") or "").split()),
            url=link,
            published_at=published, published_precision=precision,
            first_seen_at=seen_at,
            category=category or (tags[0] if tags else None),
            raw={**{k: e.get(k) for k in ("id", "title", "link", "published", "updated")},
                 # Aggregators (Google News) name the original outlet's site: the dashboard shows its logo.
                 "source_url": (e.get("source") or {}).get("href")},
        ))
    return items


class RssSource(Source):
    type = "rss"

    def feeds(self) -> list[tuple[str, str | None]]:
        if self.burst and self.cfg.get("burst_urls"):
            return _feeds(self.cfg, "burst_urls")
        return _feeds(self.cfg)

    def endpoints(self) -> list[str]:
        return [u for u, _ in _feeds(self.cfg)]

    def parse(self, content: bytes, tz: tzinfo, category: str | None) -> list[Item]:
        return parse_feed(self.name, self.rank, content, now_utc(), tz, category)

    async def poll(self) -> list[PollResult]:
        out = []
        tz = ZoneInfo(self.cfg.get("naive_tz", "Asia/Kolkata"))
        for url, category in self.feeds():
            f = await self.fetcher.get(url, expect="xml", conditional=True)
            res = PollResult(url, f)
            if f.ok:
                res.items = self.parse(f.response.content, tz, category)
                if not res.items and b"<item" not in f.response.content and b"<entry" not in f.response.content:
                    res.note = "valid response but no <item>/<entry> elements"
            elif f.blocked:
                res.events.append("blocked")
            out.append(res)
        return out
