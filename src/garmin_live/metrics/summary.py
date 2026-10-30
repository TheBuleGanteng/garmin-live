"""Compact status and summary dictionaries built from trackpoints (no coordinates)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from garmin_live.livetrack.models import Trackpoint
from garmin_live.metrics.elevation import gain_loss
from garmin_live.metrics.hr import aerobic_decoupling_pct, average_hr, hr_zone_time_s
from garmin_live.metrics.pace import (
    average_pace_s_per_km,
    elapsed_time_s,
    format_pace,
    moving_time_s,
    pace_s_per_km,
    rolling_pace_by_time,
)

STALE_AFTER_S = 300.0
ROLLING_WINDOW_S = 120.0


def live_status(points: Sequence[Trackpoint], *, now: datetime) -> dict[str, object]:
    """Current state: elapsed / moving time, distance, current and average pace, HR, cadence,
    elevation and data freshness. Fields Garmin did not send are `null` with a reason."""
    if not points:
        return {
            "points": 0,
            "note": "no trackpoints received yet",
        }
    last = points[-1]
    gain, loss = gain_loss([p.altitude_m for p in points])
    freshness = max(0.0, (now - last.time).total_seconds())
    current_pace = pace_s_per_km(last.speed_mps)
    rolling = rolling_pace_by_time(points, ROLLING_WINDOW_S)
    hrs = [p.heart_rate_bpm for p in points if p.heart_rate_bpm is not None]
    cads = [p.cadence_spm for p in points if p.cadence_spm is not None]
    return {
        "points": len(points),
        "elapsed_s": round(last.total_duration_s),
        "moving_s": round(moving_time_s(points)),
        "distance_m": round(last.total_distance_m),
        "current_pace": format_pace(current_pace),
        "rolling_pace_2min": format_pace(rolling),
        "avg_pace": format_pace(average_pace_s_per_km(points)),
        "current_hr": last.heart_rate_bpm,
        "avg_hr": average_hr(points) if hrs else None,
        "hr_note": None if hrs else "heart rate not provided by Garmin for this session",
        "cadence_spm": last.cadence_spm,
        "cadence_note": None if cads else "cadence not provided by Garmin for this session",
        "elev_gain_m": gain,
        "elev_loss_m": loss,
        "activity_type": last.activity_type,
        "data_freshness_seconds": round(freshness),
        "stale": freshness > STALE_AFTER_S,
    }


def session_summary(points: Sequence[Trackpoint], *, max_hr: int | None) -> dict[str, object]:
    """Full-session statistics including HR zones (if max HR known) and aerobic decoupling."""
    if not points:
        return {"points": 0, "note": "no trackpoints received"}
    gain, loss = gain_loss([p.altitude_m for p in points])
    hrs = [p.heart_rate_bpm for p in points if p.heart_rate_bpm is not None]
    summary: dict[str, object] = {
        "points": len(points),
        "distance_m": round(points[-1].total_distance_m),
        "elapsed_s": round(points[-1].total_duration_s),
        "points_span_s": round(elapsed_time_s(points)),
        "moving_s": round(moving_time_s(points)),
        "avg_pace_moving": format_pace(average_pace_s_per_km(points, moving=True)),
        "avg_pace_elapsed": format_pace(average_pace_s_per_km(points, moving=False)),
        "avg_hr": average_hr(points) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
        "elev_gain_m": gain,
        "elev_loss_m": loss,
        "activity_type": points[-1].activity_type,
        "aerobic_decoupling_pct": aerobic_decoupling_pct(points) if hrs else None,
    }
    if not hrs:
        summary["hr_note"] = "heart rate not provided by Garmin for this session"
    if max_hr is None:
        summary["hr_zones"] = None
        summary["hr_zones_note"] = (
            "unavailable: set ATHLETE_MAX_HR in the server configuration to enable zones"
        )
    elif not hrs:
        summary["hr_zones"] = None
        summary["hr_zones_note"] = "unavailable: no heart-rate data in this session"
    else:
        summary["hr_zones"] = hr_zone_time_s(points, max_hr)
        summary["hr_zones_note"] = (
            f"seconds per zone, zones as % of max HR {max_hr}: "
            "z1 50-60, z2 60-70, z3 70-80, z4 80-90, z5 90+"
        )
    return summary
