"""Metrics against hand-computed answers on synthetic tracks, plus sanity on the real fixture."""

from __future__ import annotations

from datetime import timedelta

import pytest

from garmin_live.livetrack.models import Trackpoint
from garmin_live.metrics.elevation import gain_loss
from garmin_live.metrics.hr import aerobic_decoupling_pct, average_hr, hr_zone, hr_zone_time_s
from garmin_live.metrics.pace import (
    average_pace_s_per_km,
    format_pace,
    moving_time_s,
    pace_s_per_km,
    rolling_pace_by_distance,
    rolling_pace_by_time,
)
from garmin_live.metrics.splits import compute_splits
from garmin_live.metrics.summary import live_status, session_summary
from garmin_live.metrics.trend import trend
from tests.conftest import T0, make_point


def test_format_pace() -> None:
    assert format_pace(300) == "5:00 /km"
    assert format_pace(307.4) == "5:07 /km"
    assert format_pace(59.6) == "1:00 /km"
    assert format_pace(None) is None
    assert format_pace(0) is None
    assert format_pace(4 * 3600) is None  # absurdly slow → treated as stopped


def test_pace_from_speed() -> None:
    assert pace_s_per_km(3.3333) == pytest.approx(300.0, rel=1e-3)
    assert pace_s_per_km(0.0) is None
    assert pace_s_per_km(None) is None


def test_moving_time_with_pause_detection() -> None:
    # 0-10 s: 30 m (moving); 10-20 s: 0 m (paused); 20-30 s: 30 m (moving);
    # 30-120 s: a gap longer than 60 s (ignored)
    pts = [
        make_point(0, 0),
        make_point(10, 30),
        make_point(20, 30),
        make_point(30, 60),
        make_point(120, 300),
    ]
    assert moving_time_s(pts) == 20.0
    # average pace by moving time: 300 m in 20 s → 66.7 s/km; by elapsed: 300 m in 120 s → 400 s/km
    assert average_pace_s_per_km(pts, moving=True) == pytest.approx(1000 / 300 * 20, rel=1e-6)
    assert average_pace_s_per_km(pts, moving=False) == pytest.approx(400.0)


def test_rolling_pace_by_time_and_distance() -> None:
    # constant 3 m/s for 100 s, then 2 m/s for 100 s
    pts = [make_point(t, 3.0 * t) for t in range(0, 101, 10)]
    pts += [make_point(100 + t, 300 + 2.0 * t) for t in range(10, 101, 10)]
    # last 60 s: 120 m at 2 m/s → 500 s/km
    assert rolling_pace_by_time(pts, 60) == pytest.approx(500.0)
    # last 200 m: 200 m at 2 m/s → 100 s → 500 s/km
    assert rolling_pace_by_distance(pts, 200) == pytest.approx(500.0)
    # last 300 m spans the boundary: 200 m @2 m/s (100 s) + 100 m @3 m/s (33.3 s)
    # = 133.3 s → 444.4 s/km
    assert rolling_pace_by_distance(pts, 300) == pytest.approx(444.44, rel=1e-3)
    assert rolling_pace_by_distance(pts, 10_000) is None
    assert rolling_pace_by_time([pts[0]], 60) is None


def test_splits_interpolated_at_boundaries() -> None:
    # 0 m @0 s, 600 m @300 s, 1200 m @600 s (constant 2 m/s = 500 s/km)
    pts = [make_point(0, 0, hr=120), make_point(300, 600, hr=140), make_point(600, 1200, hr=160)]
    splits = compute_splits(pts, 1000)
    assert len(splits) == 2
    first, second = splits
    assert first.complete is True
    assert first.start_m == 0 and first.end_m == 1000
    assert first.end_time == T0 + timedelta(seconds=500)  # interpolated 400 m into the 2nd segment
    assert first.duration_s == pytest.approx(500.0)
    assert first.pace_s_per_km == pytest.approx(500.0)
    assert second.complete is False
    assert second.distance_m == pytest.approx(200.0)
    assert second.duration_s == pytest.approx(100.0)
    d = first.to_dict()
    assert d["pace"] == "8:20 /km" and d["time_s"] == 500 and d["split"] == 1


def test_splits_edge_cases() -> None:
    assert compute_splits([], 1000) == []
    assert compute_splits([make_point(0, 0)], 1000) == []
    pts = [make_point(0, 0), make_point(100, 150)]
    assert compute_splits(pts, 1000, include_partial=False) == []
    assert len(compute_splits(pts, 1000)) == 1


def test_elevation_hysteresis_ignores_noise() -> None:
    # +1/+2 wobbles are ignored; 100→104 counts as +4; 104→100 counts as -4
    gain, loss = gain_loss([100, 101, 102, 100, 104, 100, None, 101])
    assert (gain, loss) == (4.0, 4.0)
    assert gain_loss([]) == (0.0, 0.0)
    assert gain_loss([None, None]) == (0.0, 0.0)
    # a steady climb of 0.5 m per sample still accumulates once the threshold is crossed
    gain, loss = gain_loss([100 + 0.5 * i for i in range(13)])  # 100 → 106
    assert gain == 6.0 and loss == 0.0


def test_hr_average_zones_and_decoupling() -> None:
    assert hr_zone(100, 200) == 1  # 50%
    assert hr_zone(99, 200) == 0
    assert hr_zone(180, 200) == 5
    assert hr_zone(159, 200) == 3  # 79.5%
    # 10 s at 120 (60% → z2), 20 s at 150 (75% → z3), last sample holds nothing
    pts = [make_point(0, 0, hr=120), make_point(10, 30, hr=150), make_point(30, 90, hr=170)]
    zones = hr_zone_time_s(pts, 200)
    assert zones["z2"] == 10 and zones["z3"] == 20 and zones["z4"] == 0
    assert average_hr(pts) == pytest.approx((120 * 10 + 150 * 20) / 30, rel=1e-3)
    assert average_hr([make_point(0, 0, hr=None)]) is None
    # constant speed and HR → zero decoupling
    flat = [make_point(t, 3.0 * t, hr=150) for t in range(0, 601, 30)]
    assert aerobic_decoupling_pct(flat) == 0.0
    # HR rises 150→165 in the second half at the same speed → EF drops 10% → +~10
    drift = [make_point(t, 3.0 * t, hr=150 if t <= 300 else 165) for t in range(0, 601, 30)]
    assert 8.0 <= (aerobic_decoupling_pct(drift) or 0) <= 10.0
    assert aerobic_decoupling_pct(flat[:3]) is None


def test_trend_buckets_skip_empty_and_format_pace() -> None:
    pts = [make_point(t, 2.0 * t, hr=140 + t // 60) for t in range(0, 601, 10)]
    hr = trend(pts, "hr", window_minutes=10, bucket_seconds=120)
    assert [e["t"] for e in hr] == [-600, -480, -360, -240, -120]
    assert hr[0]["value"] < hr[-1]["value"]
    pace = trend(pts, "pace", window_minutes=5, bucket_seconds=60)
    assert all(e["pace"] == "8:20 /km" for e in pace)  # 2 m/s
    assert trend(pts, "cadence", window_minutes=1, bucket_seconds=30)[0]["value"] == 170
    assert trend([], "hr") == []
    with pytest.raises(ValueError, match="metric"):
        trend(pts, "power")
    # buckets without samples are omitted rather than invented
    sparse = [make_point(0, 0), make_point(590, 1000)]
    assert len(trend(sparse, "elevation", window_minutes=10, bucket_seconds=60)) == 2


def test_live_status_and_summary_on_fixture(run1_points: list[Trackpoint]) -> None:
    now = run1_points[-1].time + timedelta(seconds=30)
    status = live_status(run1_points, now=now)
    assert status["points"] == 72
    assert status["distance_m"] == 1022
    assert status["data_freshness_seconds"] == 30
    assert status["stale"] is False
    assert status["current_pace"] is not None and status["avg_pace"] is not None
    assert "lat" not in status and "lon" not in status and "position" not in status
    stale = live_status(run1_points, now=now + timedelta(minutes=10))
    assert stale["stale"] is True

    summary = session_summary(run1_points, max_hr=None)
    assert summary["hr_zones"] is None and "ATHLETE_MAX_HR" in str(summary["hr_zones_note"])
    assert summary["distance_m"] == 1022
    with_zones = session_summary(run1_points, max_hr=185)
    zones = with_zones["hr_zones"]
    assert isinstance(zones, dict) and sum(zones.values()) > 0
    assert live_status([], now=now)["points"] == 0
    assert session_summary([], max_hr=185)["points"] == 0


def test_summary_without_hr_explains_why() -> None:
    pts = [make_point(t, 2.0 * t, hr=None) for t in range(0, 61, 10)]
    summary = session_summary(pts, max_hr=185)
    assert summary["avg_hr"] is None
    assert summary["hr_zones"] is None
    assert "no heart-rate data" in str(summary["hr_zones_note"])
    status = live_status(pts, now=pts[-1].time)
    assert status["avg_hr"] is None and "not provided" in str(status["hr_note"])
