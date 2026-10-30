"""Operator CLI (runs inside the container against the shared database).

    python -m garmin_live.admin active-labels [--exclude LABEL ...]
    python -m garmin_live.admin start-internal --session-id ID --token TOKEN \
        --api-base URL [--label L]

`active-labels` prints active session labels (the deploy pipeline's live-workout guard).
`start-internal` inserts an active session that points at an arbitrary API base — this is the
internal service path used by end-to-end tests against the replay server; it never goes through
the MCP tool and the running app adopts the session within a few seconds.
"""

from __future__ import annotations

import argparse
import json
import sys

from garmin_live.config import ConfigError, load_settings
from garmin_live.store.db import Database
from garmin_live.store.repo import Repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garmin_live.admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    labels = sub.add_parser("active-labels", help="print labels of active sessions")
    labels.add_argument("--exclude", action="append", default=[], help="label to ignore")
    start = sub.add_parser("start-internal", help="insert an active session (tests/replay only)")
    start.add_argument("--session-id", required=True)
    start.add_argument("--token", required=True)
    start.add_argument("--api-base", required=True)
    start.add_argument("--label", default="internal")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    db = Database(settings.db_path)
    try:
        repo = Repo(db)
        if args.command == "active-labels":
            print(json.dumps(repo.active_session_labels(exclude=args.exclude)))
            return 0
        session = repo.create_session(
            label=args.label,
            garmin_session_id=args.session_id,
            token=args.token,
            api_base=args.api_base,
            poll_interval_s=settings.poll_min_seconds,
        )
        print(
            json.dumps({"session_id": session.id, "label": session.label, "status": session.status})
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
