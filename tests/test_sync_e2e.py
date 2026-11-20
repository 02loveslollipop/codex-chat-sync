import base64
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))

from codex_chat_sync import sync_engine

STATE_SCHEMA = """
CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE TABLE thread_sections (id TEXT PRIMARY KEY, name TEXT NOT NULL, appearance TEXT);
CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT,
    cli_version TEXT NOT NULL DEFAULT '',
    first_user_message TEXT NOT NULL DEFAULT '',
    agent_nickname TEXT,
    agent_role TEXT,
    memory_mode TEXT NOT NULL DEFAULT 'enabled',
    model TEXT,
    reasoning_effort TEXT,
    agent_path TEXT,
    created_at_ms INTEGER,
    updated_at_ms INTEGER,
    thread_source TEXT,
    preview TEXT NOT NULL DEFAULT '',
    recency_at INTEGER NOT NULL DEFAULT 0,
    recency_at_ms INTEGER NOT NULL DEFAULT 0,
    history_mode TEXT NOT NULL DEFAULT 'legacy',
    name TEXT,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    thread_section_id TEXT REFERENCES thread_sections(id) ON DELETE SET NULL,
    section_position INTEGER,
    section_entered_at_ms INTEGER,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    originator TEXT,
    daybreak_enabled BOOLEAN,
    creator_user_id TEXT,
    creator_account_id TEXT,
    attached_thread_parent_id TEXT
);
CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT NOT NULL PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE thread_dynamic_tools (thread_id TEXT NOT NULL, position INTEGER NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, input_schema TEXT NOT NULL, defer_loading INTEGER NOT NULL DEFAULT 0, namespace TEXT, PRIMARY KEY(thread_id, position));
CREATE TABLE thread_attachments (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, attachment_type TEXT NOT NULL, identity_key TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE thread_read_receipts (thread_id TEXT PRIMARY KEY NOT NULL, first_unread_turn TEXT, revision TEXT NOT NULL, last_published_turn TEXT);
CREATE TABLE guardian_review_feedback (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, record BLOB NOT NULL);
"""

HISTORY_SCHEMA = """
CREATE TABLE thread_turns (
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    rollout_ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_json TEXT,
    started_at INTEGER,
    completed_at INTEGER,
    duration_ms INTEGER,
    first_user_item_id TEXT,
    final_agent_item_id TEXT,
    rollout_byte_offset INTEGER,
    rollout_end_ordinal INTEGER,
    rollout_end_byte_offset INTEGER,
    root_turn_id TEXT,
    PRIMARY KEY (thread_id, turn_id)
);
CREATE TABLE thread_items (
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    rollout_ordinal INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    item_json TEXT NOT NULL,
    item_type TEXT NOT NULL DEFAULT '',
    updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
    started_at_ms INTEGER,
    completed_at_ms INTEGER,
    PRIMARY KEY (thread_id, turn_id, item_id)
);
CREATE TABLE thread_history_projection_state (
    thread_id TEXT PRIMARY KEY,
    next_rollout_byte_offset INTEGER NOT NULL,
    next_rollout_ordinal INTEGER NOT NULL
);
CREATE TABLE thread_realtime_items (
    thread_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    rollout_ordinal INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    item_json TEXT NOT NULL,
    PRIMARY KEY (thread_id, item_id)
);
"""

GOALS_SCHEMA = """
CREATE TABLE thread_goals (
    thread_id TEXT PRIMARY KEY NOT NULL,
    goal_id TEXT NOT NULL,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    time_used_seconds INTEGER NOT NULL DEFAULT 0,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE thread_goal_continuation_deferrals (
    thread_id TEXT PRIMARY KEY NOT NULL REFERENCES thread_goals(thread_id) ON DELETE CASCADE
);
"""

CODEX_DB_SCHEMA = """
CREATE TABLE local_thread_catalog_sync_state (host_id TEXT PRIMARY KEY, watermark_updated_at REAL, initial_build_complete INTEGER NOT NULL DEFAULT 0, observation_sequence INTEGER NOT NULL DEFAULT 0, last_full_reconciled_at INTEGER);
CREATE TABLE local_thread_catalog_metadata (id INTEGER PRIMARY KEY CHECK (id = 1), catalog_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE local_thread_catalog (
    host_id TEXT NOT NULL, thread_id TEXT NOT NULL, display_title TEXT NOT NULL,
    source_created_at REAL NOT NULL, source_updated_at REAL NOT NULL, cwd TEXT,
    source_kind TEXT NOT NULL, source_detail TEXT, model_provider TEXT, git_branch TEXT,
    observation_sequence INTEGER NOT NULL, missing_candidate INTEGER NOT NULL DEFAULT 0,
    thread_source TEXT, source_recency_at REAL NOT NULL DEFAULT 0,
    pending_observed_title INTEGER NOT NULL DEFAULT 0, project_id TEXT,
    conversation_origin TEXT, trial_conversation_type TEXT, chatgpt_async_status TEXT,
    PRIMARY KEY (host_id, thread_id)
);
"""


def _jwt(acct: str, user: str) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    body = (
        base64.urlsafe_b64encode(
            json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": acct, "chatgpt_user_id": user}}).encode(
                "utf-8"
            )
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"{hdr}.{body}.sig"


def _write_rollout(path: Path, session_meta: dict, body_lines: list[dict]) -> tuple[int, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    l0 = (json.dumps({"timestamp": "2026-11-19T00:00:00Z", "ordinal": 0, "type": "session_meta", "payload": session_meta}, separators=(",", ":")) + "\n").encode("utf-8")
    b_start = len(l0)
    b1 = (json.dumps(body_lines[0], separators=(",", ":")) + "\n").encode("utf-8")
    b2 = (json.dumps(body_lines[1], separators=(",", ":")) + "\n").encode("utf-8")
    path.write_bytes(l0 + b1 + b2)
    return b_start, b_start + len(b1), len(l0 + b1 + b2)


class SyncE2ETests(unittest.TestCase):
    def _init_store(self, home: Path, acct: str, user: str, with_desktop_db: bool = False) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "auth.json").write_text(
            json.dumps({"tokens": {"account_id": acct, "id_token": _jwt(acct, user)}}),
            encoding="utf-8",
        )
        for db_name, schema in (
            ("state_5.sqlite", STATE_SCHEMA),
            ("thread_history_1.sqlite", HISTORY_SCHEMA),
            ("goals_1.sqlite", GOALS_SCHEMA),
        ):
            conn = sqlite3.connect(str(home / db_name))
            conn.executescript(schema)
            conn.commit()
            conn.close()
        if with_desktop_db:
            (home / "sqlite").mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(home / "sqlite" / "codex.db"))
            conn.executescript(CODEX_DB_SCHEMA)
            conn.execute("INSERT INTO local_thread_catalog_sync_state VALUES ('local', 100.0, 1, 5, 100)")
            conn.execute("INSERT INTO local_thread_catalog_metadata VALUES (1, 10)")
            conn.commit()
            conn.close()

    def test_bidirectional_sync_with_lineage_and_nested_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_home = root / "app"
            cli_home = root / ".codex-cli"
            self._init_store(app_home, "acct-app", "user-app", with_desktop_db=True)
            self._init_store(cli_home, "acct-cli", "user-cli", with_desktop_db=False)

            anc_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            main_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
            seg_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"
            sub1_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
            sub2_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

            anc_path = app_home / "sessions/2026/10/10" / f"rollout-2026-10-10T00-00-00-{anc_id}.jsonl"
            _, _, anc_sz = _write_rollout(
                anc_path,
                {"id": anc_id, "cwd": "/proj/symlink", "creator_account_id": "acct-app", "creator_user_id": "user-app"},
                [
                    {"timestamp": "2026-10-10T00:00:01Z", "ordinal": 1, "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-10-10T00:00:02Z", "ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            main_path = app_home / "sessions/2026/11/19" / f"rollout-2026-11-19T00-00-00-{main_id}_{seg_id}.jsonl"
            m_bs, m_be, m_sz = _write_rollout(
                main_path,
                {
                    "id": main_id,
                    "session_id": main_id,
                    "cwd": "/app/.chatgpt-projects/p1",
                    "runtime_workspace_roots": ["/app/.chatgpt-projects/p1"],
                    "creator_account_id": "acct-app",
                    "creator_user_id": "user-app",
                    "history_mode": "paginated",
                    "history_base": {"thread_id": anc_id, "end_ordinal_exclusive": 3, "end_byte_offset": anc_sz},
                },
                [
                    {"timestamp": "2026-11-19T00:00:01Z", "ordinal": 3, "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-11-19T00:00:02Z", "ordinal": 4, "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            sub1_path = app_home / "sessions/2026/11/19" / f"rollout-2026-11-19T00-01-00-{sub1_id}.jsonl"
            s1_bs, s1_be, s1_sz = _write_rollout(
                sub1_path,
                {"id": sub1_id, "cwd": "/real/repo", "creator_account_id": "acct-app", "creator_user_id": "user-app"},
                [
                    {"timestamp": "2026-11-19T00:01:01Z", "ordinal": 1, "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-11-19T00:01:02Z", "ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            sub2_path = app_home / "sessions/2026/11/19" / f"rollout-2026-11-19T00-02-00-{sub2_id}.jsonl"
            s2_bs, s2_be, s2_sz = _write_rollout(
                sub2_path,
                {"id": sub2_id, "cwd": "/real/repo", "creator_account_id": "acct-app", "creator_user_id": "user-app"},
                [
                    {"timestamp": "2026-11-19T00:02:01Z", "ordinal": 1, "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-11-19T00:02:02Z", "ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )

            s5 = sqlite3.connect(str(app_home / "state_5.sqlite"))
            s5.execute("INSERT INTO projects VALUES ('proj-1', 'P1', '{}', 0, 1000, 1000)")
            for tid_v, rp_v, upd, src, nick, proj in (
                (anc_id, anc_path, 1000, "vscode", None, None),
                (main_id, main_path, 2000, "vscode", None, "proj-1"),
                (sub1_id, sub1_path, 2100, '{"subagent":{"thread_spawn":{}}}', "Galileo", None),
                (sub2_id, sub2_path, 2200, '{"subagent":{"thread_spawn":{}}}', "Pascal", None),
            ):
                s5.execute(
                    "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, history_mode, name, project_id, daybreak_enabled, creator_user_id, creator_account_id, preview) "
                    "VALUES (?, ?, 1000, ?, ?, 'openai', '/real/repo', 'Test Chat', '{}', 'never', 'paginated', 'Test Name', ?, 0, 'user-app', 'acct-app', 'Test Chat')",
                    (tid_v, str(rp_v), upd, src, proj),
                )
            s5.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')", (main_id, sub1_id))
            s5.execute("INSERT INTO thread_spawn_edges VALUES (?, ?, 'open')", (sub1_id, sub2_id))
            s5.commit()
            s5.close()

            th = sqlite3.connect(str(app_home / "thread_history_1.sqlite"))
            for seg, bs, be, sz in ((anc_id, 0, 0, anc_sz), (seg_id, m_bs, m_be, m_sz), (sub1_id, s1_bs, s1_be, s1_sz), (sub2_id, s2_bs, s2_be, s2_sz)):
                th.execute(
                    "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset) VALUES (?, ?, 1, 'completed', ?, 2, ?)",
                    (seg, f"turn-{seg[:4]}", bs, be),
                )
                th.execute("INSERT INTO thread_items VALUES (?, ?, 'item-1', 1, 1000, '{}', 'userMessage', 1, 1000, 1000)", (seg, f"turn-{seg[:4]}"))
                th.execute("INSERT INTO thread_history_projection_state VALUES (?, ?, 5)", (seg, sz))
            th.commit()
            th.close()

            g = sqlite3.connect(str(app_home / "goals_1.sqlite"))
            g.execute("INSERT INTO thread_goals VALUES (?, 'g1', 'finish tests', 'usage_limited', NULL, 10, 5, 1000, 2000)", (main_id,))
            g.commit()
            g.close()

            # Sync app -> cli
            res = sync_engine.sync_threads(app_home, cli_home, [f"codex://threads/{main_id}"], backup=False)
            self.assertEqual(res["actions"][0]["action"], "create")
            self.assertEqual(res["actions"][0]["subagents"], 2)
            self.assertEqual(res["goals_copied"], 1)

            v = sync_engine.verify_thread(cli_home, main_id, check_app_server=False)
            self.assertTrue(v["ok"])
            self.assertTrue(v["byte_offset_match"])
            self.assertEqual(v["subagents_verified"], 2)
            self.assertEqual(v["segments_verified"], 2)

            # Confirm ancestor archived and main thread unarchived with cli account
            cs5 = sqlite3.connect(str(cli_home / "state_5.sqlite"))
            self.assertEqual(cs5.execute("SELECT archived, creator_account_id, project_id FROM threads WHERE id=?", (main_id,)).fetchone(), (0, "acct-cli", None))
            self.assertEqual(cs5.execute("SELECT archived FROM threads WHERE id=?", (anc_id,)).fetchone(), (1,))
            cs5.close()

            # Idempotent re-sync skips when up to date
            res_skip = sync_engine.sync_threads(app_home, cli_home, [main_id], backup=False)
            self.assertEqual(res_skip["actions"][0]["action"], "skip_up_to_date")

            # Now create a CLI-only thread and sync cli -> app (verifying desktop codex.db catalog update)
            cli_only = "ffffffff-ffff-ffff-ffff-ffffffffffff"
            cli_only_path = cli_home / "sessions/2026/11/20" / f"rollout-2026-11-20T00-00-00-{cli_only}.jsonl"
            c_bs, c_be, c_sz = _write_rollout(
                cli_only_path,
                {"id": cli_only, "cwd": "/cli/work", "creator_account_id": "acct-cli", "creator_user_id": "user-cli"},
                [
                    {"timestamp": "2026-11-20T00:00:01Z", "ordinal": 1, "type": "event_msg", "payload": {"type": "task_started"}},
                    {"timestamp": "2026-11-20T00:00:02Z", "ordinal": 2, "type": "event_msg", "payload": {"type": "task_complete"}},
                ],
            )
            cs5 = sqlite3.connect(str(cli_home / "state_5.sqlite"))
            cs5.execute(
                "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, model_provider, cwd, title, sandbox_policy, approval_mode, history_mode, name, creator_user_id, creator_account_id, preview) "
                "VALUES (?, ?, 3000, 3000, 'cli', 'openai', '/cli/work', 'CLI Thread', '{}', 'never', 'paginated', 'CLI Thread', 'user-cli', 'acct-cli', 'CLI Thread')",
                (cli_only, str(cli_only_path)),
            )
            cs5.commit()
            cs5.close()

            cth = sqlite3.connect(str(cli_home / "thread_history_1.sqlite"))
            cth.execute("INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, status, rollout_byte_offset, rollout_end_ordinal, rollout_end_byte_offset) VALUES (?, 't1', 1, 'completed', ?, 2, ?)", (cli_only, c_bs, c_be))
            cth.execute("INSERT INTO thread_history_projection_state VALUES (?, ?, 3)", (cli_only, c_sz))
            cth.commit()
            cth.close()

            res_rev = sync_engine.sync_threads(cli_home, app_home, [cli_only], backup=False)
            self.assertEqual(res_rev["desktop_catalog_updated"], 1)
            v_app = sync_engine.verify_thread(app_home, cli_only, check_app_server=False)
            self.assertTrue(v_app["byte_offset_match"])

            cdb = sqlite3.connect(str(app_home / "sqlite/codex.db"))
            cat = cdb.execute("SELECT display_title, cwd, observation_sequence FROM local_thread_catalog WHERE thread_id=?", (cli_only,)).fetchone()
            rev = cdb.execute("SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1").fetchone()[0]
            cdb.close()
            self.assertEqual(cat, ("CLI Thread", "/cli/work", 6))
            self.assertEqual(rev, 11)


if __name__ == "__main__":
    unittest.main()
