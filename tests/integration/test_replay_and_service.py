"""Replay server knobs, full ingest through the real HttpFetcher, the service layer, and the MCP
tools exercised in-process through the official client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from mcp import Client

from garmin_live.events.bus import EventBus
from garmin_live.events.notifier import NoopNotifier
from garmin_live.ingest.poller import Poller, default_fetcher_factory
from garmin_live.livetrack.urls import LIVETRACK_API_BASE
from garmin_live.mcp_server.server import build_mcp_server
from garmin_live.replay.server import ReplayConfig, ReplayServer, load_fixture
from garmin_live.service import SHORT_LINK_EXPAND_FAILED, ServiceError, TrackingService
from garmin_live.store import repo as st
from garmin_live.store.repo import Repo, SessionRow
from tests.conftest import T0, FakeClock, FakeFetcher, make_point, wait_until

SID = "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000"
TOK = "FAKETOKENnotreal0000ABCDEFGHI"
LONG = f"https://livetrack.garmin.com/session/{SID}/token/{TOK}"
SHORT = "https://gar.mn/abcDEF12"


@pytest.fixture
async def replay() -> AsyncIterator[ReplayServer]:
    server = ReplayServer(load_fixture("run1"), ReplayConfig(speed=60.0, start_offset_s=2), port=0)
    await server.start()
    yield server
    await server.stop()


async def _state(server: ReplayServer) -> dict[str, Any]:
    async with httpx.AsyncClient() as c:
        return dict((await c.get(f"{server.base_url}/_replay/state")).json())


async def _advance(server: ReplayServer, seconds: float) -> None:
    async with httpx.AsyncClient() as c:
        await c.post(f"{server.base_url}/_replay/advance", params={"seconds": seconds})


def _service(
    repo: Repo,
    notifier: NoopNotifier,
    http_client: httpx.AsyncClient | None = None,
    **poller_kw: Any,
) -> tuple[TrackingService, Poller]:
    poller = Poller(
        repo, EventBus(repo, notifier), poll_min_seconds=0.2, max_session_hours=12, **poller_kw
    )
    poller.session_check_s = 0.4
    return TrackingService(repo, poller, athlete_max_hr=185, http_client=http_client), poller


def _shortlink_client(
    routes: dict[str, tuple[int, dict[str, str]]], *, fail: bool = False
) -> httpx.AsyncClient:
    """Mock gar.mn: `routes` maps request URL → (status, headers); `fail` raises a transport error.
    Anything unmapped answers 404 so the test never reaches the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("offline", request=request)
        status, headers = routes.get(str(request.url), (404, {}))
        return httpx.Response(status, headers=headers, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


def _fake_for_real_host_factory(fake: FakeFetcher):  # type: ignore[no-untyped-def]
    """Sessions on the real LiveTrack host get `fake` (no network); replay sessions keep the
    real HttpFetcher so the rest of the test is unchanged."""

    def factory(session: SessionRow):  # type: ignore[no-untyped-def]
        if session.api_base == LIVETRACK_API_BASE:
            return fake
        return default_fetcher_factory(session)

    return factory


async def test_replay_requires_the_csrf_pair_and_mirrors_paths(replay: ReplayServer) -> None:
    fx = replay.fixture
    async with httpx.AsyncClient(follow_redirects=False) as c:
        sess_url = f"{replay.base_url}/api/v2/sessions/{fx.session_id}"
        assert (await c.get(sess_url, params={"token": fx.token})).status_code == 403
        page = await c.get(replay.share_url)
        assert page.status_code == 200 and "csrf-token" in page.text
        cookie = c.cookies.get("livetrack_csrf")
        assert cookie
        header = page.text.split('content="')[1].split('"')[0]
        ok = await c.get(
            sess_url, params={"token": fx.token}, headers={"livetrack-csrf-token": header}
        )
        assert ok.status_code == 200 and ok.json()["sessionId"] == fx.session_id
        bad_tok = await c.get(
            sess_url, params={"token": "nope"}, headers={"livetrack-csrf-token": header}
        )
        assert bad_tok.status_code == 403
        wrong = await c.get(
            sess_url, params={"token": fx.token}, headers={"livetrack-csrf-token": cookie}
        )
        assert wrong.status_code == 403
        other = await c.get(
            f"{replay.base_url}/api/v2/sessions/00000000-0000-4000-8000-000000000bad",
            params={"token": fx.token},
            headers={"livetrack-csrf-token": header},
        )
        assert other.status_code == 404
        # timestamps are rebased to "now" and points are released progressively
        tp = await c.get(
            f"{replay.base_url}/api/sessions/{fx.session_id}/track-points/common",
            params={"token": fx.token},
            headers={"livetrack-csrf-token": header},
        )
        assert tp.status_code == 200
        released = tp.json()["trackPoints"]
        assert 0 <= len(released) < len(fx.points)
        state = await _state(replay)
        assert state["points_total"] == len(fx.points) and state["ended"] is False


async def test_replay_knobs_gap_errors_rate_limit_expiry() -> None:
    fx = load_fixture("run1")
    cfg = ReplayConfig(
        speed=1.0,
        start_offset_s=0,
        gap_at=60,
        gap_seconds=120,
        error_rate=1.0,
        rate_limit_every=3,
        expire_after=10,
    )
    server = ReplayServer(fx, cfg, port=0)
    await server.start()
    try:
        async with httpx.AsyncClient(follow_redirects=False) as c:
            page = await c.get(server.share_url)
            header = page.text.split('content="')[1].split('"')[0]
            h = {"livetrack-csrf-token": header}
            url = f"{server.base_url}/api/sessions/{fx.session_id}/track-points/common"
            codes = [
                (await c.get(url, params={"token": fx.token}, headers=h)).status_code
                for _ in range(4)
            ]
            assert (
                codes[:2] == [503, 503] and codes[2] == 429
            )  # every 3rd request → 429 before the 5xx roll
            server.app.state.replay.config.error_rate = 0.0
            server.app.state.replay.config.rate_limit_every = 0
            await _advance(server, 90)  # into the gap window (starts 60 s after the first point)
            state = await _state(server)
            assert state["in_gap"] is True
            gap_points = (await c.get(url, params={"token": fx.token}, headers=h)).json()[
                "trackPoints"
            ]
            assert all(True for _ in gap_points)
            await _advance(server, 200)  # past the gap
            assert (await _state(server))["in_gap"] is False
            more = (await c.get(url, params={"token": fx.token}, headers=h)).json()["trackPoints"]
            assert len(more) > len(gap_points)
            await _advance(server, 5 * 3600)  # far past the end → ended, then expired after 10 s
            s = await _state(server)
            assert s["ended"] is True and s["expired"] is True
            assert (await c.get(url, params={"token": fx.token}, headers=h)).status_code == 404
            assert (await c.get(server.share_url)).status_code == 404
            assert s["counters"]["429"] >= 1 and s["counters"]["5xx"] >= 2
    finally:
        await server.stop()


async def test_full_ingest_through_http_fetcher_until_garmin_end(
    repo: Repo, notifier: NoopNotifier, replay: ReplayServer
) -> None:
    service, poller = _service(repo, notifier)
    fx = replay.fixture
    started = await service.start_tracking_internal(
        fx.session_id, fx.token, replay.base_url, label="it"
    )
    sid = str(started["session_id"])
    assert started["status"] == st.ACTIVE
    assert await wait_until(lambda: not (repo.get_session(sid) or started).is_active, timeout=60)  # type: ignore[union-attr]
    await poller.stop()
    final = repo.get_session(sid)
    assert final is not None and final.status == st.ENDED and final.end_reason == "garmin_end"
    assert repo.count_trackpoints(sid) == len(fx.points)
    types = [e.type for e in repo.list_events(sid)]
    assert (
        types.count("new_points") >= 5
        and "split_completed" in types
        and types[-1] == "session_ended"
    )
    assert notifier.delivered == len(types)
    status = service.get_live_status(sid)
    assert status["distance_m"] == 1022 and status["session_status"] == st.ENDED
    assert status["data_freshness_seconds"] is not None
    summary = service.get_session_summary(sid)
    assert summary["hr_zones"] is not None and summary["avg_pace_moving"] is not None
    assert "lat" not in json.dumps(summary) and "lon" not in json.dumps(status)


async def test_service_validation_and_limits(repo: Repo, notifier: NoopNotifier) -> None:
    service, poller = _service(repo, notifier)
    with pytest.raises(ServiceError, match="https"):
        await service.start_tracking("http://livetrack.garmin.com/session/x/token/y")
    with pytest.raises(ServiceError, match="No sessions yet"):
        service.get_live_status(None)
    with pytest.raises(ServiceError, match="Unknown session_id"):
        service.get_splits("s_nope")
    with pytest.raises(ServiceError, match="metric"):
        service.get_trend(None, "power")
    with pytest.raises(ServiceError, match="split_distance_m"):
        service.get_splits(None, 5)
    # concurrency cap: 3 active sessions
    poller.max_active_sessions = 3
    for i in range(3):
        repo.create_session(
            label=f"a{i}",
            garmin_session_id=f"g{i}",
            token="t",
            api_base="http://127.0.0.1:1",
            poll_interval_s=20,
        )
    with pytest.raises(ServiceError, match="maximum"):
        await service.start_tracking_internal("g-new", "t", "http://127.0.0.1:1")
    # same garmin session → returns the existing one
    again = await service.start_tracking_internal("g0", "t", "http://127.0.0.1:1")
    assert again["label"] == "a0" and "already tracking" in str(again["note"])
    listed = service.list_sessions()
    assert len(listed) == 3 and {s["status"] for s in listed} == {st.ACTIVE}
    await poller.stop()


async def test_list_sessions_ages_and_stale_semantics(repo: Repo, notifier: NoopNotifier) -> None:
    clock = FakeClock(T0 + timedelta(seconds=130))
    poller = Poller(repo, EventBus(repo, notifier), poll_min_seconds=20, max_session_hours=12)
    service = TrackingService(repo, poller, athlete_max_hr=None, clock=clock)
    s = repo.create_session(
        label="x",
        garmin_session_id="g",
        token="t",
        api_base="http://127.0.0.1:1",
        poll_interval_s=20,
    )
    # no points yet: both ages are null
    [row] = service.list_sessions()
    assert row["last_data_age_s"] is None and row["last_ingest_age_s"] is None
    assert row["stale"] is False
    # newest Garmin timestamp T0+20 s, ingested (last_point_at) at T0+100 s, now = T0+130 s
    repo.insert_trackpoints(s.id, [make_point(0, 0), make_point(20, 60)])
    repo.update_session(s.id, last_point_at=T0 + timedelta(seconds=100), stale=True)
    [row] = service.list_sessions()
    assert row["last_data_age_s"] == 110  # Garmin's time, not ingest time
    assert row["last_ingest_age_s"] == 30
    assert row["stale"] is True and row["points"] == 2
    # ended / expired / stopped sessions are never reported as stale, whatever the stored flag
    for status in (st.ENDED, st.EXPIRED, st.STOPPED):
        repo.update_session(s.id, status=status, stale=True)
        [row] = service.list_sessions()
        assert row["status"] == status and row["stale"] is False
        assert row["last_data_age_s"] == 110 and row["last_ingest_age_s"] == 30
    await service.aclose()


async def test_start_tracking_expands_short_links(repo: Repo, notifier: NoopNotifier) -> None:
    fake = FakeFetcher(batches=[[make_point(0, 0.0), make_point(10, 30.0)]])
    routes = {
        SHORT: (301, {"location": LONG}),
        "https://gar.mn/badPATH1": (301, {"location": f"https://livetrack.garmin.com/x/{SID}"}),
        "https://gar.mn/otherHST": (301, {"location": LONG.replace("garmin.com", "evil.example")}),
        "https://gar.mn/noRedir1": (200, {}),
    }
    service, poller = _service(
        repo,
        notifier,
        _shortlink_client(routes),
        fetcher_factory=_fake_for_real_host_factory(fake),
    )
    # rejected: expansion lands outside the policy or on an invalid share URL
    with pytest.raises(ServiceError, match=r"/session/<id>/token/<token>"):
        await service.start_tracking("https://gar.mn/badPATH1")
    with pytest.raises(ServiceError, match=r"did not lead to livetrack\.garmin\.com"):
        await service.start_tracking("https://gar.mn/otherHST")
    with pytest.raises(ServiceError, match="did not redirect"):
        await service.start_tracking("https://gar.mn/noRedir1")
    with pytest.raises(ServiceError, match=r"gar\.mn short link"):
        await service.start_tracking("https://gar.mn/abc/def")
    assert repo.list_sessions() == []
    # accepted: expands to LONG, which parse_share_url accepts; tracked on the real host base
    started = await service.start_tracking(SHORT, label="via short link")
    assert started["status"] == st.ACTIVE and started["label"] == "via short link"
    row = repo.find_by_garmin_id(SID)
    assert row is not None and row.token == TOK and row.api_base == LIVETRACK_API_BASE
    assert await wait_until(lambda: repo.count_trackpoints(row.id) >= 2, timeout=5)
    # idempotent: the same short link maps to the same session
    again = await service.start_tracking(SHORT)
    assert again["session_id"] == started["session_id"] and "already tracking" in str(again["note"])
    await poller.stop()
    await service.aclose()


async def test_start_tracking_wraps_short_link_transport_errors(
    repo: Repo, notifier: NoopNotifier
) -> None:
    service, poller = _service(repo, notifier, _shortlink_client({}, fail=True))
    with pytest.raises(ServiceError) as exc:
        await service.start_tracking(SHORT)
    assert str(exc.value) == SHORT_LINK_EXPAND_FAILED and "gar.mn" not in str(exc.value)
    assert repo.list_sessions() == []
    await poller.stop()
    await service.aclose()


async def test_mcp_tools_in_process(
    repo: Repo, notifier: NoopNotifier, replay: ReplayServer
) -> None:
    # gar.mn is mocked: SHORT 404s (not a redirect) and only a *different* code redirects properly
    fake = FakeFetcher(batches=[[make_point(0, 0.0), make_point(10, 30.0)]])
    service, poller = _service(
        repo,
        notifier,
        _shortlink_client({"https://gar.mn/goodCODE": (301, {"location": LONG})}),
        fetcher_factory=_fake_for_real_host_factory(fake),
    )
    server = build_mcp_server(service)
    fx = replay.fixture
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {
            "start_tracking",
            "list_sessions",
            "get_live_status",
            "get_splits",
            "get_trend",
            "get_session_summary",
            "stop_tracking",
        }
        assert (
            "when" in tools["start_tracking"].description.lower()
            or "call this" in tools["start_tracking"].description.lower()
        )
        assert client.instructions and "session_id is optional" in client.instructions

        # a gar.mn link whose expansion does not yield a LiveTrack URL is rejected...
        bad = await client.call_tool("start_tracking", {"livetrack_url": SHORT})
        assert bad.is_error and "short link" in bad.content[0].text  # type: ignore[union-attr]

        empty = await client.call_tool("get_live_status", {})
        assert empty.is_error and "No sessions yet" in empty.content[0].text  # type: ignore[union-attr]

        # the MCP tool cannot reach the replay server: host check is enforced
        local = await client.call_tool(
            "start_tracking", {"livetrack_url": replay.share_url.replace("http://", "https://")}
        )
        assert local.is_error

        started = await service.start_tracking_internal(
            fx.session_id, fx.token, replay.base_url, label="mcp"
        )
        sid = str(started["session_id"])
        assert await wait_until(lambda: repo.count_trackpoints(sid) >= 10, timeout=30)

        listed = await client.call_tool("list_sessions", {})
        assert not listed.is_error
        rows = listed.structured_content["sessions"]  # type: ignore[index]
        assert rows[0]["session_id"] == sid and rows[0]["label"] == "mcp"

        status = await client.call_tool("get_live_status", {})
        sc = status.structured_content
        assert sc is not None and sc["points"] >= 10 and "lat" not in sc and "current_pace" in sc

        splits = await client.call_tool("get_splits", {"split_distance_m": 500})
        assert splits.structured_content["split_distance_m"] == 500  # type: ignore[index]

        trend = await client.call_tool(
            "get_trend", {"metric": "hr", "window_minutes": 20, "bucket_seconds": 60}
        )
        assert trend.structured_content["unit"] == "bpm"  # type: ignore[index]

        bad_metric = await client.call_tool("get_trend", {"metric": "power"})
        assert bad_metric.is_error

        summary = await client.call_tool("get_session_summary", {"session_id": sid})
        assert summary.structured_content["hr_zones"] is not None  # type: ignore[index]

        stopped = await client.call_tool("stop_tracking", {"session_id": sid})
        assert stopped.structured_content["status"] == st.STOPPED  # type: ignore[index]
        unknown = await client.call_tool("stop_tracking", {"session_id": "s_nope"})
        assert unknown.is_error

        # a gar.mn link that expands to a valid share URL is accepted by the tool and tracked
        good = await client.call_tool(
            "start_tracking", {"livetrack_url": "https://gar.mn/goodCODE", "label": "short"}
        )
        assert not good.is_error
        gsc = good.structured_content
        assert gsc is not None and gsc["label"] == "short" and gsc["status"] == st.ACTIVE
        expanded = repo.find_by_garmin_id(SID)
        assert expanded is not None and expanded.token == TOK
        assert expanded.api_base == LIVETRACK_API_BASE
        stopped_short = await client.call_tool("stop_tracking", {"session_id": gsc["session_id"]})
        assert stopped_short.structured_content["status"] == st.STOPPED  # type: ignore[index]
    await poller.stop()
    await asyncio.sleep(0)
