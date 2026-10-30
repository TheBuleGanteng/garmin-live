# garmin-live

A small, self-hostable **read-only bridge from a Garmin LiveTrack share link to Claude**, exposed as a
remote [MCP](https://modelcontextprotocol.io) server.

You start a run, share your LiveTrack link with your own server, and during (or shortly after) the
workout you can ask Claude things like *"how's my pace holding up?"*, *"what were my last three
kilometre splits?"* or *"is my heart rate drifting?"*. Claude calls the server's tools and gets
compact, metric, token-efficient JSON back.

> **Status:** pre-alpha scaffold. Nothing works yet; the build is in progress.

## What it does — and doesn't

- **Pull model.** Claude only sees data when you ask a question. Nothing is pushed to Claude, and
  nothing runs on a schedule except the poller that ingests your LiveTrack session.
- **Read-only.** The server never writes to Garmin; it only reads the public share link you give it.
- **Single user per deployment.** One instance serves one athlete.
- **No raw coordinates** are ever returned by the tools (v1). Metrics only.
- Push notifications / alerting are *not* built. The code keeps a seam for them (an `events` table
  and a `Notifier` protocol) but ships only a no-op implementation.

## Disclaimer

This project is **unofficial** and is not affiliated with, endorsed by, or supported by Garmin Ltd.
or its subsidiaries. It relies on **undocumented LiveTrack endpoints** that can change or break at
any time without notice. Only use it with **your own** LiveTrack links. You are responsible for
complying with Garmin's terms of service.

## Security model

- Each deployment serves **one person**.
- claude.ai custom connectors support only *authless* or *OAuth* servers, so v1 uses an
  **unguessable URL**: `https://your-domain/mcp/<MCP_PATH_SECRET>`. **That URL is the credential.**
  Anyone who has it can read your live sessions. Treat it like a password.
- Any other path under `/mcp/` returns `404`, so probing reveals nothing. The secret is compared
  in constant time.
- Optional IP allow-list middleware (`ENFORCE_IP_ALLOWLIST=true`) for deployments that can pin
  client IPs.
- **To rotate the secret:** generate a new value, update `MCP_PATH_SECRET` in your `.env`, restart
  the container, and update the connector URL in claude.ai.
- Location data is sensitive. The tools never expose coordinates, but the database on your host
  does contain them for the retention window. Keep the host private.

## Requirements

- An always-on host with **Docker** (and Docker Compose v2).
- A **public HTTPS domain** pointing at that host (claude.ai must be able to reach the server).
- Python 3.12 only if you want to develop locally.

## Quickstart (standalone, Caddy handles TLS)

_Coming with the first release — see `deploy/examples/`._

## Behind an existing nginx (sub-path deployment)

_Coming with the first release — see `nginx/garmin-mcp.location.conf`._

## Adding the connector in claude.ai

_Coming with the first release._

## Tool reference

_Coming with the first release._

## Configuration

Every setting is an environment variable. `.env.example` documents each one with a safe
placeholder; copy it to `.env` and edit.

_The full table is generated from `.env.example` with the first release._

## Data retention

Sessions, trackpoints and events older than `RETENTION_DAYS` (default 30) are purged by a
background job. The SQLite database lives on the `/data` volume.

## Development

_Coming with the first release: running the tests, the replay server, capturing your own fixtures,
and the privacy-scrubbing step._

## About `ai-instructions/`

That folder contains the maintainer's AI build/deploy runbooks (used with Claude Code). It is
**not needed to self-host** and you can ignore it.

## License

[MIT](LICENSE).
