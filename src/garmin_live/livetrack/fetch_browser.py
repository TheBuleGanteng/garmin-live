"""Browser-based fetcher placeholder (`FETCH_STRATEGY=browser`).

Phase 1 decided D1 (plain HTTP works from the VM), so this strategy is not implemented in v1.
It is kept as a named seam: a future build would drive headless Chromium with Playwright and
intercept the `/api/...` network responses (not scrape the DOM), exposing the same `Fetcher`
protocol as `HttpFetcher`. Selecting it today fails fast with a clear message.
"""

from __future__ import annotations

from datetime import datetime

from garmin_live.livetrack.models import SessionInfo, Trackpoint


class BrowserFetcher:
    """Not available in this build; see docs/LIVETRACK_FINDINGS.md."""

    def __init__(self, session_id: str, token: str, *, base_url: str = "") -> None:
        self._session_id = session_id
        self._token = token
        self._base_url = base_url

    async def fetch_session(self) -> SessionInfo:
        raise NotImplementedError(
            "FETCH_STRATEGY=browser is not implemented in this build; use FETCH_STRATEGY=http "
            "(decision D1 in docs/LIVETRACK_FINDINGS.md)."
        )

    async def fetch_trackpoints(self, begin: datetime | None) -> list[Trackpoint]:
        raise NotImplementedError(
            "FETCH_STRATEGY=browser is not implemented in this build; use FETCH_STRATEGY=http."
        )

    async def aclose(self) -> None:
        return None
