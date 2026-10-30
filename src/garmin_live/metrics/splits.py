"""Distance splits with boundaries interpolated between trackpoints."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from garmin_live.livetrack.models import Trackpoint
from garmin_live.metrics.elevation import gain_loss
from garmin_live.metrics.hr import average_hr
from garmin_live.metrics.pace import format_pace


@dataclass(frozen=True, slots=True)
class Split:
    index: int  # 1-based
    start_m: float
    end_m: float  # for a partial final split this is the actual distance reached
    start_time: datetime
    end_time: datetime
    avg_hr: float | None
    gain_m: float
    loss_m: float
    complete: bool

    @property
    def duration_s(self) -> float:
        return (self.end_time - self.start_time).total_seconds()

    @property
    def distance_m(self) -> float:
        return self.end_m - self.start_m

    @property
    def pace_s_per_km(self) -> float | None:
        if self.distance_m <= 0 or self.duration_s <= 0:
            return None
        return self.duration_s / (self.distance_m / 1000.0)

    def to_dict(self) -> dict[str, object]:
        return {
            "split": self.index,
            "distance_m": round(self.distance_m),
            "time_s": round(self.duration_s),
            "pace": format_pace(self.pace_s_per_km),
            "avg_hr": self.avg_hr,
            "elev_gain_m": self.gain_m,
            "elev_loss_m": self.loss_m,
            "complete": self.complete,
        }


def _time_at_distance(prev: Trackpoint, cur: Trackpoint, target_m: float) -> datetime:
    span = cur.total_distance_m - prev.total_distance_m
    if span <= 0:
        return cur.time
    frac = min(1.0, max(0.0, (target_m - prev.total_distance_m) / span))
    return prev.time + (cur.time - prev.time) * frac


def compute_splits(
    points: Sequence[Trackpoint], split_m: float = 1000.0, *, include_partial: bool = True
) -> list[Split]:
    """Return consecutive splits of `split_m` metres, interpolating the boundary times.

    HR and elevation for a split are computed from the points that fall inside it (plus the
    boundary neighbours for elevation, so a climb spanning a boundary is split proportionally
    only in time, not altitude — altitude is taken at sample points).
    """
    if len(points) < 2 or split_m <= 0:
        return []
    base = points[0].total_distance_m
    final_m = points[-1].total_distance_m
    splits: list[Split] = []
    index = 1
    start_m = base
    start_time = points[0].time
    seg_i = 0
    while True:
        end_m = start_m + split_m
        complete = end_m <= final_m
        if not complete:
            if not include_partial or final_m - start_m < 1.0:
                break
            end_m = final_m
            end_time = points[-1].time
        else:
            # advance to the segment that crosses end_m
            while seg_i < len(points) - 1 and points[seg_i + 1].total_distance_m < end_m:
                seg_i += 1
            if seg_i >= len(points) - 1:
                break
            end_time = _time_at_distance(points[seg_i], points[seg_i + 1], end_m)
        inside = [p for p in points if start_time <= p.time <= end_time]
        gain, loss = gain_loss([p.altitude_m for p in inside])
        splits.append(
            Split(
                index=index,
                start_m=start_m,
                end_m=end_m,
                start_time=start_time,
                end_time=end_time,
                avg_hr=average_hr(inside)
                if len(inside) >= 2
                else (inside[0].heart_rate_bpm if inside else None),
                gain_m=gain,
                loss_m=loss,
                complete=complete,
            )
        )
        if not complete:
            break
        index += 1
        start_m = end_m
        start_time = end_time
    return splits
