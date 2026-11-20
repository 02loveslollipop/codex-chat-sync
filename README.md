# codex-chat-sync

Bidirectional individual thread synchronization between the **Codex / ChatGPT desktop app** (`~/.codex`) and **Codex CLI** (`~/.codex-cli`), or any custom `CODEX_HOME` directories.

Copies individual conversations (`<uuid>` or `codex://threads/<uuid>`) together with:
- Paginated `history_base` segment chains (`.jsonl` and `.jsonl.zst`)
- Recursive descendant subagent threads and `thread_spawn_edges`
- `thread_history_1.sqlite` turn/item projections with exact byte-offset delta adjustment
- `goals_1.sqlite`, auxiliary thread tables, `session_index.jsonl`, and desktop `sqlite/codex.db` catalog entries
- Automatic account scoping (`account_scoped`, `creator_account_id`, `creator_user_id`) and working directory normalization

## Installation

Install the `codex-chat-sync` launcher (`~/.local/bin/codex-chat-sync`), staged runtime (`~/.local/share/codex-chat-sync/current`), and **copied** (non-symlink) skills into `~/.codex/skills/codex-chat-sync` and `~/.gemini/config/skills/codex-chat-sync`:

```bash
./install.sh
```

Check drift or update installed copies from the repository:

```bash
codex-chat-sync status
codex-chat-sync update --pull
```

## Usage

List threads in a store (`app`, `cli`, or explicit `CODEX_HOME` path):

```bash
codex-chat-sync list --source app --query "Pokemon"
```

Inspect a thread's `history_base` chain and subagent tree:

```bash
codex-chat-sync inspect codex://threads/01a17a0d-9ba2-7bc1-9570-5f0214c5a815 --source app
```

Sync a thread (with subchats) from Codex App to Codex CLI (or vice versa):

```bash
codex-chat-sync sync codex://threads/01a17a0d-9ba2-7bc1-9570-5f0214c5a815 --from app --to cli
codex-chat-sync sync <thread-uuid> --from cli --to app
```

Verify the synced thread (optionally validating live `codex app-server --stdio` listing and full turn pagination):

```bash
codex-chat-sync verify 01a17a0d-9ba2-7bc1-9570-5f0214c5a815 --target cli --check-app-server
```

## Repository Layout

- `packages/codex_chat_sync/` — standard-library-only sync engine, SQLite/rollout handlers, daemon guard, and CLI.
- `installer/` — OpenCROW-style transactional staged installer, updater, and hash-drift verifier.
- `skills/codex-chat-sync/` — Agent Skill (`SKILL.md`, `agents/openai.yaml`, `references/architecture.md`) copied to Codex and Antigravity skill directories.
- `tests/` — unit and end-to-end integration tests.
