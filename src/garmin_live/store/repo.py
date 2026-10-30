"""Repository layer: typed access to sessions, trackpoints and events."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_live.livetrack.models import Trackpoint
from garmin_live.store.db import Database, utc_now_iso

ACTIVE = "active"
ENDED = "ended"
EXPIRED = "expired"
STOPPED = "stopped"
ERROR = "error"
TERMINAL_STATUSES = (ENDED, EXPIRED, STOPPED, ERROR)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat(timespec="milliseconds")


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


@dataclass(slots=True)
class SessionRow:
    id: str
    label: str
    garmin_session_id: str
    token: str
    api_base: str
    status: str
    end_reason: str | None
    created_at: datetime
    garmin_start: datetime | None
    garmin_end: datetime | None
    ended_at: datetime | None
    last_poll_at: datetime | None
    last_point_at: datetime | None
    last_error: str | None
    error_count: int
    poll_interval_s: float
    stale: bool
    activity_type: str | None

    @property
    def is_active(self) -> bool:
        return self.status == ACTIVE

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SessionRow:
        return cls(
            id=row["id"],
            label=row["label"],
            garmin_session_id=row["garmin_session_id"],
            token=row["token"],
            api_base=row["api_base"],
            status=row["status"],
            end_reason=row["end_reason"],
            created_at=datetime.fromisoformat(row["created_at"]),
            garmin_start=_dt(row["garmin_start"]),
            garmin_end=_dt(row["garmin_end"]),
            ended_at=_dt(row["ended_at"]),
            last_poll_at=_dt(row["last_poll_at"]),
            last_point_at=_dt(row["last_point_at"]),
            last_error=row["last_error"],
            error_count=int(row["error_count"]),
            poll_interval_s=float(row["poll_interval_s"]),
            stale=bool(row["stale"]),
            activity_type=row["activity_type"],
        )


@dataclass(slots=True)
class EventRow:
    id: int
    session_id: str
    type: str
    payload: dict[str, Any]
    created_at: datetime


def new_session_id() -> str:
    return "s_" + secrets.token_hex(4)


def _row_to_point(r: sqlite3.Row) -> Trackpoint:
    return Trackpoint(
        time=datetime.fromisoformat(r["ts"]),
        lat=r["lat"],
        lon=r["lon"],
        total_distance_m=r["total_distance_m"],
        total_duration_s=r["total_duration_s"],
        speed_mps=r["speed_mps"],
        altitude_m=r["altitude_m"],
        heart_rate_bpm=r["heart_rate_bpm"],
        cadence_spm=r["cadence_spm"],
        duration_s=r["duration_s"],
        activity_type=r["activity_type"],
        event_types=tuple(json.loads(r["event_types"])),
    )


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---- sessions ---------------------------------------------------------------------
    def create_session(
        self,
        *,
        label: str,
        garmin_session_id: str,
        token: str,
        api_base: str,
        poll_interval_s: float,
    ) -> SessionRow:
        sid = new_session_id()
        with self.db.tx() as conn:
            conn.execute(
                """INSERT INTO sessions (id, label, garmin_session_id, token, api_base, status,
                                         created_at, poll_interval_s)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sid,
                    label,
                    garmin_session_id,
                    token,
                    api_base,
                    ACTIVE,
                    utc_now_iso(),
                    poll_interval_s,
                ),
            )
        session = self.get_session(sid)
        assert session is not None
        return session

    def get_session(self, session_id: str) -> SessionRow | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else SessionRow.from_row(row)

    def find_by_garmin_id(self, garmin_session_id: str) -> SessionRow | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE garmin_session_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (garmin_session_id,),
            ).fetchone()
        return None if row is None else SessionRow.from_row(row)

    def list_sessions(self, limit: int = 50) -> list[SessionRow]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [SessionRow.from_row(r) for r in rows]

    def active_sessions(self) -> list[SessionRow]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY created_at ASC", (ACTIVE,)
            ).fetchall()
        return [SessionRow.from_row(r) for r in rows]

    def active_session_labels(self, exclude: list[str] | None = None) -> list[str]:
        """Labels of active sessions, used by the deploy pipeline's live-workout guard."""
        skip = set(exclude or [])
        return [s.label for s in self.active_sessions() if s.label not in skip]

    def most_recent_session(self, *, prefer_active: bool = True) -> SessionRow | None:
        """Default target for tools: the most recently created active session, else the newest."""
        if prefer_active:
            active = self.active_sessions()
            if active:
                return max(active, key=lambda s: s.created_at)
        sessions = self.list_sessions(limit=1)
        return sessions[0] if sessions else None

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        converted: dict[str, Any] = {}
        for key, value in fields.items():
            if isinstance(value, datetime):
                converted[key] = _iso(value)
            elif isinstance(value, bool):
                converted[key] = int(value)
            else:
                converted[key] = value
        assignments = ", ".join(f"{key} = ?" for key in converted)
        # Keys are code-controlled column names; values are bound parameters.
        query = f"UPDATE sessions SET {assignments} WHERE id = ?"  # noqa: S608
        with self.db.tx() as conn:
            conn.execute(query, (*converted.values(), session_id))

    def end_session(
        self, session_id: str, status: str, reason: str, ended_at: datetime | None = None
    ) -> None:
        self.update_session(
            session_id,
            status=status,
            end_reason=reason,
            ended_at=ended_at or datetime.now(UTC),
            stale=False,
        )

    # ---- trackpoints ------------------------------------------------------------------
    def insert_trackpoints(self, session_id: str, points: list[Trackpoint]) -> int:
        """Insert points, ignoring duplicates by timestamp. Returns the number inserted."""
        if not points:
            return 0
        rows = [
            (
                session_id,
                _iso(p.time),
                p.lat,
                p.lon,
                p.total_distance_m,
                p.total_duration_s,
                p.speed_mps,
                p.altitude_m,
                p.heart_rate_bpm,
                p.cadence_spm,
                p.duration_s,
                p.activity_type,
                json.dumps(list(p.event_types)),
            )
            for p in points
        ]
        with self.db.tx() as conn:
            before = conn.total_changes
            conn.executemany(
                """INSERT OR IGNORE INTO trackpoints
                   (session_id, ts, lat, lon, total_distance_m, total_duration_s, speed_mps,
                    altitude_m, heart_rate_bpm, cadence_spm, duration_s, activity_type, event_types)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            inserted = conn.total_changes - before
        return inserted

    def get_trackpoints(self, session_id: str, since: datetime | None = None) -> list[Trackpoint]:
        query = "SELECT * FROM trackpoints WHERE session_id = ?"
        params: list[Any] = [session_id]
        if since is not None:
            query += " AND ts > ?"
            params.append(_iso(since))
        query += " ORDER BY ts ASC"
        with self.db.read() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_point(r) for r in rows]

    def last_point_time(self, session_id: str) -> datetime | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT MAX(ts) AS ts FROM trackpoints WHERE session_id = ?", (session_id,)
            ).fetchone()
        return _dt(row["ts"]) if row and row["ts"] else None

    def count_trackpoints(self, session_id: str) -> int:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trackpoints WHERE session_id = ?", (session_id,)
            ).fetchone()
        return int(row["n"])

    # ---- events -----------------------------------------------------------------------
    def add_event(
        self, session_id: str, type_: str, payload: dict[str, Any] | None = None
    ) -> EventRow:
        created = utc_now_iso()
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO events (session_id, type, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, type_, json.dumps(payload or {}, separators=(",", ":")), created),
            )
            event_id = int(cur.lastrowid or 0)
        return EventRow(event_id, session_id, type_, payload or {}, datetime.fromisoformat(created))

    def list_events(self, session_id: str, since_id: int = 0, limit: int = 500) -> list[EventRow]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (session_id, since_id, limit),
            ).fetchall()
        return [
            EventRow(
                int(r["id"]),
                r["session_id"],
                r["type"],
                json.loads(r["payload"]),
                datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    # ---- retention --------------------------------------------------------------------
    def purge_older_than(self, days: int, now: datetime | None = None) -> dict[str, int]:
        """Delete sessions (and their points and events) created before the cutoff.

        Active sessions are never purged. Returns the number of rows removed per table.
        """
        cutoff = _iso((now or datetime.now(UTC)) - timedelta(days=days))
        with self.db.tx() as conn:
            ids = [
                (r["id"],)
                for r in conn.execute(
                    "SELECT id FROM sessions WHERE created_at < ? AND status != ?",
                    (cutoff, ACTIVE),
                ).fetchall()
            ]
            if not ids:
                return {"sessions": 0, "trackpoints": 0, "events": 0}
            points = conn.executemany("DELETE FROM trackpoints WHERE session_id = ?", ids).rowcount
            events = conn.executemany("DELETE FROM events WHERE session_id = ?", ids).rowcount
            sessions = conn.executemany("DELETE FROM sessions WHERE id = ?", ids).rowcount
        return {"sessions": sessions, "trackpoints": points, "events": events}


def active_session_labels(db_path: str, exclude: list[str] | None = None) -> list[str]:
    """Convenience for operators (used by the deploy pipeline): labels of active sessions."""
    db = Database(db_path)
    try:
        return Repo(db).active_session_labels(exclude)
    finally:
        db.close()
