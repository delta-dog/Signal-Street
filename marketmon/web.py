"""Local dashboard: GET / (page), GET /api/state (JSON), POST /api/burst. Binds to 127.0.0.1."""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import threading
from urllib.parse import parse_qs, urlsplit
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .logos import Logos
from .models import IST, now_utc
from .report import lag_report

PAGE = Path(__file__).with_name("static") / "dashboard.html"
# Brand assets served as-is (a fixed list: nothing else in static/ is reachable).
ASSETS = {"/logo.svg": "image/svg+xml", "/logo-tonal.svg": "image/svg+xml", "/logo-mono.svg": "image/svg+xml",
          "/logo-lockup.svg": "image/svg+xml", "/logo-circle.svg": "image/svg+xml",
          "/logo-circle-tonal.svg": "image/svg+xml", "/manifest.webmanifest": "application/manifest+json"}
log = logging.getLogger("marketmon.web")
# ISO timestamps carry microseconds; Safari and older mobile browsers only parse up to milliseconds.
_MICROS = re.compile(rb"(T\d\d:\d\d:\d\d\.\d{3})\d+")


def state(app) -> dict:
    st, cfg = app.store, app.cfg
    now = now_utc().astimezone(timezone.utc)
    day_start = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    last = st.q("""SELECT p.* FROM polls p JOIN
                   (SELECT source, endpoint, MAX(at) at FROM polls GROUP BY source, endpoint) m
                   USING (source, endpoint, at)""")
    fails = {r["source"]: r["n"] for r in st.q(
        """SELECT source, COUNT(*) n FROM polls WHERE at >= ? AND status NOT IN ('OK','EMPTY','NOT_MODIFIED')
           GROUP BY source""", ((now - timedelta(hours=1)).isoformat(),))}
    by_src: dict[str, list] = {}
    for r in last:
        by_src.setdefault(r["source"], []).append(dict(r))
    sources = []
    for name, s in cfg["sources"].items():
        cad = s.get("cadence", {})
        sources.append({
            "name": name, "tier": s.get("tier"), "enabled": s.get("enabled", True), "cadence": cad,
            "burst_capable": "burst" in cad, "burst_on": st.get_flag(f"burst:{name}"),
            "failures_1h": fails.get(name, 0),
            "endpoints": sorted(by_src.get(name, []), key=lambda e: e["endpoint"]),
        })
    feed = [dict(r) for r in st.q(
        """SELECT i.source, i.id, i.title, i.url, i.published_at, i.precision, i.first_seen_at,
                  i.matched_symbol, i.heavyweight, i.label, i.priority, i.drop_reason, i.role,
                  i.baseline, i.cluster_id, i.impact, c.followups, c.members,
                  CASE WHEN i.source = 'google_news' THEN json_extract(i.raw, '$.source_url') END AS outlet
           FROM items i LEFT JOIN clusters c ON c.id = i.cluster_id
           WHERE MIN(COALESCE(i.published_at, i.first_seen_at), i.first_seen_at) >= ?
           -- A publish time later than when we first saw the item is wrong (some feeds future-date
           -- stories); sort those by first-seen so they can't sit at the top of the feed.
           ORDER BY MIN(COALESCE(i.published_at, i.first_seen_at), i.first_seen_at) DESC LIMIT 500""",
        ((now - timedelta(hours=24)).astimezone(timezone.utc).isoformat(),))]
    alerts = [dict(r) for r in st.q("SELECT * FROM alerts ORDER BY id DESC LIMIT 60")]
    counts = {f"P{r['priority']}": r["n"] for r in st.q(
        "SELECT priority, COUNT(*) n FROM items WHERE first_seen_at >= ? GROUP BY priority",
        (day_start.astimezone(now.tzinfo).isoformat(),))}
    events = [dict(r) for r in st.q("SELECT * FROM events ORDER BY at DESC LIMIT 20")]
    return {
        "now": now.isoformat(), "mode": app.calendar.mode(now),
        "ui": PAGE.stat().st_mtime_ns,       # open dashboards reload themselves when this changes
        "trading_day": app.calendar.is_trading_day(now.astimezone(IST).date()),
        "counts_today": counts, "sources": sources, "feed": feed, "alerts": alerts,
        "lag": lag_report(st, days=1), "events": events, "activity": activity(st, now),
        "session": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in app.calendar.session(now).items()},
    }


def activity(st, now, hours: int = 12, bucket_s: int = 1800) -> dict:
    """Items published per 30 minutes over the last 12 hours (noise excluded), overall and per source."""
    start = now - timedelta(hours=hours)
    n = hours * 3600 // bucket_s
    counts, important, by_source = [0] * n, [0] * n, {}
    for r in st.q("""SELECT source, COALESCE(published_at, first_seen_at) t, priority FROM items
                     WHERE COALESCE(published_at, first_seen_at) >= ? AND priority <= 2""", (start.isoformat(),)):
        try:
            i = int((datetime.fromisoformat(r["t"]) - start).total_seconds() // bucket_s)
        except ValueError:
            continue
        if 0 <= i < n:
            counts[i] += 1
            important[i] += r["priority"] <= 1
            by_source.setdefault(r["source"], [0] * n)[i] += 1
    return {"start": start.isoformat(), "bucket_s": bucket_s, "counts": counts, "important": important,
            "by_source": by_source}


def make_handler(app):
    logos = Logos(Path(app.cfg.get("db", "marketmon.db")).resolve().parent / "logo_cache")

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            if ctype == "application/json":
                body = _MICROS.sub(rb"\1", body)
            # gzip large responses (JSON shrinks ~10x): matters on phone data plans.
            gz = len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
            if gz:
                body = gzip.compress(body, compresslevel=5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path.split("?")[0] in ASSETS:
                path = self.path.split("?")[0]
                self._send(200, PAGE.with_name(path.lstrip("/")).read_bytes(), ASSETS[path], cache="no-cache")   # revalidate: a new logo shows up at once
            elif self.path.startswith("/api/chart"):
                self._chart()
            elif self.path.startswith("/api/markets"):
                self._send(200, json.dumps(app.markets.view(), default=str).encode(), "application/json")
            elif self.path.startswith("/api/logo"):
                self._logo()
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(state(app), default=str).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def _logo(self):
            domain = parse_qs(urlsplit(self.path).query).get("d", [""])[0]
            try:
                found = logos.get(domain)
            except Exception as e:                   # never let an icon lookup break the page
                log.warning("logo %s: %s", domain, e)
                found = None
            if not found:
                return self._send(404, b"", "text/plain", cache="public, max-age=3600")
            self._send(200, found[0], found[1], cache="public, max-age=604800")

        def _chart(self):
            qs = parse_qs(urlsplit(self.path).query)
            symbol, rng = qs.get("symbol", [""])[0], qs.get("range", ["1D"])[0]
            loop = app.markets.loop
            if loop is None:
                return self._send(503, b'{"error":"charts need the running monitor (marketmon run --web)"}', "application/json")
            try:
                data = asyncio.run_coroutine_threadsafe(app.charts.get(symbol, rng), loop).result(timeout=40)
                self._send(200, json.dumps(data).encode(), "application/json")
            except KeyError as e:
                self._send(404, json.dumps({"error": str(e).strip("'\"")}).encode(), "application/json")
            except Exception as e:
                self._send(502, json.dumps({"error": f"{type(e).__name__}: {e}"[:200]}).encode(), "application/json")

        def do_POST(self):
            if self.path != "/api/burst":
                return self._send(404, b"not found", "text/plain")
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                name = body["source"]
                if "burst" not in app.cfg["sources"].get(name, {}).get("cadence", {}):
                    return self._send(400, b'{"error":"source has no burst cadence"}', "application/json")
                app.store.set_flag(f"burst:{name}", bool(body.get("on")))
                app.store.add_event(name, "burst_on" if body.get("on") else "burst_off", "dashboard")
                self._send(200, b'{"ok":true}', "application/json")
            except (KeyError, ValueError) as e:
                self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")

        def log_message(self, *a):   # keep the console for alerts
            pass

    return Handler


def serve_in_thread(app, port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    log.info("dashboard on http://127.0.0.1:%d", port)
    return srv


def serve_forever(app, port: int) -> None:
    log.info("dashboard on http://127.0.0.1:%d", port)
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(app)).serve_forever()
