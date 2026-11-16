"""Shared helpers for the non-Claude agent adapters.

Every adapter here follows one rule the ClaudeOS lake learned expensively:
tool INPUT and tool OUTPUT never become message text. Storing them is what left
78% of that corpus unclusterable, and each of these formats separates tool
records into their own line/part/row — so keeping them would add hundreds of
thousands of rows that are nothing BUT a tool summary.

The second rule is narrower and does more damage when broken: several of these
clients INJECT user-role messages the operator never typed, and in three of the
four they OUTNUMBER the real ones (grok 202:129, kimi 2000:987, cursor's
isSimulatedMsg). A positive claim that the operator typed it is required — never
"assume user role means the user".
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..transcript import Turn


def iso(ts) -> str:
    """Best-effort ISO-8601 Z from epoch seconds/ms or an ISO string."""
    if isinstance(ts, str) and ts:
        return ts
    if isinstance(ts, (int, float)) and ts > 0:
        secs = ts / 1000 if ts > 1e11 else ts
        try:
            return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError):
            return ""
    return ""


def make_turn(*, agent: str, session_id: str, idx: int, user_text: str,
              assistant_text: str, started_at: str, cwd: str | None,
              model: str | None, project_dir: str, transcript_path: str,
              tool_call_count: int = 0, assistant_message_count: int = 0) -> Turn:
    """One Turn with this codebase's defaults for fields a given format lacks.

    Token counts are 0 and latency None rather than guessed: only Claude Code's
    transcript reports them, and a fabricated zero that reads as "measured" is
    worse than an honest absence in a corpus whose whole point is measurement.
    """
    return Turn(
        session_id=session_id,
        user_uuid=f"{agent}:{session_id}:{idx}",
        parent_uuid=None,
        prompt_id=None,
        user_text=user_text,
        assistant_text=assistant_text,
        thinking_text="",
        model=model,
        started_at=started_at,
        finished_at=None,
        latency_ms=None,
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cwd=cwd,
        git_branch=None,
        project_dir=project_dir,
        transcript_path=transcript_path,
        cli_version=None,
        entrypoint=agent,
        user_type="external",
        attribution_skill=None,
        attribution_plugin=None,
        tool_call_count=tool_call_count,
        assistant_message_count=assistant_message_count,
        tool_calls=[],
        raw_json="",
        agent=agent,
    )


class TurnBuilder:
    """Accumulates user→assistant pairs from a linear event stream."""

    def __init__(self, agent: str, **turn_kwargs):
        self.agent = agent
        self.kw = turn_kwargs
        self.user: str | None = None
        self.parts: list[str] = []
        self.tools = 0
        self.msgs = 0
        self._ts: str = ""

    def start_user(self, text: str, ts: str = ""):
        self.user = text
        self.parts, self.tools, self.msgs = [], 0, 0
        self._ts = ts

    def add_assistant(self, text: str):
        if text:
            self.parts.append(text)
            self.msgs += 1

    def flush(self, idx: int) -> Turn | None:
        if self.user is None:
            return None
        kw = dict(self.kw)
        if self._ts:
            kw["started_at"] = self._ts
        t = make_turn(agent=self.agent, idx=idx, user_text=self.user,
                      assistant_text="\n".join(p for p in self.parts if p).strip(),
                      tool_call_count=self.tools, assistant_message_count=self.msgs, **kw)
        self.user, self.parts, self.tools, self.msgs = None, [], 0, 0
        return t
