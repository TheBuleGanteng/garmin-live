"""Tracking service: the one place that creates sessions and answers metric queries.

Both the MCP tools and `selftest.py` go through this class. Only `start_tracking_internal`
(used by selftest and tests with the replay server) may bypass the strict share-URL check; the
MCP tool always calls `start_tracking`, which enforces it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from garmin_live.ingest.poller import Poller
from garmin_live.livetrack.models import Trackpoint
from garmin_live.livetrack.shortlink import DEFAULT_TIMEOUT_S, expand_short_url, is_short_link
from garmin_live.livetrack.urls import LIVETRACK_API_BASE, InvalidLiveTrackUrlError, parse_share_url
from garmin_live.metrics.splits import compute_splits
from garmin_live.metrics.summary import live_status, session_summary
from garmin_live.metrics.trend import METRICS, trend
from garmin_live.store import repo as st
from garmin_live.store.repo import Repo, SessionRow

log = logging.getLogger(__name__)

MAX_LABEL_LEN = 60
SHORT_LINK_EXPAND_FAILED = "Couldn't expand the short link; try the full LiveTrack URL."
Clock = Callable[[], datetime]


class ServiceError(ValueError):
    """User-facing error returned to Claude as a tool error message."""


class TrackingService:
    def __init__(
        self,
        repo: Repo,
        poller: Poller,
        *,
        athlete_max_hr: int | None,
        clock: Clock | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._repo = repo
        self._poller = poller
        self._max_hr = athlete_max_hr
        self._clock = clock or (lambda: datetime.now(UTC))
        # Used only to expand gar.mn short links; injected by tests so they never touch the network.
        self._http = http_client or httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_S, follow_redirects=False
        )

    async def aclose(self) -> None:
        """Release the short-link HTTP client (called from the app's shutdown)."""
        await self._http.aclose()

    # ---- session lifecycle --------------------------------------------------------------
    async def start_tracking(
        self, livetrack_url: str, label: str | None = None
    ) -> dict[str, object]:
        """Accepts the full share URL or a `https://gar.mn/<code>` short link (expanded here)."""
        try:
            url = livetrack_url
            if is_short_link(url):
                url = await expand_short_url(url, self._http)
            share = parse_share_url(url)
        except InvalidLiveTrackUrlError as exc:
            raise ServiceError(str(exc)) from None
        except httpx.HTTPError:
            # Transport-level failure (DNS, TLS, timeout). Never include the URL.
            raise ServiceError(SHORT_LINK_EXPAND_FAILED) from None
        return await self._start(share.session_id, share.token, LIVETRACK_API_BASE, label)

    async def start_tracking_internal(
        self, garmin_session_id: str, token: str, api_base: str, label: str | None = None
    ) -> dict[str, object]:
        """Internal/service API only (selftest, tests): no host check on `api_base`."""
        return await self._start(garmin_session_id, token, api_base, label)

    async def _start(
        self, garmin_session_id: str, token: str, api_base: str, label: str | None
    ) -> dict[str, object]:
        existing = self._repo.find_by_garmin_id(garmin_session_id)
        if existing is not None and existing.is_active:
            return {
                "session_id": existing.id,
                "label": existing.label,
                "status": existing.status,
                "note": "already tracking this LiveTrack session",
            }
        active = self._repo.active_sessions()
        if len(active) >= self._poller.max_active_sessions:
            raise ServiceError(
                f"Already tracking {len(active)} sessions (the maximum). "
                "Stop one with stop_tracking first."
            )
        clean_label = _clean_label(label) or self._clock().strftime("run %Y-%m-%d %H:%M UTC")
        session = self._repo.create_session(
            label=clean_label,
            garmin_session_id=garmin_session_id,
            token=token,
            api_base=api_base,
            poll_interval_s=self._poller.poll_min_seconds,
        )
        self._poller.start_session(session)
        await self._poller.wait_first_poll(session.id)
        refreshed = self._repo.get_session(session.id) or session
        points = self._repo.count_trackpoints(session.id)
        result: dict[str, object] = {
            "session_id": refreshed.id,
            "label": refreshed.label,
            "status": refreshed.status,
            "first_ingest": {
                "points": points,
                "error": refreshed.last_error,
            },
        }
        if refreshed.status == st.ACTIVE and points == 0 and refreshed.last_error is None:
            result["note"] = (
                "connected; no trackpoints yet (they appear once the watch starts sending)"
            )
        return result

    async def stop_tracking(self, session_id: str) -> dict[str, object]:
        session = self._require(session_id)
        if session.is_active:
            await self._poller.stop_session(session.id, "stopped")
        final = self._repo.get_session(session.id) or session
        return {
            "session_id": final.id,
            "label": final.label,
            "status": final.status,
            "end_reason": final.end_reason,
            "points": self._repo.count_trackpoints(final.id),
        }

    # ---- queries ------------------------------------------------------------------------
    def list_sessions(self) -> list[dict[str, object]]:
        """Sessions newest first.

        `last_data_age_s` is measured from Garmin's own timestamp of the newest stored trackpoint
        (the same basis as `get_live_status.data_freshness_seconds`); `last_ingest_age_s` from the
        last time the poller received new points (`sessions.last_point_at`). Only active sessions
        can be `stale`; ended, expired, stopped or errored ones never are.
        """
        now = self._clock()
        out: list[dict[str, object]] = []
        for s in self._repo.list_sessions():
            out.append(
                {
                    "session_id": s.id,
                    "label": s.label,
                    "status": s.status,
                    "end_reason": s.end_reason,
                    "started": s.created_at.isoformat(timespec="seconds"),
                    "last_data_age_s": _age_s(now, self._repo.last_point_time(s.id)),
                    "last_ingest_age_s": _age_s(now, s.last_point_at),
                    "stale": s.stale if s.is_active else False,
                    "points": self._repo.count_trackpoints(s.id),
                    "ingest_error": s.last_error,
                }
            )
        return out

    def get_live_status(self, session_id: str | None) -> dict[str, object]:
        session = self._resolve(session_id)
        points = self._points(session)
        status = live_status(points, now=self._clock())
        status.update(self._session_fields(session))
        return status

    def get_splits(
        self, session_id: str | None, split_distance_m: float = 1000.0
    ) -> dict[str, object]:
        if not (100 <= split_distance_m <= 50_000):
            raise ServiceError("split_distance_m must be between 100 and 50000")
        session = self._resolve(session_id)
        points = self._points(session)
        splits = compute_splits(points, split_distance_m)
        return {
            "session_id": session.id,
            "label": session.label,
            "split_distance_m": split_distance_m,
            "splits": [s.to_dict() for s in splits],
        }

    def get_trend(
        self,
        session_id: str | None,
        metric: str,
        window_minutes: int = 10,
        bucket_seconds: int = 60,
    ) -> dict[str, object]:
        if metric not in METRICS:
            raise ServiceError(f"metric must be one of {', '.join(METRICS)}")
        if not (1 <= window_minutes <= 24 * 60) or not (10 <= bucket_seconds <= 3600):
            raise ServiceError("window_minutes must be 1-1440 and bucket_seconds 10-3600")
        session = self._resolve(session_id)
        points = self._points(session)
        series = trend(points, metric, window_minutes=window_minutes, bucket_seconds=bucket_seconds)
        unit = {"pace": "s/km", "hr": "bpm", "elevation": "m", "cadence": "spm"}[metric]
        return {
            "session_id": session.id,
            "metric": metric,
            "unit": unit,
            "bucket_seconds": bucket_seconds,
            "t_note": "t = seconds before the latest point (0 = now)",
            "series": series,
        }

    def get_session_summary(self, session_id: str | None) -> dict[str, object]:
        session = self._resolve(session_id)
        points = self._points(session)
        summary = session_summary(points, max_hr=self._max_hr)
        summary.update(self._session_fields(session))
        return summary

    # ---- helpers ------------------------------------------------------------------------
    def _resolve(self, session_id: str | None) -> SessionRow:
        if session_id:
            return self._require(session_id)
        session = self._repo.most_recent_session()
        if session is None:
            raise ServiceError(
                "No sessions yet. Call start_tracking with a LiveTrack share URL first."
            )
        return session

    def _require(self, session_id: str) -> SessionRow:
        session = self._repo.get_session(session_id.strip())
        if session is None:
            raise ServiceError(
                f"Unknown session_id {session_id!r}. Call list_sessions to see available sessions."
            )
        return session

    def _points(self, session: SessionRow) -> list[Trackpoint]:
        return self._repo.get_trackpoints(session.id)

    def _session_fields(self, session: SessionRow) -> dict[str, object]:
        return {
            "session_id": session.id,
            "label": session.label,
            "session_status": session.status,
            "end_reason": session.end_reason,
            "ingest": {
                "last_poll_age_s": None
                if session.last_poll_at is None
                else round((self._clock() - session.last_poll_at).total_seconds()),
                "error": session.last_error,
                "error_count": session.error_count,
            },
        }


def _age_s(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, round((now - then).total_seconds()))


def _clean_label(label: str | None) -> str | None:
    if label is None:
        return None
    cleaned = " ".join(label.split())[:MAX_LABEL_LEN]
    return cleaned or None
