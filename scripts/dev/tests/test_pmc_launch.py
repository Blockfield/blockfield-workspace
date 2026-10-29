import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location(
    "pmc_launch", Path(__file__).parents[1] / "pmc_launch.py"
)
pmc_launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pmc_launch)


class LaunchCommand(unittest.TestCase):
    def test_uses_the_project_java_and_loopback_debug_port(self):
        version = Mock()
        version.install.return_value = SimpleNamespace(
            jvm_args=["project-java"],
            native_libs=[],
            args_replacements={},
            main_class="MinecraftMain",
            game_args=[],
        )
        modules = {
            "portablemc.standard": SimpleNamespace(
                Context=Mock(), replace_list_vars=lambda args, _: args
            ),
            "portablemc.fabric": SimpleNamespace(
                FabricVersion=SimpleNamespace(with_fabric=Mock(return_value=version))
            ),
        }
        output = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(sys.modules, modules),
            patch.object(
                pmc_launch.shutil, "which", return_value="/project/jdk/bin/java"
            ),
            patch.object(
                sys,
                "argv",
                [
                    "pmc_launch",
                    "--main-dir",
                    tmp,
                    "--game-dir",
                    tmp,
                    "--natives-dir",
                    str(Path(tmp) / "natives"),
                    "--username",
                    "Tester",
                    "--debug-port",
                    "5006",
                ],
            ),
            contextlib.redirect_stdout(output),
        ):
            pmc_launch.main()
        self.assertEqual(version.jvm_path, Path("/project/jdk/bin/java"))
        self.assertIn(
            "-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:5006",
            json.loads(output.getvalue()),
        )

    def test_installs_selected_versions_and_extracts_natives_without_launching_java(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "natives.jar"
            with ZipFile(archive, "w") as source:
                source.writestr("lib/libsample.so", b"native bytes")
                source.writestr("META-INF/manifest", b"not a native")
            version = Mock()
            version.install.return_value = SimpleNamespace(
                jvm_args=["project-java"],
                native_libs=[archive],
                args_replacements={},
                main_class="MinecraftMain",
                game_args=[],
            )
            factory = Mock(return_value=version)
            modules = {
                "portablemc.standard": SimpleNamespace(
                    Context=Mock(), replace_list_vars=lambda args, _: args
                ),
                "portablemc.fabric": SimpleNamespace(
                    FabricVersion=SimpleNamespace(with_fabric=factory)
                ),
            }
            output = io.StringIO()
            with (
                patch.dict(sys.modules, modules),
                patch.object(
                    pmc_launch.shutil, "which", return_value="/project/jdk/bin/java"
                ),
                patch.object(
                    sys,
                    "argv",
                    [
                        "pmc_launch",
                        "--main-dir",
                        str(root / "mc"),
                        "--game-dir",
                        str(root / "game"),
                        "--natives-dir",
                        str(root / "natives"),
                        "--username",
                        "Setup",
                        "--minecraft",
                        "test-minecraft",
                        "--fabric",
                        "test-fabric",
                    ],
                ),
                contextlib.redirect_stdout(output),
            ):
                pmc_launch.main()
            self.assertEqual(factory.call_args.args, ("test-minecraft", "test-fabric"))
            version.install.assert_called_once_with()
            self.assertEqual(
                (root / "natives/libsample.so").read_bytes(), b"native bytes"
            )
            self.assertFalse((root / "natives/manifest").exists())
            self.assertEqual(json.loads(output.getvalue())[1], "-Xmx4G")


if __name__ == "__main__":
    unittest.main()
