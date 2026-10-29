"""Pure-logic checks for dev/devstack.py; nothing here starts a game."""

import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "devstack.py"
spec = importlib.util.spec_from_file_location("devstack", SCRIPT)
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)


class ClientNatives(unittest.TestCase):
    @unittest.skipUnless(hasattr(devstack.os, "getuid"), "Linux runtime directories")
    def test_linux_space_paths_use_a_private_cache_per_stack_and_session(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "linux"),
            patch.dict(devstack.os.environ, {"XDG_RUNTIME_DIR": tmp}),
            patch.object(devstack, "HOME", Path(tmp) / "Game Servers"),
            patch.object(devstack, "process_owner", return_value="session-one"),
        ):
            first = devstack.client_natives_dir()
            self.assertEqual(first.parent, Path(tmp).resolve())
            self.assertFalse(any(c.isspace() for c in str(first)))
            self.assertEqual(devstack.client_natives_dir(), first)
            with patch.object(devstack, "HOME", Path(tmp) / "Other Stack"):
                self.assertNotEqual(devstack.client_natives_dir(), first)
            with patch.object(devstack, "process_owner", return_value="session-two"):
                self.assertNotEqual(devstack.client_natives_dir(), first)
            Path(tmp).chmod(0o755)
            with self.assertRaises(SystemExit):
                devstack.client_natives_dir()


class ServerProperties(unittest.TestCase):
    def test_playtest_map_root_is_not_overwritten_when_starting_an_owned_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fabric-server-launch.jar").write_bytes(b"fixture")
            log = root / "server.out"
            log.write_text("Done (0.1s)!")
            args = SimpleNamespace(xms="512M", xmx="1536M", debug=False, gate=False)
            for selected in (None, str(root / "frozen-test-maps")):
                environment = (
                    {} if selected is None else {"BLOCKFIELD_MAPS_DIR": selected}
                )
                with (
                    patch.dict(devstack.os.environ, environment, clear=True),
                    patch.object(devstack, "SERVER", root),
                    patch.object(devstack, "LOG", log),
                    patch.object(devstack, "server_pid", side_effect=(None, 77)),
                    patch.object(devstack, "port_open", return_value=False),
                    patch.object(devstack, "java_exe", return_value="/fixture/java"),
                    patch.object(
                        devstack, "start_process", return_value=(77, {})
                    ) as start,
                ):
                    devstack.server_start(args)
                    maps = selected or "maps"
                    self.assertEqual(
                        start.call_args.kwargs["env"]["BLOCKFIELD_MAPS_DIR"], maps
                    )
                    self.assertIn(
                        f"-DBLOCKFIELD_MAPS_DIR={maps}", start.call_args.args[0]
                    )

    def test_stack_ports_are_explicit_and_checked_before_runtime_access(self):
        with patch.dict(
            devstack.os.environ, {"BF_SERVER_PORT": "25601", "BF_RCON_PORT": "25576"}
        ):
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual((module.PORT, module.RCON_PORT), (25601, 25576))
            self.assertEqual(module.SERVER_PROPERTIES["rcon.port"], "25576")
        for game, rcon in (
            ("0", "25576"),
            ("25601", "65536"),
            ("25601", "25601"),
            ("invalid", "25576"),
        ):
            with patch.dict(
                devstack.os.environ, {"BF_SERVER_PORT": game, "BF_RCON_PORT": rcon}
            ):
                with self.assertRaises(ValueError):
                    spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_overrides_kept_keys_and_appends_missing_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            devstack.SERVER = Path(tmp)
            (devstack.SERVER / "server.properties").write_text(
                "motd=BLOCKFIELD\nserver-port=25565\nserver-ip=0.0.0.0\n"
            )
            devstack.patch_server_properties()
            written = dict(
                line.split("=", 1)
                for line in (devstack.SERVER / "server.properties")
                .read_text()
                .splitlines()
            )
        self.assertEqual(written["motd"], "BLOCKFIELD")  # untouched
        self.assertEqual(written["server-port"], "25599")  # overridden in place
        self.assertEqual(written["server-ip"], "127.0.0.1")
        self.assertEqual(written["rcon.password"], "leaks")  # appended


class GameJobs(unittest.TestCase):
    def test_jobs_preserve_existing_evidence_and_other_sessions(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack, "HOME", Path(tmp)),
            patch.object(devstack, "process_owner", return_value="current-session"),
        ):
            devstack.game_job(SimpleNamespace(name="draw-check"))
            directory = Path(tmp) / "jobs" / "draw-check"
            config = directory / "job.json"
            original = config.read_bytes()
            self.assertEqual(json.loads(original)["owner"], "current-session")
            with self.assertRaises(FileExistsError):
                devstack.game_job(SimpleNamespace(name="draw-check"))
            self.assertEqual(config.read_bytes(), original)
            config.write_text('{"owner":"another-session"}')
            with self.assertRaises(SystemExit):
                devstack.game_job(SimpleNamespace(name="second-check"))
            self.assertFalse((Path(tmp) / "jobs" / "second-check").exists())
            with self.assertRaises(SystemExit):
                devstack.game_job(SimpleNamespace(name="../outside"))


class PinnedJar(unittest.TestCase):
    def test_reads_filename_from_the_metafile(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            (mods / "blockfield-server.pw.toml").write_text(
                'name = "Blockfield"\nfilename = "blockfield-server-1.2.3+mc1.21.1.jar"\nside = "server"\n'
            )
            self.assertEqual(
                devstack.pinned_jar_name(Path(tmp), "server"),
                "blockfield-server-1.2.3+mc1.21.1.jar",
            )


class WorldSeed(unittest.TestCase):
    def test_a_cached_seed_that_does_not_match_the_lock_is_downloaded_again(self):
        import hashlib
        import json

        calls = []
        real_run = devstack.run
        devstack.run = lambda cmd, **kw: calls.append(cmd)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pack = Path(tmp)
                (pack / "sources").mkdir()
                (pack / "distribution").mkdir()
                (pack / "sources" / "world-seed.zip").write_bytes(b"old seed")
                lock = {
                    "url": "https://github.com/x/y/releases/download/world-seed-v9/world-seed.zip",
                    "sha256": hashlib.sha256(b"new seed").hexdigest(),
                }
                (pack / "distribution" / "assets.lock.json").write_text(
                    json.dumps(lock)
                )
                devstack.fetch_world_seed(pack)
                self.assertEqual(
                    str(pack / "scripts/fetch-world-seed.py"), calls[0][-1]
                )
                self.assertNotIn("gh", calls[0])
                (pack / "sources" / "world-seed.zip").write_bytes(b"new seed")
                devstack.fetch_world_seed(pack)
                self.assertEqual(
                    1, len(calls), "a seed matching the lock is not fetched again"
                )
        finally:
            devstack.run = real_run


class Bot(unittest.TestCase):
    def test_says_which_port_is_dead_instead_of_hanging(self):
        devstack.BOT_PORTS[1] = 1  # nothing listens on port 1
        args = type("Args", (), {"client": 1, "command": ["state"], "timeout": 1})
        with self.assertRaises(SystemExit) as raised:
            devstack.bot(args)
        self.assertIn("127.0.0.1:1", str(raised.exception))


class GameDir(unittest.TestCase):
    def test_default_uses_selected_workspace_stack(self):
        self.assertEqual(devstack.default_game_dir(), devstack.HOME / "play")


class ClientStartup(unittest.TestCase):
    def test_play_waits_for_renderer_and_handles_a_split_log_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            log = home / "client.out"
            log.write_text("Backend library: LWJGL version 3.3.3\n")
            parts = iter(
                ("Created: 1024x1024x0 minecraft:textures/atlas/gui.png-", "atlas\n")
            )

            def loading(_seconds):
                with log.open("a") as output:
                    output.write(next(parts))

            args = SimpleNamespace(
                game_dir=str(home),
                no_testbot=False,
                testbot=None,
                testbot_port=None,
                debug=False,
                server="127.0.0.1:25599",
                name="Fixture",
            )
            with (
                patch.object(devstack, "HOME", home),
                patch.object(devstack, "process_owner", return_value="developer:1"),
                patch.object(devstack, "CLIENT_CMD_FILE", home / "client.cmd"),
                patch.object(
                    devstack, "client_pid", side_effect=[None, 321, 321, 321, 321]
                ),
                patch.object(devstack, "prepare_client", return_value=(home, ["java"])),
                patch.object(devstack, "start_process", return_value=(321, None)),
                patch.object(devstack, "client_environment", return_value={}),
                patch.object(devstack.time, "sleep", side_effect=loading) as sleep,
                patch.object(devstack, "ok") as ready,
            ):
                devstack.play(args)
            self.assertEqual(sleep.call_count, 2)
            ready.assert_called_once_with(
                f"client renderer ready (pid 321); log: {log}"
            )

    def test_agent_cannot_fall_back_to_desktop_and_stop_includes_virtual_display(self):
        with patch.object(devstack, "process_owner", return_value="agent-session"):
            with self.assertRaises(SystemExit):
                devstack.client_environment(False)
            with patch.object(devstack.sys, "platform", "darwin"):
                with self.assertRaises(SystemExit):
                    devstack.client_environment(True)
        with patch.object(devstack, "stop_process") as stop:
            devstack.client_stop()
        self.assertEqual(
            [call.args[0] for call in stop.call_args_list],
            [devstack.CLIENT_PID_FILE, devstack.HOME / "display.pid"],
        )

    def test_exited_replaced_and_timed_out_clients_do_not_report_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "client.out").write_text(
                "Created: 1024x1024x0 minecraft:textures/atlas/gui.png-atlas\n"
            )
            for pid, clock, message in (
                (None, [0, 1], "exited"),
                (322, [0, 1], "exited"),
                (321, [0, 361], "6 minutes"),
            ):
                with (
                    self.subTest(pid=pid, message=message),
                    patch.object(devstack, "HOME", home),
                    patch.object(devstack, "client_pid", return_value=pid),
                    patch.object(devstack.time, "monotonic", side_effect=clock),
                    patch.object(devstack, "progress"),
                ):
                    with self.assertRaisesRegex(SystemExit, message):
                        devstack.wait_client_ready(321)


class BuiltOverlay(unittest.TestCase):
    def test_release_download_still_falls_back_after_a_failed_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp)
            mods = pack / "files/mods"
            mods.mkdir(parents=True)
            for side in ("server", "music"):
                (mods / f"blockfield-{side}-1.jar").touch()
            with (
                patch.object(devstack, "local_jars", return_value={}),
                patch.object(
                    devstack,
                    "pinned_jar_name",
                    side_effect=lambda pack, side: f"blockfield-{side}-1.jar",
                ),
                patch.object(
                    devstack.subprocess,
                    "run",
                    side_effect=[
                        SimpleNamespace(returncode=1),
                        SimpleNamespace(returncode=0),
                    ],
                ) as download,
            ):
                devstack.fetch_mod_jars(pack, client_pack=pack)
            self.assertEqual(download.call_count, 2)
            for call, repo in zip(
                download.call_args_list,
                ("Blockfield/blockfield-mod", "netherg-io/blockfield-mod"),
            ):
                command = call.args[0]
                self.assertEqual(command[command.index("--repo") + 1], repo)
                self.assertEqual(call.kwargs["cwd"], pack)

    def test_fetch_skips_mod_covered_by_local_build(self):
        real = devstack.local_jars
        devstack.local_jars = lambda root: {
            "blockfield-server.pw.toml": Path("/tmp/x.jar")
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pack = Path(tmp)
                (pack / "files" / "mods").mkdir(parents=True)
                (pack / "mods").mkdir()
                client = Path(tmp) / "client"
                (client / "mods").mkdir(parents=True)
                (pack / "mods" / "blockfield-server.pw.toml").write_text(
                    'filename = "blockfield-server-1.jar"\n'
                )
                (client / "mods" / "blockfield-client.pw.toml").write_text(
                    'filename = "blockfield-client-1.jar"\n'
                )
                (client / "mods" / "blockfield-music.pw.toml").write_text(
                    'filename = "blockfield-music-1.jar"\n'
                )
                # Everything else already cached: only the server pin could trigger `gh`.
                (pack / "files" / "mods" / "blockfield-client-1.jar").write_bytes(b"x")
                (pack / "files" / "mods" / "blockfield-music-1.jar").write_bytes(b"x")
                real_subprocess = devstack.subprocess.run
                devstack.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("gh must not run for a locally built mod")
                )
                try:
                    devstack.fetch_mod_jars(
                        pack, client_pack=client
                    )  # server pin skipped, no gh
                finally:
                    devstack.subprocess.run = real_subprocess
        finally:
            devstack.local_jars = real

    def test_pack_build_uses_workspace_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp)
            (pack / "mods").mkdir()
            (pack / "mods/parcool.pw.toml").touch()
            jar = pack / "ParCool-x-bf6.jar"
            with (
                patch.object(devstack, "SERVER_PACK", pack),
                patch.object(
                    devstack, "local_jars", return_value={"parcool.pw.toml": jar}
                ),
                patch.object(devstack, "ensure_localmods"),
                patch.object(devstack, "run") as run,
            ):
                devstack.run_pack_build(
                    ["--out", "path with spaces"], cwd=pack, side="server"
                )
            self.assertEqual(
                run.call_args.args[0],
                [
                    devstack.sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    devstack.ROOT / "scripts/dev/pack_build.py",
                    "server",
                    "--out",
                    "path with spaces",
                ],
            )
            self.assertEqual(
                run.call_args.kwargs, {"cwd": pack, "message": "Building server pack"}
            )


class BuildMods(unittest.TestCase):
    def test_recipe_builds_use_just_and_require_the_declared_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "localmods" / "recipe"
            directory.mkdir(parents=True)
            mapping = {
                "recipe": {"both": "recipe.pw.toml", "jar": "files/mods/new.jar"}
            }
            with (
                patch.object(devstack, "ROOT", root),
                patch.object(devstack, "LOCALMODS", directory.parent),
                patch.object(devstack, "load_mapping", return_value=mapping),
                patch.object(devstack, "java_exe"),
                patch.object(devstack, "run") as run,
            ):
                args = SimpleNamespace(names=["recipe"])
                with self.assertRaises(SystemExit):
                    devstack.build_mods(args)
                run.assert_called_once()
                command, kwargs = run.call_args
                self.assertEqual(list(command[0]), ["just", "build"])
                self.assertEqual(
                    (kwargs["cwd"], kwargs["message"]),
                    (directory, "Building recipe"),
                )
                # Recipe builds run with the vorbis-shim dir first on PATH, and the
                # declared jar parent exists even in a fresh worktree.
                self.assertTrue(
                    kwargs["env"]["PATH"].startswith(
                        str(root / "scripts" / "dev" / "shims") + ":"
                    )
                )
                self.assertTrue((directory / "files" / "mods").is_dir())
                output = directory / "files/mods/new.jar"
                output.write_bytes(b"built")
                run.reset_mock()
                devstack.build_mods(args)
                run.assert_called_once()
                self.assertEqual(
                    run.call_args.kwargs["env"]["PATH"], kwargs["env"]["PATH"]
                )

    def test_plan_prints_sources_without_building(self):
        with (
            patch.object(devstack, "java_exe"),
            patch.object(devstack, "load_mapping", return_value={"a": {}}),
            patch.object(
                devstack,
                "decisions",
                return_value=[("a", "pin", "HEAD is pin bf1")],
            ) as decisions,
            patch.object(devstack, "run") as run,
        ):
            import io
            from contextlib import redirect_stdout

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                devstack.build_mods(SimpleNamespace(names=[], plan=True))
            decisions.assert_called_once()
            self.assertIn("a: pin (HEAD is pin bf1)", buffer.getvalue())
            run.assert_not_called()

    def test_pin_decision_skips_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "localmods" / "fork"
            directory.mkdir(parents=True)
            mapping = {"fork": {"both": "fork.pw.toml"}}
            with (
                patch.object(devstack, "ROOT", root),
                patch.object(devstack, "LOCALMODS", directory.parent),
                patch.object(devstack, "load_mapping", return_value=mapping),
                patch.object(devstack, "java_exe"),
                patch.object(
                    devstack, "classify", return_value=("pin", "HEAD is pin bf1")
                ),
                patch.object(devstack, "run") as run,
            ):
                devstack.build_mods(SimpleNamespace(names=["fork"]))
                run.assert_not_called()

    def test_explicit_build_writes_a_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "localmods" / "recipe"
            (directory / "files" / "mods").mkdir(parents=True)
            (directory / "files" / "mods" / "new.jar").write_bytes(b"built")
            import subprocess

            subprocess.run(
                ["git", "init", "-qb", "main"],
                cwd=directory,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "."], cwd=directory, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=t@localhost",
                    "-c",
                    "user.name=t",
                    "commit",
                    "-qm",
                    "jar",
                ],
                cwd=directory,
                check=True,
            )
            mapping = {
                "recipe": {"both": "recipe.pw.toml", "jar": "files/mods/new.jar"}
            }
            home = root / "home"
            with (
                patch.object(devstack, "ROOT", root),
                patch.object(devstack, "LOCALMODS", directory.parent),
                patch.object(devstack, "HOME", home),
                patch.object(devstack, "load_mapping", return_value=mapping),
                patch.object(devstack, "java_exe"),
                patch.object(
                    devstack, "classify", return_value=("local", "relevant changes")
                ),
                patch.object(devstack, "run"),
            ):
                devstack.build_mods(SimpleNamespace(names=["recipe"]))
                self.assertTrue((home / "localmods-stamps" / "recipe.json").is_file())


class EnsureLocalmods(unittest.TestCase):
    def test_build_failure_dies_instead_of_falling_back_to_the_pin(self):
        with (
            patch.object(devstack, "java_exe"),
            patch.object(devstack, "load_mapping", return_value={}),
            patch.object(
                devstack,
                "ensure_built",
                side_effect=devstack.BuildError("fork: build failed"),
            ),
        ):
            with self.assertRaises(SystemExit):
                devstack.ensure_localmods()

    def test_fresh_stamps_build_nothing(self):
        with (
            patch.object(devstack, "java_exe"),
            patch.object(devstack, "load_mapping", return_value={}),
            patch.object(
                devstack, "ensure_built", return_value=[("fork", "pin", "skip")]
            ) as ensured,
        ):
            devstack.ensure_localmods()
            ensured.assert_called_once()


class InstallClientMod(unittest.TestCase):
    def test_common_refresh_checks_the_whole_server_pair_before_replacing_any_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack, game, server = root / "pack", root / "game", root / "server"
            (pack / "mods").mkdir(parents=True)
            (game / "mods").mkdir(parents=True)
            (server / "mods").mkdir(parents=True)
            (server / "config").mkdir()
            (server / "config/blockfield-pack-version.txt").write_text("old-contract\n")
            built = {}
            for name in ("first", "second"):
                metafile, filename = f"{name}.pw.toml", f"{name}.jar"
                (pack / "mods" / metafile).write_text(f'filename = "{filename}"\n')
                source = root / filename
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("candidate.txt", name)
                built[metafile] = source
                (server / "mods" / filename).write_bytes(source.read_bytes())
                (game / "mods" / filename).write_bytes(b"old-client")
            mismatch = server / "mods/second.jar"
            with (
                patch.object(devstack, "SERVER", server),
                patch.object(devstack, "SERVER_PACK", pack),
                patch.object(devstack, "local_jars", return_value=built),
            ):
                for data in (b"old-server", None):
                    with self.subTest(server_data=data):
                        if data is None:
                            mismatch.unlink()
                        else:
                            mismatch.write_bytes(data)
                        with self.assertRaises(SystemExit):
                            devstack.install_client_localmods(game, pack)
                        for name in ("first", "second"):
                            self.assertEqual(
                                (game / "mods" / f"{name}.jar").read_bytes(),
                                b"old-client",
                            )
                mismatch.write_bytes(built["second.pw.toml"].read_bytes())
                devstack.install_client_localmods(game, pack)
                for name in ("first", "second"):
                    self.assertEqual(
                        (game / "mods" / f"{name}.jar").read_bytes(),
                        built[f"{name}.pw.toml"].read_bytes(),
                    )

    def test_existing_client_refreshes_selected_common_jars_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game, pack = root / "game", root / "client"
            (game / "mods").mkdir(parents=True)
            (pack / "mods").mkdir(parents=True)
            built = {}
            for name, published, candidate in (
                ("lrtactical-fabric", "lr-bf1.jar", "lr-bf2.jar"),
                ("tacz-refabricated", "tacz-bf7.jar", "tacz-bf8.jar"),
            ):
                metafile = f"{name}.pw.toml"
                (pack / "mods" / metafile).write_text(f'filename = "{published}"\n')
                source = root / candidate
                with zipfile.ZipFile(source, "w") as archive:
                    archive.writestr("candidate.txt", candidate)
                built[metafile] = source
                (game / "mods" / published).write_bytes(b"old")
                (game / "mods" / candidate).write_bytes(b"duplicate")
            (game / "mods" / "other.jar").write_bytes(b"keep")
            built["blockfield-client.pw.toml"] = root / "missing-core.jar"
            built["server-only.pw.toml"] = root / "missing-server.jar"
            with (
                patch.object(devstack, "CLIENT_PACK", pack),
                patch.object(devstack, "local_jars", return_value=built),
                patch.object(devstack, "client_pid", return_value=None),
                patch.object(devstack, "SERVER", root / "missing-server"),
            ):
                devstack.client_prepare(
                    SimpleNamespace(
                        game_dir=game,
                        no_testbot=True,
                        tools=False,
                        no_tools=False,
                        mod=False,
                        mod_repo=None,
                    )
                )
            self.assertEqual(
                sorted(p.name for p in (game / "mods").iterdir()),
                ["lr-bf1.jar", "other.jar", "tacz-bf7.jar"],
            )
            for source, published in zip(
                built.values(), ("lr-bf1.jar", "tacz-bf7.jar")
            ):
                self.assertEqual(
                    (game / "mods" / published).read_bytes(), source.read_bytes()
                )
            self.assertEqual((game / "mods" / "other.jar").read_bytes(), b"keep")

    def test_common_jar_install_failure_and_unsafe_filename_keep_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pack, game, source = root / "client", root / "game", root / "new.jar"
            (pack / "mods").mkdir(parents=True)
            (game / "mods").mkdir(parents=True)
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("candidate.txt", "new")
            meta = pack / "mods" / "test.pw.toml"
            meta.write_text('filename = "old.jar"\n')
            old = game / "mods" / "old.jar"
            old.write_bytes(b"keep")
            with patch.object(devstack, "local_jars", return_value={meta.name: source}):
                with patch.object(
                    devstack.shutil, "copyfile", side_effect=OSError("disk full")
                ):
                    with self.assertRaises(OSError):
                        devstack.install_client_localmods(game, pack)
                self.assertEqual(list((game / "mods").iterdir()), [old])
                for filename in ("../escape.jar", "C:/escape.jar", r"..\escape.jar"):
                    with self.subTest(filename=filename):
                        meta.write_text(f"filename = '{filename}'\n")
                        with self.assertRaises(SystemExit):
                            devstack.install_client_localmods(game, pack)
                        self.assertEqual(old.read_bytes(), b"keep")

    def test_dev_settings_repair_existing_config_without_losing_other_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            config = game / "config" / "blockfield-settings.properties"
            devstack.patch_client_settings(game)
            self.assertEqual(config.read_text(), "menu.vanilla=true\n")
            original = "# menu.vanilla=true\nmusic.debug=true\nmenu.vanilla=false\n"
            config.write_text(original)
            devstack.patch_client_settings(game)
            self.assertEqual(config.read_text(), original + "menu.vanilla=true\n")
            devstack.patch_client_settings(game)
            self.assertEqual(config.read_text(), original + "menu.vanilla=true\n")

    def test_replaces_published_jar_and_syncs_server_marker(self):
        real_jar, real_server = devstack.blockfield_jar, devstack.SERVER
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            local = tmp / "blockfield-client-9.9.9+mc.jar"
            local.write_bytes(b"PK-local")
            devstack.blockfield_jar = lambda side: local
            devstack.SERVER = tmp / "server"
            (devstack.SERVER / "config").mkdir(parents=True)
            (devstack.SERVER / "config" / "blockfield-pack-version.txt").write_text(
                "contract\n"
            )
            game = tmp / "game"
            (game / "mods").mkdir(parents=True)
            (game / "config").mkdir()
            (game / "mods" / "blockfield-client-1.0.0.jar").write_bytes(b"PK-published")
            (game / "config" / "blockfield-pack-version.txt").write_text(
                "client-digest\n"
            )
            try:
                devstack.install_client_mod(game, mod=True)
            finally:
                devstack.blockfield_jar, devstack.SERVER = real_jar, real_server
            self.assertEqual(
                [p.name for p in (game / "mods").glob("blockfield-client-*.jar")],
                [local.name],
                "the published jar must go: two jars, one mod id",
            )
            self.assertEqual(
                (game / "config" / "blockfield-pack-version.txt").read_text(),
                "contract\n",
            )

    def test_oracle_jar_never_installs_as_the_client_mod(self):
        real_jar = devstack.blockfield_jar
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            oracle = tmp / "blockfield-client-1.0+mc-oracle.jar"
            oracle.write_bytes(b"PK-oracle")
            devstack.blockfield_jar = lambda side: oracle
            game = tmp / "game"
            try:
                with self.assertRaisesRegex(SystemExit, "not a client runtime jar"):
                    devstack.install_client_mod(game, mod=True)
            finally:
                devstack.blockfield_jar = real_jar
            self.assertFalse(list(game.glob("mods/blockfield-client-*.jar")))

    def test_no_marker_sync_for_a_remote_server(self):
        real_server = devstack.SERVER
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            devstack.SERVER = tmp / "server"
            (devstack.SERVER / "config").mkdir(parents=True)
            (devstack.SERVER / "config" / "blockfield-pack-version.txt").write_text(
                "contract\n"
            )
            game = tmp / "game"
            (game / "config").mkdir(parents=True)
            (game / "config" / "blockfield-pack-version.txt").write_text(
                "client-digest\n"
            )
            try:
                devstack.install_client_mod(game, mod=False, sync_marker=False)
            finally:
                devstack.SERVER = real_server
            self.assertEqual(
                (game / "config" / "blockfield-pack-version.txt").read_text(),
                "client-digest\n",
            )


class ProcessOwnership(unittest.TestCase):
    def test_setup_never_rebuilds_files_of_a_running_server(self):
        for pid, port in ((123, False), (None, True)):
            with (
                self.subTest(pid=pid, port=port),
                patch.object(devstack, "server_pid", return_value=pid),
                patch.object(devstack, "port_open", return_value=port),
                patch.object(devstack, "build_distribution") as build,
            ):
                with self.assertRaises(SystemExit):
                    devstack.setup(SimpleNamespace(modpack=None, force=True))
                build.assert_not_called()

    def test_non_native_record_never_authorizes_a_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "server.pid"
            with (
                patch.object(devstack.sys, "platform", "linux"),
                patch.object(devstack.os, "kill") as signal,
            ):
                for text in ("123", json.dumps({"id": "old", "pid": 123})):
                    signal.reset_mock()
                    path.write_text(text)
                    with self.assertRaises(SystemExit):
                        devstack.stop_process(path)
                    signal.assert_called_once_with(123, 0)
                    self.assertEqual(path.read_text(), text)


class ScenarioExit(unittest.TestCase):
    def test_cli_propagates_failed_scenario(self):
        from scenarios import SCENARIOS

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack, "HOME", Path(tmp)),
            patch.object(devstack.sys, "argv", ["devstack", "scenario", "range"]),
            patch.object(SCENARIOS["range"], "run", return_value=1),
        ):
            with self.assertRaises(SystemExit) as raised:
                devstack.main()
            self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
