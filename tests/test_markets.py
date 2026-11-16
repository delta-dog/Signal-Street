import json

import pytest
from conftest import fixture_bytes

from marketmon.markets import parse_bse_movers, parse_fx, parse_indices
from marketmon.store import Store


def test_nse_indices_from_recorded_payload():
    data = json.loads(fixture_bytes("nse_allindices.json"))
    out = parse_indices(data, ["NIFTY 50", "INDIA VIX", "NOT AN INDEX"], "t")
    assert [q["symbol"] for q in out] == ["NIFTY 50", "INDIA VIX"]
    n = out[0]
    assert n["last"] > 1000 and n["prev"] > 1000
    assert n["change"] == pytest.approx(n["last"] - n["prev"], abs=0.5)


def test_bse_movers_merge_gainers_and_losers_and_keep_only_constituents():
    gainers = [{"scrip_cd": 500325, "ltradert": 1500.5, "change_val": 10.5, "change_percent": 0.7, "prevdayclose": 1490}]
    losers = [{"scrip_cd": 500180, "ltradert": 980, "change_val": -12, "change_percent": -1.21, "prevdayclose": 992},
              {"scrip_cd": 999999, "ltradert": 5, "change_val": -1, "change_percent": -16.7, "prevdayclose": 6}]
    cons = [{"symbol": "RELIANCE", "name": "Reliance Industries Ltd.", "bse_code": 500325, "heavyweight": True},
            {"symbol": "HDFCBANK", "name": "HDFC Bank Ltd.", "bse_code": 500180},
            {"symbol": "BSE", "name": "BSE Ltd.", "bse_code": None}]
    out = {q["symbol"]: q for q in parse_bse_movers([gainers, losers], cons, "t")}
    assert set(out) == {"RELIANCE", "HDFCBANK"}
    assert out["RELIANCE"]["heavyweight"] and out["HDFCBANK"]["pchange"] == -1.21


def test_fx_crosses_against_inr_with_reference_change():
    live = {"data": {"currency": "USD", "rates": {"INR": "94.0", "EUR": "0.9", "JPY": "160"}}}
    ref = {"rates": {"INR": 94.47, "EUR": 0.905, "JPY": 159.0}}
    out = {q["symbol"]: q for q in parse_fx(live, ref, ["USD", "EUR", "JPY", "XXX"], "t")}
    assert out["USD/INR"]["last"] == pytest.approx(94.0)
    assert out["EUR/INR"]["last"] == pytest.approx(94.0 / 0.9)
    assert out["JPY/INR"]["last"] == pytest.approx(94.0 / 160 * 100)       # quoted per 100 yen
    assert out["USD/INR"]["pchange"] == pytest.approx((94.0 / 94.47 - 1) * 100)
    assert "XXX/INR" not in out
    assert parse_fx(live, None, ["USD"], "t")[0]["pchange"] is None     # no reference -> no change


def test_ticks_round_trip():
    st = Store(":memory:")
    st.add_ticks([("NIFTY 50", "2026-11-16T05:00:00+00:00", 25100.0), ("NIFTY 50", "2026-11-16T05:00:15+00:00", 25110.0),
                  ("USD/INR", "2026-11-16T05:00:00+00:00", 94.0)])
    s = st.ticks_since(["NIFTY 50"], "2026-11-16T00:00:00+00:00")
    assert list(s) == ["NIFTY 50"] and [p[1] for p in s["NIFTY 50"]] == [25100.0, 25110.0]
