"""Managed Codex app-server daemon detection and safe pause/restart context."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .discovery import resolve_codex_binary


def daemon_status(codex_home: Path, codex_bin: str | None = None) -> dict:
    """Query `codex app-server daemon version` for the given CODEX_HOME."""
    sock_link = codex_home / "app-server-control" / "app-server-control.sock"
    pid_file = codex_home / "app-server-daemon" / "daemon.pid"
    if not codex_bin:
        codex_bin = resolve_codex_binary()
    if not codex_bin or (not sock_link.exists() and not pid_file.exists()):
        return {"status": "not_configured"}

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        res = subprocess.run(
            [codex_bin, "app-server", "daemon", "version"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = (res.stdout or "").strip()
        if out.startswith("{"):
            return json.loads(out)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass
    return {"status": "unknown"}


def count_active_socket_clients(codex_home: Path) -> int:
    """Best-effort count of active client connections to app-server-control.sock."""
    sock_link = codex_home / "app-server-control" / "app-server-control.sock"
    if not sock_link.exists():
        return 0
    try:
        target = str(sock_link.resolve())
        res = subprocess.run(["ss", "-x", "-H"], capture_output=True, text=True, timeout=3, check=False)
        if res.returncode != 0:
            return 0
        # Connected clients show ESTAB lines referencing the resolved socket path
        return sum(1 for line in res.stdout.splitlines() if target in line and "ESTAB" in line)
    except (OSError, subprocess.SubprocessError):
        return 0


@contextmanager
def managed_daemon_pause(
    codex_home: Path,
    *,
    manage_daemon: bool = True,
    force: bool = False,
) -> Iterator[dict]:
    """Stop a running managed app-server daemon around DB edits and restart it afterwards."""
    codex_bin = resolve_codex_binary()
    info = daemon_status(codex_home, codex_bin)
    was_running = info.get("status") == "running"
    stopped = False

    if manage_daemon and was_running and codex_bin:
        clients = count_active_socket_clients(codex_home)
        if clients > 0 and not force:
            raise RuntimeError(
                f"Target {codex_home} has {clients} active Codex client connection(s) to its daemon. "
                "Close the active CLI session or re-run with --force / --no-daemon-restart."
            )
        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        subprocess.run(
            [codex_bin, "app-server", "daemon", "stop"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        stopped = True

    state = {"was_running": was_running, "stopped": stopped, "restarted": False}
    try:
        yield state
    finally:
        if stopped and codex_bin:
            env = os.environ.copy()
            env["CODEX_HOME"] = str(codex_home)
            res = subprocess.run(
                [codex_bin, "app-server", "daemon", "start"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            state["restarted"] = res.returncode == 0
