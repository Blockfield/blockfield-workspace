import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import sync_tools  # noqa: E402 - sibling script under test lives next to the dev directory.


class SyncTools(unittest.TestCase):
    def test_sync_copies_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "sibling"
            (dest / "scripts").mkdir(parents=True)
            self.assertEqual(sync_tools.sync(dest), [])
            for relative in sync_tools.VENDORED:
                self.assertEqual(
                    (dest / relative).read_bytes(),
                    (sync_tools.ROOT / relative).read_bytes(),
                )
            self.assertEqual(sync_tools.drift(dest), [])

    def test_check_flags_drift_and_resync_repairs_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "sibling"
            (dest / "scripts").mkdir(parents=True)
            self.assertEqual(sync_tools.drift(dest), list(sync_tools.VENDORED))
            sync_tools.sync(dest)
            (dest / "scripts" / "toolchain.py").write_bytes(b"# mutated")
            self.assertEqual(sync_tools.drift(dest), ["scripts/toolchain.py"])
            self.assertEqual(sync_tools.main([str(dest), "--check"]), 1)
            self.assertEqual(sync_tools.main([str(dest)]), 0)
            self.assertEqual(sync_tools.drift(dest), [])

    def test_main_rejects_bad_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sync_tools.main([]), 2)
            self.assertEqual(sync_tools.main([tmp + "/missing"]), 2)


if __name__ == "__main__":
    unittest.main()
