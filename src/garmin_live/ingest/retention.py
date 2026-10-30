"""Retention job: purge sessions, points and events older than RETENTION_DAYS."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from garmin_live.store.repo import Repo

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 6 * 3600.0


def purge(repo: Repo, retention_days: int) -> dict[str, int]:
    removed = repo.purge_older_than(retention_days)
    if any(removed.values()):
        log.info("retention: removed %s", removed)
    return removed


async def retention_loop(
    repo: Repo, retention_days: int, interval_s: float = DEFAULT_INTERVAL_S
) -> None:
    """Run `purge` at startup and then every `interval_s` seconds until cancelled."""
    while True:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(purge, repo, retention_days)
        await asyncio.sleep(interval_s)
