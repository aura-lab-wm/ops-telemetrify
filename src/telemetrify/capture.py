import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import LOG_PATH, DATA_DIR
from .db import connect
from .store import upsert_session, insert_turn, record_ingest_run
from .transcript import parse_latest_turn
from .agents.grok import (
    is_grok_path,
    session_dir_for as grok_session_dir,
    parse_latest_turn as parse_latest_grok_turn,
)


def _log(msg: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def _payload_get(payload: dict, *names: str):
    """Hook payload keys are not spelled the same by every CLI.

    Claude Code sends snake_case (transcript_path, session_id); grok sends
    camelCase (transcriptPath, sessionId, hookEventName, workspaceRoot). This
    function read only the snake_case spellings, so every grok payload fell
    through to a ~/.claude/projects glob that could never match a grok session
    id — 3,807 dropped payloads, 97% of capture.log, each one naming a
    transcript that existed on disk the whole time.
    """
    for n in names:
        v = payload.get(n)
        if v:
            return v
    return None


def _resolve_transcript(payload: dict) -> tuple[Path, str] | None:
    """Return (path_or_session_dir, agent), or None.

    For grok the returned path is the session DIRECTORY, not the file the
    payload names: `transcriptPath` points at updates.jsonl, which is a
    JSON-RPC event stream ({timestamp, method, params}), not a transcript. The
    conversation is the sibling chat_history.jsonl. See agents/grok.py.
    """
    explicit = _payload_get(payload, "transcript_path", "transcriptPath")
    if explicit:
        p = Path(explicit)
        if is_grok_path(p):
            d = grok_session_dir(p)
            if d is not None:
                return d, "grok"
        if p.exists():
            return p, "claude_code"
    session_id = _payload_get(payload, "session_id", "sessionId", "conversation_id", "conversationId")
    if not session_id:
        return None
    root = Path.home() / ".claude" / "projects"
    matches = list(root.glob(f"*/{session_id}.jsonl"))
    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0], "claude_code"


def _detect_origin(payload: dict) -> str:
    """Tag turns originating from a rerun workspace differently so analysis can split them."""
    cwd = payload.get("cwd") or ""
    if "/data/reruns/" in cwd:
        return "rerun"
    return "organic"


def main() -> int:
    # Re-entrancy guard: telemetrify itself can spawn `claude -p` (the claude_cli
    # LLM tier). That child session's Stop hook would otherwise capture our own
    # internal calls into the corpus and trigger a nested grade. The backend
    # sets this flag in the child env so we bail before touching the DB.
    if os.environ.get("TELEMETRIFY_NO_CAPTURE"):
        return 0
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = skipped = errors = 0
    note = ""
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        _log(f"bad stdin: {exc}")
        return 0

    if payload.get("agent_id"):
        return 0

    try:
        resolved = _resolve_transcript(payload)
        if resolved is None:
            _log(f"no transcript for payload: {payload}")
            return 0
        transcript_path, agent = resolved

        if agent == "grok":
            turn = parse_latest_grok_turn(transcript_path)
        else:
            turn = parse_latest_turn(transcript_path)
        if turn is None:
            return 0
        turn.agent = agent

        conn = connect()
        turn_id = None
        with conn:
            upsert_session(conn, turn)
            from .embed import embed_turn, embed_prompt
            full_vec = embed_turn(turn.user_text, turn.assistant_text)
            prompt_vec = embed_prompt(turn.user_text)
            origin = _detect_origin(payload)
            turn_id = insert_turn(conn, turn, full_vec,
                                   origin=origin, prompt_embedding=prompt_vec)
            if turn_id is None:
                skipped = 1
                note = "already recorded"
            else:
                inserted = 1
                note = f"turn_id={turn_id}"
                # Best-effort post-processing: follow-up detection + nearest-cluster assignment.
                try:
                    from .followups import detect_for_turn
                    detect_for_turn(conn, turn_id)
                except Exception:
                    _log(f"followup detect failed: {traceback.format_exc()}")
                try:
                    from .cluster import assign_nearest_cluster
                    assign_nearest_cluster(conn, turn_id, prompt_vec)
                except Exception:
                    _log(f"cluster assign failed: {traceback.format_exc()}")
            finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            record_ingest_run(conn, "hook", started_at, finished_at,
                              inserted, skipped, errors, note)

        # Round A1: inline LLM-as-Judge grade — AFTER the capture transaction
        # commits. The grader's router issues its OWN commits; running it inside
        # `with conn:` flushes a partial turn and defeats the rollback the outer
        # except-block relies on. Fail-silent + a real 10s timeout (the router
        # now forwards timeout to the backend) so capture never blocks the user.
        if turn_id is not None:
            try:
                from .ai.grader import grade_turn
                grade_turn(conn, turn_id, timeout=10.0)
            except Exception:
                _log(f"auto-grade failed: {traceback.format_exc()}")
        return 0
    except Exception:
        _log(f"capture failed:\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
