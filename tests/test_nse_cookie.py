"""Cookie-session handling for NSE, using httpx.MockTransport and the recorded payload."""
import asyncio

import httpx
from conftest import fixture_bytes

from marketmon.http import HostLimiter
from marketmon.sources.nse import NseSource

API = "/api/corporate-announcements"


def run_source(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append((request.url.path, request.headers.get("cookie")))
        return handler(request, calls)

    src = NseSource("nse", {"tier": "exchange"}, HostLimiter({}, default=0), "test",
                    transport=httpx.MockTransport(wrapped))

    async def go():
        try:
            return await src.poll()
        finally:
            await src.aclose()

    return asyncio.run(go()), calls


def home(n):
    return httpx.Response(200, headers={"set-cookie": f"nsit=session{n}; Path=/"}, text="<html></html>")


def test_warms_home_page_then_reuses_cookie():
    def handler(req, calls):
        if req.url.path == "/":
            return home(1)
        assert "nsit=session1" in (req.headers.get("cookie") or "")
        return httpx.Response(200, content=fixture_bytes("nse_announcements.json"),
                              headers={"content-type": "application/json"})

    (res,), calls = run_source(handler)
    assert [p for p, _ in calls] == ["/", API]
    assert len(res.items) > 100 and res.events == []


def test_refreshes_cookie_on_401_and_retries():
    homes = iter(range(1, 10))

    def handler(req, calls):
        if req.url.path == "/":
            return home(next(homes))
        if "session1" in (req.headers.get("cookie") or ""):
            return httpx.Response(401, text="unauthorized")
        return httpx.Response(200, content=fixture_bytes("nse_announcements.json"),
                              headers={"content-type": "application/json"})

    (res,), calls = run_source(handler)
    assert [p for p, _ in calls] == ["/", API, "/", API]
    assert "cookie_refresh" in res.events and res.items


def test_refreshes_on_resource_not_found_body():
    state = {"n": 0}

    def handler(req, calls):
        if req.url.path == "/":
            return home(1)
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, text='{"msg":"Resource Not Found"}', headers={"content-type": "application/json"})
        return httpx.Response(200, content=fixture_bytes("nse_announcements.json"),
                              headers={"content-type": "application/json"})

    (res,), _ = run_source(handler)
    assert "cookie_refresh" in res.events and res.items


def test_persistent_block_is_reported_not_retried_forever():
    def handler(req, calls):
        if req.url.path == "/":
            return home(1)
        return httpx.Response(403, text="<html><title>Access Denied</title></html>",
                              headers={"content-type": "text/html"})

    (res,), calls = run_source(handler)
    assert len(calls) == 4                      # home, api, home (refresh), api; then give up
    assert "blocked" in res.events and not res.items


def test_blocked_home_page_degrades_gracefully():
    def handler(req, calls):
        return httpx.Response(403, text="<html><title>Access Denied</title>Reference #18</html>",
                              headers={"content-type": "text/html"})

    (res,), calls = run_source(handler)
    assert len(calls) == 1 and "blocked" in res.events and "warm-up failed" in res.note
