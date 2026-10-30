"""HttpFetcher: bootstrap handshake, re-bootstrap on 403, expiry, redirects, schema errors."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from garmin_live.livetrack.fetch_http import (
    AuthError,
    FetchError,
    HttpFetcher,
    SessionExpiredError,
    format_begin,
)
from garmin_live.livetrack.parser import LiveTrackSchemaError

SID = "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000"
TOK = "FAKETOKENnotreal0000ABCDEFGHI"
BASE = "https://livetrack.garmin.com"
PAGE = '<html><head><meta name="csrf-token" content="meta-token-1"></head><body/></html>'
SESSION_BODY = {
    "sessionId": SID,
    "start": "2026-01-01T06:00:00.000Z",
    "end": "2026-01-02T06:00:00.000Z",
    "viewable": True,
    "activeMultiDayActivities": [],
}


class FakeLiveTrack:
    """An httpx MockTransport that behaves like LiveTrack's auth handshake."""

    def __init__(self) -> None:
        self.page_loads = 0
        self.api_calls: list[httpx.Request] = []
        self.valid_pairs: dict[str, str] = {}
        self.fail_api_with: int | None = None
        self.page_status = 200
        self.trackpoints: list[dict[str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/session/"):
            self.page_loads += 1
            if self.page_status != 200:
                return httpx.Response(self.page_status)
            cookie = f"cookie-{self.page_loads}"
            header = f"meta-token-{self.page_loads}"
            self.valid_pairs[cookie] = header
            return httpx.Response(
                200,
                text=PAGE.replace("meta-token-1", header),
                headers={"set-cookie": f"livetrack_csrf={cookie}; Path=/; HttpOnly"},
            )
        self.api_calls.append(request)
        if self.fail_api_with:
            return httpx.Response(self.fail_api_with)
        if request.url.params.get("token") != TOK:
            return httpx.Response(403)
        cookie = request.headers.get("cookie", "")
        hdr = request.headers.get("livetrack-csrf-token", "")
        pair_ok = any(
            f"livetrack_csrf={c}" in cookie and h == hdr for c, h in self.valid_pairs.items()
        )
        if not pair_ok:
            return httpx.Response(403)
        if path.startswith("/api/v2/sessions/"):
            return httpx.Response(200, json=SESSION_BODY)
        if "/track-points/" in path:
            begin = request.url.params.get("begin")
            pts = [p for p in self.trackpoints if begin is None or str(p["dateTime"]) > begin]
            return httpx.Response(200, json={"trackPoints": pts})
        return httpx.Response(404)


def _fetcher(fake: FakeLiveTrack) -> HttpFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler), follow_redirects=False)
    return HttpFetcher(SID, TOK, base_url=BASE, client=client)


async def test_bootstrap_then_api_calls_succeed() -> None:
    fake = FakeLiveTrack()
    fake.trackpoints = [
        {
            "dateTime": "2026-01-01T06:06:17.216Z",
            "position": {"lat": -33.0, "lon": -40.0},
            "totalDistanceMeters": 10.0,
            "totalDurationSecs": 10.0,
            "eventTypes": [],
        }
    ]
    f = _fetcher(fake)
    info = await f.fetch_session()
    assert info.session_id == SID and fake.page_loads == 1
    pts = await f.fetch_trackpoints(None)
    assert len(pts) == 1 and fake.page_loads == 1  # pair reused
    later = await f.fetch_trackpoints(datetime(2026, 1, 1, 6, 7, tzinfo=UTC))
    assert later == []
    begin_param = fake.api_calls[-1].url.params["begin"]
    assert begin_param == "2026-01-01T06:07:00.000Z"
    assert "token" in fake.api_calls[-1].url.params
    await f.aclose()


async def test_403_triggers_one_rebootstrap_then_auth_error() -> None:
    fake = FakeLiveTrack()
    f = _fetcher(fake)
    await f.fetch_session()
    fake.valid_pairs.clear()  # server-side pair invalidated
    await f.fetch_session()  # re-bootstraps transparently
    assert fake.page_loads == 2
    fake.fail_api_with = 403
    with pytest.raises(AuthError):
        await f.fetch_session()
    assert fake.page_loads == 3  # exactly one extra bootstrap attempt


async def test_expiry_and_server_errors() -> None:
    fake = FakeLiveTrack()
    f = _fetcher(fake)
    fake.fail_api_with = 404
    with pytest.raises(SessionExpiredError):
        await f.fetch_trackpoints(None)
    fake.fail_api_with = 503
    with pytest.raises(FetchError) as info:
        await f.fetch_trackpoints(None)
    assert info.value.status == 503 and info.value.retryable
    fake.fail_api_with = 429
    with pytest.raises(FetchError) as info2:
        await f.fetch_trackpoints(None)
    assert info2.value.status == 429 and info2.value.retryable
    fake.fail_api_with = None
    fake.page_status = 410
    f2 = _fetcher(fake)
    with pytest.raises(SessionExpiredError):
        await f2.fetch_session()


async def test_redirect_is_not_followed() -> None:
    def redirecting(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(redirecting), follow_redirects=False)
    f = HttpFetcher(SID, TOK, base_url=BASE, client=client)
    with pytest.raises(FetchError) as info:
        await f.fetch_session()
    assert info.value.status == 302
    assert not info.value.retryable


async def test_page_without_meta_or_cookie_is_an_error() -> None:
    def no_meta(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>changed layout</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(no_meta), follow_redirects=False)
    with pytest.raises(FetchError, match="csrf-token"):
        await HttpFetcher(SID, TOK, base_url=BASE, client=client).fetch_session()

    def no_cookie(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=PAGE)

    client2 = httpx.AsyncClient(transport=httpx.MockTransport(no_cookie), follow_redirects=False)
    with pytest.raises(FetchError, match="cookie"):
        await HttpFetcher(SID, TOK, base_url=BASE, client=client2).fetch_session()


async def test_transport_error_and_bad_json_and_schema_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom), follow_redirects=False)
    with pytest.raises(FetchError) as info:
        await HttpFetcher(SID, TOK, base_url=BASE, client=client).fetch_session()
    assert info.value.status == 0 and info.value.retryable

    fake = FakeLiveTrack()
    f = _fetcher(fake)
    await f.fetch_session()
    fake.trackpoints = [{"dateTime": "2026-01-01T06:06:17.216Z", "eventTypes": []}]  # no position
    with pytest.raises(LiveTrackSchemaError) as err:
        await f.fetch_trackpoints(None)
    assert err.value.field == "trackPoints[0].position"

    def not_json(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/session/"):
            return fake.handler(request)
        return httpx.Response(200, text="<html>maintenance</html>")

    client3 = httpx.AsyncClient(transport=httpx.MockTransport(not_json), follow_redirects=False)
    with pytest.raises(FetchError, match="not JSON"):
        await HttpFetcher(SID, TOK, base_url=BASE, client=client3).fetch_session()


def test_format_begin_matches_livetrack_style() -> None:
    assert (
        format_begin(datetime(2026, 1, 1, 6, 6, 17, 216000, tzinfo=UTC))
        == "2026-01-01T06:06:17.216Z"
    )
    assert (
        json.dumps(format_begin(datetime(2026, 1, 1, tzinfo=UTC))) == '"2026-01-01T00:00:00.000Z"'
    )
