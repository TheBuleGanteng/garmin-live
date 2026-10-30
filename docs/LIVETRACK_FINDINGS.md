# LiveTrack findings and fetch-strategy decision

Observations from one recorded LiveTrack session (fixture `run1`, a ~15-minute run), captured with
`tools/capture_livetrack.py` in headless Chromium plus plain-HTTP probes from a laptop and from a
cloud VM. Everything below uses placeholders for identifiers. No coordinates, elevations, dates or
IP addresses from the real session appear here; the raw capture is gitignored and only the
scrubbed fixture in `fixtures/sessions/run1/` is published.

LiveTrack is an undocumented API. Everything here is an observation from one session and can
change without notice. Where a field's meaning is not evident from the data, this document says so
rather than guessing.

## Decision: **D1 — plain HTTP works from the VM → `FETCH_STRATEGY=http`**

A fresh plain-HTTP client (no browser, no special User-Agent) can bootstrap authentication from the
share URL and read all data endpoints, and it behaves identically from the laptop and from the GCP
VM. Garmin does not block the datacenter IP. Playwright therefore stays a dev-only dependency; the
`browser` strategy is reserved for a future build and `fetch_browser.py` is a documented stub.

## Share URL shapes

- Long form: `https://livetrack.garmin.com/session/<sessionId>/token/<token>`
  `<sessionId>` is a UUID; `<token>` is an opaque ~29-character alphanumeric string.
- Short form: `https://gar.mn/<code>` → `301` to the long form. The app accepts only the long
  form (host exactly `livetrack.garmin.com`, path `/session/<uuid>/token/<token>`); the short form
  must be expanded by the user or by a dedicated resolver that re-validates the final URL.

## Data endpoints and their roles

| Endpoint (GET) | Role | Query | Page poll interval |
|---|---|---|---|
| `/session/<sessionId>/token/<token>` | HTML share page. Sets the `livetrack_csrf` cookie and embeds `<meta name="csrf-token" content="…">`. **Auth bootstrap.** | — | once |
| `/api/v2/sessions/<sessionId>?token=<token>` | Session metadata: `start`, `end`, `viewable`, `sessionType`, `privacyLevels`, publisher ids, device `unitId`, a session-level `position`, `activeMultiDayActivities`, `courseIds`. | `token` | ~120 s |
| `/api/sessions/<sessionId>/track-points/common?token=<token>[&begin=<iso>]` | Trackpoints. Without `begin`: the full list so far. With `begin`: only points with `dateTime > begin`. Responses are **incremental** (no point was ever repeated across polls). The page sets `begin` = last received `dateTime` + 1 ms. | `token`, `begin` | ~15 s |
| `/api/device/<unitId>/capabilities?userProfileId=<id>` | Device capabilities; returned `{}` for this device. | `userProfileId` | once |
| `/api/user/<guid>/personal-url` | Vanity-URL lookup; `404` with an `errors[]` body when none is set. Not needed. | — | once |

Non-LiveTrack requests made by the page (analytics, Sentry, Cloudflare RUM, Google Maps RPC, a
Cloudflare geolocation worker) are irrelevant to ingestion and are excluded from fixtures.

## Authentication: what each endpoint needs

Every `/api/...` endpoint returns **`403` with an empty body** unless the request carries **both**:

1. the `livetrack_csrf` cookie (HttpOnly, Secure, SameSite; a *session* cookie with no
   `Max-Age`/`Expires`), and
2. the `livetrack-csrf-token` request header, whose value is the `content` of the
   `<meta name="csrf-token">` tag in the share page HTML (a UUID-shaped string).

The two values differ and are paired server-side: cookie alone → 403; header alone → 403;
header set to the cookie value → 403; a pair taken from one page load → 200. The `token` query
parameter is also required (it is part of the share URL). Nothing else matters: User-Agent,
Referer, Accept, the `x-garmin-livetrack-user-guid` header, Sentry tracing headers and the other
cookies (`__cflb`, analytics) can all be omitted.

A pair obtained once kept working for the whole capture and for at least 90 minutes afterwards.
The poller should bootstrap once per session and **re-bootstrap on any 403**.

Bootstrap cost measured from the laptop: one HTML GET (~90 KB) plus the API calls, about 1.3 s.

## Update cadence

| Measure | Value |
|---|---|
| Time between new trackpoints (median) | 10 s |
| Time between new trackpoints (95th percentile) | 11 s |
| Largest gap between points | 191 s (coincides with the only zero-speed point; see `durationSecs` below) |
| Points per non-empty response | 1–7 (most often 2) |
| Page's track-points poll interval | 15 s |
| Page's session-metadata poll interval | 120 s |
| First points appeared | ~6 min after the session `start`, delivered as a batch |

The poller default `POLL_MIN_SECONDS=20` is above the page's own 15 s and well above the 10 s
point cadence, so a 20 s poll typically yields 2 new points.

## Fields actually present on a trackpoint

All 72 points carried every field below (no field was ever missing):

| Field | Type | Observed |
|---|---|---|
| `dateTime` | ISO-8601 UTC string, ms precision | monotonic; used for `begin` |
| `reportedTime` | ISO-8601 UTC string | differs from `dateTime` by roughly −34 s to +22 s (median −17 s); meaning not evident, **not used** |
| `position.lat`, `position.lon` | float degrees | present on every point |
| `altitude` | int metres | constant for the whole (flat) session; `elevationSource` = `GPS` |
| `speed`, `speedMetersPerSec` | float m/s | `speed` equals `speedMetersPerSec` rounded to 3 decimals |
| `distanceMeters`, `totalDistanceMeters` | float m | identical on every point (cumulative distance) |
| `durationSecs`, `totalDurationSecs` | float s | equal on most points; `totalDurationSecs − durationSecs` grows to 233 s around the 191 s gap. Consistent with `durationSecs` excluding paused time, but **this is an inference from one session**; the app reports both and computes its own moving time from the points |
| `heartRateBeatsPerMin` | int | present |
| `cadenceCyclesPerMin` | int | present; 0 on the paused point |
| `activityType` | enum string | `RUNNING` |
| `eventTypes` | list of enum strings | usually `[]`; one point carried `["LAP"]` |

**Not present:** power, temperature, per-point lap number, any battery or device status. Tools
that would need them return `null` with a reason.

## How the end of a session is signalled

While the activity is live, the session body has `end` = `start` + 24 h (a placeholder) and an
`activeMultiDayActivities` list. When the athlete stops the activity:

- `end` is rewritten to the actual end time (observed: last trackpoint `dateTime` + 45 s), and
- `activeMultiDayActivities` disappears from the body.

The page observed this on its next 120 s session poll. Between polls, `start` and `end` also
jitter by about 1 ms, so a naive "end changed" check would misfire. The poller therefore treats
either of these as the end-of-session signal: `end` moved **earlier by more than a minute** than
the value seen on a previous poll, or `end <= now` (applied only when the session's own
timeline is current, so replayed fixtures are not ended by the wall clock). It then emits
`session_ended` and stops polling. `viewable` stayed `true` throughout.

After the end, the track-points endpoint kept returning the complete list (with `begin`, an empty
list) and the session endpoint kept returning the ended body, for at least the 30-minute post-end
probe window and again 68 minutes after the last point.

## Expiry

Not captured within this run: the share link was still fully readable 68 minutes after the end.
Expiry is expected to show up as a non-200 (e.g. `404`/`410`) or `viewable: false`; the poller
treats either as `expired`. Re-check later with
`tools/capture_livetrack.py <url> --name run1 --probe-only` and update this section.

## Laptop probe vs VM probe

Identical results from both vantage points, for every endpoint and every header variant:

| Variant | session | track-points | capabilities |
|---|---|---|---|
| (a) no extra headers | 403 | 403 | 403 |
| (b) browser-like headers (UA, Accept, Referer) | 403 | 403 | 403 |
| (c) observed headers + cookies | 200 | 200 | 200 |
| **fresh bootstrap** (page → cookie + meta token → API) | 200 | 200 | — |

Response headers show Cloudflare in front (`server: cloudflare`, `cf-cache-status: DYNAMIC`);
no rate limiting (`429`) was observed at the page's 15 s cadence.

## Notes for the parser

- Trackpoints: parse the fields above into a typed `Trackpoint`; unknown or missing required keys
  raise `LiveTrackSchemaError` naming the field. Types seen: `altitude` int, `speed` float or int
  (one point had an int `0`).
- Session: `start`/`end` ISO strings; `viewable` bool; `activeMultiDayActivities` optional.
- Everything else is passed through untouched; the app never invents fields.

## Fixture notes (`fixtures/sessions/run1`)

The scrubber regenerates the route from a fixed open-ocean origin and preserves every consecutive
segment distance (measured max error 0.000 m over 72 segments). Two totals appear in
`scrub_report.md`, and both are correct:

- `trackpoint_only_path_total_m` = 1059 m: the sum of consecutive distances between the 72
  time-ordered trackpoints.
- `synthetic_path_total_m_all_pairs` = 1195 m: the same plus one leading segment (136 m) from the
  session-level `position` in the session-metadata body, which carries the session `start`
  timestamp and therefore sorts first in the synthetic path.

Garmin's own `totalDistanceMeters` on the last point (1022 m) is a third, independent figure and is
copied through unchanged. Metric tests use Garmin's distance fields and the preserved segment
distances; neither depends on the synthetic path's total.

## Resource measurements

Peak container memory for the `http` strategy is recorded here by Phase 6: _pending_.
