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
            with patch.object(devstack.subprocess, "run") as execute:
                self.assertTrue(devstack.wire_hooks(root))
            execute.assert_called_once()
            self.assertEqual(
                execute.call_args.args[0],
                ["git", "config", "core.hooksPath", ".githooks"],
            )
            self.assertEqual(execute.call_args.kwargs["cwd"], root)

    def test_wire_hooks_leaves_checkouts_without_hooks_alone(self):
        with tempfile.TemporaryDirectory(prefix="bf hooks ") as tmp:
            root = Path(tmp)
            with patch.object(devstack.subprocess, "run") as execute:
                self.assertFalse(devstack.wire_hooks(root))
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
