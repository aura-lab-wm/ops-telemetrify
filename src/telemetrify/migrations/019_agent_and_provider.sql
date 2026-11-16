-- Two orthogonal facts that the schema conflated into "it's Claude Code".
--
-- Until now every column here was Claude-Code-shaped (entrypoint, user_type,
-- cli_version) and the only hint of who served a turn was turns.model, a free
-- string. That was survivable while 99.7% of the corpus was Claude. It is not
-- survivable now: the Stop hook already fires from other CLIs, and 3,807 grok
-- payloads have been dropped on the floor — 97% of everything in capture.log.
--
-- WHY TWO COLUMNS AND NOT ONE. Which CLI produced a session and which backend
-- served its tokens are independent, and this corpus already proves it: 83
-- turns ran on glm-5.2:cloud, 36 on deepseek/deepseek-v4-pro, 12 on
-- abliterated-model-large-v2 — all captured through Claude Code's hook. Fold
-- them into a single "provider" and you can never again ask "how does grok
-- behave against rocco versus against its own backend", which is the question
-- the whole corpus exists to answer.
--
--   sessions.agent  — which CLI wrote the transcript. A property of the
--                     session: one transcript file, one program that wrote it.
--   turns.provider  — which backend served THIS turn's tokens. Deliberately on
--                     turns, not sessions, because BackendRouter falls back
--                     mid-session (rocco -> ollama -> anthropic). Put it on the
--                     session and the fallback event — the interesting one —
--                     becomes unrepresentable.
--
-- Mirrors ai_runs.backend (017), which already made exactly this distinction
-- for telemetrify's OWN calls and then never applied it to captured ones.
--
-- BACKFILL. Every existing row came through the Claude Code hook, so 'claude_code'
-- is a fact about them, not a guess. provider is left NULL rather than assumed:
-- turns.model says glm/deepseek/abliterated for ~144 of them, and writing
-- 'anthropic' across the board would be inventing data. A NULL provider means
-- "captured before we recorded this", which is true and queryable; a wrong
-- 'anthropic' would be neither.
ALTER TABLE sessions ADD COLUMN agent TEXT NOT NULL DEFAULT 'claude_code';
ALTER TABLE turns    ADD COLUMN provider TEXT;

CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent);
CREATE INDEX IF NOT EXISTS idx_turns_provider ON turns(provider);
