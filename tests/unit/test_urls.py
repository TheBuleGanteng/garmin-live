"""Share-URL validation: only https://livetrack.garmin.com/session/<uuid>/token/<token>."""

from __future__ import annotations

import pytest

from garmin_live.livetrack.urls import InvalidLiveTrackUrlError, parse_share_url

SID = "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000"
TOK = "FAKETOKENnotreal0000ABCDEFGHI"
GOOD = f"https://livetrack.garmin.com/session/{SID}/token/{TOK}"


def test_valid_url_parses() -> None:
    share = parse_share_url(GOOD)
    assert share.session_id == SID
    assert share.token == TOK
    assert share.page_url == GOOD


def test_trailing_slash_and_whitespace_tolerated() -> None:
    assert parse_share_url(f"  {GOOD}/ ").token == TOK


def test_uppercase_uuid_is_normalised() -> None:
    assert parse_share_url(GOOD.replace(SID, SID.upper())).session_id == SID


@pytest.mark.parametrize(
    "url",
    [
        GOOD.replace("https://", "http://"),  # http
        GOOD.replace("livetrack.garmin.com", "evil.example"),  # other host
        GOOD.replace("livetrack.garmin.com", "livetrack.garmin.com.evil.example"),  # suffix trick
        GOOD.replace("livetrack.garmin.com", "203.0.113.5"),  # IP literal
        GOOD.replace("livetrack.garmin.com", "[2001:db8::1]"),  # IPv6 literal
        GOOD.replace("https://", "https://livetrack.garmin.com@"),  # userinfo trick
        GOOD.replace("https://", "https://user:pw@"),  # userinfo
        GOOD.replace("livetrack.garmin.com", "livetrack.garmin.com:8443"),  # port
        "https://gar.mn/abcdefgh",  # short link must be expanded first
        f"https://livetrack.garmin.com/session/{SID}",  # no token
        f"https://livetrack.garmin.com/session/not-a-uuid/token/{TOK}",  # bad uuid
        f"https://livetrack.garmin.com/other/{SID}/token/{TOK}",  # wrong path shape
        f"https://livetrack.garmin.com/session/{SID}/token/{TOK}/extra",  # extra segment
        f"https://livetrack.garmin.com/session/{SID}/token/short",  # token too short
        f"https://livetrack.garmin.com/session/{SID}/token/{TOK}?x=../../",  # query ignored
        "",
        "not a url",
        f"https://livetrack.garmin.com/session/{SID}/token/{TOK}\\..",
    ],
)
def test_rejects(url: str) -> None:
    if url.endswith("?x=../../"):
        # a query string does not change the path and is simply ignored
        assert parse_share_url(url).token == TOK
        return
    with pytest.raises(InvalidLiveTrackUrlError):
        parse_share_url(url)


def test_error_messages_never_echo_the_url() -> None:
    try:
        parse_share_url(GOOD.replace("https://", "http://"))
    except InvalidLiveTrackUrlError as exc:
        assert TOK not in str(exc)
        assert SID not in str(exc)
