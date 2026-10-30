"""Notifier protocol: the push-readiness seam.

v1 ships only `NoopNotifier`. A future push/alerting build implements `Notifier` (e.g. a webhook
or a chat message) and is plugged into the `EventBus`; nothing else needs to change.
"""

from __future__ import annotations

from typing import Protocol

from garmin_live.events.bus import Event


class Notifier(Protocol):
    async def notify(self, event: Event) -> None:
        """Deliver one event. Must never raise; the bus logs but does not retry."""
        ...


class NoopNotifier:
    """Counts events and does nothing else."""

    def __init__(self) -> None:
        self.delivered = 0
        self.last_event: Event | None = None

    async def notify(self, event: Event) -> None:
        self.delivered += 1
        self.last_event = event
