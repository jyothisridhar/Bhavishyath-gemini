-- ============================================================
-- Migration 001: consent, structured intake, deletion requests
-- (tracker items B1-B6)
--
-- Run ONCE in Supabase: Dashboard -> SQL Editor -> paste -> Run.
-- Safe to run again: every statement checks before it changes anything.
-- Run it BEFORE deploying the bot version that needs it; the bot refuses
-- to start if these columns are missing.
-- ============================================================

-- 1. Real columns for what intake collects, plus consent state.
--    profile_json stays for softer, model-inferred items (interests, concerns, marks).
ALTER TABLE user_memory
    ADD COLUMN IF NOT EXISTS consent_at         TIMESTAMPTZ,   -- NULL = has not consented
    ADD COLUMN IF NOT EXISTS consent_version    TEXT,          -- which consent text they agreed to
    ADD COLUMN IF NOT EXISTS intake_step        TEXT,          -- question waiting for an answer; NULL = finished
    ADD COLUMN IF NOT EXISTS name               TEXT,
    ADD COLUMN IF NOT EXISTS class_year         TEXT,
    ADD COLUMN IF NOT EXISTS stream_group       TEXT,
    ADD COLUMN IF NOT EXISTS district           TEXT,
    ADD COLUMN IF NOT EXISTS mandal             TEXT,
    ADD COLUMN IF NOT EXISTS school             TEXT,
    ADD COLUMN IF NOT EXISTS language           TEXT,          -- telugu | english | mix
    ADD COLUMN IF NOT EXISTS first_generation   BOOLEAN,       -- asked later, not at intake
    ADD COLUMN IF NOT EXISTS shared_device      BOOLEAN;       -- asked later, not at intake


-- 2. Deletion requests: what /delete_my_data writes, for the team to action.
CREATE TABLE IF NOT EXISTS deletion_requests (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT NOT NULL,                 -- Telegram user ID
    requested_at  TIMESTAMPTZ DEFAULT NOW(),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    handled_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_deletion_requests_user ON deletion_requests(user_id);

ALTER TABLE deletion_requests ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "No public access" ON deletion_requests;
CREATE POLICY "No public access" ON deletion_requests FOR ALL USING (false);


-- 3. One call that removes a student's data and closes their request.
--    Usage:  SELECT process_deletion_request(123456789);
CREATE OR REPLACE FUNCTION process_deletion_request(p_user_id BIGINT) RETURNS void
LANGUAGE sql AS $$
    DELETE FROM session_history  WHERE user_id = p_user_id;
    DELETE FROM conversation_log WHERE user_id = p_user_id;
    DELETE FROM user_memory      WHERE user_id = p_user_id;
    UPDATE deletion_requests
       SET status = 'done', handled_at = NOW()
     WHERE user_id = p_user_id AND status = 'pending';
$$;


-- ============================================================
-- Handy queries for the team
-- ============================================================
-- Pending deletion requests:
--   SELECT id, user_id, requested_at FROM deletion_requests WHERE status = 'pending' ORDER BY requested_at;
-- Action one:
--   SELECT process_deletion_request(123456789);
-- Students who have not consented yet will have no row at all, because the bot stores nothing before consent.
