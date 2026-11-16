-- Populate turns.provider for rows that predate it.
--
-- Migration 019 added the column and left it NULL on purpose: 'anthropic'
-- across the board would have been invented, and wrong for the ~130 glm /
-- deepseek / abliterated turns captured through Claude Code's hook. But NULL on
-- 100% of 58,134 rows is a column that says nothing, so here is the part that
-- can be derived without inventing anything.
--
-- THE RULE, and its one principle: name the backend only when the MODEL STRING
-- names it. Never from the agent. Defaulting codex to 'openai' because codex
-- usually talks to OpenAI is precisely the agent/provider conflation 019 exists
-- to prevent, and this corpus already disproves it — glm-5.2:cloud and
-- deepseek-v4-pro were both served through Claude Code.
--
--   claude-%     -> anthropic     53,749 rows
--   vendor/model -> vendor        openrouter-style routing states its vendor
--   name:tag     -> ollama        ollama's own naming convention
--   anything else-> stays NULL    including 7,388 rows with no model at all
--                                 and 311 '<synthetic>' rows
--
-- NULL after this still means "not recorded", which stays true and queryable.
-- It does not mean 'anthropic'.
--
-- New rows get the same rule applied in Python at capture/scan time; see
-- scan._provider_for, which this must be kept in step with.
UPDATE turns SET provider =
  CASE
    WHEN model LIKE 'claude-%'                       THEN 'anthropic'
    WHEN model LIKE '%/%'                            THEN lower(substr(model, 1, instr(model, '/') - 1))
    WHEN model LIKE '%:%'                            THEN 'ollama'
    ELSE NULL
  END
WHERE provider IS NULL AND model IS NOT NULL;
