"""Poller behaviour with a scripted fetcher and a fake clock: incremental ingest, dedupe,
stale/resumed (measured from Garmin's timestamps for live sessions, ingest time for historical
replays), backoff on 5xx/429, Garmin end signal (including a session that is already over on
the first poll), expiry, max hours, restart-resume, events and the NoopNotifier."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from garmin_live.events import bus as ev
from garmin_live.events.bus import EventBus
from garmin_live.events.notifier import NoopNotifier
from garmin_live.ingest.poller import Poller
from garmin_live.livetrack.fetch_http import FetchError, SessionExpiredError
from garmin_live.livetrack.parser import LiveTrackSchemaError
from garmin_live.store import repo as st
from garmin_live.store.repo import Repo, SessionRow
from tests.conftest import (
    T0,
    FakeClock,
    FakeFetcher,
    make_point,
    make_session_info,
    make_sleeper,
    wait_until,
)


def _poller(
    repo: Repo, fetcher: FakeFetcher, clock: FakeClock, notifier: NoopNotifier, **kw: object
) -> Poller:
    sleeps, sleeper = make_sleeper(clock)
    poller = Poller(
        repo,
        EventBus(repo, notifier),
        poll_min_seconds=20,
        max_session_hours=12,
        fetcher_factory=lambda _s: fetcher,
        clock=clock,
        sleeper=sleeper,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )
    poller.sleeps = sleeps  # type: ignore[attr-defined]
    return poller


def _session(repo: Repo, clock: FakeClock) -> SessionRow:
    s = repo.create_session(
        label="t", garmin_session_id="g", token="tok", api_base="http://x", poll_interval_s=20
    )
    repo.update_session(s.id, created_at=clock())
    return repo.get_session(s.id) or s


def _types(repo: Repo, sid: str) -> list[str]:
    return [e.type for e in repo.list_events(sid)]


async def test_incremental_ingest_dedupe_and_events(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(
        batches=[
            [make_point(0, 0), make_point(10, 60)],
            [make_point(10, 60), make_point(20, 120)],  # overlap → deduped
            [make_point(t, 6.0 * t) for t in range(30, 200, 10)],  # crosses 1000 m at t=167
        ]
    )
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await poller.wait_first_poll(s.id)
    assert await wait_until(lambda: repo.count_trackpoints(s.id) == 20)
    await poller.stop()
    assert repo.count_trackpoints(s.id) == 20
    types = _types(repo, s.id)
    assert types.count(ev.NEW_POINTS) == 3
    assert types.count(ev.SPLIT_COMPLETED) == 1
    split_event = next(e for e in repo.list_events(s.id) if e.type == ev.SPLIT_COMPLETED)
    assert split_event.payload == {"split": 1, "distance_m": 1000}
    assert notifier.delivered == len(types)
    assert notifier.last_event is not None and notifier.last_event.session_id == s.id
    # begin advances to the last ingested point (incremental polling)
    assert fetcher.begins[0] is None
    assert fetcher.begins[2] == T0 + timedelta(seconds=20)
    session = repo.get_session(s.id)
    assert session is not None and session.is_active and session.last_point_at is not None
    assert session.activity_type == "RUNNING" and session.error_count == 0
    # payloads never carry coordinates
    for e in repo.list_events(s.id):
        assert not {"lat", "lon", "position"} & set(e.payload)


async def test_gap_emits_stale_then_resumed(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(
        batches=[[make_point(0, 0)]] + [[] for _ in range(20)] + [[make_point(500, 100)]]
    )
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: ev.RESUMED in _types(repo, s.id))
    await poller.stop()
    types = _types(repo, s.id)
    resumed_at = types.index(ev.RESUMED)
    # exactly one stale before the resume, and new_points right after it
    assert types[:resumed_at].count(ev.STALE) == 1
    assert types[resumed_at + 1] == ev.NEW_POINTS
    stale = next(e for e in repo.list_events(s.id) if e.type == ev.STALE)
    assert stale.payload["since_s"] >= 300
    # the fetcher runs dry again afterwards, so the clock may reach a second stale period;
    # the stored flag must agree with the latest event either way
    final = repo.get_session(s.id)
    assert final is not None and final.stale is (types[-1] == ev.STALE)


async def test_backoff_on_5xx_and_429_then_recovery(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(
        batches=[
            FetchError(503, "down"),
            FetchError(503, "down"),
            FetchError(429, "slow"),
            [make_point(0, 0)],
        ]
    )
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: repo.count_trackpoints(s.id) == 1)
    await poller.stop()
    errors = [e for e in repo.list_events(s.id) if e.type == ev.INGEST_ERROR]
    assert [e.payload["status"] for e in errors] == [503, 503, 429]
    assert [e.payload["attempt"] for e in errors] == [1, 2, 3]
    retry = [e.payload["retry_in_s"] for e in errors]
    assert retry == [40, 80, 160]  # 20 s * 2^n
    sleeps = poller.sleeps  # type: ignore[attr-defined]
    assert sleeps[0] > 30 and sleeps[1] > 60 and sleeps[2] > 120
    final = repo.get_session(s.id)
    assert final is not None and final.error_count == 0 and final.last_error is None


async def test_backoff_is_capped_at_five_minutes(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(batches=[FetchError(500, "x") for _ in range(500)])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: len(_types(repo, s.id)) >= 10)
    snapshot = repo.get_session(s.id)
    await poller.stop()
    errors = [e for e in repo.list_events(s.id) if e.type == ev.INGEST_ERROR]
    assert max(e.payload["retry_in_s"] for e in errors) == 300
    assert snapshot is not None and snapshot.is_active and snapshot.error_count >= 9
    assert snapshot.last_error == "fetch: http 500"


async def test_schema_error_is_reported_not_fatal(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(
        batches=[LiveTrackSchemaError("trackPoints[0].position", "missing"), [make_point(0, 0)]]
    )
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: repo.count_trackpoints(s.id) == 1)
    await poller.stop()
    err = next(e for e in repo.list_events(s.id) if e.type == ev.INGEST_ERROR)
    assert err.payload["kind"] == "schema" and err.payload["detail"] == "trackPoints[0].position"


async def test_garmin_end_signal_ends_session(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    active = make_session_info(T0)
    ended = make_session_info(T0, ended_at=T0 + timedelta(minutes=15))
    fetcher = FakeFetcher(
        batches=[[make_point(0, 0), make_point(10, 30)], [], []],
        sessions=[active, active, ended],
    )
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: not (repo.get_session(s.id) or s).is_active)
    final = repo.get_session(s.id)
    assert final is not None and final.status == st.ENDED and final.end_reason == "garmin_end"
    assert final.garmin_end == ended.end and final.garmin_start == T0
    done = next(e for e in repo.list_events(s.id) if e.type == ev.SESSION_ENDED)
    assert done.payload["reason"] == "garmin_end" and done.payload["points"] == 2
    assert not poller.is_running(s.id)
    assert fetcher.closed
    await poller.stop()


async def test_one_millisecond_jitter_does_not_end_session(
    repo: Repo, notifier: NoopNotifier
) -> None:
    clock = FakeClock(T0)
    a = make_session_info(T0)
    jitter = make_session_info(T0, ended_at=a.end - timedelta(milliseconds=1))
    fetcher = FakeFetcher(batches=[[make_point(0, 0)], [], [], []], sessions=[a, jitter, a, jitter])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: fetcher.session_calls >= 4)
    await poller.stop()
    final = repo.get_session(s.id)
    assert final is not None and final.status == st.ACTIVE


async def test_not_viewable_and_expiry(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(batches=[SessionExpiredError(404, "gone")])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: not (repo.get_session(s.id) or s).is_active)
    final = repo.get_session(s.id)
    assert final is not None and final.status == st.EXPIRED and final.end_reason == "expired"
    done = next(e for e in repo.list_events(s.id) if e.type == ev.SESSION_ENDED)
    assert done.payload["detail"] == "http 404"

    hidden = dataclasses.replace(make_session_info(T0), viewable=False)
    fetcher2 = FakeFetcher(sessions=[hidden])
    poller2 = _poller(repo, fetcher2, clock, notifier)
    s2 = _session(repo, clock)
    poller2.start_session(s2)
    assert await wait_until(lambda: not (repo.get_session(s2.id) or s2).is_active)
    final2 = repo.get_session(s2.id)
    assert (
        final2 is not None and final2.status == st.EXPIRED and final2.end_reason == "not_viewable"
    )
    await poller.stop()
    await poller2.stop()


async def test_max_session_hours(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(batches=[[make_point(0, 0)]] + [[] for _ in range(5000)])
    poller = _poller(repo, fetcher, clock, notifier)
    poller.max_session_hours = 1
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: not (repo.get_session(s.id) or s).is_active, timeout=20)
    final = repo.get_session(s.id)
    assert final is not None and final.status == st.ENDED and final.end_reason == "max_hours"
    await poller.stop()


async def test_restart_resumes_active_sessions_from_last_point(
    repo: Repo, notifier: NoopNotifier
) -> None:
    clock = FakeClock(T0)
    s = _session(repo, clock)
    repo.insert_trackpoints(s.id, [make_point(0, 0), make_point(10, 30), make_point(20, 1100)])
    ended_session = _session(repo, clock)
    repo.end_session(ended_session.id, st.ENDED, "garmin_end")
    fetcher = FakeFetcher(batches=[[make_point(30, 2100)]])
    poller = _poller(repo, fetcher, clock, notifier)
    resumed = await poller.start()
    assert resumed == 1  # only the active one
    assert await wait_until(lambda: repo.count_trackpoints(s.id) == 4)
    await poller.stop()
    assert fetcher.begins[0] == T0 + timedelta(seconds=20)  # continues after the stored last point
    types = _types(repo, s.id)
    assert (
        types.count(ev.SPLIT_COMPLETED) == 1
    )  # split 1 was already passed before restart; only 2 fires
    split = next(e for e in repo.list_events(s.id) if e.type == ev.SPLIT_COMPLETED)
    assert split.payload["split"] == 2


async def test_stop_session_marks_stopped(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(batches=[[make_point(0, 0)]] + [[] for _ in range(100)])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    await poller.wait_first_poll(s.id)
    await poller.stop_session(s.id)
    final = repo.get_session(s.id)
    assert final is not None and final.status == st.STOPPED and final.end_reason == "stopped"
    assert _types(repo, s.id)[-1] == ev.SESSION_ENDED
    assert not poller.is_running(s.id)
    await poller.stop_session("s_missing")  # no-op
    assert await poller.wait_first_poll("s_missing") is False


async def test_adopt_new_starts_tasks_for_sessions_created_elsewhere(
    repo: Repo, notifier: NoopNotifier
) -> None:
    clock = FakeClock(T0)
    fetcher = FakeFetcher(batches=[[make_point(0, 0)]])
    poller = _poller(repo, fetcher, clock, notifier)
    assert poller.adopt_new() == 0
    s = _session(repo, clock)  # e.g. inserted by the admin CLI
    # loopback-bound sessions belong to their creating process (selftest) and are skipped
    repo.create_session(
        label="selftest",
        garmin_session_id="lo",
        token="t",
        api_base="http://127.0.0.1:9",
        poll_interval_s=1,
    )
    assert poller.adopt_new() == 1
    assert poller.adopt_new() == 0  # already running
    assert await wait_until(lambda: repo.count_trackpoints(s.id) == 1)
    await poller.stop()


# ---------------------------------------------------------------- FIX 1: freshness semantics
async def test_stale_is_measured_from_garmin_time_for_live_sessions(
    repo: Repo, notifier: NoopNotifier
) -> None:
    # The only point is already 290 s old (by Garmin's clock) when it is ingested, e.g. the watch
    # buffered it. The next poll, 20 s later, finds nothing new: by Garmin's time the data is now
    # ~310 s old → stale, even though ingest happened only 20 s ago.
    clock = FakeClock(T0 + timedelta(seconds=290))
    fetcher = FakeFetcher(batches=[[make_point(0, 0)], [], []])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: ev.STALE in _types(repo, s.id))
    await poller.stop()
    stale = next(e for e in repo.list_events(s.id) if e.type == ev.STALE)
    assert 300 < stale.payload["since_s"] <= 330  # measured from the point's timestamp (T0)
    # flagged by the very next poll (events: new_points, stale), not after 300 s of ingest gap
    assert _types(repo, s.id)[:2] == [ev.NEW_POINTS, ev.STALE]
    final = repo.get_session(s.id)
    assert final is not None and final.stale is True and final.is_active


async def test_stale_for_historical_timeline_uses_ingest_time(
    repo: Repo, notifier: NoopNotifier
) -> None:
    # Replayed fixtures carry historical timestamps. A point from 2020 ingested "now" must not be
    # stale on the next poll; staleness then follows the ingest gap, as for a live session.
    clock = FakeClock(T0)
    old_point = make_point(0, 0, start=datetime(2020, 1, 1, 6, 0, 0, tzinfo=UTC))
    fetcher = FakeFetcher(batches=[[old_point]] + [[] for _ in range(60)])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await wait_until(lambda: ev.STALE in _types(repo, s.id))
    await poller.stop()
    stale = next(e for e in repo.list_events(s.id) if e.type == ev.STALE)
    assert 300 < stale.payload["since_s"] < 400  # ~300 s without ingest, not six years
    assert len(fetcher.begins) >= 14  # about fifteen 20 s polls, not the second one


# ---------------------------------------------------------------- FIX 2: ended on first poll
async def test_already_ended_session_ends_on_first_poll_with_all_points(
    repo: Repo, notifier: NoopNotifier
) -> None:
    # Tracking a link to a workout that finished hours ago: Garmin reports it ended on the very
    # first session check. The poller must still ingest the full trackpoint list once, then end
    # with garmin_end, on that same poll, without flagging anything stale.
    clock = FakeClock(T0 + timedelta(hours=3))
    ended = make_session_info(T0, ended_at=T0 + timedelta(minutes=15))
    points = [make_point(t, 3.0 * t) for t in range(0, 900, 10)]  # 90 points, 2.67 km
    fetcher = FakeFetcher(batches=[points], sessions=[ended])
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await poller.wait_first_poll(s.id)
    assert await wait_until(lambda: not (repo.get_session(s.id) or s).is_active)
    final = repo.get_session(s.id)
    assert final is not None and final.status == st.ENDED and final.end_reason == "garmin_end"
    assert final.garmin_end == ended.end and final.stale is False
    assert repo.count_trackpoints(s.id) == len(points)
    assert fetcher.session_calls == 1 and len(fetcher.begins) == 1  # exactly one poll
    assert poller.sleeps == []  # type: ignore[attr-defined]  # ended before the first sleep
    types = _types(repo, s.id)
    assert types[0] == ev.NEW_POINTS and types[-1] == ev.SESSION_ENDED
    assert types.count(ev.SPLIT_COMPLETED) == 2 and ev.STALE not in types
    done = next(e for e in repo.list_events(s.id) if e.type == ev.SESSION_ENDED)
    assert done.payload == {"reason": "garmin_end", "status": st.ENDED, "points": len(points)}
    assert not poller.is_running(s.id) and fetcher.closed


async def test_live_session_is_unaffected_by_first_poll_end_check(
    repo: Repo, notifier: NoopNotifier
) -> None:
    clock = FakeClock(T0 + timedelta(minutes=5))
    active = make_session_info(T0)  # Garmin's live placeholder: end = start + 24 h
    fetcher = FakeFetcher(
        batches=[[make_point(0, 0), make_point(300, 900)], [make_point(320, 960)], []],
        sessions=[active],
    )
    poller = _poller(repo, fetcher, clock, notifier)
    s = _session(repo, clock)
    poller.start_session(s)
    assert await poller.wait_first_poll(s.id)
    assert await wait_until(lambda: len(fetcher.begins) >= 3)
    await poller.stop()
    final = repo.get_session(s.id)
    assert final is not None and final.is_active and final.end_reason is None
    assert repo.count_trackpoints(s.id) == 3 and fetcher.session_calls >= 1
    assert ev.SESSION_ENDED not in _types(repo, s.id)
