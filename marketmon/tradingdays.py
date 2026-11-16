"""Trading-day gating: hot / cool / off, with holiday list and special (Muhurat) sessions."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _t(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()


class Calendar:
    def __init__(self, cfg: dict):
        self.tz = ZoneInfo(cfg.get("timezone", "Asia/Kolkata"))
        hw = cfg.get("hot_window", {"start": "08:45", "end": "16:00"})
        self.hot = (_t(hw["start"]), _t(hw["end"]))
        self.weekend = set(cfg.get("weekend_days", [5, 6]))
        self.non_trading = cfg.get("non_trading_days", "off")
        self.holidays = {date.fromisoformat(str(d)) for d in cfg.get("holidays") or []}
        self.special = [(date.fromisoformat(str(s["date"])), _t(s["start"]), _t(s["end"]), s.get("name", ""))
                        for s in cfg.get("special_sessions") or []]

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() not in self.weekend and d not in self.holidays

    SESSION = (time(9, 15), time(15, 30))   # NSE normal market

    def session(self, now: datetime) -> dict:
        """Today's trading session (if any) and the next opening bell, all as aware datetimes in IST."""
        local = now.astimezone(self.tz)
        start, end = self.SESSION
        today_open = datetime.combine(local.date(), start, self.tz)
        today_close = datetime.combine(local.date(), end, self.tz)
        trading = self.is_trading_day(local.date())
        nxt = None
        for i in range(0, 15):
            d = local.date() + timedelta(days=i)
            if self.is_trading_day(d):
                cand = datetime.combine(d, start, self.tz)
                if cand > local:
                    nxt = cand
                    break
        return {"trading_day": trading, "open": today_open if trading else None,
                "close": today_close if trading else None, "next_open": nxt}

    def mode(self, now: datetime) -> str:
        local = now.astimezone(self.tz)
        for d, start, end, _ in self.special:
            if local.date() == d and start <= local.time() < end:
                return "hot"
        if not self.is_trading_day(local.date()):
            return self.non_trading
        start, end = self.hot
        return "hot" if start <= local.time() < end else "cool"
