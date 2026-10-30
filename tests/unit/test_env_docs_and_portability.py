"""Portability gates that are cheap enough to run as tests:

* every Settings field appears in .env.example and in the README config table, and vice versa;
* no deployment-specific strings in src/, tests/, docker/, deploy/, .env.example;
* no root-absolute routes in the app (everything is built from BASE_PATH).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from garmin_live.config import Settings

ROOT = Path(__file__).resolve().parents[2]
# Assembled from fragments so this file does not itself contain the forbidden literals.
FORBIDDEN = "|".join(["kebay" + "oran", "thebule" + "ganteng", "/home" + "/"])


def _env_example_vars() -> set[str]:
    out = set()
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            out.add(line.split("=", 1)[0].strip())
    return out


def _readme_config_vars() -> set[str]:
    text = (ROOT / "README.md").read_text()
    section = text.split("## Configuration", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([A-Z_]+)`", section, flags=re.MULTILINE))


def test_settings_env_example_and_readme_agree() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    assert fields == _env_example_vars()
    assert fields == _readme_config_vars()


def test_no_deployment_specific_strings() -> None:
    targets = ["src", "tests", "docker", "deploy", ".env.example"]
    result = subprocess.run(
        [
            "grep",
            "-r",
            "-i",
            "-l",
            "-I",
            "--exclude-dir=__pycache__",
            "-E",
            FORBIDDEN,
            *targets,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"deployment-specific strings in: {result.stdout}"


def test_no_root_absolute_routes_outside_base_path() -> None:
    """Routes and mounts must be built from settings.base_path, never literal root paths."""
    app_src = (ROOT / "src/garmin_live/app.py").read_text()
    for literal in ('"/health"', '"/mcp"', "'/health'", "'/mcp'"):
        assert literal not in app_src
    assert 'f"{base}/health"' in app_src and 'f"{base}/mcp"' in app_src
    # nothing else in src registers HTTP routes
    others = [
        p
        for p in (ROOT / "src/garmin_live").rglob("*.py")
        if p.name != "app.py" and "replay" not in p.parts
    ]
    for path in others:
        text = path.read_text()
        assert "@app.get(" not in text and "app.mount(" not in text, path
