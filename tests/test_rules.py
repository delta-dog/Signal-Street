import json

from conftest import T0, fixture_bytes, make_item

from marketmon.rules import P0, P1, P2, P3
from marketmon.sources.bse import parse_announcements as parse_bse
from marketmon.sources.nse import parse_announcements as parse_nse


def test_noise_patterns_drop_routine_filings(rules):
    for title in ["XYZ - Closure of Trading Window",
                  "XYZ - ESOP/ESOS/ESPS: allotment of shares under ESOS 2021",
                  "XYZ - Copy of Newspaper Publication",
                  "XYZ - Analysts/Institutional Investor Meet/Con. Call Updates",
                  "XYZ - Loss of Share Certificate"]:
        c = rules.classify(make_item("nse", title, symbol="RELIANCE"))
        assert c.priority == P3, title


def test_recorded_nse_newspaper_ads_are_dropped(rules):
    items = parse_nse(json.loads(fixture_bytes("nse_announcements.json")), 1, T0)
    papers = [i for i in items if "Newspaper" in i.title]
    assert papers and all(rules.classify(i).priority == P3 for i in papers)


def test_drop_never_hides_fraud_or_auditor_exit(rules):
    c = rules.classify(make_item("nse", "XYZ - Resignation of Statutory Auditor; newspaper publication", symbol="SBIN"))
    assert c.priority != P3 and c.label == "auditor"


def test_nifty50_results_are_p0_and_others_are_logged(rules):
    assert rules.classify(make_item("nse", "MARUTI - Financial Results", symbol="MARUTI")).priority == P0
    assert rules.classify(make_item("nse", "MARUTI - Outcome of Board Meeting", symbol="MARUTI")).priority == P0
    assert rules.classify(make_item("nse", "SMALLCO - Financial Results", symbol="SMALLCO")).priority == P2


def test_heavyweights_get_stricter_alerting(rules):
    order = "Bagging/Receiving of orders/contracts"
    assert rules.classify(make_item("nse", f"LT - {order}", symbol="LT")).priority == P0          # heavyweight
    assert rules.classify(make_item("nse", f"BEL - {order}", symbol="BEL")).priority == P1        # constituent


def test_bse_items_match_by_scrip_code(rules):
    rows = json.loads(fixture_bytes("bse_result.json"))
    rows["Table"][0].update(SCRIP_CD=500325, SLONGNAME="Reliance Industries Ltd")
    item = parse_bse(rows, 1, T0)[0]
    c = rules.classify(item)
    assert c.symbol == "RELIANCE" and c.heavyweight and c.priority == P0


def test_rbi_rules(rules):
    rbi = dict(source="rbi", tier="regulator")
    n = dict(category="notification", **rbi)
    assert rules.classify(make_item(title="Prudential Norms on Capital Adequacy – Risk weights on consumer credit", **n)).priority == P0
    assert rules.classify(make_item(title="Monetary Policy Statement, 2026-27", category="press_release", **rbi)).priority == P0
    assert rules.classify(make_item(title="Lending norms for NBFCs revised", **n)).priority == P1
    # Routine notifications can't move the index any more: logged, not alerted.
    assert rules.classify(make_item(title="Amendment to Master Direction on KYC", **n)).priority == P2
    assert rules.classify(make_item(title="Implementation of Section 51A of UAPA, 1967: Updates", **n)).priority == P2
    assert rules.classify(make_item(title="Money Market Operations as on November 15, 2026", category="press_release", **rbi)).priority == P2


def test_sebi_action_on_heavyweight_is_p0(rules):
    sebi = dict(source="sebi", tier="regulator", category="order")
    assert rules.classify(make_item(title="Settlement order in respect of HDFC Bank Ltd", **sebi)).priority == P0
    assert rules.classify(make_item(title="Order in the matter of Tiny Traders Pvt Ltd", **sebi)).priority == P2
    assert rules.classify(make_item(source="sebi", tier="regulator", category="circular",
                                    title="Review of position limits")).priority == P1


def test_pib_cabinet_energy_is_p0(rules):
    pib = dict(source="pib", tier="regulator")
    assert rules.classify(make_item(title="Cabinet approves Rs 20,000 crore crude oil strategic reserve", entity="Cabinet", **pib)).priority == P0
    assert rules.classify(make_item(title="Cabinet approves new AIIMS in Bihar", entity="Cabinet", **pib)).priority == P2
    wpi = rules.classify(make_item(title="Provisional WPI inflation for October 2026", entity="Ministry of Commerce & Industry", **pib))
    assert wpi.priority == P1 and wpi.impact == "index"
    trade = rules.classify(make_item(title="India and Egypt hold joint trade committee meeting", entity="Ministry of Commerce & Industry", **pib))
    assert trade.priority == P2 and trade.impact is None


def test_index_impact_tags(rules):
    m = dict(source="mint", tier="media")
    flows = rules.classify(make_item(title="FIIs pull out ₹10,000 crore as crude surges past $90", **m))
    assert flows.impact == "index" and flows.priority == P2      # index-wide, but alerts come from primary sources
    assert rules.classify(make_item(title="Stock Market LIVE: Sensex gains 500 pts", **m)).impact is None
    assert rules.classify(make_item(title="Stocks to watch: crude, rupee in focus", **m)).impact is None
    # End-of-day recaps describe the move; they don't cause it.
    for recap in ["Sensex today | Stock Market Highlights: Sensex gains 500 pts, Nifty ends at 25,291",
                  "India shares higher at close of trade; Nifty 50 up 0.58%",
                  "Rupee rises 27 paise to close at 93.92 against US dollar",
                  "Sensex ends higher as banks rally"]:
        assert rules.classify(make_item(title=recap, **m)).impact is None, recap
    assert rules.classify(make_item(title="FinMin examining changes in equity capital gains tax: CBDT", **m)).impact == "index"
    assert rules.classify(make_item("nse", "HDFCBANK - Financial Results", symbol="HDFCBANK")).impact == "heavy"
    assert rules.classify(make_item("nse", "BEL - Financial Results", symbol="BEL")).impact == "stock"
    assert rules.classify(make_item("nse", "SMALLCO - Financial Results", symbol="SMALLCO")).impact is None
    # Media chatter naming a heavyweight, with no market-moving label, can't shake the index.
    assert rules.classify(make_item(title="Jio vs Airtel vs Vi: which telco had the fewest spam complaints?", **m)).impact is None
    # A Google News query string is not part of the headline.
    gold = make_item("google_news", "Gold prices hit one-year low as dollar strengthens", tier="media", category="crude oil prices")
    assert rules.classify(gold).impact is None
    nbfc = make_item("rbi", "RBI cancels Certificate of Registration of 49 NBFCs", tier="regulator", category="press_release")
    c = rules.classify(nbfc)
    assert c.impact is None and c.priority == P2


def test_media_needs_constituent_and_label(rules):
    m = dict(source="mint", tier="media")
    assert rules.classify(make_item(title="Infosys bags $1.5 billion order worth from European bank", **m)).priority == P1
    assert rules.classify(make_item(title="Sensex ends flat; metals drag", **m)).priority == P2
    # case-sensitive aliases: the word 'eternal' is not Eternal Ltd
    assert rules.classify(make_item(title="An eternal question for bond markets", **m)).symbol is None
