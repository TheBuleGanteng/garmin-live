"""Online SQLite backup: `python -m garmin_live.db_backup DEST`.

Copies the live database (DB_PATH) to DEST with SQLite's online backup API (safe while the app
is running, WAL included) and keeps only the newest 5 `*.db` files in DEST's directory.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from garmin_live.config import ConfigError, load_settings

KEEP = 5


def backup(src: str | Path, dest: str | Path, keep: int = KEEP) -> list[Path]:
    """Back up `src` to `dest`; return the backups removed to honour `keep`."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{Path(src)}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dest_path))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    backups = sorted(
        (p for p in dest_path.parent.glob("*.db") if p.resolve() != Path(src).resolve()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for old in backups[keep:]:
        old.unlink(missing_ok=True)
        removed.append(old)
    return removed


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m garmin_live.db_backup DEST", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    removed = backup(settings.db_path, args[0])
    print(f"backup written to {args[0]}; pruned {len(removed)} old backup(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
