"""Replay server: a fake LiveTrack that serves a scrubbed fixture session progressively.

It mirrors the real endpoint paths, auth handshake and response schemas
(docs/LIVETRACK_FINDINGS.md):

- `GET /session/<id>/token/<token>` → HTML with `<meta name="csrf-token">` + `livetrack_csrf` cookie
- `GET /api/v2/sessions/<id>?token=` → session body (active, then ended once virtual time
  passes `end`)
- `GET /api/sessions/<id>/track-points/common?token=&begin=` → points with
  `dateTime > begin` that have "happened" by virtual time

Timestamps are rebased by default (`rebase=True`): the fixture's first point is placed at the
moment the server starts, intervals are kept 1:1, and the virtual clock runs `speed` times faster
than real time, so a 15-minute fixture replays in 45 s at 20x while looking like a live session.

Knobs: `speed` (virtual seconds per real second), `gap_at`/`gap_seconds` (a stale window: no points
are released while virtual time is inside it), `error_rate` (probability of a 503),
`rate_limit_every` (every Nth API request → 429), `end_behavior` (`garmin` | `never`),
`expire_after` (virtual seconds after end → 404). Test helpers live under `/_replay/`.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

CSRF_COOKIE = "livetrack_csrf"
CSRF_HEADER = "livetrack-csrf-token"
# Fixture roots, in order: the source checkout, the working directory (the container's WORKDIR
# is /app, where the Dockerfile copies fixtures/sessions), and the conventional image path.
FIXTURE_ROOTS = (
    Path(__file__).resolve().parents[3] / "fixtures" / "sessions",
    Path.cwd() / "fixtures" / "sessions",
    Path("/app/fixtures/sessions"),
)
FIXTURES_ROOT = FIXTURE_ROOTS[0]
END_AFTER_LAST_POINT_S = 45.0  # observed: Garmin sets end = last point + ~45 s


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(slots=True)
class Fixture:
    name: str
    session_id: str
    token: str
    session_active: dict[str, Any]
    session_ended: dict[str, Any]
    points: list[dict[str, Any]]  # sorted by dateTime
    page_html: str

    @property
    def first_point_time(self) -> datetime:
        return _parse(self.points[0]["dateTime"])

    @property
    def last_point_time(self) -> datetime:
        return _parse(self.points[-1]["dateTime"])

    @property
    def end_time(self) -> datetime:
        return _parse(self.session_ended["end"])


def find_fixture_dir(name: str, root: Path | None = None) -> Path:
    roots = (root,) if root is not None else FIXTURE_ROOTS
    for candidate in roots:
        if (candidate / name / "manifest.json").exists():
            return candidate / name
    searched = ", ".join(str(r) for r in roots)
    raise FileNotFoundError(f"fixture {name!r} not found (searched: {searched})")


def load_fixture(name: str, root: Path | None = None) -> Fixture:
    folder = find_fixture_dir(name, root)
    manifest = json.loads((folder / "manifest.json").read_text())
    session_bodies: list[dict[str, Any]] = []
    points: dict[str, dict[str, Any]] = {}
    for entry in sorted(manifest["responses"], key=lambda r: r["seq"]):
        rec = json.loads((folder / entry["file"]).read_text())
        if rec["response"]["status"] != 200 or not rec.get("body_text"):
            continue
        body = json.loads(rec["body_text"])
        url = rec["request"]["url"]
        if "/api/v2/sessions/" in url:
            session_bodies.append(body)
        elif "/track-points/" in url:
            for tp in body.get("trackPoints", []):
                points[tp["dateTime"]] = tp
    if not session_bodies or not points:
        raise ValueError(f"fixture {name!r} has no session or trackpoint responses")
    ordered = [points[k] for k in sorted(points, key=_parse)]
    active = session_bodies[0]
    # Garmin's start/end carry ~1 ms jitter between polls; the ended body is the one whose
    # end-start duration is materially shorter than the active placeholder (start + 24 h).
    active_span = (_parse(active["end"]) - _parse(active["start"])).total_seconds()
    shorter = [
        b
        for b in session_bodies
        if active_span - (_parse(b["end"]) - _parse(b["start"])).total_seconds() > 60
    ]
    ended = min(shorter, key=lambda b: _parse(b["end"])) if shorter else None
    if ended is None:
        ended = {k: v for k, v in active.items() if k != "activeMultiDayActivities"}
        ended["end"] = _iso(
            _parse(ordered[-1]["dateTime"]) + timedelta(seconds=END_AFTER_LAST_POINT_S)
        )
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="csrf-token" content="__CSRF__"><title>LiveTrack replay</title></head>'
        "<body>replay</body></html>"
    )
    return Fixture(
        name=name,
        session_id=manifest["fake_session_id"],
        token=manifest["fake_token"],
        session_active=active,
        session_ended=ended,
        points=ordered,
        page_html=html,
    )


@dataclass(slots=True)
class ReplayConfig:
    speed: float = 20.0
    start_offset_s: float = 5.0  # virtual seconds before the first point at which the clock starts
    gap_at: float | None = None  # virtual seconds after the first point when the stale gap begins
    gap_seconds: float = 0.0
    error_rate: float = 0.0
    rate_limit_every: int = 0
    end_behavior: Literal["garmin", "never"] = "garmin"
    expire_after: float | None = None  # virtual seconds after end → 404
    seed: int = 1
    rebase: bool = True  # shift fixture timestamps so the replay starts "now"


@dataclass(slots=True)
class ReplayState:
    fixture: Fixture
    config: ReplayConfig
    real_start: float = field(default_factory=time.monotonic)
    wall_start: datetime = field(default_factory=lambda: datetime.now(UTC))
    manual_offset_s: float = 0.0
    csrf_pairs: dict[str, str] = field(default_factory=dict)  # cookie -> header token
    api_requests: int = 0
    counters: dict[str, int] = field(
        default_factory=lambda: {
            "page": 0,
            "session": 0,
            "trackpoints": 0,
            "403": 0,
            "429": 0,
            "5xx": 0,
            "404": 0,
        }
    )
    rng: random.Random = field(default_factory=lambda: random.Random(1))

    @property
    def virtual_origin(self) -> datetime:
        return self.fixture.first_point_time - timedelta(seconds=self.config.start_offset_s)

    def virtual_now(self) -> datetime:
        elapsed = (time.monotonic() - self.real_start) * self.config.speed + self.manual_offset_s
        return self.virtual_origin + timedelta(seconds=elapsed)

    @property
    def shift(self) -> timedelta:
        """Offset added to fixture timestamps when serving (zero unless `rebase`)."""
        if not self.config.rebase:
            return timedelta(0)
        return self.wall_start - self.virtual_origin

    def out_time(self, dt: datetime) -> str:
        return _iso(dt + self.shift)

    def in_time(self, value: str) -> datetime:
        """Convert a client-supplied timestamp (e.g. `begin`) back into fixture time."""
        return _parse(value) - self.shift

    def session_body(self, body: dict[str, Any]) -> dict[str, Any]:
        out = dict(body)
        for key in ("start", "end"):
            if isinstance(out.get(key), str):
                out[key] = self.out_time(_parse(out[key]))
        return out

    def point(self, tp: dict[str, Any]) -> dict[str, Any]:
        out = dict(tp)
        for key in ("dateTime", "reportedTime"):
            if isinstance(out.get(key), str):
                out[key] = self.out_time(_parse(out[key]))
        return out

    def in_gap(self, vnow: datetime) -> bool:
        if self.config.gap_at is None or self.config.gap_seconds <= 0:
            return False
        gap_start = self.fixture.first_point_time + timedelta(seconds=self.config.gap_at)
        return gap_start <= vnow < gap_start + timedelta(seconds=self.config.gap_seconds)

    def ended(self, vnow: datetime) -> bool:
        return self.config.end_behavior == "garmin" and vnow >= self.fixture.end_time

    def expired(self, vnow: datetime) -> bool:
        if self.config.expire_after is None:
            return False
        return vnow >= self.fixture.end_time + timedelta(seconds=self.config.expire_after)


def build_app(fixture: Fixture, config: ReplayConfig | None = None) -> Starlette:
    state = ReplayState(fixture=fixture, config=config or ReplayConfig())
    state.rng = random.Random(state.config.seed)

    def _check_ids(request: Request) -> Response | None:
        if request.path_params.get("sid", fixture.session_id) != fixture.session_id:
            state.counters["404"] += 1
            return JSONResponse({"errors": [{"message": "not found"}]}, status_code=404)
        if request.query_params.get("token") != fixture.token:
            state.counters["403"] += 1
            return Response(status_code=403)
        cookie = request.cookies.get(CSRF_COOKIE)
        header = request.headers.get(CSRF_HEADER)
        if not cookie or not header or state.csrf_pairs.get(cookie) != header:
            state.counters["403"] += 1
            return Response(status_code=403)
        return None

    def _faults() -> Response | None:
        state.api_requests += 1
        if (
            state.config.rate_limit_every
            and state.api_requests % state.config.rate_limit_every == 0
        ):
            state.counters["429"] += 1
            return Response(status_code=429, headers={"Retry-After": "5"})
        if state.config.error_rate and state.rng.random() < state.config.error_rate:
            state.counters["5xx"] += 1
            return Response(status_code=503)
        return None

    async def page(request: Request) -> Response:
        state.counters["page"] += 1
        if (
            request.path_params["sid"] != fixture.session_id
            or request.path_params["tok"] != fixture.token
        ):
            state.counters["404"] += 1
            return Response("not found", status_code=404)
        if state.expired(state.virtual_now()):
            state.counters["404"] += 1
            return Response("gone", status_code=404)
        cookie = secrets.token_urlsafe(48)
        header = str(uuid.uuid4())
        state.csrf_pairs[cookie] = header
        resp = HTMLResponse(fixture.page_html.replace("__CSRF__", header))
        resp.set_cookie(CSRF_COOKIE, cookie, httponly=True, samesite="lax")
        return resp

    async def session(request: Request) -> Response:
        state.counters["session"] += 1
        vnow = state.virtual_now()
        if state.expired(vnow):
            state.counters["404"] += 1
            return Response(status_code=404)
        denied = _check_ids(request) or _faults()
        if denied is not None:
            return denied
        body = fixture.session_ended if state.ended(vnow) else fixture.session_active
        return JSONResponse(state.session_body(body))

    async def trackpoints(request: Request) -> Response:
        state.counters["trackpoints"] += 1
        vnow = state.virtual_now()
        if state.expired(vnow):
            state.counters["404"] += 1
            return Response(status_code=404)
        denied = _check_ids(request) or _faults()
        if denied is not None:
            return denied
        begin_raw = request.query_params.get("begin")
        begin = state.in_time(begin_raw) if begin_raw else None
        if state.in_gap(vnow):
            gap_start = fixture.first_point_time + timedelta(seconds=state.config.gap_at or 0)
            cutoff = min(vnow, gap_start)
        else:
            cutoff = vnow
        visible = [
            state.point(p)
            for p in fixture.points
            if _parse(p["dateTime"]) <= cutoff and (begin is None or _parse(p["dateTime"]) > begin)
        ]
        return JSONResponse({"trackPoints": visible})

    async def replay_state(request: Request) -> Response:
        vnow = state.virtual_now()
        return JSONResponse(
            {
                "virtual_now": _iso(vnow),
                "points_released": sum(1 for p in fixture.points if _parse(p["dateTime"]) <= vnow),
                "points_total": len(fixture.points),
                "ended": state.ended(vnow),
                "expired": state.expired(vnow),
                "in_gap": state.in_gap(vnow),
                "counters": state.counters,
                "api_requests": state.api_requests,
            }
        )

    async def replay_advance(request: Request) -> Response:
        seconds = float(request.query_params.get("seconds", "0"))
        state.manual_offset_s += seconds
        return await replay_state(request)

    routes = [
        Route("/session/{sid}/token/{tok}", page),
        Route("/api/v2/sessions/{sid}", session),
        Route("/api/sessions/{sid}/track-points/common", trackpoints),
        Route("/_replay/state", replay_state),
        Route("/_replay/advance", replay_advance, methods=["POST", "GET"]),
    ]
    app = Starlette(routes=routes)
    app.state.replay = state
    return app


class ReplayServer:
    """Runs the replay app on 127.0.0.1 with uvicorn inside the current event loop."""

    def __init__(
        self,
        fixture: Fixture,
        config: ReplayConfig | None = None,
        port: int = 0,
        host: str = "127.0.0.1",
    ) -> None:
        self.fixture = fixture
        self.app = build_app(fixture, config)
        self.host = host
        self._port = port
        self._server: Any = None
        self._task: Any = None
        self.port: int = port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def share_url(self) -> str:
        return f"{self.base_url}/session/{self.fixture.session_id}/token/{self.fixture.token}"

    async def start(self) -> None:
        if self._port == 0:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                self._port = s.getsockname()[1]
        self.port = self._port
        config = uvicorn.Config(
            self.app, host=self.host, port=self._port, log_level="warning", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("replay server did not start")

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task


def main() -> None:
    """`python -m garmin_live.replay.server [fixture] [port] [speed] [host]` for manual use.

    `host` defaults to 127.0.0.1; the local docker stack passes 0.0.0.0 so other containers can
    reach it. The share URL is printed with the token masked.
    """
    name = sys.argv[1] if len(sys.argv) > 1 else "run1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
    speed = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    host = sys.argv[4] if len(sys.argv) > 4 else "127.0.0.1"

    async def run() -> None:
        server = ReplayServer(load_fixture(name), ReplayConfig(speed=speed), port=port, host=host)
        await server.start()
        masked = server.share_url.rsplit("/token/", 1)[0] + "/token/<token>"
        print(f"replay serving fixture {name!r} at {masked} (speed {speed}x)", flush=True)
        try:
            await asyncio.Event().wait()  # run until interrupted
        finally:
            await server.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
