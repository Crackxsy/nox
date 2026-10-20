-- 0014_consents: stored consent for recording others (§ 201 StGB, "Vertraulichkeit des Wortes").
-- `mic.record_long` captures speech that may include other people (a lesson, a meeting). Recording
-- them is only lawful with their consent, so Nox keeps a row per context: who was asked, when, and
-- the exact wording they agreed to. No matching, active row for a context => the tool refuses; the
-- decision lives in the database, below the model, not in a prompt. `revoked_at` withdraws consent
-- without deleting the record of what was agreed. Timestamps are ISO-8601 UTC text (0001 convention).

CREATE TABLE IF NOT EXISTS consents (
    id          TEXT PRIMARY KEY,
    context     TEXT NOT NULL,   -- a stable key for the situation, e.g. "lesson:math-mr-weber"
    who         TEXT NOT NULL,   -- who consented (free text; a class, a person)
    wording     TEXT NOT NULL,   -- the exact words they agreed to
    granted_at  TEXT NOT NULL,
    revoked_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_consents_context ON consents(context);
