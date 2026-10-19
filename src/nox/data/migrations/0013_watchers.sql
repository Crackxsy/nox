-- 0013_watchers: "sag mir, wenn ..." - things Nox checks on a schedule and reports on.
-- (0012 is left to the consents table of the sandbox wave; migration ids need not be contiguous.)
-- A watcher is one read-only tool call (`tool` + JSON `args`), a structured condition on one value
-- of its answer (`path`, `op`, JSON `value`, optional text anchor `near`), an interval and a
-- cooldown. Created through the `watch.create` tool (asks first) and listed/deleted from the
-- dashboard. `last_fired_hash` is the value last reported: the same value is never reported twice,
-- and for `changed` it is the baseline the next value is compared with. `next_check_at` makes a
-- watcher survive a restart without checking early. Timestamps are ISO-8601 UTC text.

CREATE TABLE IF NOT EXISTS watchers (
    id               TEXT    PRIMARY KEY,
    title            TEXT    NOT NULL,
    tool             TEXT    NOT NULL,
    args             TEXT    NOT NULL DEFAULT '{}',
    path             TEXT    NOT NULL DEFAULT '',
    near             TEXT    NOT NULL DEFAULT '',
    op               TEXT    NOT NULL,
    value            TEXT    NOT NULL DEFAULT 'null',
    interval_min     INTEGER NOT NULL,
    cooldown_min     INTEGER NOT NULL,
    created_at       TEXT    NOT NULL,
    source           TEXT    NOT NULL DEFAULT '',
    next_check_at    TEXT    NOT NULL,
    last_checked_at  TEXT,
    last_value       TEXT    NOT NULL DEFAULT '',
    last_fired_at    TEXT,
    last_fired_hash  TEXT,
    last_error       TEXT    NOT NULL DEFAULT '',
    checks           INTEGER NOT NULL DEFAULT 0,
    fired            INTEGER NOT NULL DEFAULT 0,
    deleted_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchers_next ON watchers(next_check_at);
