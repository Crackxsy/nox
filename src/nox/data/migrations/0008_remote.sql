-- 0008_remote: Mobile Companion (Spec v0.8 §6, EPIC-17). Reserved slot for the `remote` area.
-- Four things live here: pending pairings, the hashed per-device key, revocation, and the audit
-- trail of every remote command.
--
-- Nothing in this migration stores a secret or a reversible remote identity in the clear:
--   * a pairing code is only ever persisted as `code_hash` = SHA-256(salt + code),
--   * the per-device key is the verified transport identity (Telegram user id) bound to a random
--     per-device salt; it is persisted only as `public_key` = SHA-256(key_salt + channel + ':' +
--     sender_id). The column keeps its 0001_initial name; what it holds is a *hash*. Verification
--     is a constant-time compare over the (max 3) non-revoked rows - the id itself is never stored,
--     so a copy of the database does not reveal which Telegram account is paired.
--   * `remote_audit` stores the command verb, a decision and a non-reversible `sender_ref`
--     pseudonym - never arguments, chat text or notification bodies.
-- Timestamps are ISO-8601 UTC text, matching 0001_initial.

ALTER TABLE paired_devices ADD COLUMN revoked_at TEXT;
ALTER TABLE paired_devices ADD COLUMN revoked_reason TEXT NOT NULL DEFAULT '';
-- Which one-time code created this pairing (audit trail per Spec §6), never the code itself.
ALTER TABLE paired_devices ADD COLUMN pairing_code_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE paired_devices ADD COLUMN channel TEXT NOT NULL DEFAULT 'telegram';
-- Salt for `public_key` (see header): random per device, so two devices never share a hash.
ALTER TABLE paired_devices ADD COLUMN key_salt TEXT NOT NULL DEFAULT '';
-- Replay protection: the transport's own monotonic sequence number, highest one accepted so far.
ALTER TABLE paired_devices ADD COLUMN last_update_id INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_paired_devices_channel ON paired_devices(channel, revoked_at);

-- Pending one-time pairing codes. A code is single use (`redeemed_at`) and short-lived
-- (`expires_at`, default 5 min per Spec §5.2); the row survives redemption as the audit trail.
CREATE TABLE IF NOT EXISTS remote_pairings (
    id          TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    salt        TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'telegram',
    device_name TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    redeemed_at TEXT,
    device_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_remote_pairings_expires ON remote_pairings(expires_at);

-- Every remote command attempt, allowed or denied (Spec §5.3). `command` is the verb only; the
-- arguments, the chat text and any notification body are deliberately absent - there is no column
-- they could be written into later.
CREATE TABLE IF NOT EXISTS remote_audit (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    channel    TEXT NOT NULL DEFAULT 'telegram',
    sender_ref TEXT NOT NULL DEFAULT '',     -- SHA-256 prefix of channel:sender_id, not reversible
    device_id  TEXT NOT NULL DEFAULT '',
    command    TEXT NOT NULL,
    decision   TEXT NOT NULL,                -- allow | deny
    reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_remote_audit_ts ON remote_audit(ts);
