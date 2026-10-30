"""Access control for the MCP endpoint, kept in one module so it can be swapped for OAuth later.

v1 model: the MCP app lives at `{BASE_PATH}/mcp/{MCP_PATH_SECRET}`. The secret is compared in
constant time; any other path under `{BASE_PATH}/mcp/` returns a plain 404 so probing reveals
nothing. An optional IP allow-list (off by default) reads the client IP from `X-Real-IP` only
when the connection comes from a trusted proxy.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
from collections.abc import Sequence

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def constant_time_equals(provided: str, secret: str) -> bool:
    """Length-independent constant-time comparison (hash both sides first)."""
    a = hashlib.sha256(provided.encode("utf-8")).digest()
    b = hashlib.sha256(secret.encode("utf-8")).digest()
    return hmac.compare_digest(a, b)


def route_path(scope: Scope) -> str:
    """The part of the request path that belongs to this app (full path minus mount prefix)."""
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path) :]
    return path


async def _not_found(scope: Scope, receive: Receive, send: Send) -> None:
    response = PlainTextResponse("Not Found", status_code=404)
    await response(scope, receive, send)


class SecretPathGate:
    """ASGI app mounted at `{BASE_PATH}/mcp`: forwards `/{secret}` to the MCP app, else 404."""

    def __init__(self, secret: str, inner: ASGIApp) -> None:
        self._secret = secret
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        # Starlette keeps the full path in scope["path"] and records the mount prefix in
        # scope["root_path"]; the part that belongs to us is their difference.
        segment = route_path(scope).strip("/")
        if not segment or "/" in segment or not constant_time_equals(segment, self._secret):
            await _not_found(scope, receive, send)
            return
        # Forward so the inner app sees route path "/" (its single route) without redirecting.
        forwarded = dict(scope)
        forwarded["root_path"] = scope.get("root_path", "") + "/" + segment
        forwarded["path"] = forwarded["root_path"] + "/"
        forwarded["raw_path"] = forwarded["path"].encode("utf-8")
        await self._inner(forwarded, receive, send)


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _in_any(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: Sequence[Network]) -> bool:
    return any(ip in net for net in networks)


def client_ip(scope: Scope, trusted_proxies: Sequence[Network]) -> str | None:
    """Determine the client IP.

    The peer address is used unless the peer is a trusted proxy, in which case `X-Real-IP` is
    used. With no trusted proxies configured, peers on private networks (Docker bridge) are
    trusted, since the app is only ever reached through the reverse proxy.
    """
    client = scope.get("client")
    peer = _parse_ip(client[0]) if client else None
    if peer is None:
        return None
    trusted = (
        _in_any(peer, trusted_proxies) if trusted_proxies else peer.is_private or peer.is_loopback
    )
    if trusted:
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])
        }
        real = _parse_ip(headers.get("x-real-ip"))
        if real is not None:
            return str(real)
    return str(peer)


class IpAllowlist:
    """ASGI wrapper: 404 unless the client IP is in the allow-list."""

    def __init__(
        self, inner: ASGIApp, allowed: Sequence[Network], trusted_proxies: Sequence[Network]
    ) -> None:
        self._inner = inner
        self._allowed = list(allowed)
        self._trusted = list(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return
        ip_text = client_ip(scope, self._trusted)
        ip = _parse_ip(ip_text)
        if ip is None or not _in_any(ip, self._allowed):
            log.warning("mcp request denied by ip allowlist")
            await _not_found(scope, receive, send)
            return
        await self._inner(scope, receive, send)
