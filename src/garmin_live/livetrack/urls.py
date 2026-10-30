"""Strict validation of LiveTrack share URLs (SSRF guard).

Only `https://livetrack.garmin.com/session/<uuid>/token/<token>` is accepted. Anything else —
other hosts, http, IP literals, userinfo tricks, ports, odd paths — is rejected. Redirects are
never followed by the fetcher, so a URL that redirects elsewhere cannot escape this check either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

LIVETRACK_HOST = "livetrack.garmin.com"
LIVETRACK_API_BASE = f"https://{LIVETRACK_HOST}"

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_OPAQUE_SEGMENT = r"[A-Za-z0-9_-]{8,128}"
_PATH_RE = re.compile(rf"^/session/({_UUID})/token/({_OPAQUE_SEGMENT})/?$")


class InvalidLiveTrackUrlError(ValueError):
    """Not an acceptable LiveTrack share link; the message is safe to show to the user."""


@dataclass(frozen=True, slots=True)
class ShareUrl:
    session_id: str
    token: str

    @property
    def page_url(self) -> str:
        return f"{LIVETRACK_API_BASE}/session/{self.session_id}/token/{self.token}"


def parse_share_url(url: str) -> ShareUrl:
    """Validate a share URL and extract its session ID and token.

    Raises `InvalidLiveTrackUrlError` with a user-facing reason. Never logs the URL.
    """
    if not isinstance(url, str) or not url.strip():
        raise InvalidLiveTrackUrlError("A LiveTrack share URL is required.")
    url = url.strip()
    if len(url) > 2048 or any(ch.isspace() for ch in url) or "\\" in url:
        raise InvalidLiveTrackUrlError("That does not look like a LiveTrack share URL.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise InvalidLiveTrackUrlError("The LiveTrack URL must use https.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise InvalidLiveTrackUrlError("The LiveTrack URL must not contain user information.")
    if parts.port is not None:
        raise InvalidLiveTrackUrlError("The LiveTrack URL must not specify a port.")
    if parts.hostname != LIVETRACK_HOST or parts.netloc != LIVETRACK_HOST:
        raise InvalidLiveTrackUrlError(
            f"The host must be exactly {LIVETRACK_HOST}. "
            "Short links (gar.mn) must be expanded first."
        )
    match = _PATH_RE.match(parts.path)
    if match is None:
        raise InvalidLiveTrackUrlError("Expected a path like /session/<id>/token/<token>.")
    return ShareUrl(session_id=match.group(1).lower(), token=match.group(2))
