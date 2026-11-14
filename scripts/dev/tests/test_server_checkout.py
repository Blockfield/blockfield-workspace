"""Cloning the dedicated and gameplay checkouts is idempotent and needs no network."""

import importlib.util
import subprocess
import tempfile
import unittest
import unittest.mock
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


def make_origin(root, name):
    origin = root / name
    origin.mkdir()
    git("init", "-q", cwd=origin)
    git("config", "user.email", "test@localhost", cwd=origin)
    git("config", "user.name", "test", cwd=origin)
    (origin / "pack.toml").write_text("[versions]\n", encoding="utf-8")
    git("add", "pack.toml", cwd=origin)
    git("commit", "-qm", "fixture", cwd=origin)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class CheckoutPair(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "pins").mkdir()
        self.dedicated = make_origin(self.root, "dedicated-origin")
        self.gameplay = make_origin(self.root, "gameplay-origin")
        (self.root / "pins" / "dedicated.sha").write_text(self.dedicated + "\n")
        (self.root / "pins" / "gameplay.sha").write_text(self.gameplay + "\n")
        self.patched = unittest.mock.patch.object(
            server_checkout,
            "CHECKOUTS",
            (
                ("server", "pins/dedicated.sha", str(self.root / "dedicated-origin")),
                ("gameplay", "pins/gameplay.sha", str(self.root / "gameplay-origin")),
            ),
        )
        self.patched.start()
        self.addCleanup(self.patched.stop)

    def head(self, dirname):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root / dirname,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_clone_checkout_and_idempotent_rerun(self):
        pair = server_checkout.checkout_pair(self.root)
        self.assertTrue((pair[0] / "pack.toml").is_file())
        self.assertTrue((pair[1] / "pack.toml").is_file())
        self.assertEqual(self.head("server"), self.dedicated)
        self.assertEqual(self.head("gameplay"), self.gameplay)
        rerun = server_checkout.checkout_pair(self.root)
        self.assertEqual(rerun, pair)
        self.assertEqual(self.head("server"), self.dedicated)
        self.assertEqual(self.head("gameplay"), self.gameplay)

    def test_missing_pin_fails_loudly(self):
        (self.root / "pins" / "dedicated.sha").unlink()
        with self.assertRaisesRegex(SystemExit, "pins/dedicated.sha"):
            server_checkout.checkout_pair(self.root)

    def test_non_repo_server_dir_fails_loudly(self):
        (self.root / "server").mkdir()
        with self.assertRaisesRegex(SystemExit, "not a git checkout"):
            server_checkout.checkout_repo(
                self.root,
                "server",
                str(self.root / "dedicated-origin"),
                "pins/dedicated.sha",
            )

    def test_old_blockfield_server_origin_is_repointed(self):
        # Review finding on the dedicated/gameplay repoint: a server/
        # checkout from the archived blockfield-server repo kept its
        # origin, so the fetch could never obtain the dedicated pin.
        legacy = make_origin(self.root, "blockfield-server")
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "legacy-only"],
            cwd=self.root / "blockfield-server",
            check=True,
        )
        legacy = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root / "blockfield-server",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(legacy, self.dedicated)
        subprocess.run(
            ["git", "clone", "-q", str(self.root / "blockfield-server"), "server"],
            cwd=self.root,
            check=True,
        )
        server_checkout.checkout_repo(
            self.root,
            "server",
            str(self.root / "dedicated-origin"),
            "pins/dedicated.sha",
        )
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=self.root / "server",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(origin, str(self.root / "dedicated-origin"))
        self.assertEqual(self.head("server"), self.dedicated)

    def test_dirty_checkout_is_refused(self):
        server_checkout.checkout_repo(
            self.root,
            "server",
            str(self.root / "dedicated-origin"),
            "pins/dedicated.sha",
        )
        (self.root / "server" / "pack.toml").write_text("[versions]\ndirty\n")
        with self.assertRaisesRegex(SystemExit, "local changes"):
            server_checkout.checkout_repo(
                self.root,
                "server",
                str(self.root / "dedicated-origin"),
                "pins/dedicated.sha",
            )


if __name__ == "__main__":
    unittest.main()
