"""Logging setup with a redaction filter.

Nothing in this app should log coordinates, LiveTrack tokens, session IDs or the MCP path secret.
Call sites avoid logging them in the first place; the filter below is a safety net that masks
token-like query parameters, long opaque strings next to `token`, and the MCP secret if present.
"""

from __future__ import annotations

import logging
import re
import sys

_TOKEN_QUERY = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)
_TOKEN_PATH = re.compile(r"(/token/)[^/\s\"']+", re.IGNORECASE)
_SESSION_PATH = re.compile(r"(/session(?:s)?/)[0-9a-fA-F-]{36}")
_MCP_PATH = re.compile(r"(/mcp/)[^/\s\"']+")
_LATLON = re.compile(
    r"""(["']?(?:lat|lon|latitude|longitude)["']?\s*[:=]\s*)-?\d+\.\d+""", re.IGNORECASE
)


class RedactionFilter(logging.Filter):
    """Masks secrets and coordinates in log messages and arguments."""

    def __init__(self, extra_literals: list[str] | None = None) -> None:
        super().__init__()
        self._literals = [lit for lit in (extra_literals or []) if lit]

    def _scrub(self, text: str) -> str:
        text = _TOKEN_QUERY.sub(r"\1<redacted>", text)
        text = _TOKEN_PATH.sub(r"\1<redacted>", text)
        text = _SESSION_PATH.sub(r"\1<redacted>", text)
        text = _MCP_PATH.sub(r"\1<redacted>", text)
        text = _LATLON.sub(r"\1<redacted>", text)
        for lit in self._literals:
            text = text.replace(lit, "<redacted>")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            try:
                record.msg = record.getMessage()
            except Exception:
                record.msg = str(record.msg)
            record.args = None
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        return True


def configure_logging(level: str = "INFO", *, secrets: list[str] | None = None) -> None:
    """Configure root logging to stderr with the redaction filter installed."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactionFilter(secrets))
    root.addHandler(handler)
    # Quiet noisy libraries; our own loggers inherit the root level.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
