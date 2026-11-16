"""The one normalised item schema every source produces."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Lower number = more authoritative. Used to pick a cluster representative and for display.
TIER_RANK = {"exchange": 1, "regulator": 2, "wire": 3, "media": 4}


_clock = None   # dry-run / tests can set this to a callable returning a fixed datetime


def set_clock(fn) -> None:
    global _clock
    _clock = fn


def now_utc() -> datetime:
    return _clock() if _clock else datetime.now(timezone.utc)


@dataclass
class Item:
    source: str                 # config key, e.g. "nse", "rbi"
    source_rank: int            # see TIER_RANK
    id: str                     # stable per-source id (exchange id, or canonical URL)
    title: str
    url: str
    published_at: datetime | None   # as stated by the source (tz-aware), None if not given
    first_seen_at: datetime         # when we first fetched it (UTC)
    published_precision: str = "s"  # "s" = has a time of day, "day" = date only, "none"
    symbol: str | None = None
    isin: str | None = None
    entity: str | None = None       # company / ministry / issuer name
    category: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def lag_seconds(self) -> float | None:
        """first_seen - published. Only meaningful when the source gives a time of day."""
        if self.published_at is None or self.published_precision != "s":
            return None
        return (self.first_seen_at - self.published_at).total_seconds()
