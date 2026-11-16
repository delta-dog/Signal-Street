"""The exchanges' own RSS feeds of corporate announcements.

NSE: https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml (listed on NSE's RSS page,
     served from the archive host rather than the /api/ path that robots.txt disallows)
BSE: https://www.bseindia.com/data/xml/announcements.xml

Both stamp items '16-Nov-2026 14:16:53' in IST. We turn each item into the same shape the JSON
API adapters produce (symbol / BSE code, company, subject) so rules, matching and the
cross-exchange dedupe treat them alike.
"""
from __future__ import annotations

import re
from datetime import tzinfo

import feedparser

from ..models import Item, now_utc
from .rss import RssSource, parse_loose_date

_NSE_SYM = re.compile(r"/corporate/([A-Z0-9&\-]+)_\d{8,}")
_BSE_CODE = re.compile(r"\((\d{6})\)\s*$")


def parse_nse_rss(source: str, rank: int, content: bytes, tz: tzinfo) -> list[Item]:
    items, seen_at = [], now_utc()
    for e in feedparser.parse(content).entries:
        link = e.get("link") or ""
        desc = " ".join((e.get("description") or e.get("summary") or "").split())
        body, _, subject = desc.partition("|SUBJECT:")
        subject = subject.strip()
        m = _NSE_SYM.search(link)
        sym = m.group(1) if m else None
        company = (e.get("title") or "").strip()
        pub = parse_loose_date(e.get("published") or "", tz)
        items.append(Item(
            source=source, source_rank=rank, id=link or f"{company}|{e.get('published')}",
            title=f"{sym or company} - {subject or body}: {body}".strip()[:500] if subject else f"{sym or company} - {body}"[:500],
            url=link, published_at=pub, published_precision="s" if pub else "none", first_seen_at=seen_at,
            symbol=sym, entity=company, category=subject or None,
            raw={"title": company, "link": link, "published": e.get("published"), "description": desc},
        ))
    return items


def parse_bse_rss(source: str, rank: int, content: bytes, tz: tzinfo) -> list[Item]:
    items, seen_at = [], now_utc()
    for e in feedparser.parse(content).entries:
        link = e.get("link") or ""
        title = (e.get("title") or "").strip()
        m = _BSE_CODE.search(title)
        code = e.get("scripcode") or (m.group(1) if m else None)
        company = _BSE_CODE.sub("", title).strip()
        desc = " ".join((e.get("description") or e.get("summary") or "").split())
        pub = parse_loose_date(e.get("published") or "", tz)
        items.append(Item(
            source=source, source_rank=rank, id=link.rsplit("/", 1)[-1] or f"{title}|{e.get('published')}",
            title=f"{company} - {desc}"[:500], url=link, published_at=pub, published_precision="s" if pub else "none",
            first_seen_at=seen_at, symbol=str(code) if code else None, entity=company,
            raw={"title": title, "link": link, "published": e.get("published"), "description": desc},
        ))
    return items


class NseRssSource(RssSource):
    type = "nse_rss"
    browser = True

    def parse(self, content, tz, category):
        return parse_nse_rss(self.name, self.rank, content, tz)


class BseRssSource(RssSource):
    type = "bse_rss"
    browser = True

    def parse(self, content, tz, category):
        return parse_bse_rss(self.name, self.rank, content, tz)
