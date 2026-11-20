#!/usr/bin/env python3
"""Transactional installer, updater, and drift checker for codex-chat-sync.

Standard-library-only, inspired by OpenCROW's manager pattern. Copies skills
(never symlinks) into Codex and Antigravity skill directories and tracks hashes
in install-state.json so `update` and `status` keep installed copies synced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
TARGETS = ("codex", "antigravity")
LAUNCHER_NAME = "codex-chat-sync"
SKILL_NAME = "codex-chat-sync"


class InstallError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        existing_mode = mode
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(root).as_posix()] = sha256_file(path)
    return result


def copy_tree_no_symlinks(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise InstallError(f"Required source directory is missing: {source}")
    shutil.copytree(
        source,
        target,
        dirs_exist_ok=True,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache"),
    )


@dataclass(frozen=True)
class InstallPaths:
    home: Path
    data: Path
    bin_dir: Path

    @classmethod
    def resolve(cls, home: Path | None = None) -> "InstallPaths":
        resolved_home = (home or Path(os.environ.get("CODEX_CHAT_SYNC_HOME") or Path.home())).expanduser().resolve()
        xdg_data = os.environ.get("XDG_DATA_HOME")
        data_root = Path(xdg_data).expanduser().resolve() if xdg_data else (resolved_home / ".local" / "share")
        return cls(
            home=resolved_home,
            data=data_root / "codex-chat-sync",
            bin_dir=resolved_home / ".local" / "bin",
        )

    @property
    def current(self) -> Path:
        return self.data / "current"

    @property
    def previous(self) -> Path:
        return self.data / "previous"

    @property
    def backups(self) -> Path:
        return self.data / "backups"

    @property
    def state_file(self) -> Path:
        return self.data / "install-state.json"

    @property
    def launcher(self) -> Path:
        return self.bin_dir / LAUNCHER_NAME


class Manager:
    """Transactional installer, updater, and drift verifier for codex-chat-sync."""

    def __init__(self, home: Path | None = None) -> None:
        self.paths = InstallPaths.resolve(home)

    def state(self) -> dict[str, Any]:
        if not self.paths.state_file.is_file():
            return {}
        try:
            data = json.loads(self.paths.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _skill_roots_for_target(self, target: str) -> list[Path]:
        if target == "codex":
            codex_dir = self.paths.home / ".codex" / "skills"
            cli_dir = self.paths.home / ".codex-cli" / "skills"
            roots = [codex_dir]
            # Only install separately to .codex-cli/skills if it exists and does NOT resolve to .codex/skills
            if (self.paths.home / ".codex-cli").exists():
                if not cli_dir.exists() and not cli_dir.is_symlink():
                    pass
                elif cli_dir.resolve(strict=False) != codex_dir.resolve(strict=False):
                    roots.append(cli_dir)
            return roots
        if target == "antigravity":
            return [self.paths.home / ".gemini" / "config" / "skills"]
        raise InstallError(f"Unknown skill target: {target}")

    def _validate_source(self, source: Path) -> None:
        required = [
            source / "packages" / "codex_chat_sync" / "cli.py",
            source / "installer" / "manager.py",
            source / "skills" / SKILL_NAME / "SKILL.md",
        ]
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            raise InstallError(f"Incomplete source repository {source}; missing: {', '.join(missing)}")

    def _stage(self, source: Path) -> Path:
        self.paths.data.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=self.paths.data))
        try:
            copy_tree_no_symlinks(source / "packages", stage / "packages")
            copy_tree_no_symlinks(source / "installer", stage / "installer")
            copy_tree_no_symlinks(source / "skills", stage / "skills")
            if (source / "README.md").is_file():
                shutil.copy2(source / "README.md", stage / "README.md")
            return stage
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise

    def _commit_stage(self, stage: Path) -> None:
        retiring = self.paths.data / "previous.retiring"
        if retiring.exists():
            shutil.rmtree(retiring, ignore_errors=True)
        if self.paths.previous.exists():
            self.paths.previous.replace(retiring)
        if self.paths.current.exists():
            self.paths.current.replace(self.paths.previous)
        stage.replace(self.paths.current)
        shutil.rmtree(retiring, ignore_errors=True)

    def _backup_existing(self, path: Path, backups: list[dict[str, Any]]) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if path.is_symlink():
            backups.append({"path": str(path), "kind": "symlink", "target": os.readlink(path)})
            return
        if path.is_dir():
            dest = self.paths.backups / f"{stamp}-{path.parent.parent.name}-{path.name}"
            counter = 1
            while dest.exists():
                dest = self.paths.backups / f"{stamp}-{counter}-{path.name}"
                counter += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(path, dest, symlinks=True)
            backups.append({"path": str(path), "kind": "directory", "backup": str(dest)})
        elif path.is_file():
            dest = self.paths.backups / f"{stamp}-{path.name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            backups.append({"path": str(path), "kind": "file", "backup": str(dest)})

    def _install_launcher(self, backups: list[dict[str, Any]]) -> Path:
        launcher = self.paths.launcher
        launcher.parent.mkdir(parents=True, exist_ok=True)
        if launcher.exists() or launcher.is_symlink():
            if launcher.is_symlink() or "codex_chat_sync" not in launcher.read_text(encoding="utf-8", errors="ignore"):
                self._backup_existing(launcher, backups)
            if launcher.is_symlink():
                launcher.unlink()
        py_exec = sys.executable or "python3"
        script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'PKG_DIR="{self.paths.current / "packages"}"\n'
            'export PYTHONPATH="$PKG_DIR${PYTHONPATH:+:$PYTHONPATH}"\n'
            f'exec "{py_exec}" -m codex_chat_sync.cli "$@"\n'
        )
        atomic_text(launcher, script, mode=0o755)
        os.chmod(launcher, 0o755)
        return launcher

    def _copy_skills(
        self,
        targets: list[str],
        backups: list[dict[str, Any]],
        previous_state: dict[str, Any],
    ) -> tuple[list[str], dict[str, dict[str, str]]]:
        staged_skill = self.paths.current / "skills" / SKILL_NAME
        managed_dirs: list[str] = []
        skill_hashes: dict[str, dict[str, str]] = {}

        prev_targets = set(previous_state.get("selected_targets", []))
        for removed in prev_targets - set(targets):
            if removed in TARGETS:
                for root in self._skill_roots_for_target(removed):
                    dest = root / SKILL_NAME
                    if dest.is_symlink():
                        dest.unlink()
                    elif dest.is_dir():
                        shutil.rmtree(dest)

        for target in targets:
            for root in self._skill_roots_for_target(target):
                root.mkdir(parents=True, exist_ok=True)
                dest = root / SKILL_NAME
                if dest.exists() or dest.is_symlink():
                    is_managed = str(dest) in set(previous_state.get("skill_paths", []))
                    if dest.is_symlink() or not is_managed:
                        self._backup_existing(dest, backups)
                    if dest.is_symlink():
                        dest.unlink()
                    elif dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                # Strictly copy directory (no symlinks)
                copy_tree_no_symlinks(staged_skill, dest)
                if dest.is_symlink() or not (dest / "SKILL.md").is_file():
                    raise InstallError(f"Failed to copy skill directory cleanly to {dest}")
                managed_dirs.append(str(dest))
                skill_hashes[str(dest)] = tree_hashes(dest)

        return managed_dirs, skill_hashes

    def install(
        self,
        *,
        source: Path,
        targets: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        src = source.expanduser().resolve()
        self._validate_source(src)
        selected = list(dict.fromkeys(targets or list(TARGETS)))
        invalid = sorted(set(selected) - set(TARGETS))
        if invalid:
            raise InstallError(f"Unknown skill targets: {', '.join(invalid)} (expected: {', '.join(TARGETS)})")

        skill_destinations = [
            str(root / SKILL_NAME)
            for t in selected
            for root in self._skill_roots_for_target(t)
        ]

        if dry_run:
            return {
                "dry_run": True,
                "version": VERSION,
                "source_root": str(src),
                "staged_current": str(self.paths.current),
                "launcher": str(self.paths.launcher),
                "skill_destinations": skill_destinations,
                "copy_mode": "directory_copy_no_symlink",
            }

        previous_state = self.state()
        stage = self._stage(src)
        self._commit_stage(stage)

        backups: list[dict[str, Any]] = []
        launcher = self._install_launcher(backups)
        skill_paths, skill_hashes = self._copy_skills(selected, backups, previous_state)

        state = {
            "schema_version": 1,
            "version": VERSION,
            "source_root": str(src),
            "selected_targets": selected,
            "launcher_path": str(launcher),
            "staged_path": str(self.paths.current),
            "skill_paths": skill_paths,
            "managed_paths": sorted({str(launcher), str(self.paths.current), *skill_paths}),
            "runtime_hashes": tree_hashes(self.paths.current / "packages"),
            "skill_source_hashes": tree_hashes(self.paths.current / "skills" / SKILL_NAME),
            "skill_hashes": skill_hashes,
            "backups": backups,
            "installed_at": previous_state.get("installed_at") or utc_now(),
            "updated_at": utc_now(),
        }
        atomic_text(self.paths.state_file, json.dumps(state, indent=2) + "\n")
        return state

    def update(
        self,
        *,
        source: Path | None = None,
        targets: list[str] | None = None,
        pull: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        prev = self.state()
        src = (
            source.expanduser().resolve()
            if source
            else Path(str(prev.get("source_root") or Path(__file__).resolve().parent.parent)).expanduser().resolve()
        )
        if not src.is_dir():
            raise InstallError(f"Source repository not found: {src}")
        if pull and not dry_run and (src / ".git").exists():
            res = subprocess.run(
                ["git", "-C", str(src), "pull", "--ff-only"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raise InstallError(f"git pull --ff-only failed in {src}: {res.stderr.strip()}")
        selected = targets if targets is not None else list(prev.get("selected_targets") or TARGETS)
        return self.install(source=src, targets=selected, dry_run=dry_run)

    def status(self) -> dict[str, Any]:
        st = self.state()
        if not st:
            return {"installed": False, "in_sync": False, "state_file": str(self.paths.state_file)}

        src = Path(str(st.get("source_root", "")))
        src_pkg_hashes = tree_hashes(src / "packages") if src.is_dir() else {}
        src_skill_hashes = tree_hashes(src / "skills" / SKILL_NAME) if src.is_dir() else {}
        cur_pkg_hashes = tree_hashes(self.paths.current / "packages")

        launcher = Path(str(st.get("launcher_path") or self.paths.launcher))
        launcher_ok = launcher.is_file() and os.access(launcher, os.X_OK)
        runtime_in_sync = bool(src_pkg_hashes) and (src_pkg_hashes == cur_pkg_hashes)

        skills_status: dict[str, Any] = {}
        all_skills_ok = True
        for sp in st.get("skill_paths", []):
            p = Path(sp)
            is_sym = p.is_symlink()
            exists = p.is_dir() and (p / "SKILL.md").is_file()
            cur_h = tree_hashes(p) if exists else {}
            matches_src = bool(src_skill_hashes) and (cur_h == src_skill_hashes)
            if is_sym or not exists or not matches_src:
                all_skills_ok = False
            skills_status[sp] = {
                "exists": exists,
                "is_symlink": is_sym,
                "matches_source": matches_src,
            }

        return {
            "installed": True,
            "version": st.get("version"),
            "source_root": str(src),
            "launcher": {"path": str(launcher), "executable": launcher_ok},
            "runtime_in_sync": runtime_in_sync,
            "skills": skills_status,
            "in_sync": launcher_ok and runtime_in_sync and all_skills_ok,
            "updated_at": st.get("updated_at"),
        }

    def uninstall(self) -> dict[str, Any]:
        st = self.state()
        removed: list[str] = []
        launcher = Path(str(st.get("launcher_path") or self.paths.launcher))
        if launcher.exists() or launcher.is_symlink():
            launcher.unlink()
            removed.append(str(launcher))
        for sp in st.get("skill_paths", []):
            p = Path(sp)
            if p.is_symlink():
                p.unlink()
                removed.append(str(p))
            elif p.is_dir():
                shutil.rmtree(p)
                removed.append(str(p))
        for d in (self.paths.current, self.paths.previous):
            if d.exists():
                shutil.rmtree(d)
                removed.append(str(d))
        if self.paths.state_file.exists():
            self.paths.state_file.unlink()
            removed.append(str(self.paths.state_file))
        return {"uninstalled": True, "removed": removed}


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-chat-sync-manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inst = sub.add_parser("install")
    p_inst.add_argument("--source", required=True)
    p_inst.add_argument("--targets", default="codex,antigravity")
    p_inst.add_argument("--dry-run", action="store_true")

    p_upd = sub.add_parser("update")
    p_upd.add_argument("--source")
    p_upd.add_argument("--targets")
    p_upd.add_argument("--pull", action="store_true")
    p_upd.add_argument("--dry-run", action="store_true")

    sub.add_parser("status")
    sub.add_parser("uninstall")

    args = parser.parse_args(argv)
    mgr = Manager()
    if args.cmd == "install":
        targets = [t.strip() for t in args.targets.split(",") if t.strip()]
        res = mgr.install(source=Path(args.source), targets=targets, dry_run=args.dry_run)
    elif args.cmd == "update":
        targets = [t.strip() for t in args.targets.split(",") if t.strip()] if args.targets else None
        src = Path(args.source) if args.source else None
        res = mgr.update(source=src, targets=targets, pull=args.pull, dry_run=args.dry_run)
    elif args.cmd == "status":
        res = mgr.status()
    else:
        res = mgr.uninstall()
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
