-- 0006_clips: Clip Pipeline tables (Spec v0.6 §7, EPIC-15). `clips.file_path` stays NOT NULL by
-- design: an unclipped highlight during a recording-only session (no replay-buffer file yet) is a
-- `clip_markers` row instead, never a `clips` row with no file. `parent_clip_id` links a trim to
-- its source clip; trimming always writes a new file, never edits the parent's.

CREATE TABLE IF NOT EXISTS clips (
    id                TEXT    PRIMARY KEY,
    source            TEXT    NOT NULL,             -- event | manual | marker_promoted
    trigger_kind      TEXT    NOT NULL,              -- rl.goal | rl.save | chat_hype | user_marker | ...
    origin_event_id   TEXT    NOT NULL DEFAULT '',
    session_id        TEXT    NOT NULL DEFAULT '',
    file_path         TEXT    NOT NULL,
    duration_s        REAL    NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    thumbnail_path    TEXT,
    tags              TEXT    NOT NULL DEFAULT '[]', -- JSON array
    status            TEXT    NOT NULL DEFAULT 'new', -- new | reviewed | exported | discarded
    parent_clip_id    TEXT    REFERENCES clips(id) ON DELETE SET NULL,
    checksum          TEXT    NOT NULL DEFAULT '',
    notes             TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status, created_at);
CREATE INDEX IF NOT EXISTS idx_clips_session ON clips(session_id);
CREATE INDEX IF NOT EXISTS idx_clips_checksum ON clips(checksum);
CREATE INDEX IF NOT EXISTS idx_clips_parent ON clips(parent_clip_id);

-- Recording-only marker path (Spec v0.6 §4.3): a DaVinci-style timestamp marker, promotable to a
-- real `clips` row later once the recording file exists on disk.
CREATE TABLE IF NOT EXISTS clip_markers (
    id                TEXT    PRIMARY KEY,
    session_id        TEXT    NOT NULL DEFAULT '',
    timestamp_s       REAL    NOT NULL,
    reason            TEXT    NOT NULL DEFAULT '',
    promoted_clip_id  TEXT    REFERENCES clips(id) ON DELETE SET NULL,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clip_markers_session ON clip_markers(session_id, timestamp_s);
