"""Self-test: replay a fixture through the real ingest path and check that metrics populate.

`python -m garmin_live.selftest` (inside the container, or locally with the app's env):

1. starts the replay server in-process on 127.0.0.1 at 20x,
2. creates a session labelled `selftest` through the internal service API (the only path that
   may bypass the LiveTrack host check; the MCP tool cannot),
3. waits for ingest and checks that metrics are populated,
4. leaves the session in `ended` state, prints a JSON report and exits 0 (ok) or 1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from garmin_live.app import build_components
from garmin_live.config import ConfigError, load_settings
from garmin_live.logging import configure_logging
from garmin_live.replay.server import ReplayConfig, ReplayServer, load_fixture
from garmin_live.store import repo as st

SELFTEST_LABEL = "selftest"
FIXTURE = "run1"
SPEED = 20.0
POLL_S = 2.0
TIMEOUT_S = 180.0

log = logging.getLogger(__name__)


async def run_selftest() -> dict[str, object]:
    settings = load_settings()
    configure_logging(settings.log_level, secrets=[settings.mcp_path_secret])
    fixture = load_fixture(FIXTURE)
    replay = ReplayServer(fixture, ReplayConfig(speed=SPEED, start_offset_s=5), port=0)
    await replay.start()
    components = build_components(settings)
    components.poller.poll_min_seconds = POLL_S
    components.poller.session_check_s = POLL_S * 2
    report: dict[str, object] = {"fixture": FIXTURE, "speed": SPEED, "ok": False, "checks": {}}
    checks: dict[str, bool] = {}
    report["checks"] = checks
    try:
        started = await components.service.start_tracking_internal(
            fixture.session_id, fixture.token, replay.base_url, label=SELFTEST_LABEL
        )
        sid = str(started["session_id"])
        report["session_id"] = sid
        t0 = time.monotonic()
        while time.monotonic() - t0 < TIMEOUT_S:
            session = components.repo.get_session(sid)
            if session is not None and not session.is_active:
                break
            await asyncio.sleep(1)
        session = components.repo.get_session(sid)
        assert session is not None
        points = components.repo.count_trackpoints(sid)
        status = components.service.get_live_status(sid)
        summary = components.service.get_session_summary(sid)
        splits = components.service.get_splits(sid, 1000.0)["splits"]
        events = {e.type for e in components.repo.list_events(sid)}
        checks["session_ended_by_garmin_signal"] = (
            session.status == st.ENDED and session.end_reason == "garmin_end"
        )
        checks["all_points_ingested"] = points == len(fixture.points)
        checks["distance_populated"] = float(str(status.get("distance_m") or 0)) > 0
        checks["pace_populated"] = status.get("avg_pace") is not None
        checks["hr_populated"] = status.get("avg_hr") is not None
        checks["splits_populated"] = isinstance(splits, list) and len(splits) >= 1
        checks["summary_populated"] = summary.get("avg_pace_moving") is not None
        checks["events_emitted"] = {"new_points", "session_ended"} <= events
        checks["no_ingest_errors"] = session.error_count == 0
        report["points"] = points
        report["status"] = session.status
        report["end_reason"] = session.end_reason
        report["elapsed_real_s"] = round(time.monotonic() - t0, 1)
        report["ok"] = all(checks.values())
    finally:
        await components.poller.stop()
        components.db.close()
        await replay.stop()
    return report


def main() -> int:
    try:
        report = asyncio.run(run_selftest())
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
