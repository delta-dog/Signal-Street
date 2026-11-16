"""The live loop: one async worker per source -> pipeline (dedupe, rules, cluster) -> alerts."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta

from .alerts import Alert, Dispatcher
from .dedupe import Clusterer
from .http import backoff_delay
from .models import Item, now_utc
from .rules import Rules
from .sources import PollResult, Source, status_of
from .store import Store
from .tradingdays import Calendar

log = logging.getLogger("marketmon")


class Pipeline:
    """Hard-key dedupe -> rules -> clustering -> store -> lag sample -> alert."""

    STALE = timedelta(hours=24)       # published this long before we first saw it -> not news
    STALE_DAY = timedelta(hours=48)   # same, for date-only sources (SEBI listings)

    def __init__(self, store: Store, rules: Rules, clusterer: Clusterer, dispatcher: Dispatcher,
                 fresh_minutes: float = 10):
        self.store, self.rules, self.clusterer, self.dispatcher = store, rules, clusterer, dispatcher
        self.fresh = timedelta(minutes=fresh_minutes)

    def warm_from_db(self, hours: float) -> None:
        """Reload the clustering window so a restart doesn't re-alert on known stories."""
        from datetime import datetime
        for r in self.store.recent_items(hours):
            if r["cluster_id"] is not None:
                self.clusterer.add(r["cluster_id"], source=r["source"], rank=r["source_rank"],
                                   title=r["title"] or "", symbol=r["matched_symbol"], label=r["label"],
                                   at=datetime.fromisoformat(r["first_seen_at"]), sh=int(r["simhash"], 16))

    def reclassify_recent(self, hours: float = 24) -> int:
        """Re-score stored items with the current rules (after a rules change). Alerts are not re-sent."""
        from datetime import datetime
        n = 0
        for r in self.store.recent_items(hours):
            it = Item(source=r["source"], source_rank=r["source_rank"], id=r["id"], title=r["title"] or "",
                      url=r["url"] or "", published_at=datetime.fromisoformat(r["published_at"]) if r["published_at"] else None,
                      first_seen_at=datetime.fromisoformat(r["first_seen_at"]), published_precision=r["precision"] or "none",
                      symbol=r["symbol"], isin=r["isin"], entity=r["entity"], category=r["category"])
            c = self.rules.classify(it)
            self.store.x("""UPDATE items SET matched_symbol=?, heavyweight=?, label=?, priority=?, drop_reason=?, impact=?
                            WHERE source=? AND id=?""",
                         (c.symbol, int(c.heavyweight), c.label, c.priority, c.drop_reason, c.impact, r["source"], r["id"]))
            n += 1
        return n

    async def ingest(self, items: list[Item], *, baseline: bool) -> int:
        """baseline=True for a source's first poll after start: store everything, but record no
        lag (we weren't watching) and only alert on items published in the last few minutes."""
        new = 0
        for it in items:
            if self.store.known(it.source, it.id):
                continue
            new += 1
            cls = self.rules.classify(it)
            cid, role, sh = self.clusterer.assign(source=it.source, rank=it.source_rank, title=it.title,
                                                  symbol=cls.symbol, label=cls.label, at=it.first_seen_at)
            if cid is None:
                cid = self.store.new_cluster(it, cls)
            else:
                self.store.join_cluster(cid, role)
            self.clusterer.add(cid, source=it.source, rank=it.source_rank, title=it.title,
                               symbol=cls.symbol, label=cls.label, at=it.first_seen_at, sh=sh)
            self.store.add_item(it, cls=cls, cluster_id=cid, role=role, simhash=sh, baseline=baseline)
            age = it.first_seen_at - it.published_at if it.published_at else None
            # Old stories that resurface (Google News re-ranks, feeds re-order) are not news and
            # their "lag" would be meaningless.
            stale = age is not None and age > (self.STALE_DAY if it.published_precision == "day" else self.STALE)
            if not baseline and not stale and it.lag_seconds is not None:
                self.store.add_lag(it)
            fresh = not stale and (not baseline or (age is not None and age <= self.fresh))
            if role == "lead" and cls.priority <= 1 and fresh:
                await self.dispatcher.submit(Alert(cls.priority, it.source, it.title, it.url,
                                                   it.lag_seconds, cid))
        return new


class Monitor:
    def __init__(self, cfg: dict, store: Store, sources: list[Source], pipeline: Pipeline,
                 calendar: Calendar, dispatcher: Dispatcher):
        self.cfg, self.store, self.sources = cfg, store, sources
        self.pipeline, self.calendar, self.dispatcher = pipeline, calendar, dispatcher

    def record(self, src: Source, results: list[PollResult], n_new: dict[str, int]) -> None:
        for r in results:
            f = r.fetched
            resp = f.response if f else None
            self.store.add_poll(src.name, r.endpoint, status_of(r), resp.status_code if resp is not None else None,
                                f.elapsed_ms if f else None, len(r.items), n_new.get(r.endpoint, 0),
                                (f.error or f.blocked or r.note) if f else r.note)
            for ev in r.events:
                self.store.add_event(src.name, ev, (f.blocked or "") if f else "")

    async def poll_once(self, src: Source, *, baseline: bool) -> tuple[bool, bool]:
        """Returns (any endpoint OK, any endpoint blocked)."""
        try:
            results = await src.poll()
        except Exception as e:
            log.exception("%s poll crashed", src.name)
            self.store.add_event(src.name, "error", f"{type(e).__name__}: {e}")
            return False, False
        n_new = {r.endpoint: await self.pipeline.ingest(r.items, baseline=baseline) for r in results}
        self.record(src, results, n_new)
        ok = any(status_of(r) in ("OK", "EMPTY", "NOT_MODIFIED") for r in results)
        blocked = any("blocked" in r.events for r in results)
        return ok, blocked

    async def worker(self, src: Source) -> None:
        cadence = src.cfg.get("cadence", {"hot": 60, "cool": 300})
        baseline, failures = True, 0
        await asyncio.sleep(random.uniform(0, 2))     # don't start every worker in the same tick
        while True:
            mode = self.calendar.mode(now_utc())
            if mode == "off":
                await asyncio.sleep(300)
                continue
            src.burst = "burst" in cadence and self.store.get_flag(f"burst:{src.name}")
            ok, blocked = await self.poll_once(src, baseline=baseline)
            if ok:
                baseline = False
            failures = 0 if ok and not blocked else failures + 1
            interval = cadence["burst"] if src.burst else cadence.get(mode, cadence["cool"])
            if failures:
                # Exponential backoff with jitter; blocks back off from at least a minute.
                interval = max(interval, backoff_delay(failures, base=max(interval, 60 if blocked else interval),
                                                       cap=1800))
                if blocked:
                    log.warning("%s blocked; backing off %.0fs", src.name, interval)
            await self._sleep(src, interval * random.uniform(0.9, 1.1), "burst" in cadence)

    async def _sleep(self, src: Source, seconds: float, burst_capable: bool) -> None:
        """Sleep, but wake early if burst mode is toggled from the dashboard."""
        if not burst_capable:
            await asyncio.sleep(seconds)
            return
        end = asyncio.get_running_loop().time() + seconds
        while (left := end - asyncio.get_running_loop().time()) > 0:
            await asyncio.sleep(min(1.0, left))
            if self.store.get_flag(f"burst:{src.name}") != src.burst:
                return

    async def run(self, markets=None) -> None:
        tasks = [asyncio.create_task(self.worker(s), name=s.name) for s in self.sources]
        tasks.append(asyncio.create_task(self.dispatcher.digest_loop(), name="digest"))
        if markets is not None:
            tasks += [asyncio.create_task(t) for t in markets.tasks()]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self.dispatcher.flush()
            await asyncio.gather(*(s.aclose() for s in self.sources), return_exceptions=True)
            if markets is not None:
                await markets.aclose()
