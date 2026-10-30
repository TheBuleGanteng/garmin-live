#!/usr/bin/env python3
"""Turn a raw LiveTrack capture into a publishable, privacy-scrubbed fixture.

Input:  ``fixtures/raw/<NAME>/`` (gitignored; written by ``capture_livetrack.py``)
Output: ``fixtures/sessions/<NAME>/`` (committed to a public repo)

The output must not let anyone locate or date the activity, or replay the real
session. Transformations (see ``scrub_report.md`` in the output):

* IDs      - the real session ID and token are replaced with fixed fakes everywhere
             (URLs, headers, bodies). Cookies and auth headers are dropped.
* Geometry - the real route is discarded. Coordinates are regenerated as a synthetic
             path from a fixed origin in open ocean that preserves every segment's
             distance but uses invented headings.
* Elevation- shifted by a fixed offset (gain/loss preserved).
* Time     - shifted so the session starts at 2020-01-01T06:00:00Z (intervals kept).
* Heart rate - replaced by a smooth synthetic series (``--keep-hr`` keeps the real one).
* Identity - names, emails, device serials, user/profile IDs removed.

The scrubber changes values, never schema: the output must still parse with the
real parser. A self-check fails the run if any original token, session ID,
coordinate, real date, or ``.secret-denylist`` entry survives in the output.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = REPO_ROOT / "fixtures" / "raw"
OUT_ROOT = REPO_ROOT / "fixtures" / "sessions"
DENYLIST = REPO_ROOT / ".secret-denylist"

FAKE_SESSION_ID = "00000000-0000-4000-8000-00000000f1x7"
FAKE_TOKEN = "FAKETOKENFAKETOKENFAKETOKEN000"  # noqa: S105 - obviously fake placeholder
FAKE_START = datetime(2020, 1, 1, 6, 0, 0, tzinfo=UTC)
# Open ocean, South Atlantic, far from any coast.
FAKE_ORIGIN_LAT = -33.0
FAKE_ORIGIN_LON = -40.0
FAKE_FIRST_ALTITUDE_M = 100.0
FAKE_GUID = "11111111-1111-4111-8111-111111111111"
FAKE_IDENTIFIER = "22222222-2222-4222-8222-222222222222"
FAKE_PROFILE_ID = 10000001
FAKE_UNIT_ID = 1000000001
FAKE_CSRF = "FAKE-CSRF-TOKEN-0000"
FAKE_SESSION_NAME = "Jan 01, 2020"
# Only LiveTrack API responses belong in a fixture; telemetry beacons, analytics, map tiles and
# Cloudflare worker responses are dropped entirely (they carry viewer-identifying data).
KEEP_HOST = "livetrack.garmin.com"
KEEP_PATH_PREFIX = "/api/"
EARTH_RADIUS_M = 6_371_008.8
GEOM_SEED = 20200101
HR_SEED = 20200102

# Key heuristics. Phase 1 refines these against the real schema; unknown keys are left as-is
# and listed in the report for review.
LAT_KEYS = {"lat", "latitude"}
LON_KEYS = {"lon", "lng", "long", "longitude"}
ALT_KEY_RE = re.compile(r"(alt(itude)?|elev(ation)?)", re.IGNORECASE)
HR_KEY_RE = re.compile(r"(heart.?rate|^hr$|hrBpm|heartRateBeatsPerMin)", re.IGNORECASE)
TIME_KEY_RE = re.compile(
    r"(time|date|timestamp|start|end|expire|created|updated|modified)", re.IGNORECASE
)
ID_KEY_RE = re.compile(
    r"(session.?id|user.?id|profile.?id|customer.?id|device.?id|unit.?id|serial|garmin.?guid|"
    r"request.?id|^identifier$|session.?name|"
    r"display.?name|first.?name|last.?name|full.?name|user.?name|nick.?name|^name$|email|avatar|image.?url|"
    r"profile.?image|user.?pro|owner|publisher)",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ISO_DT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d{1,6})?(Z|[+-]\d{2}:?\d{2})?$"
)
# Also matches URL-encoded colons (%3A) so timestamps inside query strings are shifted too.
ISO_DT_INLINE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}(?::|%3A)\d{2}(?::|%3A)\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}(?::|%3A)?\d{2})?"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EPOCH_MS_MIN, EPOCH_MS_MAX = 1_000_000_000_000, 4_000_000_000_000
EPOCH_S_MIN, EPOCH_S_MAX = 1_000_000_000, 4_000_000_000
KEEP_RESPONSE_HEADERS = {"content-type", "cache-control", "content-encoding"}
KEEP_REQUEST_HEADERS = {
    "accept",
    "accept-language",
    "content-type",
    "user-agent",
    "referer",
    "origin",
    "livetrack-csrf-token",
    "x-garmin-livetrack-user-guid",
}
MASKED_HEADERS = {"livetrack-csrf-token": FAKE_CSRF}


# --------------------------------------------------------------------------- geometry
def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def destination(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Point at ``dist_m`` metres from (lat, lon) along ``bearing_deg`` (great-circle)."""
    d = dist_m / EARTH_RADIUS_M
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(
        math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2)
    )
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def synthetic_path(distances_m: list[float], seed: int = GEOM_SEED) -> list[tuple[float, float]]:
    """Regenerate a path with the given consecutive segment distances and invented headings."""
    rng = random.Random(seed)  # noqa: S311 - not security sensitive, deterministic by design
    heading = rng.uniform(0, 360)
    lat, lon = FAKE_ORIGIN_LAT, FAKE_ORIGIN_LON
    out = [(lat, lon)]
    for d in distances_m:
        heading = (heading + rng.gauss(0, 12)) % 360  # smooth wander, like a real route
        lat, lon = destination(lat, lon, heading, d)
        out.append((lat, lon))
    return out


# --------------------------------------------------------------------------- time
def parse_iso(value: str) -> datetime | None:
    m = ISO_DT_RE.match(value)
    if not m:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def format_like(original: str, dt: datetime) -> str:
    """Render ``dt`` in the same ISO style as ``original`` (Z vs offset, fraction digits)."""
    m = ISO_DT_RE.match(original)
    assert m is not None
    frac = m.group(3)
    tz = m.group(4)
    sep = "T" if "T" in original else " "
    base = dt.strftime(f"%Y-%m-%d{sep}%H:%M:%S")
    if frac:
        digits = len(frac) - 1
        base += "." + f"{dt.microsecond:06d}"[:digits]
    if tz == "Z":
        base += "Z"
    elif tz:
        off = dt.utcoffset() or timedelta(0)
        sign = "+" if off >= timedelta(0) else "-"
        off = abs(off)
        hh, mm = divmod(int(off.total_seconds()) // 60, 60)
        base += f"{sign}{hh:02d}{':' if ':' in tz else ''}{mm:02d}"
    return base


# --------------------------------------------------------------------------- walker
@dataclass
class Stats:
    counts: Counter[str] = field(default_factory=Counter)
    remaining_strings: dict[str, str] = field(default_factory=dict)
    original_dates: set[str] = field(default_factory=set)
    original_coords: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


@dataclass
class Scrubber:
    session_id: str | None
    token: str | None
    keep_hr: bool
    extra_literals: list[str]
    stats: Stats = field(default_factory=Stats)
    # discovered on pass 1
    coord_map: dict[tuple[float, float], tuple[float, float]] = field(default_factory=dict)
    time_delta: timedelta = timedelta(0)
    alt_offset: float = 0.0
    hr_map: dict[str, int] = field(default_factory=dict)
    id_map: dict[str, str] = field(default_factory=dict)
    _ordered_points: list[tuple[float, float, float | None]] = field(default_factory=list)
    _seen_points: set[tuple[float, float]] = field(default_factory=set)
    _earliest: datetime | None = None
    _first_alt: float | None = None
    _hr_samples: list[str] = field(default_factory=list)

    # ---- pass 1: discovery -----------------------------------------------------------
    def discover(self, node: Any, path: str = "$") -> None:
        if isinstance(node, dict):
            latlon = _extract_latlon(node)
            if latlon is not None:
                lat, lon = latlon
                ts = _point_time(node)
                if (lat, lon) not in self._seen_points:
                    self._seen_points.add((lat, lon))
                    self._ordered_points.append((lat, lon, ts))
                self.stats.original_coords.add(_coord_sig(lat))
                self.stats.original_coords.add(_coord_sig(lon))
            for k, v in node.items():
                child = f"{path}.{k}"
                if ID_KEY_RE.search(k) and isinstance(v, str | int) and not isinstance(v, bool):
                    self._register_id(k, v)
                if isinstance(v, str):
                    dt = parse_iso(v) if TIME_KEY_RE.search(k) or ISO_DT_RE.match(v) else None
                    if dt is not None:
                        self._earliest = (
                            dt if self._earliest is None or dt < self._earliest else self._earliest
                        )
                        self.stats.original_dates.add(dt.date().isoformat())
                    elif ISO_DATE_RE.fullmatch(v):
                        self.stats.original_dates.add(v)
                elif isinstance(v, int | float) and not isinstance(v, bool):
                    if TIME_KEY_RE.search(k):
                        dt = _epoch_to_dt(v)
                        if dt is not None:
                            self._earliest = (
                                dt
                                if self._earliest is None or dt < self._earliest
                                else self._earliest
                            )
                            self.stats.original_dates.add(dt.date().isoformat())
                    if ALT_KEY_RE.search(k) and self._first_alt is None:
                        self._first_alt = float(v)
                    if HR_KEY_RE.search(k):
                        self._hr_samples.append(child)
                self.discover(v, child)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self.discover(v, f"{path}[{i}]")

    def _register_id(self, key: str, value: str | int) -> None:
        """Map a real identity value to a fixed fake so bodies, URLs and headers stay consistent."""
        text = str(value)
        if len(text) < 6 or text in self.id_map or text in (self.session_id, self.token):
            return
        k = key.lower()
        if "guid" in k:
            fake = FAKE_GUID
        elif k == "identifier":
            fake = FAKE_IDENTIFIER
        elif "profile" in k and isinstance(value, int):
            fake = str(FAKE_PROFILE_ID)
        elif ("unit" in k or "device" in k) and isinstance(value, int):
            fake = str(FAKE_UNIT_ID)
        elif "session" in k and "name" in k:
            fake = FAKE_SESSION_NAME
        elif "name" in k:
            fake = "Fixture Athlete"
        elif isinstance(value, int):
            fake = str(FAKE_PROFILE_ID + len(self.id_map) + 1)
        else:
            fake = f"REDACTED-{len(self.id_map) + 1:02d}"
        self.id_map[text] = fake

    def finalize_discovery(self) -> None:
        pts = self._ordered_points
        if any(p[2] is not None for p in pts):
            pts.sort(key=lambda p: (p[2] is None, p[2] if p[2] is not None else 0.0))
        dists = [haversine_m(a[0], a[1], b[0], b[1]) for a, b in pairwise(pts)]
        fake = synthetic_path(dists)
        self.coord_map = {(p[0], p[1]): f for p, f in zip(pts, fake, strict=False)}
        self.stats.counts["coordinate_pairs_regenerated"] = len(self.coord_map)
        self.stats.counts["path_total_distance_m"] = int(sum(dists))
        if self._earliest is not None:
            self.time_delta = FAKE_START - self._earliest
            self.stats.notes.append(
                "timestamps shifted so the earliest timestamp is 2020-01-01T06:00:00Z"
            )
        if self._first_alt is not None:
            self.alt_offset = FAKE_FIRST_ALTITUDE_M - self._first_alt
            self.stats.notes.append(
                f"altitude shifted so the first trackpoint sits at {FAKE_FIRST_ALTITUDE_M:g} m"
            )
        if not self.keep_hr:
            self._build_hr_series()

    def _build_hr_series(self) -> None:
        rng = random.Random(HR_SEED)  # noqa: S311
        n = max(len(self._hr_samples), 1)
        base = 128.0
        series: list[int] = []
        for i in range(n):
            trend = 25.0 * (1 - math.exp(-i / (n * 0.35)))  # warm-up drift upward
            wobble = 6.0 * math.sin(i / 9.0)
            series.append(round(base + trend + wobble + rng.gauss(0, 1.5)))
        self.hr_map = dict(zip(self._hr_samples, series, strict=False))

    # ---- pass 2: rewrite ---------------------------------------------------------------
    def scrub(self, node: Any, path: str = "$") -> Any:
        if isinstance(node, dict):
            out: dict[str, Any] = {}
            latlon = _extract_latlon(node)
            for k, v in node.items():
                child = f"{path}.{k}"
                out[k] = self._scrub_field(k, v, child, latlon)
            return out
        if isinstance(node, list):
            return [self.scrub(v, f"{path}[{i}]") for i, v in enumerate(node)]
        if isinstance(node, str):
            return self._scrub_string(node, path)
        return node

    def _scrub_field(
        self, key: str, value: Any, path: str, latlon: tuple[float, float] | None
    ) -> Any:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return self._scrub_number(key, value, path, latlon)
        if isinstance(value, str):
            if ID_KEY_RE.search(key):
                self.stats.counts["identity_strings_replaced"] += 1
                if value in self.id_map or value in (self.session_id, self.token):
                    return self.replace_ids(value)
                return _fake_identity(key)
            return self._scrub_string(value, path)
        return self.scrub(value, path)

    def _scrub_number(
        self, key: str, value: float, path: str, latlon: tuple[float, float] | None
    ) -> float:
        if latlon is not None and key.lower() in LAT_KEYS | LON_KEYS:
            fake = self.coord_map.get(latlon)
            if fake is not None:
                self.stats.counts["coordinates_rewritten"] += 1
                return round(fake[0] if key.lower() in LAT_KEYS else fake[1], 7)
        if ALT_KEY_RE.search(key) and self.alt_offset:
            self.stats.counts["altitudes_shifted"] += 1
            shifted_alt = float(value) + self.alt_offset
            return round(shifted_alt) if isinstance(value, int) else round(shifted_alt, 2)
        if HR_KEY_RE.search(key) and not self.keep_hr:
            self.stats.counts["hr_values_synthesized"] += 1
            return self.hr_map.get(path, 140)
        shifted = _shift_epoch(value, self.time_delta) if TIME_KEY_RE.search(key) else None
        if shifted is not None:
            self.stats.counts["epoch_timestamps_shifted"] += 1
            return shifted
        if ID_KEY_RE.search(key):
            self.stats.counts["numeric_ids_replaced"] += 1
            return int(self.id_map.get(str(value), "0"))
        return value

    def _scrub_string(self, value: str, path: str) -> str:
        dt = parse_iso(value)
        if dt is not None:
            self.stats.counts["iso_timestamps_shifted"] += 1
            return format_like(value, dt + self.time_delta)
        if ISO_DATE_RE.fullmatch(value) and value in self.stats.original_dates:
            self.stats.counts["dates_shifted"] += 1
            return (
                (datetime.fromisoformat(value).replace(tzinfo=UTC) + self.time_delta)
                .date()
                .isoformat()
            )
        new = self.replace_ids(value)
        if EMAIL_RE.search(new):
            new = EMAIL_RE.sub("user@example.com", new)
            self.stats.counts["emails_replaced"] += 1
        if new != value:
            self.stats.counts["id_strings_replaced"] += 1
        if len(new) <= 200 and not ISO_DT_RE.match(new):
            self.stats.remaining_strings[path] = new
        return new

    def replace_ids(self, text: str) -> str:
        """Rewrite identifiers and timestamps embedded in free text (URLs, headers, messages)."""
        if self.session_id:
            text = text.replace(self.session_id, FAKE_SESSION_ID)
        if self.token:
            text = text.replace(self.token, FAKE_TOKEN)
        for real, fake in sorted(self.id_map.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(real, fake)
        for lit in self.extra_literals:
            text = text.replace(lit, "REDACTED")
        text = ISO_DT_INLINE_RE.sub(self._shift_inline_dt, text)
        for d in self.stats.original_dates:
            if d in text:
                shifted_day = datetime.fromisoformat(d).replace(tzinfo=UTC) + self.time_delta
                text = text.replace(d, shifted_day.date().isoformat())
        return text

    def _shift_inline_dt(self, m: re.Match[str]) -> str:
        raw_text = m.group(0)
        encoded = "%3A" in raw_text
        plain = raw_text.replace("%3A", ":")
        dt = parse_iso(plain)
        if dt is None:
            return raw_text
        self.stats.counts["inline_timestamps_shifted"] += 1
        shifted = format_like(plain, dt + self.time_delta)
        return shifted.replace(":", "%3A") if encoded else shifted


def _extract_latlon(node: dict[str, Any]) -> tuple[float, float] | None:
    lat = lon = None
    for k, v in node.items():
        if isinstance(v, int | float) and not isinstance(v, bool):
            if k.lower() in LAT_KEYS:
                lat = float(v)
            elif k.lower() in LON_KEYS:
                lon = float(v)
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def _point_time(node: dict[str, Any]) -> float | None:
    for k, v in node.items():
        if TIME_KEY_RE.search(k):
            if isinstance(v, str):
                dt = parse_iso(v)
                if dt:
                    return dt.timestamp()
            elif isinstance(v, int | float) and not isinstance(v, bool):
                dt = _epoch_to_dt(v)
                if dt:
                    return dt.timestamp()
    return None


def _epoch_to_dt(v: float) -> datetime | None:
    if EPOCH_MS_MIN <= v <= EPOCH_MS_MAX:
        return datetime.fromtimestamp(v / 1000, tz=UTC)
    if EPOCH_S_MIN <= v <= EPOCH_S_MAX:
        return datetime.fromtimestamp(v, tz=UTC)
    return None


def _shift_epoch(v: float, delta: timedelta) -> float | None:
    if EPOCH_MS_MIN <= v <= EPOCH_MS_MAX:
        r = v + delta.total_seconds() * 1000
        return int(r) if isinstance(v, int) else r
    if EPOCH_S_MIN <= v <= EPOCH_S_MAX:
        r = v + delta.total_seconds()
        return int(r) if isinstance(v, int) else r
    return None


def _coord_sig(x: float) -> str:
    """A short prefix of the coordinate's decimal form; substring-checked against the output."""
    text = repr(float(x))
    head, _, frac = text.partition(".")
    return f"{head}.{frac[:4]}"


def _fake_identity(key: str) -> str:
    k = key.lower()
    if "email" in k:
        return "user@example.com"
    if "url" in k or "avatar" in k or "image" in k:
        return "https://example.com/redacted"
    if "name" in k:
        return "Fixture Athlete"
    return "REDACTED"


# --------------------------------------------------------------------------- io
def extract_ids(share_url: str) -> tuple[str | None, str | None]:
    """Pull the session ID and token out of a share URL like /session/<id>/token/<token>."""
    parts = [p for p in urlsplit(share_url).path.split("/") if p]
    session_id = token = None
    for i, seg in enumerate(parts):
        if seg.lower() == "session" and i + 1 < len(parts):
            session_id = parts[i + 1]
        if seg.lower() == "token" and i + 1 < len(parts):
            token = parts[i + 1]
    if session_id is None and parts:
        session_id = parts[-1]
    return session_id, token


def load_denylist(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line and not line.startswith("!"):
            out.append(line)
    return out


def scrub_headers(headers: dict[str, str], keep: set[str], scrubber: Scrubber) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk in keep:
            out[k] = MASKED_HEADERS.get(lk) or scrubber.replace_ids(v)
    return out


def is_fixture_response(rec: dict[str, Any]) -> bool:
    parts = urlsplit(rec["request"]["url"])
    return parts.hostname == KEEP_HOST and parts.path.startswith(KEEP_PATH_PREFIX)


def self_check(out_dir: Path, scrubber: Scrubber, denylist: list[str]) -> list[str]:
    problems: list[str] = []
    blob = "\n".join(
        p.read_text()
        for p in sorted(out_dir.rglob("*"))
        if p.is_file() and p.suffix in {".json", ".md"}
    )
    for label, lit in (("session id", scrubber.session_id), ("token", scrubber.token)):
        if lit and lit in blob:
            problems.append(f"original {label} survives in output")
    for i, lit in enumerate(denylist):
        if lit in blob:
            problems.append(f"denylist entry #{i + 1} survives in output")
    for sig in scrubber.stats.original_coords:
        if sig in blob:
            problems.append("an original coordinate value survives in output")
            break
    textual_formats = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%Y/%m/%d", "%m/%d/%Y", "%d.%m.%Y")
    for d in scrubber.stats.original_dates:
        if d == "2020-01-01":
            continue
        day = datetime.fromisoformat(d)
        if d in blob or any(day.strftime(f) in blob for f in textual_formats):
            problems.append("an original date survives in output")
            break
    for real in scrubber.id_map:
        if real in blob:
            problems.append("an original identity value (guid/profile/unit id) survives in output")
            break
    if EMAIL_RE.search(blob.replace("user@example.com", "")):
        problems.append("an email address survives in output")
    return problems


def write_report(
    out_dir: Path, name: str, scrubber: Scrubber, n_records: int, problems: list[str]
) -> None:
    st = scrubber.stats
    lines = [
        f"# Scrub report — fixture `{name}`",
        "",
        "Generated by `tools/scrub_fixtures.py`. Review before committing: everything below is",
        "public.",
        "",
        "## Transformations",
        "",
        f"- Records processed: {n_records}",
        f"- Session ID → `{FAKE_SESSION_ID}`; token → `{FAKE_TOKEN}`",
        "- Cookies and auth headers dropped",
        "- Route geometry regenerated from a fixed open-ocean origin; segment distances preserved,",
        "  headings invented",
        f"- Heart rate: {'kept (--keep-hr)' if scrubber.keep_hr else 'synthetic series'}",
        *[f"- {n}" for n in st.notes],
        "",
        "| transformation | count |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in sorted(st.counts.items())],
        "",
        "## Remaining string fields (review these)",
        "",
        "Each JSON path that still carries a string after scrubbing, with its (scrubbed) value.",
        "Timestamps are omitted. Paths are shown once (first occurrence).",
        "",
        "| path | value |",
        "|---|---|",
    ]
    seen_shapes: set[str] = set()
    for path, val in st.remaining_strings.items():
        shape = re.sub(r"\[\d+\]", "[*]", re.sub(r"^\$r\d+", "$", path))
        if shape in seen_shapes:
            continue
        seen_shapes.add(shape)
        lines.append(f"| `{shape}` | `{val.replace('|', '\\|')[:120]}` |")
    lines += ["", "## Self-check", ""]
    lines += (
        ["- PASS: no original IDs, coordinates, dates, emails or denylist entries found"]
        if not problems
        else [f"- FAIL: {p}" for p in problems]
    )
    (out_dir / "scrub_report.md").write_text("\n".join(lines) + "\n")


def run(name: str, raw_root: Path, out_root: Path, keep_hr: bool, denylist_path: Path) -> int:
    raw_dir = raw_root / name
    responses_dir = raw_dir / "responses"
    if not responses_dir.is_dir():
        print(f"scrub: no raw capture at {raw_dir}", file=sys.stderr)
        return 2
    meta = (
        json.loads((raw_dir / "meta.json").read_text()) if (raw_dir / "meta.json").exists() else {}
    )
    session_id, token = extract_ids(meta.get("share_url", ""))
    denylist = load_denylist(denylist_path)
    scrubber = Scrubber(
        session_id=session_id, token=token, keep_hr=keep_hr, extra_literals=denylist
    )

    all_records = [json.loads(p.read_text()) for p in sorted(responses_dir.glob("*.json"))]
    records = sorted((r for r in all_records if is_fixture_response(r)), key=lambda r: r["seq"])
    scrubber.stats.counts["responses_dropped_non_livetrack_api"] = len(all_records) - len(records)
    scrubber.stats.notes.append(
        f"only {KEEP_HOST}{KEEP_PATH_PREFIX}* responses are kept; telemetry, analytics, map and "
        "geolocation responses are dropped"
    )

    # pass 1: discover coordinates, times, altitude, HR sample positions
    for rec in records:
        if rec.get("body_text"):
            try:
                scrubber.discover(json.loads(rec["body_text"]), f"$r{rec['seq']}")
            except ValueError:
                continue
        captured = parse_iso(rec["captured_at"])
        if captured:
            scrubber.stats.original_dates.add(captured.date().isoformat())
    scrubber.finalize_discovery()

    # pass 2: rewrite
    out_dir = out_root / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "responses").mkdir(parents=True)
    manifest: list[dict[str, Any]] = []
    for rec in records:
        # discovery used "$r<seq>" path prefixes so HR positions line up per record
        scrubbed = scrub_record_with_prefix(rec, scrubber)
        fname = f"{rec['seq']:04d}.json"
        (out_dir / "responses" / fname).write_text(
            json.dumps(scrubbed, indent=1, sort_keys=True) + "\n"
        )
        manifest.append(
            {
                "file": f"responses/{fname}",
                "seq": rec["seq"],
                "monotonic_s": rec.get("monotonic_s"),
                "method": scrubbed["request"]["method"],
                "url": scrubbed["request"]["url"],
                "status": scrubbed["response"]["status"],
            }
        )
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": name,
                "fake_session_id": FAKE_SESSION_ID,
                "fake_token": FAKE_TOKEN,
                "share_url": scrubber.replace_ids(meta.get("share_url", "")),
                "responses": manifest,
            },
            indent=1,
        )
        + "\n"
    )
    problems = self_check(out_dir, scrubber, denylist)
    write_report(out_dir, name, scrubber, len(records), problems)
    if problems:
        print("scrub: SELF-CHECK FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"scrub: wrote {len(records)} scrubbed responses to {out_dir}")
    print(f"scrub: review {out_dir / 'scrub_report.md'} before committing")
    return 0


def scrub_record_with_prefix(rec: dict[str, Any], scrubber: Scrubber) -> dict[str, Any]:
    prefix = f"$r{rec['seq']}"
    body_text = rec.get("body_text")
    scrubbed_text: str | None = None
    if body_text:
        try:
            body = json.loads(body_text)
        except ValueError:
            body = None
        scrubbed_text = (
            json.dumps(scrubber.scrub(body, prefix), separators=(",", ":"))
            if body is not None
            else scrubber.replace_ids(body_text)
        )
    captured = parse_iso(rec["captured_at"])
    return {
        "seq": rec["seq"],
        "captured_at": format_like(rec["captured_at"], captured + scrubber.time_delta)
        if captured
        else None,
        "monotonic_s": rec.get("monotonic_s"),
        "request": {
            "method": rec["request"]["method"],
            "url": scrubber.replace_ids(rec["request"]["url"]),
            "headers": scrub_headers(
                rec["request"].get("headers", {}), KEEP_REQUEST_HEADERS, scrubber
            ),
            "post_data": scrubber.replace_ids(rec["request"]["post_data"])
            if rec["request"].get("post_data")
            else None,
        },
        "response": {
            "status": rec["response"]["status"],
            "headers": scrub_headers(
                rec["response"].get("headers", {}), KEEP_RESPONSE_HEADERS, scrubber
            ),
        },
        "body_text": scrubbed_text,
        "body_base64": None if body_text else rec.get("body_base64"),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--name",
        required=True,
        help="fixture name (fixtures/raw/<NAME> → fixtures/sessions/<NAME>)",
    )
    p.add_argument(
        "--keep-hr", action="store_true", help="keep real heart-rate values (default: synthesize)"
    )
    p.add_argument("--raw-root", type=Path, default=RAW_ROOT, help=argparse.SUPPRESS)
    p.add_argument("--out-root", type=Path, default=OUT_ROOT, help=argparse.SUPPRESS)
    p.add_argument("--denylist", type=Path, default=DENYLIST, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main() -> int:
    args = parse_args()
    return run(args.name, args.raw_root, args.out_root, args.keep_hr, args.denylist)


if __name__ == "__main__":
    sys.exit(main())
