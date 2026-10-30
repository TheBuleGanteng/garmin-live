"""Parser vs every fixture, plus schema-drift detection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from garmin_live.livetrack.parser import (
    LiveTrackSchemaError,
    parse_iso8601,
    parse_session,
    parse_trackpoint,
    parse_trackpoints,
)
from tests.conftest import (
    fixture_names,
    fixture_sessions,
    fixture_trackpoints,
    load_fixture_records,
)


@pytest.mark.parametrize("name", fixture_names())
def test_every_fixture_response_parses(name: str) -> None:
    n_points = 0
    n_sessions = 0
    for rec in load_fixture_records(name):
        url = rec["request"]["url"]
        if rec["response"]["status"] != 200 or not rec.get("body_text"):
            continue
        body = json.loads(rec["body_text"])
        if "/track-points/" in url:
            n_points += len(parse_trackpoints(body))
        elif "/api/v2/sessions/" in url:
            info = parse_session(body)
            assert info.viewable is True
            n_sessions += 1
    assert n_points > 0 and n_sessions > 0


def test_run1_points_are_monotonic_and_typed() -> None:
    points = fixture_trackpoints("run1")
    assert len(points) == 72
    assert all(a.time < b.time for a, b in pairwise(points))
    assert all(a.total_distance_m <= b.total_distance_m for a, b in pairwise(points))
    first = points[0]
    assert first.time.tzinfo is UTC
    assert isinstance(first.heart_rate_bpm, int)
    assert isinstance(first.cadence_spm, int)
    assert first.activity_type == "RUNNING"
    assert first.altitude_m is not None


def test_run1_session_bodies_include_active_and_ended() -> None:
    sessions = fixture_sessions("run1")
    spans = {round((s.end - s.start).total_seconds() / 3600, 1) for s in sessions}
    assert 24.0 in spans  # live placeholder
    assert any(span < 1 for span in spans)  # the ended body
    assert any(s.has_active_activities is None for s in sessions)  # key absent when ended


def _good_point() -> dict[str, object]:
    return {
        "dateTime": "2020-01-01T06:06:17.216Z",
        "reportedTime": "2020-01-01T06:05:56.488Z",
        "position": {"lat": -33.0, "lon": -40.0},
        "speed": 1.558,
        "altitude": 100,
        "totalDurationSecs": 732.4,
        "speedMetersPerSec": 1.558,
        "totalDistanceMeters": 610.6,
        "activityType": "RUNNING",
        "distanceMeters": 610.6,
        "durationSecs": 732.4,
        "elevationSource": "GPS",
        "eventTypes": [],
        "heartRateBeatsPerMin": 142,
        "cadenceCyclesPerMin": 57,
    }


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda p: p.pop("position"), "trackPoints[].position"),
        (lambda p: p["position"].pop("lon"), "trackPoints[].position.lon"),
        (lambda p: p.update(dateTime=None), "trackPoints[].dateTime"),
        (lambda p: p.update(dateTime="yesterday"), "trackPoints[].dateTime"),
        (lambda p: p.update(totalDistanceMeters="far"), "trackPoints[].totalDistanceMeters"),
        (lambda p: p.pop("totalDurationSecs"), "trackPoints[].totalDurationSecs"),
        (lambda p: p.update(altitude="high"), "trackPoints[].altitude"),
        (lambda p: p.update(heartRateBeatsPerMin=True), "trackPoints[].heartRateBeatsPerMin"),
        (lambda p: p.update(eventTypes="LAP"), "trackPoints[].eventTypes"),
        (lambda p: p["position"].update(lat=123.0), "trackPoints[].position"),
    ],
)
def test_schema_drift_raises_naming_the_field(mutate, field: str) -> None:  # type: ignore[no-untyped-def]
    point = _good_point()
    mutate(point)
    with pytest.raises(LiveTrackSchemaError) as info:
        parse_trackpoint(point)
    assert info.value.field == field
    assert field in str(info.value)


def test_optional_fields_become_none_not_invented() -> None:
    point = _good_point()
    for key in (
        "altitude",
        "heartRateBeatsPerMin",
        "cadenceCyclesPerMin",
        "speed",
        "speedMetersPerSec",
        "durationSecs",
    ):
        point.pop(key)
    parsed = parse_trackpoint(point)
    assert parsed.altitude_m is None
    assert parsed.heart_rate_bpm is None
    assert parsed.cadence_spm is None
    assert parsed.speed_mps is None
    assert parsed.duration_s is None


def test_trackpoints_envelope_errors() -> None:
    with pytest.raises(LiveTrackSchemaError) as info:
        parse_trackpoints({"points": []})
    assert info.value.field == "$.trackPoints"
    with pytest.raises(LiveTrackSchemaError):
        parse_trackpoints([])
    with pytest.raises(LiveTrackSchemaError):
        parse_trackpoints({"trackPoints": {"a": 1}})


def test_session_parse_and_drift() -> None:
    body = {
        "sessionId": "ABCDEFAB-0000-4000-8000-000000000000",
        "start": "2020-01-01T06:00:00.000Z",
        "end": "2020-01-02T06:00:00.000Z",
        "viewable": True,
        "sessionType": "FITNESS_TRACKING",
        "activeMultiDayActivities": [],
    }
    info = parse_session(body)
    assert info.session_id == body["sessionId"].lower()  # type: ignore[union-attr]
    assert info.has_active_activities is True
    assert info.is_ended(datetime(2020, 1, 1, 7, tzinfo=UTC)) is False
    body.pop("activeMultiDayActivities")
    assert parse_session(body).has_active_activities is None
    body["viewable"] = "yes"
    with pytest.raises(LiveTrackSchemaError) as err:
        parse_session(body)
    assert err.value.field == "$.viewable"
    with pytest.raises(LiveTrackSchemaError):
        parse_session({"sessionId": "x", "start": "2020-01-01T06:00:00Z", "viewable": True})


def test_parse_iso8601_variants() -> None:
    assert parse_iso8601("2020-01-01T06:00:00Z", "x") == datetime(2020, 1, 1, 6, tzinfo=UTC)
    assert parse_iso8601("2020-01-01T07:00:00+01:00", "x") == datetime(2020, 1, 1, 6, tzinfo=UTC)
    assert parse_iso8601("2020-01-01T06:00:00", "x").tzinfo is UTC
    with pytest.raises(LiveTrackSchemaError):
        parse_iso8601(1577858400, "x")
