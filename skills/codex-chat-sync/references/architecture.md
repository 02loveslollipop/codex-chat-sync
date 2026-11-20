# Codex Thread Storage & Sync Invariants

## Two Separate `CODEX_HOME` Stores

- **Codex / ChatGPT Desktop App**: `~/.codex` (`CODEX_APP_HOME`)
- **Codex CLI**: `~/.codex-cli` (`CODEX_CLI_HOME`), typically invoked via `alias codex="CODEX_HOME=$HOME/.codex-cli codex"`

Each store can be authenticated to a different ChatGPT account (`auth.json`) and maintains its own SQLite databases, rollout files, and `app-server` daemon.

## Critical Sync Invariants

1. **Account Scoping (`currentAccountOnly=true`)**
   - `codex resume` filters threads using `account_scoped: true`, `creator_account_id`, and `creator_user_id`.
   - `codex-chat-sync` extracts the target store's `chatgpt_account_id` and `chatgpt_user_id` from `auth.json` (`id_token` claims, falling back to `state_5.sqlite`) and updates both `threads` in `state_5.sqlite` and line 0 (`session_meta.payload`) of active rollout files.

2. **Byte-Offset Delta Adjustment (`thread_history_1.sqlite`)**
   - `thread_turns.rollout_byte_offset`, `thread_turns.rollout_end_byte_offset`, and `thread_history_projection_state.next_rollout_byte_offset` store exact byte offsets inside the active rollout JSONL file.
   - Rewriting line 0 (`session_meta`) changes its byte length by `delta = len(new_line0) - len(orig_line0)`. Every offset for that active segment in `thread_history_1.sqlite` is shifted by `delta`.

3. **`history_base` Paginated Lineage**
   - Paginated threads chain across multiple rollout segments (`.jsonl` and `.jsonl.zst`) via `session_meta.payload.history_base.thread_id`.
   - All ancestor segments are copied byte-for-byte into `archived_sessions/` so `turnsBackwardsCursor` / `thread/turns/list` can page all turns back to the origin without creating duplicate entries in the active session picker.
   - Any distinct root parent thread row (`forked_from_id`) not already present in target `state_5.sqlite` is registered with `archived = 1`.

4. **Working Directory (`cwd`) & Project Foreign Keys**
   - Desktop threads created inside `.chatgpt-projects/<id>` often retain that symlink in `session_meta.cwd` even after switching `state_5.sqlite` `cwd`. `codex-chat-sync` normalizes `session_meta.cwd` and `runtime_workspace_roots` to match the effective `cwd`.
   - Cross-store `project_id` and `thread_section_id` foreign keys are cleared (`NULL`) on target `threads` rows to prevent `FOREIGN KEY constraint failed`.

5. **Recursive Subagents (`thread_spawn_edges`)**
   - Descendant subagent threads are discovered recursively via `thread_spawn_edges`, including their own rollout segments, `thread_history_1.sqlite` projections, and spawn edges.

6. **Desktop App Catalog (`sqlite/codex.db`)**
   - When syncing into a store containing `sqlite/codex.db`, top-level threads are also upserted into `local_thread_catalog` (`host_id = 'local'`) and `local_thread_catalog_metadata.catalog_revision` is incremented.
