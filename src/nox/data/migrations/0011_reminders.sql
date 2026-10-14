-- 0011_reminders: the user's own reminders ("erinner mich morgen um 9 an den Zahnarzt").
-- Set through the `reminder.set` tool or the dashboard, fired by `nox.day.reminders` as a spoken
-- proactive notification when `due_at` passes. A reminder is never deleted on firing: `fired_at`
-- is set, so the dashboard can still show what Nox said this morning, and a restart cannot fire
-- it twice. `cancelled_at` is the user's withdrawal. `source` says who created it (`tool:<agent>`
-- or `dashboard`). Timestamps are ISO-8601 UTC text, matching 0001_initial's convention.

CREATE TABLE IF NOT EXISTS reminders (
    id            TEXT    PRIMARY KEY,
    text          TEXT    NOT NULL,
    due_at        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    source        TEXT    NOT NULL DEFAULT '',
    fired_at      TEXT,
    cancelled_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_at);
