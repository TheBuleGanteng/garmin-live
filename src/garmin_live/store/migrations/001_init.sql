-- 001_init: sessions, trackpoints, events, schema_version
-- Timestamps are ISO-8601 UTC strings ("...+00:00") so they sort lexically.

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,            -- short app-local id (never Garmin's)
    label              TEXT NOT NULL,
    garmin_session_id  TEXT NOT NULL,
    token              TEXT NOT NULL,
    api_base           TEXT NOT NULL,               -- https://livetrack.garmin.com or a replay server
    status             TEXT NOT NULL,               -- active | ended | expired | stopped | error
    end_reason         TEXT,                        -- garmin_end | expired | max_hours | stopped | error
    created_at         TEXT NOT NULL,
    garmin_start       TEXT,
    garmin_end         TEXT,
    ended_at           TEXT,
    last_poll_at       TEXT,
    last_point_at      TEXT,
    last_error         TEXT,
    error_count        INTEGER NOT NULL DEFAULT 0,
    poll_interval_s    REAL    NOT NULL,
    stale              INTEGER NOT NULL DEFAULT 0,
    activity_type      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_status_created ON sessions (status, created_at);

CREATE TABLE IF NOT EXISTS trackpoints (
    session_id        TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    ts                TEXT NOT NULL,
    lat               REAL NOT NULL,
    lon               REAL NOT NULL,
    total_distance_m  REAL NOT NULL,
    total_duration_s  REAL NOT NULL,
    speed_mps         REAL,
    altitude_m        REAL,
    heart_rate_bpm    INTEGER,
    cadence_spm       INTEGER,
    duration_s        REAL,
    activity_type     TEXT,
    event_types       TEXT NOT NULL DEFAULT '[]',   -- JSON list
    PRIMARY KEY (session_id, ts)                    -- dedupe by timestamp
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    type        TEXT NOT NULL,                       -- new_points | split_completed | stale | resumed | session_ended | ingest_error
    payload     TEXT NOT NULL DEFAULT '{}',          -- JSON object; never contains coordinates or tokens
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session_id ON events (session_id, id);
