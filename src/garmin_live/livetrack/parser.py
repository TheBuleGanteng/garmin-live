"""Parse Garmin LiveTrack JSON into typed models.

Required fields that are missing, null or of the wrong type raise `LiveTrackSchemaError` naming
the offending field. Data is never silently dropped or invented; optional fields become `None`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from garmin_live.livetrack.models import SessionInfo, Trackpoint


class LiveTrackSchemaError(ValueError):
    """Garmin's JSON does not have the expected shape. `field` names the problem."""

    def __init__(self, field: str, detail: str) -> None:
        self.field = field
        self.detail = detail
        super().__init__(f"LiveTrack schema error at '{field}': {detail}")


def _require(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj or obj[key] is None:
        raise LiveTrackSchemaError(f"{where}.{key}", "missing")
    return obj[key]


def _number(value: Any, where: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LiveTrackSchemaError(where, f"expected a number, got {type(value).__name__}")
    return float(value)


def _integer(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise LiveTrackSchemaError(where, f"expected an integer, got {type(value).__name__}")
    return int(value)


def parse_iso8601(value: Any, where: str) -> datetime:
    if not isinstance(value, str):
        raise LiveTrackSchemaError(
            where, f"expected an ISO-8601 string, got {type(value).__name__}"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveTrackSchemaError(where, "not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_trackpoint(obj: Any, where: str = "trackPoints[]") -> Trackpoint:
    if not isinstance(obj, dict):
        raise LiveTrackSchemaError(where, f"expected an object, got {type(obj).__name__}")
    position = _require(obj, "position", where)
    if not isinstance(position, dict):
        raise LiveTrackSchemaError(f"{where}.position", "expected an object")
    lat = _number(_require(position, "lat", f"{where}.position"), f"{where}.position.lat")
    lon = _number(_require(position, "lon", f"{where}.position"), f"{where}.position.lon")
    assert lat is not None and lon is not None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise LiveTrackSchemaError(f"{where}.position", "coordinates out of range")
    speed_raw = obj.get("speedMetersPerSec", obj.get("speed"))
    event_types_raw = obj.get("eventTypes", [])
    if not isinstance(event_types_raw, list) or not all(
        isinstance(e, str) for e in event_types_raw
    ):
        raise LiveTrackSchemaError(f"{where}.eventTypes", "expected a list of strings")
    activity_type = obj.get("activityType")
    if activity_type is not None and not isinstance(activity_type, str):
        raise LiveTrackSchemaError(f"{where}.activityType", "expected a string")
    total_distance = _number(
        _require(obj, "totalDistanceMeters", where), f"{where}.totalDistanceMeters"
    )
    total_duration = _number(
        _require(obj, "totalDurationSecs", where), f"{where}.totalDurationSecs"
    )
    assert total_distance is not None and total_duration is not None
    return Trackpoint(
        time=parse_iso8601(_require(obj, "dateTime", where), f"{where}.dateTime"),
        lat=lat,
        lon=lon,
        total_distance_m=total_distance,
        total_duration_s=total_duration,
        speed_mps=_number(speed_raw, f"{where}.speedMetersPerSec", optional=True),
        altitude_m=_number(obj.get("altitude"), f"{where}.altitude", optional=True),
        heart_rate_bpm=_integer(obj.get("heartRateBeatsPerMin"), f"{where}.heartRateBeatsPerMin"),
        cadence_spm=_integer(obj.get("cadenceCyclesPerMin"), f"{where}.cadenceCyclesPerMin"),
        duration_s=_number(obj.get("durationSecs"), f"{where}.durationSecs", optional=True),
        activity_type=activity_type,
        event_types=tuple(event_types_raw),
    )


def parse_trackpoints(payload: Any) -> list[Trackpoint]:
    """Parse a `/track-points/common` response body (already JSON-decoded)."""
    if not isinstance(payload, dict):
        raise LiveTrackSchemaError("$", f"expected an object, got {type(payload).__name__}")
    points = _require(payload, "trackPoints", "$")
    if not isinstance(points, list):
        raise LiveTrackSchemaError("$.trackPoints", "expected a list")
    return [parse_trackpoint(p, f"trackPoints[{i}]") for i, p in enumerate(points)]


def parse_session(payload: Any) -> SessionInfo:
    """Parse a `/api/v2/sessions/<id>` response body (already JSON-decoded)."""
    if not isinstance(payload, dict):
        raise LiveTrackSchemaError("$", f"expected an object, got {type(payload).__name__}")
    session_id = _require(payload, "sessionId", "$")
    if not isinstance(session_id, str):
        raise LiveTrackSchemaError("$.sessionId", "expected a string")
    viewable = _require(payload, "viewable", "$")
    if not isinstance(viewable, bool):
        raise LiveTrackSchemaError("$.viewable", "expected a boolean")
    session_type = payload.get("sessionType")
    if session_type is not None and not isinstance(session_type, str):
        raise LiveTrackSchemaError("$.sessionType", "expected a string")
    active = payload.get("activeMultiDayActivities")
    has_active: bool | None
    if active is None:
        has_active = None
    elif isinstance(active, list):
        has_active = True
    else:
        raise LiveTrackSchemaError("$.activeMultiDayActivities", "expected a list")
    return SessionInfo(
        session_id=session_id.lower(),
        start=parse_iso8601(_require(payload, "start", "$"), "$.start"),
        end=parse_iso8601(_require(payload, "end", "$"), "$.end"),
        viewable=viewable,
        session_type=session_type,
        has_active_activities=has_active,
    )
