"""Pruning is explicit, bounded to task stands, and refuses live users."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prune_dev  # noqa: E402


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process inspection")
class Prune(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.stand = self.root / ".dev/task"
        self.stand.mkdir(parents=True)
        self.patch = patch.object(prune_dev, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        original = Path.iterdir
        self.proc_patch = patch.object(
            Path,
            "iterdir",
            lambda path: (
                iter([Path("/proc") / str(os.getpid())])
                if path == Path("/proc")
                else original(path)
            ),
        )
        self.proc_patch.start()
        self.addCleanup(self.proc_patch.stop)

    def file(self, name):
        path = self.stand / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        return path

    def test_evidence_kept_runtime_removed_symlinks_never_followed(self):
        evidence = [
            self.file(name)
            for name in (
                "report.md",
                "logs/latest.log",
                "logs/server.out",
                "evidence/frame.png",
                "jobs/run/first-client.out",
                "SHA256SUMS",
            )
        ]
        runtime = [
            self.file(name)
            for name in (
                "jdk/release.json",
                "server/mods/mod.jar",
                "play/saves/world/level.dat",
            )
        ]
        outside = self.root / "outside"
        outside.mkdir()
        secret = outside / "keep.txt"
        secret.write_bytes(b"untouched")
        (self.stand / "linked").symlink_to(outside, target_is_directory=True)
        (self.stand / "linked.txt").symlink_to(secret)
        prune_dev.prune(self.stand)
        self.assertTrue(all(path.exists() for path in evidence))
        self.assertFalse(any(path.exists() for path in runtime))
        self.assertEqual(secret.read_bytes(), b"untouched")
        self.assertFalse((self.stand / "linked.txt").is_symlink())
        self.assertTrue((self.stand / "PRUNED.json").is_file())

    def test_keep_and_live_record_refuse_before_deletion(self):
        runtime = self.file("mod.jar")
        keep = self.file(".keep")
        with self.assertRaisesRegex(ValueError, "keep"):
            prune_dev.prune(self.stand)
        keep.unlink()
        ticks = int(
            Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]
        )
        (self.stand / "display.pid").write_text(
            json.dumps({"pid": os.getpid(), "start_ticks": ticks})
        )
        with self.assertRaisesRegex(ValueError, "in use"):
            prune_dev.prune(self.stand)
        self.assertTrue(runtime.exists())

    def test_reused_pid_with_different_birth_is_not_a_live_record(self):
        self.file("mod.jar")
        (self.stand / "server.pid").write_text(
            json.dumps({"pid": os.getpid(), "start_ticks": 1})
        )
        prune_dev.prune(self.stand)
        self.assertFalse((self.stand / "mod.jar").exists())

    def test_open_fd_refuses_without_a_record(self):
        runtime = self.file("mod.jar")
        with runtime.open("rb"):
            with self.assertRaisesRegex(ValueError, "in use"):
                prune_dev.prune(self.stand)
        self.assertTrue(runtime.exists())

    def test_unreadable_processes_are_skipped_not_blocking(self):
        runtime = self.file("mod.jar")
        with patch.object(
            prune_dev.os, "readlink", side_effect=PermissionError("restricted")
        ):
            prune_dev.prune(self.stand)
        self.assertFalse(runtime.exists())

    def test_protected_outside_and_symlink_stands_refused(self):
        for name in ("server", "jdk", "worktrees", "coord-test", "jobs"):
            path = self.root / ".dev" / name
            path.mkdir()
            with self.assertRaises(ValueError):
                prune_dev.prune(path)
        (self.root / ".dev/alias").symlink_to(self.stand, target_is_directory=True)
        for path in (
            self.root,
            self.root / ".dev",
            self.root / ".dev/alias",
            self.root / ".dev/worktrees/task",
        ):
            with self.assertRaises(ValueError):
                prune_dev.prune(path)

    def test_dry_run_does_not_delete_and_apply_requires_paths(self):
        runtime = self.file("mod.jar")
        with patch.object(sys, "argv", ["prune"]):
            self.assertEqual(prune_dev.main(), 0)
        self.assertTrue(runtime.exists())
        with patch.object(sys, "argv", ["prune", "--apply"]):
            with self.assertRaises(SystemExit):
                prune_dev.main()
