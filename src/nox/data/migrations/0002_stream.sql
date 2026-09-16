-- 0002_stream: Stream Bot tables (Spec v0.2 §7, extends vault 03 - Data Model Layer 2).
-- Timestamps are ISO-8601 UTC text, matching 0001_initial. chat_events.text retention is
-- short-lived and configurable (stream.chat.retain_raw_text_days, pending approval Spec v0.2 §7);
-- viewers/viewer_memory follow the decided 12-month-inactivity default (FR-7.9).

CREATE TABLE IF NOT EXISTS stream_sessions (
    id                   INTEGER PRIMARY KEY,
    started_at           TEXT    NOT NULL,
    ended_at             TEXT,
    mode                 TEXT    NOT NULL DEFAULT 'live',   -- live | recording_only
    preflight_json       TEXT    NOT NULL DEFAULT '{}',
    peak_viewers         INTEGER NOT NULL DEFAULT 0,
    chat_message_count   INTEGER NOT NULL DEFAULT 0,
    funken_awarded_total REAL    NOT NULL DEFAULT 0,
    summary              TEXT    NOT NULL DEFAULT '',
    ended_reason         TEXT                                -- manual | panic | obs_lost | crash
);
CREATE INDEX IF NOT EXISTS idx_stream_sessions_started ON stream_sessions(started_at);

CREATE TABLE IF NOT EXISTS viewers (
    twitch_user_id TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    loyalty_tier   TEXT NOT NULL DEFAULT 'none',
    funken_balance REAL NOT NULL DEFAULT 0,
    opt_out        INTEGER NOT NULL DEFAULT 0,               -- 0/1 (FR-9.15 viewer privacy)
    retain_until   TEXT                                       -- 12 months inactivity (FR-7.9)
);
CREATE INDEX IF NOT EXISTS idx_viewers_retain ON viewers(retain_until);

CREATE TABLE IF NOT EXISTS viewer_memory (
    id              INTEGER PRIMARY KEY,
    viewer_id       TEXT    NOT NULL REFERENCES viewers(twitch_user_id) ON DELETE CASCADE,
    what            TEXT    NOT NULL,
    source_event_id INTEGER,
    confidence      REAL    NOT NULL DEFAULT 0.5,
    created_at      TEXT    NOT NULL,
    retain_until    TEXT
);
CREATE INDEX IF NOT EXISTS idx_viewer_memory_viewer ON viewer_memory(viewer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_viewer_memory_retain ON viewer_memory(retain_until);

CREATE TABLE IF NOT EXISTS chat_events (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER REFERENCES stream_sessions(id) ON DELETE CASCADE,
    ts           TEXT    NOT NULL,
    viewer_id    TEXT    REFERENCES viewers(twitch_user_id) ON DELETE SET NULL,
    kind         TEXT    NOT NULL DEFAULT 'message',  -- message|follow|sub|bits|raid|cheer|
                                                        -- points_redemption|command
    text         TEXT    NOT NULL DEFAULT '',
    priority     INTEGER NOT NULL DEFAULT 0,
    handled_by   TEXT    NOT NULL DEFAULT '',          -- relevance|command|moderation|ignored
    decision     TEXT    NOT NULL DEFAULT '',
    retain_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat_events_session ON chat_events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_chat_events_viewer ON chat_events(viewer_id, ts);
CREATE INDEX IF NOT EXISTS idx_chat_events_retain ON chat_events(retain_until);

CREATE TABLE IF NOT EXISTS funken_ledger (
    id            INTEGER PRIMARY KEY,
    viewer_id     TEXT    NOT NULL REFERENCES viewers(twitch_user_id) ON DELETE CASCADE,
    delta         REAL    NOT NULL,
    reason        TEXT    NOT NULL DEFAULT '',
    balance_after REAL    NOT NULL,
    ts            TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'earn'   -- earn|spend|admin|decay
);
CREATE INDEX IF NOT EXISTS idx_funken_ledger_viewer ON funken_ledger(viewer_id, ts);

CREATE TABLE IF NOT EXISTS moderation_actions (
    id            INTEGER PRIMARY KEY,
    ts            TEXT    NOT NULL,
    viewer_id     TEXT    REFERENCES viewers(twitch_user_id) ON DELETE SET NULL,
    chat_event_id INTEGER REFERENCES chat_events(id) ON DELETE SET NULL,
    stage         TEXT    NOT NULL,  -- ignore|assess|inform|moderate|timeout_confirmed|ban_confirmed
    hard_list_hit INTEGER NOT NULL DEFAULT 0,
    confirmed_by  TEXT
);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_viewer ON moderation_actions(viewer_id, ts);
CREATE INDEX IF NOT EXISTS idx_moderation_actions_chat_event ON moderation_actions(chat_event_id);

-- reserved, pending OP-B (first minigame decision)
CREATE TABLE IF NOT EXISTS minigame_sessions (
    id                 INTEGER PRIMARY KEY,
    session_id         INTEGER REFERENCES stream_sessions(id) ON DELETE CASCADE,
    game_id            TEXT    NOT NULL,
    started_at         TEXT    NOT NULL,
    ended_at           TEXT,
    participants_json  TEXT    NOT NULL DEFAULT '[]',
    result_json        TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_minigame_sessions_session ON minigame_sessions(session_id);
