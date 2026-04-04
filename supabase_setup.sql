-- ============================================================
-- Bhavishyat Bot - Supabase Schema Setup
-- Run this once in your Supabase project:
--   Dashboard → SQL Editor → paste this → Run
-- ============================================================


-- 1. User memory: one row per student, holds profile + summary
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_memory (
    user_id     BIGINT PRIMARY KEY,          -- Telegram user ID
    username    TEXT,
    first_name  TEXT,
    profile_json JSONB DEFAULT '{}'::jsonb, -- structured profile facts
    summary     TEXT,                        -- free-text summary of past sessions
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fast lookups (already the PK, but explicit for clarity)
CREATE INDEX IF NOT EXISTS idx_user_memory_user_id ON user_memory(user_id);


-- 2. Conversation log: permanent record of every message
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversation_log (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    username    TEXT,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    message     TEXT NOT NULL,
    timestamp   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_convo_log_user_id  ON conversation_log(user_id);
CREATE INDEX IF NOT EXISTS idx_convo_log_timestamp ON conversation_log(timestamp DESC);


-- 3. Session history: rolling context window for Gemini
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_history (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'model')),
    content     TEXT NOT NULL,
    timestamp   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_history_user_id ON session_history(user_id);


-- ============================================================
-- Row Level Security (optional but recommended)
-- These tables are accessed by your backend using the
-- service_role key, which bypasses RLS.
-- Enable RLS so anon/public keys can't read student data.
-- ============================================================

ALTER TABLE user_memory      ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_history  ENABLE ROW LEVEL SECURITY;

-- No public access — service_role key bypasses these policies
-- so your bot still works fine.
CREATE POLICY "No public access" ON user_memory      FOR ALL USING (false);
CREATE POLICY "No public access" ON conversation_log FOR ALL USING (false);
CREATE POLICY "No public access" ON session_history  FOR ALL USING (false);


-- ============================================================
-- Handy admin queries (run in SQL Editor anytime)
-- ============================================================

-- See all users and their profiles:
-- SELECT user_id, username, first_name, profile_json, updated_at FROM user_memory ORDER BY updated_at DESC;

-- See recent conversations:
-- SELECT timestamp, username, role, LEFT(message, 200) AS preview FROM conversation_log ORDER BY timestamp DESC LIMIT 50;

-- See conversations for one user:
-- SELECT timestamp, role, message FROM conversation_log WHERE user_id = 123456789 ORDER BY timestamp;

-- Count messages per user:
-- SELECT username, COUNT(*) AS messages FROM conversation_log WHERE role = 'user' GROUP BY username ORDER BY messages DESC;

-- Find any flagged crisis events:
-- SELECT timestamp, user_id, username FROM conversation_log WHERE message LIKE '%CRISIS%';
