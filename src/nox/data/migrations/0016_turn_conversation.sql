-- 0016_turn_conversation: the conversation a turn belongs to. Voice, each dashboard client and each
-- paired phone have conversations of their own, and only a conversation's own turns are replayed
-- into the next model call. Rows from before this column read as an empty string.

ALTER TABLE turns ADD COLUMN conversation TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation, id);
