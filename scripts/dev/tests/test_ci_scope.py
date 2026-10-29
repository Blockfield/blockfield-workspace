"""Exercise compilation scope against real git commits, including missing bases."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "ci_scope.py"
spec = importlib.util.spec_from_file_location("ci_scope", SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class CompilationScope(unittest.TestCase):
    def test_changed_inputs_and_unknown_bases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return subprocess.check_output(
                    ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "--quiet")
            git("config", "commit.gpgsign", "false")
            git("config", "user.name", "CI test")
            git("config", "user.email", "ci@example.invalid")
            git("commit", "--quiet", "--allow-empty", "-m", "base")
            base = git("rev-parse", "HEAD")
            for path, expected in (
                ("docs/README.md", False),
                (".gitmodules", False),
                ("scripts/dev/mcp/game.py", False),
                ("scripts/dev/testbot/Main.java", True),
                ("scripts/toolchain.py", True),
                ("scripts/dev_cache.py", True),
                (".github/workflows/standards.yml", True),
            ):
                with self.subTest(path=path):
                    file = root / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text("changed\n")
                    git("add", ".")
                    git("commit", "--quiet", "-m", "change")
                    self.assertEqual(
                        scope.needs_compile(base, root),
                        expected,
                    )
                    base = git("rev-parse", "HEAD")
            git("update-index", "--add", "--cacheinfo", f"160000,{base},client")
            git("commit", "--quiet", "-m", "pin")
            self.assertFalse(scope.needs_compile(base, root))
            base = git("rev-parse", "HEAD")
            git("rm", "scripts/dev/testbot/Main.java")
            git("commit", "--quiet", "-m", "delete")
            self.assertTrue(scope.needs_compile(base, root))
            for invalid in ("", "0" * 40, "f" * 40, "--help"):
                with self.subTest(base=invalid):
                    self.assertTrue(scope.needs_compile(invalid, root))
