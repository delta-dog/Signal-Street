"""`dry-run`: replay recorded responses through the full pipeline (no network)."""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
import yaml

from .config import build_app
from .models import set_clock
from .report import lag_report, print_report
from .runner import Monitor, Pipeline

CTYPES = {".json": "application/json", ".xml": "application/xml", ".html": "text/html; charset=utf-8"}


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, fixture_dir: str | Path):
        self.dir = Path(fixture_dir)
        man = yaml.safe_load((self.dir / "manifest.yaml").read_text(encoding="utf-8"))
        self.recorded_at = datetime.fromisoformat(man["recorded_at"])
        self.routes = [(re.compile(r["match"]), self.dir / r["file"]) for r in man["routes"]]
        self.hits: Counter = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for rx, path in self.routes:
            if rx.search(url):
                self.hits[path.name] += 1
                return httpx.Response(200, content=path.read_bytes(),
                                      headers={"content-type": CTYPES.get(path.suffix, "text/plain")})
        return httpx.Response(404, content=b"no fixture for " + url.encode())


async def dry_run(cfg: dict, fixtures: str, db: str = ":memory:") -> None:
    transport = FixtureTransport(fixtures)
    set_clock(lambda: transport.recorded_at)       # latencies as they were when recorded
    app = build_app(cfg, db=db, sinks=["console"], transport=transport)
    pipe = Pipeline(app.store, app.rules, app.clusterer, app.dispatcher)
    mon = Monitor(cfg, app.store, app.sources, pipe, app.calendar, app.dispatcher)
    print(f"Replaying {fixtures} as of {transport.recorded_at:%Y-%m-%d %H:%M %Z}: one poll per enabled source.\n")
    for src in app.sources:
        await mon.poll_once(src, baseline=False)
    await app.dispatcher.flush()
    for src in app.sources:
        await src.aclose()

    st = app.store
    print("\n=== Summary ===")
    pri = {r["priority"]: r["n"] for r in st.q("SELECT priority, COUNT(*) n FROM items GROUP BY priority")}
    print("items by priority:", {f"P{k}": v for k, v in sorted(pri.items())})
    print("items by role:    ", {r["role"]: r["n"] for r in st.q("SELECT role, COUNT(*) n FROM items GROUP BY role")})
    print("alerts:           ", {f"P{r['priority']}": r["n"] for r in
                                 st.q("SELECT priority, COUNT(*) n FROM alerts GROUP BY priority")})
    fu = st.q("""SELECT c.title lead, i.source, i.title FROM items i JOIN clusters c ON c.id = i.cluster_id
                 WHERE i.role = 'followup' LIMIT 5""")
    for r in fu:
        print(f"  follow-up: [{r['source']}] {r['title'][:70]}\n        of: {r['lead'][:70]}")
    print()
    print_report(lag_report(st, days=1), 1)
    set_clock(None)
