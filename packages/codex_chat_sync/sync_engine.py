"""Core inspect, sync, list, and verify engine for Codex threads and subagents."""

from __future__ import annotations

import os
import shutil
import subprocess
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import db, rollout
from .daemon import managed_daemon_pause
from .discovery import AccountIdentity, extract_target_identity, normalize_thread_id, resolve_codex_binary


@dataclass(frozen=True)
class SubagentBundle:
    row: dict[str, Any]
    active_path: Path
    chain: list[rollout.SegmentInfo]


@dataclass(frozen=True)
class ThreadBundle:
    thread_id: str
    row: dict[str, Any]
    active_path: Path
    chain: list[rollout.SegmentInfo]
    subagents: list[SubagentBundle]
    edges: list[tuple[str, str, str]]
    root_ancestor_row: dict[str, Any] | None
    history_stats: dict[str, Any]
    has_goal: bool


def _resolve_active_path(codex_home: Path, row: dict[str, Any]) -> Path:
    rp = Path(str(row["rollout_path"]))
    if rp.is_file():
        return rp
    found = rollout.find_rollout_by_segment_id(codex_home, str(row["id"]))
    if found is not None:
        return found
    raise FileNotFoundError(f"Rollout file for thread {row['id']} not found at {rp}")


def _build_bundle(codex_home: Path, thread_id: str) -> ThreadBundle:
    state_db = codex_home / "state_5.sqlite"
    if not state_db.is_file():
        raise FileNotFoundError(f"Missing state DB: {state_db}")
    row = db.get_thread_row(state_db, thread_id)
    if row is None:
        raise KeyError(f"Thread {thread_id} not found in {state_db}")

    active_path = _resolve_active_path(codex_home, row)
    chain = rollout.trace_history_chain(codex_home, active_path, thread_id)

    child_rows, edges = db.get_descendant_threads(state_db, thread_id)
    subagents: list[SubagentBundle] = []
    for crow in child_rows:
        cpath = _resolve_active_path(codex_home, crow)
        cchain = rollout.trace_history_chain(codex_home, cpath, str(crow["id"]))
        subagents.append(SubagentBundle(row=crow, active_path=cpath, chain=cchain))

    root_ancestor_row: dict[str, Any] | None = None
    root_seg = chain[-1]
    if root_seg.meta_thread_id != thread_id:
        root_ancestor_row = db.get_thread_row(state_db, root_seg.meta_thread_id)

    all_seg_ids = [s.segment_id for s in chain]
    for sb in subagents:
        all_seg_ids.extend(s.segment_id for s in sb.chain)
    history_stats = db.get_history_counts(codex_home / "thread_history_1.sqlite", all_seg_ids)

    has_goal = False
    goals_db = codex_home / "goals_1.sqlite"
    if goals_db.is_file():
        conn = db.connect_ro(goals_db)
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM thread_goals WHERE thread_id = ?", (thread_id,)).fetchone()[0]
            has_goal = cnt > 0
        finally:
            conn.close()

    return ThreadBundle(
        thread_id=thread_id,
        row=row,
        active_path=active_path,
        chain=chain,
        subagents=subagents,
        edges=edges,
        root_ancestor_row=root_ancestor_row,
        history_stats=history_stats,
        has_goal=has_goal,
    )


def inspect_thread(source_home: Path, thread_spec: str) -> dict[str, Any]:
    """Inspect a thread, its history_base lineage, and descendant subagents."""
    tid = normalize_thread_id(thread_spec)
    bundle = _build_bundle(source_home, tid)
    return {
        "thread_id": bundle.thread_id,
        "source_home": str(source_home),
        "name": bundle.row.get("name"),
        "title": bundle.row.get("title"),
        "cwd": bundle.row.get("cwd"),
        "archived": bool(bundle.row.get("archived")),
        "history_mode": bundle.row.get("history_mode"),
        "model": bundle.row.get("model"),
        "updated_at": bundle.row.get("updated_at"),
        "active_rollout": str(bundle.active_path),
        "segments": [
            {
                "segment_id": s.segment_id,
                "meta_thread_id": s.meta_thread_id,
                "path": str(s.path),
                "compressed": s.compressed,
                "history_base_id": s.history_base_id,
                "cwd": s.cwd,
            }
            for s in bundle.chain
        ],
        "subagents": [
            {
                "thread_id": sb.row["id"],
                "nickname": sb.row.get("agent_nickname"),
                "role": sb.row.get("agent_role"),
                "agent_path": sb.row.get("agent_path"),
                "rollout": str(sb.active_path),
                "segments": len(sb.chain),
            }
            for sb in bundle.subagents
        ],
        "edges": [{"parent": p, "child": c, "status": st} for p, c, st in bundle.edges],
        "root_ancestor_thread_id": bundle.root_ancestor_row["id"] if bundle.root_ancestor_row else None,
        "history_stats": {
            "turns": bundle.history_stats["turns"],
            "items": bundle.history_stats["items"],
            "projected_segments": bundle.history_stats["projected_segments"],
        },
        "has_goal": bundle.has_goal,
    }


def list_threads(
    source_home: Path,
    *,
    cwd: str | None = None,
    query: str | None = None,
    include_archived: bool = False,
    include_subagents: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    state_db = source_home / "state_5.sqlite"
    if not state_db.is_file():
        raise FileNotFoundError(f"Missing state DB: {state_db}")
    return db.list_threads(
        state_db,
        cwd=cwd,
        query=query,
        include_archived=include_archived,
        include_subagents=include_subagents,
        limit=limit,
    )


def _target_active_rollout_path(src_home: Path, dst_home: Path, src_path: Path, thread_row: dict[str, Any]) -> Path:
    try:
        rel = src_path.resolve().relative_to(src_home.resolve())
        if rel.parts and rel.parts[0] == "sessions":
            return dst_home / rel
    except ValueError:
        pass
    ts = int(thread_row.get("created_at") or datetime.now(timezone.utc).timestamp())
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    sub = Path("archived_sessions") if thread_row.get("archived") else Path("sessions") / f"{dt:%Y/%m/%d}"
    return dst_home / sub / src_path.name


def _create_backup(dst_home: Path) -> Path:
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = xdg_data / "codex-chat-sync" / "sync-backups" / stamp
    counter = 1
    while backup_dir.exists():
        backup_dir = xdg_data / "codex-chat-sync" / "sync-backups" / f"{stamp}-{counter}"
        counter += 1
    backup_dir.mkdir(parents=True, exist_ok=True)

    for rel in ("state_5.sqlite", "thread_history_1.sqlite", "goals_1.sqlite", "sqlite/codex.db"):
        p = dst_home / rel
        if p.is_file():
            db.backup_sqlite(p, backup_dir / rel)
    idx = dst_home / "session_index.jsonl"
    if idx.is_file():
        shutil.copy2(idx, backup_dir / "session_index.jsonl")
    return backup_dir


def _prepare_thread_record(
    row: dict[str, Any],
    *,
    dst_rollout_path: Path,
    effective_cwd: str,
    identity: AccountIdentity,
    target_is_cli: bool,
    archived_override: int | None = None,
) -> dict[str, Any]:
    rec = dict(row)
    rec["rollout_path"] = str(dst_rollout_path)
    rec["cwd"] = effective_cwd
    if identity.account_id:
        rec["creator_account_id"] = identity.account_id
    if identity.user_id:
        rec["creator_user_id"] = identity.user_id
    # Avoid cross-store foreign key failures on projects / thread_sections
    rec["project_id"] = None
    rec["thread_section_id"] = None
    rec["section_position"] = None
    rec["section_entered_at_ms"] = None
    if target_is_cli:
        rec["daybreak_enabled"] = None
    if archived_override is not None:
        rec["archived"] = archived_override
        if archived_override == 1 and not rec.get("archived_at"):
            rec["archived_at"] = rec.get("updated_at")
    return rec


def _looks_like_cli_store(codex_home: Path) -> bool:
    if codex_home.name == ".codex-cli":
        return True
    return not (codex_home / "sqlite" / "codex.db").exists()


def sync_threads(
    source_home: Path,
    target_home: Path,
    thread_specs: list[str],
    *,
    with_subagents: bool = True,
    cwd_override: str | None = None,
    rewrite_session_cwd: bool = True,
    on_existing: str = "if-newer",
    include_goals: bool = True,
    goal_status: str | None = None,
    manage_daemon: bool = True,
    force: bool = False,
    dry_run: bool = False,
    backup: bool = True,
) -> dict[str, Any]:
    """Sync one or more threads (and their subagents & history chains) from source_home to target_home."""
    src_home = source_home.expanduser().resolve()
    dst_home = target_home.expanduser().resolve()
    if src_home == dst_home:
        raise ValueError(f"Source and target CODEX_HOME are identical: {src_home}")
    if not (src_home / "state_5.sqlite").is_file():
        raise FileNotFoundError(f"Source state_5.sqlite not found in {src_home}")
    if on_existing not in {"skip", "if-newer", "overwrite", "error"}:
        raise ValueError(f"Invalid on_existing policy: {on_existing}")

    requested_ids = []
    for spec in thread_specs:
        tid = normalize_thread_id(spec)
        if tid not in requested_ids:
            requested_ids.append(tid)

    bundles = [_build_bundle(src_home, tid) for tid in requested_ids]
    identity = extract_target_identity(dst_home)
    target_is_cli = _looks_like_cli_store(dst_home)
    dst_state_db = dst_home / "state_5.sqlite"

    # Evaluate existing threads policy
    actions: list[dict[str, Any]] = []
    active_bundles: list[ThreadBundle] = []
    for bundle in bundles:
        existing_row = db.get_thread_row(dst_state_db, bundle.thread_id) if dst_state_db.is_file() else None
        status = "create"
        if existing_row is not None:
            src_ms = int(bundle.row.get("updated_at_ms") or (bundle.row.get("updated_at") or 0) * 1000)
            dst_ms = int(existing_row.get("updated_at_ms") or (existing_row.get("updated_at") or 0) * 1000)
            if on_existing == "error":
                raise FileExistsError(f"Thread {bundle.thread_id} already exists in {dst_home}")
            if on_existing == "skip":
                status = "skip"
            elif on_existing == "if-newer" and src_ms <= dst_ms:
                status = "skip_up_to_date"
            else:
                status = "overwrite"
        actions.append(
            {
                "thread_id": bundle.thread_id,
                "name": bundle.row.get("name") or bundle.row.get("title"),
                "action": status,
                "subagents": len(bundle.subagents) if with_subagents else 0,
                "segments": len(bundle.chain),
            }
        )
        if not status.startswith("skip"):
            active_bundles.append(bundle)

    if dry_run or not active_bundles:
        return {
            "dry_run": dry_run,
            "source_home": str(src_home),
            "target_home": str(dst_home),
            "target_account_id": identity.account_id,
            "target_user_id": identity.user_id,
            "actions": actions,
            "copied_files": [],
            "backup_dir": None,
        }

    dst_home.mkdir(parents=True, exist_ok=True)
    db.ensure_db_initialized(src_home / "state_5.sqlite", dst_state_db)

    copied_files: list[str] = []
    backup_dir: Path | None = None

    with managed_daemon_pause(dst_home, manage_daemon=manage_daemon, force=force) as daemon_info:
        if backup:
            backup_dir = _create_backup(dst_home)

        segment_deltas: dict[str, int] = {}
        thread_records: list[dict[str, Any]] = []
        all_edges: list[tuple[str, str, str]] = []
        aux_thread_ids: list[str] = []
        goal_thread_ids: list[str] = []
        main_thread_records: list[dict[str, Any]] = []

        arch_dir = dst_home / "archived_sessions"
        arch_dir.mkdir(parents=True, exist_ok=True)

        for bundle in active_bundles:
            effective_cwd = cwd_override or str(bundle.row.get("cwd") or Path.home())
            session_cwd = str(bundle.chain[0].cwd or "")
            should_rewrite_cwd = bool(
                cwd_override or (rewrite_session_cwd and session_cwd != effective_cwd)
            )

            # 1. Active rollout of main thread
            dst_active = _target_active_rollout_path(src_home, dst_home, bundle.active_path, bundle.row)
            rw = rollout.rewrite_active_rollout(
                bundle.active_path,
                dst_active,
                account_id=identity.account_id,
                user_id=identity.user_id,
                cwd=effective_cwd,
                rewrite_cwd=should_rewrite_cwd,
                account_scoped=True,
            )
            segment_deltas[bundle.chain[0].segment_id] = rw.delta_bytes
            copied_files.append(str(dst_active))

            main_rec = _prepare_thread_record(
                bundle.row,
                dst_rollout_path=dst_active,
                effective_cwd=effective_cwd,
                identity=identity,
                target_is_cli=target_is_cli,
            )
            thread_records.append(main_rec)
            main_thread_records.append(main_rec)
            aux_thread_ids.append(bundle.thread_id)
            goal_thread_ids.append(bundle.thread_id)

            # 2. Ancestor history_base segments into archived_sessions/ (unmodified bytes)
            for seg in bundle.chain[1:]:
                segment_deltas.setdefault(seg.segment_id, 0)
                existing_in_dst = rollout.find_rollout_by_segment_id(dst_home, seg.segment_id)
                if existing_in_dst is not None and existing_in_dst.parent != arch_dir:
                    # Segment is already present in dst sessions/ (e.g. parent synced standalone)
                    continue
                dst_seg = arch_dir / seg.path.name
                if not dst_seg.exists() or dst_seg.stat().st_size != seg.path.stat().st_size:
                    shutil.copy2(seg.path, dst_seg)
                    copied_files.append(str(dst_seg))

            # Register archived root ancestor thread if it doesn't already exist in target
            if bundle.root_ancestor_row is not None:
                anc_id = str(bundle.root_ancestor_row["id"])
                if anc_id not in requested_ids and db.get_thread_row(dst_state_db, anc_id) is None:
                    anc_seg = next(
                        (s for s in bundle.chain[1:] if s.meta_thread_id == anc_id),
                        bundle.chain[-1],
                    )
                    anc_existing = rollout.find_rollout_by_segment_id(dst_home, anc_seg.segment_id)
                    anc_path = anc_existing or (arch_dir / anc_seg.path.name)
                    anc_rec = _prepare_thread_record(
                        bundle.root_ancestor_row,
                        dst_rollout_path=anc_path,
                        effective_cwd=effective_cwd,
                        identity=identity,
                        target_is_cli=target_is_cli,
                        archived_override=1,
                    )
                    thread_records.append(anc_rec)

            # 3. Subagent descendants (recursive)
            if with_subagents:
                all_edges.extend(bundle.edges)
                for sb in bundle.subagents:
                    cid = str(sb.row["id"])
                    raw_sub_cwd = str(sb.row.get("cwd") or effective_cwd)
                    try:
                        same_resolved = Path(raw_sub_cwd).resolve() == Path(effective_cwd).resolve()
                    except OSError:
                        same_resolved = False
                    sub_cwd = cwd_override or (effective_cwd if same_resolved else raw_sub_cwd)
                    sub_session_cwd = str(sb.chain[0].cwd or "")
                    sub_rewrite_cwd = bool(
                        cwd_override or (rewrite_session_cwd and sub_session_cwd != sub_cwd)
                    )
                    dst_sub_active = _target_active_rollout_path(
                        src_home, dst_home, sb.active_path, sb.row
                    )
                    sub_rw = rollout.rewrite_active_rollout(
                        sb.active_path,
                        dst_sub_active,
                        account_id=identity.account_id,
                        user_id=identity.user_id,
                        cwd=sub_cwd,
                        rewrite_cwd=sub_rewrite_cwd,
                        account_scoped=True,
                    )
                    segment_deltas[sb.chain[0].segment_id] = sub_rw.delta_bytes
                    copied_files.append(str(dst_sub_active))

                    for seg in sb.chain[1:]:
                        segment_deltas.setdefault(seg.segment_id, 0)
                        dst_seg = arch_dir / seg.path.name
                        if not dst_seg.exists() or dst_seg.stat().st_size != seg.path.stat().st_size:
                            shutil.copy2(seg.path, dst_seg)
                            copied_files.append(str(dst_seg))

                    thread_records.append(
                        _prepare_thread_record(
                            sb.row,
                            dst_rollout_path=dst_sub_active,
                            effective_cwd=sub_cwd,
                            identity=identity,
                            target_is_cli=target_is_cli,
                        )
                    )
                    aux_thread_ids.append(cid)
                    goal_thread_ids.append(cid)

        # 4. Commit SQLite and session_index updates
        db.upsert_threads_and_edges(src_home / "state_5.sqlite", dst_state_db, thread_records, all_edges)
        aux_counts = db.copy_auxiliary_tables(src_home / "state_5.sqlite", dst_state_db, aux_thread_ids)
        history_counts = db.copy_thread_history(
            src_home / "thread_history_1.sqlite",
            dst_home / "thread_history_1.sqlite",
            segment_deltas,
        )
        goals_copied = 0
        if include_goals:
            goals_copied = db.copy_thread_goals(
                src_home / "goals_1.sqlite",
                dst_home / "goals_1.sqlite",
                goal_thread_ids,
                goal_status_override=goal_status,
            )

        catalog_updated = 0
        codex_db = dst_home / "sqlite" / "codex.db"
        for mrec in main_thread_records:
            tname = str(mrec.get("name") or mrec.get("title") or "Untitled thread")
            db.upsert_session_index(
                dst_home / "session_index.jsonl",
                str(mrec["id"]),
                tname,
                updated_at_ms=mrec.get("updated_at_ms"),
                updated_at_s=mrec.get("updated_at"),
            )
            if codex_db.is_file() and db.upsert_desktop_catalog(codex_db, mrec):
                catalog_updated += 1

        integrity: dict[str, str] = {}
        for rel in ("state_5.sqlite", "thread_history_1.sqlite", "goals_1.sqlite", "sqlite/codex.db"):
            p = dst_home / rel
            if p.is_file():
                ok, msg = db.integrity_check(p)
                integrity[rel] = msg
                if not ok:
                    raise RuntimeError(f"SQLite integrity check failed for {p}: {msg}")

    return {
        "dry_run": False,
        "source_home": str(src_home),
        "target_home": str(dst_home),
        "target_account_id": identity.account_id,
        "target_user_id": identity.user_id,
        "actions": actions,
        "copied_files_count": len(copied_files),
        "copied_files": copied_files,
        "aux_counts": aux_counts,
        "history_counts": history_counts,
        "goals_copied": goals_copied,
        "desktop_catalog_updated": catalog_updated,
        "daemon": daemon_info,
        "integrity": integrity,
        "backup_dir": str(backup_dir) if backup_dir else None,
    }


def _verify_via_app_server(codex_home: Path, bundle: ThreadBundle) -> dict[str, Any]:
    codex_bin = resolve_codex_binary()
    if not codex_bin:
        raise FileNotFoundError("Could not locate `codex` binary for app-server verification.")

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    proc = subprocess.Popen(
        [codex_bin, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stdin is not None and proc.stdout is not None

    def rpc(rid: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server EOF during verification")
            obj = json.loads(line)
            if obj.get("id") == rid:
                return obj

    try:
        rpc(1, "initialize", {"clientInfo": {"name": "codex-chat-sync-verify", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()

        r_list = rpc(
            2,
            "thread/list",
            {
                "cwd": bundle.row.get("cwd"),
                "currentAccountOnly": True,
                "useStateDbOnly": False,
                "archived": bool(bundle.row.get("archived")),
                "limit": 100,
            },
        )
        listed_ids = [x["id"] for x in r_list.get("result", {}).get("data", [])]

        r_kids = rpc(
            3,
            "thread/list",
            {
                "ancestorThreadId": bundle.thread_id,
                "sourceKinds": ["subAgentThreadSpawn"],
                "limit": 100,
            },
        )
        child_ids = [x["id"] for x in r_kids.get("result", {}).get("data", [])]

        cur = None
        paged_turns = 0
        req_id = 10
        while True:
            rt = rpc(
                req_id,
                "thread/turns/list",
                {
                    "threadId": bundle.thread_id,
                    "cursor": cur,
                    "limit": 100,
                    "itemsView": "summary",
                    "sortDirection": "desc",
                },
            )
            req_id += 1
            if "error" in rt:
                raise RuntimeError(f"thread/turns/list error: {rt['error']}")
            res = rt["result"]
            paged_turns += len(res.get("data", []))
            cur = res.get("nextCursor")
            if not cur:
                break

        return {
            "listed_under_current_account": bundle.thread_id in listed_ids,
            "listed_children_count": len(child_ids),
            "paged_turns": paged_turns,
        }
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def verify_thread(
    target_home: Path,
    thread_spec: str,
    *,
    check_app_server: bool = False,
) -> dict[str, Any]:
    """Verify thread files, history_base chain, SQLite integrity, and byte offsets."""
    dst_home = target_home.expanduser().resolve()
    tid = normalize_thread_id(thread_spec)

    integrity: dict[str, str] = {}
    for rel in ("state_5.sqlite", "thread_history_1.sqlite", "goals_1.sqlite"):
        p = dst_home / rel
        if p.is_file():
            ok, msg = db.integrity_check(p)
            integrity[rel] = msg
            if not ok:
                raise RuntimeError(f"Integrity check failed for {p}: {msg}")

    bundle = _build_bundle(dst_home, tid)

    # Check active rollout projection byte offset alignment (if uncompressed & projected)
    active_seg_id = bundle.chain[0].segment_id
    proj = bundle.history_stats["projections"].get(active_seg_id)
    byte_offset_match: bool | None = None
    if proj is not None and not bundle.active_path.name.endswith(".zst"):
        byte_offset_match = bundle.active_path.stat().st_size == proj["next_rollout_byte_offset"]
        if not byte_offset_match:
            raise RuntimeError(
                f"Projection byte offset mismatch for {active_seg_id}: "
                f"file={bundle.active_path.stat().st_size}, proj={proj['next_rollout_byte_offset']}"
            )

    app_server_info: dict[str, Any] | None = None
    if check_app_server:
        app_server_info = _verify_via_app_server(dst_home, bundle)
        if not app_server_info["listed_under_current_account"]:
            raise RuntimeError(
                f"Thread {tid} did not appear in app-server thread/list(currentAccountOnly=True)"
            )

    return {
        "ok": True,
        "target_home": str(dst_home),
        "thread_id": tid,
        "cwd": bundle.row.get("cwd"),
        "segments_verified": len(bundle.chain),
        "subagents_verified": len(bundle.subagents),
        "turns_in_db": bundle.history_stats["turns"],
        "items_in_db": bundle.history_stats["items"],
        "byte_offset_match": byte_offset_match,
        "integrity": integrity,
        "app_server": app_server_info,
    }
