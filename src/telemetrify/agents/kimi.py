"""Kimi CLI parsing. Ported from ClaudeOS engine/ingest/sources/kimi.ts.

The conversation is NOT a chat file — it is a per-agent EVENT LOG at
~/.kimi-code/sessions/<workspace slug>/session_<uuid>/agents/<agentId>/wire.jsonl
(one `main` log per session plus up to 30 `agent-N` subagent logs). Measured:
592 MB, 860 wire logs across 126 sessions, 258,358 lines, every one parsed.

96% of the file is machinery. Full census by `type`: 169,155
`context.append_loop_event`, 29,693 `llm.request` (model/token caps/prompt
HASHES — no prose), 29,108 `usage.record`, 10,380 `token_counting.measured`,
4,613 `context.append_message`, 4,259 `staleGuard.recorded`, 1,613
`turn.prompt`, 729 `turn.steer`, and ~35 more mechanism types.

THE USER SIDE, and the injected turns that outnumber the real ones. All 4,613
`context.append_message` lines carry role "user", but `message.origin.kind`
says who actually typed them:

    injection        2,000   client-injected context
    user               987   INGESTED — the operator typed it
    system_trigger     818   subagent spawn prompts
    task               371   a finished task reporting back
    background_task    353   ditto, backgrounded
    cron_job            36   a scheduled job firing
    skill_activation    33   a skill's own preamble
    shell_command        8   a `!cmd` transcript
    (no origin)          7   skipped — see below

79% of what looks like the operator talking is the client talking. Only
`origin.kind == "user"` is ingested. The seven with NO origin are skipped too:
one per file across seven protocol-1.5 logs, mid-session, never the opening
prompt, and nothing in them claims the operator typed them. With injected turns
running 2:1 against real ones, "assume the operator wrote it" is the wrong
default; a positive claim is required.

`turn.prompt` / `turn.steer` are the prompt as SUBMITTED and are skipped as
DUPLICATES: of 2,342 such lines, 2,340 have a byte-identical
`context.append_message` twin a millisecond later. `context.append_message`
wins because it is the superset — it also carries prompts submitted through
paths that emit no turn.prompt.

THE ASSISTANT SIDE lives one level down, inside `context.append_loop_event`:

    content.part  38,770   part.type "text"   10,013  INGESTED
                           part.type "think"  28,757  SKIPPED
    tool.call     35,873   SKIPPED — tool INPUT
    tool.result   35,871   SKIPPED — tool OUTPUT, the 8 MB-per-session mistake
    step.begin    29,415 / step.end 29,226  SKIPPED — latency/usage telemetry

A `content.part` is a WHOLE part, not a stream delta: across 80 sampled logs
every (turnId, step, stepUuid) group holds exactly one text part and every uuid
is unique, so concatenating them is neither needed nor safe.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._common import TurnBuilder, iso


def iter_turns(path: Path):
    """path is a wire.jsonl. Session id comes from the session_<uuid> dir."""
    session_dir = path.parent.parent.parent          # agents/<id>/wire.jsonl -> session_<uuid>
    session_id = session_dir.name
    agent_id = path.parent.name
    b = TurnBuilder("kimi", session_id=f"{session_id}:{agent_id}", started_at="",
                    cwd=session_dir.parent.name.replace("-", "/"), model=None,
                    project_dir=str(session_dir), transcript_path=str(path))
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
            typ = rec.get("type")
            if typ == "context.append_message":
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                origin = msg.get("origin")
                if not isinstance(origin, dict) or origin.get("kind") != "user":
                    continue                       # injection, task, cron_job, or no claim at all
                content = msg.get("content")
                text = content if isinstance(content, str) else ""
                if isinstance(content, list):
                    text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = text.strip()
                if not text:
                    continue
                done = b.flush(idx)
                if done is not None:
                    yield done
                idx += 1
                b.start_user(text, iso(rec.get("timestamp") or rec.get("ts")))
            elif typ == "context.append_loop_event":
                ev = rec.get("event")
                if not isinstance(ev, dict):
                    continue
                et = ev.get("type")
                if et in ("tool.call", "tool.result"):
                    b.tools += 1
                    continue
                if et != "content.part":
                    continue
                part = ev.get("part")
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue                        # "think" parts are not speech
                t = part.get("text")
                if isinstance(t, str) and t.strip():
                    b.add_assistant(t.strip())
    last = b.flush(idx)
    if last is not None:
        yield last


def sessions(root: Path | None = None):
    root = root or (Path.home() / ".kimi-code" / "sessions")
    return sorted(root.rglob("wire.jsonl")) if root.exists() else []
