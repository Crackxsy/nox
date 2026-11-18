-- 0004_pm: PM index mirror (`pm_items`) - a fast-query mirror of the vault's Project/Epic/
-- Story notes, rebuilt from the vault by `nox.pm.index.PmIndex.rebuild`/`upsert`. The vault note is
-- always authoritative on conflict: the vault wins.

CREATE TABLE IF NOT EXISTS pm_items (
    id         TEXT PRIMARY KEY,       -- vault id: the note's filename prefix
    kind       TEXT NOT NULL,          -- project | epic | story
    title      TEXT NOT NULL,
    status     TEXT NOT NULL,
    priority   TEXT,
    estimate   TEXT,
    epic_id    TEXT,
    project_id TEXT,
    note_path  TEXT NOT NULL,
    note_hash  TEXT NOT NULL,          -- sha256 of the note's raw text (write-back conflict check)
    created    TEXT,
    updated    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pm_items_kind_status ON pm_items(kind, status);
CREATE INDEX IF NOT EXISTS idx_pm_items_epic ON pm_items(epic_id);
CREATE INDEX IF NOT EXISTS idx_pm_items_project ON pm_items(project_id);
