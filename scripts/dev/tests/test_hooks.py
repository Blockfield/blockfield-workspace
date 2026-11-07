"""Versioned pre-push hook must stay wired and fast."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]

spec = importlib.util.spec_from_file_location(
    "setup_devstack", Path(__file__).parents[1] / "devstack.py"
)
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)

qspec = importlib.util.spec_from_file_location(
    "quality", ROOT / "scripts" / "quality.py"
)
quality = importlib.util.module_from_spec(qspec)
qspec.loader.exec_module(quality)


class PrePushHook(unittest.TestCase):
    def test_hook_runs_lint_and_format_check_without_gradle(self):
        hook = ROOT / ".githooks" / "pre-push"
        self.assertTrue(hook.is_file())
        self.assertTrue(hook.stat().st_mode & 0o111)
        text = hook.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("just lint", text)
        self.assertIn("just format-check", text)
        commands = [
            line for line in text.splitlines() if line and not line.startswith("#")
        ]
        self.assertFalse(any("gradle" in line.lower() for line in commands))

    def test_wire_hooks_points_checkout_at_versioned_hooks(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "--get" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="")
                return subprocess.CompletedProcess(args, 0, stdout="")

            with patch.object(devstack.subprocess, "run", side_effect=fake_run):
                self.assertTrue(devstack.wire_hooks(root))
            self.assertEqual(calls[-1][0], "git")
            self.assertIn(f"--git-dir={root / '.git'}", calls[-1])
            self.assertEqual(calls[-1][-3:], ["--local", "core.hooksPath", ".githooks"])

    def test_wire_hooks_leaves_explicit_choice_alone(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "--get" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=".custom\n")
                return subprocess.CompletedProcess(args, 0, stdout="")

            with patch.object(devstack.subprocess, "run", side_effect=fake_run):
                self.assertFalse(devstack.wire_hooks(root))
            self.assertEqual(len(calls), 1)

    def test_wire_hooks_leaves_subtree_without_own_git_alone(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            (root / ".githooks").mkdir()
            with patch.object(devstack.subprocess, "run") as execute:
                self.assertFalse(devstack.wire_hooks(root))
            execute.assert_not_called()

    def test_wire_hooks_leaves_checkouts_without_hooks_alone(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            with patch.object(devstack.subprocess, "run") as execute:
                self.assertFalse(devstack.wire_hooks(root))
            execute.assert_not_called()


class QualityWireHooks(unittest.TestCase):
    def test_wire_hooks_only_sets_when_unset(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "--get" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="")
                return subprocess.CompletedProcess(args, 0, stdout="")

            with patch.object(quality.subprocess, "run", side_effect=fake_run):
                self.assertTrue(quality.wire_hooks(root))
            self.assertEqual(calls[-1][0], "git")
            self.assertIn(f"--git-dir={root / '.git'}", calls[-1])
            self.assertEqual(calls[-1][-3:], ["--local", "core.hooksPath", ".githooks"])

    def test_wire_hooks_leaves_explicit_choice_alone(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "--local" in args and "--get" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=".custom\n")
                return subprocess.CompletedProcess(args, 0, stdout="")

            with patch.object(quality.subprocess, "run", side_effect=fake_run):
                self.assertFalse(quality.wire_hooks(root))
            self.assertEqual(len(calls), 1)

    def test_wire_hooks_prefers_local_over_outer_scope(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "--local" in args and "--get" in args:
                    return subprocess.CompletedProcess(args, 1, stdout="")
                if "--get" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=".other\n")
                return subprocess.CompletedProcess(args, 0, stdout="")

            with patch.object(quality.subprocess, "run", side_effect=fake_run):
                self.assertTrue(quality.wire_hooks(root))
            self.assertEqual(calls[-1][0], "git")
            self.assertIn(f"--git-dir={root / '.git'}", calls[-1])
            self.assertEqual(calls[-1][-3:], ["--local", "core.hooksPath", ".githooks"])

    def test_wire_hooks_reports_already_wired_as_success(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            subprocess.run(
                ["git", "config", "core.hooksPath", ".githooks"],
                cwd=root,
                check=True,
            )
            self.assertTrue(quality.wire_hooks(root))

    def test_wire_hooks_warns_every_time_it_supersedes_outer_hooks(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (root / ".githooks").mkdir()
            subprocess.run(
                ["git", "config", "core.hooksPath", ".githooks"],
                cwd=root,
                check=True,
            )
            calls = []

            def fake_run(args, **kwargs):
                calls.append(args)
                if "--local" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=".githooks\n")
                if "--global" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=".outer\n")
                return subprocess.CompletedProcess(args, 1, stdout="")

            with patch.object(quality.subprocess, "run", side_effect=fake_run):
                self.assertTrue(quality.wire_hooks(root))
            self.assertEqual(len(calls), 2)
            self.assertFalse(
                any("--local" in c and c[-1] == ".githooks" for c in calls)
            )

    def test_wire_hooks_leaves_outer_repo_alone_on_invalid_gitdir(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            outer = Path(tmp)
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            subprocess.run(
                ["git", "config", "core.hooksPath", "/outer/hooks"],
                cwd=outer,
                check=True,
            )
            sub = outer / "sub"
            sub.mkdir()
            (sub / ".git").mkdir()  # invalid git dir: git would resolve upward
            (sub / ".githooks").mkdir()
            self.assertFalse(quality.wire_hooks(sub))
            probe = subprocess.run(
                ["git", "config", "--local", "--get", "core.hooksPath"],
                cwd=outer,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), "/outer/hooks")


class PreCommitHook(unittest.TestCase):
    def test_hook_formats_staged_files_without_gradle(self):
        hook = ROOT / ".githooks" / "pre-commit"
        self.assertTrue(hook.is_file())
        mode = subprocess.run(
            ["git", "ls-files", "-s", ".githooks/pre-commit"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertTrue(mode.startswith("100755"), mode)
        text = hook.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("just format-staged", text)
        commands = [
            line for line in text.splitlines() if line and not line.startswith("#")
        ]
        self.assertFalse(any("gradle" in line.lower() for line in commands))

    def test_commit_formats_staged_files(self):
        # Fail-before: without the hook + format-staged, the raw bytes commit.
        with tempfile.TemporaryDirectory(prefix="bf precommit ") as tmp:
            root = Path(tmp)
            for name in (
                ".gitignore",
                "justfile",
                "scripts/quality.py",
                "scripts/toolchain.py",
                "scripts/terminal.py",
                "scripts/dev_cache.py",
            ):
                src = ROOT / name
                dst = root / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            (root / ".githooks").mkdir()
            (root / ".githooks" / "pre-commit").write_bytes(
                (ROOT / ".githooks" / "pre-commit").read_bytes()
            )
            (root / ".githooks" / "pre-commit").chmod(0o755)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                ).stdout

            git("init", "--quiet")
            git("config", "user.email", "hook-test@example.com")
            git("config", "user.name", "hook-test")
            git("config", "core.hooksPath", str(root / ".githooks"))
            git("add", ".")
            git("commit", "--quiet", "-m", "fixture")
            (root / "probe.py").write_text("x=1\n", encoding="utf-8")
            (root / "probe.sh").write_text(
                "if [ 1 = 1 ] ; then echo hi ; fi\n", encoding="utf-8"
            )
            git("add", "probe.py", "probe.sh")
            git("commit", "--quiet", "-m", "probe")
            formatted_py = git("show", "HEAD:probe.py")
            formatted_sh = git("show", "HEAD:probe.sh")
            self.assertNotEqual(formatted_py, "x=1\n")
            self.assertNotEqual(formatted_sh, "if [ 1 = 1 ] ; then echo hi ; fi\n")
            self.assertEqual(
                subprocess.run(
                    ["just", "format-check"],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=300,
                ).returncode,
                0,
            )

    def test_commit_leaves_partially_staged_files_alone(self):
        # Fail-before: the old hook formatted the worktree copy and staged it
        # whole, committing hunks the user never staged.
        with tempfile.TemporaryDirectory(prefix="bf precommit ") as tmp:
            root = Path(tmp)
            for name in (
                ".gitignore",
                "justfile",
                "scripts/quality.py",
                "scripts/toolchain.py",
                "scripts/terminal.py",
                "scripts/dev_cache.py",
            ):
                src = ROOT / name
                dst = root / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
            (root / ".githooks").mkdir()
            (root / ".githooks" / "pre-commit").write_bytes(
                (ROOT / ".githooks" / "pre-commit").read_bytes()
            )
            (root / ".githooks" / "pre-commit").chmod(0o755)

            def git(*args):
                return subprocess.run(
                    ["git", *args],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120,
                ).stdout

            git("init", "--quiet")
            git("config", "user.email", "hook-test@example.com")
            git("config", "user.name", "hook-test")
            git("config", "core.hooksPath", str(root / ".githooks"))
            git("add", ".")
            git("commit", "--quiet", "-m", "fixture")
            (root / "probe.py").write_text("x=1\n", encoding="utf-8")
            git("add", "probe.py")
            (root / "probe.py").write_text(
                "x=1\n# scratch, never staged\n", encoding="utf-8"
            )
            git("commit", "--quiet", "-m", "probe")
            self.assertEqual(git("show", "HEAD:probe.py"), "x=1\n")
            self.assertEqual(
                (root / "probe.py").read_text(encoding="utf-8"),
                "x=1\n# scratch, never staged\n",
            )


class WireHooksStdout(unittest.TestCase):
    def test_wiring_notice_stays_off_stdout(self):
        # CI captures `quality.py run ... ` stdout into $GITHUB_OUTPUT; a notice there broke it.
        import contextlib
        import io

        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            subprocess.run(["git", "init", "--quiet", tmp], check=True)
            (Path(tmp) / ".githooks").mkdir()
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertTrue(quality.wire_hooks(Path(tmp)))
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
