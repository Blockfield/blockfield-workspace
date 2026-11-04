"""Pinned-JDK regression: Gradle entrypoints ignore a wrong host java.

With a poisoned host (JAVA_HOME=/wrong, PATH java 17), every entrypoint that
forks Gradle must still export the Temurin 21 from scripts/toolchain.py.
No network, no Gradle: java_home() is mocked, subprocess.run is captured.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

Completed = subprocess.CompletedProcess

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import toolchain  # noqa: E402 - pinned JDK under test.

HERE = Path(__file__).resolve().parent
modmap_spec = importlib.util.spec_from_file_location(
    "modmap_jdk", HERE.parent / "modmap.py"
)
modmap = importlib.util.module_from_spec(modmap_spec)
modmap_spec.loader.exec_module(modmap)
watch_spec = importlib.util.spec_from_file_location(
    "watch_jdk", HERE.parent / "watch.py"
)
watch = importlib.util.module_from_spec(watch_spec)
watch_spec.loader.exec_module(watch)


class PinnedJdk(unittest.TestCase):
    def test_jdk_env_ignores_wrong_host_java(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = Path(tmp)
            (sdk / "bin").mkdir()
            poisoned = {
                "JAVA_HOME": "/wrong/jdk",
                "PATH": "/usr/bin:/bin",
                "JAVA_TOOL_OPTIONS": "-Xmx1g",
            }
            with patch.object(toolchain, "java_home", return_value=sdk):
                with patch.dict(os.environ, poisoned, clear=True):
                    env = toolchain.jdk_env()
            self.assertEqual(env["JAVA_HOME"], str(sdk))
            parts = env["PATH"].split(os.pathsep)
            self.assertEqual(parts[0], str(sdk / "bin"))
            self.assertEqual(parts[-2:], ["/usr/bin", "/bin"])
            self.assertNotIn("JAVA_TOOL_OPTIONS", env)

    def test_modmap_runner_exports_pinned_jdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = Path(tmp)
            seen = {}

            def fake_run(cmd, **kwargs):
                seen.update(kwargs)
                return Completed(cmd, 0, "", "")

            with patch.object(toolchain, "java_home", return_value=sdk):
                with patch("subprocess.run", side_effect=fake_run):
                    modmap._default_runner(["true"], cwd=tmp)
            self.assertEqual(seen["env"]["JAVA_HOME"], str(sdk))
            self.assertTrue(
                seen["env"]["PATH"].split(os.pathsep)[0] == str(sdk / "bin")
            )

    def test_remap_jar_defaults_to_pinned_jdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk, mod = Path(tmp), Path(tmp) / "mod"
            mod.mkdir()
            seen = {}

            def fake_run(cmd, **kwargs):
                seen.update(kwargs)
                return Completed(cmd, 0, "", "")

            with patch.object(toolchain, "java_home", return_value=sdk):
                with patch("subprocess.run", side_effect=fake_run):
                    self.assertIsNone(watch.remap_jar(mod, "server"))
            self.assertEqual(seen["env"]["JAVA_HOME"], str(sdk))
            self.assertTrue(
                seen["env"]["PATH"].split(os.pathsep)[0] == str(sdk / "bin")
            )


if __name__ == "__main__":
    unittest.main()
