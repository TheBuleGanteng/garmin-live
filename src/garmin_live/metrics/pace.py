"""Pace, speed and moving time."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise

from garmin_live.livetrack.models import Trackpoint

# A segment slower than this (m/s) counts as paused: ~0.5 m/s = 33 min/km.
PAUSE_SPEED_MPS = 0.5
# A gap longer than this between points is treated as a pause regardless of distance.
PAUSE_GAP_S = 60.0


def pace_s_per_km(speed_mps: float | None) -> float | None:
    """Seconds per kilometre for a speed, or None when stopped/unknown."""
    if speed_mps is None or speed_mps <= 0.05:
        return None
    return 1000.0 / speed_mps


def format_pace(seconds_per_km: float | None) -> str | None:
    """Render pace as `m:ss /km` (e.g. `5:07 /km`); None stays None."""
    if seconds_per_km is None or seconds_per_km <= 0 or seconds_per_km > 3600 * 3:
        return None
    total = round(seconds_per_km)
    return f"{total // 60}:{total % 60:02d} /km"


def elapsed_time_s(points: Sequence[Trackpoint]) -> float:
    if len(points) < 2:
        return 0.0
    return (points[-1].time - points[0].time).total_seconds()


def distance_m(points: Sequence[Trackpoint]) -> float:
    """Distance covered, from Garmin's cumulative distance field."""
    if not points:
        return 0.0
    return max(0.0, points[-1].total_distance_m - points[0].total_distance_m)


def moving_time_s(
    points: Sequence[Trackpoint],
    *,
    pause_speed_mps: float = PAUSE_SPEED_MPS,
    pause_gap_s: float = PAUSE_GAP_S,
) -> float:
    """Time spent moving: sum of segment durations whose average speed is above the pause
    threshold and whose duration is not an obvious gap."""
    total = 0.0
    for prev, cur in pairwise(points):
        dt = (cur.time - prev.time).total_seconds()
        if dt <= 0 or dt > pause_gap_s:
            continue
        dd = cur.total_distance_m - prev.total_distance_m
        if dd / dt >= pause_speed_mps:
            total += dt
    return total


def average_pace_s_per_km(points: Sequence[Trackpoint], *, moving: bool = True) -> float | None:
    """Average pace over the whole list, by moving time (default) or elapsed time."""
    dist = distance_m(points)
    if dist < 1.0:
        return None
    time_s = moving_time_s(points) if moving else elapsed_time_s(points)
    if time_s <= 0:
        return None
    return time_s / (dist / 1000.0)


def rolling_pace_by_time(
    points: Sequence[Trackpoint], window_s: float, at: datetime | None = None
) -> float | None:
    """Pace over the last `window_s` seconds ending at `at` (default: last point)."""
    if len(points) < 2:
        return None
    end_time = at or points[-1].time
    start_time = end_time - timedelta(seconds=window_s)
    window = [p for p in points if start_time <= p.time <= end_time]
    if len(window) < 2:
        return None
    return average_pace_s_per_km(window, moving=False)


def rolling_pace_by_distance(points: Sequence[Trackpoint], window_m: float) -> float | None:
    """Pace over the last `window_m` metres (interpolated at the window start)."""
    if len(points) < 2:
        return None
    end = points[-1]
    target = end.total_distance_m - window_m
    if target < points[0].total_distance_m:
        return None
    # find the segment that crosses `target` and interpolate its time
    for prev, cur in pairwise(points):
        if prev.total_distance_m <= target <= cur.total_distance_m:
            span = cur.total_distance_m - prev.total_distance_m
            frac = 0.0 if span <= 0 else (target - prev.total_distance_m) / span
            t_start = prev.time + (cur.time - prev.time) * frac
            seconds = (end.time - t_start).total_seconds()
            return None if seconds <= 0 else seconds / (window_m / 1000.0)
    return None


def current_speed_mps(points: Sequence[Trackpoint]) -> float | None:
    """Garmin's reported speed on the latest point, if present."""
    return points[-1].speed_mps if points else None
