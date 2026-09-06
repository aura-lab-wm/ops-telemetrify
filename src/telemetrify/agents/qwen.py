"""Qwen Code parsing. Ported from ClaudeOS engine/ingest/sources/qwen.ts.

Qwen Code is a fork of the Gemini CLI, so its files are Claude Code's envelope
wrapped around Gemini's message body — ~/.qwen/projects/<slugged cwd>/**/*.jsonl,
one line per turn:

    { type, timestamp, uuid, sessionId, cwd, gitBranch, isSidechain,
      message: { role: 'user' | 'model', parts: [...] } }

WHAT BECOMES A MESSAGE. `parts` is where this diverges from every other adapter
here, because Gemini puts three different things in one array and distinguishes
two of them by a FLAG rather than a type. Measured over the real ~/.qwen,
161 files, 8,278 user-or-assistant lines:

    { text }            2,125  INGESTED — the actual prose
    { text, thought }   4,314  SKIPPED — `thought: true` marks internal
                               reasoning. It has a `text` field exactly like
                               real output, so anything reading parts[].text
                               without checking the flag files TWICE as much
                               thinking as speech.
    { functionCall }    5,680  SKIPPED — a tool call in its own part.

2,107 messages survive, 92.6% over the clustering floor.

`role` is Gemini's vocabulary: 'model', not 'assistant'. Mapped on the way in so
one client's word for the same thing does not leak into a corpus that spells it
'assistant' everywhere else.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._common import TurnBuilder, iso


def _prose(parts) -> tuple[str, int]:
    """Returns (text, tool_part_count). `thought` parts never contribute."""
    if not isinstance(parts, list):
        return "", 0
    out, tools = [], 0
    for p in parts:
        if not isinstance(p, dict):
            continue
        if p.get("functionCall") is not None or p.get("functionResponse") is not None:
            tools += 1
            continue
        if p.get("thought"):
            continue
        t = p.get("text")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return "\n".join(out).strip(), tools


def iter_turns(path: Path):
    b = None
    idx = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            if b is None:
                b = TurnBuilder("qwen", session_id=rec.get("sessionId") or path.stem,
                                started_at="", cwd=rec.get("cwd"), model=None,
                                project_dir=str(path.parent), transcript_path=str(path))
            text, tools = _prose(msg.get("parts"))
            b.tools += tools
            if not text:
                continue
            role = msg.get("role")
            if role == "user":
                done = b.flush(idx)
                if done is not None:
                    yield done
                idx += 1
                b.start_user(text, iso(rec.get("timestamp")))
            elif role in ("model", "assistant"):
                b.add_assistant(text)
    if b is not None:
        last = b.flush(idx)
        if last is not None:
            yield last


def sessions(root: Path | None = None):
    root = root or (Path.home() / ".qwen" / "projects")
    return sorted(root.rglob("*.jsonl")) if root.exists() else []
