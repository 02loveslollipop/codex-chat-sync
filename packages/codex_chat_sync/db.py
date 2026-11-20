"""SQLite and session_index.jsonl helpers for Codex thread synchronization."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def connect_rw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def backup_sqlite(src_db: Path, dst_db: Path) -> None:
    """Create a consistent backup of a WAL-mode SQLite database."""
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = connect_ro(src_db)
    dst = sqlite3.connect(str(dst_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def ensure_db_initialized(src_db: Path, dst_db: Path) -> None:
    """If dst_db does not exist yet, initialize its schema (empty tables) from src_db."""
    if dst_db.is_file() or not src_db.is_file():
        return
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    src = connect_ro(src_db)
    dst = connect_rw(dst_db)
    try:
        src.backup(dst)
        # Keep schema + _sqlx_migrations, clear thread data tables
        tables = [
            r[0]
            for r in dst.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        dst.execute("PRAGMA foreign_keys = OFF")
        for tbl in tables:
            if tbl in {"_sqlx_migrations", "backfill_state", "codex_schema_migrations"}:
                continue
            dst.execute(f'DELETE FROM "{tbl}"')
        dst.commit()
    finally:
        dst.close()
        src.close()


def integrity_check(db_path: Path) -> tuple[bool, str]:
    """Run SQLite PRAGMA integrity_check on db_path."""
    if not db_path.is_file():
        return False, "missing"
    conn = connect_ro(db_path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        val = str(row[0]) if row else "unknown"
        return val == "ok", val
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _common_columns(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> list[str]:
    src_cols = _table_columns(src, table)
    dst_set = set(_table_columns(dst, table))
    return [c for c in src_cols if c in dst_set]


def get_thread_row(state_db: Path, thread_id: str) -> dict[str, Any] | None:
    conn = connect_ro(state_db)
    try:
        row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_threads(
    state_db: Path,
    *,
    cwd: str | None = None,
    query: str | None = None,
    include_archived: bool = False,
    include_subagents: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query threads from state_5.sqlite with child subagent counts."""
    conn = connect_ro(state_db)
    try:
        clauses = ["1 = 1"]
        params: list[Any] = []
        if not include_archived:
            clauses.append("t.archived = 0")
        if not include_subagents:
            clauses.append("t.source NOT LIKE '{\"subagent\"%'")
        if cwd:
            clauses.append("t.cwd = ?")
            params.append(cwd)
        if query:
            clauses.append("(LOWER(COALESCE(t.name, '')) LIKE ? OR LOWER(t.title) LIKE ? OR LOWER(t.id) LIKE ?)")
            pat = f"%{query.lower()}%"
            params.extend([pat, pat, pat])

        sql = f"""
            SELECT
                t.*,
                (SELECT COUNT(*) FROM thread_spawn_edges e WHERE e.parent_thread_id = t.id) AS direct_subagents
            FROM threads t
            WHERE {' AND '.join(clauses)}
            ORDER BY t.updated_at DESC, t.id DESC
            LIMIT ?
        """
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_descendant_threads(
    state_db: Path, root_thread_id: str
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Return all descendant subagent thread rows and thread_spawn_edges via BFS."""
    conn = connect_ro(state_db)
    try:
        seen: set[str] = {root_thread_id}
        queue: deque[str] = deque([root_thread_id])
        edges: list[tuple[str, str, str]] = []
        child_rows: list[dict[str, Any]] = []

        while queue:
            parent_id = queue.popleft()
            found = conn.execute(
                "SELECT parent_thread_id, child_thread_id, status FROM thread_spawn_edges WHERE parent_thread_id = ?",
                (parent_id,),
            ).fetchall()
            for p_id, c_id, status in found:
                edges.append((str(p_id), str(c_id), str(status)))
                if c_id not in seen:
                    seen.add(c_id)
                    row = conn.execute("SELECT * FROM threads WHERE id = ?", (c_id,)).fetchone()
                    if row is not None:
                        child_rows.append(dict(row))
                    queue.append(c_id)

        return child_rows, edges
    finally:
        conn.close()


def upsert_threads_and_edges(
    src_state_db: Path,
    dst_state_db: Path,
    thread_records: list[dict[str, Any]],
    edges: list[tuple[str, str, str]],
) -> None:
    """Upsert prepared thread rows and spawn edges into destination state_5.sqlite."""
    src = connect_ro(src_state_db)
    dst = connect_rw(dst_state_db)
    try:
        cols = _common_columns(src, dst, "threads")
        col_list = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["?"] * len(cols))
        dst.execute("BEGIN")
        for rec in thread_records:
            vals = [rec.get(c) for c in cols]
            dst.execute(f"INSERT OR REPLACE INTO threads ({col_list}) VALUES ({placeholders})", vals)
        for parent_id, child_id, status in edges:
            dst.execute(
                "INSERT OR REPLACE INTO thread_spawn_edges (parent_thread_id, child_thread_id, status) "
                "VALUES (?, ?, ?)",
                (parent_id, child_id, status),
            )
        dst.commit()
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()


def copy_auxiliary_tables(
    src_state_db: Path, dst_state_db: Path, thread_ids: list[str]
) -> dict[str, int]:
    """Copy thread_dynamic_tools, thread_attachments, thread_read_receipts, guardian_review_feedback."""
    if not thread_ids:
        return {}
    aux_tables = (
        "thread_dynamic_tools",
        "thread_attachments",
        "thread_read_receipts",
        "guardian_review_feedback",
    )
    src = connect_ro(src_state_db)
    dst = connect_rw(dst_state_db)
    counts: dict[str, int] = {}
    ph = ", ".join(["?"] * len(thread_ids))
    try:
        dst_tables = {
            r[0]
            for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        src_tables = {
            r[0]
            for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        dst.execute("BEGIN")
        for tbl in aux_tables:
            if tbl not in src_tables or tbl not in dst_tables:
                continue
            cols = _common_columns(src, dst, tbl)
            if not cols or "thread_id" not in cols:
                continue
            dst.execute(f'DELETE FROM "{tbl}" WHERE thread_id IN ({ph})', thread_ids)
            col_list = ", ".join(f'"{c}"' for c in cols)
            qm = ", ".join(["?"] * len(cols))
            rows = src.execute(
                f'SELECT {col_list} FROM "{tbl}" WHERE thread_id IN ({ph})', thread_ids
            ).fetchall()
            if rows:
                dst.executemany(
                    f'INSERT INTO "{tbl}" ({col_list}) VALUES ({qm})',
                    [tuple(r) for r in rows],
                )
            counts[tbl] = len(rows)
        dst.commit()
        return counts
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()


def get_history_counts(th_db: Path, segment_ids: list[str]) -> dict[str, Any]:
    """Return turn/item counts and projection state summary for segment_ids."""
    if not th_db.is_file() or not segment_ids:
        return {"turns": 0, "items": 0, "projected_segments": 0, "projections": {}}
    conn = connect_ro(th_db)
    ph = ", ".join(["?"] * len(segment_ids))
    try:
        turns = conn.execute(
            f"SELECT COUNT(*) FROM thread_turns WHERE thread_id IN ({ph})", segment_ids
        ).fetchone()[0]
        items = conn.execute(
            f"SELECT COUNT(*) FROM thread_items WHERE thread_id IN ({ph})", segment_ids
        ).fetchone()[0]
        projs = {
            r[0]: {"next_rollout_byte_offset": r[1], "next_rollout_ordinal": r[2]}
            for r in conn.execute(
                f"SELECT thread_id, next_rollout_byte_offset, next_rollout_ordinal "
                f"FROM thread_history_projection_state WHERE thread_id IN ({ph})",
                segment_ids,
            ).fetchall()
        }
        return {
            "turns": turns,
            "items": items,
            "projected_segments": len(projs),
            "projections": projs,
        }
    finally:
        conn.close()


def copy_thread_history(
    src_th_db: Path,
    dst_th_db: Path,
    segment_deltas: dict[str, int],
) -> dict[str, int]:
    """Copy thread_history_1.sqlite rows for segment_ids, applying byte-offset deltas."""
    if not src_th_db.is_file() or not segment_deltas:
        return {"thread_turns": 0, "thread_items": 0, "thread_history_projection_state": 0}
    ensure_db_initialized(src_th_db, dst_th_db)
    segment_ids = list(segment_deltas.keys())
    ph = ", ".join(["?"] * len(segment_ids))

    src = connect_ro(src_th_db)
    dst = connect_rw(dst_th_db)
    counts = {
        "thread_turns": 0,
        "thread_items": 0,
        "thread_history_projection_state": 0,
        "thread_realtime_items": 0,
    }
    try:
        dst.execute("BEGIN")
        # Delete projection_state first because cleanup trigger deletes thread_realtime_items
        for tbl in (
            "thread_history_projection_state",
            "thread_realtime_items",
            "thread_items",
            "thread_turns",
        ):
            dst.execute(f'DELETE FROM "{tbl}" WHERE thread_id IN ({ph})', segment_ids)

        # thread_turns with byte offset deltas
        tcols = _common_columns(src, dst, "thread_turns")
        t_tid_idx = tcols.index("thread_id")
        t_start_idx = tcols.index("rollout_byte_offset") if "rollout_byte_offset" in tcols else None
        t_end_idx = (
            tcols.index("rollout_end_byte_offset") if "rollout_end_byte_offset" in tcols else None
        )
        t_list = ", ".join(f'"{c}"' for c in tcols)
        t_qm = ", ".join(["?"] * len(tcols))
        t_rows = src.execute(
            f"SELECT {t_list} FROM thread_turns WHERE thread_id IN ({ph})", segment_ids
        ).fetchall()
        adj_turns = []
        for r in t_rows:
            vals = list(r)
            delta = segment_deltas.get(str(vals[t_tid_idx]), 0)
            if delta:
                if t_start_idx is not None and vals[t_start_idx] is not None:
                    vals[t_start_idx] += delta
                if t_end_idx is not None and vals[t_end_idx] is not None:
                    vals[t_end_idx] += delta
            adj_turns.append(tuple(vals))
        if adj_turns:
            dst.executemany(f"INSERT INTO thread_turns ({t_list}) VALUES ({t_qm})", adj_turns)
        counts["thread_turns"] = len(adj_turns)

        # thread_items & thread_realtime_items
        for tbl in ("thread_items", "thread_realtime_items"):
            cols = _common_columns(src, dst, tbl)
            if not cols:
                continue
            cl = ", ".join(f'"{c}"' for c in cols)
            qm = ", ".join(["?"] * len(cols))
            rows = src.execute(
                f'SELECT {cl} FROM "{tbl}" WHERE thread_id IN ({ph})', segment_ids
            ).fetchall()
            if rows:
                dst.executemany(
                    f'INSERT INTO "{tbl}" ({cl}) VALUES ({qm})', [tuple(r) for r in rows]
                )
            counts[tbl] = len(rows)

        # thread_history_projection_state with byte offset delta
        pcols = _common_columns(src, dst, "thread_history_projection_state")
        p_tid_idx = pcols.index("thread_id")
        p_off_idx = pcols.index("next_rollout_byte_offset")
        p_list = ", ".join(f'"{c}"' for c in pcols)
        p_qm = ", ".join(["?"] * len(pcols))
        p_rows = src.execute(
            f"SELECT {p_list} FROM thread_history_projection_state WHERE thread_id IN ({ph})",
            segment_ids,
        ).fetchall()
        adj_proj = []
        for r in p_rows:
            vals = list(r)
            delta = segment_deltas.get(str(vals[p_tid_idx]), 0)
            if delta and vals[p_off_idx] is not None:
                vals[p_off_idx] += delta
            adj_proj.append(tuple(vals))
        if adj_proj:
            dst.executemany(
                f"INSERT INTO thread_history_projection_state ({p_list}) VALUES ({p_qm})",
                adj_proj,
            )
        counts["thread_history_projection_state"] = len(adj_proj)

        dst.commit()
        return counts
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()


def copy_thread_goals(
    src_goals_db: Path,
    dst_goals_db: Path,
    thread_ids: list[str],
    *,
    goal_status_override: str | None = None,
) -> int:
    """Copy thread_goals and deferral rows for synced threads."""
    if not src_goals_db.is_file() or not thread_ids:
        return 0
    ensure_db_initialized(src_goals_db, dst_goals_db)
    ph = ", ".join(["?"] * len(thread_ids))
    src = connect_ro(src_goals_db)
    dst = connect_rw(dst_goals_db)
    try:
        gcols = _common_columns(src, dst, "thread_goals")
        if not gcols:
            return 0
        g_status_idx = gcols.index("status") if "status" in gcols else None
        g_list = ", ".join(f'"{c}"' for c in gcols)
        g_qm = ", ".join(["?"] * len(gcols))
        rows = src.execute(
            f"SELECT {g_list} FROM thread_goals WHERE thread_id IN ({ph})", thread_ids
        ).fetchall()

        dst.execute("BEGIN")
        copied = 0
        for r in rows:
            vals = list(r)
            if goal_status_override and g_status_idx is not None:
                vals[g_status_idx] = goal_status_override
            dst.execute(f"INSERT OR REPLACE INTO thread_goals ({g_list}) VALUES ({g_qm})", vals)
            copied += 1

        dcols = _common_columns(src, dst, "thread_goal_continuation_deferrals")
        if dcols:
            d_list = ", ".join(f'"{c}"' for c in dcols)
            d_qm = ", ".join(["?"] * len(dcols))
            d_rows = src.execute(
                f"SELECT {d_list} FROM thread_goal_continuation_deferrals WHERE thread_id IN ({ph})",
                thread_ids,
            ).fetchall()
            for r in d_rows:
                dst.execute(
                    f"INSERT OR REPLACE INTO thread_goal_continuation_deferrals ({d_list}) VALUES ({d_qm})",
                    tuple(r),
                )
        dst.commit()
        return copied
    except Exception:
        dst.rollback()
        raise
    finally:
        dst.close()
        src.close()


def upsert_desktop_catalog(codex_db: Path, thread_row: dict[str, Any]) -> bool:
    """Upsert a top-level thread into ~/.codex/sqlite/codex.db local_thread_catalog if present."""
    if not codex_db.is_file():
        return False
    conn = connect_rw(codex_db)
    try:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "local_thread_catalog" not in tables:
            return False

        conn.execute("BEGIN")
        sync_row = conn.execute(
            "SELECT observation_sequence FROM local_thread_catalog_sync_state WHERE host_id = 'local'"
        ).fetchone()
        obs_seq = int(sync_row[0]) + 1 if sync_row else 1

        display_title = thread_row.get("name") or thread_row.get("title") or "Untitled thread"
        source_created = float(thread_row.get("created_at") or 0.0)
        source_updated = float(thread_row.get("updated_at") or source_created)
        source_recency = float(thread_row.get("recency_at") or source_updated)
        source_kind = str(thread_row.get("source") or "cli")
        if source_kind not in {"cli", "vscode", "exec", "appServer"}:
            source_kind = "cli"

        conn.execute(
            """
            INSERT INTO local_thread_catalog (
                host_id, thread_id, display_title, source_created_at, source_updated_at,
                cwd, source_kind, source_detail, model_provider, git_branch,
                observation_sequence, missing_candidate, thread_source, source_recency_at,
                pending_observed_title, project_id, conversation_origin,
                trial_conversation_type, chatgpt_async_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, ?, ?, 0, NULL, NULL, NULL, NULL)
            ON CONFLICT(host_id, thread_id) DO UPDATE SET
                display_title = excluded.display_title,
                source_updated_at = excluded.source_updated_at,
                cwd = excluded.cwd,
                source_kind = excluded.source_kind,
                model_provider = excluded.model_provider,
                git_branch = excluded.git_branch,
                observation_sequence = excluded.observation_sequence,
                missing_candidate = 0,
                thread_source = excluded.thread_source,
                source_recency_at = excluded.source_recency_at
            """,
            (
                "local",
                thread_row["id"],
                display_title,
                source_created,
                source_updated,
                thread_row.get("cwd"),
                source_kind,
                thread_row.get("model_provider") or "openai",
                thread_row.get("git_branch"),
                obs_seq,
                thread_row.get("thread_source") or "user",
                source_recency,
            ),
        )
        if "local_thread_catalog_sync_state" in tables:
            conn.execute(
                "UPDATE local_thread_catalog_sync_state SET observation_sequence = ? WHERE host_id = 'local'",
                (obs_seq,),
            )
        if "local_thread_catalog_metadata" in tables:
            conn.execute(
                "UPDATE local_thread_catalog_metadata SET catalog_revision = catalog_revision + 1 WHERE id = 1"
            )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _epoch_ms_to_iso_z(epoch_ms: int | None, epoch_s: int | None) -> str:
    ts = (epoch_ms / 1000.0) if epoch_ms else float(epoch_s or 0)
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def upsert_session_index(
    session_index_path: Path,
    thread_id: str,
    thread_name: str,
    *,
    updated_at_ms: int | None = None,
    updated_at_s: int | None = None,
) -> None:
    """Idempotently update or append a thread entry in session_index.jsonl."""
    entry = json.dumps(
        {
            "id": thread_id,
            "thread_name": thread_name,
            "updated_at": _epoch_ms_to_iso_z(updated_at_ms, updated_at_s),
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    lines: list[str] = []
    replaced = False
    if session_index_path.is_file():
        for raw in session_index_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                parsed = json.loads(raw)
                if parsed.get("id") == thread_id:
                    if not replaced:
                        lines.append(entry)
                        replaced = True
                    continue
            except json.JSONDecodeError:
                pass
            lines.append(raw)
    if not replaced:
        lines.append(entry)

    session_index_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".session_index.", dir=session_index_path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, session_index_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
