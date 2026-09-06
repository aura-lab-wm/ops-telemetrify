"""Codex rollout parsing. Ported from ClaudeOS engine/ingest/sources/codex.ts.

Codex writes ROLLOUT files, not chat lines: ~/.codex/sessions/**/*.jsonl, every
line `{timestamp, type, ordinal?, payload}`. (`ordinal` exists only in recent
files, so nothing here may depend on it.) An earlier ClaudeOS version assumed
`{role, content}` with content a string; that was false for all 438,767 lines
and quarantined 100% of a 6.4 GB corpus.

WHAT BECOMES A MESSAGE, and why so little does. A rollout interleaves the
conversation with the machinery that produced it. Of 15,836 lines sampled over
60 real sessions: 2,127 `reasoning`, 1,645 `function_call` + 1,645 outputs,
1,198 `custom_tool_call` + 1,198 outputs, 2,651 `item_completed`, 2,456
`token_count` — and 905 actual messages. Only the messages are kept.

Codex gives each tool call its OWN response_item, so ingesting them would not
enrich a real message the way claude_code's renderBlock does — it would add
~400k rows that are nothing but a tool summary.

`reasoning` is skipped because there is no text to keep: `summary` is an empty
array in all 2,127 sampled and the content sits under `encrypted_content`.

`event_msg` lines are skipped because they RESTATE the response items
(`user_message`, `agent_message`) alongside telemetry — ingesting both would
enter every turn twice.

What survives is prose: 905 of 905 sampled messages carry text, median 266
chars, 96% over the 40-char clustering floor.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._common import TurnBuilder, iso

TOOLISH = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")

# Client-injected user-role messages. This is a DELIBERATE DIVERGENCE from
# ClaudeOS's codex.ts, which does not filter them — and that omission is
# visible in its lake: auditing that corpus turned up 128 surviving
# `<environment_context>` openers and a family of `# AGENTS.md instructions`
# blocks sitting in what was supposed to be the operator's own words.
#
# Census over 40 real rollouts, user-role messages by opening text:
#   <environment_context>            27
#   # AGENTS.md instructions for     19
#   <subagent_notification>           7
#   <turn_aborted>                    5
#   # In app browser:                 5
#   # Files mentioned by the user:    4
#
# Same rule as grok's synthetic_reason, kimi's origin.kind and cursor's
# isSimulatedMsg: a user ROLE is not a claim that the operator typed it.
INJECTED_PREFIXES = (
    "<environment_context>", "<subagent_notification>", "<turn_aborted>",
    "<user_instructions>", "<system-reminder>",
    "# AGENTS.md instructions", "# In app browser:", "# Files mentioned by the user:",
)


def _is_injected(text: str) -> bool:
    return text.lstrip().startswith(INJECTED_PREFIXES)


def _render_block(b) -> str:
    if isinstance(b, str):
        return b
    if not isinstance(b, dict):
        return ""
    if isinstance(b.get("text"), str):
        return b["text"]
    # input_image and friends carry a data: URL, never prose. Tagged rather than
    # dropped so an image-only message is still visibly a turn — and tagged
    # SHORT, so it cannot reach the clustering floor on its own.
    t = b.get("type")
    return f"[{t}]" if isinstance(t, str) else ""


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(s for s in (_render_block(b) for b in content) if s).strip()


def iter_turns(path: Path):
    session_id = path.stem
    cwd = None
    b = TurnBuilder("codex", session_id=session_id, started_at="", cwd=None, model=None,
                    project_dir=str(path.parent), transcript_path=str(path))
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
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                continue
            ptype = payload.get("type")
            if cwd is None and isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
                b.kw["cwd"] = cwd
            if ptype in TOOLISH:
                b.tools += 1
                continue
            if ptype != "message":
                continue                      # reasoning, event_msg, token_count, …
            role = payload.get("role")
            text = _flatten(payload.get("content"))
            if not text:
                continue
            if role == "user":
                if _is_injected(text):
                    continue
                done = b.flush(idx)
                if done is not None:
                    yield done
                idx += 1
                b.start_user(text, iso(rec.get("timestamp")))
            elif role == "assistant":
                b.add_assistant(text)
    last = b.flush(idx)
    if last is not None:
        yield last


def sessions(root: Path | None = None):
    root = root or (Path.home() / ".codex" / "sessions")
    return sorted(root.rglob("*.jsonl")) if root.exists() else []
