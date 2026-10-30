"""Secret-path gate and IP allow-list behaviour (ASGI level)."""

from __future__ import annotations

import ipaddress

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from garmin_live.auth import IpAllowlist, SecretPathGate, client_ip, constant_time_equals
from tests.conftest import TEST_SECRET


def _inner() -> Starlette:
    async def root(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "root_path": request.scope.get("root_path")})

    return Starlette(routes=[Route("/", root, methods=["GET", "POST"])])


def _app(
    secret: str = TEST_SECRET, base: str = "/garmin-mcp", allow: list[str] | None = None
) -> Starlette:
    gate = SecretPathGate(secret, _inner())
    if allow is not None:
        gate = IpAllowlist(  # type: ignore[assignment]
            gate,
            [ipaddress.ip_network(a) for a in allow],
            [ipaddress.ip_network("10.0.0.0/8")],
        )
    return Starlette(routes=[Mount(f"{base}/mcp", app=gate)])


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc")
    assert not constant_time_equals("abc", "abd")
    assert not constant_time_equals("", "abc")
    assert not constant_time_equals("abc" * 20, "abc")


def test_correct_secret_reaches_inner_app_without_redirect() -> None:
    c = TestClient(_app())
    r = c.get(f"/garmin-mcp/mcp/{TEST_SECRET}", follow_redirects=False)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["root_path"] == f"/garmin-mcp/mcp/{TEST_SECRET}"
    r2 = c.post(f"/garmin-mcp/mcp/{TEST_SECRET}/", follow_redirects=False)
    assert r2.status_code == 200


def test_wrong_or_partial_secret_is_404_not_401() -> None:
    c = TestClient(_app())
    for path in (
        "/garmin-mcp/mcp/",
        "/garmin-mcp/mcp/wrong-secret-not-real-000000000000000000",
        f"/garmin-mcp/mcp/{TEST_SECRET[:-1]}",
        f"/garmin-mcp/mcp/{TEST_SECRET}x",
        f"/garmin-mcp/mcp/{TEST_SECRET}/extra",
        f"/garmin-mcp/mcp/{TEST_SECRET.upper()}",
    ):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 404, path
        assert r.text == "Not Found"
        assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_root_base_path_mount() -> None:
    c = TestClient(_app(base=""))
    assert c.get(f"/mcp/{TEST_SECRET}").status_code == 200
    assert c.get("/mcp/nope").status_code == 404


def test_ip_allowlist_uses_x_real_ip_only_from_trusted_proxy() -> None:
    c = TestClient(_app(allow=["203.0.113.0/24"]))
    # the TestClient peer is 'testclient' (not an IP) → denied
    assert c.get(f"/garmin-mcp/mcp/{TEST_SECRET}").status_code == 404
    # direct scope-level checks for the IP logic
    scope_trusted = {"client": ("10.1.2.3", 1), "headers": [(b"x-real-ip", b"203.0.113.9")]}
    assert client_ip(scope_trusted, [ipaddress.ip_network("10.0.0.0/8")]) == "203.0.113.9"
    scope_untrusted = {"client": ("198.51.100.7", 1), "headers": [(b"x-real-ip", b"203.0.113.9")]}
    assert client_ip(scope_untrusted, [ipaddress.ip_network("10.0.0.0/8")]) == "198.51.100.7"
    # with no trusted proxies configured, private peers are trusted
    assert client_ip(scope_trusted, []) == "203.0.113.9"
    assert client_ip({"client": None, "headers": []}, []) is None
    assert (
        client_ip({"client": ("10.1.2.3", 1), "headers": [(b"x-real-ip", b"garbage")]}, [])
        == "10.1.2.3"
    )
