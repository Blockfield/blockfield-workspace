"""Checkout rules must survive developer autocrlf settings without touching binaries."""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class LineEndings(unittest.TestCase):
    def test_git_normalizes_text_preserves_windows_scripts_and_binary_bytes(self):
        for autocrlf in ("true", "false"):
            with self.subTest(autocrlf=autocrlf), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)

                def git(*args):
                    return subprocess.run(
                        ["git", "-c", f"core.autocrlf={autocrlf}", *args],
                        cwd=root,
                        check=True,
                        capture_output=True,
                        timeout=15,
                    ).stdout

                git("init")
                (root / ".gitattributes").write_bytes(
                    (ROOT / ".gitattributes").read_bytes()
                )
                names = ("source.py", "settings.json", "run.bat", "run.cmd")
                binary = ("image.png", "generated.class", "archive.jar", "map.schem")
                for name in (*names, *binary):
                    (root / name).write_bytes(b"first\r\nsecond\r\n")
                git("add", ".")
                for name in (*names, *binary):
                    expected = (
                        b"first\r\nsecond\r\n"
                        if name in binary or name.endswith((".bat", ".cmd"))
                        else b"first\nsecond\n"
                    )
                    self.assertEqual(git("show", f":{name}"), expected, name)
                    (root / name).unlink()
                git("checkout", "--", *names, *binary)
                for name in (*names, *binary):
                    crlf = name in binary or name.endswith((".bat", ".cmd"))
                    expected = b"first\r\nsecond\r\n" if crlf else b"first\nsecond\n"
                    self.assertEqual((root / name).read_bytes(), expected, name)
