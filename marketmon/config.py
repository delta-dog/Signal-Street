from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from .alerts import Dispatcher, build_sinks
from .dedupe import Clusterer
from .http import HostLimiter
from .charts import Charts
from .markets import MarketData
from .rules import Index, Rules
from .sources import SOURCE_TYPES, Source
from .store import Store
from .tradingdays import Calendar


def _yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(path: str | Path) -> dict:
    cfg = _yaml(path)
    base = Path(path).resolve().parent
    files = cfg.get("files", {})
    cfg["_index"] = _yaml(base / files.get("index", "config/index.yaml"))
    cfg["_rules"] = _yaml(base / files.get("rules", "config/rules.yaml"))
    cfg["_calendar"] = _yaml(base / files.get("calendar", "config/calendar.yaml"))
    disabled = {s.strip() for s in os.environ.get("MARKETMON_DISABLE", "").split(",") if s.strip()}
    for name, scfg in cfg["sources"].items():
        if name in disabled:
            scfg["enabled"] = False
    return cfg


def make_limiter(cfg: dict) -> HostLimiter:
    intervals = dict(cfg.get("host_min_interval", {}))
    return HostLimiter(intervals, default=intervals.pop("default", 1.0))


def build_sources(cfg: dict, *, only: set[str] | None = None, include_disabled: bool = False,
                  transport: httpx.AsyncBaseTransport | None = None,
                  limiter: HostLimiter | None = None) -> list[Source]:
    limiter = limiter or make_limiter(cfg)
    out = []
    for name, scfg in cfg["sources"].items():
        if only and name not in only:
            continue
        if not include_disabled and not scfg.get("enabled", True):
            continue
        cls = SOURCE_TYPES[scfg["type"]]
        out.append(cls(name, scfg, limiter, cfg["user_agent"], transport=transport))
    return out


@dataclass
class App:
    cfg: dict
    store: Store
    sources: list[Source]
    rules: Rules
    clusterer: Clusterer
    calendar: Calendar
    dispatcher: Dispatcher
    markets: MarketData
    charts: Charts


def build_app(cfg: dict, *, db: str | None = None, sinks: list[str] | None = None,
              transport: httpx.AsyncBaseTransport | None = None) -> App:
    store = Store(db or cfg.get("db", "marketmon.db"))
    a = cfg.get("alerts", {})
    d = cfg.get("dedupe", {})
    limiter = make_limiter(cfg)          # one limiter: news and prices share the per-host caps
    calendar = Calendar(cfg["_calendar"])
    constituents = cfg["_index"].get("constituents", [])
    markets = MarketData(cfg, store, calendar, constituents, limiter, transport=transport)
    return App(
        cfg=cfg, store=store,
        sources=build_sources(cfg, transport=transport, limiter=limiter),
        rules=Rules(cfg["_rules"], Index(constituents)),
        clusterer=Clusterer(d.get("window_hours", 6), d.get("max_hamming", 3), d.get("min_jaccard", 0.6),
                            d.get("cross_exchange_minutes", 30)),
        calendar=calendar,
        dispatcher=Dispatcher(build_sinks(sinks if sinks is not None else a.get("sinks", ["console"])),
                              store, digest_seconds=60 * a.get("digest_minutes", 5)),
        markets=markets, charts=Charts(markets),
    )
