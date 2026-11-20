import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages"))

from codex_chat_sync import discovery, rollout


def _make_fake_jwt(claims: dict) -> str:
    hdr = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{hdr}.{body}.sig"


class RolloutTests(unittest.TestCase):
    def test_normalize_thread_id(self) -> None:
        uid = "01A17A0D-9BA2-7BC1-9570-5F0214C5A815"
        self.assertEqual(discovery.normalize_thread_id(uid), uid.lower())
        self.assertEqual(discovery.normalize_thread_id(f"codex://threads/{uid}/"), uid.lower())
        with self.assertRaises(ValueError):
            discovery.normalize_thread_id("not-a-uuid")

    def test_extract_target_identity_from_jwt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            jwt = _make_fake_jwt(
                {
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acct-123",
                        "chatgpt_user_id": "user-456",
                    }
                }
            )
            (home / "auth.json").write_text(
                json.dumps({"tokens": {"id_token": jwt, "account_id": "acct-fallback"}}),
                encoding="utf-8",
            )
            ident = discovery.extract_target_identity(home)
            self.assertEqual(ident.account_id, "acct-123")
            self.assertEqual(ident.user_id, "user-456")

    def test_trace_chain_with_zst_and_rewrite_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            s_dir = home / "sessions" / "2026" / "11" / "19"
            a_dir = home / "archived_sessions"
            s_dir.mkdir(parents=True)
            a_dir.mkdir(parents=True)

            root_id = "11111111-1111-1111-1111-111111111111"
            tid = "22222222-2222-2222-2222-222222222222"
            seg_id = "33333333-3333-3333-3333-333333333333"

            root_line = (
                json.dumps(
                    {
                        "timestamp": "2026-10-01T00:00:00Z",
                        "ordinal": 0,
                        "type": "session_meta",
                        "payload": {"id": root_id, "cwd": "/old"},
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            root_zst = a_dir / f"rollout-2026-10-01T00-00-00-{root_id}.jsonl.zst"
            zstd_bin = rollout.find_zstd_binary()
            if zstd_bin:
                root_zst.write_bytes(subprocess.check_output([zstd_bin, "-c", "-q"], input=root_line))
                expected_root_path = root_zst
            else:
                root_plain = a_dir / f"rollout-2026-10-01T00-00-00-{root_id}.jsonl"
                root_plain.write_bytes(root_line)
                expected_root_path = root_plain

            active = s_dir / f"rollout-2026-11-19T00-00-00-{tid}_{seg_id}.jsonl"
            line0 = (
                json.dumps(
                    {
                        "timestamp": "2026-11-19T00:00:00Z",
                        "ordinal": 10,
                        "type": "session_meta",
                        "payload": {
                            "id": tid,
                            "session_id": tid,
                            "creator_account_id": "old-acct",
                            "creator_user_id": "old-user",
                            "cwd": "/old-symlink",
                            "runtime_workspace_roots": ["/old-symlink"],
                            "history_base": {"thread_id": root_id, "end_ordinal_exclusive": 10, "end_byte_offset": len(root_line)},
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            line1 = b'{"timestamp":"2026-11-19T00:00:01Z","ordinal":11,"type":"event_msg","payload":{"type":"task_started"}}\n'
            active.write_bytes(line0 + line1)

            chain = rollout.trace_history_chain(home, active, tid)
            self.assertEqual(len(chain), 2)
            self.assertEqual(chain[0].segment_id, seg_id)
            self.assertEqual(chain[1].segment_id, root_id)
            self.assertEqual(chain[1].path, expected_root_path)

            dst = Path(tmp) / "out.jsonl"
            res = rollout.rewrite_active_rollout(
                active,
                dst,
                account_id="new-acct-1234567890",
                user_id="new-user",
                cwd="/new/cwd",
                rewrite_cwd=True,
                account_scoped=True,
            )
            raw_out = dst.read_bytes()
            nl = raw_out.find(b"\n")
            new_l0 = raw_out[: nl + 1]
            self.assertEqual(res.delta_bytes, len(new_l0) - len(line0))
            self.assertEqual(raw_out[nl + 1 :], line1)
            meta = json.loads(new_l0.decode("utf-8"))["payload"]
            self.assertTrue(meta["account_scoped"])
            self.assertEqual(meta["creator_account_id"], "new-acct-1234567890")
            self.assertEqual(meta["cwd"], "/new/cwd")
            self.assertEqual(meta["runtime_workspace_roots"], ["/new/cwd"])


if __name__ == "__main__":
    unittest.main()
