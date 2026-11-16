"""SEBI listing pages (press releases, orders, circulars).

The SEBI RSS feed is unreliable, so these HTML listings are the real source. They carry
only a date, not a time, so items get published_precision="day" and are excluded from lag
percentiles.
"""
from __future__ import annotations

import re
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ..models import IST, Item, now_utc
from .base import PollResult, Source

_ID = re.compile(r"_(\d+)\.html")


def parse_listing(html: str, rank: int, seen_at: datetime, category: str) -> list[Item]:
    items = []
    for tr in HTMLParser(html).css("tr"):
        tds = tr.css("td")
        a = tr.css_first("a")
        if len(tds) < 2 or a is None:
            continue
        try:
            day = datetime.strptime(tds[0].text(strip=True), "%b %d, %Y").replace(tzinfo=IST)
        except ValueError:
            continue
        href = a.attributes.get("href") or ""
        m = _ID.search(href)
        items.append(Item(
            source="sebi", source_rank=rank,
            id=m.group(1) if m else href,
            title=(a.attributes.get("title") or a.text(strip=True)).strip(),
            url=href, published_at=day, published_precision="day",
            first_seen_at=seen_at, entity="SEBI", category=category,
            raw={"date": tds[0].text(strip=True), "href": href,
                 "cells": [td.text(strip=True) for td in tds]},
        ))
    return items


class SebiHtmlSource(Source):
    type = "sebi_html"
    browser = True

    def endpoints(self) -> list[str]:
        return [u["url"] for u in self.cfg["urls"]]

    async def poll(self) -> list[PollResult]:
        out = []
        for u in self.cfg["urls"]:
            f = await self.fetcher.get(u["url"], expect="html")
            res = PollResult(u["url"], f)
            if f.blocked:
                res.events.append("blocked")
            elif f.ok:
                res.items = parse_listing(f.response.text, self.rank, now_utc(), u.get("category", ""))
                if not res.items:
                    res.note = "page fetched but no rows parsed (layout change?)"
            out.append(res)
        return out
