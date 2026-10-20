-- 0015_consent_recorded_by: how a consent was recorded - typed in the dashboard, or a sentence the
-- model proposed and the user confirmed. Rows from before this column read as an empty string.

ALTER TABLE consents ADD COLUMN recorded_by TEXT NOT NULL DEFAULT '';
