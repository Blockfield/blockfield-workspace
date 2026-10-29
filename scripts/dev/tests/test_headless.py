"""Headless launch isolation and restart checks; no Minecraft or user desktop access."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devstack  # noqa: E402
import media  # noqa: E402


class Headless(unittest.TestCase):
    def test_completed_probe_metadata_does_not_become_a_cli_error_status(self):
        import headless_check

        args = SimpleNamespace(probe=True, headless=True)
        with patch.object(
            headless_check, "run", return_value={"complete": True}
        ) as run:
            self.assertIsNone(devstack.play(args))
        run.assert_called_once_with(args)

    def test_agent_refuses_foreign_default_server_before_preparing_client(self):
        args = SimpleNamespace(
            game_dir=None,
            testbot=None,
            testbot_port=None,
            debug=False,
            no_testbot=True,
            name="Fixture",
            server="127.0.0.1:25599",
        )
        with (
            patch.object(devstack, "client_pid", return_value=None),
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(
                devstack,
                "process_record",
                return_value={
                    "pid": 321,
                    "live": True,
                    "native": "linux",
                    "owner": "other",
                },
            ),
            patch.object(devstack, "prepare_client") as prepare,
            patch.object(devstack, "start_process") as start,
            self.assertRaisesRegex(SystemExit, "own local server"),
        ):
            devstack.play(args)
        prepare.assert_not_called()
        start.assert_not_called()

    def test_agent_restart_refuses_saved_foreign_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            receipt = home / "client.cmd"
            receipt.write_text(
                json.dumps(
                    {
                        "command": ["java", "--quickPlayMultiplayer=127.0.0.1:25599"],
                        "headless": True,
                    }
                )
            )
            with (
                patch.object(devstack, "CLIENT_CMD_FILE", receipt),
                patch.object(devstack, "process_owner", return_value="current"),
                patch.object(devstack, "process_record", return_value=None),
                patch.object(devstack, "java_exe", return_value="java"),
                patch.object(devstack, "start_process") as start,
                self.assertRaisesRegex(SystemExit, "own local server"),
            ):
                devstack.client_restart(home)
            start.assert_not_called()

    def test_headless_options_prevent_monitor_changes_and_keep_volume_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / "options.txt"
            path.write_text(
                "fullscreen:true\npauseOnLostFocus:true\nsoundCategory_master:0.8\n"
            )
            devstack.patch_headless_options(home)
            expected = (
                "soundCategory_master:0.8\nfullscreen:false\npauseOnLostFocus:false\n"
            )
            self.assertEqual(path.read_text(), expected)
            devstack.patch_headless_options(home)
            self.assertEqual(path.read_text(), expected)

    def test_linux_and_windows_discard_user_display_and_audio_backends(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "display.number").write_text("87")
            for platform in ("linux",):
                with (
                    self.subTest(platform=platform),
                    patch.object(devstack, "HOME", home),
                    patch.object(devstack.sys, "platform", platform),
                    patch.object(devstack, "process_owner", return_value="current"),
                    patch.object(devstack, "recorded_pid", return_value=321),
                    patch.object(
                        devstack, "process_record", return_value={"owner": "current"}
                    ),
                    patch.dict(
                        os.environ,
                        DISPLAY=":0",
                        WAYLAND_DISPLAY="wayland-0",
                        ALSOFT_DRIVERS="pulse,wasapi,",
                    ),
                ):
                    env = devstack.client_environment(True)
                self.assertEqual(env["ALSOFT_DRIVERS"], "null")
                self.assertNotIn("WAYLAND_DISPLAY", env)
                if platform == "linux":
                    self.assertEqual(env["DISPLAY"], ":87")
                    self.assertEqual(env["XAUTHORITY"], str(home / "display.auth"))
                else:
                    self.assertNotIn("DISPLAY", env)
            with (
                patch.object(devstack, "process_owner", return_value="developer:1"),
                patch.dict(os.environ, ALSOFT_DRIVERS="pulse"),
            ):
                self.assertEqual(
                    devstack.client_environment(False)["ALSOFT_DRIVERS"], "pulse"
                )

    def test_linux_refuses_another_sessions_virtual_display(self):
        with (
            patch.object(devstack.sys, "platform", "linux"),
            patch.object(devstack, "recorded_pid", return_value=321),
            patch.object(devstack, "process_record", return_value={"owner": "other"}),
            patch.object(devstack, "process_owner", return_value="current"),
            self.assertRaisesRegex(SystemExit, "another session"),
        ):
            devstack.client_environment(True)

    def test_restart_preserves_headless_and_enforces_it_for_legacy_agent_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            receipt = home / "client.cmd"
            for owner, saved, gpu in (
                ("developer:1", True, False),
                ("current", False, True),
            ):
                receipt.write_text(
                    json.dumps({"command": ["java"], "headless": saved, "gpu": gpu})
                )
                with (
                    patch.object(devstack, "CLIENT_CMD_FILE", receipt),
                    patch.object(devstack, "HOME", home),
                    patch.object(devstack, "process_owner", return_value=owner),
                    patch.object(devstack, "java_exe", return_value="java"),
                    patch.object(
                        devstack, "client_environment", return_value={}
                    ) as env,
                    patch.object(
                        devstack, "start_process", return_value=(321, None)
                    ) as start,
                    patch.object(devstack, "wait_client_ready"),
                ):
                    devstack.client_restart(home)
                env.assert_called_once_with(True, gpu)
                self.assertTrue(start.call_args.kwargs["headless"])

    def test_agent_client_defaults_to_gpu_only_when_a_render_node_is_usable(self):
        for flag, node, expected in (
            (None, True, True),
            (None, False, False),
            (False, True, False),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                args = SimpleNamespace(
                    game_dir=str(home),
                    no_testbot=True,
                    testbot=None,
                    testbot_port=None,
                    debug=False,
                    server=None,
                    name="Fixture",
                    gpu=flag,
                )
                with (
                    patch.object(devstack, "HOME", home),
                    patch.object(devstack, "CLIENT_CMD_FILE", home / "client.cmd"),
                    patch.object(devstack, "process_owner", return_value="current"),
                    patch.object(devstack, "client_pid", return_value=None),
                    patch.object(devstack, "check_agent_server"),
                    patch.object(
                        devstack, "prepare_client", return_value=(home, ["java"])
                    ),
                    patch.object(devstack, "patch_headless_options"),
                    patch.object(devstack, "gpu_render_node", return_value=node),
                    patch.object(
                        devstack, "client_environment", return_value={}
                    ) as env,
                    patch.object(devstack, "start_process", return_value=(321, None)),
                    patch.object(devstack, "wait_client_ready"),
                    patch.object(devstack, "ok"),
                ):
                    devstack.play(args)
                env.assert_called_once_with(True, expected)
                receipt = json.loads((home / "client.cmd").read_text())
                self.assertIs(receipt["gpu"], expected)

    def test_gpu_renders_through_virtualgl_on_the_own_virtual_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "display.number").write_text("87")
            faker = home / "with space" / "virtualgl"
            with (
                patch.object(devstack, "HOME", home),
                patch.object(devstack.sys, "platform", "linux"),
                patch.object(devstack, "process_owner", return_value="current"),
                patch.object(devstack, "recorded_pid", return_value=321),
                patch.object(
                    devstack, "process_record", return_value={"owner": "current"}
                ),
                patch.object(
                    devstack,
                    "deb_libraries",
                    return_value=[faker / "libdlfaker.so", faker / "libvglfaker.so"],
                ),
                patch.object(
                    devstack.Path, "glob", return_value=[Path("/dev/dri/renderD128")]
                ),
                patch.object(devstack.os, "access", return_value=True),
                patch.dict(os.environ, DISPLAY=":0", LD_LIBRARY_PATH="/opt/lib"),
            ):
                env = devstack.client_environment(True, True)
                with (
                    patch.object(devstack.os, "access", return_value=False),
                    self.assertRaisesRegex(SystemExit, "renderD"),
                ):
                    devstack.client_environment(True, True)
            self.assertEqual(env["DISPLAY"], ":87")
            self.assertEqual(env["LD_PRELOAD"], "libdlfaker.so:libvglfaker.so")
            self.assertEqual(env["LD_LIBRARY_PATH"], f"{faker}:/opt/lib")
            self.assertEqual(env["VGL_DISPLAY"], "egl")
            self.assertNotIn("LIBGL_ALWAYS_SOFTWARE", env)

    def test_windows_refuses_missing_renderer_before_any_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                patch.object(devstack.sys, "platform", "win32"),
                patch.object(devstack.subprocess, "Popen") as launch,
                self.assertRaisesRegex(SystemExit, "desktop fallback is forbidden"),
            ):
                devstack.start_process(
                    ["java"], home, home / "client.pid", home / "log", headless=True
                )
            launch.assert_not_called()

    def test_windows_uses_verified_private_renderer_without_inherited_desktop(self):
        with (
            patch.object(devstack.sys, "platform", "win32"),
            patch.object(
                devstack, "java_exe", return_value="/project/jdk/bin/java.exe"
            ),
            patch.object(
                media,
                "windows_java",
                return_value=Path("/project/isolated/bin/java.exe"),
            ),
            patch.object(devstack, "verify_windows_backend") as verify,
            patch.dict(
                os.environ,
                DISPLAY=":0",
                WAYLAND_DISPLAY="wayland-0",
                ALSOFT_DRIVERS="wasapi",
            ),
        ):
            environment = devstack.client_environment(True)
        self.assertNotIn("DISPLAY", environment)
        self.assertNotIn("WAYLAND_DISPLAY", environment)
        self.assertEqual(environment["ALSOFT_DRIVERS"], "null")
        self.assertEqual(environment["GALLIUM_DRIVER"], "llvmpipe")
        self.assertEqual(
            environment["BF_HEADLESS_JAVA"], str(Path("/project/isolated/bin/java.exe"))
        )
        verify.assert_called_once_with(environment)

    def test_windows_renderer_failure_never_falls_back_to_popen(self):
        with (
            patch.object(devstack.sys, "platform", "win32"),
            patch.object(devstack, "java_exe", return_value="java.exe"),
            patch.object(
                media, "windows_java", side_effect=RuntimeError("checksum mismatch")
            ),
            patch.object(devstack.subprocess, "Popen") as launch,
            patch.object(devstack, "verify_windows_backend") as verify,
            self.assertRaisesRegex(RuntimeError, "checksum mismatch"),
        ):
            devstack.client_environment(True)
        launch.assert_not_called()
        verify.assert_not_called()

    def test_windows_retains_private_backend_until_its_renderer_is_ready(self):
        import windows_headless
        from unittest.mock import Mock

        for failure in (None, RuntimeError("renderer failed")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                process = Mock(pid=321)
                process.identity = {"pid": 321, "birth": 42, "exe": "java.exe"}
                process.backend = {"station": "private", "desktop": "game"}
                events = []
                process.close.side_effect = lambda: events.append("close")
                process.terminate.side_effect = lambda: events.append("terminate")

                def ready(pid):
                    self.assertEqual(pid, 321)
                    self.assertTrue((home / "client.pid").is_file())
                    self.assertEqual(events, [])
                    events.append("ready")
                    if failure:
                        raise failure

                with (
                    patch.object(devstack.sys, "platform", "win32"),
                    patch.object(devstack, "recorded_pid", return_value=None),
                    patch.object(devstack, "process_owner", return_value="own"),
                    patch.object(windows_headless, "launch", return_value=process),
                    patch.object(devstack, "wait_client_ready", side_effect=ready),
                ):
                    if failure:
                        with self.assertRaisesRegex(RuntimeError, "renderer failed"):
                            devstack.start_process(
                                ["java"],
                                home,
                                home / "client.pid",
                                home / "client.out",
                                {"BF_HEADLESS_JAVA": "java.exe"},
                                headless=True,
                            )
                    else:
                        self.assertEqual(
                            devstack.start_process(
                                ["java"],
                                home,
                                home / "client.pid",
                                home / "client.out",
                                {"BF_HEADLESS_JAVA": "java.exe"},
                                headless=True,
                            ),
                            (321, None),
                        )
                self.assertEqual(
                    events,
                    ["ready", "terminate", "close"] if failure else ["ready", "close"],
                )
                if failure:
                    process.wait.assert_called_once_with(timeout=5)
                    self.assertFalse((home / "client.pid").exists())
                else:
                    process.terminate.assert_not_called()

    def test_windows_probe_timeout_closes_only_its_fresh_process(self):
        import windows_headless
        from unittest.mock import Mock

        process = Mock()
        process.wait.side_effect = devstack.subprocess.TimeoutExpired("own probe", 30)
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(devstack, "HOME", Path(temporary)),
                patch.object(windows_headless, "launch", return_value=process),
                self.assertRaises(devstack.subprocess.TimeoutExpired),
            ):
                devstack.verify_windows_backend(
                    {"BF_HEADLESS_JAVA": str(Path(temporary) / "java.exe")}
                )
        process.terminate.assert_called_once_with()
        process.close.assert_called_once_with()

    def test_windows_probe_rejects_visible_station_before_any_minecraft_launch(self):
        import windows_headless
        from unittest.mock import Mock

        process = Mock()
        process.wait.return_value = 0
        process.poll.return_value = 0
        process.backend = {"station": "own", "desktop": "game"}
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)

            def launch(command, cwd, env, log, owner):
                log.write(
                    json.dumps(
                        {
                            "station": "own",
                            "desktop": "game",
                            "visible": True,
                            "gl": {"renderer": "llvmpipe"},
                        }
                    )
                )
                return process

            with (
                patch.object(devstack, "HOME", home),
                patch.object(windows_headless, "launch", side_effect=launch),
                self.assertRaisesRegex(SystemExit, "identity is not verified"),
            ):
                devstack.verify_windows_backend(
                    {"BF_HEADLESS_JAVA": str(home / "java.exe")}
                )
        process.terminate.assert_not_called()
        process.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
