"""Per-session asyncio poller.

For each active session a task repeatedly fetches new trackpoints (incrementally, with `begin`),
dedupes them into the store, emits events, detects staleness, checks the session metadata for
Garmin's end signal, backs off exponentially on errors (cap 5 min), and resumes after restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from garmin_live.events import bus as ev
from garmin_live.events.bus import EventBus
from garmin_live.livetrack.fetch_http import (
    AuthError,
    Fetcher,
    FetchError,
    HttpFetcher,
    SessionExpiredError,
)
from garmin_live.livetrack.models import Trackpoint
from garmin_live.livetrack.parser import LiveTrackSchemaError
from garmin_live.store import repo as st
from garmin_live.store.repo import Repo, SessionRow

log = logging.getLogger(__name__)

STALE_AFTER_S = 300.0
MAX_BACKOFF_S = 300.0
SESSION_CHECK_EVERY_S = 120.0
JITTER_FRACTION = 0.1
FIRST_POLL_TIMEOUT_S = 10.0

FetcherFactory = Callable[[SessionRow], Fetcher]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


def default_fetcher_factory(session: SessionRow) -> Fetcher:
    return HttpFetcher(session.garmin_session_id, session.token, base_url=session.api_base)


class _SessionDone(Exception):  # noqa: N818 - control-flow signal, not an error
    """Raised inside the loop when the session reached a terminal state."""


@dataclass(slots=True)
class _LoopState:
    session: SessionRow
    begin: datetime | None
    last_received_at: datetime | None
    stale_flagged: bool
    last_split: int
    garmin_end: datetime | None = None
    errors: int = 0
    last_session_check: datetime | None = None
    last_poll_inserted: int = -1


class Poller:
    def __init__(
        self,
        repo: Repo,
        bus: EventBus,
        *,
        poll_min_seconds: float,
        max_session_hours: float,
        max_active_sessions: int = 3,
        fetcher_factory: FetcherFactory = default_fetcher_factory,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        split_m: float = 1000.0,
        session_check_s: float = SESSION_CHECK_EVERY_S,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self.poll_min_seconds = poll_min_seconds
        self.max_session_hours = max_session_hours
        self.max_active_sessions = max_active_sessions
        self._factory = fetcher_factory
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleeper or asyncio.sleep
        self._split_m = split_m
        self.session_check_s = session_check_s
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._first_poll: dict[str, asyncio.Event] = {}

    # ---- lifecycle --------------------------------------------------------------------
    async def start(self) -> int:
        """Resume every active session from the store. Returns how many were resumed."""
        resumed = 0
        for session in self._repo.active_sessions():
            self.start_session(session)
            resumed += 1
        if resumed:
            log.info("resumed %d active session(s)", resumed)
        return resumed

    def adopt_new(self) -> int:
        """Start tasks for active sessions in the store that have no running task.

        Covers sessions created out-of-process (the admin CLI) and tasks that died unexpectedly.
        """
        started = 0
        for session in self._repo.active_sessions():
            if not self.is_running(session.id):
                self.start_session(session)
                started += 1
        return started

    async def adopt_loop(self, interval_s: float = 15.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                if self.adopt_new():
                    log.info("adopted new active session(s) from the store")
            except Exception:
                log.exception("adopt loop failed")

    async def stop(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def start_session(self, session: SessionRow) -> None:
        if session.id in self._tasks and not self._tasks[session.id].done():
            return
        self._first_poll[session.id] = asyncio.Event()
        task = asyncio.create_task(self._run(session.id), name=f"poll:{session.id}")
        self._tasks[session.id] = task

    async def wait_first_poll(
        self, session_id: str, max_wait_s: float = FIRST_POLL_TIMEOUT_S
    ) -> bool:
        event = self._first_poll.get(session_id)
        if event is None:
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), max_wait_s)
        return event.is_set()

    async def stop_session(self, session_id: str, reason: str = "stopped") -> None:
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        session = self._repo.get_session(session_id)
        if session is not None and session.is_active:
            self._repo.end_session(session_id, st.STOPPED, reason, self._clock())
            await self._bus.emit(session_id, ev.SESSION_ENDED, {"reason": reason})

    def is_running(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        return task is not None and not task.done()

    # ---- the loop ---------------------------------------------------------------------
    async def _run(self, session_id: str) -> None:
        session = self._repo.get_session(session_id)
        if session is None:
            return
        fetcher = self._factory(session)
        state = _LoopState(
            session=session,
            begin=self._repo.last_point_time(session_id),
            last_received_at=session.last_point_at,
            stale_flagged=session.stale,
            last_split=int(self._last_total_distance(session_id) // self._split_m),
            garmin_end=session.garmin_end,
        )
        try:
            while True:
                interval = await self._poll_once(fetcher, state)
                self._mark_first_poll(session_id)
                jitter = random.uniform(-JITTER_FRACTION, JITTER_FRACTION)  # noqa: S311
                await self._sleep(interval * (1 + jitter))
        except _SessionDone:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poller for %s crashed", session_id)
            await self._end(session_id, st.ERROR, "crash", self._clock())
        finally:
            self._mark_first_poll(session_id)
            await fetcher.aclose()

    def _mark_first_poll(self, session_id: str) -> None:
        first = self._first_poll.get(session_id)
        if first is not None:
            first.set()

    async def _poll_once(self, fetcher: Fetcher, state: _LoopState) -> float:
        """One poll cycle. Returns the seconds to sleep before the next one."""
        sid = state.session.id
        now = self._clock()
        if now - state.session.created_at >= timedelta(hours=self.max_session_hours):
            await self._end(sid, st.ENDED, "max_hours", now)
            raise _SessionDone
        try:
            if state.last_session_check is None:
                await self._check_session(fetcher, state, now)  # validates the session first
            points = await fetcher.fetch_trackpoints(state.begin)
            await self._ingest(points, state, now)
            if await self._check_session(fetcher, state, now):
                await self._end(sid, st.ENDED, "garmin_end", now)
                raise _SessionDone
        except SessionExpiredError as exc:
            log.info("session %s expired (%s)", sid, exc.status)
            await self._end(sid, st.EXPIRED, "expired", now, detail=f"http {exc.status}")
            raise _SessionDone from None
        except (FetchError, LiveTrackSchemaError) as exc:
            return await self._handle_error(exc, state, now)
        state.errors = 0
        return self.poll_min_seconds

    async def _check_session(self, fetcher: Fetcher, state: _LoopState, now: datetime) -> bool:
        """Fetch session metadata when due and report Garmin's end signal.

        Due on the first poll, every `session_check_s`, and eagerly after a poll that brought no
        new points (the likely moment the activity has ended). Returns True when ended.
        """
        last = state.last_session_check
        if last is not None:
            elapsed = (now - last).total_seconds()
            due = elapsed >= self.session_check_s or (
                state.last_poll_inserted == 0 and elapsed >= self.poll_min_seconds * 0.9
            )
            if not due:
                return False
        info = await fetcher.fetch_session()
        state.last_session_check = now
        previous_end = state.garmin_end
        state.garmin_end = info.end
        self._repo.update_session(state.session.id, garmin_start=info.start, garmin_end=info.end)
        if not info.viewable:
            await self._end(state.session.id, st.EXPIRED, "not_viewable", now)
            raise _SessionDone
        return info.is_ended(now, previous_end=previous_end)

    async def _ingest(self, points: list[Trackpoint], state: _LoopState, now: datetime) -> None:
        sid = state.session.id
        inserted = self._repo.insert_trackpoints(sid, points)
        state.last_poll_inserted = inserted
        fields: dict[str, object] = {"last_poll_at": now, "last_error": None, "error_count": 0}
        if inserted:
            state.begin = max(p.time for p in points)
            state.last_received_at = now
            fields["last_point_at"] = now
            if points[-1].activity_type:
                fields["activity_type"] = points[-1].activity_type
            if state.stale_flagged:
                state.stale_flagged = False
                fields["stale"] = False
                await self._bus.emit(sid, ev.RESUMED, {"after_s": round(STALE_AFTER_S)})
            total_m = max(p.total_distance_m for p in points)
            await self._bus.emit(
                sid,
                ev.NEW_POINTS,
                {
                    "count": inserted,
                    "total_points": self._repo.count_trackpoints(sid),
                    "distance_m": round(total_m),
                },
            )
            split_now = int(total_m // self._split_m)
            for split in range(state.last_split + 1, split_now + 1):
                payload = {"split": split, "distance_m": round(split * self._split_m)}
                await self._bus.emit(sid, ev.SPLIT_COMPLETED, payload)
            state.last_split = max(state.last_split, split_now)
        elif (
            not state.stale_flagged
            and state.last_received_at is not None
            and (now - state.last_received_at).total_seconds() > STALE_AFTER_S
        ):
            state.stale_flagged = True
            fields["stale"] = True
            since = round((now - state.last_received_at).total_seconds())
            await self._bus.emit(sid, ev.STALE, {"since_s": since})
        self._repo.update_session(sid, **fields)

    async def _handle_error(
        self, exc: FetchError | LiveTrackSchemaError, state: _LoopState, now: datetime
    ) -> float:
        state.errors += 1
        if isinstance(exc, LiveTrackSchemaError):
            kind, detail, status = "schema", exc.field, None
        else:
            kind = "auth" if isinstance(exc, AuthError) else "fetch"
            detail, status = f"http {exc.status}", exc.status
        interval = min(MAX_BACKOFF_S, self.poll_min_seconds * (2.0 ** min(state.errors, 8)))
        self._repo.update_session(
            state.session.id,
            last_poll_at=now,
            last_error=f"{kind}: {detail}",
            error_count=state.errors,
        )
        await self._bus.emit(
            state.session.id,
            ev.INGEST_ERROR,
            {
                "kind": kind,
                "status": status,
                "detail": detail,
                "attempt": state.errors,
                "retry_in_s": round(interval),
            },
        )
        log.warning(
            "ingest error on %s (%s %s); retry in %.0fs", state.session.id, kind, detail, interval
        )
        return interval

    def _last_total_distance(self, session_id: str) -> float:
        points = self._repo.get_trackpoints(session_id)
        return points[-1].total_distance_m if points else 0.0

    async def _end(
        self, session_id: str, status: str, reason: str, now: datetime, detail: str | None = None
    ) -> None:
        self._repo.end_session(session_id, status, reason, now)
        payload: dict[str, object] = {
            "reason": reason,
            "status": status,
            "points": self._repo.count_trackpoints(session_id),
        }
        if detail:
            payload["detail"] = detail
        await self._bus.emit(session_id, ev.SESSION_ENDED, payload)
        log.info("session %s ended: %s", session_id, reason)
