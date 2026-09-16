-- 0009_vision: Vision Stage 2 (Spec v0.9 §7, EPIC-18, ST-18-01..06). Two tables, mirroring
-- 0003_rl's pattern. `rl_vision_frames` is the short-retention raw detection log written by
-- `nox.rl.vision.RlVisionPersistenceService` from `rl.vision.detections` (retain_until,
-- config-driven `rl.vision.detections_retain_hours` - Spec §7 open point's proposed minimization
-- default "not persisted beyond session unless a callout fired" is approximated here as a short
-- bounded window rather than an unretained-only store, so the post-match rotation analysis - run
-- moments after `rl.match_ended` - can still read the match's own frames). `rl_vision_analysis` is
-- the durable per-match rough-rotation-analysis result plus the short coaching text queued right
-- after the match (Personality v1 B.9: very short right after the match). Timestamps are ISO-8601
-- UTC text, matching 0001_initial/0003_rl.

CREATE TABLE IF NOT EXISTS rl_vision_frames (
    id           INTEGER PRIMARY KEY,
    match_id     INTEGER REFERENCES rl_matches(id) ON DELETE SET NULL,
    ts           TEXT    NOT NULL,
    entity       TEXT    NOT NULL,                     -- ball | car
    confidence   REAL    NOT NULL DEFAULT 0.0,
    x            REAL    NOT NULL,
    y            REAL    NOT NULL,
    w            REAL    NOT NULL,
    h            REAL    NOT NULL,
    team         TEXT,                                  -- self | opponent | NULL
    backend      TEXT    NOT NULL DEFAULT 'none',        -- none | opencv | onnx
    retain_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_rl_vision_frames_match ON rl_vision_frames(match_id, ts);
CREATE INDEX IF NOT EXISTS idx_rl_vision_frames_retain ON rl_vision_frames(retain_until);

CREATE TABLE IF NOT EXISTS rl_vision_analysis (
    id                         INTEGER PRIMARY KEY,
    match_id                   INTEGER REFERENCES rl_matches(id) ON DELETE SET NULL,
    analyzed_at                TEXT    NOT NULL,
    frames_analyzed            INTEGER NOT NULL DEFAULT 0,
    ball_side_ratio            REAL,                     -- fraction of ball detections on self half
    avg_self_car_ball_distance REAL,                      -- rough proximity signal (frame-fraction)
    coaching_summary           TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_rl_vision_analysis_match ON rl_vision_analysis(match_id);
