"""Retention loop, online DB backup with pruning, and the selftest entry point."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from garmin_live import db_backup, selftest
from garmin_live.ingest.retention import purge, retention_loop
from garmin_live.store import repo as st
from garmin_live.store.db import Database
from garmin_live.store.repo import Repo
from tests.conftest import TEST_SECRET, make_point


def _old_session(repo: Repo, label: str, days: int) -> str:
    s = repo.create_session(
        label=label, garmin_session_id=label, token="t", api_base="b", poll_interval_s=20
    )
    repo.insert_trackpoints(s.id, [make_point(0, 0), make_point(10, 10)])
    repo.update_session(s.id, created_at=datetime.now(UTC) - timedelta(days=days), status=st.ENDED)
    return s.id


def test_purge_and_loop(repo: Repo) -> None:
    old = _old_session(repo, "old", 45)
    fresh = _old_session(repo, "fresh", 3)
    assert purge(repo, 30) == {"sessions": 1, "trackpoints": 2, "events": 0}
    assert repo.get_session(old) is None and repo.get_session(fresh) is not None

    async def one_iteration() -> None:
        _old_session(repo, "older", 60)
        task = asyncio.create_task(retention_loop(repo, 30, interval_s=3600))
        await asyncio.sleep(0.2)  # first purge runs immediately
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(one_iteration())
    assert repo.find_by_garmin_id("older") is None


def test_db_backup_keeps_newest_five(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "live.db")
    Repo(db).create_session(
        label="x", garmin_session_id="g", token="t", api_base="b", poll_interval_s=20
    )
    dest_dir = tmp_path / "backups"
    for i in range(7):
        dest = dest_dir / f"pre-deploy-{i:02d}.db"
        db_backup.backup(db.path, dest)
        dest.touch()
    remaining = sorted(p.name for p in dest_dir.glob("*.db"))
    assert len(remaining) == 5 and remaining[0] == "pre-deploy-02.db"
    copy = sqlite3.connect(dest_dir / "pre-deploy-06.db")
    assert copy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    copy.close()
    db.close()
    # CLI entry point
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "live.db"))
    assert db_backup.main([str(dest_dir / "cli.db")]) == 0
    assert db_backup.main([]) == 2
    monkeypatch.setenv("MCP_PATH_SECRET", "short")
    assert db_backup.main([str(dest_dir / "cli2.db")]) == 1


def test_selftest_passes_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "selftest.db"))
    monkeypatch.setenv("BASE_PATH", "/garmin-mcp")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setattr(selftest, "SPEED", 300.0)
    monkeypatch.setattr(selftest, "POLL_S", 0.2)
    assert selftest.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True and report["points"] == 72 and report["end_reason"] == "garmin_end"
    assert all(report["checks"].values())
    # the selftest session is left in the ended state in the real DB
    db = Database(tmp_path / "selftest.db")
    labels = [s.label for s in Repo(db).list_sessions()]
    assert labels == ["selftest"] and Repo(db).list_sessions()[0].status == st.ENDED
    db.close()


def test_selftest_reports_config_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("MCP_PATH_SECRET", raising=False)
    assert selftest.main() == 1
    assert "MCP_PATH_SECRET" in capsys.readouterr().err
