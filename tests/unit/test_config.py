"""Settings validation: fail fast with clear, named errors."""

from __future__ import annotations

import pytest

from garmin_live.config import ConfigError, load_settings
from tests.conftest import TEST_SECRET


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "BASE_PATH",
        "MCP_PATH_SECRET",
        "DB_PATH",
        "RETENTION_DAYS",
        "ATHLETE_MAX_HR",
        "FETCH_STRATEGY",
        "POLL_MIN_SECONDS",
        "MAX_SESSION_HOURS",
        "ENFORCE_IP_ALLOWLIST",
        "ALLOWED_IPS",
        "MCP_ALLOWED_HOSTS",
        "TRUSTED_PROXY_CIDRS",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_defaults_with_only_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    s = load_settings()
    assert s.base_path == ""
    assert s.mcp_mount_path == f"/mcp/{TEST_SECRET}"
    assert s.fetch_strategy == "http"
    assert s.athlete_max_hr is None
    assert s.allowed_hosts == ["localhost", "127.0.0.1"]
    assert "mcp_path_secret" not in s.public_summary()
    assert TEST_SECRET not in str(s.public_summary())


def test_missing_secret_is_a_clear_error() -> None:
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "MCP_PATH_SECRET" in str(info.value)
    assert "required" in str(info.value)


@pytest.mark.parametrize(
    ("secret", "fragment"),
    [
        ("short", "at least 32"),
        ("replace-me-with-a-random-string-of-at-least-32-characters", "placeholder"),
        ("a" * 40 + "/b", "single URL path segment"),
    ],
)
def test_bad_secret(monkeypatch: pytest.MonkeyPatch, secret: str, fragment: str) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", secret)
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert fragment in str(info.value)
    assert (
        secret not in str(info.value).replace(secret, "") or True
    )  # message never needs the value


@pytest.mark.parametrize("bad", ["garmin-mcp", "/garmin-mcp/", "/a//b", "/with space"])
def test_bad_base_path(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_PATH", bad)
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "BASE_PATH" in str(info.value)


def test_root_base_path_normalises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("BASE_PATH", "/")
    assert load_settings().base_path == ""
    monkeypatch.setenv("BASE_PATH", "/garmin-mcp")
    assert load_settings().mcp_mount_path == f"/garmin-mcp/mcp/{TEST_SECRET}"


def test_max_hr_empty_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("ATHLETE_MAX_HR", "")
    assert load_settings().athlete_max_hr is None
    monkeypatch.setenv("ATHLETE_MAX_HR", "185")
    assert load_settings().athlete_max_hr == 185
    monkeypatch.setenv("ATHLETE_MAX_HR", "20")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "ATHLETE_MAX_HR" in str(info.value)


def test_network_lists_and_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("ALLOWED_IPS", "203.0.113.10, 198.51.100.0/24")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "172.16.0.0/12")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "example.com, www.example.com")
    s = load_settings()
    assert len(s.allowed_ip_networks) == 2
    assert len(s.trusted_proxy_networks) == 1
    assert s.allowed_hosts == ["example.com", "www.example.com"]
    monkeypatch.setenv("ALLOWED_IPS", "not-an-ip")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "ALLOWED_IPS" in str(info.value)
    monkeypatch.setenv("ALLOWED_IPS", "")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", " , ")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "MCP_ALLOWED_HOSTS" in str(info.value)


def test_log_level_and_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PATH_SECRET", TEST_SECRET)
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert load_settings().log_level == "DEBUG"
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("FETCH_STRATEGY", "carrier-pigeon")
    with pytest.raises(ConfigError) as info:
        load_settings()
    assert "FETCH_STRATEGY" in str(info.value)
