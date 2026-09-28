-- 0011_security_state: the security state that survives a restart.
-- Privacy mode, panic and the kill switch used to live in memory only, so a crash or a watchdog
-- restart rebuilt them from the configuration: an OFFLINE set from the tray came back as the
-- configured mode, and an engaged kill switch came back released. One row, rewritten on every
-- change (`nox.data.security_repos.SecurityStateRepository`); restored at boot before anything can
-- act (`nox.security.persisted_state.restore_security_state`). Timestamps are ISO-8601 UTC text.

CREATE TABLE IF NOT EXISTS security_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    privacy_mode        TEXT    NOT NULL,
    panic               INTEGER NOT NULL DEFAULT 0,
    kill_engaged        INTEGER NOT NULL DEFAULT 0,
    kill_security_path  INTEGER NOT NULL DEFAULT 0,
    kill_origin         TEXT    NOT NULL DEFAULT '',
    kill_reason         TEXT    NOT NULL DEFAULT '',
    updated_at          TEXT    NOT NULL
);
