"""Operator CLI: active-labels and start-internal against the configured database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from garmin_live import admin
from garmin_live.store.db import Database
from garmin_live.store.repo import Repo
from tests.conftest import TEST_SECRET


def test_admin_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "admin.db"))
    assert admin.main(["active-labels"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    rc = admin.main(
        [
            "start-internal",
            "--session-id",
            "00000000-0000-4000-8000-00000000f1x7",
            "--token",
            "FAKETOKEN-not-real",
            "--api-base",
            "http://replay:8765",
            "--label",
            "e2e",
        ]
    )
    assert rc == 0
    created = json.loads(capsys.readouterr().out)
    assert created["label"] == "e2e" and created["status"] == "active"
    assert admin.main(["active-labels", "--exclude", "selftest"]) == 0
    assert json.loads(capsys.readouterr().out) == ["e2e"]
    db = Database(tmp_path / "admin.db")
    row = Repo(db).get_session(created["session_id"])
    assert row is not None and row.api_base == "http://replay:8765"
    db.close()
    monkeypatch.setenv("MCP_PATH_SECRET", "short")
    assert admin.main(["active-labels"]) == 1
