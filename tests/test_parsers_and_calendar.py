import asyncio
import json
from datetime import datetime

import yaml
from conftest import FIX, ROOT, T0, fixture_bytes

from marketmon.models import IST
from marketmon.sources.bse import parse_announcements as parse_bse
from marketmon.sources.nse import parse_announcements as parse_nse
from marketmon.sources.pib import parse_all_releases, parse_posted_on
from marketmon.sources.rss import parse_feed
from marketmon.sources.sebi import parse_listing
from marketmon.tradingdays import Calendar


def test_nse_fields():
    items = parse_nse(json.loads(fixture_bytes("nse_announcements.json")), 1, T0)
    it = items[0]
    assert it.id.isdigit() and it.symbol and it.isin.startswith("IN")
    assert it.published_at.tzinfo is not None and it.published_precision == "s"


def test_bse_fields():
    items = parse_bse(json.loads(fixture_bytes("bse_company_update.json")), 1, T0)
    assert len(items) == 50 and all(i.published_at for i in items)
    assert items[0].url.endswith(".pdf") and items[0].symbol.isdigit()
    assert parse_bse(json.loads(fixture_bytes("bse_board_meeting.json")), 1, T0) == []


def test_rbi_dates_without_timezone_are_ist():
    items = parse_feed("rbi", 2, fixture_bytes("rbi_pressreleases.xml"), T0, category="press_release")
    assert items and items[0].published_at.utcoffset().total_seconds() == 5.5 * 3600
    assert items[0].category == "press_release"


def test_media_feed_uses_canonical_url_as_id():
    items = parse_feed("business_standard", 4, fixture_bytes("bs_markets.xml"), T0)
    assert items and all(i.id.startswith("https://") and "utm_" not in i.id for i in items)


def test_aggregator_items_keep_the_outlet_site():
    # Google News names the original publisher; the dashboard shows that site's logo.
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Fed may hold rates - Barron's</title><link>https://news.google.com/rss/articles/x</link>
      <pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate><source url="https://www.barrons.com">Barron's</source></item>
      </channel></rss>"""
    item = parse_feed("google_news", 4, rss, T0)[0]
    assert item.raw["source_url"] == "https://www.barrons.com"


def test_sebi_listing_is_day_precision():
    items = parse_listing((FIX / "sebi_press_releases.html").read_text(encoding="utf-8"), 2, T0, "press_release")
    assert len(items) == 25 and all(i.published_precision == "day" for i in items)
    assert items[0].lag_seconds is None


def test_sebi_rss_is_empty():
    assert parse_feed("sebi_rss", 2, fixture_bytes("sebi_rss.xml"), T0) == []


def test_pib_listing_and_posted_on():
    items = parse_all_releases((FIX / "pib_all_releases.html").read_text(encoding="utf-8"), 2, T0)
    assert any(i.entity == "Ministry of Commerce & Industry" for i in items)
    posted = parse_posted_on((FIX / "pib_release_excerpt.html").read_text(encoding="utf-8"))
    assert posted == datetime(2026, 11, 16, 12, 0, tzinfo=IST)


def test_calendar_modes():
    cal = Calendar(yaml.safe_load((ROOT / "config/calendar.yaml").read_text(encoding="utf-8")))
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=IST)  # noqa: E731
    assert cal.mode(at("2026-11-16T10:00")) == "hot"      # Monday, market hours
    assert cal.mode(at("2026-11-16T08:40")) == "cool"     # before 08:45
    assert cal.mode(at("2026-11-16T16:00")) == "cool"
    assert cal.mode(at("2026-11-24T10:00")) == "off"      # Guru Nanak Jayanti holiday
    assert cal.mode(at("2026-11-21T10:00")) == "off"      # Saturday


def test_session_and_next_open_skip_weekends_and_holidays():
    cal = Calendar(yaml.safe_load((ROOT / "config/calendar.yaml").read_text(encoding="utf-8")))
    s = cal.session(datetime(2026, 11, 16, 16, 30, tzinfo=IST))          # Monday after the close
    assert s["trading_day"] and s["close"].hour == 15 and s["close"].minute == 30
    assert s["next_open"] == datetime(2026, 11, 17, 9, 15, tzinfo=IST)   # Tuesday
    s = cal.session(datetime(2026, 11, 23, 16, 0, tzinfo=IST))           # Monday; Tuesday 24th is Guru Nanak Jayanti
    assert s["next_open"] == datetime(2026, 11, 25, 9, 15, tzinfo=IST)
    s = cal.session(datetime(2026, 11, 21, 10, 0, tzinfo=IST))           # Saturday
    assert not s["trading_day"] and s["open"] is None and s["next_open"].weekday() == 0


def test_muhurat_override():
    cfg = yaml.safe_load((ROOT / "config/calendar.yaml").read_text(encoding="utf-8"))
    cfg["special_sessions"] = [{"date": "2026-11-08", "start": "17:30", "end": "19:30", "name": "Muhurat"}]
    cal = Calendar(cfg)
    assert cal.mode(datetime(2026, 11, 8, 18, 0, tzinfo=IST)) == "hot"
    assert cal.mode(datetime(2026, 11, 8, 12, 0, tzinfo=IST)) == "off"


def test_dry_run_replays_all_fixtures(capsys):
    from marketmon.config import load_config
    from marketmon.replay import dry_run
    asyncio.run(dry_run(load_config(ROOT / "sources.yaml"), str(FIX)))
    out = capsys.readouterr().out
    # The recorded RBI notifications are days old, so they are stale on replay: P1 digest only.
    assert "[P1 digest]" in out and "items by priority" in out
