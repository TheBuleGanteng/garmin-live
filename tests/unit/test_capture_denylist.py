"""The capture tool must register a share URL's session ID and token in .secret-denylist.

Loads tools/capture_livetrack.py by path (it is a script, not a package) and exercises the
denylist registration with obviously fake identifiers in a temporary directory.
"""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "tools" / "capture_livetrack.py"
CHECK_SCRIPT = REPO_ROOT / "tools" / "check_denylist.sh"

FAKE_SESSION = "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000"
FAKE_TOKEN = "FAKETOKEN-not-real-0000-ABCDEFGHIJ"
SHARE_URL = f"https://livetrack.garmin.com/session/{FAKE_SESSION}/token/{FAKE_TOKEN}"


@pytest.fixture(scope="module")
def capture() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capture_livetrack", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses in the script need the module registered
    spec.loader.exec_module(module)
    return module


def _entries(path: Path) -> list[str]:
    return [
        ln.split("#", 1)[0].strip()
        for ln in path.read_text().splitlines()
        if ln.split("#", 1)[0].strip()
    ]


def test_extract_secret_segments_finds_session_and_token(capture: ModuleType) -> None:
    assert capture.extract_secret_segments(SHARE_URL) == [FAKE_SESSION, FAKE_TOKEN]


def test_extract_secret_segments_falls_back_to_id_like_segments(capture: ModuleType) -> None:
    url = f"https://livetrack.garmin.com/share/{FAKE_SESSION}"
    assert capture.extract_secret_segments(url) == [FAKE_SESSION]
    assert capture.extract_secret_segments("https://livetrack.garmin.com/") == []


def test_register_appends_ids_once_and_keeps_existing(capture: ModuleType, tmp_path: Path) -> None:
    denylist = tmp_path / ".secret-denylist"
    denylist.write_text("# comment\n203.0.113.9\n!LICENSE\n")

    assert capture.register_in_denylist(SHARE_URL, denylist) == 2
    entries = _entries(denylist)
    assert entries == ["203.0.113.9", "!LICENSE", FAKE_SESSION, FAKE_TOKEN]
    assert stat.S_IMODE(denylist.stat().st_mode) == 0o600

    # idempotent: a second run (e.g. --probe-only later) adds nothing and duplicates nothing
    assert capture.register_in_denylist(SHARE_URL, denylist) == 0
    assert _entries(denylist) == entries


def test_register_creates_missing_file(capture: ModuleType, tmp_path: Path) -> None:
    denylist = tmp_path / "nested" / ".secret-denylist"
    assert capture.register_in_denylist(SHARE_URL, denylist) == 2
    assert _entries(denylist) == [FAKE_SESSION, FAKE_TOKEN]


def test_register_prints_nothing(
    capture: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capture.register_in_denylist(SHARE_URL, tmp_path / ".secret-denylist")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_check_denylist_blocks_registered_token_in_staged_content(
    capture: ModuleType, tmp_path: Path
) -> None:
    """After registration, the denylist check refuses a staged file containing the token."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    denylist = repo / ".secret-denylist"
    capture.register_in_denylist(SHARE_URL, denylist)

    (repo / "leak.txt").write_text(f"token here: {FAKE_TOKEN}\n")
    _git(repo, "add", "leak.txt")
    res = subprocess.run(
        ["bash", str(CHECK_SCRIPT), "staged"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert res.returncode == 1
    assert "leak.txt" in res.stderr
    assert FAKE_TOKEN not in res.stdout + res.stderr  # never prints the value

    # a clean file passes
    _git(repo, "reset", "-q")
    (repo / "ok.txt").write_text("nothing secret\n")
    _git(repo, "add", "ok.txt")
    res = subprocess.run(
        ["bash", str(CHECK_SCRIPT), "staged"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert res.returncode == 0, res.stderr


def test_check_denylist_fails_closed_when_missing_or_empty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    res = subprocess.run(
        ["bash", str(CHECK_SCRIPT), "staged"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert res.returncode == 1 and "missing" in res.stderr
    (repo / ".secret-denylist").write_text("# only comments\n")
    res = subprocess.run(
        ["bash", str(CHECK_SCRIPT), "staged"], cwd=repo, capture_output=True, text=True, check=False
    )
    assert res.returncode == 1 and "no literals" in res.stderr
