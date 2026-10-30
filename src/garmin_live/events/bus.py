"""Event bus: persists ingest events to the `events` table and hands them to the Notifier.

Event types: new_points, split_completed, stale, resumed, session_ended, ingest_error.
Payloads are small JSON objects with counts, distances and reasons — never coordinates or tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from garmin_live.store.repo import Repo

if TYPE_CHECKING:
    from garmin_live.events.notifier import Notifier

log = logging.getLogger(__name__)

NEW_POINTS = "new_points"
SPLIT_COMPLETED = "split_completed"
STALE = "stale"
RESUMED = "resumed"
SESSION_ENDED = "session_ended"
INGEST_ERROR = "ingest_error"
EVENT_TYPES = (NEW_POINTS, SPLIT_COMPLETED, STALE, RESUMED, SESSION_ENDED, INGEST_ERROR)


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    session_id: str
    type: str
    created_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self, repo: Repo, notifier: Notifier) -> None:
        self._repo = repo
        self._notifier = notifier

    async def emit(
        self, session_id: str, type_: str, payload: dict[str, Any] | None = None
    ) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type_!r}")
        row = self._repo.add_event(session_id, type_, payload)
        event = Event(row.id, row.session_id, row.type, row.created_at, row.payload)
        try:
            await self._notifier.notify(event)
        except Exception:  # a notifier failure must never break ingest
            log.exception("notifier failed for event %s", type_)
        return event
