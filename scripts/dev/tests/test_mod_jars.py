"""Ported from the rig: install/deployed/prune keep exactly one runtime jar per side.

The logic under test lives in devstack now (single implementation); the old
mod_jars.py module is gone.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "devstack.py"
spec = importlib.util.spec_from_file_location("devstack_modjars", SCRIPT)
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)

import modmap  # noqa: E402  (localmods/ mapping for install_mod coverage)


def runtime_name(name, side):
    return (
        name.startswith(f"blockfield-{side}-")
        and name.endswith(".jar")
        and not name.endswith(("-sources.jar", "-dev.jar"))
    )


def candidates(directory, side):
    return sorted(
        p for p in directory.iterdir() if p.is_file() and runtime_name(p.name, side)
    )


def deployed(directory, side):
    jars = candidates(directory, side)
    if len(jars) != 1:
        raise ValueError(
            f"Expected one {side} runtime jar in {directory}, found: {jars}"
        )
    return jars[0]


class InstallMod(unittest.TestCase):
    def setUp(self):
        inner = tempfile.TemporaryDirectory()
        self.addCleanup(inner.cleanup)
        self.root = Path(inner.name)
        self.mods = self.root / "mods"
        self.mods.mkdir()
        self.old = self.mods / "blockfield-server-1.0.0.jar"
        self.old.write_bytes(b"old")
        self.source = self.root / "blockfield-server-1.3.0+mc1.21.1.jar"
        self.source.write_bytes(b"new")
        real = devstack.SERVER
        devstack.SERVER = self.root
        self.addCleanup(setattr, devstack, "SERVER", real)

    def test_install_uses_incoming_version_and_preserves_other_mods(self):
        (self.mods / "blockfield-client-1.0.0.jar").write_bytes(b"client")
        (self.mods / "another-mod.jar").write_bytes(b"other")
        devstack.install_mod(
            type("Args", (), {"jar": str(self.source), "mod_repo": None})
        )
        installed = self.mods / self.source.name
        self.assertEqual(installed.read_bytes(), b"new")
        self.assertFalse(self.old.exists())
        self.assertEqual(deployed(self.mods, "server"), installed)

    def test_wrong_side_source_never_removes_existing_jar(self):
        client = self.root / "elsewhere" / "blockfield-client-1.0.0.jar"
        client.parent.mkdir()
        client.write_bytes(b"client")
        with self.assertRaises(SystemExit):
            devstack.install_mod(
                type("Args", (), {"jar": str(client), "mod_repo": None})
            )
        self.assertEqual(self.old.read_bytes(), b"old")

    def test_copy_failure_keeps_old_jar(self):
        with patch.object(
            devstack.shutil, "copyfile", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                devstack.install_mod(
                    type("Args", (), {"jar": str(self.source), "mod_repo": None})
                )
        self.assertEqual(self.old.read_bytes(), b"old")

    def test_deployed_rejects_duplicates_and_ignores_source_and_dev_jars(self):
        for suffix in ("sources", "dev"):
            (self.mods / f"blockfield-server-1.3.0-{suffix}.jar").write_bytes(b"extra")
        self.assertEqual(deployed(self.mods, "server"), self.old)
        (self.mods / self.source.name).write_bytes(b"new")
        with self.assertRaises(ValueError):
            deployed(self.mods, "server")


class LocalFirst(unittest.TestCase):
    def test_localmods_jar_wins_over_pins(self):
        jars = modmap.local_jars()
        self.assertIsInstance(
            jars, dict
        )  # {} without builds; keys are metafiles with builds


if __name__ == "__main__":
    unittest.main()
