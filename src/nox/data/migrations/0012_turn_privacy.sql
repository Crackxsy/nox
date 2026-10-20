-- 0012_turn_privacy: whether a turn was said while cloud models were not allowed (privacy mode
-- offline). Such turns stay in the history for local models, but a cloud provider never sends
-- them - not even later, as conversation history, after the mode changed back. Turns in privacy
-- mode private are not recorded at all.
ALTER TABLE turns ADD COLUMN private INTEGER NOT NULL DEFAULT 0;
