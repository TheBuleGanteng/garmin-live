"""End-to-end through the local docker stack (nginx → app under /garmin-mcp, replay at 20x).

Local-only (needs Docker); skipped in CI. Run with:

    MCP_PATH_SECRET=test-secret-not-real-0000-abcdefghijklmnop \
      .venv/bin/python -m pytest tests/e2e -m e2e -q

Covers Phase 6 steps 4-8: health through nginx, the MCP smoke tool, wrong secret → 404, a full
tracking session against the replay container via the MCP tools, selftest exit 0 inside the
container, the privacy check on captured logs and tool outputs, peak memory, and the root
BASE_PATH variant.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "docker/docker-compose.local.yml"]
SECRET = os.environ.get("MCP_PATH_SECRET", "test-secret-not-real-0000-abcdefghijklmnop")
PORT = os.environ.get("LOCAL_NGINX_PORT", "8080")
BASE = f"http://localhost:{PORT}/garmin-mcp"
MEMORY_LIMIT_MB = 200.0
PEAK_FILE = ROOT / "fixtures" / "raw" / "e2e_peak_memory_mb.txt"

pytestmark = pytest.mark.e2e

if shutil.which("docker") is None:
    pytest.skip("docker not available", allow_module_level=True)


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MCP_PATH_SECRET": SECRET, "LOCAL_NGINX_PORT": PORT}
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=False, **kw)


def _exec(service: str, *args: str) -> subprocess.CompletedProcess[str]:
    return _run([*COMPOSE, "exec", "-T", service, *args], timeout=600)


def _wait_health(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} did not become healthy")


def _container_mem_mb(name_fragment: str = "garmin-live") -> float:
    out = _run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"]).stdout
    for line in out.splitlines():
        if name_fragment in line and "replay" not in line and "nginx" not in line:
            usage = line.split()[1]  # e.g. 58.3MiB
            value = float(re.sub(r"[^0-9.]", "", usage))
            return (
                value / 1024
                if usage.endswith("KiB")
                else value * 1024
                if usage.endswith("GiB")
                else value
            )
    return 0.0


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, Any]]:
    build = _run([*COMPOSE, "build"], timeout=900)
    assert build.returncode == 0, build.stderr[-2000:]
    up = _run([*COMPOSE, "up", "-d"], timeout=300)
    assert up.returncode == 0, up.stderr[-2000:]
    try:
        _wait_health(f"{BASE}/health")
        yield {"base": BASE, "mcp": f"{BASE}/mcp/{SECRET}"}
    finally:
        logs = _run([*COMPOSE, "logs", "--no-color"], timeout=120).stdout
        (ROOT / "fixtures" / "raw").mkdir(exist_ok=True)
        (ROOT / "fixtures" / "raw" / "e2e_logs.txt").write_text(logs)
        _run([*COMPOSE, "down", "-v"], timeout=300)


def test_health_through_nginx(stack: dict[str, Any]) -> None:
    r = httpx.get(f"{stack['base']}/health", timeout=10)
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert httpx.get(f"http://localhost:{PORT}/", timeout=10).status_code == 404
    assert httpx.get(f"{stack['base']}/mcp/", timeout=10).status_code == 404


def test_mcp_smoke_tool_and_wrong_secret(stack: dict[str, Any]) -> None:
    smoke = _run(
        [str(ROOT / ".venv/bin/python"), "tools/mcp_smoke.py", "--url", stack["mcp"]], timeout=120
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    assert SECRET not in smoke.stdout
    wrong = httpx.post(
        f"{stack['base']}/mcp/wrong-secret-not-real-00000000000000000",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
        timeout=10,
    )
    assert wrong.status_code == 404


async def test_full_tracking_session_via_mcp_tools(stack: dict[str, Any]) -> None:
    """A real 20x session: the replay container serves the fixture, the app ingests it, and the
    MCP tools (through nginx) report live status, splits, trend and the summary."""
    manifest = json.loads((ROOT / "fixtures/sessions/run1/manifest.json").read_text())
    # restart the replay so its rebased clock starts now, then register the session in the app DB
    assert _run([*COMPOSE, "restart", "replay"], timeout=120).returncode == 0
    time.sleep(2)
    created = _exec(
        "garmin-live",
        "python",
        "-m",
        "garmin_live.admin",
        "start-internal",
        "--session-id",
        manifest["fake_session_id"],
        "--token",
        manifest["fake_token"],
        "--api-base",
        "http://replay:8765",
        "--label",
        "e2e-replay",
    )
    assert created.returncode == 0, created.stderr
    sid = json.loads(created.stdout)["session_id"]
    peak = 0.0
    async with Client(stack["mcp"]) as client:
        deadline = time.monotonic() + 240
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            peak = max(peak, _container_mem_mb())
            res = await client.call_tool("get_live_status", {"session_id": sid})
            assert not res.is_error, res.content
            status = dict(res.structured_content or {})
            if status.get("session_status") == "ended":
                break
            time.sleep(3)
        assert status.get("session_status") == "ended", status
        assert status["points"] == 72 and status["distance_m"] == 1022
        assert status["end_reason"] == "garmin_end"
        assert not {"lat", "lon", "position"} & set(status)
        splits = (
            await client.call_tool("get_splits", {"session_id": sid})
        ).structured_content or {}
        assert len(splits["splits"]) == 2 and splits["splits"][0]["complete"] is True
        trend = (
            await client.call_tool(
                "get_trend", {"session_id": sid, "metric": "pace", "window_minutes": 20}
            )
        ).structured_content or {}
        assert trend["series"]
        summary = (
            await client.call_tool("get_session_summary", {"session_id": sid})
        ).structured_content or {}
        assert summary["avg_pace_moving"] is not None and summary["hr_zones"] is not None
        listed = (await client.call_tool("list_sessions", {})).structured_content or {}
        assert any(
            s["label"] == "e2e-replay" and s["status"] == "ended" for s in listed["sessions"]
        )
        (ROOT / "fixtures" / "raw" / "e2e_tool_outputs.json").write_text(
            json.dumps(
                {
                    "status": status,
                    "splits": splits,
                    "trend": trend,
                    "summary": summary,
                    "list": listed,
                }
            )
        )
    peak = max(peak, _container_mem_mb())
    PEAK_FILE.write_text(f"{peak:.1f}\n")
    assert 0 < peak <= MEMORY_LIMIT_MB, f"peak container memory {peak:.1f} MB"


def test_selftest_inside_container(stack: dict[str, Any]) -> None:
    result = _exec("garmin-live", "python", "-m", "garmin_live.selftest")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True and report["points"] == 72


def test_privacy_of_logs_and_outputs(stack: dict[str, Any]) -> None:
    """No fixture coordinate values, no fake token/session id, no secret in logs or tool output."""
    logs = _run([*COMPOSE, "logs", "--no-color"], timeout=120).stdout
    outputs = (ROOT / "fixtures" / "raw" / "e2e_tool_outputs.json").read_text()
    manifest = json.loads((ROOT / "fixtures/sessions/run1/manifest.json").read_text())
    coords: set[str] = set()
    for rec_meta in manifest["responses"]:
        rec = json.loads((ROOT / "fixtures/sessions/run1" / rec_meta["file"]).read_text())
        if "/track-points/" in rec["request"]["url"] and rec.get("body_text"):
            for tp in json.loads(rec["body_text"]).get("trackPoints", []):
                coords.add(repr(tp["position"]["lat"]))
                coords.add(repr(tp["position"]["lon"]))
    app_logs = "\n".join(line for line in logs.splitlines() if "garmin-live" in line.split("|")[0])
    for blob, label in ((app_logs, "app logs"), (outputs, "tool outputs")):
        assert SECRET not in blob, label
        assert manifest["fake_token"] not in blob, label
        assert manifest["fake_session_id"] not in blob, label
        assert not any(c in blob for c in coords), label


async def test_root_base_path_variant(stack: dict[str, Any]) -> None:
    """Portability: the same image serves at the root when BASE_PATH is empty."""
    name = "garmin-live-root-e2e"
    _run(["docker", "rm", "-f", name])
    run = _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-p",
            "127.0.0.1:18090:8000",
            "-e",
            "BASE_PATH=",
            "-e",
            f"MCP_PATH_SECRET={SECRET}",
            "-e",
            "MCP_ALLOWED_HOSTS=localhost,127.0.0.1",
            "garmin-live:local",
        ]
    )
    assert run.returncode == 0, run.stderr
    try:
        _wait_health("http://127.0.0.1:18090/health")
        assert httpx.get("http://127.0.0.1:18090/garmin-mcp/health", timeout=5).status_code == 404
        async with Client(f"http://127.0.0.1:18090/mcp/{SECRET}") as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert len(names) == 7
        bad = _run(
            ["docker", "run", "--rm", "-e", "MCP_PATH_SECRET=short", "garmin-live:local"],
            timeout=120,
        )
        assert bad.returncode != 0 and "MCP_PATH_SECRET" in (bad.stderr + bad.stdout)
    finally:
        _run(["docker", "rm", "-f", name])
