"""Application settings, loaded from environment variables.

Every deployment-specific value comes from the environment. The app fails fast at startup with a
clear message for any missing or invalid setting. See `.env.example` for the documented list.
"""

from __future__ import annotations

import ipaddress
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32
ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or invalid; the message is safe to print."""


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    """All runtime configuration. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    base_path: str = Field(default="", description="URL prefix the app is served under; '' = root")
    mcp_path_secret: str = Field(description="Unguessable path segment for the MCP endpoint")
    db_path: str = Field(default="/data/garmin.db", description="SQLite database file")
    retention_days: int = Field(default=30, ge=1, le=3650)
    athlete_max_hr: int | None = Field(default=None, description="Max HR in bpm; enables HR zones")
    fetch_strategy: Literal["http", "browser"] = "http"
    poll_min_seconds: int = Field(default=20, ge=5, le=3600)
    max_session_hours: int = Field(default=12, ge=1, le=48)
    enforce_ip_allowlist: bool = False
    allowed_ips: str = Field(default="", description="Comma-separated IPs/CIDRs")
    mcp_allowed_hosts: str = Field(
        default="localhost,127.0.0.1", description="Comma-separated hosts"
    )
    trusted_proxy_cidrs: str = Field(default="", description="Comma-separated proxy CIDRs")
    log_level: str = "INFO"

    @field_validator("base_path")
    @classmethod
    def _check_base_path(cls, value: str) -> str:
        if value in ("", "/"):
            return ""
        if not value.startswith("/") or value.endswith("/") or "//" in value or " " in value:
            raise ValueError("BASE_PATH must start with '/' and not end with '/', e.g. /garmin-mcp")
        return value

    @field_validator("mcp_path_secret")
    @classmethod
    def _check_secret(cls, value: str) -> str:
        value = value.strip()
        if len(value) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"MCP_PATH_SECRET must be at least {MIN_SECRET_LENGTH} characters "
                "(generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
            )
        if "/" in value or "?" in value or "#" in value or " " in value:
            raise ValueError(
                "MCP_PATH_SECRET must be a single URL path segment (no '/', '?', '#', spaces)"
            )
        if value.lower().startswith("replace-me"):
            raise ValueError("MCP_PATH_SECRET still has the placeholder value from .env.example")
        return value

    @field_validator("athlete_max_hr", mode="before")
    @classmethod
    def _empty_hr_is_none(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("athlete_max_hr")
    @classmethod
    def _check_hr(cls, value: int | None) -> int | None:
        if value is not None and not (100 <= value <= 250):
            raise ValueError(
                "ATHLETE_MAX_HR must be between 100 and 250 bpm, or empty to disable zones"
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        upper = value.strip().upper()
        if upper not in ALLOWED_LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {', '.join(ALLOWED_LOG_LEVELS)}")
        return upper

    @field_validator("allowed_ips", "trusted_proxy_cidrs")
    @classmethod
    def _check_networks(cls, value: str) -> str:
        for item in _split_csv(value):
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError as exc:
                raise ValueError(f"'{item}' is not a valid IP address or CIDR") from exc
        return value

    @field_validator("mcp_allowed_hosts")
    @classmethod
    def _check_hosts(cls, value: str) -> str:
        hosts = _split_csv(value)
        if not hosts:
            raise ValueError("MCP_ALLOWED_HOSTS must list at least one hostname (e.g. example.com)")
        for host in hosts:
            if "/" in host or " " in host:
                raise ValueError(f"'{host}' is not a valid hostname")
        return value

    # ---- derived views -----------------------------------------------------------------
    @property
    def allowed_ip_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(item, strict=False) for item in _split_csv(self.allowed_ips)]

    @property
    def trusted_proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [
            ipaddress.ip_network(item, strict=False)
            for item in _split_csv(self.trusted_proxy_cidrs)
        ]

    @property
    def allowed_hosts(self) -> list[str]:
        return _split_csv(self.mcp_allowed_hosts)

    @property
    def mcp_mount_path(self) -> str:
        """Path (under BASE_PATH) where the MCP endpoint lives."""
        return f"{self.base_path}/mcp/{self.mcp_path_secret}"

    def public_summary(self) -> dict[str, object]:
        """Settings safe to log at startup (no secret)."""
        return {
            "base_path": self.base_path or "/",
            "db_path": self.db_path,
            "retention_days": self.retention_days,
            "athlete_max_hr_set": self.athlete_max_hr is not None,
            "fetch_strategy": self.fetch_strategy,
            "poll_min_seconds": self.poll_min_seconds,
            "max_session_hours": self.max_session_hours,
            "enforce_ip_allowlist": self.enforce_ip_allowlist,
            "mcp_allowed_hosts": self.allowed_hosts,
            "log_level": self.log_level,
        }


def load_settings(**overrides: object) -> Settings:
    """Build `Settings` from the environment (plus explicit overrides), raising `ConfigError`."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "settings"
            name = loc.upper()
            msg = err["msg"]
            if err["type"] == "missing":
                msg = "is required"
            lines.append(f"  {name}: {msg}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from None
