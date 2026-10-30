"""Log redaction: tokens, session ids, the MCP secret and coordinates never reach the output."""

from __future__ import annotations

import logging

from garmin_live.logging import RedactionFilter, configure_logging, get_logger
from tests.conftest import TEST_SECRET


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_filter_masks_tokens_paths_and_coordinates() -> None:
    f = RedactionFilter([TEST_SECRET])
    rec = _record(
        "GET https://livetrack.garmin.com/session/%s/token/%s?token=%s lat=%s lon=%s",
        "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000",
        "FAKETOKENnotreal0000ABCDEFGHI",
        "FAKETOKENnotreal0000ABCDEFGHI",
        "-33.123456",
        "-40.654321",
    )
    assert f.filter(rec) is True
    text = rec.getMessage()
    assert "FAKETOKENnotreal0000ABCDEFGHI" not in text
    assert "0f0f0f0f-aaaa-4bbb-8ccc-dddddddd0000" not in text
    assert "-33.123456" not in text and "-40.654321" not in text
    assert text.count("<redacted>") >= 4  # the /token/ mask also swallows the adjacent query
    rec2 = _record(f"mcp mounted at /garmin-mcp/mcp/{TEST_SECRET} and secret={TEST_SECRET}")
    f.filter(rec2)
    assert TEST_SECRET not in rec2.getMessage()
    rec3 = _record('{"position": {"lat": 1.5, "lon": 2.5}}')
    f.filter(rec3)
    assert "1.5" not in rec3.getMessage() and "2.5" not in rec3.getMessage()


def test_filter_survives_bad_format_args() -> None:
    f = RedactionFilter()
    rec = _record("%d points", "not-a-number")
    assert f.filter(rec) is True
    assert "points" in str(rec.msg)


def test_configure_logging_installs_filter(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("DEBUG", secrets=[TEST_SECRET])
    log = get_logger("garmin_live.test")
    log.info("secret is %s", TEST_SECRET)
    err = capsys.readouterr().err
    assert "secret is <redacted>" in err
    assert TEST_SECRET not in err
    assert logging.getLogger("httpx").level >= logging.WARNING
    configure_logging("INFO")  # idempotent: handlers replaced, not duplicated
    assert len(logging.getLogger().handlers) == 1
