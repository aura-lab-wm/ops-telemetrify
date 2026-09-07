"""The scanner's contract: incremental, idempotent, and honest about provider."""
import sqlite3
import pytest
from telemetrify.scan import _provider_for, SCANNED_AGENTS


class TestProviderDerivation:
    """Name the backend only when the model string names it — never from the agent."""

    def test_claude_models_are_anthropic(self):
        assert _provider_for("claude-opus-4-8") == "anthropic"
        assert _provider_for("claude-sonnet-5") == "anthropic"

    def test_vendor_slash_model_names_its_vendor(self):
        assert _provider_for("deepseek/deepseek-v4-pro-20260423") == "deepseek"
        assert _provider_for("anthropic/claude-4.7-opus") == "anthropic"

    def test_name_colon_tag_is_ollama(self):
        assert _provider_for("glm-5.2:cloud") == "ollama"

    def test_unnamed_backend_stays_none(self):
        # The whole point of migration 019: an unnamed backend is NULL, never a
        # guess. '<synthetic>' and a bare model name say nothing about who served it.
        assert _provider_for("<synthetic>") is None
        assert _provider_for("abliterated-model-large-v2") is None
        assert _provider_for(None) is None
        assert _provider_for("") is None


class TestScannedAgents:
    def test_hook_capable_agents_are_excluded(self):
        # claude_code and grok reach us through capture.py's Stop hook. Walking
        # 41 GB of their directories to re-find what the hook already filed is
        # pure cost, so the scanner must not own them.
        assert "claude_code" not in SCANNED_AGENTS
        assert "grok" not in SCANNED_AGENTS

    def test_every_hookless_agent_is_covered(self):
        # These five have no hook mechanism at all — checked on the real machine:
        # no `hook` key in codex/qwen/gemini config, no settings file for kimi,
        # and cursor is an IDE with a SQLite store. If one drops off this tuple
        # it silently stops being captured.
        assert set(SCANNED_AGENTS) == {"codex", "qwen", "kimi", "gemini", "cursor"}


class TestScanFilesCursor:
    def test_cursor_is_keyed_on_mtime_not_a_high_water_mark(self):
        # Sessions are APPENDED to. A "newest turn I have" cursor would never
        # re-read a file and would miss every continuation of a scanned session.
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE scan_files (
            agent TEXT NOT NULL, path TEXT NOT NULL PRIMARY KEY,
            mtime_ms INTEGER NOT NULL, last_seen_at TEXT NOT NULL,
            turns INTEGER NOT NULL DEFAULT 0)""")
        up = """INSERT INTO scan_files(agent,path,mtime_ms,last_seen_at,turns)
                VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
                mtime_ms=excluded.mtime_ms, turns=excluded.turns"""
        conn.execute(up, ("codex", "/a.jsonl", 1000, "t0", 2))
        conn.execute(up, ("codex", "/a.jsonl", 2000, "t1", 5))   # file grew
        row = conn.execute("SELECT mtime_ms, turns FROM scan_files").fetchone()
        assert row == (2000, 5)
        assert conn.execute("SELECT COUNT(*) FROM scan_files").fetchone()[0] == 1
