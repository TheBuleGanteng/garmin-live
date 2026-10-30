"""Store: migrations, dedupe, session lifecycle, events, retention purge, schema guard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from garmin_live.store import repo as st
from garmin_live.store.db import Database, SchemaTooNewError, current_version, list_migrations
from garmin_live.store.repo import Repo, active_session_labels
from tests.conftest import make_point


def test_migrations_apply_idempotently(tmp_path: Path) -> None:
    path = tmp_path / "m.db"
    db1 = Database(path)
    assert db1.schema_version == len(list_migrations()) >= 1
    assert current_version(db1.conn) == db1.schema_version
    db1.close()
    db2 = Database(path)  # second start: nothing to apply, no error
    assert db2.schema_version == db1.schema_version
    assert db2.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db2.close()


def test_refuses_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    db = Database(path)
    db.conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (999, 'x')")
    db.close()
    with pytest.raises(SchemaTooNewError):
        Database(path)


def test_session_lifecycle_and_dedupe(repo: Repo) -> None:
    s = repo.create_session(
        label="morning run",
        garmin_session_id="gsid-1",
        token="tok-not-real",
        api_base="http://127.0.0.1:1",
        poll_interval_s=20,
    )
    assert s.id.startswith("s_") and s.is_active and s.stale is False
    assert repo.find_by_garmin_id("gsid-1") is not None
    assert repo.most_recent_session() is not None and repo.most_recent_session().id == s.id  # type: ignore[union-attr]
    pts = [make_point(0, 0), make_point(10, 30), make_point(20, 60)]
    assert repo.insert_trackpoints(s.id, pts) == 3
    assert repo.insert_trackpoints(s.id, [*pts, make_point(30, 90)]) == 1  # dedupe by timestamp
    assert repo.count_trackpoints(s.id) == 4
    assert repo.last_point_time(s.id) == pts[0].time + timedelta(seconds=30)
    since = repo.get_trackpoints(s.id, since=pts[1].time)
    assert [p.total_distance_m for p in since] == [60.0, 90.0]
    got = repo.get_trackpoints(s.id)[0]
    assert got.heart_rate_bpm == 140 and got.event_types == () and got.activity_type == "RUNNING"
    repo.update_session(s.id, stale=True, last_error="fetch: http 503", error_count=2)
    again = repo.get_session(s.id)
    assert again is not None and again.stale is True and again.error_count == 2
    repo.end_session(s.id, st.ENDED, "garmin_end", datetime.now(UTC))
    ended = repo.get_session(s.id)
    assert ended is not None and ended.status == st.ENDED and ended.end_reason == "garmin_end"
    assert ended.stale is False and ended.ended_at is not None
    assert repo.active_sessions() == []
    assert repo.most_recent_session() is not None  # falls back to the newest non-active
    assert repo.insert_trackpoints(s.id, []) == 0
    assert repo.get_session("s_nope") is None


def test_active_session_labels_helper(repo: Repo, db: Database) -> None:
    repo.create_session(
        label="selftest", garmin_session_id="g1", token="t", api_base="b", poll_interval_s=20
    )
    repo.create_session(
        label="tempo", garmin_session_id="g2", token="t", api_base="b", poll_interval_s=20
    )
    assert repo.active_session_labels(exclude=["selftest"]) == ["tempo"]
    assert sorted(active_session_labels(db.path)) == ["selftest", "tempo"]


def test_events_roundtrip(repo: Repo) -> None:
    s = repo.create_session(
        label="x", garmin_session_id="g", token="t", api_base="b", poll_interval_s=20
    )
    e1 = repo.add_event(s.id, "new_points", {"count": 2})
    e2 = repo.add_event(s.id, "stale", {"since_s": 310})
    events = repo.list_events(s.id)
    assert [e.type for e in events] == ["new_points", "stale"]
    assert events[0].payload == {"count": 2} and e1.id < e2.id
    assert [e.id for e in repo.list_events(s.id, since_id=e1.id)] == [e2.id]


def test_retention_purges_old_non_active_sessions(repo: Repo) -> None:
    old = repo.create_session(
        label="old", garmin_session_id="g1", token="t", api_base="b", poll_interval_s=20
    )
    keep_active = repo.create_session(
        label="old-active", garmin_session_id="g2", token="t", api_base="b", poll_interval_s=20
    )
    fresh = repo.create_session(
        label="fresh", garmin_session_id="g3", token="t", api_base="b", poll_interval_s=20
    )
    for sid in (old.id, keep_active.id, fresh.id):
        repo.insert_trackpoints(sid, [make_point(0, 0), make_point(10, 10)])
        repo.add_event(sid, "new_points", {"count": 2})
    long_ago = datetime.now(UTC) - timedelta(days=40)
    repo.update_session(old.id, created_at=long_ago, status=st.ENDED)
    repo.update_session(keep_active.id, created_at=long_ago)  # still active → never purged
    removed = repo.purge_older_than(30)
    assert removed == {"sessions": 1, "trackpoints": 2, "events": 1}
    assert repo.get_session(old.id) is None
    assert repo.count_trackpoints(old.id) == 0 and repo.list_events(old.id) == []
    assert (
        repo.get_session(keep_active.id) is not None and repo.count_trackpoints(keep_active.id) == 2
    )
    assert repo.get_session(fresh.id) is not None
    assert repo.purge_older_than(30) == {"sessions": 0, "trackpoints": 0, "events": 0}
