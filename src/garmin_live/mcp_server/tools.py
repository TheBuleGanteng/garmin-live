"""MCP tool definitions.

Descriptions are written for Claude: they say what each tool returns and when to call it.
`session_id` is optional everywhere and defaults to the most recent active session. Outputs are
compact JSON, metric units, pace as `m:ss /km`, and never contain coordinates.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from garmin_live.service import ServiceError, TrackingService

_SESSION_ID_DOC = (
    "Optional. The session_id from start_tracking or list_sessions. Omit it to use the most "
    "recent active session (or the newest session if none is active)."
)


def register_tools(server: MCPServer, service: TrackingService) -> None:
    @server.tool(
        name="start_tracking",
        description=(
            "Start following a Garmin LiveTrack share link so its live data can be queried. "
            "Call this once when the athlete gives you a LiveTrack URL of the form "
            "https://livetrack.garmin.com/session/<id>/token/<token> (short gar.mn links must be "
            "expanded first). Returns the new session_id, its status and the first-ingest result. "
            "Only one server-side session is needed per workout; calling it again for the same "
            "link returns the existing session."
        ),
    )
    async def start_tracking(livetrack_url: str, label: str | None = None) -> dict[str, Any]:
        """livetrack_url: full LiveTrack share URL. label: optional short name for the session."""
        try:
            return await service.start_tracking(livetrack_url, label)
        except ServiceError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="list_sessions",
        description=(
            "List tracked sessions with id, label, status (active/ended/expired/stopped/error), "
            "when tracking started, the age of the latest data and ingest errors. Call this to "
            "find a session_id or to check whether anything is being tracked right now."
        ),
    )
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": service.list_sessions()}

    @server.tool(
        name="get_live_status",
        description=(
            "Current snapshot of a session: elapsed and moving time, distance, current (rolling) "
            "pace, average pace, current/average heart rate, cadence, elevation gain/loss, "
            "data_freshness_seconds, a stale flag (no new data for over 5 minutes) and ingest "
            "health. Call this for 'how is the run going right now' questions."
        ),
    )
    async def get_live_status(session_id: str | None = None) -> dict[str, Any]:
        try:
            return service.get_live_status(session_id)
        except ServiceError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="get_splits",
        description=(
            "Per-split breakdown (default 1000 m): time, pace, average heart rate and elevation "
            "gain/loss for each split, with boundaries interpolated between GPS points. The last "
            "split is marked complete=false while in progress. Call this for lap/kilometre "
            "comparisons or to see whether pace is fading."
        ),
    )
    async def get_splits(
        session_id: str | None = None, split_distance_m: int = 1000
    ) -> dict[str, Any]:
        try:
            return service.get_splits(session_id, float(split_distance_m))
        except ServiceError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="get_trend",
        description=(
            "Bucketed time series of one metric over the recent window: metric is 'pace' (s/km), "
            "'hr' (bpm), 'elevation' (m) or 'cadence' (spm); window_minutes (default 10) and "
            "bucket_seconds (default 60) control resolution. Each entry has t (seconds before the "
            "latest point) and value. Call this for drift or trend questions such as 'is my heart "
            "rate creeping up' or 'how has pace changed over the last 10 minutes'."
        ),
    )
    async def get_trend(
        session_id: str | None = None,
        metric: str = "pace",
        window_minutes: int = 10,
        bucket_seconds: int = 60,
    ) -> dict[str, Any]:
        try:
            return service.get_trend(session_id, metric, window_minutes, bucket_seconds)
        except ServiceError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="get_session_summary",
        description=(
            "Whole-session statistics: distance, elapsed and moving time, average pace, average "
            "and max heart rate, elevation gain/loss, heart-rate zone time (only if the server has "
            "ATHLETE_MAX_HR configured; otherwise it says zones are unavailable and why) and "
            "aerobic decoupling (pace:HR efficiency first half vs second half, positive = HR "
            "drifted up). Call this after a workout or for an overall assessment."
        ),
    )
    async def get_session_summary(session_id: str | None = None) -> dict[str, Any]:
        try:
            return service.get_session_summary(session_id)
        except ServiceError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(
        name="stop_tracking",
        description=(
            "Stop following a session and mark it stopped. Data already ingested stays available "
            "to the other tools. Call this only when the athlete asks to stop tracking; sessions "
            "also end by themselves when Garmin signals the end of the activity."
        ),
    )
    async def stop_tracking(session_id: str) -> dict[str, Any]:
        try:
            return await service.stop_tracking(session_id)
        except ServiceError as exc:
            raise ToolError(str(exc)) from None

    # Keep the parameter docs discoverable for humans reading the code.
    for fn in (get_live_status, get_splits, get_trend, get_session_summary):
        fn.__doc__ = _SESSION_ID_DOC
