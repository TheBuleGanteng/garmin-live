#!/usr/bin/env python3
"""MCP smoke test: `tools/mcp_smoke.py --url URL [--expect-session LABEL]`.

Connects as an MCP client over Streamable HTTP, runs `initialize`, lists the tools, calls
`list_sessions` and `get_live_status`, and checks that a wrong secret returns 404. Prints a
short report; never prints the secret (URLs are shown with the last path segment masked).
Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from mcp import Client

EXPECTED_TOOLS = {
    "start_tracking",
    "list_sessions",
    "get_live_status",
    "get_splits",
    "get_trend",
    "get_session_summary",
    "stop_tracking",
}


def masked(url: str) -> str:
    parts = urlsplit(url)
    segments = parts.path.rstrip("/").split("/")
    if segments:
        segments[-1] = "<secret>"
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), "", ""))


def wrong_secret_url(url: str) -> str:
    parts = urlsplit(url)
    segments = parts.path.rstrip("/").split("/")
    segments[-1] = "wrong-secret-not-real-" + "0" * 24
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), parts.query, ""))


def result_payload(result: Any) -> Any:
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def run(url: str, expect_session: str | None) -> int:
    failures: list[str] = []
    print(f"[smoke] connecting to {masked(url)}")
    async with Client(url) as client:
        info = client.server_info
        print(
            f"[smoke] initialize ok: server={getattr(info, 'name', '?')} "
            f"protocol={client.protocol_version}"
        )
        tools = {t.name for t in (await client.list_tools()).tools}
        missing = EXPECTED_TOOLS - tools
        print(f"[smoke] tools: {sorted(tools)}")
        if missing:
            failures.append(f"missing tools: {sorted(missing)}")

        sessions_result = await client.call_tool("list_sessions", {})
        sessions = result_payload(sessions_result)
        if sessions_result.is_error:
            failures.append(f"list_sessions error: {sessions}")
        rows = sessions.get("sessions", []) if isinstance(sessions, dict) else []
        print(f"[smoke] list_sessions: {len(rows)} session(s)")
        if expect_session is not None and not any(r.get("label") == expect_session for r in rows):
            failures.append(f"expected a session labelled {expect_session!r}")

        status_result = await client.call_tool("get_live_status", {})
        status = result_payload(status_result)
        if status_result.is_error:
            # No sessions yet is a valid state; anything else is a failure.
            text = str(status)
            if "No sessions yet" in text and not rows:
                print("[smoke] get_live_status: no sessions yet (ok)")
            else:
                failures.append(f"get_live_status error: {text}")
        else:
            keys = sorted(status.keys()) if isinstance(status, dict) else status
            print(f"[smoke] get_live_status keys: {keys}")
            if isinstance(status, dict) and any(k in status for k in ("lat", "lon", "position")):
                failures.append("get_live_status leaked coordinates")

    # wrong secret → 404 (no 401; nothing revealed)
    bad = wrong_secret_url(url)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
        resp = await http.post(
            bad,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
    print(f"[smoke] wrong secret → {resp.status_code}")
    if resp.status_code != 404:
        failures.append(f"wrong secret returned {resp.status_code}, expected 404")

    if failures:
        for f in failures:
            print(f"[smoke] FAIL: {f}")
        return 1
    print("[smoke] OK")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--url", required=True, help="full MCP endpoint URL including the secret path segment"
    )
    parser.add_argument(
        "--expect-session", default=None, help="fail unless a session with this label exists"
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.url, args.expect_session))
    except Exception as exc:
        print(f"[smoke] FAIL: {type(exc).__name__}: {str(exc)[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
