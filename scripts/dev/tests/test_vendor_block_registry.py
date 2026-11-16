import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import release  # noqa: E402
import vendor_block_registry as vendor  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bump_mod_jar import offline  # noqa: E402

SHORT = "9331f58"
SHA = "9331f588539b89695a5de8c70dcc3bbca6340e5f"
REGISTRY = b'{\n  "blocks": ["minecraft:stone"]\n}\n'
DIGEST = hashlib.sha256(REGISTRY).hexdigest()


class FakeGit:
    """release.run stand-in: emulates the git calls vendor makes."""

    def __init__(self, branches=("origin/next", "origin/main"), registry=REGISTRY):
        self.branches = list(branches)
        self.registry = registry
        self.calls = []

    def fail(self, cmd, message, check):
        if check:
            raise RuntimeError(message)
        return CompletedProcess(cmd, 1, "", message)

    def __call__(self, *cmd, cwd=None, env=None, check=True):
        argv = [str(c) for c in cmd]
        self.calls.append(argv)
        sub = argv[argv.index("-C") + 2 :]
        if sub[:1] == ["fetch"]:
            return "" if check else CompletedProcess(cmd, 0, "", "")
        if sub[:2] == ["rev-parse", "--is-shallow-repository"]:
            return "false"
        if sub[:1] == ["rev-parse"]:
            if sub[1].removesuffix("^{commit}") in (SHORT, SHA):
                return SHA
            return self.fail(cmd, f"unknown revision {sub[1]}", check)
        if sub[:3] == ["branch", "-r", "--contains"]:
            if sub[3] != SHA:
                return self.fail(cmd, f"unknown revision {sub[3]}", check)
            return "\n".join(f"  {branch}" for branch in self.branches)
        if sub[:1] == ["show"]:
            if self.registry is None or sub[1] != f"{SHA}:{vendor.REGISTRY}":
                return self.fail(cmd, f"no such file {sub[1:]}", check)
            text = self.registry.decode("utf-8")
            return text if check else CompletedProcess(cmd, 0, text, "")
        raise AssertionError(f"unexpected git call: {argv}")


def setUpModule():
    unittest.enterModuleContext(offline())


class Vendor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gameplay = Path(tmp.name, "gameplay")
        self.dedicated = Path(tmp.name, "dedicated")
        (self.dedicated / vendor.REGISTRY).parent.mkdir(parents=True)
        self.git = FakeGit()

    def main(self, *args):
        """vendor.main with fake git; (exit code, stdout, stderr)."""
        with (
            mock.patch.object(release, "run", self.git),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = vendor.main(
                [
                    *args,
                    "--gameplay",
                    str(self.gameplay),
                    "--dedicated",
                    str(self.dedicated),
                ]
            )
        return code, out.getvalue(), err.getvalue()

    def test_vendors_the_registry_and_rewrites_the_pin(self):
        code, out, _ = self.main(SHORT)
        self.assertEqual(code, 0)
        self.assertEqual((self.dedicated / vendor.REGISTRY).read_bytes(), REGISTRY)
        self.assertEqual((self.dedicated / vendor.PIN).read_text(), f"{SHA} {DIGEST}\n")
        self.assertIn(f"from gameplay {SHA}", out)
        self.assertIn(f"{SHA} {DIGEST}", out)

    def test_full_sha_is_accepted(self):
        code, _, _ = self.main(SHA)
        self.assertEqual(code, 0)
        self.assertIn(SHA, (self.dedicated / vendor.PIN).read_text())

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.main(SHORT, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would vend", out)
        self.assertFalse((self.dedicated / vendor.PIN).exists())
        self.assertFalse((self.dedicated / vendor.REGISTRY).exists())

    def test_sha_off_next_and_main_is_an_error(self):
        self.git.branches = []
        code, _, err = self.main(SHORT)
        self.assertEqual(code, 1)
        self.assertIn("not on gameplay", err)
        self.assertFalse((self.dedicated / vendor.PIN).exists())

    def test_unknown_sha_is_an_error(self):
        code, _, err = self.main("deadbee")
        self.assertEqual(code, 1)
        self.assertIn("not a commit", err)
        self.assertFalse((self.dedicated / vendor.PIN).exists())

    def test_missing_registry_at_the_sha_is_an_error(self):
        self.git.registry = None
        code, _, err = self.main(SHORT)
        self.assertEqual(code, 1)
        self.assertIn("cannot read", err)
        self.assertFalse((self.dedicated / vendor.PIN).exists())

    def test_missing_dedicated_checkout_is_an_error(self):
        (self.dedicated / vendor.REGISTRY).parent.rmdir()
        code, _, err = self.main(SHORT)
        self.assertEqual(code, 1)
        self.assertIn("dedicated checkout missing", err)


if __name__ == "__main__":
    unittest.main()
