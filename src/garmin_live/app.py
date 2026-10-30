"""FastAPI application served natively under BASE_PATH, with the MCP app at the secret path.

Run with exactly one uvicorn worker: the poller runs in-process and must not be duplicated.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI
from starlette.types import ASGIApp

from garmin_live import __version__
from garmin_live.auth import IpAllowlist, SecretPathGate
from garmin_live.config import ConfigError, Settings, load_settings
from garmin_live.events.bus import EventBus
from garmin_live.events.notifier import NoopNotifier, Notifier
from garmin_live.ingest.poller import FetcherFactory, Poller, default_fetcher_factory
from garmin_live.ingest.retention import retention_loop
from garmin_live.livetrack.fetch_browser import BrowserFetcher
from garmin_live.logging import configure_logging
from garmin_live.mcp_server.server import build_mcp_asgi_app, build_mcp_server
from garmin_live.service import TrackingService
from garmin_live.store.db import Database
from garmin_live.store.repo import Repo

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Components:
    settings: Settings
    db: Database
    repo: Repo
    poller: Poller
    service: TrackingService


def build_components(
    settings: Settings,
    *,
    notifier: Notifier | None = None,
    fetcher_factory: FetcherFactory | None = None,
) -> Components:
    db = Database(settings.db_path)
    repo = Repo(db)
    bus = EventBus(repo, notifier or NoopNotifier())
    if fetcher_factory is None:
        if settings.fetch_strategy == "browser":

            def fetcher_factory(session):  # type: ignore[no-untyped-def]
                return BrowserFetcher(
                    session.garmin_session_id, session.token, base_url=session.api_base
                )

        else:
            fetcher_factory = default_fetcher_factory
    poller = Poller(
        repo,
        bus,
        poll_min_seconds=settings.poll_min_seconds,
        max_session_hours=settings.max_session_hours,
        fetcher_factory=fetcher_factory,
    )
    service = TrackingService(repo, poller, athlete_max_hr=settings.athlete_max_hr)
    return Components(settings=settings, db=db, repo=repo, poller=poller, service=service)


def create_app(
    settings: Settings | None = None,
    *,
    notifier: Notifier | None = None,
    fetcher_factory: FetcherFactory | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, secrets=[settings.mcp_path_secret])
    components = build_components(settings, notifier=notifier, fetcher_factory=fetcher_factory)
    mcp = build_mcp_server(components.service)
    mcp_asgi = build_mcp_asgi_app(mcp, settings)
    base = settings.base_path

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("starting garmin-live %s %s", __version__, settings.public_summary())
        background = [
            asyncio.create_task(
                retention_loop(components.repo, settings.retention_days), name="retention"
            ),
            asyncio.create_task(components.poller.adopt_loop(), name="adopt"),
        ]
        async with mcp.session_manager.run():
            await components.poller.start()
            try:
                yield
            finally:
                for task in background:
                    task.cancel()
                for task in background:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await components.poller.stop()
                components.db.close()

    app = FastAPI(
        title="garmin-live",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,  # /mcp must 404, not redirect to /mcp/
    )
    app.state.components = components
    app.state.mcp = mcp

    @app.get(f"{base}/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": __version__,
            "active_sessions": len(components.repo.active_sessions()),
        }

    gate: ASGIApp = SecretPathGate(settings.mcp_path_secret, mcp_asgi)
    if settings.enforce_ip_allowlist:
        gate = IpAllowlist(gate, settings.allowed_ip_networks, settings.trusted_proxy_networks)
    app.mount(f"{base}/mcp", gate, name="mcp")
    return app


def main() -> None:
    """Console entry point: one uvicorn worker on port 8000."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        logging.basicConfig(level="ERROR")
        logging.getLogger(__name__).error("%s", exc)
        raise SystemExit(2) from None
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104 - container port, reached only via the reverse proxy
        port=8000,
        workers=1,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )


if __name__ == "__main__":
    main()
