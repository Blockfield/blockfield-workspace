"""Pure-logic checks for dev/devstack.py; nothing here starts a game."""

import hashlib
import importlib.util
import json
import os
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
    def test_direct_start_restores_proxy_mods_left_by_via_proxy(self):
        # A --via-proxy run moves the proxy-only mods into mods/; the next
        # plain start must move them back, or direct connects are refused.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fabric-server-launch.jar").write_bytes(b"fixture")
            (root / "mods").mkdir()
            (root / "mods" / "FabricProxy-Lite.jar").write_bytes(b"proxy")
            (root / "mods" / "blockfield.jar").write_bytes(b"game")
            log = root / "server.out"
            log.write_text("Done (0.1s)!")
            args = SimpleNamespace(xms="512M", xmx="1536M", debug=False, gate=False)
            with (
                patch.dict(devstack.os.environ, {}, clear=True),
                patch.object(devstack, "SERVER", root),
                patch.object(devstack, "LOG", log),
                patch.object(devstack, "server_pid", side_effect=(None, 77)),
                patch.object(devstack, "port_open", return_value=False),
                patch.object(devstack, "java_exe", return_value="/fixture/java"),
                patch.object(devstack, "stand_preflight", return_value=None),
                patch.object(devstack, "start_process", return_value=(77, {})),
            ):
                devstack.server_start(args)
            self.assertFalse((root / "mods" / "FabricProxy-Lite.jar").exists())
            self.assertTrue((root / "mods-off" / "FabricProxy-Lite.jar").exists())
            self.assertTrue((root / "mods" / "blockfield.jar").exists())

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
                    patch.object(devstack, "stand_preflight", return_value=None),
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

    def test_extra_jvm_opts_reach_the_server_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fabric-server-launch.jar").write_bytes(b"fixture")
            log = root / "server.out"
            log.write_text("Done (0.1s)!")
            args = SimpleNamespace(
                xms="1G",
                xmx="4G",
                debug=False,
                gate=False,
                jvm_opt=["-XX:MaxGCPauseMillis=100", "-Xlog:gc:file=gc.log"],
            )
            with (
                patch.dict(devstack.os.environ, {}, clear=True),
                patch.object(devstack, "SERVER", root),
                patch.object(devstack, "LOG", log),
                patch.object(devstack, "server_pid", side_effect=(None, 77)),
                patch.object(devstack, "port_open", return_value=False),
                patch.object(devstack, "java_exe", return_value="/fixture/java"),
                patch.object(devstack, "stand_preflight", return_value=None),
                patch.object(devstack, "start_process", return_value=(77, {})) as start,
            ):
                devstack.server_start(args)
                command = start.call_args.args[0]
                self.assertIn("-XX:MaxGCPauseMillis=100", command)
                self.assertIn("-Xlog:gc:file=gc.log", command)
                self.assertLess(
                    command.index("-XX:+UseG1GC"),
                    command.index("-XX:MaxGCPauseMillis=100"),
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


class StandPreflight(unittest.TestCase):
    def test_xmx_parses_heap_cap_units(self):
        self.assertEqual(devstack._xmx_gib(("-Xmx3G",)), 3.0)
        self.assertEqual(devstack._xmx_gib(("-Xmx512M",)), 0.5)
        self.assertEqual(devstack._xmx_gib(()), 0.0)
        self.assertEqual(devstack._xmx_gib(("-Xmxbogus", "-jar", "app.jar")), 0.0)

    def _preflight(self, new, running, mem):
        records = {
            path: (
                {"live": True, "command": ["java", f"-Xmx{cap}"]}
                if cap is not None
                else None
            )
            for path, cap in running.items()
        }
        with (
            patch.object(devstack, "PID_FILE", Path("server.pid")),
            patch.object(
                devstack, "client_pid_file", side_effect=lambda n: Path(f"c{n}.pid")
            ),
            patch.object(devstack, "process_record", side_effect=lambda p: records[p]),
            patch.object(devstack, "_total_memory_gib", return_value=mem),
        ):
            devstack.stand_preflight(new)

    def test_rc_layout_that_thrashed_is_refused_on_a_large_vm(self):
        # 2026-10-31: 3G + 4G + 4G stalled all JVMs for 60 s (swap, watchdog).
        with self.assertRaisesRegex(SystemExit, "over-commits RAM"):
            self._preflight(
                4.0,
                {Path("server.pid"): "3G", Path("c1.pid"): "4G", Path("c2.pid"): None},
                15.6,
            )

    def test_reduced_two_client_layout_fits_a_large_vm(self):
        self._preflight(
            3.0,
            {Path("server.pid"): "3G", Path("c1.pid"): "3G", Path("c2.pid"): None},
            15.6,
        )

    def test_live_records_without_a_heap_cap_do_not_block_small_hosts(self):
        # 7.7 GiB CI runner: a lone client start must reach later checks.
        with patch.object(devstack, "process_record") as record:
            record.return_value = {"live": True}
            with patch.object(devstack, "_total_memory_gib", return_value=7.7):
                devstack.stand_preflight(3.0)

    def test_server_start_runs_the_preflight_first(self):
        with (
            patch.object(devstack, "server_pid", return_value=None),
            patch.object(
                devstack, "stand_preflight", side_effect=SystemExit("nope")
            ) as gate,
        ):
            args = SimpleNamespace(xms="1G", xmx="3G", debug=False, gate=False)
            with self.assertRaisesRegex(SystemExit, "nope"):
                devstack.server_start(args)
            gate.assert_called_once_with(3.0)


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


class BotProfiles(unittest.TestCase):
    def test_scenario_bots_are_cached_offline_and_other_players_kept(self):
        # Java: UUID.nameUUIDFromBytes("OfflinePlayer:Notch".getBytes(UTF_8)).
        self.assertEqual(
            devstack.offline_uuid("Notch"), "b50ad385-829d-3141-a216-7e7d7539ba7f"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "usercache.json"
            stale = {
                "name": "bot01",
                "uuid": "x",
                "expiresOn": "2020-01-01 00:00:00 +0000",
            }
            owner = {
                "name": "Owner",
                "uuid": "y",
                "expiresOn": "2026-12-01 00:00:00 +0000",
            }
            cache.write_text(json.dumps([stale, owner]), encoding="utf-8")
            devstack.seed_bot_profiles(cache)
            devstack.seed_bot_profiles(cache)  # idempotent
            entries = json.loads(cache.read_text(encoding="utf-8"))
        by_name = {e["name"]: e for e in entries}
        self.assertEqual(len(entries), 1 + len(devstack.SCENARIO_BOTS))
        self.assertEqual(by_name["Owner"], owner)
        self.assertEqual(by_name["bot01"]["uuid"], devstack.offline_uuid("bot01"))
        self.assertEqual(by_name["hitbot"]["uuid"], devstack.offline_uuid("hitbot"))
        self.assertEqual(
            by_name["TabletBot"]["uuid"], devstack.offline_uuid("TabletBot")
        )
        self.assertTrue(by_name["bot30"]["expiresOn"].startswith("2099-"))

    def test_unreadable_cache_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "usercache.json"
            cache.write_text("{not json", encoding="utf-8")
            devstack.seed_bot_profiles(cache)
            self.assertEqual(
                len(json.loads(cache.read_text(encoding="utf-8"))),
                len(devstack.SCENARIO_BOTS),
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


class SecondClient(unittest.TestCase):
    def test_per_client_files_dirs_and_ports(self):
        self.assertEqual(devstack.client_pid_file(1), devstack.HOME / "client.pid")
        self.assertEqual(devstack.client_pid_file(2), devstack.HOME / "client2.pid")
        self.assertEqual(devstack.client_cmd_file(2), devstack.HOME / "client2.cmd")
        self.assertEqual(devstack.client_log_file(2), devstack.HOME / "client2.out")
        self.assertEqual(devstack.default_game_dir(2), devstack.HOME / "play2")
        self.assertEqual(devstack.BOT_PORTS[2], 47778)
        self.assertEqual(devstack.CLIENT_DEBUG_PORTS[2], 5007)
        self.assertEqual(devstack.client_number(SimpleNamespace(client=2)), 2)
        self.assertEqual(devstack.client_number(SimpleNamespace()), 1)
        with self.assertRaises(SystemExit):
            devstack.client_number(SimpleNamespace(client=3))

    def test_testbot_default_port_follows_the_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp)
            (game_dir / "mods").mkdir()
            (game_dir / "mods" / "bftestbot.jar").write_bytes(b"fake")
            with patch.dict(devstack.BOT_PORTS, {1: 47777, 2: 47778}, clear=True):
                for client, port in ((1, "47777"), (2, "47778")):
                    args = SimpleNamespace(
                        client=client,
                        testbot=None,
                        testbot_port=None,
                        no_testbot=False,
                    )
                    self.assertEqual(devstack.install_testbot(args, game_dir), port)

    def test_second_client_reuses_stack_testbot_without_flags(self):
        # rc-final6: `just play --client 2` without --testbot silently skipped
        # the testbot (no jar in play2/mods), so bot port 47778 refused and the
        # RC degraded to SKIP cascades. `just setup --testbot` must equip every
        # stack client automatically; --no-testbot still opts out.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "play" / "mods").mkdir(parents=True)
            (home / "play" / "mods" / "bftestbot.jar").write_bytes(b"stack-bot")
            game2 = home / "play2"
            (game2 / "mods").mkdir(parents=True)
            args = SimpleNamespace(
                client=2,
                testbot=None,
                testbot_port=None,
                no_testbot=False,
            )
            with (
                patch.object(devstack, "HOME", home),
                patch.dict(devstack.BOT_PORTS, {1: 47777, 2: 47778}, clear=True),
            ):
                self.assertTrue(devstack.testbot_enabled(args, game2))
                self.assertEqual(devstack.install_testbot(args, game2), "47778")
                self.assertEqual(
                    (game2 / "mods" / "bftestbot.jar").read_bytes(), b"stack-bot"
                )
                # A stale second-client jar converges to the refreshed stack jar
                # on a plain run (blockfield-bot review on the rc-final7 PR).
                (home / "play" / "mods" / "bftestbot.jar").write_bytes(b"fresh-bot")
                (game2 / "mods" / "bftestbot.jar").write_bytes(b"stale-bot")
                self.assertEqual(devstack.install_testbot(args, game2), "47778")
                self.assertEqual(
                    (game2 / "mods" / "bftestbot.jar").read_bytes(), b"fresh-bot"
                )
                # An explicit bare `--testbot` is a build request: it must build
                # instead of silently reusing the installed stack jar.
                root = home / "root"
                libs = root / "scripts" / "dev" / "testbot" / "build" / "libs"
                libs.mkdir(parents=True)

                def fake_build(args):
                    (libs / "bftestbot-9.9.9.jar").write_bytes(b"fresh-build")

                args = SimpleNamespace(
                    client=2,
                    testbot=True,
                    testbot_port=None,
                    no_testbot=False,
                )
                with (
                    patch.object(devstack, "HOME", home),
                    patch.object(devstack, "ROOT", root),
                    patch.object(devstack, "build_testbot", side_effect=fake_build),
                    patch.dict(devstack.BOT_PORTS, {1: 47777, 2: 47778}, clear=True),
                ):
                    self.assertEqual(devstack.install_testbot(args, game2), "47778")
                    self.assertEqual(
                        (game2 / "mods" / "bftestbot.jar").read_bytes(), b"fresh-build"
                    )
            args = SimpleNamespace(
                client=2,
                testbot=None,
                testbot_port=None,
                no_testbot=True,
            )
            with (
                patch.object(devstack, "HOME", home),
                patch.dict(devstack.BOT_PORTS, {1: 47777, 2: 47778}, clear=True),
            ):
                self.assertFalse(devstack.testbot_enabled(args, game2))
                self.assertIsNone(devstack.install_testbot(args, game2))

    def test_setup_disables_skinrestorer_join_autofetch_on_stands(self):
        # rc-final6 log-scan FAIL: stands join with throwaway offline names, so
        # SkinRestorer's join auto-fetch logs "Failed to fetch skin" WARNs.
        # Stands must not fetch (prod keeps fetching for real players).
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config" / "skinrestorer" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "join": {
                            "autoFetch": {
                                "enabled": True,
                                "providers": ["blockfield"],
                            }
                        },
                        "other": 1,
                    }
                )
            )
            with patch.object(devstack, "SERVER", Path(tmp)):
                devstack.patch_dev_skinrestorer()
            patched = json.loads(config.read_text())
            self.assertFalse(patched["join"]["autoFetch"]["enabled"])
            self.assertEqual(patched["other"], 1)
            with patch.object(devstack, "SERVER", Path(tmp)):
                devstack.patch_dev_skinrestorer()
            config.unlink()
            with patch.object(devstack, "SERVER", Path(tmp)):
                devstack.patch_dev_skinrestorer()

    def test_client_stop_keeps_the_shared_display_until_the_last_client(self):
        with (
            patch.object(devstack, "stop_process") as stop,
            patch.object(devstack, "recorded_pid", return_value=None),
        ):
            devstack.client_stop(2)
        self.assertEqual(
            [call.args[0] for call in stop.call_args_list],
            [devstack.HOME / "client2.pid", devstack.HOME / "display.pid"],
        )
        with (
            patch.object(devstack, "stop_process") as stop,
            patch.object(devstack, "recorded_pid", side_effect=[123, 123]),
        ):
            devstack.client_stop(1)
        self.assertEqual(
            [call.args[0] for call in stop.call_args_list],
            [devstack.HOME / "client.pid"],
        )


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
                patch.object(devstack, "stand_preflight", return_value=None),
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
            # Only the client mod still downloads; the server jar is built, never fetched.
            (mods / "blockfield-music-1.jar").touch()
            attempts = []

            def gh(command, **kwargs):
                # The first repo fails; the fallback repo delivers the client jar.
                attempts.append(command)
                if len(attempts) == 1:
                    return SimpleNamespace(returncode=1)
                (mods / "blockfield-client-1.jar").touch()
                return SimpleNamespace(returncode=0)

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
                    side_effect=gh,
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

    def test_stale_jar_under_the_pin_name_is_downloaded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp)
            mods = pack / "files/mods"
            mods.mkdir(parents=True)
            (pack / "mods").mkdir()
            good = b"published"
            for side in ("client", "music"):
                (pack / "mods" / f"blockfield-{side}.pw.toml").write_text(
                    f'filename = "blockfield-{side}-1.jar"\n[download]\n'
                    f'hash-format = "sha1"\nhash = "{hashlib.sha1(good).hexdigest()}"\n'
                )
                (mods / f"blockfield-{side}-1.jar").write_bytes(good)
            (mods / "blockfield-client-1.jar").write_bytes(b"stale build")

            def gh(command, **kwargs):
                (mods / "blockfield-client-1.jar").write_bytes(good)
                return SimpleNamespace(returncode=0)

            with (
                patch.object(devstack, "local_jars", return_value={}),
                patch.object(devstack.subprocess, "run", side_effect=gh) as download,
            ):
                devstack.fetch_mod_jars(pack, client_pack=pack)
            self.assertEqual(download.call_count, 1)
            self.assertIn("blockfield-client-1.jar", download.call_args.args[0])
            self.assertEqual((mods / "blockfield-client-1.jar").read_bytes(), good)

    def test_server_jar_is_never_downloaded(self):
        # The download pin is gone: even a stale server jar under files/mods
        # must not trigger `gh`; the stand jar is built from mod-server.
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp)
            mods = pack / "files/mods"
            mods.mkdir(parents=True)
            (pack / "mods").mkdir()
            good = b"published"
            for side in ("client", "music"):
                (pack / "mods" / f"blockfield-{side}.pw.toml").write_text(
                    f'filename = "blockfield-{side}-1.jar"\n'
                )
                (mods / f"blockfield-{side}-1.jar").write_bytes(good)
            (pack / "mods" / "blockfield-server.pw.toml").write_text(
                'filename = "blockfield-server-1.jar"\n[download]\n'
                f'hash-format = "sha256"\nhash = "{hashlib.sha256(good).hexdigest()}"\n'
            )
            (mods / "blockfield-server-1.jar").write_bytes(b"stale build")
            with (
                patch.object(devstack, "local_jars", return_value={}),
                patch.object(devstack.subprocess, "run") as download,
            ):
                devstack.fetch_mod_jars(pack, client_pack=pack)
            download.assert_not_called()
            self.assertEqual(
                (mods / "blockfield-server-1.jar").read_bytes(), b"stale build"
            )

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
                # Everything else already cached: nothing may trigger `gh`
                # (the server pin never downloads anymore).
                (pack / "files" / "mods" / "blockfield-client-1.jar").write_bytes(b"x")
                (pack / "files" / "mods" / "blockfield-music-1.jar").write_bytes(b"x")
                real_subprocess = devstack.subprocess.run
                devstack.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
                    AssertionError("gh must not run for a locally built mod")
                )
                try:
                    devstack.fetch_mod_jars(
                        pack, client_pack=client
                    )  # server pin never fetched, no gh
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


class ServerJar(unittest.TestCase):
    def test_explicit_jar_wins_over_local_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "blockfield-server-9.9.9+mc1.21.1.jar"
            explicit.write_bytes(b"PK")
            local = Path(tmp) / "blockfield-server-1.0+mc.jar"
            local.write_bytes(b"PK")
            args = SimpleNamespace(server_jar=str(explicit))
            with patch.object(
                devstack,
                "local_jars",
                return_value={"blockfield-server.pw.toml": local},
            ):
                self.assertEqual(devstack.resolve_server_jar(args.server_jar), explicit)

    def test_env_jar_used_when_no_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "blockfield-server-9.9.9+mc1.21.1.jar"
            jar.write_bytes(b"PK")
            args = SimpleNamespace(server_jar=None)
            with (
                patch.object(devstack, "local_jars", return_value={}),
                patch.dict(
                    devstack.os.environ, {"BF_SERVER_JAR": str(jar)}, clear=True
                ),
            ):
                self.assertEqual(devstack.resolve_server_jar(args.server_jar), jar)

    def test_local_build_used_as_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "blockfield-server-1.0+mc.jar"
            jar.write_bytes(b"PK")
            args = SimpleNamespace(server_jar=None)
            with (
                patch.object(
                    devstack,
                    "local_jars",
                    return_value={"blockfield-server.pw.toml": jar},
                ),
                patch.dict(devstack.os.environ, {}, clear=True),
            ):
                self.assertEqual(devstack.resolve_server_jar(args.server_jar), jar)

    def test_missing_jar_dies_loudly(self):
        args = SimpleNamespace(server_jar=None)
        with (
            patch.object(devstack, "local_jars", return_value={}),
            patch.dict(devstack.os.environ, {}, clear=True),
        ):
            with self.assertRaises(SystemExit):
                devstack.resolve_server_jar(args.server_jar)

    def test_non_runtime_jar_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "blockfield-server-1.0-sources.jar"
            jar.write_bytes(b"PK")
            with self.assertRaises(SystemExit):
                devstack.resolve_server_jar(str(jar))

    def test_build_distribution_passes_server_jar_to_pack_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            modpack = home / "pack"
            (modpack / "server").mkdir(parents=True)
            jar = home / "blockfield-server-1.57.0+mc1.21.1.jar"
            jar.write_bytes(b"PK")
            seed = home / "seed.zip"
            seed.write_bytes(b"seed")
            with (
                patch.object(devstack, "HOME", home),
                patch.object(devstack, "ensure_localmods"),
                patch.object(devstack, "fetch_mod_jars"),
                patch.object(devstack, "fetch_world_seed", return_value=seed),
                patch.object(devstack, "run_pack_build") as pack,
            ):
                devstack.build_distribution(modpack, modpack, jar)
            args = pack.call_args.args[0]
            self.assertIn("--server-jar", args)
            self.assertEqual(args[args.index("--server-jar") + 1], jar)

    def test_pack_build_forwards_server_jar_to_the_server_build(self):
        pack_build = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location(
                "pack_build_case", devstack.ROOT / "scripts/dev/pack_build.py"
            )
        )
        pack_build.__spec__.loader.exec_module(pack_build)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "scripts" / "build-distribution.py").write_text(
                "def resolve_mod(root, item, built):\n"
                "    raise AssertionError('no pins in this tree')\n"
                "def build(args):\n"
                f"    __import__('pathlib').Path({str(root / 'seen.txt')!r}).write_text(\n"
                "        repr((str(args.server_jar), args.no_server_jar)))\n"
            )
            seed = root / "seed.zip"
            seed.write_bytes(b"seed")
            jar = root / "blockfield-server-1.57.0+mc1.21.1.jar"
            jar.write_bytes(b"PK")
            out = root / "out"
            out.mkdir()
            pack_build.main(
                [
                    "server",
                    "--root",
                    str(root),
                    "--out",
                    str(out),
                    "--server-source",
                    str(root / "server"),
                    "--world-seed",
                    str(seed),
                    "--build-id",
                    "dev",
                    "--server-jar",
                    str(jar),
                ]
            )
            self.assertEqual(
                (root / "seen.txt").read_text(),
                repr((str(jar), False)),
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
                # The pinned-JDK wrapper is covered in test_jdk_auto; here it
                # returns a fresh minimal env so the test stays hermetic.
                patch.object(
                    devstack, "jdk_env", side_effect=lambda: {"PATH": "test-tools"}
                ),
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

    def test_dev_settings_opt_out_pins_vanilla_off_for_pack_stands(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            config = game / "config" / "blockfield-settings.properties"
            with patch.dict(os.environ, {"BLOCKFIELD_MENU_VANILLA": "0"}):
                devstack.patch_client_settings(game)
                self.assertEqual(config.read_text(), "menu.vanilla=false\n")
                # A stale forced-true from an earlier stand is neutralized: last wins.
                config.write_text("menu.vanilla=true\n")
                devstack.patch_client_settings(game)
                self.assertEqual(
                    config.read_text(), "menu.vanilla=true\nmenu.vanilla=false\n"
                )
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            config = game / "config" / "blockfield-settings.properties"
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("BLOCKFIELD_MENU_VANILLA", None)
                devstack.patch_client_settings(game)
                self.assertEqual(config.read_text(), "menu.vanilla=true\n")

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
