"""Bucketed time series for pace / hr / elevation / cadence."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from garmin_live.livetrack.models import Trackpoint
from garmin_live.metrics.pace import average_pace_s_per_km, format_pace

METRICS = ("pace", "hr", "elevation", "cadence")


def trend(
    points: Sequence[Trackpoint],
    metric: str,
    *,
    window_minutes: int = 10,
    bucket_seconds: int = 60,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Return one entry per bucket covering the last `window_minutes` up to the latest point.

    Each entry: `t` (seconds before the latest point, negative), `value`, and for pace also
    `pace` formatted. Buckets with no samples are omitted so Claude is not shown invented values.
    """
    if metric not in METRICS:
        raise ValueError(f"metric must be one of {', '.join(METRICS)}")
    if not points or bucket_seconds <= 0 or window_minutes <= 0:
        return []
    end = now or points[-1].time
    start = end - timedelta(minutes=window_minutes)
    out: list[dict[str, object]] = []
    bucket_start = start
    while bucket_start < end:
        bucket_end = bucket_start + timedelta(seconds=bucket_seconds)
        last_bucket = bucket_end >= end
        inside = [
            p
            for p in points
            if bucket_start <= p.time < bucket_end or (last_bucket and p.time == end)
        ]
        value = _bucket_value(inside, metric)
        if value is not None:
            entry: dict[str, object] = {
                "t": round((bucket_start - end).total_seconds()),
                "value": value,
            }
            if metric == "pace":
                entry["pace"] = format_pace(value)
            out.append(entry)
        bucket_start = bucket_end
    return out


def _bucket_value(inside: Sequence[Trackpoint], metric: str) -> float | None:
    if metric == "pace":
        return _rounded(average_pace_s_per_km(inside, moving=False)) if len(inside) >= 2 else None
    if metric == "hr":
        vals = [p.heart_rate_bpm for p in inside if p.heart_rate_bpm is not None]
        return _rounded(sum(vals) / len(vals)) if vals else None
    if metric == "elevation":
        vals_e = [p.altitude_m for p in inside if p.altitude_m is not None]
        return _rounded(sum(vals_e) / len(vals_e)) if vals_e else None
    vals_c = [p.cadence_spm for p in inside if p.cadence_spm is not None]
    return _rounded(sum(vals_c) / len(vals_c)) if vals_c else None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
