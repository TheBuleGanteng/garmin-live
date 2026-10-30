"""Server-side expansion of Garmin short links (`https://gar.mn/<code>`).

The Garmin app shares LiveTrack sessions as `https://gar.mn/<code>`, which answers `301` with the
full `https://livetrack.garmin.com/session/<uuid>/token/<token>` URL (see
docs/LIVETRACK_FINDINGS.md, "Short links"). `expand_short_url` follows that chain **manually**,
one hop at a time, so every intermediate `Location` is checked as strictly as the input:

- the input must be exactly `https://gar.mn/<code>` (no userinfo, port, query or fragment);
- every redirect target must be https, carry no userinfo or port, and point at an allowlisted
  host (`gar.mn` or `livetrack.garmin.com`), with at most `MAX_HOPS` hops;
- the first URL on `livetrack.garmin.com` is returned **without being fetched**; the caller runs
  it through `parse_share_url`, which remains the only authority on the final shape.

Response bodies are never read and URLs, codes and tokens are never logged. Every failure raises
`InvalidLiveTrackUrlError` with a message that is safe to show to the user; transport failures
(`httpx.HTTPError`) propagate for the caller to wrap.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx

from garmin_live.livetrack.urls import LIVETRACK_HOST, InvalidLiveTrackUrlError

SHORT_LINK_HOST = "gar.mn"
#: Hosts a redirect may land on. Matches the chain observed in the findings doc exactly.
REDIRECT_HOST_ALLOWLIST = frozenset({SHORT_LINK_HOST, LIVETRACK_HOST})
MAX_HOPS = 3
DEFAULT_TIMEOUT_S = 10.0
MAX_SHORT_URL_LEN = 64
MAX_LOCATION_LEN = 2048

_CODE_PATH_RE = re.compile(r"^/[A-Za-z0-9]{4,32}$")

_MSG_NOT_SHORT = "That does not look like a gar.mn short link."
_MSG_BAD_TARGET = "The short link did not lead to livetrack.garmin.com."
_MSG_TOO_MANY = "The short link redirected too many times."
_MSG_NO_REDIRECT = "The short link did not redirect to a LiveTrack session (it may have expired)."


def is_short_link(url: str) -> bool:
    """True when the (stripped) URL's host is `gar.mn`; validation happens in `expand_short_url`."""
    try:
        return urlsplit(url.strip()).hostname == SHORT_LINK_HOST
    except ValueError:
        return False


def validate_short_url(url: str) -> str:
    """Return the normalised short URL or raise `InvalidLiveTrackUrlError` (never echoes it)."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT)
    url = url.strip()
    if (
        len(url) > MAX_SHORT_URL_LEN
        or any(ch.isspace() for ch in url)
        or "\\" in url
        or "?" in url
        or "#" in url
    ):
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT)
    try:
        parts = urlsplit(url)
    except ValueError:
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT) from None
    if parts.scheme != "https":
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT)
    if parts.port is not None or parts.netloc != SHORT_LINK_HOST:
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT)
    if _CODE_PATH_RE.match(parts.path) is None:
        raise InvalidLiveTrackUrlError(_MSG_NOT_SHORT)
    return url


def _check_redirect_target(location: str, current: str) -> str:
    """Resolve `location` against `current` and enforce the redirect policy."""
    if not location or len(location) > MAX_LOCATION_LEN:
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET)
    location = location.strip()
    if any(ch.isspace() for ch in location) or "\\" in location:
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET)
    try:
        target = urljoin(current, location)
        parts = urlsplit(target)
    except ValueError:
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET) from None
    if parts.scheme != "https":
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET)
    if parts.port is not None:
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET)
    if parts.hostname not in REDIRECT_HOST_ALLOWLIST or parts.netloc != parts.hostname:
        raise InvalidLiveTrackUrlError(_MSG_BAD_TARGET)
    return target


async def expand_short_url(url: str, client: httpx.AsyncClient) -> str:
    """Expand a `https://gar.mn/<code>` link to its `livetrack.garmin.com` target.

    `client` must have been created with `follow_redirects=False`; redirects are followed here,
    one hop at a time, and `follow_redirects=False` is also forced per request. Bodies are not
    read. Returns the first https URL on `livetrack.garmin.com` (unfetched). Raises
    `InvalidLiveTrackUrlError` on any policy violation; `httpx.HTTPError` on transport failure.
    """
    current = validate_short_url(url)
    for _hop in range(MAX_HOPS):
        request = client.build_request("GET", current, headers={"Accept": "text/html,*/*"})
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            status = response.status_code
            location = response.headers.get("location")
        finally:
            await response.aclose()
        if not (300 <= status < 400) or location is None:
            raise InvalidLiveTrackUrlError(_MSG_NO_REDIRECT)
        current = _check_redirect_target(location, current)
        if urlsplit(current).hostname == LIVETRACK_HOST:
            return current
    raise InvalidLiveTrackUrlError(_MSG_TOO_MANY)
