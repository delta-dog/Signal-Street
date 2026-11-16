import json

import pytest

from marketmon.charts import bse_points, fx_points, nse_points


def test_nse_chart_timestamps_are_ist_clock_shifted_to_utc():
    # NSE encodes 09:00 IST as 09:00 "UTC" epoch milliseconds.
    payload = {"data": {"closePrice": 25144.3, "grapthData": [[1794819600000, 25144.3, "PO"], [1794819659000, 25088.8, "PO"]]}}
    pts, close = nse_points(payload)
    assert close == 25144.3
    assert pts[0] == ["2026-11-16T03:30:00+00:00", 25144.3]        # 09:00 IST
    assert pts[1][1] == 25088.8                                     # only pre-open so far: keep it
    payload["data"]["grapthData"].append([1794820500000, 25311.1, "NM"])
    payload["data"]["grapthData"].append([1794842401000, 25404.35, "IC"])   # 15:20 indicative close
    pts, _ = nse_points(payload)
    assert pts == [["2026-11-16T03:45:00+00:00", 25311.1]]          # pre-open and indicative close dropped


def test_bse_chart_data_string_is_parsed_and_ist():
    payload = {"PrevClose": "1339.6", "Data": json.dumps([
        {"dttm": "Mon Nov 16 2026 09:15:59", "vale1": "1342.85", "vole": "100"},
        {"dttm": "garbage", "vale1": "1"}])}
    pts, prev = bse_points(json.dumps(payload))                     # BSE sometimes returns the JSON as a string
    assert prev == 1339.6
    assert pts == [["2026-11-16T03:45:59+00:00", 1342.85]]
    assert bse_points({"PrevClose": "0", "Data": "[]"}) == ([], None)


def test_fx_history_crosses_against_inr():
    payload = {"rates": {"2026-11-13": {"INR": 94.25, "EUR": 0.86, "JPY": 160.0}, "2026-11-12": {"INR": 94.0, "EUR": 0.85}}}
    usd = fx_points(payload, "USD")
    assert [p[1] for p in usd] == [94.0, 94.25]                     # sorted by date
    eur = fx_points(payload, "EUR")
    assert eur[-1][1] == pytest.approx(94.25 / 0.86)
    jpy = fx_points(payload, "JPY")
    assert len(jpy) == 1 and jpy[0][1] == pytest.approx(94.25 / 160 * 100)
