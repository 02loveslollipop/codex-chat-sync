---
name: codex-chat-sync
description: >-
  Sync individual Codex chats/threads (including paginated history_base chains and
  descendant subagent threads) bidirectionally between the Codex/ChatGPT app
  (~/.codex) and Codex CLI (~/.codex-cli), or custom CODEX_HOME stores. Use
  whenever the user asks to copy, transfer, mirror, inspect, or verify a
  codex://threads/<id> chat or thread UUID across Codex stores.
---

# Codex Chat Sync

Use `codex-chat-sync` (installed in `~/.local/bin/codex-chat-sync`, repo at `~/codex-chat-sync`) to inspect, copy, and verify individual threads with their full history and subchats across Codex homes.

## Workflow

1. **Locate or inspect the thread first** (read-only):
   ```bash
   codex-chat-sync list --source app --query "<keyword>"
   codex-chat-sync inspect "<thread_id_or_codex://threads/...>" --source app
   ```
   Use `--source cli` when inspecting from `~/.codex-cli`.

2. **Preview with `--dry-run` before mutating** (especially if the target thread may already exist):
   ```bash
   codex-chat-sync sync "<thread_id_or_uri>" --from app --to cli --dry-run
   ```

3. **Run the sync** (includes recursive descendant subagents, `history_base` lineage, `thread_history_1.sqlite` projection with byte-offset delta adjustment, `goals_1.sqlite`, and target account/CWD normalization):
   ```bash
   codex-chat-sync sync "<thread_id_or_uri>" --from app --to cli
   ```
   Common variants:
   - Reverse direction (`~/.codex-cli` -> `~/.codex`):
     ```bash
     codex-chat-sync sync "<thread_id>" --from cli --to app
     ```
   - Override target working directory:
     ```bash
     codex-chat-sync sync "<thread_id>" --from app --to cli --cwd /path/to/repo
     ```
   - Overwrite an existing target copy unconditionally:
     ```bash
     codex-chat-sync sync "<thread_id>" --from app --to cli --overwrite
     ```
   - Exclude child subagent threads:
     ```bash
     codex-chat-sync sync "<thread_id>" --from app --to cli --no-subagents
     ```

4. **Verify after syncing**:
   ```bash
   codex-chat-sync verify "<thread_id>" --target cli --check-app-server
   ```

## Keeping the Tool & Skill Up to Date

```bash
codex-chat-sync status
codex-chat-sync update --pull
```

## Notes & Safety

- `sync` automatically backs up target SQLite DBs and `session_index.jsonl` under `~/.local/share/codex-chat-sync/sync-backups/` before writing, and pauses/restarts a managed `app-server` daemon when no active client is attached.
- If syncing into `~/.codex` while the ChatGPT/Codex desktop GUI is open, restart or reload the desktop app if the sidebar does not refresh immediately.
- For storage internals and invariants (`state_5.sqlite`, `thread_history_1.sqlite`, `history_base`, `account_scoped`, `sqlite/codex.db`), read [references/architecture.md](references/architecture.md).
