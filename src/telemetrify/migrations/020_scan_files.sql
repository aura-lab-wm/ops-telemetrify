-- Per-file cursor for the periodic scanner.
--
-- WHY A SCANNER EXISTS AT ALL. telemetrify captures through a Stop hook, and
-- exactly one of the six agents it now parses can call one. Checked on this
-- machine: ~/.claude/settings.json wires bin/capture-hook; ~/.codex/config.toml,
-- ~/.qwen/settings.json and ~/.gemini/config/config.json contain no `hook` key
-- of any kind, and ~/.grok and ~/.kimi-code have no settings file at all. So
-- codex, qwen, kimi, gemini and cursor can never push to us; they have to be
-- pulled. Without this table their turns are a snapshot frozen at the day they
-- were backfilled.
--
-- WHY MTIME AND NOT A HIGH-WATER TIMESTAMP. Sessions are APPENDED to. A cursor
-- of "newest turn I have" would re-read nothing and miss every continuation of
-- a session that was already scanned. Keying on the file's own mtime re-reads a
-- file exactly when it has grown, and insert_turn is INSERT OR IGNORE on
-- user_uuid, so re-reading is free of duplicates. The cursor is an optimisation
-- over 1,281 codex files, not a correctness mechanism — deleting every row here
-- costs a slow scan, never a wrong one.
--
-- turns records what the file yielded last time, so `scan --status` can show
-- which sources are actually producing without re-parsing them.
CREATE TABLE IF NOT EXISTS scan_files (
    agent        TEXT NOT NULL,
    path         TEXT NOT NULL PRIMARY KEY,
    mtime_ms     INTEGER NOT NULL,
    last_seen_at TEXT NOT NULL,
    turns        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_scan_files_agent ON scan_files(agent);
