"""CLI entrypoint for codex-chat-sync."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, sync_engine
from .discovery import resolve_store


def _repo_manager():
    """Import installer.manager from the staged or repo root."""
    pkg_dir = Path(__file__).resolve().parent
    root_dir = pkg_dir.parent.parent
    installer_dir = root_dir / "installer"
    if str(installer_dir) not in sys.path:
        sys.path.insert(0, str(installer_dir))
    import manager  # type: ignore

    return manager, root_dir


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-chat-sync",
        description="Sync individual Codex threads (with history chains and subagents) between Codex App and Codex CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="List threads in a Codex store")
    p_list.add_argument("--source", default="app", help="Source store: app, cli, or path (default: app)")
    p_list.add_argument("--cwd", help="Filter by exact working directory")
    p_list.add_argument("--query", "-q", help="Substring filter across id, name, and title")
    p_list.add_argument("--include-archived", action="store_true")
    p_list.add_argument("--include-subagents", action="store_true")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.add_argument("--json", action="store_true")

    # inspect
    p_insp = sub.add_parser("inspect", help="Inspect a thread's lineage, segments, and subagents")
    p_insp.add_argument("thread", help="Thread UUID or codex://threads/<uuid>")
    p_insp.add_argument("--source", default="app", help="Source store: app, cli, or path (default: app)")
    p_insp.add_argument("--json", action="store_true")

    # sync
    p_sync = sub.add_parser("sync", help="Copy one or more threads and their subagents to another store")
    p_sync.add_argument("threads", nargs="+", help="Thread UUID(s) or codex://threads/<uuid>")
    p_sync.add_argument("--from", dest="source", default="app", help="Source store: app, cli, or path (default: app)")
    p_sync.add_argument("--to", dest="target", default="cli", help="Target store: app, cli, or path (default: cli)")
    p_sync.add_argument("--cwd", help="Override target cwd and session_meta.cwd")
    p_sync.add_argument("--no-subagents", action="store_true", help="Do not include child subagent threads")
    p_sync.add_argument("--no-rewrite-session-cwd", action="store_true", help="Keep original session_meta.cwd")
    p_sync.add_argument(
        "--on-existing",
        choices=["skip", "if-newer", "overwrite", "error"],
        default="if-newer",
        help="Behavior if thread already exists in target (default: if-newer)",
    )
    p_sync.add_argument("--overwrite", action="store_true", help="Shortcut for --on-existing overwrite")
    p_sync.add_argument("--no-goals", action="store_true", help="Do not copy thread_goals rows")
    p_sync.add_argument("--goal-status", help="Override copied goal status (e.g., paused)")
    p_sync.add_argument("--no-daemon-restart", action="store_true", help="Do not stop/restart target managed daemon")
    p_sync.add_argument("--no-backup", action="store_true", help="Skip pre-sync target DB backup")
    p_sync.add_argument("--force", action="store_true", help="Proceed even if active daemon clients are detected")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview plan without changing target")
    p_sync.add_argument("--json", action="store_true")

    # verify
    p_ver = sub.add_parser("verify", help="Verify a synced thread in a Codex store")
    p_ver.add_argument("thread", help="Thread UUID or codex://threads/<uuid>")
    p_ver.add_argument("--target", default="cli", help="Target store: app, cli, or path (default: cli)")
    p_ver.add_argument("--check-app-server", action="store_true", help="Also verify via `codex app-server --stdio`")
    p_ver.add_argument("--json", action="store_true")

    # install / update / status / uninstall
    p_inst = sub.add_parser("install", help="Install codex-chat-sync CLI and copy skills to providers")
    p_inst.add_argument("--source", help="Source repository directory")
    p_inst.add_argument("--targets", default="codex,antigravity", help="Comma-separated skill targets: codex,antigravity")
    p_inst.add_argument("--dry-run", action="store_true")
    p_inst.add_argument("--json", action="store_true")

    p_upd = sub.add_parser("update", help="Update installed runtime and copied skills from source repo")
    p_upd.add_argument("--source", help="Source repository directory (defaults to recorded source_root)")
    p_upd.add_argument("--targets", help="Override comma-separated targets (default: keep installed targets)")
    p_upd.add_argument("--pull", action="store_true", help="Run `git pull --ff-only` in source repo before updating")
    p_upd.add_argument("--dry-run", action="store_true")
    p_upd.add_argument("--json", action="store_true")

    p_stat = sub.add_parser("status", help="Check installed runtime and copied skill drift status")
    p_stat.add_argument("--json", action="store_true")

    p_unin = sub.add_parser("uninstall", help="Remove installed launcher, staged runtime, and copied skills")
    p_unin.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            rows = sync_engine.list_threads(
                resolve_store(args.source),
                cwd=args.cwd,
                query=args.query,
                include_archived=args.include_archived,
                include_subagents=args.include_subagents,
                limit=args.limit,
            )
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
            else:
                for r in rows:
                    label = (r.get("name") or r.get("title") or "").replace("\n", " ")[:68]
                    subs = r.get("direct_subagents", 0)
                    print(f"{r['id']}  subs={subs:<2}  cwd={r.get('cwd')}  {label}")
            return 0

        if args.command == "inspect":
            info = sync_engine.inspect_thread(resolve_store(args.source), args.thread)
            if args.json:
                print(json.dumps(info, indent=2))
            else:
                print(f"Thread: {info['thread_id']} ({info.get('name') or 'unnamed'})")
                print(f"  cwd:       {info['cwd']}")
                print(f"  model:     {info.get('model')}  mode={info.get('history_mode')}  archived={info['archived']}")
                print(f"  segments:  {len(info['segments'])}  turns={info['history_stats']['turns']}  items={info['history_stats']['items']}")
                for idx, seg in enumerate(info["segments"]):
                    print(f"    [{idx}] {seg['segment_id']} ({'zst' if seg['compressed'] else 'jsonl'}) -> base={seg['history_base_id']}")
                print(f"  subagents: {len(info['subagents'])}")
                for sb in info["subagents"]:
                    print(f"    - {sb['thread_id']} ({sb.get('nickname') or '-'} / {sb.get('role') or '-'})")
            return 0

        if args.command == "sync":
            policy = "overwrite" if args.overwrite else args.on_existing
            res = sync_engine.sync_threads(
                resolve_store(args.source),
                resolve_store(args.target),
                args.threads,
                with_subagents=not args.no_subagents,
                cwd_override=args.cwd,
                rewrite_session_cwd=not args.no_rewrite_session_cwd,
                on_existing=policy,
                include_goals=not args.no_goals,
                goal_status=args.goal_status,
                manage_daemon=not args.no_daemon_restart,
                force=args.force,
                dry_run=args.dry_run,
                backup=not args.no_backup,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                prefix = "[dry-run] " if res["dry_run"] else ""
                print(f"{prefix}Sync {res['source_home']} -> {res['target_home']}")
                for act in res["actions"]:
                    print(
                        f"  {act['thread_id']}: {act['action']} "
                        f"(segments={act['segments']}, subagents={act['subagents']})"
                    )
                if not res["dry_run"]:
                    print(
                        f"  files={res['copied_files_count']} turns={res['history_counts'].get('thread_turns', 0)} "
                        f"items={res['history_counts'].get('thread_items', 0)} goals={res['goals_copied']}"
                    )
                    if res.get("backup_dir"):
                        print(f"  backup={res['backup_dir']}")
            return 0

        if args.command == "verify":
            res = sync_engine.verify_thread(
                resolve_store(args.target),
                args.thread,
                check_app_server=args.check_app_server,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(
                    f"OK: {res['thread_id']} in {res['target_home']} "
                    f"(cwd={res['cwd']}, segments={res['segments_verified']}, "
                    f"subagents={res['subagents_verified']}, turns={res['turns_in_db']})"
                )
                if res.get("app_server"):
                    as_info = res["app_server"]
                    print(
                        f"  app-server: listed={as_info['listed_under_current_account']}, "
                        f"paged_turns={as_info['paged_turns']}, children={as_info['listed_children_count']}"
                    )
            return 0

        manager, root_dir = _repo_manager()
        if args.command == "install":
            targets = [t.strip() for t in args.targets.split(",") if t.strip()]
            src = Path(args.source).expanduser().resolve() if args.source else root_dir
            res = manager.Manager().install(source=src, targets=targets, dry_run=args.dry_run)
            print(json.dumps(res, indent=2))
            return 0

        if args.command == "update":
            targets = [t.strip() for t in args.targets.split(",") if t.strip()] if args.targets else None
            src = Path(args.source).expanduser().resolve() if args.source else None
            res = manager.Manager().update(source=src, targets=targets, pull=args.pull, dry_run=args.dry_run)
            print(json.dumps(res, indent=2))
            return 0

        if args.command == "status":
            res = manager.Manager().status()
            print(json.dumps(res, indent=2))
            return 0

        if args.command == "uninstall":
            res = manager.Manager().uninstall()
            print(json.dumps(res, indent=2))
            return 0

    except Exception as exc:
        print(f"codex-chat-sync error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
