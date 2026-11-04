"""Cloning the private server checkout is idempotent and needs no network."""

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "server_checkout", Path(__file__).parents[1] / "server_checkout.py"
)
server_checkout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_checkout)


def git(*args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class ServerCheckout(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "pins").mkdir()
        origin = self.root / "origin"
        origin.mkdir()
        git("init", "-q", cwd=origin)
        git("config", "user.email", "test@localhost", cwd=origin)
        git("config", "user.name", "test", cwd=origin)
        (origin / "pack.toml").write_text("[versions]\n", encoding="utf-8")
        git("add", "pack.toml", cwd=origin)
        git("commit", "-qm", "fixture", cwd=origin)
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (self.root / "pins" / "server.sha").write_text(self.sha + "\n")
        self.origin = origin

    def head(self):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root / "server",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_clone_checkout_and_idempotent_rerun(self):
        target = server_checkout.checkout_server(self.root, url=str(self.origin))
        self.assertTrue((target / "pack.toml").is_file())
        self.assertEqual(self.head(), self.sha)
        rerun = server_checkout.checkout_server(self.root, url=str(self.origin))
        self.assertEqual(rerun, target)
        self.assertEqual(self.head(), self.sha)

    def test_missing_pin_fails_loudly(self):
        (self.root / "pins" / "server.sha").unlink()
        with self.assertRaisesRegex(SystemExit, "pins/server.sha"):
            server_checkout.checkout_server(self.root, url=str(self.origin))

    def test_non_repo_server_dir_fails_loudly(self):
        (self.root / "server").mkdir()
        with self.assertRaisesRegex(SystemExit, "not a git checkout"):
            server_checkout.checkout_server(self.root, url=str(self.origin))

    def test_dirty_checkout_is_refused(self):
        server_checkout.checkout_server(self.root, url=str(self.origin))
        (self.root / "server" / "pack.toml").write_text("[versions]\ndirty\n")
        with self.assertRaisesRegex(SystemExit, "local changes"):
            server_checkout.checkout_server(self.root, url=str(self.origin))


if __name__ == "__main__":
    unittest.main()
