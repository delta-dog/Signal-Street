"""`report`: per-source latency and reliability, from the lag/polls/events tables.

lag = first_seen_at - published_at, using the source's own timestamp. It includes our polling
interval, which is the point: it is what you actually experience end to end. Sources that
give only a date (SEBI listings) or no time at all (PIB listing) have no lag samples.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from .models import IST, now_utc
from .store import Store

BAD_CLOCK_S = -60   # samples more negative than this are bad source timestamps, not lag


def pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[k]


def lag_report(store: Store, days: int = 7) -> list[dict]:
    since_day = (now_utc().astimezone(IST).date() - timedelta(days=days - 1)).isoformat()
    since_ts = (now_utc() - timedelta(days=days)).astimezone(timezone.utc).isoformat()
    out: dict[str, dict] = {}

    def row(src):
        return out.setdefault(src, {"source": src, "lags": [], "bad_clock": 0, "items": 0, "days": set(),
                                    "polls": 0, "errors": 0, "blocked": 0, "captcha": 0, "cookie": 0})

    for r in store.q("SELECT source, lag_s FROM lag WHERE day >= ?", (since_day,)):
        d = row(r["source"])
        if r["lag_s"] < BAD_CLOCK_S:
            d["bad_clock"] += 1
        else:
            d["lags"].append(max(0.0, r["lag_s"]))
    for r in store.q("""SELECT source, COUNT(*) n FROM items WHERE first_seen_at >= ? AND baseline = 0
                        GROUP BY source""", (since_ts,)):
        row(r["source"])["items"] = r["n"]
    for r in store.q("""SELECT source, substr(at,1,10) d, COUNT(*) n,
                        SUM(status NOT IN ('OK','EMPTY','NOT_MODIFIED')) e
                        FROM polls WHERE at >= ? GROUP BY source, d""", (since_ts,)):
        d = row(r["source"])
        d["polls"] += r["n"]
        d["errors"] += r["e"] or 0
        d["days"].add(r["d"])
    for r in store.q("SELECT source, kind, detail FROM events WHERE at >= ?", (since_ts,)):
        d = row(r["source"])
        if r["kind"] == "blocked":
            d["blocked"] += 1
            d["captcha"] += "captcha" in (r["detail"] or "").lower()
        elif r["kind"] == "cookie_refresh":
            d["cookie"] += 1

    rows = []
    for src, d in sorted(out.items()):
        if src == "alerts":
            continue
        lags = sorted(d["lags"])
        ndays = max(1, len(d["days"]))
        rows.append({
            "source": src, "n_lag": len(lags), "p50": pct(lags, 50), "p95": pct(lags, 95),
            "bad_clock": d["bad_clock"], "items_per_day": d["items"] / ndays, "polls": d["polls"],
            "error_rate": d["errors"] / d["polls"] if d["polls"] else None,
            "blocked": d["blocked"], "captcha": d["captcha"], "cookie_refreshes": d["cookie"],
        })
    return rows


def _s(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.0f}s" if v < 120 else f"{v / 60:.1f}m"


def print_report(rows: list[dict], days: int) -> None:
    hdr = ["SOURCE", "LAG p50", "LAG p95", "SAMPLES", "BAD CLOCK", "ITEMS/DAY", "POLLS", "ERR%",
           "BLOCKED", "CAPTCHA", "COOKIE REFRESH"]
    table = [[r["source"], _s(r["p50"]), _s(r["p95"]), str(r["n_lag"]), str(r["bad_clock"]),
              f"{r['items_per_day']:.0f}", str(r["polls"]),
              "-" if r["error_rate"] is None else f"{100 * r['error_rate']:.1f}",
              str(r["blocked"]), str(r["captcha"]), str(r["cookie_refreshes"])] for r in rows]
    w = [max(len(h), *(len(t[i]) for t in table)) if table else len(h) for i, h in enumerate(hdr)]
    print(f"Last {days} day(s). LAG = first_seen - published (source's own timestamp, includes our poll interval).")
    print("  ".join(h.ljust(x) for h, x in zip(hdr, w)))
    print("-" * (sum(w) + 2 * len(w)))
    for t in table:
        print("  ".join(v.ljust(x) for v, x in zip(t, w)))
    if not rows:
        print("(no data yet: run `marketmon run` for a while first)")
