"""Gemini CLI parsing. No ClaudeOS parser existed for this one.

~/.gemini/tmp/<project>/chats/session-<ISO>-<hash>.jsonl. The format is a
HYBRID, and that is the whole difficulty:

    line 0   {sessionId, projectHash, startTime, lastUpdated, kind}
    then     a mix of
             (a) bare append records   {id, timestamp, type, content, …}
             (b) {"$set": {"messages": [ …the same records… ]}}

(b) is a state SNAPSHOT, not an event. Measured over the real ~/.gemini,
31 chat files: 172 bare records against 160 `$set` lines whose payloads carry
only 30 messages in total. So the bare stream is the superset and the snapshots
restate a slice of it — ingesting both would enter turns twice, the same defect
codex's `event_msg` lines create. Only bare records are read.

CONTENT IS TWO SHAPES, keyed on `type`:
    type "user"    -> content is a LIST of {text} blocks
    type "gemini"  -> content is a plain STRING
`gemini` is this client's word for the assistant; mapped on the way in so it
does not leak into a corpus that spells it 'assistant' everywhere else.

THE FIRST USER MESSAGE IS ALWAYS SYNTHETIC — a `<session_context>` preamble the
CLI writes ("This is the Gemini CLI. We are setting up the context for our
chat. Today's date is…" plus the workspace directory listing). Dropped, on the
same rule as grok's synthetic_reason and kimi's origin.kind: a user role is not
a claim the operator typed it.

CWD is not in the file. Sessions key on `projectHash`, a sha256, so the working
directory is resolved through ~/.gemini/projects.json when that mapping has an
entry and left NULL when it does not — an unresolvable hash is not a directory
name worth inventing.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._common import TurnBuilder

SYNTHETIC_PREFIXES = ("<session_context>", "<environment_context>")


def _project_map(root: Path) -> dict:
    f = root / "projects.json"
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        # {hash: {path: …}} or {hash: "path"} — accept either.
        out = {}
        for k, v in data.items():
            if isinstance(v, str):
                out[k] = v
            elif isinstance(v, dict) and isinstance(v.get("path"), str):
                out[k] = v["path"]
        return out
    return {}


def _content_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        ).strip()
    return ""


def iter_turns(path: Path, project_map: dict | None = None):
    project_map = project_map if project_map is not None else _project_map(Path.home() / ".gemini")
    b = None
    idx = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if lineno == 0 and "sessionId" in rec:
                b = TurnBuilder("gemini", session_id=rec.get("sessionId") or path.stem,
                                started_at=rec.get("startTime") or "",
                                cwd=project_map.get(rec.get("projectHash") or ""),
                                model=None, project_dir=str(path.parent),
                                transcript_path=str(path))
                continue
            if "$set" in rec:
                continue                     # snapshot; the bare stream is the superset
            if b is None:
                b = TurnBuilder("gemini", session_id=path.stem, started_at="", cwd=None,
                                model=None, project_dir=str(path.parent),
                                transcript_path=str(path))
            typ = rec.get("type")
            text = _content_text(rec.get("content"))
            if not text:
                continue
            if typ == "user":
                if text.lstrip().startswith(SYNTHETIC_PREFIXES):
                    continue
                done = b.flush(idx)
                if done is not None:
                    yield done
                idx += 1
                b.start_user(text, rec.get("timestamp") or "")
            elif typ in ("gemini", "model", "assistant"):
                if isinstance(rec.get("model"), str):
                    b.kw["model"] = rec["model"]
                b.add_assistant(text)
    if b is not None:
        last = b.flush(idx)
        if last is not None:
            yield last


def sessions(root: Path | None = None):
    root = root or (Path.home() / ".gemini" / "tmp")
    return sorted(root.glob("*/chats/*.jsonl")) if root.exists() else []
