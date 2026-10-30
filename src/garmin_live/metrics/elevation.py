"""Elevation gain/loss with hysteresis so GPS noise does not inflate the totals."""

from __future__ import annotations

from collections.abc import Sequence

HYSTERESIS_M = 3.0


def gain_loss(
    altitudes: Sequence[float | None], hysteresis_m: float = HYSTERESIS_M
) -> tuple[float, float]:
    """Return (gain, loss) in metres.

    A change is only counted once the altitude has moved more than `hysteresis_m` away from the
    last accepted reference; the reference then moves to the new altitude. Small oscillations
    around a level are ignored. `None` samples are skipped.
    """
    gain = 0.0
    loss = 0.0
    reference: float | None = None
    for alt in altitudes:
        if alt is None:
            continue
        if reference is None:
            reference = alt
            continue
        delta = alt - reference
        if delta >= hysteresis_m:
            gain += delta
            reference = alt
        elif delta <= -hysteresis_m:
            loss += -delta
            reference = alt
    return round(gain, 1), round(loss, 1)
