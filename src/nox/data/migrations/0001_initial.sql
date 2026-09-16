-- 0001_initial: every table of vault 03 - Data Model (Layer 2). memory_vec is created at runtime
-- by Database.ensure_memory_vec() only when sqlite-vec loads. Timestamps are ISO-8601 UTC text.

CREATE TABLE IF NOT EXISTS state_checkpoints (
    id          INTEGER PRIMARY KEY,
    version     INTEGER NOT NULL,
    ts          TEXT    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    json        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_state_checkpoints_ts ON state_checkpoints(ts);

CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    tool         TEXT NOT NULL DEFAULT '',
    action       TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    decision     TEXT NOT NULL,
    result       TEXT NOT NULL DEFAULT '',
    task_id      TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    mode         TEXT NOT NULL,
    privacy_mode TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

CREATE TABLE IF NOT EXISTS turns (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    role         TEXT NOT NULL,
    text         TEXT NOT NULL,
    provider     TEXT NOT NULL DEFAULT '',
    latency_ms   INTEGER,
    retain_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_turns_retain ON turns(retain_until);

CREATE TABLE IF NOT EXISTS memory_items (
    id            INTEGER PRIMARY KEY,
    type          TEXT NOT NULL,
    text          TEXT NOT NULL,
    importance    REAL NOT NULL DEFAULT 0.5,
    source        TEXT NOT NULL DEFAULT '',
    vault_path    TEXT,
    created_at    TEXT NOT NULL,
    last_used_at  TEXT,
    retain_until  TEXT,
    privacy_class TEXT NOT NULL DEFAULT 'normal'
);
CREATE INDEX IF NOT EXISTS idx_memory_items_vault ON memory_items(vault_path);
CREATE INDEX IF NOT EXISTS idx_memory_items_retain ON memory_items(retain_until);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
    text, content='memory_items', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_items_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE OF text ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO memory_items_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS vault_index (
    path        TEXT PRIMARY KEY,
    mtime       REAL NOT NULL,
    hash        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT '',
    tags_json   TEXT NOT NULL DEFAULT '[]',
    indexed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_chunks (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL REFERENCES vault_index(path) ON DELETE CASCADE,
    ord   INTEGER NOT NULL,
    text  TEXT NOT NULL,
    hash  TEXT NOT NULL,
    UNIQUE(path, ord)
);

CREATE VIRTUAL TABLE IF NOT EXISTS vault_chunks_fts USING fts5(
    text, content='vault_chunks', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS vault_chunks_ai AFTER INSERT ON vault_chunks BEGIN
    INSERT INTO vault_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS vault_chunks_ad AFTER DELETE ON vault_chunks BEGIN
    INSERT INTO vault_chunks_fts(vault_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS vault_chunks_au AFTER UPDATE OF text ON vault_chunks BEGIN
    INSERT INTO vault_chunks_fts(vault_chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO vault_chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS health_history (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    component TEXT NOT NULL,
    status    TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_health_history_component ON health_history(component, ts);

CREATE TABLE IF NOT EXISTS temporary_grants (
    grant_id    TEXT PRIMARY KEY,
    agent       TEXT NOT NULL DEFAULT '*',
    tool        TEXT NOT NULL DEFAULT '*',
    action_glob TEXT NOT NULL DEFAULT '*',
    target_glob TEXT NOT NULL DEFAULT '*',
    scope       TEXT NOT NULL DEFAULT '',
    max_risk    TEXT NOT NULL DEFAULT 'low',
    expires_at  TEXT NOT NULL,
    origin      TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_temporary_grants_expires ON temporary_grants(expires_at);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    priority        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    payload_json    TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT,
    error           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority DESC, created_at);

-- v0.8 reserved (paired remote devices); columns fixed now so migrations stay linear.
CREATE TABLE IF NOT EXISTS paired_devices (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'remote',
    public_key   TEXT NOT NULL DEFAULT '',
    paired_at    TEXT NOT NULL,
    last_seen_at TEXT
);
