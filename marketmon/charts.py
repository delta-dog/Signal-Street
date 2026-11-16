"""Chart data for any instrument on the dashboard (checked 2026-11-16 from this machine).

  indices  NSE  /api/NextApi/apiClient/indexTrackerApi?functionName=getIndexChart&index=..&flag=1D|1M|3M|6M|1Y
           1D = minute points from 09:00 plus previous close. Timestamps are IST wall-clock time
           encoded as if UTC, so we shift them by -5:30. (flag=5D returns 404.)
  stocks   BSE  /BseIndiaAPI/api/StockReachGraph/w?scripcode=..&flag=0|5D|1M|6M|12M  (0 = today, intraday)
           'Data' is a JSON string of {dttm: 'Mon Nov 16 2026 09:15:59' (IST), vale1: price}.
  fx       1D from our own ticks (Coinbase); longer ranges from Frankfurter (ECB daily reference rates).

Personal use only: exchange data may not be redistributed without a licence.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from .markets import BSE_API
from .models import IST, now_utc

NSE_CHART = "https://www.nseindia.com/api/NextApi/apiClient/indexTrackerApi"
BSE_CHART = "https://api.bseindia.com/BseIndiaAPI/api/StockReachGraph/w"
FRANKFURTER = "https://api.frankfurter.dev/v1/{start}..?base=USD&symbols={symbols}"

RANGES = {
    "index": ["1D", "1M", "3M", "6M", "1Y"],
    "stock": ["1D", "5D", "1M", "6M", "1Y"],
    "fx": ["1D", "1M", "3M", "6M", "1Y"],
}
BSE_FLAG = {"1D": "0", "5D": "5D", "1M": "1M", "6M": "6M", "1Y": "12M"}
DAYS = {"1M": 31, "3M": 92, "6M": 183, "1Y": 366}
IST_SHIFT = timedelta(hours=5, minutes=30)


def nse_points(payload: dict) -> tuple[list[list], float | None]:
    d = (payload or {}).get("data") or {}
    rows = d.get("grapthData") or []
    # Flags: 'PO' = 09:00-09:08 pre-open auction, 'NM' = normal market, 'IC' = indicative close (the projected
    # closing-auction price NSE publishes from 15:20; it can sit far from the traded index). Plot traded points only.
    if any(len(r) > 2 and r[2] == "NM" for r in rows):
        rows = [r for r in rows if len(r) < 3 or r[2] == "NM"]
    pts = []
    for row in rows:
        ts = datetime.fromtimestamp(row[0] / 1000, timezone.utc) - IST_SHIFT   # IST clock -> real UTC
        pts.append([ts.isoformat(), float(row[1])])
    close = d.get("closePrice")
    return pts, (float(close) if close else None)


def bse_points(payload) -> tuple[list[list], float | None]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows = payload.get("Data") or "[]"
    rows = json.loads(rows) if isinstance(rows, str) else rows
    pts = []
    for r in rows:
        try:
            ts = datetime.strptime(r["dttm"], "%a %b %d %Y %H:%M:%S").replace(tzinfo=IST)
            pts.append([ts.astimezone(timezone.utc).isoformat(), float(r["vale1"])])
        except (KeyError, ValueError, TypeError):
            continue
    prev = payload.get("PrevClose")
    prev = float(prev) if prev not in (None, "", "0") else None
    return pts, prev


def fx_points(payload: dict, code: str) -> list[list]:
    pts = []
    for day, rates in sorted((payload or {}).get("rates", {}).items()):
        inr, other = rates.get("INR"), (1.0 if code == "USD" else rates.get(code))
        if inr and other:
            ts = datetime.fromisoformat(day).replace(hour=16, tzinfo=timezone.utc)   # ECB fixes ~16:00 CET
            pts.append([ts.isoformat(), inr / other * (100 if code == "JPY" else 1)])
    return pts


class Charts:
    def __init__(self, markets):
        self.md = markets
        self.cache: dict[tuple[str, str], tuple[float, dict]] = {}

    def kind_of(self, symbol: str) -> str | None:
        if symbol.endswith("/INR"):
            return "fx"
        if any(q["symbol"] == symbol for q in self.md.snap["indices"]) or symbol in self.md.cfg.get("indices", []):
            return "index"
        if any(c["symbol"] == symbol for c in self.md.constituents):
            return "stock"
        return None

    async def get(self, symbol: str, rng: str) -> dict:
        kind = self.kind_of(symbol)
        if kind is None:
            raise KeyError(f"unknown symbol {symbol!r}")
        if rng not in RANGES[kind]:
            raise KeyError(f"range {rng!r} not available for {kind}")
        key = (symbol, rng)
        hit = self.cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        data = await getattr(self, f"_{kind}")(symbol, rng)
        data.update(symbol=symbol, kind=kind, range=rng, ranges=RANGES[kind], fetched_at=now_utc().isoformat())
        self.cache[key] = (time.monotonic() + (60 if rng == "1D" else 3600), data)
        return data

    async def _index(self, symbol: str, rng: str) -> dict:
        url = str(httpx.URL(NSE_CHART, params={"functionName": "getIndexChart", "index": symbol, "flag": rng}))
        pts, close = nse_points(await self.md._nse_json(url))
        return {"points": pts, "prev_close": close if rng == "1D" else None, "source": "NSE"}

    async def _stock(self, symbol: str, rng: str) -> dict:
        c = next(c for c in self.md.constituents if c["symbol"] == symbol)
        if not c.get("bse_code"):
            raise KeyError(f"{symbol} is not listed on BSE, so no chart source")
        f = await self.md.bse.get(BSE_CHART, expect="json", headers=BSE_API,
                                  params={"scripcode": c["bse_code"], "flag": BSE_FLAG[rng], "fromdate": "", "todate": "", "seriesid": ""})
        if not f.ok:
            raise RuntimeError(f.blocked or f.error or f"HTTP {f.response.status_code}")
        pts, prev = bse_points(f.response.json())
        return {"points": pts, "prev_close": prev if rng == "1D" else None, "source": "BSE"}

    async def _fx(self, symbol: str, rng: str) -> dict:
        code = symbol.split("/")[0]
        if rng == "1D":
            since = now_utc().astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            pts = self.md.store.ticks_since([symbol], since.isoformat()).get(symbol, [])
            q = next((q for q in self.md.snap["fx"] if q["symbol"] == symbol), {})
            return {"points": pts, "prev_close": q.get("prev"), "source": "Coinbase (our ticks today)"}
        start = (now_utc() - timedelta(days=DAYS[rng])).date().isoformat()
        syms = "INR" if code == "USD" else f"INR,{code}"
        f = await self.md.fx.get(FRANKFURTER.format(start=start, symbols=syms), expect="json")
        if not f.ok:
            raise RuntimeError(f.blocked or f.error or f"HTTP {f.response.status_code}")
        return {"points": fx_points(f.response.json(), code), "prev_close": None, "source": "ECB reference rates (Frankfurter)"}
