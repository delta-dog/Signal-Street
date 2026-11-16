"""CLI entry point: python -m marketmon <command>."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import build_app, build_sources, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="marketmon", description="Signal Street: Indian market-news monitor")
    ap.add_argument("--config", default="sources.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    cs = sub.add_parser("check-sources", help="hit every endpoint once and print a health table")
    cs.add_argument("--source", action="append", help="only these sources (repeatable)")
    cs.add_argument("--record", metavar="DIR", help="save raw response bodies here (for fixtures)")
    cs.add_argument("--enabled-only", action="store_true", help="skip sources marked enabled: false")

    run = sub.add_parser("run", help="run the live monitor")
    run.add_argument("--db", help="SQLite path (default from sources.yaml)")
    run.add_argument("--web", type=int, metavar="PORT", help="also serve the dashboard on 127.0.0.1:PORT")

    rep = sub.add_parser("report", help="per-source lag p50/p95, items/day, error and block rates")
    rep.add_argument("--db")
    rep.add_argument("--days", type=int, default=7)

    dr = sub.add_parser("dry-run", help="replay recorded fixtures through the full pipeline")
    dr.add_argument("--fixtures", default="tests/fixtures")
    dr.add_argument("--db", default=":memory:")

    web = sub.add_parser("web", help="serve the dashboard for an existing database (read-mostly)")
    web.add_argument("--db")
    web.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # Hindi/₹ in titles on Windows consoles
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)

    if args.cmd == "check-sources":
        from .health import check_sources, print_table
        sources = build_sources(cfg, only=set(args.source) if args.source else None,
                                include_disabled=not args.enabled_only)
        rows = asyncio.run(check_sources(sources, Path(args.record) if args.record else None))
        print_table(rows)
        return 0 if any(r.status == "OK" for r in rows) else 1

    if args.cmd == "run":
        from .runner import Monitor, Pipeline
        app = build_app(cfg, db=args.db)
        pipe = Pipeline(app.store, app.rules, app.clusterer, app.dispatcher,
                        fresh_minutes=cfg.get("alerts", {}).get("fresh_minutes", 10))
        pipe.warm_from_db(cfg.get("dedupe", {}).get("window_hours", 6))
        app.store.prune_ticks(days=3)
        logging.info("re-scored %d stored items with the current rules", pipe.reclassify_recent(24))
        if args.web:
            from .web import serve_in_thread
            serve_in_thread(app, args.web)
        logging.info("monitoring %d sources: %s", len(app.sources), ", ".join(s.name for s in app.sources))
        try:
            asyncio.run(Monitor(cfg, app.store, app.sources, pipe, app.calendar, app.dispatcher).run(app.markets))
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "report":
        from .report import lag_report, print_report
        from .store import Store
        print_report(lag_report(Store(args.db or cfg.get("db", "marketmon.db")), args.days), args.days)
        return 0

    if args.cmd == "dry-run":
        from .replay import dry_run
        asyncio.run(dry_run(cfg, args.fixtures, args.db))
        return 0

    if args.cmd == "web":
        from .web import serve_forever
        serve_forever(build_app(cfg, db=args.db, sinks=[]), args.port)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
