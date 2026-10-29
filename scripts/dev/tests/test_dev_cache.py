"""Private materializations share bytes, never mutations."""

import errno
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import dev_cache  # noqa: E402


class DevCache(unittest.TestCase):
    def test_cache_and_stands_are_independent_and_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jar"
            source.write_bytes(b"pinned bytes")
            source.chmod(0o755)
            with patch.object(dev_cache, "CACHE", root / "cache"):
                dev_cache.materialize(source, root / "first")
                dev_cache.materialize(source, root / "second")
                (root / "first").write_bytes(b"stand changes")
                self.assertEqual((root / "second").read_bytes(), source.read_bytes())
                self.assertEqual(
                    (root / "second").stat().st_mode, source.stat().st_mode
                )
                blob = (
                    root
                    / "cache/sha256"
                    / hashlib.sha256(source.read_bytes()).hexdigest()
                )
                self.assertEqual(blob.read_bytes(), source.read_bytes())
                blob.write_bytes(b"corrupted")
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    dev_cache.materialize(source, root / "third")
                self.assertFalse((root / "third").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux reflink")
    def test_unsupported_reflink_falls_back_but_disk_errors_propagate(self):
        import fcntl

        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "source", Path(tmp) / "destination"
            source.write_bytes(b"bytes")
            for code in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL):
                with patch.object(
                    fcntl, "ioctl", side_effect=OSError(code, "unsupported")
                ):
                    dev_cache.clone_file(source, destination)
                self.assertEqual(source.read_bytes(), destination.read_bytes())
            with patch.object(
                fcntl, "ioctl", side_effect=OSError(errno.ENOSPC, "full")
            ):
                with self.assertRaises(OSError):
                    dev_cache.clone_file(source, destination)

    def test_windows_uses_copy_without_fcntl(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, destination = Path(tmp) / "source", Path(tmp) / "destination"
            source.write_bytes(b"bytes")
            with patch.object(dev_cache.sys, "platform", "win32"):
                dev_cache.clone_file(source, destination)
            destination.write_bytes(b"changed")
            self.assertEqual(source.read_bytes(), b"bytes")
