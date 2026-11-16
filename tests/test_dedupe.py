import asyncio
import json
from datetime import timedelta

from conftest import T0, fixture_bytes, make_item

from marketmon.alerts import Dispatcher
from marketmon.dedupe import Clusterer, hamming, simhash, title_tokens
from marketmon.runner import Pipeline
from marketmon.sources.base import canonical_url
from marketmon.sources.nse import parse_announcements
from marketmon.store import Store


def assign(c: Clusterer, *, source, rank, title, symbol=None, label=None, at=T0):
    cid, role, sh = c.assign(source=source, rank=rank, title=title, symbol=symbol, label=label, at=at)
    if cid is None:
        cid = len(c.entries) + 100
    c.add(cid, source=source, rank=rank, title=title, symbol=symbol, label=label, at=at, sh=sh)
    return cid, role


def test_canonical_url_strips_tracking():
    a = canonical_url("https://www.livemint.com/market/x-123.html?utm_source=rss&utm_medium=feed#top")
    b = canonical_url("https://WWW.livemint.com/market/x-123.html")
    assert a == b


def test_simhash_near_duplicate_headlines():
    a = simhash(title_tokens("Reliance Q2 profit rises 10% to Rs 19,000 crore, beats estimates"))
    b = simhash(title_tokens("Reliance Q2 profit rises 10% to Rs 19,000 crore; beats estimates - Mint",
                             strip_publisher=True))
    c = simhash(title_tokens("Infosys wins $1.5 billion deal from European bank"))
    assert hamming(a, b) <= 3
    assert hamming(a, c) > 10


def test_media_duplicate_across_outlets():
    c = Clusterer()
    cid, role = assign(c, source="business_standard", rank=4,
                       title="Crude oil jumps 3% as OPEC+ extends output cuts into 2027")
    assert role == "lead"
    cid2, role2 = assign(c, source="mint", rank=4, at=T0 + timedelta(minutes=20),
                         title="Crude oil jumps 3% as OPEC+ extends output cuts into 2027 - Livemint")
    assert (cid2, role2) == (cid, "dupe")


def test_different_companies_same_template_are_not_merged():
    c = Clusterer()
    assign(c, source="et_markets", rank=4, symbol="ICICIBANK",
           title="ICICI Bank Share Price Live Updates: ICICI Bank Stock Details")
    _, role = assign(c, source="et_markets", rank=4, symbol="HDFCLIFE",
                     title="HDFC Life Share Price Live Updates: HDFC Life Stock Details")
    assert role == "lead"


def test_regulator_documents_only_merge_when_identical():
    c = Clusterer()
    assign(c, source="sebi", rank=2, title="Framework for Issuance of Depository Receipts on Units of REITs")
    _, role = assign(c, source="sebi", rank=2, title="Framework for Issuance of Depository Receipts on Units of InvITs")
    assert role == "lead"
    _, role = assign(c, source="sebi_rss", rank=2, title="Framework for Issuance of Depository Receipts on Units of REITs")
    assert role == "dupe"


def test_nse_and_bse_copies_of_one_filing():
    c = Clusterer(cross_exchange_minutes=30)
    cid, _ = assign(c, source="nse", rank=1, symbol="RELIANCE", label="results",
                    title="RELIANCE - Financial Results")
    cid2, role = assign(c, source="bse", rank=1, symbol="RELIANCE", label="results", at=T0 + timedelta(seconds=40),
                        title="Reliance Industries Ltd - Unaudited Financial Results for Q2")
    assert (cid2, role) == (cid, "dupe")
    _, role = assign(c, source="bse", rank=1, symbol="RELIANCE", label="results", at=T0 + timedelta(minutes=45),
                     title="Reliance Industries Ltd - Results: clarification")
    assert role == "lead"


def test_unlabelled_nse_bse_copies_merge_on_headline_overlap():
    c = Clusterer()
    cid, _ = assign(c, source="bse", rank=1, symbol="ADANIENT",
                    title="Adani Enterprises Ltd - Media Release on the subject - Adani Wind Begins Production of India's "
                          "Longest Wind Turbine Blade; 5MW Platform Certified for Serial Production")
    cid2, role = assign(c, source="nse", rank=1, symbol="ADANIENT", at=T0 + timedelta(seconds=4),
                        title="ADANIENT - Press Release: Adani Enterprises Limited has informed the Exchange regarding a press "
                              "release titled Media Release on the subject Adani Wind Begins Production of India's Longest Wind "
                              "Turbine Blade; 5 MW Platform Certified for Serial Production")
    assert (cid2, role) == (cid, "dupe")
    _, role = assign(c, source="nse", rank=1, symbol="ADANIENT", at=T0 + timedelta(seconds=9),
                     title="ADANIENT - Updates: Allotment of commercial paper")
    assert role == "lead"                                  # different filing from the same company


def test_media_story_about_a_filing_is_a_followup():
    c = Clusterer()
    cid, _ = assign(c, source="nse", rank=1, symbol="TCS", label="orders",
                    title="TCS - Bagging/Receiving of orders/contracts")
    cid2, role = assign(c, source="mint", rank=4, symbol="TCS", label="orders", at=T0 + timedelta(minutes=9),
                        title="TCS shares rise after IT major bags multi-year deal from UK insurer")
    assert (cid2, role) == (cid, "followup")


def test_cluster_window_expires_after_6_hours():
    c = Clusterer(window_hours=6)
    assign(c, source="mint", rank=4, title="Sensex ends 500 points higher as banks rally on rate cut hopes")
    _, role = assign(c, source="ndtv_profit", rank=4, at=T0 + timedelta(hours=7),
                     title="Sensex ends 500 points higher as banks rally on rate cut hopes")
    assert role == "lead"


def test_resurfaced_old_story_is_not_alerted_or_counted_as_lag(rules):
    store = Store(":memory:")
    sink = Recorder()
    pipe = Pipeline(store, rules, Clusterer(), Dispatcher([sink], store))
    old = make_item("mint", "Infosys bags $1.5 billion order worth from European bank", tier="media",
                    id="https://livemint.com/old", published=T0 - timedelta(days=30), seen=T0)
    new = make_item("mint", "HCLTech bags $700 million order worth from US retailer", tier="media",
                    id="https://livemint.com/new", published=T0 - timedelta(minutes=4), seen=T0)

    async def go():
        await pipe.ingest([old, new], baseline=False)
        await pipe.dispatcher.flush()

    asyncio.run(go())
    assert [r["item_id"] for r in store.q("SELECT item_id FROM lag")] == ["https://livemint.com/new"]
    assert len(sink.sent) == 1 and "HCLTech" in sink.sent[0] and "Infosys" not in sink.sent[0]


class Recorder:
    def __init__(self):
        self.sent = []

    name = "rec"

    async def send(self, text):
        self.sent.append(text)


def test_pipeline_hard_key_and_followups_do_not_alert(rules):
    store = Store(":memory:")
    sink = Recorder()
    pipe = Pipeline(store, rules, Clusterer(), Dispatcher([sink], store))
    items = parse_announcements(json.loads(fixture_bytes("nse_announcements.json")), 1, T0)

    async def go():
        n1 = await pipe.ingest(items, baseline=False)
        n2 = await pipe.ingest(items, baseline=False)    # same announcement ids again
        filing = make_item("nse", "TCS - Bagging/Receiving of orders/contracts", symbol="TCS", id="t1")
        story = make_item("mint", "TCS bags order: TCS shares rise after IT major wins deal", tier="media",
                          id="https://livemint.com/tcs", seen=filing.first_seen_at + timedelta(minutes=5))
        await pipe.ingest([filing, story], baseline=False)
        return n1, n2

    n1, n2 = asyncio.run(go())
    assert n1 == len(items) and n2 == 0
    roles = {r["source"]: r["role"] for r in store.q("SELECT source, role FROM items WHERE id IN ('t1','https://livemint.com/tcs')")}
    assert roles == {"nse": "lead", "mint": "followup"}
    assert sum("TCS" in s for s in sink.sent) == 1   # the filing (heavyweight + orders = P0), not the story
