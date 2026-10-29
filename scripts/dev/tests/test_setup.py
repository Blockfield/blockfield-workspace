"""Setup prepares an isolated stack without starting a game or external tools."""

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "setup_devstack", Path(__file__).parents[1] / "devstack.py"
)
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)


class Setup(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root = Path(
            self.context.enter_context(tempfile.TemporaryDirectory())
        ).resolve()
        self.context.enter_context(
            patch.dict(devstack.os.environ, {"XDG_RUNTIME_DIR": str(self.root)})
        )
        self.home = self.root / "stack with spaces"
        self.server = self.home / "server"
        self.game = self.home / "play"
        self.client_pack = self.root / "client pack"
        self.client_pack.mkdir()
        (self.client_pack / "pack.toml").write_text(
            '[versions]\nminecraft = "1.21.1"\nfabric = "0.19.3"\n'
        )
        self.args = SimpleNamespace(
            modpack=None,
            client_modpack=None,
            game_dir=None,
            force=False,
            testbot=True,
            no_testbot=False,
        )
        self.contract = "current-contract"
        self.client_digest = "c" * 64
        self.bot = self.root / "bftestbot.jar"
        self.bot.write_bytes(b"current testbot")
        self.carpet = self.root / devstack.CARPET_ASSET[0]
        self.carpet.write_bytes(b"pinned carpet")
        for name, value in {
            "HOME": self.home,
            "SERVER": self.server,
            "PID_FILE": self.home / "server.pid",
            "CLIENT_PID_FILE": self.home / "client.pid",
        }.items():
            self.context.enter_context(patch.object(devstack, name, value))
        self.mocks = {}
        for name, value in {
            "server_pid": None,
            "client_pid": None,
            "process_record": None,
            "port_open": False,
            "java_exe": "/project/jdk/bin/java",
            "download": self.carpet,
            "testbot_jar": self.bot,
            "pmc_python": "pmc-python",
            "install_client_localmods": None,
            "build_testbot": None,
            "start_process": None,
        }.items():
            self.mocks[name] = self.context.enter_context(
                patch.object(devstack, name, return_value=value)
            )
        self.context.enter_context(
            patch.object(
                devstack,
                "modpack_dir",
                side_effect=lambda argument=None, side="server": self.client_pack,
            )
        )
        self.server_build = self.context.enter_context(
            patch.object(devstack, "build_distribution", side_effect=self.build_server)
        )
        self.client_build = self.context.enter_context(
            patch.object(devstack, "build_client_pack", side_effect=self.build_client)
        )
        self.command = self.context.enter_context(
            patch.object(
                devstack.subprocess,
                "run",
                return_value=SimpleNamespace(
                    stdout=json.dumps(["/project/jdk/bin/java", "MinecraftMain"]),
                    returncode=0,
                ),
            )
        )

    def test_minecraft_snapshot_is_cloned_before_install_and_stays_private(self):
        cache = self.root / "inputs"
        first = self.root / "first"
        second = self.root / "second"
        expected = b"pinned minecraft library"
        cached = cache / "minecraft" / f"1.21.1-0.19.3-{sys.platform}"
        (cached / "libraries").mkdir(parents=True)
        (cached / "libraries/library.jar").write_bytes(expected)

        def install(*args, **kwargs):
            library = devstack.HOME / "mc/libraries/library.jar"
            self.assertTrue(library.is_file(), "snapshot was not cloned before install")
            self.assertEqual(library.read_bytes(), expected)
            return SimpleNamespace(stdout=json.dumps(["/project/jdk/bin/java"]))

        with (
            patch.object(devstack, "INPUT_CACHE", cache),
            patch.object(devstack, "run", side_effect=install),
        ):
            for home in (first, second):
                home.mkdir()
                with patch.object(devstack, "HOME", home):
                    devstack.client_launch_command(
                        self.args, home / "play", "Setup", self.client_pack, None
                    )
                self.assertEqual(
                    (home / "mc/libraries/library.jar").read_bytes(), expected
                )
                (home / "mc/libraries/library.jar").write_bytes(b"private change")
        self.assertEqual((cached / "libraries/library.jar").read_bytes(), expected)
        self.assertEqual(
            (second / "mc/libraries/library.jar").read_bytes(), b"private change"
        )

    def build_server(self, _pack, _client_pack):
        out = self.home / "dist"
        built = out / "server"
        for name in ("mods", "config", "world", "blockfield", "maps"):
            (built / name).mkdir(parents=True)
        (built / "fabric-server-launch.jar").write_bytes(b"launcher")
        (built / "mods/current.jar").write_bytes(b"new server mod")
        (built / "config/blockfield-pack-version.txt").write_text("server-marker\n")
        (out / "build.json").write_text(
            json.dumps(
                {
                    "compatibility": self.contract,
                    "mods": [
                        {
                            "path": "mods/current.jar",
                            "sha256": hashlib.sha256(b"new server mod").hexdigest(),
                        }
                    ],
                }
            )
        )
        return out

    def build_client(self, _pack, out):
        out.mkdir(parents=True, exist_ok=True)
        (out / "web").mkdir()
        (out / "web/build.json").write_text(
            json.dumps({"compatibility": self.contract, "client": self.client_digest})
        )
        with zipfile.ZipFile(out / "Blockfield-client.zip", "w") as archive:
            archive.writestr("mods/current.jar", b"new client mod")
            archive.writestr("config/blockfield-pack-version.txt", "client-marker\n")
            archive.writestr("options.txt", "default options")
            archive.writestr("logs/latest.log", "archive log must not replace evidence")

    def test_fresh_setup_prepares_both_sides_and_command_without_launching(self):
        devstack.setup(self.args)
        self.assertTrue((self.server / "fabric-server-launch.jar").is_file())
        self.assertEqual(
            (self.server / "mods" / self.carpet.name).read_bytes(), b"pinned carpet"
        )
        self.assertFalse((self.game / "mods" / self.carpet.name).exists())
        self.assertNotIn(self.carpet.name, (self.server / "build.json").read_text())
        self.mocks["download"].assert_called_once_with(
            *devstack.CARPET_ASSET, cache=devstack.INPUT_CACHE / "downloads"
        )
        self.assertEqual(
            (self.game / "mods/current.jar").read_bytes(), b"new client mod"
        )
        self.assertEqual(
            (self.game / "mods/bftestbot.jar").read_bytes(), b"current testbot"
        )
        self.assertEqual(
            (self.game / "config/blockfield-pack-version.txt").read_text(),
            "server-marker\n",
        )
        self.mocks["build_testbot"].assert_called_once_with(self.args)
        self.mocks["start_process"].assert_not_called()
        command = self.command.call_args.args[0]
        self.assertEqual(command[command.index("--minecraft") + 1], "1.21.1")
        self.assertEqual(command[command.index("--fabric") + 1], "0.19.3")
        self.assertIn(str(self.home / "mc"), command)
        natives = Path(command[command.index("--natives-dir") + 1])
        if devstack.sys.platform == "linux":
            self.assertEqual(natives.parent, self.root)
            self.assertFalse(any(c.isspace() for c in str(natives.resolve())))
        else:
            self.assertEqual(natives, self.home / "play-natives")
        self.assertFalse((self.home / "client.cmd").exists())

    def test_repeat_repairs_missing_client_files_and_refreshes_testbot_without_server_rebuild(
        self,
    ):
        devstack.setup(self.args)
        self.server_build.reset_mock()
        self.mocks["build_testbot"].reset_mock()
        (self.game / "mods/current.jar").unlink()
        (self.game / "mods/bftestbot.jar").write_bytes(b"stale bot")
        devstack.setup(self.args)
        self.server_build.assert_not_called()
        self.mocks["build_testbot"].assert_called_once_with(self.args)
        self.assertEqual(
            (self.game / "mods/current.jar").read_bytes(), b"new client mod"
        )
        self.assertEqual(
            (self.game / "mods/bftestbot.jar").read_bytes(), b"current testbot"
        )

    def test_repeat_repairs_missing_or_corrupted_server_mods(self):
        devstack.setup(self.args)
        jar = self.server / "mods/current.jar"
        for damaged in (None, b"corrupted server jar"):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    jar.unlink()
                else:
                    jar.write_bytes(damaged)
                self.server_build.reset_mock()
                devstack.setup(self.args)
                self.server_build.assert_called_once()
                self.assertEqual(jar.read_bytes(), b"new server mod")

    def test_repeat_repairs_carpet_and_removes_old_versions_without_server_rebuild(
        self,
    ):
        devstack.setup(self.args)
        jar = self.server / "mods" / self.carpet.name
        for damaged in (None, b"corrupted carpet"):
            with self.subTest(damaged=damaged):
                if damaged is None:
                    jar.unlink()
                else:
                    jar.write_bytes(damaged)
                old = self.server / "mods/fabric-carpet-old.jar"
                old.write_bytes(b"old carpet")
                self.server_build.reset_mock()
                devstack.setup(self.args)
                self.server_build.assert_not_called()
                self.assertEqual(jar.read_bytes(), b"pinned carpet")
                self.assertFalse(old.exists())
                self.assertFalse((self.game / "mods" / jar.name).exists())

    def test_carpet_download_failure_preserves_installed_version(self):
        devstack.setup(self.args)
        jar = self.server / "mods" / self.carpet.name
        old = self.server / "mods/fabric-carpet-old.jar"
        old.write_bytes(b"old carpet")
        self.mocks["download"].side_effect = RuntimeError("Tool checksum mismatch")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            devstack.setup(self.args)
        self.assertEqual(jar.read_bytes(), b"pinned carpet")
        self.assertEqual(old.read_bytes(), b"old carpet")

    def test_proxy_only_mods_are_validated_in_mods_off(self):
        devstack.setup(self.args)
        path = self.server / "mods-off/FabricProxy-Lite.jar"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"proxy mod")
        receipt = self.server / "build.json"
        manifest = json.loads(receipt.read_text())
        manifest["mods"].append(
            {
                "path": "mods/FabricProxy-Lite.jar",
                "sha256": hashlib.sha256(b"proxy mod").hexdigest(),
            }
        )
        receipt.write_text(json.dumps(manifest))
        self.assertTrue(devstack.server_runtime_ready())
        path.write_bytes(b"corrupted proxy mod")
        self.assertFalse(devstack.server_runtime_ready())

    def test_client_only_digest_change_requires_force_before_merging_mods(self):
        devstack.setup(self.args)
        previous = (self.game / "build.json").read_bytes()
        stale = self.game / "mods/old-client-only.jar"
        stale.write_bytes(b"old client-only mod")
        self.client_digest = "d" * 64
        with self.assertRaises(SystemExit):
            devstack.setup(self.args)
        self.assertEqual((self.game / "build.json").read_bytes(), previous)
        self.assertEqual(stale.read_bytes(), b"old client-only mod")
        self.args.force = True
        devstack.setup(self.args)
        self.assertFalse(stale.exists())
        self.assertEqual(
            json.loads((self.game / "build.json").read_text())["client"],
            self.client_digest,
        )

    def test_legacy_client_without_receipt_refreshes_runtime_preserving_user_data(self):
        devstack.setup(self.args)
        (self.game / "build.json").unlink()
        (self.game / "mods/obsolete.jar").write_bytes(b"old")
        options = self.game / "options.txt"
        options.write_bytes(b"own options")
        self.server_build.reset_mock()
        devstack.setup(self.args)
        self.server_build.assert_not_called()
        self.assertFalse((self.game / "mods/obsolete.jar").exists())
        self.assertEqual(options.read_bytes(), b"own options")
        self.assertTrue((self.game / "build.json").is_file())

    def test_fresh_play_preparation_also_persists_the_client_receipt(self):
        devstack.prepare_client(self.args)
        self.assertEqual(
            json.loads((self.game / "build.json").read_text())["client"],
            self.client_digest,
        )
        self.mocks["start_process"].assert_not_called()

    def test_force_refreshes_runtime_and_preserves_worlds_and_client_user_data(self):
        devstack.setup(self.args)
        kept = {}
        for directory in ("world", "blockfield", "maps"):
            path = self.server / directory / "keep.dat"
            path.write_bytes(b"server data")
            kept[path] = path.read_bytes()
        for relative in (
            "options.txt",
            "logs/latest.log",
            "screenshots/frame.png",
            "saves/world/level.dat",
        ):
            path = self.game / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"client data")
            kept[path] = path.read_bytes()
        (self.game / "mods/obsolete.jar").write_bytes(b"old")
        (self.server / "mods/obsolete.jar").write_bytes(b"old")
        self.args.force = True
        devstack.setup(self.args)
        for path, data in kept.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse((self.game / "mods/obsolete.jar").exists())
        self.assertFalse((self.server / "mods/obsolete.jar").exists())
        self.assertEqual(
            (self.server / "mods" / self.carpet.name).read_bytes(), b"pinned carpet"
        )
        self.assertFalse((self.game / "mods" / self.carpet.name).exists())
        self.mocks["start_process"].assert_not_called()

    def test_force_restores_missing_built_dirs_preserving_user_worlds(self):
        devstack.setup(self.args)
        shutil.rmtree(self.server / "maps")
        user_world = self.server / "world/keep.dat"
        user_world.write_bytes(b"user world")
        self.args.force = True
        devstack.setup(self.args)
        self.assertTrue((self.server / "maps").is_dir())
        self.assertEqual(user_world.read_bytes(), b"user world")
        self.assertEqual(
            (self.server / "mods/current.jar").read_bytes(), b"new server mod"
        )

    def test_restore_missing_merges_without_overwriting(self):
        src = self.root / "src"
        dst = self.root / "dst"
        (src / "sub").mkdir(parents=True)
        (src / "sub/built.dat").write_bytes(b"built")
        (src / "new.dat").write_bytes(b"new")
        (dst / "sub").mkdir(parents=True)
        (dst / "sub/user.dat").write_bytes(b"user")
        devstack._restore_missing(src, dst)
        self.assertEqual((dst / "new.dat").read_bytes(), b"new")
        self.assertEqual((dst / "sub/built.dat").read_bytes(), b"built")
        self.assertEqual((dst / "sub/user.dat").read_bytes(), b"user")
        (src / "sub/built.dat").write_bytes(b"changed")
        devstack._restore_missing(src, dst)
        self.assertEqual((dst / "sub/built.dat").read_bytes(), b"built")

    def test_live_foreign_and_listener_guards_precede_any_preparation(self):
        for name, value in (
            ("server_pid", 123),
            ("client_pid", 456),
            ("process_record", {"owner": "foreign-session", "live": False}),
            ("port_open", True),
        ):
            with (
                self.subTest(guard=name),
                patch.object(devstack, name, return_value=value),
            ):
                with self.assertRaises(SystemExit):
                    devstack.setup(self.args)
        self.mocks["java_exe"].assert_not_called()
        self.mocks["download"].assert_not_called()
        self.server_build.assert_not_called()
        self.client_build.assert_not_called()
        self.mocks["build_testbot"].assert_not_called()
        self.assertFalse(self.home.exists())

    def test_changed_common_contract_requires_force_before_touching_client(self):
        devstack.setup(self.args)
        before = (self.game / "mods/current.jar").read_bytes()
        self.contract = "new-contract"
        with self.assertRaises(SystemExit):
            devstack.setup(self.args)
        self.assertEqual((self.game / "mods/current.jar").read_bytes(), before)
        self.args.force = True
        devstack.setup(self.args)

    def test_no_testbot_skips_build_and_install(self):
        self.args.no_testbot = True
        devstack.setup(self.args)
        self.mocks["build_testbot"].assert_not_called()
        self.assertFalse((self.game / "mods/bftestbot.jar").exists())

    def test_default_setup_and_subsequent_play_preparation_leave_bot_disabled(self):
        self.args.testbot = None
        devstack.setup(self.args)
        self.args.no_testbot = False
        devstack.prepare_client(self.args)
        self.mocks["build_testbot"].assert_not_called()
        self.mocks["testbot_jar"].assert_not_called()
        self.assertFalse((self.game / "mods/bftestbot.jar").exists())
        self.assertNotIn("--testbot-port=47777", self.command.call_args.args[0])

    def test_explicit_jar_skips_build_and_refresh_keeps_it_until_no_testbot(self):
        self.args.testbot = str(self.bot)
        devstack.setup(self.args)
        self.mocks["build_testbot"].assert_not_called()
        self.assertTrue((self.game / "mods/bftestbot.jar").exists())
        self.args.testbot = None
        self.args.force = True
        devstack.setup(self.args)
        self.assertEqual(
            (self.game / "mods/bftestbot.jar").read_bytes(), b"current testbot"
        )
        self.args.force = False
        self.args.no_testbot = True
        devstack.setup(self.args)
        self.assertFalse((self.game / "mods/bftestbot.jar").exists())

    def test_setup_testbot_flag_accepts_opt_in_without_a_jar(self):
        with (
            patch.object(devstack.sys, "argv", ["devstack", "setup", "--testbot"]),
            patch.object(devstack, "setup") as setup,
        ):
            with self.assertRaises(SystemExit):
                devstack.main()
        self.assertIs(setup.call_args.args[0].testbot, True)

    def test_cli_refuses_busy_stack_before_creating_the_runtime_directory(self):
        self.mocks["port_open"].return_value = True
        with patch.object(devstack.sys, "argv", ["devstack", "setup"]):
            with self.assertRaises(SystemExit):
                devstack.main()
        self.assertFalse(self.home.exists())
        self.mocks["java_exe"].assert_not_called()

    def test_legacy_server_without_receipt_is_repaired_without_losing_world_data(self):
        devstack.setup(self.args)
        (self.server / "build.json").unlink()
        world = self.server / "world/keep.dat"
        world.write_bytes(b"existing world")
        self.server_build.reset_mock()
        devstack.setup(self.args)
        self.server_build.assert_called_once()
        self.assertEqual(world.read_bytes(), b"existing world")
        self.assertTrue((self.server / "build.json").is_file())

    def test_missing_producer_receipt_refuses_server_runtime_replacement(self):
        def build_without_receipt(*args):
            out = self.build_server(*args)
            (out / "build.json").unlink()
            return out

        self.server_build.side_effect = build_without_receipt
        with self.assertRaises(SystemExit):
            devstack.setup(self.args)
        self.assertFalse(self.server.exists())
        self.client_build.assert_not_called()

    def test_cli_keeps_server_and_client_pack_overrides_separate(self):
        with (
            patch.object(
                devstack.sys,
                "argv",
                [
                    "devstack",
                    "setup",
                    "--modpack",
                    "server path",
                    "--client-modpack",
                    "client path",
                    "--game-dir",
                    "game path",
                    "--testbot",
                    "bot.jar",
                    "--force",
                ],
            ),
            patch.object(devstack, "setup") as setup,
        ):
            with self.assertRaises(SystemExit):
                devstack.main()
        args = setup.call_args.args[0]
        self.assertEqual(args.modpack, "server path")
        self.assertEqual(args.client_modpack, "client path")
        self.assertEqual(args.game_dir, "game path")
        self.assertEqual(args.testbot, "bot.jar")
        self.assertTrue(args.force)
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
