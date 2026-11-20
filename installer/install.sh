#!/usr/bin/env bash
set -euo pipefail

INSTALLER_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE_ROOT=$(CDPATH= cd -- "$INSTALLER_DIR/.." && pwd)

DRY_RUN=0
TARGETS="codex,antigravity"
UPDATE_MODE=0
PULL_MODE=0
UNINSTALL_MODE=0
STATUS_MODE=0

usage() {
  cat <<'EOF'
Usage: install.sh [OPTIONS]

Installs codex-chat-sync to ~/.local/bin/codex-chat-sync, stages the runtime in
~/.local/share/codex-chat-sync/current, and copies (never symlinks) the skill
to ~/.codex/skills/codex-chat-sync and ~/.gemini/config/skills/codex-chat-sync.

Options:
  --targets codex,antigravity   Comma-separated skill copy targets
  --update                      Update existing installation from this repo
  --pull                        Run git pull --ff-only before updating
  --status                      Print installation and skill drift status
  --uninstall                   Remove launcher, staged runtime, and copied skills
  --dry-run                     Show install/update plan without changing files
  -h, --help                    Show this help
EOF
}

while (($#)); do
  case "$1" in
    --targets) TARGETS=${2:?missing value}; shift 2 ;;
    --update) UPDATE_MODE=1; shift ;;
    --pull) PULL_MODE=1; UPDATE_MODE=1; shift ;;
    --status) STATUS_MODE=1; shift ;;
    --uninstall) UNINSTALL_MODE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'install.sh: unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

PYTHON_CMD=$(command -v python3 || command -v python || true)
if [[ -z "$PYTHON_CMD" ]]; then
  printf 'install.sh: python3 (>=3.10) is required.\n' >&2
  exit 1
fi

"$PYTHON_CMD" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
  printf 'install.sh: Python 3.10 or newer is required.\n' >&2
  exit 1
}

if ((STATUS_MODE)); then
  exec "$PYTHON_CMD" "$INSTALLER_DIR/manager.py" status
fi
if ((UNINSTALL_MODE)); then
  exec "$PYTHON_CMD" "$INSTALLER_DIR/manager.py" uninstall
fi

extra=()
if ((DRY_RUN)); then extra+=(--dry-run); fi

if ((UPDATE_MODE)); then
  if ((PULL_MODE)); then extra+=(--pull); fi
  exec "$PYTHON_CMD" "$INSTALLER_DIR/manager.py" update --source "$SOURCE_ROOT" --targets "$TARGETS" "${extra[@]}"
fi

exec "$PYTHON_CMD" "$INSTALLER_DIR/manager.py" install --source "$SOURCE_ROOT" --targets "$TARGETS" "${extra[@]}"
