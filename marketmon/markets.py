"""Live market data for the dashboard: NSE index levels, BSE prices for Nifty 50 stocks, FX vs INR.

Sources (checked 2026-11-16 from this machine):
  - NSE  /api/allIndices        every index with last, change, OHLC, previous close (cookie session)
  - NSE  /api/marketStatus      capital-market open/closed message
  - BSE  MktRGainerLoserData    group A gainers + losers = every group-A stock with LTP and change
                                (covers all 49 BSE-listed Nifty 50 names in two requests)
  - Coinbase /v2/exchange-rates near-live fiat rates (USD base); INR crosses computed from it
  - ExchangeRate-API open feed  once-a-day reference rate, used as the baseline for FX change

All of this is for personal use. Exchange price data may not be redistributed without a licence.
NSE's per-stock quote API returns 403 and its intraday chart API returns no data, so intraday
lines are built from our own ticks (saved in SQLite so they survive a restart).
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import httpx

from .http import BROWSER_HEADERS, Fetcher, HostLimiter, backoff_delay, make_client
from .models import IST, now_utc
from .sources.nse import HOME as NSE_HOME, needs_cookie_refresh

log = logging.getLogger("marketmon.markets")

NSE_API = {"Accept": "application/json, text/plain, */*",
           "Referer": "https://www.nseindia.com/market-data/live-equity-market",
           "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
BSE_API = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.bseindia.com",
           "Referer": "https://www.bseindia.com/", "Sec-Fetch-Site": "same-site",
           "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
BSE_MOVERS = "https://api.bseindia.com/BseIndiaAPI/api/MktRGainerLoserData/w"
COINBASE = "https://api.coinbase.com/v2/exchange-rates?currency=USD"
FX_REF = "https://open.er-api.com/v6/latest/USD"
FX_NAMES = {"USD": "US dollar", "EUR": "Euro", "GBP": "British pound", "JPY": "Japanese yen (100)",
            "AED": "UAE dirham", "SGD": "Singapore dollar", "CNY": "Chinese yuan", "CHF": "Swiss franc"}


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_indices(payload: dict, wanted: list[str], at: str) -> list[dict]:
    by = {r.get("index"): r for r in payload.get("data", [])}
    out = []
    for name in wanted:
        r = by.get(name)
        if not r:
            continue
        out.append({"symbol": name, "name": name, "last": _f(r.get("last")), "change": _f(r.get("variation")), "pchange": _f(r.get("percentChange")),
                    "open": _f(r.get("open")), "high": _f(r.get("high")), "low": _f(r.get("low")),
                    "prev": _f(r.get("previousClose")), "advances": r.get("advances"), "declines": r.get("declines"),
                    "at": at, "source": "NSE"})
    return out


def parse_bse_movers(tables: list[list[dict]], constituents: list[dict], at: str) -> list[dict]:
    rows = {str(r.get("scrip_cd")): r for t in tables for r in t}
    out = []
    for c in constituents:
        r = rows.get(str(c.get("bse_code")))
        if not r:
            continue
        out.append({"symbol": c["symbol"], "name": c["name"], "heavyweight": bool(c.get("heavyweight")),
                    "last": _f(r.get("ltradert")), "change": _f(r.get("change_val")), "pchange": _f(r.get("change_percent")),
                    "open": _f(r.get("openrate")), "high": _f(r.get("highrate")), "low": _f(r.get("lowrate")),
                    "prev": _f(r.get("prevdayclose")), "volume": r.get("trd_vol"),
                    "at": at, "trade_time_ist": r.get("dt_tm"), "source": "BSE"})
    return out


def parse_fx(live: dict, ref: dict | None, codes: list[str], at: str) -> list[dict]:
    """INR per unit of each currency (per 100 for JPY). Change is vs the daily reference rate."""
    rates = {k: _f(v) for k, v in live.get("data", {}).get("rates", {}).items()}
    ref_rates = (ref or {}).get("rates", {})
    inr = rates.get("INR")
    out = []
    for code in codes:
        per_usd = 1.0 if code == "USD" else rates.get(code)
        if not inr or not per_usd:
            continue
        mult = 100 if code == "JPY" else 1
        last = inr / per_usd * mult
        r_inr, r_code = _f(ref_rates.get("INR")), (1.0 if code == "USD" else _f(ref_rates.get(code)))
        prev = r_inr / r_code * mult if r_inr and r_code else None
        out.append({"symbol": f"{code}/INR", "name": FX_NAMES.get(code, code), "last": last, "prev": prev,
                    "change": last - prev if prev else None, "pchange": (last / prev - 1) * 100 if prev else None,
                    "at": at, "source": "Coinbase", "ref_source": "ExchangeRate-API daily"})
    return out


class MarketData:
    def __init__(self, cfg: dict, store, calendar, constituents: list[dict], limiter: HostLimiter,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg.get("markets", {})
        self.store, self.calendar, self.constituents = store, calendar, constituents
        self.nse = Fetcher(make_client(browser=True, transport=transport), limiter)
        self.bse = Fetcher(make_client(browser=True, transport=transport, timeout=30), limiter)
        self.fx = Fetcher(make_client(user_agent=cfg.get("user_agent"), transport=transport), limiter)
        self.nse_warm = False
        self.fx_ref: dict | None = None
        self.fx_ref_at: datetime | None = None
        self.snap: dict = {"indices": [], "stocks": [], "fx": [], "status": None, "errors": {}, "updated": {}}
        self.loop = None    # the monitor's event loop; the web thread schedules chart fetches on it

    # ------------------------------------------------------------------ fetchers
    async def _nse_json(self, url: str):
        if not self.nse_warm:
            w = await self.nse.get(NSE_HOME, expect="html")
            self.nse_warm = w.ok
            if not w.ok:
                raise RuntimeError(w.blocked or w.error or "NSE warm-up failed")
        f = await self.nse.get(url, expect="json", headers=NSE_API)
        if needs_cookie_refresh(f):
            self.nse.client.cookies.clear()
            await self.nse.get(NSE_HOME, expect="html")
            f = await self.nse.get(url, expect="json", headers=NSE_API)
        if not f.ok:
            self.nse_warm = False
            raise RuntimeError(f.blocked or f.error or f"HTTP {f.response.status_code}")
        return f.response.json()

    async def poll_indices(self) -> None:
        at = now_utc().isoformat()
        data = await self._nse_json("https://www.nseindia.com/api/allIndices")
        self.snap["indices"] = parse_indices(data, self.cfg.get("indices", ["NIFTY 50"]), at)
        try:
            st = await self._nse_json("https://www.nseindia.com/api/marketStatus")
            cm = next((m for m in st.get("marketState", []) if m.get("market") == "Capital Market"), None)
            if cm:
                self.snap["status"] = {"status": cm.get("marketStatus"), "message": cm.get("marketStatusMessage"),
                                       "trade_date": cm.get("tradeDate")}
        except Exception as e:          # status is nice-to-have
            log.debug("marketStatus failed: %s", e)
        self._tick(self.snap["indices"], at)

    async def poll_stocks(self) -> None:
        at = now_utc().isoformat()
        tables = []
        for gl in ("gainer", "loser"):
            f = await self.bse.get(BSE_MOVERS, expect="json", headers=BSE_API,
                                   params={"GLtype": gl, "IndxGrp": "group", "IndxGrpval": "A", "orderby": "all"})
            if not f.ok:
                raise RuntimeError(f.blocked or f.error or f"HTTP {f.response.status_code}")
            tables.append(f.response.json().get("Table") or [])
        self.snap["stocks"] = parse_bse_movers(tables, self.constituents, at)
        self._tick(self.snap["stocks"], at)

    async def poll_fx(self) -> None:
        at = now_utc()
        if self.fx_ref is None or self.fx_ref_at is None or at - self.fx_ref_at > timedelta(hours=1):
            r = await self.fx.get(FX_REF, expect="json")
            if r.ok:
                self.fx_ref, self.fx_ref_at = r.response.json(), at
        f = await self.fx.get(COINBASE, expect="json")
        if not f.ok:
            raise RuntimeError(f.blocked or f.error or f"HTTP {f.response.status_code}")
        self.snap["fx"] = parse_fx(f.response.json(), self.fx_ref, self.cfg.get("fx", ["USD", "EUR", "GBP", "JPY"]),
                                   at.isoformat())
        self._tick(self.snap["fx"], at.isoformat())

    def _tick(self, quotes: list[dict], at: str) -> None:
        self.store.add_ticks([(q["symbol"], at, q["last"]) for q in quotes if q.get("last") is not None])

    # ------------------------------------------------------------------ loop
    async def worker(self, name: str, fn) -> None:
        cad = self.cfg.get("cadence", {}).get(name, {"hot": 30, "cool": 300})
        failures = 0
        self.loop = asyncio.get_running_loop()
        await asyncio.sleep(random.uniform(0, 2))
        while True:
            mode = self.calendar.mode(now_utc())
            if mode == "off" and name != "fx":
                await asyncio.sleep(300)
                continue
            try:
                await fn()
                failures = 0
                self.snap["errors"].pop(name, None)
                self.snap["updated"][name] = now_utc().isoformat()
            except Exception as e:
                failures += 1
                self.snap["errors"][name] = f"{type(e).__name__}: {e}"[:200]
                self.store.add_event(f"markets:{name}", "error", str(e))
            interval = cad.get(mode, cad.get("cool", 300)) if mode != "off" else cad.get("cool", 300)
            if failures:
                interval = max(interval, backoff_delay(failures, base=max(interval, 30), cap=1800))
            await asyncio.sleep(interval * random.uniform(0.9, 1.1))

    def tasks(self) -> list:
        if not self.cfg.get("enabled", True):
            return []
        return [self.worker("indices", self.poll_indices), self.worker("stocks", self.poll_stocks),
                self.worker("fx", self.poll_fx)]

    async def aclose(self) -> None:
        for f in (self.nse, self.bse, self.fx):
            await f.client.aclose()

    # ------------------------------------------------------------------ API view
    def view(self, points: int = 90) -> dict:
        today = now_utc().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        syms = [q["symbol"] for k in ("indices", "stocks", "fx") for q in self.snap[k]]
        series = self.store.ticks_since(syms, today.isoformat())
        def thin(xs, n):
            if len(xs) <= n:
                return xs
            step = len(xs) / n
            return [[xs[int(i * step)][0], round(xs[int(i * step)][1], 4)] for i in range(n)] + [xs[-1]]
        # Stock rows only draw a small sparkline, so 40 points is plenty (keeps the payload light on phones).
        size = {"indices": points, "fx": points, "stocks": 40}
        out = {k: [dict(q, series=thin(series.get(q["symbol"], []), size[k])) for q in self.snap[k]]
               for k in ("indices", "stocks", "fx")}
        out.update(status=self.snap["status"], errors=self.snap["errors"], updated=self.snap["updated"],
                   constituents=[{"symbol": c["symbol"], "name": c["name"], "heavyweight": bool(c.get("heavyweight"))}
                                 for c in self.constituents])
        return out
