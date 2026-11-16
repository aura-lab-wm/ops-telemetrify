"""Which agent wrote a transcript, and who can parse it.

Detection is by PATH, not by content sniffing: every one of these clients owns a
directory under $HOME and writes only its own format there, so the path is a
fact while a content guess would be an inference.

Cursor is deliberately absent from HOOK_ROUTES. It is an IDE, not a CLI — it
fires no Stop hook and keeps its chat in a SQLite key/value store rather than
per-session files, so it is reachable by backfill only.

Antigravity is absent because there is nothing to read. Its globalStorage
state.vscdb holds 88 rows of VS Code workbench UI state and its two
workspaceStorage stores 46 and 55; ~/.gemini/antigravity*/conversations/ is
empty and the rest of its 59 MB is Electron cache (blob_storage, GPUCache,
Session Storage). Either it has not been used or its conversations live
server-side. A parser written against that would be a guess, not a port.
"""
from __future__ import annotations

from pathlib import Path

# (path marker, agent id). First match wins; order is longest-marker-first so a
# nested directory cannot be claimed by a shorter prefix.
_MARKERS: list[tuple[tuple[str, ...], str]] = [
    ((".kimi-code", "sessions"), "kimi"),
    ((".grok", "sessions"), "grok"),
    ((".codex", "sessions"), "codex"),
    ((".qwen", "projects"), "qwen"),
    ((".gemini", "tmp"), "gemini"),
    ((".claude", "projects"), "claude_code"),
]


def detect_agent(path: Path) -> str:
    parts = path.parts
    for markers, agent in _MARKERS:
        if all(m in parts for m in markers):
            return agent
    return "claude_code"


def parser_for(agent: str):
    """Return a callable(path)->Turn|None for the newest turn, or None."""
    if agent == "grok":
        from .grok import parse_latest_turn
        return parse_latest_turn
    if agent == "codex":
        from .codex import iter_turns
    elif agent == "qwen":
        from .qwen import iter_turns
    elif agent == "kimi":
        from .kimi import iter_turns
    elif agent == "gemini":
        from .gemini import iter_turns
    else:
        return None

    def latest(p: Path):
        last = None
        for t in iter_turns(p):
            last = t
        return last

    return latest
