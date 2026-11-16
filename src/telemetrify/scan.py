"""Pull turns from agents that cannot push them.

telemetrify's capture path is a Stop hook, and exactly ONE of the six agents it
parses can call one. Measured on this machine: ~/.claude/settings.json wires
bin/capture-hook; ~/.codex/config.toml, ~/.qwen/settings.json and
~/.gemini/config/config.json have no `hook` key at all; ~/.grok and
~/.kimi-code have no settings file. Cursor is an IDE and keeps its chat in a
SQLite key/value store rather than session files.

So five of six sources will never notify us. They have to be walked. Without
this module their turns are a snapshot frozen on the day they were backfilled,
and "telemetrify captures everything" is false the morning after.

WHAT THIS IS NOT. It is not a replacement for the hook. claude_code and grok
land within minutes through capture.py and are skipped here by default — a
scanner that re-walks 23 GB of ~/.claude to find turns the hook already filed is
pure cost. Pass --agents to override.

IDEMPOTENCE comes from insert_turn's INSERT OR IGNORE on user_uuid, not from the
cursor. scan_files exists so a run costs seconds instead of minutes; losing it
costs a slow scan, never a wrong one.
"""
from __future__ import annotations

import importlib
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import connect
from .store import upsert_session, insert_turn, record_ingest_run

# Agents this scanner owns. claude_code and grok are absent on purpose: they
# reach us through the Stop hook, and walking their directories would re-read
# 41 GB to find what capture.py already has.
SCANNED_AGENTS = ("codex", "qwen", "kimi", "gemini", "cursor")


def _mod(agent: str):
    return importlib.import_module(f".agents.{agent}", package="telemetrify")


def _files(agent: str) -> list[Path]:
    m = _mod(agent)
    if agent == "cursor":
        # One SQLite store, not per-session files. Its mtime moves whenever
        # Cursor writes anything, which is the correct trigger to re-read it.
        db = m.DEFAULT_DB
        return [db] if db.exists() else []
    return list(m.sessions())


def _turns_from(agent: str, path: Path):
    m = _mod(agent)
    if agent == "cursor":
        yield from m.iter_turns(path)
    else:
        yield from m.iter_turns(path)


def _provider_for(model: str | None) -> str | None:
    """Name the backend only when the MODEL STRING names it.

    Deliberately NOT derived from the agent. Migration 019 exists because those
    are independent — this corpus already holds glm, deepseek and abliterated
    models captured through Claude Code — and defaulting codex to 'openai'
    because codex usually talks to OpenAI would re-introduce exactly the
    conflation the column was added to prevent. An unnamed backend stays NULL.
    """
    if not model:
        return None
    m = model.lower()
    if m.startswith("claude-"):
        return "anthropic"
    if "/" in m:                       # openrouter-style "vendor/model"
        return m.split("/", 1)[0]
    if ":" in m:                       # ollama-style "name:tag"
        return "ollama"
    return None


def scan(agents: tuple[str, ...] = SCANNED_AGENTS, *, full: bool = False,
         log=print) -> dict:
    from .embed import embed_turn, embed_prompt
    from .cluster import assign_nearest_cluster

    conn = connect()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    totals = {"inserted": 0, "skipped": 0, "errors": 0, "files": 0, "reread": 0}
    per_agent: dict[str, int] = {}

    cursor = {} if full else {
        row[0]: row[1] for row in conn.execute("SELECT path, mtime_ms FROM scan_files")
    }

    for agent in agents:
        t0 = time.time()
        ins = skip = err = reread = 0
        try:
            files = _files(agent)
        except Exception:
            log(f"  {agent}: could not list sessions"); continue
        for p in files:
            try:
                mtime_ms = int(p.stat().st_mtime * 1000)
            except OSError:
                continue
            totals["files"] += 1
            if cursor.get(str(p)) == mtime_ms:
                continue                       # unchanged since last scan
            reread += 1
            n_turns = 0
            try:
                for t in _turns_from(agent, p):
                    if not t.user_text.strip():
                        continue
                    n_turns += 1
                    t.provider = _provider_for(t.model)
                    pv = embed_prompt(t.user_text)
                    with conn:
                        upsert_session(conn, t)
                        tid = insert_turn(conn, t,
                                          embed_turn(t.user_text, t.assistant_text),
                                          origin="organic", prompt_embedding=pv)
                    if tid is None:
                        skip += 1
                    else:
                        ins += 1
                        try:
                            with conn:
                                assign_nearest_cluster(conn, tid, pv)
                        except Exception:
                            pass
            except Exception:
                err += 1
            with conn:
                conn.execute(
                    """INSERT INTO scan_files(agent, path, mtime_ms, last_seen_at, turns)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET
                           mtime_ms=excluded.mtime_ms,
                           last_seen_at=excluded.last_seen_at,
                           turns=excluded.turns""",
                    (agent, str(p), mtime_ms,
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), n_turns))
        per_agent[agent] = ins
        totals["inserted"] += ins; totals["skipped"] += skip
        totals["errors"] += err; totals["reread"] += reread
        log(f"  {agent:8s} files={len(files):5,} reread={reread:5,} "
            f"inserted={ins:5,} skipped={skip:5,} errors={err:3,}  {time.time()-t0:5.1f}s")

    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        record_ingest_run(conn, "scan", started_at, finished_at,
                          totals["inserted"], totals["skipped"], totals["errors"],
                          note=",".join(f"{a}={n}" for a, n in per_agent.items()))
    return totals


def status(conn=None) -> list[tuple]:
    conn = conn or connect()
    return list(conn.execute(
        """SELECT agent, COUNT(*) files, SUM(turns) turns, MAX(last_seen_at) last_scan
           FROM scan_files GROUP BY agent ORDER BY 2 DESC"""))


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "status":
        rows = status()
        if not rows:
            print("no scan has run yet")
            return 0
        print(f"{'agent':10s} {'files':>7} {'turns':>7}  last scan")
        for a, f, t, last in rows:
            print(f"{a:10s} {f:7,} {(t or 0):7,}  {last}")
        return 0
    full = "--full" in argv
    agents = SCANNED_AGENTS
    for i, a in enumerate(argv):
        if a == "--agents" and i + 1 < len(argv):
            agents = tuple(argv[i + 1].split(","))
    print(f"scanning {', '.join(agents)}{' (full)' if full else ''}")
    t = scan(agents, full=full)
    print(f"inserted={t['inserted']:,} skipped={t['skipped']:,} errors={t['errors']:,} "
          f"files={t['files']:,} reread={t['reread']:,}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
