-- 0010_notifications: persist EPIC-19's proactive notification store (ST-19-08 follow-up).
-- `nox.proactive.store.NotificationStore` was an in-memory-only ring buffer (see the now-stale
-- note on `ProactiveConfig`); this table lets it survive a restart. `id` is the same uuid4 hex
-- string `nox.proactive.models.NotificationRecord.id` already carries (the public id used by
-- `proactive.status.read` and the dashboard), kept as the primary key rather than introducing a
-- second id. `title`/`body` split the record's text (currently only `body` is populated - `title`
-- is reserved for a future notification shape, never backfilled); `channel`/`spoken`/`announced`/
-- `suppressed_reason` mirror the rest of `NotificationRecord` so a restart never loses information
-- the in-memory buffer used to carry. `dismissed_at` backs the new `proactive.notification.dismiss`
-- request; `expires_at` is an optional per-notification TTL. Timestamps are ISO-8601 UTC text,
-- matching 0001_initial's convention.

CREATE TABLE IF NOT EXISTS proactive_notifications (
    id                 TEXT    PRIMARY KEY,
    created_at         TEXT    NOT NULL,
    priority           TEXT    NOT NULL,
    kind               TEXT    NOT NULL,
    title              TEXT    NOT NULL DEFAULT '',
    body               TEXT    NOT NULL DEFAULT '',
    source             TEXT    NOT NULL DEFAULT '',
    channel            TEXT    NOT NULL DEFAULT '',
    spoken             INTEGER NOT NULL DEFAULT 0,
    announced          INTEGER NOT NULL DEFAULT 0,
    suppressed_reason  TEXT    NOT NULL DEFAULT '',
    dismissed_at       TEXT,
    expires_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_proactive_notifications_created ON proactive_notifications(created_at);
CREATE INDEX IF NOT EXISTS idx_proactive_notifications_dismissed ON proactive_notifications(dismissed_at);
