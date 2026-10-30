"""Typed representations of LiveTrack data (see docs/LIVETRACK_FINDINGS.md)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

#: A session timeline counts as "current" when its timestamps lie within this many seconds of
#: the wall clock. Replayed historical fixtures fall outside it.
TIMELINE_WINDOW_S = 48 * 3600


def timeline_is_current(ts: datetime, now: datetime) -> bool:
    """True when `ts` lies within two days of `now`, i.e. the session is live rather than a
    replayed historical fixture, so wall-clock rules (end <= now, staleness measured from
    Garmin's own timestamps) apply."""
    return abs((now - ts).total_seconds()) < TIMELINE_WINDOW_S


@dataclass(frozen=True, slots=True)
class Trackpoint:
    """One LiveTrack trackpoint. Optional fields are `None` when Garmin did not send them."""

    time: datetime
    lat: float
    lon: float
    total_distance_m: float
    total_duration_s: float
    speed_mps: float | None = None
    altitude_m: float | None = None
    heart_rate_bpm: int | None = None
    cadence_spm: int | None = None
    duration_s: float | None = None
    activity_type: str | None = None
    event_types: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Session metadata from `/api/v2/sessions/<id>`."""

    session_id: str
    start: datetime
    end: datetime
    viewable: bool
    session_type: str | None = None
    has_active_activities: bool | None = None

    def is_ended(self, now: datetime, *, previous_end: datetime | None = None) -> bool:
        """End-of-session signal (docs/LIVETRACK_FINDINGS.md).

        While live, Garmin reports `end` = `start` + 24 h; when the activity stops it rewrites
        `end` to the actual end time. Two rules, either is enough:

        * relative: `end` moved earlier than the value seen on a previous poll by more than a
          minute (Garmin's `start`/`end` carry ~1 ms jitter between polls);
        * absolute: `end <= now`, applied only when the session timeline is current (its
          `start` lies within two days of `now`), so replayed historical fixtures are not
          declared ended by the wall clock.
        """
        if previous_end is not None and (previous_end - self.end).total_seconds() > 60:
            return True
        return timeline_is_current(self.start, now) and self.end <= now
