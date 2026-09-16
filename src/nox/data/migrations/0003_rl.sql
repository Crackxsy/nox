-- 0003_rl: Rocket League Stage 1 tables (Spec v0.3 §7, extends vault 03 - Data Model Layer 2).
-- Timestamps are ISO-8601 UTC text, matching 0001_initial/0002_stream. Retention (rl_events/
-- rl_matches) is config-driven (rl.retention.*, pending approval per Spec §7/§15). `rl_replays`
-- rows are never deleted by Nox except on explicit request (Spec §7) - no retain_until column.

CREATE TABLE IF NOT EXISTS rl_replays (
    id              INTEGER PRIMARY KEY,
    file_path       TEXT    NOT NULL UNIQUE,
    file_hash       TEXT    NOT NULL DEFAULT '',
    parsed_at       TEXT    NOT NULL,
    parser_version  TEXT    NOT NULL DEFAULT '',
    parse_status    TEXT    NOT NULL DEFAULT 'failed',   -- ok | partial | failed
    header_json     TEXT    NOT NULL DEFAULT '{}',
    matched_match_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rl_replays_parsed_at ON rl_replays(parsed_at);
CREATE INDEX IF NOT EXISTS idx_rl_replays_status ON rl_replays(parse_status);

CREATE TABLE IF NOT EXISTS rl_matches (
    id               INTEGER PRIMARY KEY,
    started_at       TEXT    NOT NULL,
    ended_at         TEXT,
    score_self       INTEGER,
    score_opponent   INTEGER,
    result           TEXT    NOT NULL DEFAULT 'unknown',  -- win | loss | unknown
    summary_short    TEXT    NOT NULL DEFAULT '',
    summary_detailed TEXT    NOT NULL DEFAULT '',
    replay_id        INTEGER REFERENCES rl_replays(id) ON DELETE SET NULL,
    ended_reason     TEXT    NOT NULL DEFAULT 'unknown',  -- normal | abandoned | disconnected | unknown
    retain_until     TEXT
);
CREATE INDEX IF NOT EXISTS idx_rl_matches_started ON rl_matches(started_at);
CREATE INDEX IF NOT EXISTS idx_rl_matches_retain ON rl_matches(retain_until);

CREATE TABLE IF NOT EXISTS rl_events (
    id           INTEGER PRIMARY KEY,
    match_id     INTEGER REFERENCES rl_matches(id) ON DELETE SET NULL,
    ts           TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                        -- goal|overtime|boost_low|demo|save|...
    source       TEXT    NOT NULL,                         -- hud | replay
    confidence   REAL    NOT NULL DEFAULT 0.0,
    payload_json TEXT    NOT NULL DEFAULT '{}',
    retain_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_rl_events_match ON rl_events(match_id, ts);
CREATE INDEX IF NOT EXISTS idx_rl_events_retain ON rl_events(retain_until);
