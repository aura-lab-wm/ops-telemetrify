"""Cursor parsing. Ported from ClaudeOS engine/ingest/sources/cursor.ts.

Cursor keeps its chat in a key/value SQLite store, not in files:
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb, table
`cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)`. Measured on
the real machine 2026-09-04, 2,050 rows:

    bubbleId:<composerId>:<bubbleId>   1,160   the conversation itself
    agentKv:<store>:<id>                 619   the agent runtime's transport
    composer.content.<sha256>            219   content-addressed file bodies
    composerData:<composerId>             30   per-conversation metadata
    checkpointId / ofsContent / …         22   editor mechanism

Only `bubbleId:` rows become messages. Their value is TEXT JSON — 1,160 of
1,160 parsed — carrying `{_v, type, bubbleId, text, createdAt, …}`. `type` is
the role, and the conventional 1=user / 2=assistant reading is VERIFIED here,
not assumed: 16 rows are type 1 and 1,144 are type 2, and the type-1 rows are
the only ones also carrying `richText` (the editor's record of what the
operator typed).

Of the 1,160 bubbles only 286 carry non-empty `text`, and the split is clean:

    286  text, no toolFormerData        INGESTED
    556  toolFormerData, no text        a tool call; skipped
    308  thinking.text, no text         the model's monologue; skipped
     10  neither                        a turn not written yet, or abandoned
      0  BOTH text and toolFormerData

That last zero is the important one: unlike claude_code, a Cursor tool call
never rides along inside a real message, so skipping tool rows costs no prose.

`isSimulatedMsg` marks turns the client injected rather than the operator
typing them — the same defect as grok's synthetic_reason and kimi's
origin.kind, and skipped for the same reason.

The DB is copied to a temp file before reading: Cursor holds it open, and
reading a live SQLite file out from under a writer is how you get
`database is locked` on someone else's editor session.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from ._common import make_turn, iso

DEFAULT_DB = (Path.home() / "Library" / "Application Support" / "Cursor" /
              "User" / "globalStorage" / "state.vscdb")


def _rows(db: Path):
    tmpdir = tempfile.mkdtemp(prefix="telemetrify-cursor-")
    tmp = Path(tmpdir) / "state.vscdb"
    try:
        shutil.copyfile(db, tmp)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            yield from conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%' ORDER BY key")
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def iter_turns(db: Path | None = None):
    db = db or DEFAULT_DB
    if not db.exists():
        return
    convos: dict[str, list[dict]] = {}
    for key, value in _rows(db):
        parts = key.split(":")
        if len(parts) < 3:
            continue
        composer = parts[1]
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        try:
            v = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(v, dict):
            continue
        convos.setdefault(composer, []).append(v)

    for composer, bubbles in convos.items():
        bubbles.sort(key=lambda b: (b.get("createdAt") or 0, b.get("bubbleId") or ""))
        pending_user, parts_out, tools, msgs = None, [], 0, 0
        idx = 0
        for b in bubbles:
            if b.get("toolFormerData") is not None:
                tools += 1
                continue
            text = b.get("text")
            if not isinstance(text, str) or not text.strip():
                continue                          # thinking-only, or not written yet
            if b.get("isSimulatedMsg"):
                continue                          # injected, not typed
            text = text.strip()
            if b.get("type") == 1:
                if pending_user is not None:
                    yield make_turn(agent="cursor", session_id=composer, idx=idx,
                                    user_text=pending_user,
                                    assistant_text="\n".join(parts_out).strip(),
                                    started_at=iso(b.get("createdAt")), cwd=None, model=None,
                                    project_dir=str(db.parent), transcript_path=str(db),
                                    tool_call_count=tools, assistant_message_count=msgs)
                    idx += 1
                pending_user, parts_out, tools, msgs = text, [], 0, 0
            elif b.get("type") == 2:
                parts_out.append(text)
                msgs += 1
        if pending_user is not None:
            yield make_turn(agent="cursor", session_id=composer, idx=idx,
                            user_text=pending_user,
                            assistant_text="\n".join(parts_out).strip(),
                            started_at="", cwd=None, model=None,
                            project_dir=str(db.parent), transcript_path=str(db),
                            tool_call_count=tools, assistant_message_count=msgs)
