# CLAUDE_INSTRUCTIONS.md — Local build, test, fix, push (garmin-live)

> Audience: Claude Code on the laptop (WSL), in
> `/home/thebuleganteng/01_Repos/06_personal_work/garmin-live`.
> Read `ai-instructions/CLAUDE.md` first; its global hard rules **and public-repo rules** apply
> throughout. All instruction files live in `ai-instructions/`; all code goes at the repo root.
>
> Mode: **unattended**, except the STOP gates marked 🛑. The run ends with code committed and
> pushed to `main` with all quality gates green and CI passing. This doc never touches the VM
> except the read-only probe in Phase 0.
>
> The GitHub repo is **public, MIT-licensed, and already exists** (it contains at least the
> `LICENSE` commit). The project must be **self-hostable by others**: Matthew's deployment is one
> configuration, not something baked into the code.

---

## Product spec (what "done" means)

**Purpose.** Given a LiveTrack share URL, the server ingests the in-progress activity and lets
Claude query it via MCP tools during or shortly after the workout.

**MCP tools** (Streamable HTTP transport). Descriptions must be written for Claude, saying when to
call each one. `session_id` is optional everywhere and defaults to the most recent active session.
| Tool | Returns |
|---|---|
| `start_tracking(livetrack_url, label?)` | session_id, status, first-ingest result |
| `list_sessions()` | id, label, status, started, last data age |
| `get_live_status(session_id?)` | elapsed / moving time, distance, current (rolling) pace, avg pace, current/avg HR, cadence, elevation gain/loss, `data_freshness_seconds`, `stale`, ingest health |
| `get_splits(session_id?, split_distance_m=1000)` | per-split time, pace, avg HR, elevation +/- |
| `get_trend(session_id?, metric, window_minutes=10, bucket_seconds=60)` | bucketed series for `pace` / `hr` / `elevation` / `cadence` |
| `get_session_summary(session_id?)` | full-session stats incl. HR zone time (only if `ATHLETE_MAX_HR` set; otherwise say zones are unavailable and why) and aerobic decoupling (pace:HR first half vs second half) |
| `stop_tracking(session_id)` | final status |

- Units: metric; pace as `m:ss /km`. Outputs are compact JSON (token-efficient).
- **No raw coordinates in any tool output in v1.**
- Fields Garmin doesn't provide are returned as `null` with a reason, never invented.

**Ingestion.**
- Validate the URL: `https` + host exactly `livetrack.garmin.com` + the path shape seen in
  fixtures. Reject everything else (SSRF guard).
- Poll interval: start at `max(POLL_MIN_SECONDS=20, observed cadence)`, with jitter.
- Back off exponentially on errors (cap 5 min).
- Dedupe points by timestamp.
- Session ends on Garmin's end signal (from Phase 1 findings), on expiry (e.g. 404/410), or at
  `MAX_SESSION_HOURS=12`. `stale` = no new points for more than 5 min.
- Allow at most 3 concurrent active sessions.
- Active sessions resume after an app restart.

**Push-readiness seam (do not build push).** The poller writes rows to an `events` table
(`new_points`, `split_completed`, `stale`, `resumed`, `session_ended`, `ingest_error`) and calls a
`Notifier` protocol whose only v1 implementation is `NoopNotifier`. A future push build adds a
notifier; nothing else should need to change.

**Storage.** SQLite at `DB_PATH` (WAL mode). Tables: `sessions`, `trackpoints`, `events`,
`schema_version`. Migrations are numbered SQL files in `src/garmin_live/store/migrations/`, applied
idempotently at startup. The app refuses to start if the DB schema is newer than the code. A
retention job purges sessions, points and events older than `RETENTION_DAYS=30`.

**HTTP surface** (everything under `BASE_PATH`; examples assume `BASE_PATH=/garmin-mcp`):
- `GET {BASE_PATH}/health` → `{"status","version","active_sessions"}`. No PII.
- MCP endpoint at `{BASE_PATH}/mcp/{MCP_PATH_SECRET}`.
  - Any other path under `{BASE_PATH}/mcp/` returns **404** (not 401, so nothing is revealed).
  - Compare the secret in constant time.
- Optional IP-allowlist middleware: `ENFORCE_IP_ALLOWLIST=false` by default. It reads the client
  IP from `X-Real-IP` only when the request comes from the proxy.

> **Why a secret URL instead of a bearer token:** claude.ai custom connectors support only
> **authless** or **OAuth** servers; there is no field for a static bearer header. v1 is therefore
> "authless + unguessable path + optional IP allowlist". OAuth is a planned later upgrade; keep
> auth in one middleware module so it can be swapped.

**Config (env).** `BASE_PATH`, `MCP_PATH_SECRET`, `DB_PATH`, `RETENTION_DAYS`, `ATHLETE_MAX_HR`
(optional), `FETCH_STRATEGY` (`http` | `browser`), `POLL_MIN_SECONDS`, `MAX_SESSION_HOURS`,
`ENFORCE_IP_ALLOWLIST`, `ALLOWED_IPS`, `MCP_ALLOWED_HOSTS` (if the SDK enforces Host/Origin checks),
`LOG_LEVEL`.

**Self-hostability requirements.**
- Every deployment-specific value comes from env. `BASE_PATH` defaults to `""` (root) in code;
  Matthew's VM sets `/garmin-mcp`. Both must work (tested in Phase 6).
- `config.py` fails fast at startup with a clear message for any missing or invalid setting, e.g.
  a missing or short `MCP_PATH_SECRET` (require ≥ 32 characters).
- No Matthew-specific strings anywhere in `src/`, `tests/`, `docker/`, or the examples: no
  `kebayorantechnologies`, no `TheBuleGanteng` except in README links, no personal paths. Use
  `example.com` in examples.
- `.env.example` documents every variable with a safe placeholder and a one-line comment.

**Stack.** Python 3.12, FastAPI, uvicorn (1 worker), the official MCP Python SDK, httpx, pydantic.
Playwright is a dev dependency only, unless Phase 1 decides `browser`. Use `uv` if installed,
otherwise `python3 -m venv .venv` + pip.

**Repo layout**
```
src/garmin_live/
  config.py  app.py  auth.py  logging.py  selftest.py  db_backup.py
  livetrack/  urls.py  fetch_http.py  fetch_browser.py  parser.py  models.py
  ingest/     poller.py  retention.py
  metrics/    pace.py  hr.py  splits.py  elevation.py  summary.py
  events/     bus.py  notifier.py   (Notifier protocol + NoopNotifier)
  mcp_server/ tools.py  server.py
  store/      db.py  repo.py  migrations/001_init.sql
  replay/     server.py  (fake LiveTrack)
tools/  capture_livetrack.py  scrub_fixtures.py  mcp_smoke.py  check_denylist.sh
tools/hooks/pre-commit            (gitleaks + denylist; installed via `git config core.hooksPath tools/hooks`)
fixtures/raw/ (gitignored)   fixtures/sessions/<name>/ (scrubbed, committed)
nginx/garmin-mcp.location.conf    (nginx block for subpath deployments — PROVISION copies this)
docker/  Dockerfile  docker-compose.local.yml  nginx.local.conf
deploy/examples/  docker-compose.example.yml  Caddyfile.example   (standalone self-host path)
docs/LIVETRACK_FINDINGS.md
tests/  unit/  integration/  e2e/
.github/workflows/ci.yml
README.md  LICENSE (exists — never modify)  .env.example  .gitleaks.toml  .gitignore
ai-instructions/  (instructions only — committed, never edited by Claude Code)
```

**README.md** (written for a self-hoster, not for Matthew) must cover:
- What it does and what it doesn't (pull model; Claude only sees data when you ask).
- **Disclaimer:** unofficial, not affiliated with or endorsed by Garmin; relies on undocumented
  LiveTrack endpoints that can change or break at any time; only use it with your own LiveTrack
  links; you're responsible for complying with Garmin's terms.
- **Security model:** single-user per deployment; the secret URL *is* the credential; anyone with
  it can read your live sessions; how to rotate it. Location data is sensitive.
- Requirements: an always-on host with Docker and a public HTTPS domain.
- Quickstart via `deploy/examples/` (Caddy handles TLS automatically), and the alternative of
  putting it behind an existing nginx with `nginx/garmin-mcp.location.conf`.
- How to generate the secret and add the custom connector in claude.ai.
- Tool reference, config table (generated from or checked against `.env.example`), data retention.
- Development: running tests, the replay server, capturing your own fixtures, and the privacy
  scrubbing step.
- A note that `ai-instructions/` contains the maintainer's AI build/deploy runbooks and is not
  needed to self-host.
- License: MIT (link the existing `LICENSE`).

---

## Phase 0 — Scaffold, capture tooling, and fixture gate

0. **Environment bootstrap (automated; no user action).** The kickoff prompt supplies the VM
   address (`user@host`). Treat it as private: it goes only into the two local, uncommitted files
   below and never into any repo file, commit message, or printed report.
   - **SSH alias.** Check whether `kt-vm` already resolves with
     `ssh -G kt-vm | awk '/^hostname /{print $2}'`. If it already maps to the given host, leave it.
     Otherwise:
     - create `~/.ssh` (mode 700) if it's missing;
     - back up `~/.ssh/config` to `~/.ssh/config.bak-<timestamp>` if it exists;
     - append a block `Host kt-vm / HostName <host> / User <user>`;
     - `chmod 600 ~/.ssh/config`.

     Never modify or remove any existing `Host` block. If a `Host kt-vm` block exists but points
     elsewhere, 🛑 STOP and report rather than overwrite it.
   - **Connectivity check.** `ssh -o BatchMode=yes -o ConnectTimeout=10 kt-vm true`. If it fails,
     🛑 STOP: key-based SSH from WSL isn't working, and that's a credential problem Claude Code must
     not try to fix (no key generation, no `ssh-copy-id`).
   - **Denylist.** Create `.secret-denylist` (gitignored, mode 600) if absent. Add, without
     duplicates:
     - the VM host/IP;
     - `git config user.email`;
     - the user's home path `/home/thebuleganteng`.

     The capture tool (step 5) appends real LiveTrack session IDs and tokens automatically.
     Never print this file.
   - **Commit email.** If `git config user.email` isn't a GitHub `noreply` address, note in the
     final report that commit metadata will publish that email. Don't change the user's git config.
1. **Connect to the existing public repo — never overwrite its history.**
   - The remote already has commits (at least `LICENSE`). If the local folder is not yet a git
     repo, attach it without touching local files:
     ```bash
     git init -b main
     git remote add origin git@github.com:TheBuleGanteng/garmin-live.git
     git fetch origin
     git reset origin/main          # mixed reset: adopts remote history, keeps local files as-is
     git checkout -- LICENSE        # restore remote-tracked files missing locally (README too, if present)
     git status                     # local files (ai-instructions/, .claude/) now show as untracked
     ```
     If the remote has a README, build on its content rather than replacing it wholesale.
   - If `git fetch` fails (auth, wrong URL, repo missing), 🛑 STOP and report. Never create the
     repo, change its visibility, or `push --force`.
   - Confirm visibility with `gh repo view TheBuleGanteng/garmin-live --json visibility` if `gh` is
     available; record it in the summary. Treat the repo as public regardless.
2. **Secret-leak protection — before the first commit.**
   - `.gitignore` must cover at least: `.venv/`, `__pycache__/`, `*.pyc`, `.env`, `.env.*` with
     `!.env.example`, `*.pem`, `*.key`, `secrets/`, `fixtures/raw/`, `*.har`, `*.db`, `*.db-*`,
     `*.sqlite*`, `CLAUDE_SUMMARY.md`, `.secret-denylist`, `.claude/settings.local.json`,
     `coverage.xml`, `.coverage`, `htmlcov/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`.
   - `.gitleaks.toml`: extend the default ruleset; allowlist only the obviously fake test-secret
     pattern. Pin the gitleaks Docker image version.
   - `tools/check_denylist.sh`: reads `.secret-denylist` (gitignored; one literal per line, `#`
     comments) and fails if any entry appears in staged content (`git diff --cached`). It prints
     only the file name and line number, never the value. It must fail closed: if
     `.secret-denylist` is missing or empty, exit non-zero. Step 0 populates it.
   - `tools/hooks/pre-commit` runs gitleaks on staged changes plus the denylist check. Enable it
     with `git config core.hooksPath tools/hooks`.
   - Before **every** push: run `gitleaks detect` over the full history plus the denylist check.
     Any finding → 🛑 STOP, don't push.
3. **Scaffold.** Create the layout and `pyproject.toml` with pinned dependencies and
   `license = "MIT"`. Write `.env.example` and a first README skeleton. Commit; the hook must pass.
4. **Install Playwright.** Run `playwright install chromium`.
   If browser system dependencies are missing, 🛑 STOP and ask the user to run, once:
   `sudo .venv/bin/playwright install-deps chromium`. Never run sudo yourself.
5. **Build `tools/capture_livetrack.py URL --name NAME [--idle-stop-min 20] [--max-hours 3] [--probe-after-end-min 30] [--vm-probe]`:**
   - Validate the host is `livetrack.garmin.com`.
   - Open the URL in headless Chromium.
   - Record a HAR to `fixtures/raw/NAME/session.har`.
   - Save every XHR/fetch response body plus request metadata (method, URL, headers, status,
     timestamp) as numbered files in `fixtures/raw/NAME/responses/`.
   - Leave the page open so its own polling continues. Survive network blips.
   - Print a heartbeat every 60 s: response count and seconds since last new data. **No
     coordinates.**
   - Stop on whichever comes first: Ctrl-C, `--max-hours`, or `--idle-stop-min` with no new data.
     Then keep a light probe (one request every 5 min) for `--probe-after-end-min` to capture
     end-of-session and expiry behaviour.
   - **Plain-HTTP replay probe:** for each distinct data endpoint seen, re-request it with httpx in
     three variants: (a) no extra headers, (b) browser-like headers, (c) the exact observed headers
     and cookies. Record status and size to `fixtures/raw/NAME/replay_probe.json`.
   - `--vm-probe`: run the same requests **from the VM** via
     `ssh kt-vm curl -s -o /dev/null -w '%{http_code} %{size_download}' ...`.
     This is read-only; it tests whether Garmin blocks GCP datacenter IPs. Record results to
     `fixtures/raw/NAME/vm_probe.json`.
   - Support `--probe-only` for a later "has this link expired yet?" check.
   - On start, append the real session ID and token from the URL to `.secret-denylist`, skipping
     duplicates and printing nothing. This way the scrubber self-check and the pre-commit scan
     both know them.
6. **Build `tools/scrub_fixtures.py --name NAME` → `fixtures/sessions/NAME/`.** The output is
   published in a public repo, so it must not allow anyone to locate or date the activity:
   - **IDs:** replace session IDs and tokens with fixed fake values everywhere, including URLs and
     headers. Drop cookies and auth headers entirely.
   - **Geometry:** discard the real route. Regenerate coordinates as a synthetic path starting at a
     fixed fake origin (in open ocean) that **preserves each segment's distance** but uses
     invented headings. Distances, speeds and pace stay exactly correct; the route's shape and
     location are gone.
   - **Elevation:** shift by a fixed offset so absolute altitude can't hint at location. Gain and
     loss are preserved.
   - **Time:** shift all timestamps so the session starts at `2020-01-01T06:00:00Z`. Intervals are
     preserved.
   - **Heart rate:** replaced by default with a plausible synthetic series (same sample times and
     field presence, smooth trend plus noise). `--keep-hr` keeps the real values, only if the user
     explicitly asks.
   - **Identity:** remove names, emails, display names, device serials and user or profile IDs.
   - **Report:** write `scrub_report.md` listing every transformation and every remaining string
     field, for the user to review.
   - **Self-check:** fail if any original token, session ID, coordinate pair, real date, or
     `.secret-denylist` entry survives anywhere in the output.
   - Scrubbed fixtures must still pass the parser and contract tests. The scrubber changes values,
     never schema.
7. 🛑 **Fixture gate.** If `fixtures/sessions/` has no scrubbed session:
   - Commit and push the scaffold, with the full secret scan passing first.
   - Write the summary entry.
   - Ask the user for **only one thing**: *"Paste your LiveTrack share link here when your activity
     has started."* Don't ask them to run commands, edit files, or touch `.secret-denylist`.
   - When the link arrives:
     1. Validate it.
     2. Launch the capture **yourself, in the background** with `--vm-probe` (e.g. in `tmux` or
        as a background task), logging to `fixtures/raw/<name>/capture.log`. The tool appends the
        session ID and token to `.secret-denylist` itself (step 5); verify that it did, without
        printing the values.
     3. Monitor until the capture process exits on its own (idle stop plus the end-of-session
        probes). Don't stop it early.
     4. Continue automatically: scrub (step 6), then the review stop below.
   - **Review stop.** Before committing any fixture, 🛑 STOP and show the user the full path to
     `fixtures/sessions/<name>/scrub_report.md` plus a short summary of what was transformed. Wait
     for `confirmed`. Fixtures in a public repo are irreversible.
   - If the session is closed before the capture finishes, the resume prompt is: "Continue
     ai-instructions/CLAUDE_INSTRUCTIONS.md from Phase 0 step 6 (scrub) onward." Resuming needs no
     manual file edits.

## Phase 1 — Findings and fetch-strategy decision

Analyse the raw capture, the HAR and the probes. Write `docs/LIVETRACK_FINDINGS.md` with tokens
redacted. It must cover:
- Data endpoints and their roles.
- What auth each one needs: URL token, cookies, headers.
- Update cadence: median and 95th-percentile time between new points.
- Fields actually present: HR, cadence, power, elevation, speed, distance.
- How the end of a session is signalled.
- What expiry looks like, if captured.
- Laptop probe results vs VM probe results.

Then decide **exactly one** strategy and record it in the findings:
- **D1 — plain HTTP works from the VM** → `FETCH_STRATEGY=http`. Proceed (expected outcome).
- **D2 — works from the laptop but not the VM** (Garmin or its CDN blocking datacenter IPs) →
  🛑 STOP. Report it. Do not build workarounds (proxies, tunnels) without the user's decision.
- **D3 — data only obtainable via a real browser** → `FETCH_STRATEGY=browser`.
  - Implement `fetch_browser.py` by intercepting network responses, not by scraping the DOM.
  - Measure peak RSS during a replayed session and record it. PROVISION will compare it with VM
    free memory.
  - Proceed.

If any parser assumption is unclear from the fixtures, say so in the findings. Do not guess field
semantics.

The findings doc is **published**. Use placeholders for session IDs and tokens, and give no real
coordinates, elevations, dates or probe IPs. Any raw evidence stays in `fixtures/raw/`.

## Phase 2 — Core

**Parser.**
- `parser.py` turns Garmin JSON into a typed `Trackpoint` with optional fields.
- On an unknown or changed schema, raise `LiveTrackSchemaError` naming the offending field.
  Never silently drop data.

**Store and repo layer.** Migrations, dedupe, and retention as specified above.

**Metrics.** Each one gets synthetic-track unit tests with hand-computed expected values:
- Moving time with pause detection.
- Rolling pace over a configurable window: by time and by distance.
- Splits, interpolated at the boundaries.
- Elevation gain and loss with hysteresis (~3 m) so GPS noise doesn't inflate them.
- HR average, HR zones (only when `ATHLETE_MAX_HR` is set), and aerobic decoupling.

**Poller.** Asyncio task per session; backoff; stale detection; end/expiry handling;
resume-on-start.

**Events and Notifier seam.** As specified above.

## Phase 3 — Replay server (the key to unattended testing)

`replay/server.py` is a fake LiveTrack that serves a scrubbed fixture session **progressively**.
- It mirrors the real endpoint paths and response schemas exactly, per the findings.
- Knobs:
  - `speed` (e.g. 10×, 20×)
  - `gap_at` / `gap_seconds` (stale simulation)
  - `error_rate` (5xx)
  - `rate_limit` (429)
  - `end_behavior`
  - `expire_after`
- Used by integration tests, end-to-end tests, and `selftest.py`.

## Phase 4 — MCP server and HTTP app

**App.** FastAPI under `BASE_PATH`, with the MCP Streamable HTTP app mounted at the secret path.

**Wiring.**
- Check the SDK docs for how its session-manager lifespan must be wired into the parent app.
- Decide stateful vs `stateless_http`; with a single worker, either works. Document the choice.
- If the SDK enforces Host/Origin (DNS-rebinding) checks, make allowed hosts configurable via
  `MCP_ALLOWED_HOSTS`. In prod this must include `kebayorantechnologies.com`.

**Selftest.** `selftest.py` (`python -m garmin_live.selftest`) does the following, then exits 0 or 1:
1. Starts the replay server in-process on `127.0.0.1` at 20×.
2. Creates a session labelled `selftest` through the **internal service API**. This is the only
   path allowed to bypass the host check; the MCP tool can never bypass it.
3. Waits for ingest and checks that metrics are populated.
4. Leaves the session in `ended` state.

**DB backup.** `db_backup.py DEST` uses the SQLite online backup API and keeps the newest 5 copies.

**MCP smoke tool.** `tools/mcp_smoke.py --url URL [--expect-session selftest]` is an MCP client that
runs `initialize`, lists tools, and calls `list_sessions` and `get_live_status`. It also checks
that a wrong secret returns 404. It must never print the secret.

## Phase 5 — Container and local prod-mimic

**Dockerfile.**
- `python:3.12-slim`, or the matching Playwright image if D3.
- Run as a non-root user.
- One uvicorn worker on port 8000.
- `HEALTHCHECK` via python `urllib` against `/garmin-mcp/health`.
- Data at `/data`.

**nginx block.** `nginx/garmin-mcp.location.conf` is the canonical prod block:
```nginx
location = /garmin-mcp { return 301 /garmin-mcp/; }
location /garmin-mcp/ {
    proxy_pass http://garmin-live:8000;      # no URI part: full path passed through (app serves under the prefix)
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;  # PROVISION adjusts if the VM derives client IP from CF-Connecting-IP
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;                      # streamable HTTP / SSE
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```

**Local stack.** `docker/docker-compose.local.yml` runs the app plus an nginx container that
includes **that same file**, so local end-to-end tests go through the real prefix routing.

**Self-host example.** `deploy/examples/docker-compose.example.yml` is the path for other people:
- The app plus Caddy, which handles TLS automatically.
- `env_file: .env`.
- A named volume for `/data`.
- `Caddyfile.example` reverse-proxies `{$DOMAIN}` to the app with streaming-friendly settings,
  serving at root (`BASE_PATH=""`).
- Placeholders only, e.g. `example.com`.

## Phase 6 — Quality gates and test suite (fix loop, max 5 attempts per gate)

1. `ruff check` and `ruff format --check`
2. `mypy --strict src/`
3. `pytest tests/unit tests/integration` with coverage ≥ 85% on `src/` (excluding `replay/`).
   Required cases:
   - Parser vs every fixture.
   - Schema-drift raises `LiveTrackSchemaError`.
   - URL validation rejects: other hosts, http, IP literals, userinfo tricks, redirects to other
     hosts.
   - Metrics against known answers.
   - Poller against replay, including:
     - incremental ingest
     - dedupe
     - gap → `stale` event
     - 5xx/429 backoff
     - end → `session_ended`
     - expiry
     - restart-resume
   - Retention purge.
   - Events are emitted; `NoopNotifier` is called.
4. **End-to-end via the local docker stack** (through nginx):
   - Build the image.
   - `/garmin-mcp/health` returns 200.
   - `mcp_smoke.py` passes against `http://localhost:<port>/garmin-mcp/mcp/<test-secret>`.
   - A wrong secret returns 404.
   - Run a full tracking session against a replay container at 20× via the MCP tools.
   - `python -m garmin_live.selftest` exits 0 inside the container.
5. **Privacy test:**
   - Run the end-to-end flow with logs captured.
   - Assert no fixture coordinate values, no fake token or session ID, and no secret appear in
     logs or tool outputs.
6. **Prefix test:** `rg` the source for root-absolute paths or routes not built from `BASE_PATH`.
   Zero hits allowed.
7. **Resource test:** record peak container memory during the replay session via
   `docker stats --no-stream` sampling. Fail if above 200 MB (http) or 900 MB (browser). Record the
   number in the findings.
8. **Portability tests:**
   - Run the e2e MCP flow twice, once with `BASE_PATH=/garmin-mcp` (through the local nginx) and
     once with `BASE_PATH=""` (root).
   - Startup with a missing or short `MCP_PATH_SECRET` fails with a clear error.
   - `docker compose -f deploy/examples/docker-compose.example.yml config` validates.
   - Every variable in `config.py` appears in `.env.example` and the README config table, and vice
     versa.
   - `rg -i 'kebayoran|thebuleganteng|/home/' src tests docker deploy .env.example` returns zero
     hits.
9. **Secret scan:** full-history `gitleaks detect` plus `tools/check_denylist.sh` must be clean.
10. **README check:** every command in the README quickstart and development sections is one you
    actually ran successfully in this session, or is marked as requiring a real domain.

## Phase 7 — Commit, push, log

- **CI.** Add `.github/workflows/ci.yml`, which runs on push and PR to `main`:
  - ruff
  - mypy
  - pytest unit and integration
  - the portability grep
  - gitleaks (pinned action or image version)

  No secrets are needed in CI. Docker e2e may be skipped in CI and documented as local-only.
- Run `git status` and confirm nothing from `fixtures/raw/`, `.env` (other than `.env.example`),
  `.secret-denylist`, `*.db`, `*.har`, or `CLAUDE_SUMMARY.md` is staged.
- Run the pre-push secret scan (CLAUDE.md rule 12). It must be clean.
- Commit with a conventional message and push to `origin main` (no force, no `--no-verify`).
- Watch CI: `gh run watch` on the triggered run. If CI fails, fix it within the same fix-loop cap,
  then push again. CI must be green for the run to count as done. If `gh` is unavailable, say so
  and ask the user to check the Actions tab.
- Append to `CLAUDE_SUMMARY.md`:
  - timestamp
  - phases completed
  - fetch strategy decision
  - gate results
  - peak memory
  - commit hash
  - any STOPs

**Done** = all gates green, pushed, CI green, findings doc and README present. Print a one-paragraph report and the next
command the user should run: provision (first time) or deploy.
