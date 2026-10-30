"""Heart-rate metrics: average, zone time (needs ATHLETE_MAX_HR) and aerobic decoupling."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

from garmin_live.livetrack.models import Trackpoint
from garmin_live.metrics.pace import distance_m, elapsed_time_s

# Zone lower bounds as a fraction of max HR (five classic zones).
ZONE_BOUNDS = (0.50, 0.60, 0.70, 0.80, 0.90)
MAX_SAMPLE_GAP_S = 60.0


def average_hr(points: Sequence[Trackpoint]) -> float | None:
    """Time-weighted average HR (each sample holds until the next point), or None if no HR."""
    weighted = 0.0
    total_t = 0.0
    for prev, cur in pairwise(points):
        if prev.heart_rate_bpm is None:
            continue
        dt = (cur.time - prev.time).total_seconds()
        if dt <= 0 or dt > MAX_SAMPLE_GAP_S:
            continue
        weighted += prev.heart_rate_bpm * dt
        total_t += dt
    if total_t <= 0:
        samples = [p.heart_rate_bpm for p in points if p.heart_rate_bpm is not None]
        return round(sum(samples) / len(samples), 1) if samples else None
    return round(weighted / total_t, 1)


def hr_zone(bpm: int, max_hr: int) -> int:
    """Zone 1-5 for a heart rate; 0 means below zone 1."""
    ratio = bpm / max_hr
    zone = 0
    for i, bound in enumerate(ZONE_BOUNDS, start=1):
        if ratio >= bound:
            zone = i
    return zone


def hr_zone_time_s(points: Sequence[Trackpoint], max_hr: int) -> dict[str, float]:
    """Seconds spent in each zone (keys `z0`..`z5`), each sample holding until the next point."""
    times = {f"z{i}": 0.0 for i in range(6)}
    for prev, cur in pairwise(points):
        if prev.heart_rate_bpm is None:
            continue
        dt = (cur.time - prev.time).total_seconds()
        if dt <= 0 or dt > MAX_SAMPLE_GAP_S:
            continue
        times[f"z{hr_zone(prev.heart_rate_bpm, max_hr)}"] += dt
    return {k: round(v) for k, v in times.items()}


def aerobic_decoupling_pct(points: Sequence[Trackpoint]) -> float | None:
    """Pace:HR decoupling between the first and second half (by elapsed time).

    Efficiency factor EF = speed (m/s) / HR. Decoupling = (EF_first - EF_second) / EF_first * 100.
    Positive values mean HR drifted up relative to pace. None when HR or distance is missing.
    """
    if len(points) < 4:
        return None
    mid_time = points[0].time + (points[-1].time - points[0].time) / 2
    first = [p for p in points if p.time <= mid_time]
    second = [p for p in points if p.time >= mid_time]
    ef1 = _efficiency_factor(first)
    ef2 = _efficiency_factor(second)
    if ef1 is None or ef2 is None or ef1 <= 0:
        return None
    return round((ef1 - ef2) / ef1 * 100.0, 1)


def _efficiency_factor(points: Sequence[Trackpoint]) -> float | None:
    if len(points) < 2:
        return None
    dist = distance_m(points)
    secs = elapsed_time_s(points)
    hr = average_hr(points)
    if dist <= 0 or secs <= 0 or hr is None or hr <= 0:
        return None
    return (dist / secs) / hr
