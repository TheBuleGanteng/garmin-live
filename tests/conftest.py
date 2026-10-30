"""Shared test fixtures: scrubbed-fixture loading, temp databases, fake fetchers and clocks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from garmin_live.events.bus import EventBus
from garmin_live.events.notifier import NoopNotifier
from garmin_live.livetrack.models import SessionInfo, Trackpoint
from garmin_live.livetrack.parser import parse_session, parse_trackpoints
from garmin_live.store.db import Database
from garmin_live.store.repo import Repo

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "fixtures" / "sessions"
TEST_SECRET = "test-secret-not-real-0000-abcdefghijklmnop"


def fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if (p / "manifest.json").exists())


def load_fixture_records(name: str) -> list[dict]:  # type: ignore[type-arg]
    folder = FIXTURES / name
    manifest = json.loads((folder / "manifest.json").read_text())
    return [json.loads((folder / r["file"]).read_text()) for r in manifest["responses"]]


def fixture_trackpoints(name: str = "run1") -> list[Trackpoint]:
    points: dict[datetime, Trackpoint] = {}
    for rec in load_fixture_records(name):
        if "/track-points/" in rec["request"]["url"] and rec.get("body_text"):
            for tp in parse_trackpoints(json.loads(rec["body_text"])):
                points[tp.time] = tp
    return [points[k] for k in sorted(points)]


def fixture_sessions(name: str = "run1") -> list[SessionInfo]:
    out = []
    for rec in load_fixture_records(name):
        if "/api/v2/sessions/" in rec["request"]["url"] and rec.get("body_text"):
            out.append(parse_session(json.loads(rec["body_text"])))
    return out


@pytest.fixture
def run1_points() -> list[Trackpoint]:
    return fixture_trackpoints("run1")


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def repo(db: Database) -> Repo:
    return Repo(db)


@pytest.fixture
def notifier() -> NoopNotifier:
    return NoopNotifier()


@pytest.fixture
def bus(repo: Repo, notifier: NoopNotifier) -> EventBus:
    return EventBus(repo, notifier)


# --------------------------------------------------------------------------- fakes
T0 = datetime(2026, 1, 1, 6, 0, 0, tzinfo=UTC)


def make_point(
    t_s: float,
    dist_m: float,
    *,
    hr: int | None = 140,
    alt: float | None = 100.0,
    speed: float | None = None,
    cadence: int | None = 170,
    start: datetime = T0,
) -> Trackpoint:
    """Synthetic trackpoint `t_s` seconds after `start` at cumulative distance `dist_m`."""
    return Trackpoint(
        time=start + timedelta(seconds=t_s),
        lat=-33.0,
        lon=-40.0,
        total_distance_m=dist_m,
        total_duration_s=t_s,
        speed_mps=speed,
        altitude_m=alt,
        heart_rate_bpm=hr,
        cadence_spm=cadence,
        duration_s=t_s,
        activity_type="RUNNING",
    )


def make_session_info(start: datetime = T0, *, ended_at: datetime | None = None) -> SessionInfo:
    return SessionInfo(
        session_id="00000000-0000-4000-8000-000000000001",
        start=start,
        end=ended_at or (start + timedelta(hours=24)),
        viewable=True,
        session_type="FITNESS_TRACKING",
        has_active_activities=ended_at is None,
    )


class FakeClock:
    """A controllable clock; `advance` moves it forward."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeFetcher:
    """Scripted fetcher. Each `fetch_trackpoints` call pops the next batch (a list of points or
    an exception to raise); when exhausted it returns `[]`. `fetch_session` pops from
    `sessions` the same way, repeating the last value once exhausted."""

    batches: list[list[Trackpoint] | Exception] = field(default_factory=list)
    sessions: list[SessionInfo | Exception] = field(default_factory=list)
    begins: list[datetime | None] = field(default_factory=list)
    session_calls: int = 0
    closed: bool = False
    _last_session: SessionInfo | Exception | None = None

    async def fetch_session(self) -> SessionInfo:
        self.session_calls += 1
        if self.sessions:
            self._last_session = self.sessions.pop(0)
        item = self._last_session if self._last_session is not None else make_session_info()
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_trackpoints(self, begin: datetime | None) -> list[Trackpoint]:
        self.begins.append(begin)
        if not self.batches:
            return []
        item = self.batches.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


def make_sleeper(clock: FakeClock) -> tuple[list[float], object]:
    """A sleeper that advances the fake clock instead of waiting; records requested sleeps."""
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)
        await asyncio.sleep(0)

    return sleeps, sleeper


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:  # type: ignore[no-untyped-def]
    """Poll `predicate()` until true or timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())
