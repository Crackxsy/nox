-- 0005_memory: additions for EPIC-07 Memory & Vault beyond 0001_initial's memory_items/vault_index/
-- vault_chunks (those already carry their FTS5 shadow tables and triggers). `memory_vec` itself is
-- still created at runtime by Database.ensure_memory_vec() only when sqlite-vec loads; vec0 rowids
-- are shared between memory_items and vault_chunks, so `vec_map` gives each embedded row its own
-- rowid instead of the two id spaces colliding.

CREATE TABLE IF NOT EXISTS vec_map (
    id      INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL CHECK (kind IN ('memory_item', 'vault_chunk')),
    ref_id  INTEGER NOT NULL,
    UNIQUE (kind, ref_id)
);

-- Previous version of a Nox-written vault note, kept for rollback (ST-07-04 "keep previous
-- version", scoped in this pass to Nox's own Inbox writes only).
CREATE TABLE IF NOT EXISTS vault_note_versions (
    id         INTEGER PRIMARY KEY,
    path       TEXT NOT NULL,
    content    TEXT NOT NULL,
    saved_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vault_note_versions_path ON vault_note_versions(path, id DESC);
