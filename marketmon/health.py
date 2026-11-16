"""`check-sources`: hit every configured endpoint once and print a health table."""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import now_utc
from .sources import PollResult, Source
from .sources.base import status_of


@dataclass
class Row:
    source: str
    enabled: bool
    endpoint: str
    status: str
    http: str
    items: int
    newest_age: str
    ms: str
    conditional: str
    note: str


def _future(r: PollResult) -> int:
    """Items stamped more than 2 min in the future: a bad source clock or zone label."""
    now = now_utc()
    return sum(1 for i in r.items if i.published_at and i.published_precision == "s"
               and (i.published_at - now).total_seconds() > 120)


def _age(r: PollResult) -> str:
    now = now_utc()
    timed = [i.published_at for i in r.items if i.published_at and i.published_precision == "s"
             and (i.published_at - now).total_seconds() <= 120]
    if timed:
        return _fmt_secs(max(0.0, (now - max(timed)).total_seconds()))
    dated = [i.published_at for i in r.items if i.published_at]
    if dated:
        return f"{max(dated):%d %b} (date only)"
    return "n/a (no time)" if r.items else "-"


def _fmt_secs(s: float) -> str:
    if s < 120:
        return f"{s:.0f}s"
    if s < 7200:
        return f"{s / 60:.0f}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.0f}d"


def _short(url: str, n: int = 58) -> str:
    u = re.sub(r"^https?://(www\.)?", "", url)
    return u if len(u) <= n else u[: n - 1] + "…"


async def _check_one(src: Source, record: Path | None) -> list[Row]:
    try:
        results = await src.poll()
        second = {}
        if src.type == "rss":
            # Re-poll once to see whether the server honours If-None-Match / If-Modified-Since.
            second = {r.endpoint: r for r in await src.poll()}
    except Exception as e:  # a broken adapter must not hide the rest of the table
        return [Row(src.name, src.enabled, "(adapter)", "ERROR", "", 0, "-", "", "", f"{type(e).__name__}: {e}"[:120])]
    rows = []
    for r in results:
        f = r.fetched
        resp = f.response if f else None
        cond = "-"
        if src.type == "rss" and resp is not None:
            again = second.get(r.endpoint)
            has_v = any(h in resp.headers for h in ("etag", "last-modified"))
            if again and again.fetched and again.fetched.not_modified:
                cond = "304 ok"
            elif has_v:
                cond = "no 304"
            else:
                cond = "none"
        note = r.note or (f.error if f and f.error else "") or (f.blocked if f and f.blocked else "")
        if "cookie_refresh" in r.events:
            note = ("cookie refreshed; " + note).strip("; ")
        if n := _future(r):
            note = (f"{n} item(s) dated in the future; " + note).strip("; ")
        rows.append(Row(
            source=src.name, enabled=src.enabled, endpoint=r.endpoint, status=status_of(r),
            http=(resp.http_version.replace("HTTP/", "h") + f" {resp.status_code}") if resp is not None else "",
            items=len(r.items), newest_age=_age(r), ms=f"{f.elapsed_ms:.0f}" if f else "",
            conditional=cond, note=note,
        ))
        if record is not None and resp is not None:
            _record(record, src.name, r, resp)
    return rows


def _record(root: Path, name: str, r: PollResult, resp) -> None:
    """Save the raw body so tests and --dry-run can replay real payloads."""
    root.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", _short(r.endpoint, 80)).strip("_")[:70]
    ext = {"json": "json", "xml": "xml", "rss": "xml", "html": "html"}
    ctype = resp.headers.get("content-type", "")
    kind = next((v for k, v in ext.items() if k in ctype), "txt")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (root / f"{name}__{slug}__{stamp}.{kind}").write_bytes(resp.content)
    meta = {"source": name, "endpoint": r.endpoint, "status": resp.status_code,
            "url": str(resp.request.url), "headers": dict(resp.headers), "fetched_at": stamp}
    (root / f"{name}__{slug}__{stamp}.meta.json").write_text(json.dumps(meta, indent=2))


async def check_sources(sources: list[Source], record: Path | None = None) -> list[Row]:
    # Sources run concurrently; the shared HostLimiter serialises requests per host.
    try:
        groups = await asyncio.gather(*(_check_one(s, record) for s in sources))
    finally:
        await asyncio.gather(*(s.aclose() for s in sources))
    return [row for g in groups for row in g]


def print_table(rows: list[Row]) -> None:
    cols = [("SOURCE", "source"), ("ON", "enabled"), ("STATUS", "status"), ("HTTP", "http"),
            ("ITEMS", "items"), ("NEWEST", "newest_age"), ("MS", "ms"), ("COND", "conditional"),
            ("ENDPOINT", "endpoint"), ("NOTE", "note")]
    table = []
    for r in rows:
        vals = {k: getattr(r, k) for _, k in cols}
        vals["enabled"] = "y" if r.enabled else "n"
        vals["endpoint"] = _short(r.endpoint)
        table.append([str(vals[k]) for _, k in cols])
    widths = [max(len(h), *(len(t[i]) for t in table)) for i, (h, _) in enumerate(cols)]
    widths[-1] = min(widths[-1], 70)
    line = "  ".join(h.ljust(w) for (h, _), w in zip(cols, widths))
    print(line)
    print("-" * len(line))
    for t in table:
        print("  ".join(v[:w].ljust(w) for v, w in zip(t, widths)))
    ok = sum(r.status == "OK" for r in rows)
    print(f"\n{ok}/{len(rows)} endpoints OK. "
          "NEWEST = age of newest item by the source's own timestamp (not our fetch latency). "
          "COND = conditional GET support on a repeat request.")
