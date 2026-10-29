"""Repair interrupted checkouts at the recorded gitlink without losing local work."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import worktree_setup  # noqa: E402


def git(cwd, *args):
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "-c",
            "protocol.file.allow=always",
            "-C",
            str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class WorktreeSetup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        source = base / "source"
        source.mkdir()
        git(source, "init", "-q")
        (source / "a.txt").write_text("pinned\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "pinned")
        self.recorded = git(source, "rev-parse", "HEAD")
        (source / "a.txt").write_text("newer\n")
        git(source, "commit", "-qam", "newer")
        self.newer = git(source, "rev-parse", "HEAD")
        parent = base / "parent"
        parent.mkdir()
        git(parent, "init", "-q")
        git(parent, "submodule", "add", str(source), "mod")
        git(parent / "mod", "checkout", "--detach", self.recorded)
        git(parent, "add", ".")
        git(parent, "commit", "-qm", "pin submodule")
        self.root = base / "worktree"
        git(parent, "worktree", "add", "--detach", str(self.root))
        git(self.root, "submodule", "update", "--init")
        self.module = self.root / "mod"

    def run_setup(self):
        return subprocess.run(
            [sys.executable, "-I", worktree_setup.__file__, str(self.root)],
            env={**os.environ, "GIT_ALLOW_PROTOCOL": "file"},
            capture_output=True,
            text=True,
        )

    def test_repair_uses_recorded_gitlink_and_rerun_is_noop(self):
        git(self.module, "checkout", "--detach", self.newer)
        git(self.module, "rm", "a.txt")
        self.assertTrue(git(self.root, "submodule", "status").startswith("+"))
        self.assertEqual(worktree_setup.state(self.root, "mod"), "broken")
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(git(self.module, "rev-parse", "HEAD"), self.recorded)
        self.assertEqual(git(self.module, "status", "--porcelain"), "")
        self.assertEqual((self.module / "a.txt").read_text(), "pinned\n")
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "worktree already ready")

    def test_preserves_local_changes_and_moved_head(self):
        for change in ("moved", "unstaged deletion", "mixed", "untracked"):
            with self.subTest(change=change):
                git(self.module, "reset", "--hard", self.newer)
                if change == "unstaged deletion":
                    (self.module / "a.txt").unlink()
                elif change == "mixed":
                    git(self.module, "rm", "a.txt")
                    (self.module / "a.txt").write_text("local work\n")
                elif change == "untracked":
                    (self.module / "new.txt").write_text("local work\n")
                before = git(self.module, "status", "--porcelain")
                result = self.run_setup()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("warning: leaving mod", result.stderr)
                self.assertEqual(git(self.module, "rev-parse", "HEAD"), self.newer)
                self.assertEqual(git(self.module, "status", "--porcelain"), before)
                if change == "mixed":
                    self.assertEqual(
                        (self.module / "a.txt").read_text(), "local work\n"
                    )

    def test_repairs_staged_deletions_at_pin(self):
        git(self.module, "rm", "a.txt")
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(worktree_setup.state(self.root, "mod"), "ready")

    def test_missing_module_is_initialized(self):
        git(self.root, "submodule", "deinit", "-f", "mod")
        self.assertEqual(worktree_setup.state(self.root, "mod"), "missing")
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(worktree_setup.state(self.root, "mod"), "ready")

    def test_repair_must_leave_clean_recorded_commit(self):
        git(self.module, "rm", "a.txt")
        original = worktree_setup.git

        def skip_repair(root, *args, **kwargs):
            if args[:4] == ("submodule", "update", "--init", "--force"):
                return subprocess.CompletedProcess(args, 0, "", "")
            return original(root, *args, **kwargs)

        with patch.object(sys, "argv", ["worktree_setup.py", str(self.root)]):
            with patch.object(worktree_setup, "git", side_effect=skip_repair):
                with self.assertRaisesRegex(
                    SystemExit, "submodules still not ready: mod"
                ):
                    worktree_setup.main()

    def test_repair_tolerates_only_line_ending_changes(self):
        original = worktree_setup.git
        git(self.module, "config", "core.autocrlf", "false")
        for staged in (False, True):
            for content in (b"pinned\r\n", b"changed\r\n"):
                with self.subTest(staged=staged, content=content):
                    git(self.module, "reset", "--hard", self.recorded)
                    git(self.module, "rm", "a.txt")

                    def repair(root, *args, **kwargs):
                        result = original(root, *args, **kwargs)
                        if args[:4] == ("submodule", "update", "--init", "--force"):
                            (self.module / "a.txt").write_bytes(content)
                            if staged:
                                git(self.module, "add", "a.txt")
                        return result

                    with (
                        patch.object(
                            sys, "argv", ["worktree_setup.py", str(self.root)]
                        ),
                        patch.object(worktree_setup, "git", side_effect=repair),
                        patch.object(sys, "stderr", new_callable=io.StringIO) as stderr,
                    ):
                        if content == b"pinned\r\n":
                            worktree_setup.main()
                            self.assertIn(
                                "warning: mod: line-ending-only changes",
                                stderr.getvalue(),
                            )
                        else:
                            with self.assertRaisesRegex(
                                SystemExit, "submodules still not ready: mod"
                            ):
                                worktree_setup.main()


if __name__ == "__main__":
    unittest.main()
