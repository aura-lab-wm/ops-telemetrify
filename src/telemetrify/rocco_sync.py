"""telemetrify.rocco_sync — replicate the local SQLite corpus to Rocco.

The local DB (`data/prompts.db`) is telemetrify's "generous local cache": every
captured turn lands there first. This module copies that whole DB up to the
Rocco GPU box's persistent storage so the corpus survives a Mac wipe and is
reachable from Rocco-side tooling. **Copy semantics, never move** — the local
DB is never modified or deleted; Rocco is a durable replica.

Push triggers (evaluated each `tick`, driven by a 5-min launchd agent):
  • bootstrap  — first ever run, no prior push → seed the remote.
  • retry      — the previous push failed → try again while connected.
  • reconnect  — connectivity returned after an outage (prev_connected False
                 → now True): flush the backlog accumulated during the outage.
  • active-2h  — connection healthy AND ≥2h of *cumulative active session time*
                 (Σ per-session max(finished_at)-min(started_at)) has elapsed
                 since the last successful push.

`restore` is the reverse direction, used only for disaster recovery: if the
local DB is missing/empty on startup, pull the snapshot back down from Rocco so
general ("whole-corpus") queries have history to search. It refuses to clobber a
present local DB unless `--force`.

Transport: `sqlite3_rsync` (SQLite's own replication tool) over ssh to
`$ROCCO_SSH_HOST` (default "rocco") at `$ROCCO_STORE_DIR` (default
"~/telemetrify-store"). It reads the live DB in one read transaction and sends
only the pages that differ, so a push needs no local copy. Without the tool on
the Mac, the fallback writes one compact snapshot with VACUUM INTO, rsyncs it
and always deletes it.

Size cap: after each tick, if the local DB holds more than
`$TELEMETRIFY_LOCAL_CAP_GB` (default 5) GiB of data, the oldest turns move to
`$ROCCO_STORE_DIR/archive/` (see telemetrify.rocco_archive). Archive slices are
never rewritten, so they keep the turns the replica no longer has.

Every ssh/rsync call funnels through the single `_run` seam so tests can stub
the network entirely.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import DATA_DIR, DB_PATH, ROOT
from . import rocco_archive as archive

# 2h of cumulative active session time triggers a healthy-connection flush.
THRESHOLD_S = 2 * 3600
# Per-session cap on counted active time. A session_id whose turns span days
# (idle gaps, a resumed session, clock skew) would otherwise inflate the total
# to months and over-eagerly trip the 2h trigger.
SESSION_ACTIVE_CAP_S = 6 * 3600

STATE_PATH = DATA_DIR / "rocco-sync-state.json"
SNAPSHOT_PATH = DATA_DIR / ".rocco-snapshot" / "prompts.db"


class RoccoSyncError(RuntimeError):
    """A push/restore step failed (mkdir/rsync non-zero, etc.)."""


# ── config (env-driven) ──────────────────────────────────────────────────
def ssh_host() -> str:
    return os.environ.get("ROCCO_SSH_HOST", "rocco")


def store_dir() -> str:
    return os.environ.get("ROCCO_STORE_DIR", "~/telemetrify-store").rstrip("/")


def _ssh_opts() -> list[str]:
    return os.environ.get(
        "ROCCO_SSH_OPTS", "-o BatchMode=yes -o ConnectTimeout=4").split()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _remote_target() -> str:
    return f"{ssh_host()}:{store_dir()}/prompts.db"


def sqlite3_rsync_path() -> str | None:
    """The local sqlite3_rsync, or None. launchd's PATH lacks ~/.local/bin, so
    that is checked explicitly."""
    explicit = os.environ.get("ROCCO_SQLITE3_RSYNC")
    if explicit:
        return explicit if Path(explicit).exists() else None
    found = shutil.which("sqlite3_rsync")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "sqlite3_rsync"
    return str(local) if local.exists() else None


def _remote_sqlite3_rsync() -> str:
    # Relative to the remote home, where ssh starts the command.
    return os.environ.get("ROCCO_SQLITE3_RSYNC_REMOTE", ".local/bin/sqlite3_rsync")


def _remote_path(name: str) -> str:
    """`name` inside the store dir, as a path relative to the remote home
    (sqlite3_rsync and the verify script get it unexpanded)."""
    d = store_dir()
    if d.startswith("~/"):
        d = d[2:]
    return f"{d}/{name}"


# ── subprocess seam (tests monkeypatch this) ─────────────────────────────
def _run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ── persistent state ─────────────────────────────────────────────────────
@dataclass
class SyncState:
    last_push_finished_at: str | None = None
    last_push_ok: bool = False
    active_seconds_at_last_push: int = 0
    prev_connected: bool | None = None
    # Slices moved to Rocco: name, turns, bytes, sha256, archived_at.
    archives: list = field(default_factory=list)


def load_state(path: Path | None = None) -> SyncState:
    p = Path(path) if path is not None else STATE_PATH
    try:
        d = json.loads(p.read_text())
    except Exception:
        return SyncState()
    return SyncState(
        last_push_finished_at=d.get("last_push_finished_at"),
        last_push_ok=bool(d.get("last_push_ok", False)),
        active_seconds_at_last_push=int(d.get("active_seconds_at_last_push", 0)),
        prev_connected=d.get("prev_connected"),
        archives=list(d.get("archives", [])),
    )


def save_state(state: SyncState, path: Path | None = None) -> None:
    p = Path(path) if path is not None else STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(state), indent=2))


# ── active session time ──────────────────────────────────────────────────
def _parse_iso(s: object) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def active_session_seconds(conn: sqlite3.Connection) -> int:
    """Σ over sessions of (max(finished_at) − min(started_at)), in whole seconds.

    A session that hasn't finished a turn falls back to started_at for its end,
    contributing 0 rather than crashing. NULL session_ids are excluded."""
    rows = conn.execute(
        "SELECT session_id, MIN(started_at), MAX(COALESCE(finished_at, started_at)) "
        "FROM turns WHERE session_id IS NOT NULL GROUP BY session_id"
    ).fetchall()
    total = 0
    for r in rows:
        a, b = _parse_iso(r[1]), _parse_iso(r[2])
        if a and b:
            secs = (b - a).total_seconds()
            if secs > 0:
                total += min(int(secs), SESSION_ACTIVE_CAP_S)
    return total


# ── connectivity ─────────────────────────────────────────────────────────
def probe_connected(host: str | None = None, opts: list[str] | None = None) -> bool:
    """`ssh <opts> <host> true` → exit 0 means reachable. Any error → False."""
    host = host if host is not None else ssh_host()
    opts = opts if opts is not None else _ssh_opts()
    try:
        cp = _run(["ssh", *opts, host, "true"], timeout=15)
        return cp.returncode == 0
    except Exception:
        return False


# ── snapshot + push ──────────────────────────────────────────────────────
def remove_snapshot(path: Path | None = None) -> None:
    """Delete a snapshot and any journal files beside it."""
    out = Path(path) if path is not None else SNAPSHOT_PATH
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{out}{suffix}").unlink(missing_ok=True)


def make_snapshot(db_path: Path | None = None, dest: Path | None = None) -> Path:
    """Consistent, compact copy of the live DB, so we never rsync a file
    mid-write. VACUUM INTO writes a fresh file directly; the old backup-API copy
    landed in a WAL-mode leftover from a failed push and doubled through a
    journal as large as the DB. Returns the snapshot path."""
    src_path = Path(db_path) if db_path is not None else DB_PATH
    out = Path(dest) if dest is not None else SNAPSHOT_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    remove_snapshot(out)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        src.execute("VACUUM INTO ?", (str(out),))
    finally:
        src.close()
    return out


def ensure_remote_dir() -> None:
    cp = _run(["ssh", *_ssh_opts(), ssh_host(), f"mkdir -p {store_dir()}"], timeout=15)
    if cp.returncode != 0:
        raise RoccoSyncError(f"remote mkdir failed ({cp.returncode}): {cp.stderr.strip()}")


def rsync_push(snapshot: Path) -> None:
    transport = "ssh " + " ".join(_ssh_opts())
    cp = _run(["rsync", "-a", "--partial", "--inplace", "-e", transport,
               str(snapshot), _remote_target()], timeout=3600)
    if cp.returncode != 0:
        raise RoccoSyncError(
            f"rsync push failed ({cp.returncode}): {cp.stderr.strip()[-300:]}")


def page_sync(tool: str) -> None:
    """Make the remote prompts.db a copy of the live local DB, page by page."""
    cp = _run([tool, str(DB_PATH), f"{ssh_host()}:{_remote_path('prompts.db')}",
               "--exe", _remote_sqlite3_rsync(), "--ssh", str(ROOT / "bin" / "rocco-ssh")],
              timeout=3600)
    if cp.returncode != 0:
        raise RoccoSyncError(
            f"sqlite3_rsync failed ({cp.returncode}): {(cp.stderr or cp.stdout).strip()[-300:]}")


def push() -> dict:
    """Bring the remote replica up to date and ship a manifest. Raises on failure."""
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = int(src.execute("SELECT COUNT(*) FROM turns").fetchone()[0])
    finally:
        src.close()

    ensure_remote_dir()
    tool = sqlite3_rsync_path()
    if tool:
        page_sync(tool)
        size, method = DB_PATH.stat().st_size, "sqlite3_rsync"
    else:
        snap = make_snapshot()
        try:
            rsync_push(snap)
            size, method = snap.stat().st_size, "snapshot"
        finally:
            # Never leave a snapshot behind, success or failure.
            remove_snapshot(snap)

    manifest = {"rows": rows, "bytes": size, "method": method, "finished_at": _now_iso()}
    mpath = SNAPSHOT_PATH.parent / "manifest.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest))
    transport = "ssh " + " ".join(_ssh_opts())
    mcp = _run(["rsync", "-a", "-e", transport, str(mpath),
                f"{ssh_host()}:{store_dir()}/manifest.json"], timeout=60)
    # The DB already landed; a manifest failure is non-fatal but worth flagging
    # so the remote manifest can't silently diverge from the pushed DB.
    manifest["manifest_pushed"] = (mcp.returncode == 0)

    return manifest


# ── archive (size cap) ───────────────────────────────────────────────────
_VERIFY_SCRIPT = """
import hashlib, json, os, sqlite3, sys
p = os.path.expanduser(sys.argv[1])
h = hashlib.sha256()
with open(p, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
        h.update(chunk)
c = sqlite3.connect("file:" + p + "?mode=ro", uri=True)
print(json.dumps({"sha256": h.hexdigest(),
                  "check": c.execute("PRAGMA quick_check").fetchone()[0],
                  "turns": c.execute("SELECT COUNT(*) FROM turns").fetchone()[0]}))
"""


def upload_slice(path: Path) -> None:
    cp = _run(["ssh", *_ssh_opts(), ssh_host(), f"mkdir -p {store_dir()} && mkdir -p -m 700 {store_dir()}/archive"], timeout=15)
    if cp.returncode != 0:
        raise RoccoSyncError(f"remote mkdir archive failed ({cp.returncode}): {cp.stderr.strip()}")
    transport = "ssh " + " ".join(_ssh_opts())
    cp = _run(["rsync", "-a", "--partial", "-e", transport, str(path),
               f"{ssh_host()}:{store_dir()}/archive/{path.name}"], timeout=3600)
    if cp.returncode != 0:
        raise RoccoSyncError(f"slice upload failed ({cp.returncode}): {cp.stderr.strip()[-300:]}")


def verify_remote_slice(name: str) -> dict:
    """SHA-256, quick_check and turn count of the slice as Rocco reads it."""
    remote = shlex.join(["python3", "-c", _VERIFY_SCRIPT, _remote_path(f"archive/{name}")])
    cp = _run(["ssh", *_ssh_opts(), ssh_host(), remote], timeout=600)
    if cp.returncode != 0:
        raise RoccoSyncError(f"remote verify failed ({cp.returncode}): {cp.stderr.strip()[-300:]}")
    return json.loads(cp.stdout.strip().splitlines()[-1])


def archive_if_over_cap(*, db_path: Path | None = None, dry_run: bool = False) -> dict | None:
    """Move the oldest turns to Rocco when the local DB passes the cap. Returns
    None when under the cap. Nothing local is deleted unless Rocco's copy of
    the slice matches byte for byte and opens cleanly there."""
    dbp = Path(db_path) if db_path is not None else DB_PATH
    conn = archive.open_db(dbp)
    try:
        live, cap = archive.live_bytes(conn), archive.cap_bytes()
        if live <= cap:
            return None
        ids = archive.pick_oldest(conn, live - int(cap * archive.TARGET_FRACTION))
        if not ids:
            return None
        name = archive.slice_name(conn, ids)
        plan = {"slice": name, "turns": len(ids), "live_bytes": live, "cap_bytes": cap}
        if dry_run:
            return {**plan, "dry_run": True}
        path = archive.SLICE_DIR / name
        try:
            counts = archive.export_slice(conn, ids, path)
            sha, size = archive.sha256_file(path), path.stat().st_size
            upload_slice(path)
            remote = verify_remote_slice(name)
            if (remote.get("sha256") != sha or remote.get("check") != "ok"
                    or remote.get("turns") != counts["turns"]):
                raise RoccoSyncError(f"remote slice does not match: {remote} vs sha={sha} "
                                     f"turns={counts['turns']}")
            deleted = archive.delete_archived(conn, ids, counts)
            shrunk = archive.shrink(conn)
        finally:
            for suffix in ("", "-journal"):
                Path(f"{path}{suffix}").unlink(missing_ok=True)
        return {**plan, "deleted": deleted, "counts": counts, "sha256": sha, "bytes": size,
                "shrink": shrunk, "live_bytes_after": archive.live_bytes(conn),
                "file_bytes_after": dbp.stat().st_size,
                "archived_at": _now_iso()}
    finally:
        conn.close()


# ── decision ─────────────────────────────────────────────────────────────
def decide(state: SyncState, connected: bool, active_now: int) -> tuple[bool, str]:
    if not connected:
        return (False, "offline")
    if state.last_push_finished_at is None:
        return (True, "bootstrap")
    if not state.last_push_ok:
        return (True, "retry")
    if state.prev_connected is False:
        return (True, "reconnect")
    if active_now - state.active_seconds_at_last_push >= THRESHOLD_S:
        return (True, "active-2h")
    return (False, "hold")


# ── tick (driven by the launchd agent) ───────────────────────────────────
@contextlib.contextmanager
def _single_flight():
    """Yields True when this process holds the sync lock, False when another
    run does. A push or an archive can outlast the 5-min launchd interval."""
    lock_path = Path(STATE_PATH).parent / ".rocco-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(lock_path, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lockf.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        finally:
            lockf.close()


def tick(*, db_path: Path | None = None) -> dict:
    with _single_flight() as held:
        if not held:
            return {"skipped": "locked", "pushed": False}
        return _tick_locked(db_path=db_path)


def _tick_locked(*, db_path: Path | None = None) -> dict:
    state = load_state()
    connected = probe_connected()

    dbp = Path(db_path) if db_path is not None else DB_PATH
    try:
        conn = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        try:
            active_now = active_session_seconds(conn)
        finally:
            conn.close()
    except Exception:
        active_now = state.active_seconds_at_last_push

    should, reason = decide(state, connected, active_now)
    result: dict = {"connected": connected, "active_seconds": active_now,
                    "decision": reason, "pushed": False}

    if should:
        try:
            manifest = push()
            state.last_push_finished_at = manifest["finished_at"]
            state.last_push_ok = True
            state.active_seconds_at_last_push = active_now
            result["pushed"] = True
            result["manifest"] = manifest
        except Exception as e:
            state.last_push_finished_at = _now_iso()
            state.last_push_ok = False
            result["error"] = str(e)

    if connected:
        try:
            moved = archive_if_over_cap(db_path=dbp)
            if moved:
                state.archives.append({k: moved[k] for k in
                                       ("slice", "turns", "bytes", "sha256", "archived_at")})
                result["archive"] = moved
        except Exception as e:
            result["archive_error"] = str(e)

    state.prev_connected = connected
    save_state(state)
    return result


# ── restore (disaster recovery on startup) ───────────────────────────────
def restore(*, force: bool = False, db_path: Path | None = None) -> dict:
    dbp = Path(db_path) if db_path is not None else DB_PATH
    present = dbp.exists() and dbp.stat().st_size > 0
    if present and not force:
        return {"restored": False, "reason": "local DB present"}

    dbp.parent.mkdir(parents=True, exist_ok=True)
    # Download to a temp path and atomically rename on success, so the final DB
    # path NEVER appears as a truncated/partial file. A truncated prompts.db
    # would look "present" and permanently block a re-restore while serving a
    # corrupt corpus.
    tmp = dbp.with_name(dbp.name + ".restore-tmp")
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    transport = "ssh " + " ".join(_ssh_opts())
    cp = _run(["rsync", "-a", "--inplace", "-e", transport,
               _remote_target(), str(tmp)], timeout=3600)
    if cp.returncode != 0 or not tmp.exists():
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return {"restored": False,
                "reason": f"rsync pull failed ({cp.returncode}): {cp.stderr.strip()[-200:]}"}

    os.replace(tmp, dbp)  # atomic publish
    return {"restored": True,
            "bytes": dbp.stat().st_size if dbp.exists() else 0}


# ── CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="telemetrify-rocco-sync",
        description="Replicate the local corpus to Rocco persistent storage.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tick", help="Evaluate triggers and push if due.")
    rp = sub.add_parser("restore", help="Pull the corpus back from Rocco (if local is missing).")
    rp.add_argument("--force", action="store_true", help="Overwrite a present local DB.")
    sub.add_parser("status", help="Print current sync state.")
    ap = sub.add_parser("archive", help="Move the oldest turns to Rocco if over the size cap.")
    ap.add_argument("--dry-run", action="store_true", help="Show what would move; change nothing.")

    args = p.parse_args(argv)
    if args.cmd == "tick":
        print(json.dumps(tick()))
        return 0
    if args.cmd == "restore":
        print(json.dumps(restore(force=args.force)))
        return 0
    if args.cmd == "status":
        st = load_state()
        local = {}
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                local = {"live_bytes": archive.live_bytes(conn),
                         "file_bytes": DB_PATH.stat().st_size,
                         "oldest_local_turn": conn.execute(
                             "SELECT MIN(started_at) FROM turns WHERE started_at <> ''").fetchone()[0]}
            finally:
                conn.close()
        except Exception as e:
            local = {"error": str(e)}
        print(json.dumps({**asdict(st), "host": ssh_host(), "store": store_dir(),
                          "threshold_s": THRESHOLD_S, "cap_bytes": archive.cap_bytes(),
                          "method": "sqlite3_rsync" if sqlite3_rsync_path() else "snapshot",
                          **local}))
        return 0
    if args.cmd == "archive":
        with _single_flight() as held:
            if not held:
                print(json.dumps({"skipped": "locked"}))
                return 1
            moved = archive_if_over_cap(dry_run=args.dry_run)
            if moved and not args.dry_run:
                st = load_state()
                st.archives.append({k: moved[k] for k in
                                    ("slice", "turns", "bytes", "sha256", "archived_at")})
                save_state(st)
            print(json.dumps(moved or {"under_cap": True}))
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
