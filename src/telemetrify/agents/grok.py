"""Grok CLI transcript parsing.

WHY THIS EXISTS. Grok's Stop hook has been firing into telemetrify all along:
3,807 of the 3,924 lines in capture.log — 97% — are `no transcript for payload`
on a `/.grok/` path. Two independent bugs stacked, and only fixing both helps.

BUG 1: KEY CASING. capture._resolve_transcript reads `transcript_path` and
`session_id`. Claude Code sends snake_case; grok sends camelCase — the dropped
payloads carry `transcriptPath`, `sessionId`, `hookEventName`, `workspaceRoot`.
So the explicit path was never read, the fallback globbed ~/.claude/projects for
a grok session id, missed, and the payload was logged and discarded. The file it
named existed the whole time.

BUG 2: WRONG FILE. That `transcriptPath` points at `updates.jsonl`, which is not
a transcript — it is a JSON-RPC event stream, every line
`{"timestamp", "method", "params"}` with method `session/update` or
`_x.ai/session/update` (183 and 102 of 285 lines in the sampled session). The
conversation lives in the SIBLING `chat_history.jsonl`, discriminated by `type`.
So we resolve the directory, not the named file.

WHAT BECOMES A TURN. Ported from ClaudeOS engine/ingest/sources/grok.ts, which
measured this against 80 sessions of the real corpus. Per that survey, and
confirmed on a session here:

    tool_result        skipped — tool OUTPUT. Ingesting it is the defect that
                       left 78% of the claude_code corpus unclusterable.
    reasoning          skipped — encrypted_content, no text.
    assistant no-text  skipped — pure tool_calls.
    system             skipped — the system prompt, ~4,580 chars of boilerplate
                       repeated once per session.
    backend_tool_call  skipped.
    assistant + text   KEPT.
    user + text        KEPT — unless synthetic, see below.

SYNTHETIC USER TURNS ARE THE TRAP. Grok injects user-role messages the operator
never typed, marked `synthetic_reason`. In the ClaudeOS survey there were MORE
of them than real ones — 202 against 129. Filing them would make 61% of "his"
grok prompts words he never wrote. Same rule the Cursor adapter applies to
`isSimulatedMsg`.

TIME. chat_history.jsonl lines carry no timestamp — every field of every line
was checked across 25 sessions upstream, and the session sampled here agrees.
events.jsonl has timestamps but no prompt_index to join on, so mapping turn N to
the Nth `turn_started` would be an inference dressed as a fact. What IS a fact:
the session directory name is a UUIDv7 whose first 48 bits are creation-time
milliseconds. Every turn therefore carries its SESSION's start time — exact at
session granularity, invented at none. Order within a session is line order.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from ..transcript import Turn

SKIP_TYPES = frozenset({"tool_result", "reasoning", "system", "backend_tool_call"})


def is_grok_path(p: Path) -> bool:
    return ".grok" in p.parts and "sessions" in p.parts


def session_dir_for(p: Path) -> Path | None:
    """A grok payload names updates.jsonl; the conversation is its sibling."""
    d = p if p.is_dir() else p.parent
    return d if (d / "chat_history.jsonl").exists() else None


def _uuid7_ms(session_id: str) -> str | None:
    """UUIDv7's first 48 bits are creation ms. Returns ISO-8601 Z, or None."""
    hexpart = session_id.replace("-", "")
    if len(hexpart) < 12:
        return None
    try:
        ms = int(hexpart[:12], 16)
    except ValueError:
        return None
    # Sanity-bound it: a v7 id decodes to a plausible wall clock, a v4 does not.
    if not (1_500_000_000_000 < ms < 4_000_000_000_000):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text_of(rec: dict) -> str:
    c = rec.get("content")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") in (None, "text")]
        return "\n".join(p for p in parts if p).strip()
    t = rec.get("text")
    return t.strip() if isinstance(t, str) else ""


def _cwd_from_dir(session_dir: Path) -> str | None:
    """~/.grok/sessions/<url-encoded cwd>/<session uuid>/ — the parent is the cwd."""
    enc = session_dir.parent.name
    return unquote(enc) if enc else None


def iter_turns(session_dir: Path):
    """Yield every user→assistant turn in a grok session, oldest first."""
    hist = session_dir / "chat_history.jsonl"
    session_id = session_dir.name
    started = _uuid7_ms(session_id) or ""
    cwd = _cwd_from_dir(session_dir)
    model = None
    recs: list[dict] = []
    with hist.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    pending_user: str | None = None
    assistant_parts: list[str] = []
    tool_count = 0
    asst_msgs = 0

    def flush(idx: int) -> Turn | None:
        nonlocal pending_user, assistant_parts, tool_count, asst_msgs
        if pending_user is None:
            return None
        body = "\n".join(p for p in assistant_parts if p).strip()
        t = Turn(
            session_id=session_id,
            user_uuid=f"grok:{session_id}:{idx}",
            parent_uuid=None,
            prompt_id=None,
            user_text=pending_user,
            assistant_text=body,
            thinking_text="",
            model=model,
            started_at=started,
            finished_at=None,
            latency_ms=None,
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            cwd=cwd,
            git_branch=None,
            project_dir=str(session_dir),
            transcript_path=str(hist),
            cli_version=None,
            entrypoint="grok",
            user_type="external",
            attribution_skill=None,
            attribution_plugin=None,
            tool_call_count=tool_count,
            assistant_message_count=asst_msgs,
            tool_calls=[],
            raw_json="",
        )
        pending_user, assistant_parts, tool_count, asst_msgs = None, [], 0, 0
        return t

    for i, r in enumerate(recs):
        typ = r.get("type")
        if typ == "assistant" and not r.get("model") is None:
            model = model or r.get("model")
        if typ in SKIP_TYPES:
            if typ in ("tool_result", "backend_tool_call"):
                tool_count += 1
            continue
        if typ == "user":
            if r.get("synthetic_reason"):
                continue                      # injected by the client, never typed
            txt = _text_of(r)
            if not txt:
                continue
            done = flush(i)
            if done is not None:
                yield done
            pending_user = txt
        elif typ == "assistant":
            txt = _text_of(r)
            if txt:
                assistant_parts.append(txt)
                asst_msgs += 1
    last = flush(len(recs))
    if last is not None:
        yield last


def parse_latest_turn(session_dir: Path) -> Turn | None:
    last = None
    for t in iter_turns(session_dir):
        last = t
    return last
