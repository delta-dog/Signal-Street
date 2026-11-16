from conftest import T0, fixture_bytes, make_item

from marketmon.models import IST
from marketmon.rules import P0, P3
from marketmon.sources.exchange_rss import parse_bse_rss, parse_nse_rss
from marketmon.sources.rss import parse_loose_date


def test_exchange_date_format_is_ist():
    d = parse_loose_date("16-Nov-2026 14:16:53", IST)
    assert d.isoformat() == "2026-11-16T14:16:53+05:30"
    assert parse_loose_date("Mon, 16 Nov 2026 12:05:00", IST).utcoffset().total_seconds() == 19800


def test_nse_rss_items_look_like_api_items():
    items = parse_nse_rss("nse_rss", 1, fixture_bytes("nse_rss_announcements.xml"), IST)
    assert len(items) == 60
    it = items[0]
    assert it.symbol == "SASTASUNDR" and it.published_precision == "s"
    assert it.title.startswith("SASTASUNDR - Copy of Newspaper Publication")
    assert it.url.startswith("https://nsearchives.nseindia.com/corporate/")


def test_bse_rss_items_carry_scrip_code():
    items = parse_bse_rss("bse_rss", 1, fixture_bytes("bse_rss_announcements.xml"), IST)
    assert len(items) == 60
    assert items[0].symbol == "539785" and items[0].entity == "Pudumjee Paper Products Ltd"
    assert items[0].id.endswith(".pdf")


def test_rss_exchange_items_match_constituents_and_rules(rules):
    noise = parse_nse_rss("nse_rss", 1, fixture_bytes("nse_rss_announcements.xml"), IST)[0]
    assert rules.classify(noise).priority == P3                     # newspaper publication
    ril = make_item("bse_rss", "Reliance Industries Ltd - Outcome of Board Meeting", symbol="500325")
    c = rules.classify(ril)
    assert c.symbol == "RELIANCE" and c.priority == P0
    by_name = make_item("nse_rss", "XYZ - Financial Results", entity="Infosys Limited")
    assert rules.classify(by_name).symbol == "INFY"
