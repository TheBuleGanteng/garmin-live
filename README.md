# garmin-live

A small, self-hostable **read-only bridge from a Garmin LiveTrack share link to Claude**, exposed as a
remote [MCP](https://modelcontextprotocol.io) server.

You start a run, give your LiveTrack link to your own server, and during (or shortly after) the
workout you can ask Claude things like *"how's my pace holding up?"*, *"what were my last three
kilometre splits?"* or *"is my heart rate drifting?"*. Claude calls the server's tools and gets
compact, metric, token-efficient JSON back.

## What it does, and doesn't

- **Pull model.** Claude only sees data when you ask a question. Nothing is pushed to Claude; the
  only background activity is the poller that ingests your LiveTrack session every ~20 s.
- **Read-only.** The server never writes to Garmin; it only reads the share link you give it.
- **Single user per deployment.** One instance serves one athlete.
- **No raw coordinates** are ever returned by the tools. Metrics only.
- Push notifications / alerting are *not* built. The code keeps a seam for them (an `events`
  table and a `Notifier` protocol with a no-op implementation), nothing more.

## Disclaimer

This project is **unofficial** and is not affiliated with, endorsed by, or supported by Garmin Ltd.
or its subsidiaries. It relies on **undocumented LiveTrack endpoints** that can change or break at
any time without notice (see [docs/LIVETRACK_FINDINGS.md](docs/LIVETRACK_FINDINGS.md) for what was
observed). Only use it with **your own** LiveTrack links. You are responsible for complying with
Garmin's terms of service.

## Security model

- Each deployment serves **one person**.
- claude.ai custom connectors support only *authless* or *OAuth* servers, so v1 uses an
  **unguessable URL**: `https://your-domain/mcp/<MCP_PATH_SECRET>`. **That URL is the credential.**
  Anyone who has it can read your live sessions. Treat it like a password and never paste it into
  chat messages, issues or logs.
- Any other path under `/mcp/` returns a plain `404` (never `401`), so probing reveals nothing. The
  secret is compared in constant time. Host/Origin checks (DNS-rebinding protection) are on; set
  `MCP_ALLOWED_HOSTS` to your domain.
- Optional IP allow-list middleware (`ENFORCE_IP_ALLOWLIST=true`) for deployments that can pin the
  client IPs; it reads `X-Real-IP` only from a trusted proxy.
- **To rotate the secret:** generate a new value, update `MCP_PATH_SECRET` in your `.env`, restart
  the container, and update the connector URL in claude.ai. The old URL stops working immediately.
- Location data is sensitive. The tools never expose coordinates, but the SQLite database on your
  host does contain them for the retention window. Keep the host private.
- Nothing in the logs identifies you: tokens, session IDs, the secret and coordinates are redacted.

## Requirements

- An always-on host with **Docker** and Docker Compose v2.
- A **public HTTPS domain** pointing at that host (claude.ai must be able to reach the server).
- Python 3.12 only if you want to develop locally.

## Quickstart (standalone, Caddy handles TLS)

Caddy obtains and renews the TLS certificate automatically. These commands need a real domain
pointing at the host.

```bash
git clone https://github.com/TheBuleGanteng/garmin-live.git && cd garmin-live
cp deploy/examples/docker-compose.example.yml docker-compose.yml
cp deploy/examples/Caddyfile.example Caddyfile
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # paste as MCP_PATH_SECRET in .env
# in .env also set: MCP_ALLOWED_HOSTS=example.com   and optionally ATHLETE_MAX_HR=185
DOMAIN=example.com docker compose up -d
curl -s https://example.com/health
```

Your MCP endpoint is now `https://example.com/mcp/<MCP_PATH_SECRET>`.

## Behind an existing nginx (sub-path deployment)

If the host already runs nginx for other sites, run only the app container (no published ports,
same Docker network as nginx) with `BASE_PATH=/garmin-mcp`, and include
[`nginx/garmin-mcp.location.conf`](nginx/garmin-mcp.location.conf) inside your `server {}` block.
The app serves natively under the prefix, so `proxy_pass` has **no URI part**; `proxy_buffering off`
keeps the MCP streaming responses flowing. Your endpoint is then
`https://example.com/garmin-mcp/mcp/<MCP_PATH_SECRET>`.

## Adding the connector in claude.ai

1. Get the URL: `https://<your domain>[/garmin-mcp]/mcp/<MCP_PATH_SECRET>`.
2. claude.ai → Settings → Connectors → **Add custom connector** → paste the URL. Leave the OAuth
   fields blank (the server is authless; the secret path is the credential).
3. In a new chat, enable the connector and ask *"list my Garmin sessions"*. Then, when you start an
   activity, paste the LiveTrack share link and ask Claude to start tracking it.

Both link forms work: the full `https://livetrack.garmin.com/session/<id>/token/<token>` URL and
the short `https://gar.mn/<code>` link the Garmin app shares. The server expands short links
itself, following the redirect one hop at a time and accepting only `https` targets on `gar.mn` or
`livetrack.garmin.com`; the expanded URL must still match the exact host and path shape above.

## Tool reference

All tools return compact JSON. Units are metric; pace is `m:ss /km`. `session_id` is optional and
defaults to the most recent active session. Fields Garmin does not provide are `null` with a
reason, never invented.

| Tool | Returns | When Claude should call it |
|---|---|---|
| `start_tracking(livetrack_url, label?)` | `session_id`, status, first-ingest result | Once, when you give it a LiveTrack URL (full or `gar.mn`) |
| `list_sessions()` | id, label, status, started, `last_data_age_s` (Garmin time of newest point), `last_ingest_age_s` (last receipt of new points), `stale` (active sessions only), ingest errors | To find a session or check what is tracked |
| `get_live_status(session_id?)` | elapsed / moving time, distance, current (rolling) and average pace, current/avg HR, cadence, elevation gain/loss, `data_freshness_seconds`, `stale`, ingest health | "How is it going right now?" |
| `get_splits(session_id?, split_distance_m=1000)` | per-split time, pace, avg HR, elevation +/- (boundaries interpolated) | Kilometre comparisons, fade detection |
| `get_trend(session_id?, metric, window_minutes=10, bucket_seconds=60)` | bucketed series for `pace` / `hr` / `elevation` / `cadence` | Drift questions ("is HR creeping up?") |
| `get_session_summary(session_id?)` | whole-session stats, HR zone time (only with `ATHLETE_MAX_HR`), aerobic decoupling | After the workout, overall assessment |
| `stop_tracking(session_id)` | final status | Only when you ask to stop; sessions end by themselves when Garmin signals the end |

Sessions end automatically when Garmin marks the activity finished, when the link expires, or after
`MAX_SESSION_HOURS`. `stale` means no new points for more than 5 minutes. At most 3 sessions are
tracked concurrently, and active sessions resume after a restart.

## Configuration

Every setting is an environment variable. [`.env.example`](.env.example) documents each one with
a safe placeholder; copy it to `.env` and edit.

| Variable | Default | Meaning |
|---|---|---|
| `BASE_PATH` | `` (root) | URL prefix the app is served under, e.g. `/garmin-mcp`. Must start with `/` and not end with one. |
| `MCP_PATH_SECRET` | *(required)* | Unguessable path segment for the MCP endpoint, at least 32 characters. **This is the credential.** |
| `DB_PATH` | `/data/garmin.db` | SQLite database (WAL mode). In Docker this is on the `/data` volume. |
| `RETENTION_DAYS` | `30` | Sessions, points and events older than this are purged. |
| `ATHLETE_MAX_HR` | *(empty)* | Your max heart rate in bpm; enables HR zone time. Empty disables zones. |
| `FETCH_STRATEGY` | `http` | `http` (plain requests, the supported mode) or `browser` (reserved; not implemented in v1). |
| `POLL_MIN_SECONDS` | `20` | Minimum seconds between polls of a LiveTrack session. |
| `MAX_SESSION_HOURS` | `12` | A session is force-ended after this many hours. |
| `ENFORCE_IP_ALLOWLIST` | `false` | Restrict the MCP endpoint to `ALLOWED_IPS`. |
| `ALLOWED_IPS` | *(empty)* | Comma-separated IPs or CIDRs allowed when the allow-list is on. |
| `MCP_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated hostnames accepted in the `Host` header. Set to your domain(s). |
| `TRUSTED_PROXY_CIDRS` | *(empty)* | Proxies trusted to set `X-Real-IP`. Empty trusts private-network peers (Docker bridge). |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |

The app refuses to start with a clear message if a setting is missing or invalid (for example a
short or placeholder `MCP_PATH_SECRET`), or if the database schema is newer than the code.

## Data retention and backups

Sessions, trackpoints and events older than `RETENTION_DAYS` are purged by a background job (at
startup and every six hours). Active sessions are never purged. `python -m garmin_live.db_backup DEST`
takes an online SQLite backup and keeps the newest five `*.db` files in `DEST`'s directory.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy --strict src/
.venv/bin/python -m pytest tests/unit tests/integration --cov=src/garmin_live
```

**Replay server.** A fake LiveTrack that serves a scrubbed fixture progressively, with knobs for
speed, stale gaps, 5xx/429 injection and expiry. Integration tests and the selftest use it; you can
also run it by hand:

```bash
.venv/bin/python -m garmin_live.replay.server run1 8765 20     # fixture, port, speed
MCP_PATH_SECRET=<32+ chars> DB_PATH=/tmp/garmin.db .venv/bin/python -m garmin_live.selftest
```

**Local prod-mimic stack** (app under `/garmin-mcp` behind nginx, plus a replay container):

```bash
export MCP_PATH_SECRET=test-secret-not-real-0000-abcdefghijklmnop
docker compose -f docker/docker-compose.local.yml up --build -d
curl -s http://localhost:8080/garmin-mcp/health
.venv/bin/python tools/mcp_smoke.py --url "http://localhost:8080/garmin-mcp/mcp/$MCP_PATH_SECRET"
.venv/bin/python -m pytest tests/e2e -m e2e        # full end-to-end suite (local only)
docker compose -f docker/docker-compose.local.yml down -v
```

**Capturing your own fixtures.** `tools/capture_livetrack.py <share URL> --name NAME` records a
real session (HAR plus every response) into the gitignored `fixtures/raw/NAME/`, and registers the
link's identifiers in the gitignored `.secret-denylist` so they can never be committed. Add
`--vm-probe` to also test the endpoints from a remote host over SSH.

**Privacy scrubbing.** `tools/scrub_fixtures.py --name NAME` turns a raw capture into a publishable
fixture: identifiers replaced, the route regenerated from an open-ocean origin with every segment
distance preserved, timestamps shifted to 2020-01-01, altitude offset, heart rate synthesised
(`--keep-hr` to keep it), identity fields removed. It writes a `scrub_report.md` for review and
fails if anything original survives. Review the report before committing a fixture: a public repo
is forever.

**Secret scanning.** `git config core.hooksPath tools/hooks` enables the pre-commit hook, which runs
gitleaks (Docker) and `tools/check_denylist.sh` on every commit. CI runs gitleaks over the full
history.

## License

[MIT](LICENSE).
