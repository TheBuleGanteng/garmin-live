#!/usr/bin/env python3
"""Record a Garmin LiveTrack session for fixture building.

Opens a LiveTrack share URL in headless Chromium, records a HAR plus every
XHR/fetch response, keeps the page open so the site's own polling continues,
and afterwards probes the data endpoints with plain HTTP (and optionally from
the VM) to learn what the server needs to fetch without a browser.

Everything this tool writes lands in ``fixtures/raw/<NAME>/`` which is
gitignored. Nothing printed to the terminal contains coordinates, tokens,
session IDs or cookies.

Usage::

    tools/capture_livetrack.py URL --name NAME [--idle-stop-min 20] [--max-hours 3]
        [--probe-after-end-min 30] [--vm-probe] [--probe-only]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from playwright.async_api import async_playwright

ALLOWED_HOST = "livetrack.garmin.com"
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_ROOT = REPO_ROOT / "fixtures" / "raw"
DENYLIST_PATH = REPO_ROOT / ".secret-denylist"
DATA_RESOURCE_TYPES = {"xhr", "fetch"}
BROWSER_LIKE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.6367.207 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
# Headers that are hop-by-hop or that httpx/curl set themselves.
SKIP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    ":method",
    ":path",
    ":authority",
    ":scheme",
}
PROBE_TIMEOUT_S = 30.0
POST_END_PROBE_INTERVAL_S = 300
_UUIDISH = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-]{16,}")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def redact_url(url: str) -> str:
    """Return a printable version of a URL with ID-like path segments and the query removed."""
    parts = urlsplit(url)
    segments = []
    for seg in parts.path.split("/"):
        if _UUIDISH.fullmatch(seg) or _LONG_TOKEN.fullmatch(seg) or seg.isdigit():
            segments.append("<id>")
        else:
            segments.append(seg)
    query = "?<query>" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), "", "")) + query


def endpoint_key(url: str) -> str:
    """Group URLs by scheme+host+path (query stripped) so re-polls collapse to one endpoint."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def validate_livetrack_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SystemExit("refusing: URL must use https")
    if parts.hostname != ALLOWED_HOST:
        raise SystemExit(f"refusing: host must be exactly {ALLOWED_HOST}")
    if parts.username or parts.password:
        raise SystemExit("refusing: URL must not contain userinfo")
    return url


def extract_secret_segments(url: str) -> list[str]:
    """Return the session ID and token from a share URL, plus any other ID-like path segments.

    Handles the ``/session/<id>/token/<token>`` shape and falls back to any UUID-like or
    long opaque segment, so a changed URL layout still gets its identifiers denylisted.
    """
    parts = [p for p in urlsplit(url).path.split("/") if p]
    found: list[str] = []
    for i, seg in enumerate(parts):
        if seg.lower() in ("session", "token") and i + 1 < len(parts):
            found.append(parts[i + 1])
    for seg in parts:
        if (_UUIDISH.fullmatch(seg) or _LONG_TOKEN.fullmatch(seg)) and seg not in found:
            found.append(seg)
    return [f for f in found if len(f) >= 8]


def register_in_denylist(url: str, denylist: Path = DENYLIST_PATH) -> int:
    """Append the URL's session ID and token to the gitignored denylist, skipping duplicates.

    Prints nothing (the values are secrets). Returns the number of entries added. The file is
    created (mode 600) if missing so the scrubber self-check and the pre-commit hook know the
    identifiers from the first capture onward.
    """
    values = extract_secret_segments(url)
    if not values:
        return 0
    existing: set[str] = set()
    if denylist.exists():
        for raw_line in denylist.read_text().splitlines():
            entry = raw_line.split("#", 1)[0].strip()
            if entry:
                existing.add(entry)
    new_values = [v for v in values if v not in existing]
    if not new_values:
        return 0
    denylist.parent.mkdir(parents=True, exist_ok=True)
    with denylist.open("a") as fh:
        if denylist.stat().st_size and not denylist.read_text().endswith("\n"):
            fh.write("\n")
        fh.write("# LiveTrack identifiers added automatically by tools/capture_livetrack.py\n")
        for v in new_values:
            fh.write(v + "\n")
    denylist.chmod(0o600)
    return len(new_values)


@dataclass
class CaptureState:
    raw_dir: Path
    responses_dir: Path
    response_count: int = 0
    last_new_data_at: float = field(default_factory=time.monotonic)
    started_at: float = field(default_factory=time.monotonic)
    body_hashes: dict[str, str] = field(default_factory=dict)
    endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: int = 0


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


class Recorder:
    """Attaches to a Playwright page and persists every XHR/fetch exchange."""

    def __init__(self, state: CaptureState) -> None:
        self.state = state

    async def on_response(self, response: Any) -> None:
        request = response.request
        if request.resource_type not in DATA_RESOURCE_TYPES:
            return
        st = self.state
        try:
            body_bytes: bytes = await response.body()
        except Exception as exc:
            st.errors += 1
            print(f"[capture] body unavailable ({type(exc).__name__}): {redact_url(response.url)}")
            body_bytes = b""
        try:
            req_headers = await request.all_headers()
            resp_headers = await response.all_headers()
        except Exception:
            req_headers, resp_headers = dict(request.headers), dict(response.headers)

        st.response_count += 1
        seq = st.response_count
        body_text: str | None
        body_b64: str | None
        try:
            body_text = body_bytes.decode("utf-8")
            body_b64 = None
        except UnicodeDecodeError:
            body_text = None
            body_b64 = base64.b64encode(body_bytes).decode("ascii")

        record = {
            "seq": seq,
            "captured_at": utc_now_iso(),
            "monotonic_s": round(time.monotonic() - st.started_at, 3),
            "request": {
                "method": request.method,
                "url": response.url,
                "headers": req_headers,
                "post_data": request.post_data,
                "resource_type": request.resource_type,
            },
            "response": {
                "status": response.status,
                "status_text": response.status_text,
                "headers": resp_headers,
                "size": len(body_bytes),
            },
            "body_text": body_text,
            "body_base64": body_b64,
        }
        write_json(st.responses_dir / f"{seq:04d}.json", record)

        key = endpoint_key(response.url)
        digest = hashlib.sha256(body_bytes).hexdigest()
        if st.body_hashes.get(key) != digest and response.status < 400:
            st.last_new_data_at = time.monotonic()
        st.body_hashes[key] = digest
        ep = st.endpoints.setdefault(
            key,
            {
                "method": request.method,
                "count": 0,
                "statuses": {},
                "last_url": response.url,
                "last_seq": seq,
            },
        )
        ep["count"] += 1
        ep["statuses"][str(response.status)] = ep["statuses"].get(str(response.status), 0) + 1
        ep["last_url"] = response.url
        ep["last_seq"] = seq
        ep["last_request_headers"] = {
            k: v for k, v in req_headers.items() if k.lower() not in SKIP_REQUEST_HEADERS
        }

    def on_request_failed(self, request: Any) -> None:
        if request.resource_type in DATA_RESOURCE_TYPES:
            self.state.errors += 1
            failure = request.failure or "unknown"
            print(f"[capture] request failed ({failure}) for {redact_url(request.url)}")


def heartbeat(state: CaptureState) -> None:
    now = time.monotonic()
    print(
        f"[heartbeat {utc_now_iso()}] responses={state.response_count} "
        f"endpoints={len(state.endpoints)} since_new_data={int(now - state.last_new_data_at)}s "
        f"elapsed={int(now - state.started_at)}s errors={state.errors}"
    )


async def record_session(
    url: str,
    state: CaptureState,
    *,
    idle_stop_min: float,
    max_hours: float,
    heartbeat_s: int,
) -> list[dict[str, Any]]:
    """Run the browser capture loop. Returns the context cookies at the end."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    recorder = Recorder(state)
    cookies: list[dict[str, Any]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            record_har_path=str(state.raw_dir / "session.har"),
            record_har_content="embed",
            record_har_mode="full",
            locale="en-US",
        )
        page = await context.new_page()
        page.on("response", lambda r: asyncio.create_task(recorder.on_response(r)))
        page.on("requestfailed", recorder.on_request_failed)
        page.on("crash", lambda _: print("[capture] page crashed; will reload"))

        print(f"[capture] opening {redact_url(url)}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as exc:
            print(f"[capture] initial navigation problem: {type(exc).__name__}; continuing")

        next_heartbeat = time.monotonic() + heartbeat_s
        reason = "unknown"
        while not stop_event.is_set():
            await asyncio.sleep(1)
            now = time.monotonic()
            if now >= next_heartbeat:
                heartbeat(state)
                next_heartbeat = now + heartbeat_s
            if now - state.started_at >= max_hours * 3600:
                reason = "max-hours"
                break
            if now - state.last_new_data_at >= idle_stop_min * 60:
                reason = "idle"
                break
            if page.is_closed():
                print("[capture] page closed unexpectedly; reopening")
                page = await context.new_page()
                page.on("response", lambda r: asyncio.create_task(recorder.on_response(r)))
                page.on("requestfailed", recorder.on_request_failed)
                with contextlib.suppress(Exception):
                    await page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        if stop_event.is_set():
            reason = "signal"
        print(f"[capture] stopping browser capture: {reason}")
        heartbeat(state)
        cookies = await context.cookies()
        await context.close()  # flushes the HAR
        await browser.close()
    write_json(state.raw_dir / "cookies.json", cookies)
    return cookies


def cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if "name" in c and "value" in c)


def is_data_endpoint(key: str, ep: dict[str, Any]) -> bool:
    """Only Garmin GET endpoints that answered 2xx are worth probing (skips telemetry beacons)."""
    host = urlsplit(key).hostname or ""
    ok_status = any(s.startswith("2") for s in ep.get("statuses", {}))
    return host.endswith(".garmin.com") and ep.get("method", "GET") == "GET" and ok_status


def select_data_endpoints(endpoints: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in endpoints.items() if is_data_endpoint(k, v)}


def probe_variants(ep: dict[str, Any], cookies: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    observed = dict(ep.get("last_request_headers", {}))
    observed.pop("cookie", None)
    exact = dict(observed)
    ck = cookie_header(cookies)
    if ck:
        exact["Cookie"] = ck
    return {
        "a_no_headers": {},
        "b_browser_like": dict(BROWSER_LIKE_HEADERS),
        "c_observed_headers_and_cookies": exact,
    }


def summarize_body(content_type: str, body: bytes) -> dict[str, Any]:
    """Describe a body without exposing values: type, size, JSON shape."""
    info: dict[str, Any] = {"size": len(body), "content_type": content_type}
    if "json" in content_type or body[:1] in (b"{", b"["):
        try:
            data = json.loads(body)
        except ValueError:
            info["json"] = "invalid"
            return info
        if isinstance(data, dict):
            info["json"] = {"type": "object", "keys": sorted(data.keys())[:40]}
            for k, v in data.items():
                if isinstance(v, list):
                    info["json"][f"len({k})"] = len(v)
        elif isinstance(data, list):
            info["json"] = {"type": "array", "len": len(data)}
        else:
            info["json"] = {"type": type(data).__name__}
    return info


async def http_probe(
    endpoints: dict[str, dict[str, Any]], cookies: list[dict[str, Any]], referer: str
) -> dict[str, Any]:
    results: dict[str, Any] = {"probed_at": utc_now_iso(), "endpoints": {}}
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S, follow_redirects=False) as client:
        for key, ep in endpoints.items():
            url = ep["last_url"]
            per_variant: dict[str, Any] = {}
            for name, headers in probe_variants(ep, cookies).items():
                hdrs = dict(headers)
                if name == "b_browser_like":
                    hdrs["Referer"] = referer
                t0 = time.monotonic()
                try:
                    resp = await client.request(ep.get("method", "GET"), url, headers=hdrs)
                    per_variant[name] = {
                        "status": resp.status_code,
                        "elapsed_ms": int((time.monotonic() - t0) * 1000),
                        "response_headers": {
                            k: v
                            for k, v in resp.headers.items()
                            if k.lower()
                            in (
                                "content-type",
                                "cache-control",
                                "server",
                                "cf-ray",
                                "via",
                                "location",
                            )
                        },
                        **summarize_body(resp.headers.get("content-type", ""), resp.content),
                    }
                except httpx.HTTPError as exc:
                    per_variant[name] = {"error": type(exc).__name__}
            results["endpoints"][redact_url(url)] = {
                "method": ep.get("method", "GET"),
                "observed_count": ep.get("count"),
                "observed_statuses": ep.get("statuses"),
                "variants": per_variant,
                "_url_key": key,
            }
    return results


def vm_probe(
    endpoints: dict[str, dict[str, Any]], cookies: list[dict[str, Any]], referer: str
) -> dict[str, Any]:
    """Run the same variants from the VM via ``ssh kt-vm curl``. Read-only; prints no URLs."""
    results: dict[str, Any] = {"probed_at": utc_now_iso(), "endpoints": {}}
    for ep in endpoints.values():
        url = ep["last_url"]
        per_variant: dict[str, Any] = {}
        for name, headers in probe_variants(ep, cookies).items():
            hdrs = dict(headers)
            if name == "b_browser_like":
                hdrs["Referer"] = referer
            cmd = [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code} %{size_download} %{time_total}",
                "-m",
                "30",
            ]
            for k, v in hdrs.items():
                cmd += ["-H", f"{k}: {v}"]
            cmd += ["-X", ep.get("method", "GET"), url]
            remote = " ".join(shlex.quote(c) for c in cmd)
            try:
                out = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "kt-vm", remote],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                fields = out.stdout.strip().split()
                per_variant[name] = {
                    "status": int(fields[0]) if fields and fields[0].isdigit() else None,
                    "size": int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None,
                    "time_total_s": float(fields[2]) if len(fields) > 2 else None,
                    "ssh_exit": out.returncode,
                    "stderr": out.stderr.strip()[:200] if out.returncode else "",
                }
            except (subprocess.TimeoutExpired, OSError) as exc:
                per_variant[name] = {"error": type(exc).__name__}
        results["endpoints"][redact_url(url)] = {
            "method": ep.get("method", "GET"),
            "variants": per_variant,
        }
    return results


def print_probe_summary(label: str, results: dict[str, Any]) -> None:
    print(f"[{label}] probed at {results['probed_at']}")
    for shape, info in results["endpoints"].items():
        statuses = ", ".join(
            f"{v}={d.get('status', d.get('error'))}/{d.get('size', '?')}B"
            for v, d in info["variants"].items()
        )
        print(f"  {info['method']} {shape}: {statuses}")


def load_endpoints_from_disk(responses_dir: Path) -> dict[str, dict[str, Any]]:
    endpoints: dict[str, dict[str, Any]] = {}
    for path in sorted(responses_dir.glob("*.json")):
        rec = json.loads(path.read_text())
        url = rec["request"]["url"]
        key = endpoint_key(url)
        ep = endpoints.setdefault(
            key,
            {
                "method": rec["request"]["method"],
                "count": 0,
                "statuses": {},
                "last_url": url,
                "last_seq": 0,
            },
        )
        ep["count"] += 1
        status = str(rec["response"]["status"])
        ep["statuses"][status] = ep["statuses"].get(status, 0) + 1
        ep["last_url"] = url
        ep["last_seq"] = rec["seq"]
        ep["last_request_headers"] = {
            k: v
            for k, v in rec["request"]["headers"].items()
            if k.lower() not in SKIP_REQUEST_HEADERS
        }
    return endpoints


async def post_end_probe(
    endpoints: dict[str, dict[str, Any]],
    cookies: list[dict[str, Any]],
    referer: str,
    minutes: float,
    out: Path,
) -> None:
    """Light probe (one pass every 5 minutes) to catch end-of-session / expiry responses."""
    deadline = time.monotonic() + minutes * 60
    rounds: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        res = await http_probe(endpoints, cookies, referer)
        rounds.append(res)
        print_probe_summary("post-end probe", res)
        write_json(out, rounds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(POST_END_PROBE_INTERVAL_S, remaining))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("url", help="LiveTrack share URL (https://livetrack.garmin.com/...)")
    p.add_argument(
        "--name", required=True, help="fixture name; output goes to fixtures/raw/<NAME>/"
    )
    p.add_argument(
        "--idle-stop-min",
        type=float,
        default=20,
        help="stop after this many minutes without new data",
    )
    p.add_argument("--max-hours", type=float, default=3, help="hard stop after this many hours")
    p.add_argument(
        "--probe-after-end-min", type=float, default=30, help="light probing after the capture ends"
    )
    p.add_argument("--heartbeat-sec", type=int, default=60)
    p.add_argument(
        "--vm-probe", action="store_true", help="also probe each endpoint from the VM via ssh kt-vm"
    )
    p.add_argument(
        "--probe-only",
        action="store_true",
        help="skip the browser; re-probe endpoints from a prior run",
    )
    p.add_argument("--raw-root", type=Path, default=RAW_ROOT, help=argparse.SUPPRESS)
    p.add_argument("--denylist", type=Path, default=DENYLIST_PATH, help=argparse.SUPPRESS)
    return p.parse_args(argv)


async def amain(args: argparse.Namespace) -> int:
    url = validate_livetrack_url(args.url)
    added = register_in_denylist(url, args.denylist)
    print(f"[capture] denylist: {added} new identifier(s) registered in {args.denylist.name}")
    raw_dir = args.raw_root / args.name
    responses_dir = raw_dir / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    state = CaptureState(raw_dir=raw_dir, responses_dir=responses_dir)

    if args.probe_only:
        endpoints = select_data_endpoints(load_endpoints_from_disk(responses_dir))
        if not endpoints:
            print("[probe-only] no prior responses found; nothing to probe")
            return 1
        cookies_path = raw_dir / "cookies.json"
        cookies = json.loads(cookies_path.read_text()) if cookies_path.exists() else []
        res = await http_probe(endpoints, cookies, url)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        write_json(raw_dir / f"probe_only_{stamp}.json", res)
        print_probe_summary("probe-only", res)
        if args.vm_probe:
            vres = vm_probe(endpoints, cookies, url)
            write_json(raw_dir / f"vm_probe_only_{stamp}.json", vres)
            print_probe_summary("vm probe-only", vres)
        return 0

    write_json(
        raw_dir / "meta.json",
        {
            "name": args.name,
            "share_url": url,
            "started_at": utc_now_iso(),
            "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        },
    )
    existing = len(list(responses_dir.glob("*.json")))
    if existing:
        state.response_count = existing
        print(f"[capture] resuming numbering after {existing} existing responses")

    cookies = await record_session(
        url,
        state,
        idle_stop_min=args.idle_stop_min,
        max_hours=args.max_hours,
        heartbeat_s=args.heartbeat_sec,
    )
    write_json(raw_dir / "endpoints.json", state.endpoints)
    print(f"[capture] {state.response_count} responses across {len(state.endpoints)} endpoints")

    data_endpoints = select_data_endpoints(state.endpoints)
    if not data_endpoints:
        print("[capture] no Garmin data endpoints observed; skipping probes")
        return 1
    print(f"[capture] probing {len(data_endpoints)} Garmin data endpoint(s)")

    replay = await http_probe(data_endpoints, cookies, url)
    write_json(raw_dir / "replay_probe.json", replay)
    print_probe_summary("replay probe (laptop)", replay)

    if args.vm_probe:
        vres = vm_probe(data_endpoints, cookies, url)
        write_json(raw_dir / "vm_probe.json", vres)
        print_probe_summary("vm probe", vres)

    if args.probe_after_end_min > 0:
        print(
            f"[capture] post-end probing for {args.probe_after_end_min:g} min (one pass per 5 min)"
        )
        await post_end_probe(
            data_endpoints, cookies, url, args.probe_after_end_min, raw_dir / "post_end_probe.json"
        )

    print(f"[capture] done. Raw output in {raw_dir} (gitignored).")
    print(f"[capture] next: tools/scrub_fixtures.py --name {args.name}")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("[capture] interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
