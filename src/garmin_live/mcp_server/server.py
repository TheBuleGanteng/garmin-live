"""Build the MCP server and its Streamable HTTP ASGI app.

Transport choice: **stateless** Streamable HTTP (`stateless_http=True`). With a single uvicorn
worker either mode works; stateless keeps no per-client session table, so there is nothing to
expire or lose on restart, and every request is independently routable by the secret path.
Claude.ai's custom connectors work with stateless servers.

Host/Origin (DNS-rebinding) protection is on; the allowed hosts come from `MCP_ALLOWED_HOSTS`.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from garmin_live import __version__
from garmin_live.config import Settings
from garmin_live.mcp_server.tools import register_tools
from garmin_live.service import TrackingService

INSTRUCTIONS = (
    "garmin-live exposes a single athlete's Garmin LiveTrack session (a live running/cycling "
    "workout) as read-only tools. Workflow: when given a LiveTrack share URL call start_tracking "
    "once; then answer questions with get_live_status (now), get_splits (per-km), get_trend "
    "(recent drift of pace/hr/elevation/cadence) and get_session_summary (whole workout). "
    "session_id is optional and defaults to the most recent active session. Units are metric, "
    "pace is m:ss per km, and fields Garmin did not provide are null with a reason. No "
    "coordinates are ever returned. Data arrives every ~10-20 s while the watch is sending; "
    "check data_freshness_seconds/stale before interpreting a reading as current."
)


def build_mcp_server(service: TrackingService) -> MCPServer:
    server = MCPServer(
        name="garmin-live",
        title="Garmin LiveTrack (read-only)",
        version=__version__,
        instructions=INSTRUCTIONS,
    )
    register_tools(server, service)
    return server


def transport_security_for(settings: Settings) -> TransportSecuritySettings:
    hosts: list[str] = []
    origins: list[str] = []
    for host in settings.allowed_hosts:
        hosts += [host, f"{host}:*"]
        origins += [f"https://{host}", f"https://{host}:*", f"http://{host}", f"http://{host}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def build_mcp_asgi_app(server: MCPServer, settings: Settings) -> Starlette:
    """Starlette app serving MCP at its root path. Its session manager must be run by the parent
    app's lifespan (`server.session_manager.run()`)."""
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=False,
        transport_security=transport_security_for(settings),
        host="0.0.0.0",  # noqa: S104 - informational only; security settings are explicit above
    )
