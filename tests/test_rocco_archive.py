"""Tests for the 5 GB cap (telemetrify.rocco_archive) and the page-level push.

The archive tests run on the real migrated schema (the `migrated_db` fixture),
so every foreign key, cascade and vec0 table is the production one. The network
is replaced at the two seams rocco_sync exposes: `upload_slice` copies the slice
into a temp "remote" dir and `verify_remote_slice` runs the same checks Rocco
runs, on that copy.
"""
from __future__ import annotations

import hashlib
import re
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

GIB = 1024 ** 3


def _seed(conn: sqlite3.Connection) -> dict:
    """Two old sessions and one new one. Old turns carry every kind of child row."""
    import sqlite_vec  # noqa: F401  (the fixture already loaded it)

    conn.executemany(
        "INSERT INTO sessions(id, started_at, cwd) VALUES (?,?,?)",
        [("old-a", "2026-05-01T00:00:00Z", "/x"), ("old-b", "2026-05-02T00:00:00Z", "/x"),
         ("new", "2026-11-10T00:00:00Z", "/x")])
    ids = {}

    def turn(key, session, started, size):
        cur = conn.execute(
            "INSERT INTO turns(session_id, user_text, assistant_text, started_at) VALUES (?,?,?,?)",
            (session, "q" * 10, "a" * size, started))
        ids[key] = cur.lastrowid
        conn.execute("INSERT INTO turn_vec(turn_id, embedding) VALUES (?, ?)",
                     (cur.lastrowid, sqlite_vec.serialize_float32([0.5] * 384)))
        return cur.lastrowid

    # Kimi-style turn with no timestamp of its own, dated by its May session.
    turn("undated", "old-b", "", 1_000_000)
    turn("old1", "old-a", "2026-05-01T01:00:00Z", 1_000_000)
    turn("old2", "old-a", "2026-05-03T01:00:00Z", 1_000_000)
    for i in range(6):
        turn(f"new{i}", "new", f"2026-11-1{i}T01:00:00Z", 1_000_000)

    o1 = ids["old1"]
    conn.execute("INSERT INTO tool_calls(turn_id, seq, tool_name, output_text) VALUES (?,?,?,?)",
                 (o1, 0, "Bash", "x" * 1000))
    conn.execute("INSERT INTO annotations(turn_id, rating, label) VALUES (?, 1, 'good')", (o1,))
    rerun = conn.execute("INSERT INTO reruns(original_turn_id, replay_turn_id) VALUES (?, ?)",
                         (o1, ids["new0"])).lastrowid
    conn.execute("INSERT INTO rerun_judgments(rerun_id, verdict) VALUES (?, 'same')", (rerun,))
    # A cluster on the Mac whose representative is an old turn: survives, pointer nulls.
    cluster = conn.execute("INSERT INTO prompt_clusters(label, representative_turn_id) VALUES ('c', ?)",
                           (ids["old2"],)).lastrowid
    conn.execute("INSERT INTO turn_cluster(turn_id, cluster_id) VALUES (?, ?)", (ids["new1"], cluster))
    conn.commit()
    return ids


@pytest.fixture
def seeded(migrated_db, tmp_path, monkeypatch):
    from telemetrify import rocco_archive as archive
    from telemetrify import rocco_sync as rs
    import telemetrify as pkg

    ids = _seed(migrated_db)
    db_path = pkg.DB_PATH  # the fixture's temp DB
    remote = tmp_path / "remote-archive"
    remote.mkdir()
    monkeypatch.setattr(archive, "SLICE_DIR", tmp_path / "slices")
    monkeypatch.setattr(rs, "STATE_PATH", tmp_path / "state.json")

    def fake_upload(path: Path) -> None:
        shutil.copy(path, remote / (path.name + rs.PART))

    def fake_verify(name: str) -> dict:
        p = remote / (name + rs.PART)
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    "check": c.execute("PRAGMA quick_check").fetchone()[0],
                    "turns": c.execute("SELECT COUNT(*) FROM turns").fetchone()[0]}
        finally:
            c.close()

    monkeypatch.setattr(rs, "upload_slice", fake_upload)
    monkeypatch.setattr(rs, "verify_remote_slice", fake_verify)
    monkeypatch.setattr(rs, "publish_remote_slice",
                        lambda name: (remote / (name + rs.PART)).rename(remote / name))
    monkeypatch.setattr(rs, "discard_remote_part",
                        lambda name: (remote / (name + rs.PART)).unlink(missing_ok=True))
    return {"conn": migrated_db, "ids": ids, "db": db_path, "remote": remote}


def _cap_at(monkeypatch, conn, fraction: float) -> None:
    from telemetrify import rocco_archive as archive
    monkeypatch.setenv("TELEMETRIFY_LOCAL_CAP_GB", str(archive.live_bytes(conn) * fraction / GIB))


def test_under_cap_does_nothing(seeded, monkeypatch):
    from telemetrify import rocco_sync as rs
    monkeypatch.setenv("TELEMETRIFY_LOCAL_CAP_GB", "5")
    assert rs.archive_if_over_cap(db_path=seeded["db"]) is None
    assert seeded["conn"].execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 9


def test_pick_oldest_dates_undated_turns_by_their_session(seeded):
    from telemetrify import rocco_archive as archive
    conn, ids = seeded["conn"], seeded["ids"]
    picked = archive.pick_oldest(conn, excess=1)
    # old1 (May 1 01:00) precedes the undated turn, whose session began May 2.
    assert picked == [ids["old1"]]
    picked = archive.pick_oldest(conn, excess=archive.live_bytes(conn) * 3 // 10)
    assert picked[:3] == [ids["old1"], ids["undated"], ids["old2"]]


def test_archive_moves_oldest_turns_with_every_child_row(seeded, monkeypatch):
    from telemetrify import rocco_sync as rs
    conn, ids = seeded["conn"], seeded["ids"]
    _cap_at(monkeypatch, conn, 0.75)

    moved = rs.archive_if_over_cap(db_path=seeded["db"])

    assert moved and moved["deleted"] == moved["turns"] >= 3
    gone = {ids["old1"], ids["undated"], ids["old2"]}
    left = {r[0] for r in conn.execute("SELECT id FROM turns")}
    assert not gone & left
    assert ids["new5"] in left
    # Children cascaded away locally, vectors deleted explicitly.
    for table in ("tool_calls", "annotations", "reruns", "rerun_judgments"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM turn_vec WHERE turn_id IN (?,?,?)",
                        tuple(gone)).fetchone()[0] == 0
    # The cluster survives with its pointer nulled; the old value is in the slice.
    assert conn.execute("SELECT representative_turn_id FROM prompt_clusters").fetchone()[0] is None
    # Full-text index follows the delete trigger.
    assert conn.execute("SELECT COUNT(*) FROM turns_fts").fetchone()[0] == len(left)

    remote = seeded["remote"] / moved["slice"]
    s = sqlite3.connect(f"file:{remote}?mode=ro", uri=True)
    try:
        assert {r[0] for r in s.execute("SELECT id FROM turns")} >= gone
        assert s.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
        assert s.execute("SELECT label FROM annotations").fetchone()[0] == "good"
        assert s.execute("SELECT COUNT(*) FROM rerun_judgments").fetchone()[0] == 1
        assert s.execute("SELECT COUNT(*) FROM turn_vec").fetchone()[0] == moved["turns"]
        assert len(s.execute("SELECT embedding FROM turn_vec").fetchone()[0]) == 384 * 4
        assert s.execute("SELECT tbl, col, turn_id FROM set_null_links "
                         "WHERE tbl='prompt_clusters'").fetchone() == ("prompt_clusters",
                                                                     "representative_turn_id",
                                                                     ids["old2"])
        assert {r[0] for r in s.execute("SELECT id FROM sessions")} >= {"old-a", "old-b"}
    finally:
        s.close()
    # The local slice is gone once Rocco holds it, and the file shrank.
    assert not list((Path(seeded["db"]).parent.parent / "slices").glob("*.db"))
    assert moved["live_bytes_after"] <= moved["cap_bytes"]
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_nothing_is_deleted_when_rocco_copy_differs(seeded, monkeypatch):
    from telemetrify import rocco_sync as rs
    conn = seeded["conn"]
    _cap_at(monkeypatch, conn, 0.75)
    monkeypatch.setattr(rs, "verify_remote_slice",
                        lambda name: {"sha256": "0" * 64, "check": "ok", "turns": 3})
    with pytest.raises(rs.RoccoSyncError):
        rs.archive_if_over_cap(db_path=seeded["db"])
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 9
    assert list(seeded["remote"].iterdir()) == []


def test_upload_never_leaves_a_file_under_a_slice_name(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs
    calls = []

    def run(cmd, timeout=None):
        calls.append(cmd)
        rc = 30 if cmd[0] == "rsync" else 0  # rsync: I/O timeout on a stalled link
        return subprocess.CompletedProcess(cmd, rc, "", "timeout in data send/receive")
    monkeypatch.setattr(rs, "_run", run)
    slice_path = tmp_path / "turns-2026-01-01_2026-02-01-5-20261116T0800Z.db"
    slice_path.write_bytes(b"x")
    with pytest.raises(rs.RoccoSyncError):
        rs.upload_slice(slice_path)
    rsync = next(c for c in calls if c[0] == "rsync")
    assert "--partial" not in rsync and "--timeout=120" in rsync
    assert rsync[-1].endswith(slice_path.name + ".part")
    assert any(c[0] == "ssh" and "rm -f" in c[-1] and ".part" in c[-1] for c in calls)


def test_nothing_is_deleted_when_upload_fails(seeded, monkeypatch):
    from telemetrify import rocco_sync as rs
    conn = seeded["conn"]
    _cap_at(monkeypatch, conn, 0.75)

    def boom(path):
        raise rs.RoccoSyncError("network down")
    monkeypatch.setattr(rs, "upload_slice", boom)
    with pytest.raises(rs.RoccoSyncError):
        rs.archive_if_over_cap(db_path=seeded["db"])
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 9


def test_delete_aborts_when_rows_changed_since_export(seeded, tmp_path):
    from telemetrify import rocco_archive as archive
    conn, ids = seeded["conn"], seeded["ids"]
    picked = [ids["old2"]]
    counts = archive.export_slice(conn, picked, tmp_path / "s.db")
    # A grade lands on the old turn after the slice was made.
    conn.execute("INSERT INTO annotations(turn_id, rating) VALUES (?, -1)", (ids["old2"],))
    conn.commit()
    with pytest.raises(archive.ArchiveError):
        archive.delete_archived(conn, picked, counts)
    assert conn.execute("SELECT COUNT(*) FROM turns WHERE id=?", (ids["old2"],)).fetchone()[0] == 1


def test_dry_run_changes_nothing(seeded, monkeypatch):
    from telemetrify import rocco_sync as rs
    conn = seeded["conn"]
    _cap_at(monkeypatch, conn, 0.75)
    plan = rs.archive_if_over_cap(db_path=seeded["db"], dry_run=True)
    assert plan["dry_run"] and plan["turns"] >= 3
    assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 9
    assert not list(seeded["remote"].iterdir())


def test_tick_archives_when_connected_and_records_it(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs
    monkeypatch.setattr(rs, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rs, "probe_connected", lambda: True)
    monkeypatch.setattr(rs, "active_session_seconds", lambda conn: 0)
    rs.save_state(rs.SyncState(last_push_finished_at="x", last_push_ok=True, prev_connected=True))
    moved = {"slice": "turns-a_b-3-z.db", "turns": 3, "bytes": 10, "sha256": "s",
             "archived_at": "t"}
    monkeypatch.setattr(rs, "archive_if_over_cap", lambda db_path=None: moved)
    db = tmp_path / "p.db"
    sqlite3.connect(db).execute("CREATE TABLE turns(id)").connection.close()
    res = rs.tick(db_path=db)
    assert res["archive"] == moved
    assert rs.load_state().archives == [moved]


def test_tick_offline_never_archives(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs
    monkeypatch.setattr(rs, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(rs, "probe_connected", lambda: False)
    called = []
    monkeypatch.setattr(rs, "archive_if_over_cap", lambda db_path=None: called.append(1))
    db = tmp_path / "p.db"
    sqlite3.connect(db).execute("CREATE TABLE turns(id)").connection.close()
    rs.tick(db_path=db)
    assert called == []


# ── push ─────────────────────────────────────────────────────────────────
def test_push_uses_sqlite3_rsync_without_a_local_copy(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs
    db = tmp_path / "prompts.db"
    sqlite3.connect(db).execute("CREATE TABLE turns(id)").connection.close()
    snap = tmp_path / "snap" / "prompts.db"
    monkeypatch.setattr(rs, "DB_PATH", db)
    monkeypatch.setattr(rs, "SNAPSHOT_PATH", snap)
    monkeypatch.setattr(rs, "sqlite3_rsync_path", lambda: "/opt/sqlite3_rsync")
    monkeypatch.setenv("ROCCO_STORE_DIR", "~/telemetrify-store")
    calls = []
    monkeypatch.setattr(rs, "_run", lambda cmd, timeout=None: (
        calls.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1])

    manifest = rs.push()

    sync = [c for c in calls if c[0] == "/opt/sqlite3_rsync"]
    assert sync and sync[0][1:3] == [str(db), "rocco:telemetrify-store/prompts.db"]
    assert sync[0][sync[0].index("--exe") + 1] == ".local/bin/sqlite3_rsync"
    assert sync[0][sync[0].index("--ssh") + 1].endswith("bin/rocco-ssh")
    assert manifest["method"] == "sqlite3_rsync"
    assert not snap.exists()


def test_snapshot_is_removed_even_when_the_push_fails(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs
    db = tmp_path / "prompts.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("CREATE TABLE turns(id)"); c.commit(); c.close()
    snap = tmp_path / "snap" / "prompts.db"
    # A WAL-mode leftover from an earlier failed push.
    snap.parent.mkdir()
    (Path(f"{snap}-wal")).write_bytes(b"stale")
    monkeypatch.setattr(rs, "DB_PATH", db)
    monkeypatch.setattr(rs, "SNAPSHOT_PATH", snap)
    monkeypatch.setattr(rs, "sqlite3_rsync_path", lambda: None)
    monkeypatch.setattr(rs, "_run", lambda cmd, timeout=None: subprocess.CompletedProcess(
        cmd, 0 if cmd[0] == "ssh" else 23, "", "boom"))
    with pytest.raises(rs.RoccoSyncError):
        rs.push()
    assert not any(snap.parent.glob("prompts.db*"))


def test_make_snapshot_writes_a_single_compact_file(tmp_path):
    from telemetrify import rocco_sync as rs
    db = tmp_path / "prompts.db"
    c = sqlite3.connect(db)
    c.execute("PRAGMA journal_mode=WAL"); c.execute("CREATE TABLE turns(id, t)")
    c.executemany("INSERT INTO turns VALUES (?, ?)", [(i, "x" * 500) for i in range(500)])
    c.commit()
    out = rs.make_snapshot(db_path=db, dest=tmp_path / "s" / "prompts.db")
    c.close()
    assert sqlite3.connect(out).execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 500
    assert not Path(f"{out}-wal").exists()


def test_dashboard_reports_turns_archived_on_rocco(tmp_path, monkeypatch):
    from telemetrify import rocco_sync as rs
    from telemetrify.ui import app as app_mod
    monkeypatch.setattr(rs, "STATE_PATH", tmp_path / "state.json")
    assert app_mod._archive_summary() == {"slices": 0, "turns": 0}
    rs.save_state(rs.SyncState(archives=[{"slice": "a.db", "turns": 31958},
                                         {"slice": "b.db", "turns": 42}]))
    assert app_mod._archive_summary() == {"slices": 2, "turns": 32000}


def test_slice_name_never_carries_shell_text_from_a_transcript(migrated_db):
    from telemetrify import rocco_archive as archive
    migrated_db.execute("INSERT INTO sessions(id, started_at, cwd) VALUES ('s', '$(id)', '/')")
    tid = migrated_db.execute(
        "INSERT INTO turns(session_id, user_text, assistant_text, started_at) "
        "VALUES ('s', 'q', 'a', '$(touch x)2026-01-01')").lastrowid
    migrated_db.commit()
    name = archive.slice_name(migrated_db, [tid])
    assert re.fullmatch(r"turns-[0-9A-Za-z_-]+-1-\d{8}T\d{4}Z\.db", name), name
    assert "undated" in name
