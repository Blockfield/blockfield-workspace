"""Execute the real Java input lease checks without Minecraft, Gradle or a desktop."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import toolchain  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


class RawInput(unittest.TestCase):
    def test_client_ownership_expiry_release_and_cursor_bounds(self):
        with tempfile.TemporaryDirectory() as output:
            env = toolchain.child_env()
            subprocess.run(
                [
                    toolchain.native("javac"),
                    "-J-Xmx256m",
                    "--release",
                    "21",
                    "-Xlint:all",
                    "-Werror",
                    "-d",
                    output,
                    str(
                        ROOT
                        / "scripts/dev/testbot/src/main/java/bf/testbot/RawInputState.java"
                    ),
                ],
                env=env,
                check=True,
                timeout=60,
            )
            subprocess.run(
                [
                    toolchain.native("java"),
                    "-Xmx128m",
                    "-cp",
                    output,
                    "bf.testbot.RawInputState",
                ],
                env=env,
                check=True,
                timeout=60,
            )
