"""Short-link expansion (`https://gar.mn/<code>`): strict input validation, manual hop-by-hop
redirect following with a host allowlist, and user-safe errors. No network: httpx.MockTransport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from garmin_live.livetrack.shortlink import (
    MAX_HOPS,
    expand_short_url,
    is_short_link,
    validate_short_url,
)
from garmin_live.livetrack.urls import InvalidLiveTrackUrlError, parse_share_url

SID = "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000"
TOK = "FAKETOKENnotreal0000ABCDEFGHI"
LONG = f"https://livetrack.garmin.com/session/{SID}/token/{TOK}"
SHORT = "https://gar.mn/abcDEF12"
SHORT2 = "https://gar.mn/second12"
SHORT3 = "https://gar.mn/third123"
SHORT4 = "https://gar.mn/fourth12"


class _BodyStream(httpx.AsyncByteStream):
    """A response body that records whether anyone tried to read it."""

    def __init__(self) -> None:
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        yield b"<html>should never be read</html>"


def _client(
    routes: dict[str, tuple[int, dict[str, str]]],
) -> tuple[httpx.AsyncClient, list[str], list[_BodyStream]]:
    """Mock transport: `routes` maps the exact request URL to (status, headers). Records every
    requested URL and returns a body stream per response so tests can assert it was not read."""
    requested: list[str] = []
    bodies: list[_BodyStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        status, headers = routes.get(str(request.url), (404, {}))
        body = _BodyStream()
        bodies.append(body)
        return httpx.Response(status, headers=headers, stream=body, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client, requested, bodies


async def _expect_rejected(
    routes: dict[str, tuple[int, dict[str, str]]], url: str = SHORT
) -> tuple[str, list[str]]:
    client, requested, _ = _client(routes)
    async with client:
        with pytest.raises(InvalidLiveTrackUrlError) as exc:
            await expand_short_url(url, client)
    return str(exc.value), requested


# ---------------------------------------------------------------------------- success paths
async def test_one_hop_success_returns_long_url_without_fetching_it() -> None:
    client, requested, bodies = _client({SHORT: (301, {"location": LONG})})
    async with client:
        assert await expand_short_url(SHORT, client) == LONG
    assert requested == [SHORT]  # livetrack.garmin.com is never requested
    assert bodies and not any(b.read for b in bodies)  # bodies are never read
    assert parse_share_url(LONG).token == TOK


async def test_multi_hop_through_allowlisted_hosts() -> None:
    client, requested, _ = _client(
        {SHORT: (302, {"location": SHORT2}), SHORT2: (301, {"location": LONG})}
    )
    async with client:
        assert await expand_short_url(SHORT, client) == LONG
    assert requested == [SHORT, SHORT2]


async def test_relative_location_is_resolved_against_the_current_url() -> None:
    client, requested, _ = _client(
        {SHORT: (301, {"location": "/second12"}), SHORT2: (301, {"location": LONG})}
    )
    async with client:
        assert await expand_short_url(SHORT, client) == LONG
    assert requested == [SHORT, SHORT2]


async def test_input_is_stripped_and_uppercase_scheme_host_tolerated() -> None:
    client, requested, _ = _client({SHORT: (301, {"location": LONG})})
    async with client:
        assert await expand_short_url(f"  {SHORT} \n", client) == LONG
    assert requested == [SHORT]


# ---------------------------------------------------------------------------- redirect policy
@pytest.mark.parametrize(
    ("location", "reason"),
    [
        (LONG.replace("livetrack.garmin.com", "evil.example"), "another host"),
        (LONG.replace("livetrack.garmin.com", "livetrack.garmin.com.evil.example"), "suffix"),
        (LONG.replace("livetrack.garmin.com", "203.0.113.5"), "ip literal"),
        (LONG.replace("https://", "http://"), "https→http downgrade"),
        (LONG.replace("https://", "https://user:pw@"), "userinfo"),
        (LONG.replace("https://", "https://livetrack.garmin.com@"), "userinfo trick"),
        (LONG.replace("livetrack.garmin.com", "livetrack.garmin.com:8443"), "port"),
        (LONG.replace("livetrack.garmin.com", "livetrack.garmin.com:443"), "explicit port"),
        ("//evil.example/x", "scheme-relative other host"),
        ("", "empty location"),
        (LONG + "\\..", "backslash"),
        ("x" * 3000, "oversized"),
    ],
)
async def test_rejects_disallowed_redirect_targets(location: str, reason: str) -> None:
    msg, requested = await _expect_rejected({SHORT: (301, {"location": location})})
    assert requested == [SHORT], reason
    assert "livetrack.garmin.com" in msg and TOK not in msg and SID not in msg


async def test_too_many_hops() -> None:
    chain = [SHORT, SHORT2, SHORT3, SHORT4]
    routes = {chain[i]: (301, {"location": chain[i + 1]}) for i in range(len(chain) - 1)}
    routes[SHORT4] = (301, {"location": LONG})  # would succeed on hop 4, one too many
    msg, requested = await _expect_rejected(routes)
    assert len(requested) == MAX_HOPS == 3
    assert "too many" in msg


@pytest.mark.parametrize("status", [200, 404, 410, 500])
async def test_non_redirect_responses_are_rejected(status: int) -> None:
    msg, requested = await _expect_rejected({SHORT: (status, {"location": LONG})})
    assert requested == [SHORT]
    assert "did not redirect" in msg and "gar.mn/" not in msg


async def test_redirect_without_location_is_rejected() -> None:
    msg, _ = await _expect_rejected({SHORT: (301, {})})
    assert "did not redirect" in msg


async def test_final_url_that_fails_parse_share_url_is_returned_then_rejected_by_parser() -> None:
    odd = f"https://livetrack.garmin.com/other/{SID}/token/{TOK}"
    client, _, _ = _client({SHORT: (301, {"location": odd})})
    async with client:
        result = await expand_short_url(SHORT, client)
    assert result == odd  # the expander stops at the host; parse_share_url is the final authority
    with pytest.raises(InvalidLiveTrackUrlError):
        parse_share_url(result)


async def test_transport_errors_propagate_as_httpx_errors() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(httpx.HTTPError):
            await expand_short_url(SHORT, client)


# ---------------------------------------------------------------------------- input validation
@pytest.mark.parametrize(
    "url",
    [
        "https://gar.mn.evil.example/abcDEF12",  # suffix trick
        "https://evil.example/abcDEF12",  # other host
        "https://user:pw@gar.mn/abcDEF12",  # userinfo
        "https://gar.mn@evil.example/abcDEF12",  # userinfo trick
        "https://gar.mn:443/abcDEF12",  # port
        "https://gar.mn:8443/abcDEF12",  # other port
        "http://gar.mn/abcDEF12",  # http
        "https://gar.mn/abc/def",  # extra path segment
        "https://gar.mn/abcDEF12/",  # trailing slash
        "https://gar.mn/abcDEF12?x=1",  # query string
        "https://gar.mn/abcDEF12?",  # empty query
        "https://gar.mn/abcDEF12#frag",  # fragment
        "https://gar.mn/" + "a" * 33,  # code too long
        "https://gar.mn/abc",  # code too short
        "https://gar.mn/abc-DEF",  # non-alphanumeric code
        "https://gar.mn/",  # no code
        "https://gar.mn",  # no path
        "https://gar.mn/abc DEF1",  # whitespace inside
        "https://gar.mn/abcDEF12\\..",  # backslash
        "https://gar.mn/" + "a" * 60,  # oversized overall
        "",
        "not a url",
        "gar.mn/abcDEF12",  # no scheme
    ],
)
async def test_malformed_short_urls_are_rejected_before_any_request(url: str) -> None:
    client, requested, _ = _client({})
    async with client:
        with pytest.raises(InvalidLiveTrackUrlError) as exc:
            await expand_short_url(url, client)
    assert requested == []
    assert "gar.mn short link" in str(exc.value)


def test_validate_short_url_normalises_whitespace() -> None:
    assert validate_short_url(f" {SHORT} ") == SHORT


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SHORT, True),
        (f"  {SHORT}  ", True),
        ("https://gar.mn:8443/abcDEF12", True),  # host is gar.mn; expand_short_url rejects the port
        ("http://gar.mn/abcDEF12", True),  # likewise: routed to the expander, which rejects http
        (LONG, False),
        ("https://gar.mn.evil.example/abcDEF12", False),
        ("https://evil.example/gar.mn/abcDEF12", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_is_short_link(url: str, expected: bool) -> None:
    assert is_short_link(url) is expected


@pytest.mark.parametrize(
    "factory",
    [
        lambda: {SHORT: (301, {"location": LONG.replace("https://", "http://")})},
        lambda: {SHORT: (200, {})},
        lambda: {},
    ],
)
async def test_error_messages_never_echo_urls_codes_or_tokens(
    factory: Callable[[], dict[str, tuple[int, dict[str, str]]]],
) -> None:
    msg, _ = await _expect_rejected(factory())
    for secret in ("abcDEF12", SID, TOK, "http://", "https://gar.mn/"):
        assert secret not in msg
