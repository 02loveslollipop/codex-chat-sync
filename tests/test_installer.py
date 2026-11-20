import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "installer"))

import manager


class InstallerTests(unittest.TestCase):
    def test_install_copies_not_symlinks_detects_drift_and_updates(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            old_xdg = os.environ.pop("XDG_DATA_HOME", None)
            try:
                mgr = manager.Manager(home=fake_home)

                # Create a pre-existing symlink at ~/.codex/skills/codex-chat-sync to ensure it is replaced by a real copy
                codex_skills = fake_home / ".codex" / "skills"
                codex_skills.mkdir(parents=True)
                (codex_skills / "codex-chat-sync").symlink_to(repo_root / "skills" / "codex-chat-sync")

                state = mgr.install(source=repo_root, targets=["codex", "antigravity"])
                self.assertEqual(len(state["skill_paths"]), 2)

                codex_copy = fake_home / ".codex" / "skills" / "codex-chat-sync"
                agy_copy = fake_home / ".gemini" / "config" / "skills" / "codex-chat-sync"
                for copy_dir in (codex_copy, agy_copy):
                    self.assertTrue(copy_dir.is_dir())
                    self.assertFalse(copy_dir.is_symlink())
                    self.assertTrue((copy_dir / "SKILL.md").is_file())

                launcher = fake_home / ".local" / "bin" / "codex-chat-sync"
                self.assertTrue(launcher.is_file())
                self.assertTrue(os.access(launcher, os.X_OK))

                st = mgr.status()
                self.assertTrue(st["installed"])
                self.assertTrue(st["in_sync"])

                # Simulate local drift in copied skill
                (agy_copy / "SKILL.md").write_text("drifted\n", encoding="utf-8")
                st_drift = mgr.status()
                self.assertFalse(st_drift["in_sync"])
                self.assertFalse(st_drift["skills"][str(agy_copy)]["matches_source"])

                # Update restores copied skill
                mgr.update()
                st_fixed = mgr.status()
                self.assertTrue(st_fixed["in_sync"])

                # Uninstall removes managed files
                u = mgr.uninstall()
                self.assertTrue(u["uninstalled"])
                self.assertFalse(codex_copy.exists())
                self.assertFalse(agy_copy.exists())
                self.assertFalse(launcher.exists())
            finally:
                if old_xdg is not None:
                    os.environ["XDG_DATA_HOME"] = old_xdg


if __name__ == "__main__":
    unittest.main()
