"""BSE corporate announcements via the unofficial api.bseindia.com JSON endpoint.

Endpoint and headers as used by the open-source BseIndiaApi package (BSE.announcements),
verified by check-sources. We query page 1 per category instead of paging the full day.
"""
from __future__ import annotations

from datetime import datetime

from ..models import IST, Item, now_utc
from .base import PollResult, Source

API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.bseindia.com",
    "Referer": "https://www.bseindia.com/",
    "Sec-Fetch-Site": "same-site",   # BSE's edge returns 403 without this
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}
PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{}"


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def parse_announcements(payload: dict, rank: int, seen_at: datetime) -> list[Item]:
    items = []
    for r in (payload or {}).get("Table", []) or []:
        published = _parse_dt(r.get("DissemDT")) or _parse_dt(r.get("DT_TM")) or _parse_dt(r.get("NEWS_DT"))
        att = r.get("ATTACHMENTNAME")
        items.append(Item(
            source="bse", source_rank=rank,
            id=str(r.get("NEWSID")),
            title=f"{r.get('SLONGNAME') or r.get('SCRIP_CD')} - {r.get('HEADLINE') or r.get('NEWSSUB') or ''}"[:500],
            url=PDF.format(att) if att else (r.get("NSURL") or "https://www.bseindia.com/corporates/ann.html"),
            published_at=published, published_precision="s" if published else "none",
            first_seen_at=seen_at,
            symbol=str(r.get("SCRIP_CD")) if r.get("SCRIP_CD") else None,
            entity=r.get("SLONGNAME"),
            category="/".join(x for x in (r.get("CATEGORYNAME"), r.get("SUBCATNAME")) if x) or None,
            raw=r,
        ))
    return items


class BseSource(Source):
    type = "bse"
    browser = True

    def endpoints(self) -> list[str]:
        return [f"{API} [{c}]" for c in self.cfg.get("categories", ["-1"])]

    async def poll(self) -> list[PollResult]:
        day = datetime.now(IST).strftime("%Y%m%d")
        out = []
        for cat in self.cfg.get("categories", ["-1"]):
            params = {"pageno": 1, "strCat": cat, "subcategory": "-1", "strPrevDate": day,
                      "strToDate": day, "strSearch": "P", "strScrip": "", "strType": "C"}
            f = await self.fetcher.get(API, expect="json", params=params, headers=HEADERS)
            res = PollResult(f"{API} [{cat}]", f)
            if f.blocked:
                res.events.append("blocked")
            elif f.ok:
                try:
                    payload = f.response.json()
                    res.items = parse_announcements(payload, self.rank, now_utc())
                    total = (payload.get("Table1") or [{}])[0].get("ROWCNT")
                    if total is not None:
                        res.note = f"{total} today in category"
                except ValueError:
                    res.note = "200 but body is not JSON"
            out.append(res)
        return out
