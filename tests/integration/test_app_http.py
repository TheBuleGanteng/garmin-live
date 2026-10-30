"""The full FastAPI app over real HTTP (uvicorn in-process): health under BASE_PATH, 404 policy,
Host/Origin protection, and the MCP endpoint through the official client, for both a prefixed
and a root BASE_PATH."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp import Client

from garmin_live import __version__
from garmin_live.app import create_app
from garmin_live.config import load_settings
from tests.conftest import TEST_SECRET

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


class AppServer:
    def __init__(self, base_path: str, tmp_path: Path, **env: str) -> None:
        self.base_path = base_path
        self.settings = load_settings(
            mcp_path_secret=TEST_SECRET,
            base_path=base_path,
            db_path=str(tmp_path / "app.db"),
            mcp_allowed_hosts="localhost,127.0.0.1",
            log_level="WARNING",
            **env,
        )
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = create_app(self.settings)
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False
            )
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}{self.base_path}"

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp/{TEST_SECRET}"

    async def __aenter__(self) -> AppServer:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(300):
            if self._server.started:
                return self
            await asyncio.sleep(0.02)
        raise RuntimeError("app did not start")

    async def __aexit__(self, *exc: object) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await self._task


@pytest.fixture(params=["/garmin-mcp", ""], ids=["prefixed", "root"])
async def app_server(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[AppServer]:
    async with AppServer(request.param, tmp_path) as server:
        yield server


async def test_health_and_404_policy(app_server: AppServer) -> None:
    async with httpx.AsyncClient(follow_redirects=False) as c:
        health = await c.get(f"{app_server.url}/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "version": __version__, "active_sessions": 0}
        assert set(health.json()) == {"status", "version", "active_sessions"}  # no PII
        for path in (
            "/mcp",
            "/mcp/",
            "/mcp/wrong-secret-not-real-0000000000000000",
            f"/mcp/{TEST_SECRET}/x",
        ):
            r = await c.post(f"{app_server.url}{path}", json=INIT, headers=MCP_HEADERS)
            assert r.status_code == 404, path
            assert "www-authenticate" not in r.headers
        if app_server.base_path:
            assert (await c.get(f"http://127.0.0.1:{app_server.port}/health")).status_code == 404
            assert (
                await c.post(
                    f"http://127.0.0.1:{app_server.port}/mcp/{TEST_SECRET}",
                    json=INIT,
                    headers=MCP_HEADERS,
                )
            ).status_code == 404
        ok = await c.post(app_server.mcp_url, json=INIT, headers=MCP_HEADERS)
        assert ok.status_code == 200  # no redirect dance
        bad_host = await c.post(
            app_server.mcp_url, json=INIT, headers={**MCP_HEADERS, "Host": "evil.example"}
        )
        assert bad_host.status_code == 421
        bad_origin = await c.post(
            app_server.mcp_url, json=INIT, headers={**MCP_HEADERS, "Origin": "https://evil.example"}
        )
        assert bad_origin.status_code == 403
        assert (await c.get(f"{app_server.url}/docs")).status_code == 404
        assert (await c.get(f"{app_server.url}/openapi.json")).status_code == 404


async def test_mcp_over_http_with_official_client(app_server: AppServer) -> None:
    async with Client(app_server.mcp_url) as client:
        assert client.server_info is not None and client.server_info.name == "garmin-live"
        names = {t.name for t in (await client.list_tools()).tools}
        assert "get_live_status" in names and len(names) == 7
        listed = await client.call_tool("list_sessions", {})
        assert not listed.is_error and listed.structured_content == {"sessions": []}
        status = await client.call_tool("get_live_status", {})
        assert status.is_error and "No sessions yet" in status.content[0].text  # type: ignore[union-attr]
        rejected = await client.call_tool(
            "start_tracking", {"livetrack_url": "https://evil.example/session/x/token/y"}
        )
        assert rejected.is_error and "livetrack.garmin.com" in rejected.content[0].text  # type: ignore[union-attr]
    # independent requests work in stateless mode: a second client is fine
    async with Client(app_server.mcp_url) as client2:
        assert (await client2.call_tool("list_sessions", {})).structured_content == {"sessions": []}


async def test_ip_allowlist_blocks_untrusted_clients(tmp_path: Path) -> None:
    async with (
        AppServer(
            "/garmin-mcp",
            tmp_path,
            enforce_ip_allowlist="true",
            allowed_ips="203.0.113.0/24",
            trusted_proxy_cidrs="127.0.0.1/32",
        ) as server,
        httpx.AsyncClient() as c,
    ):
        # peer is the trusted proxy (127.0.0.1) → X-Real-IP decides
        denied = await c.post(
            server.mcp_url, json=INIT, headers={**MCP_HEADERS, "X-Real-IP": "198.51.100.1"}
        )
        assert denied.status_code == 404
        allowed = await c.post(
            server.mcp_url, json=INIT, headers={**MCP_HEADERS, "X-Real-IP": "203.0.113.7"}
        )
        assert allowed.status_code == 200
        no_header = await c.post(server.mcp_url, json=INIT, headers=MCP_HEADERS)
        assert no_header.status_code == 404  # 127.0.0.1 itself is not allow-listed
        assert (await c.get(f"{server.url}/health")).status_code == 200  # health is not gated
