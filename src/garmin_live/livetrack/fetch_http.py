"""Plain-HTTP LiveTrack fetcher (`FETCH_STRATEGY=http`).

Protocol, per docs/LIVETRACK_FINDINGS.md:

1. GET the share page `{base}/session/<id>/token/<token>`. The response sets the `livetrack_csrf`
   cookie and embeds `<meta name="csrf-token" content="...">`.
2. Call the API with that cookie, the `livetrack-csrf-token` header and `?token=<token>`.
3. On 403, the pair is stale: bootstrap again once, then give up for this poll.

`base_url` is `https://livetrack.garmin.com` for real sessions. The replay server (tests,
selftest) is reached through the internal service API with a different base; the MCP tool can
never supply one. Redirects are never followed.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Protocol

import httpx

from garmin_live.livetrack.models import SessionInfo, Trackpoint
from garmin_live.livetrack.parser import parse_session, parse_trackpoints
from garmin_live.livetrack.urls import LIVETRACK_API_BASE

_META_CSRF = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')
CSRF_COOKIE = "livetrack_csrf"
CSRF_HEADER = "livetrack-csrf-token"
DEFAULT_TIMEOUT_S = 20.0


class FetchError(Exception):
    """A LiveTrack request failed. `status` is the HTTP status (0 for transport errors)."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"LiveTrack fetch failed ({status}): {detail}")

    @property
    def retryable(self) -> bool:
        return self.status in (0, 429) or self.status >= 500


class SessionExpiredError(FetchError):
    """The share link no longer serves data (404/410)."""


class AuthError(FetchError):
    """403 even after a fresh bootstrap."""


class Fetcher(Protocol):
    async def fetch_session(self) -> SessionInfo: ...

    async def fetch_trackpoints(self, begin: datetime | None) -> list[Trackpoint]: ...

    async def aclose(self) -> None: ...


def format_begin(begin: datetime) -> str:
    """Render `begin` the way the LiveTrack page does: ISO-8601 UTC with milliseconds and 'Z'."""
    return begin.strftime("%Y-%m-%dT%H:%M:%S.") + f"{begin.microsecond // 1000:03d}Z"


class HttpFetcher:
    """Fetches one LiveTrack session over plain HTTPS."""

    def __init__(
        self,
        session_id: str,
        token: str,
        *,
        base_url: str = LIVETRACK_API_BASE,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._session_id = session_id
        self._token = token
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
        self._csrf_token: str | None = None

    # ---- bootstrap --------------------------------------------------------------------
    async def bootstrap(self) -> None:
        """Load the share page to obtain the csrf cookie + header pair."""
        url = f"{self._base}/session/{self._session_id}/token/{self._token}"
        try:
            resp = await self._client.get(url, headers={"Accept": "text/html,*/*"})
        except httpx.HTTPError as exc:
            raise FetchError(0, f"share page request failed: {type(exc).__name__}") from exc
        if resp.status_code in (404, 410):
            raise SessionExpiredError(resp.status_code, "share page is gone")
        if resp.status_code != 200:
            raise FetchError(resp.status_code, "share page did not load")
        match = _META_CSRF.search(resp.text)
        if match is None:
            raise FetchError(200, "share page has no csrf-token meta tag (page layout changed?)")
        if not self._client.cookies.get(CSRF_COOKIE):
            raise FetchError(200, f"share page did not set the {CSRF_COOKIE} cookie")
        self._csrf_token = match.group(1)

    # ---- API calls --------------------------------------------------------------------
    async def _get_json(self, path: str, params: dict[str, str]) -> Any:
        if self._csrf_token is None:
            await self.bootstrap()
        for attempt in (1, 2):
            assert self._csrf_token is not None
            try:
                resp = await self._client.get(
                    f"{self._base}{path}",
                    params={"token": self._token, **params},
                    headers={CSRF_HEADER: self._csrf_token, "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                raise FetchError(0, f"request failed: {type(exc).__name__}") from exc
            if resp.status_code == 403 and attempt == 1:
                self._csrf_token = None
                await self.bootstrap()
                continue
            if resp.status_code == 403:
                raise AuthError(403, "forbidden even after a fresh bootstrap")
            if resp.status_code in (404, 410):
                raise SessionExpiredError(resp.status_code, "session no longer available")
            if resp.status_code != 200:
                raise FetchError(resp.status_code, "unexpected status")
            try:
                return resp.json()
            except ValueError as exc:
                raise FetchError(200, "response is not JSON") from exc
        raise AuthError(403, "unreachable")  # pragma: no cover

    async def fetch_session(self) -> SessionInfo:
        payload = await self._get_json(f"/api/v2/sessions/{self._session_id}", {})
        return parse_session(payload)

    async def fetch_trackpoints(self, begin: datetime | None) -> list[Trackpoint]:
        params = {"begin": format_begin(begin)} if begin is not None else {}
        payload = await self._get_json(
            f"/api/sessions/{self._session_id}/track-points/common", params
        )
        return parse_trackpoints(payload)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
