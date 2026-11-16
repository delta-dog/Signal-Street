"""PIB releases from the English 'all releases' page, filtered by ministry.

Why not PIB RSS: from this machine RssMain.aspx returns Hindi titles whatever Lang= is,
has no pubDate and no ministry field. allRel.aspx groups English releases under a
ministry heading. Neither gives a time, so for releases from a watched ministry we fetch
the iframe release page (capped per poll; ~2.5 MB each, so keep the cap low) and read its
'Posted On: 16 NOV 2026 12:00PM by PIB Delhi' line. PressReleaseDetail.aspx only shows a
site-wide 'Last Updated' time, which is NOT the release time.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import IST, Item, now_utc
from .base import PollResult, Source

BASE = "https://pib.gov.in/"
_PRID = re.compile(r"PRID=(\d+)")
DETAIL = "https://pib.gov.in/PressReleaseIframePage.aspx?PRID={}&reg=3&lang=1"
# The label is translated ('Posted On' / Hindi), the 'by PIB <city>' suffix is not.
_POSTED = re.compile(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)\s+by\s+PIB", re.I)


def parse_all_releases(html: str, rank: int, seen_at: datetime) -> list[Item]:
    items = []
    for h3 in HTMLParser(html).css("h3"):
        ministry = h3.text(strip=True)
        ul = h3.next
        while ul is not None and ul.tag != "ul":
            ul = ul.next
        if ul is None:
            continue
        for a in ul.css("a"):
            href = a.attributes.get("href") or ""
            m = _PRID.search(href)
            if not m:
                continue
            items.append(Item(
                source="pib", source_rank=rank, id=m.group(1),
                title=(a.attributes.get("title") or a.text(strip=True)).strip(),
                url=urljoin(BASE, href), published_at=None, published_precision="none",
                first_seen_at=seen_at, entity=ministry, category=ministry,
                raw={"ministry": ministry, "href": href},
            ))
    return items


def parse_posted_on(html: str) -> datetime | None:
    m = _POSTED.search(html)
    if not m:
        return None
    try:
        day = " ".join(m.group(1).split()).title()
        return datetime.strptime(f"{day} {m.group(2).replace(' ', '').upper()}",
                                 "%d %b %Y %I:%M%p").replace(tzinfo=IST)
    except ValueError:
        return None


class PibSource(Source):
    type = "pib_html"
    browser = True

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.known: set[str] = set()

    def endpoints(self) -> list[str]:
        return [self.cfg["url"]]

    def watched(self, item: Item) -> bool:
        m = (item.entity or "").lower()
        return any(w.lower() in m for w in self.cfg.get("ministries", []))

    async def poll(self) -> list[PollResult]:
        f = await self.fetcher.get(self.cfg["url"], expect="html")
        res = PollResult(self.cfg["url"], f)
        if f.blocked:
            res.events.append("blocked")
        if not f.ok:
            return [res]
        all_items = parse_all_releases(f.response.text, self.rank, now_utc())
        mine = [i for i in all_items if self.watched(i)]
        new = [i for i in mine if i.id not in self.known]
        budget = self.cfg.get("detail_fetch_max", 5)
        timed = 0
        for item in new[:budget]:
            d = await self.fetcher.get(DETAIL.format(item.id), expect="html")
            if d.ok:
                item.published_at = parse_posted_on(d.response.text)
                if item.published_at:
                    item.published_precision = "s"
                    timed += 1
        self.known.update(i.id for i in mine)
        res.items = mine
        res.note = (f"{len(all_items)} releases on page, {len(mine)} from watched ministries; "
                    f"timestamp read for {timed}/{min(len(new), budget)} via detail page")
        if not all_items:
            res.note = "page fetched but no releases parsed (layout change?)"
        return [res]
