"""telemetrify.rocco_archive — keep the local corpus under a size cap by moving
the oldest turns to Rocco.

The local DB is a window over recent history. When its live size (pages in
use, not the file size) passes the cap, the oldest turns move into an archive
slice: a self-contained SQLite file holding those turns and every row that
hangs off them. The slice is uploaded to `$ROCCO_STORE_DIR/archive/` and
checked there (SHA-256 and per-table row counts, read back on Rocco) before a
single local row is deleted. Slices are never rewritten, so the archive plus
the replicated `prompts.db` together hold the full corpus.

Order of operations, each step gated on the one before:
  1. pick the oldest turns until the estimated size drops to TARGET_FRACTION
     of the cap (so the cap doesn't trip again on the next tick),
  2. export them into a slice under data/.rocco-archive/,
  3. upload the slice and verify the remote copy,
  4. in one write transaction, re-count the rows to be removed (a grade or
     annotation added since the export aborts the run), then delete,
  5. give the freed pages back to the filesystem.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import DATA_DIR, DB_PATH

GIB = 1024 ** 3
# Trim down to 90% of the cap so a run frees real headroom.
TARGET_FRACTION = 0.9
SLICE_DIR = DATA_DIR / ".rocco-archive"
# Vector tables are vec0 virtual tables; slices store their rows as plain BLOBs
# so a slice opens without the sqlite-vec extension.
VECTOR_TABLES = ("turn_vec", "prompt_vec")
# Tables a slice never copies: the turns themselves are copied explicitly, and
# the full-text index is rebuilt from turns by the AFTER DELETE trigger.
_SKIP_CHILDREN = {"turns"}


class ArchiveError(RuntimeError):
    """A step failed; nothing local was deleted."""


def cap_bytes() -> int:
    return int(float(os.environ.get("TELEMETRIFY_LOCAL_CAP_GB", "5")) * GIB)


def live_bytes(conn: sqlite3.Connection) -> int:
    """Bytes in pages that hold data. The file can be larger: deleted rows leave
    free pages until a vacuum gives them back."""
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return (pages - free) * page_size


# ── step 1: which turns ──────────────────────────────────────────────────
def pick_oldest(conn: sqlite3.Connection, excess: int) -> list[int]:
    """Oldest turns first until their estimated share of the DB covers `excess`.

    A turn's estimate is the bytes of its text and blobs plus its tool calls.
    Deleting a turn also frees its index entries and page slack, so the real
    saving is larger and a run lands a little under the target rather than
    over the cap. (Scaling estimates up to the live size would count fixed
    overhead, such as preallocated vector chunks, that no delete frees.)
    Turns with no timestamp of their own (some importers have none) are dated
    by their session."""
    if excess <= 0:
        return []
    rows = conn.execute(
        """
        SELECT t.id,
               octet_length(t.user_text) + octet_length(t.assistant_text)
               + IFNULL(octet_length(t.thinking_text), 0)
               + IFNULL(octet_length(t.raw_json_z), 0)
               + IFNULL(tc.b, 0) AS est
        FROM turns t
        JOIN sessions s ON s.id = t.session_id
        LEFT JOIN (
            SELECT turn_id,
                   SUM(IFNULL(octet_length(input_json), 0)
                       + IFNULL(octet_length(output_text), 0)) AS b
            FROM tool_calls GROUP BY turn_id
        ) tc ON tc.turn_id = t.id
        ORDER BY COALESCE(NULLIF(t.started_at, ''), s.started_at), t.id
        """
    ).fetchall()
    picked: list[int] = []
    freed = 0
    for turn_id, est in rows:
        if freed >= excess:
            break
        picked.append(turn_id)
        freed += est
    return picked


# ── step 2: export ───────────────────────────────────────────────────────
def _foreign_keys_into(conn: sqlite3.Connection, parent: str) -> list[tuple[str, str, str, str]]:
    """(table, column, referenced column, on_delete) for every foreign key into `parent`."""
    out = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall():
        if name in _SKIP_CHILDREN:
            continue
        for fk in conn.execute(
            'SELECT "from", "table", "to", on_delete FROM pragma_foreign_key_list(?)', (name,)
        ):
            if fk[1] == parent:
                out.append((name, fk[0], fk[2] or "rowid", fk[3].upper()))
    return out


def _row_plan(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, WHERE clause over temp.archive_ids) for the rows deleting the
    archived turns removes: the turns, every row a CASCADE reaches from them,
    and one level beyond (rerun_judgments of cascaded reruns)."""
    ids = "(SELECT id FROM temp.archive_ids)"
    cascaded: dict[str, list[str]] = {}
    for table, col, _to, action in _foreign_keys_into(conn, "turns"):
        if action == "CASCADE":
            cascaded.setdefault(table, []).append(f'"{col}" IN {ids}')
    plan = [("turns", f"id IN {ids}")]
    plan += [(t, " OR ".join(clauses)) for t, clauses in cascaded.items()]
    for table, where in list(plan[1:]):
        for grandchild, col, to, action in _foreign_keys_into(conn, table):
            if action == "CASCADE" and grandchild not in cascaded and grandchild != "turns":
                plan.append((grandchild, f'"{col}" IN (SELECT "{to}" FROM "{table}" WHERE {where})'))
    return plan


def _set_null_links(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) pairs whose pointer to an archived turn becomes NULL
    rather than deleting the row. The slice records the old values."""
    return [(t, c) for t, c, _to, action in _foreign_keys_into(conn, "turns") if action == "SET NULL"]


def _sessions_clause() -> str:
    return "id IN (SELECT session_id FROM turns WHERE id IN (SELECT id FROM temp.archive_ids))"


def count_rows(conn: sqlite3.Connection, plan: list[tuple[str, str]], schema: str = "main") -> dict:
    counts = {}
    for table, where in plan:
        counts[table] = conn.execute(f'SELECT COUNT(*) FROM {schema}."{table}" WHERE {where}').fetchone()[0]
    return counts


def _create_in_slice(conn: sqlite3.Connection, table: str) -> None:
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]
    # Same columns, types and keys as the live table, created in the slice.
    pattern = (r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?'
               + re.escape(table) + r'["`\]]?(?=\s*\()')
    created, n = re.subn(pattern, f'CREATE TABLE IF NOT EXISTS slice."{table}"', sql,
                         count=1, flags=re.IGNORECASE)
    if n != 1:
        raise ArchiveError(f"cannot read the definition of {table}")
    conn.execute(created)


def _load_ids(conn: sqlite3.Connection, ids: list[int]) -> None:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS archive_ids (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp.archive_ids")
    conn.executemany("INSERT INTO temp.archive_ids(id) VALUES (?)", [(i,) for i in ids])
    # ATTACH and BEGIN IMMEDIATE both refuse to run inside an open transaction.
    conn.commit()


def export_slice(conn: sqlite3.Connection, ids: list[int], path: Path) -> dict:
    """Write the turns and every row that hangs off them into a new file.
    Returns the per-table row counts written. `conn` must have sqlite-vec."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    _load_ids(conn, ids)
    plan = _row_plan(conn)
    conn.execute("ATTACH DATABASE ? AS slice", (str(path),))
    # The slice holds a subset, so references to tables it leaves out (clusters)
    # must not be enforced while copying. Nothing in main is written here.
    fk_was = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("PRAGMA slice.journal_mode=DELETE")
        with conn:
            counts = {}
            for table, where in plan:
                _create_in_slice(conn, table)
                cur = conn.execute(f'INSERT INTO slice."{table}" SELECT * FROM main."{table}" WHERE {where}')
                counts[table] = cur.rowcount
            # Sessions stay on the Mac too; the slice carries a copy so it stands alone.
            _create_in_slice(conn, "sessions")
            conn.execute(f'INSERT INTO slice.sessions SELECT * FROM main.sessions WHERE {_sessions_clause()}')
            conn.execute("CREATE TABLE slice.set_null_links "
                         "(tbl TEXT, row_id INTEGER, col TEXT, turn_id INTEGER)")
            for table, col in _set_null_links(conn):
                cur = conn.execute(
                    f'INSERT INTO slice.set_null_links SELECT ?, rowid, ?, "{col}" FROM main."{table}" '
                    f'WHERE "{col}" IN (SELECT id FROM temp.archive_ids)', (table, col))
                counts[f"{table}.{col}"] = cur.rowcount
            for vt in VECTOR_TABLES:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (vt,)).fetchone():
                    continue
                conn.execute(f'CREATE TABLE slice."{vt}" (turn_id INTEGER PRIMARY KEY, embedding BLOB)')
                cur = conn.execute(f'INSERT INTO slice."{vt}" SELECT turn_id, embedding FROM main."{vt}" '
                                   f'WHERE turn_id IN (SELECT id FROM temp.archive_ids)')
                counts[vt] = cur.rowcount
            conn.execute("CREATE TABLE slice.slice_info (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO slice.slice_info VALUES (?, ?)", [
                ("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
                ("counts", json.dumps(counts)),
                ("note", "Turns moved off the Mac by telemetrify.rocco_archive. "
                         "Plain tables; vectors are float32 BLOBs."),
            ])
    finally:
        conn.execute(f"PRAGMA foreign_keys={'ON' if fk_was else 'OFF'}")
        conn.execute("DETACH DATABASE slice")
    return counts


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── step 4: delete ───────────────────────────────────────────────────────
def delete_archived(conn: sqlite3.Connection, ids: list[int], expected: dict) -> int:
    """Delete the archived turns in one transaction, after checking that the rows
    about to go are exactly the rows the verified slice holds. foreign_keys=ON
    cascades the child rows; the vec0 rows are deleted explicitly."""
    if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ArchiveError("foreign_keys is off; children would be orphaned")
    _load_ids(conn, ids)
    plan = _row_plan(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = count_rows(conn, plan)
        for table, col in _set_null_links(conn):
            now[f"{table}.{col}"] = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" IN (SELECT id FROM temp.archive_ids)'
            ).fetchone()[0]
        for vt in VECTOR_TABLES:
            if vt in expected:
                now[vt] = conn.execute(f'SELECT COUNT(*) FROM "{vt}" WHERE turn_id IN '
                                       f'(SELECT id FROM temp.archive_ids)').fetchone()[0]
        if now != expected:
            raise ArchiveError(f"rows changed since export: {now} != {expected}")
        for vt in VECTOR_TABLES:
            if vt in expected:
                conn.execute(f'DELETE FROM "{vt}" WHERE turn_id IN (SELECT id FROM temp.archive_ids)')
        deleted = conn.execute("DELETE FROM turns WHERE id IN (SELECT id FROM temp.archive_ids)").rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return deleted


# ── step 5: shrink ───────────────────────────────────────────────────────
def shrink(conn: sqlite3.Connection) -> str:
    """Give free pages back to the filesystem. The first run switches the DB to
    incremental auto-vacuum, which needs one full VACUUM; later runs are cheap."""
    mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
    if mode != 2:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
        how = "vacuum (switched to incremental)"
    else:
        conn.execute("PRAGMA incremental_vacuum")
        how = "incremental_vacuum"
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return how


def slice_name(conn: sqlite3.Connection, ids: list[int]) -> str:
    _load_ids(conn, ids)
    lo, hi = conn.execute(
        "SELECT MIN(d), MAX(d) FROM (SELECT substr(COALESCE(NULLIF(t.started_at,''), s.started_at),1,10) d "
        "FROM turns t JOIN sessions s ON s.id=t.session_id WHERE t.id IN (SELECT id FROM temp.archive_ids)) "
        "WHERE d <> ''"
    ).fetchone()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    # Dates come from captured transcripts, and rsync hands the remote name to
    # Rocco's shell, so only a well-formed date may reach the file name.

    def safe(d: object) -> str:
        return d if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else "undated"
    return f"turns-{safe(lo)}_{safe(hi)}-{len(ids)}-{stamp}.db"


def open_db(path: Path | None = None) -> sqlite3.Connection:
    """A connection with foreign keys on and sqlite-vec loaded, without running
    migrations (the archive never changes the schema)."""
    from .db import _raw_connect
    return _raw_connect(Path(path) if path is not None else DB_PATH)
