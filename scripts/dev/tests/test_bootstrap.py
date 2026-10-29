"""Portable bootstrap: pinned tools, clean environment, preserved argv."""

import importlib.util
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[3] / "bf.py"
spec = importlib.util.spec_from_file_location("bf", SCRIPT)
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)
CLEAN_ENV = (
    "APPIMAGE",
    "__MISE_SHIM",
    "__MISE_DIFF",
    "PYTHONHOME",
    "PYTHONPATH",
    "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS",
    "JDK_JAVA_OPTIONS",
    "CLASSPATH",
)


class Bootstrap(unittest.TestCase):
    def test_public_help_uses_just_recipes_and_preserves_dev_only_commands(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        for arguments, usage in (
            (["--help"], "usage: just <recipe>"),
            (["server", "start", "--help"], "usage: just server start"),
            (["play", "--help"], "usage: just play"),
            (["scenario", "parkour", "--help"], "usage: just scenario parkour"),
            (["game-job", "--help"], "usage: just dev game-job"),
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(devstack.sys, "argv", ["bf.py", *arguments]),
            ):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    if arguments == ["--help"]:
                        devstack.main()
                    else:
                        with self.assertRaises(SystemExit) as exit:
                            devstack.main()
                        self.assertEqual(exit.exception.code, 0)
                self.assertIn(usage, output.getvalue())
                self.assertNotRegex(
                    output.getvalue(),
                    r"\bbf(?:\.py)? (?:server|play|setup|scenario|complete)",
                )
        with self.assertRaisesRegex(SystemExit, r"just.*: fixture failure"):
            devstack.die("fixture failure")

    def test_completion_uses_native_just_without_custom_wrapper_scripts(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with (
            patch.object(devstack, "native", return_value="/native/just"),
            patch.object(devstack.subprocess, "run") as run,
        ):
            devstack.print_completion(["powershell"])
            run.assert_called_once_with(
                ["/native/just", "--completions", "powershell"], check=True
            )

    def test_all_commands_use_pinned_python_and_clean_environment(self):
        with (
            patch.object(bf.sys, "platform", "linux"),
            patch.object(bf.os, "execve") as execute,
            patch.object(bf, "canonical_python", return_value=Path(sys.executable)),
            patch.dict(bf.os.environ, {key: "stale" for key in CLEAN_ENV}),
        ):
            for arguments in (
                [],
                ["--help"],
                ["status"],
                ["bot", "state"],
                ["setup"],
                ["build-testbot"],
                ["server", "stop"],
                ["build-mods"],
                ["play", "--game-dir", str(SCRIPT.parent / "game with spaces")],
            ):
                bf.main(arguments)
                executable, command, env = execute.call_args.args
                self.assertEqual(executable, sys.executable)
                self.assertEqual(
                    command,
                    [sys.executable, "-I", "-X", "utf8", str(bf.DEVSTACK), *arguments],
                )
                self.assertFalse(set(CLEAN_ENV) & env.keys())
                self.assertEqual(env["PYTHONUTF8"], "1")

    def test_mcp_has_the_same_bootstrap(self):
        with (
            patch.object(bf.sys, "platform", "linux"),
            patch.object(bf.os, "execve") as execute,
            patch.object(bf, "canonical_python", return_value=Path(sys.executable)),
        ):
            bf.main(["mcp"])
            self.assertEqual(
                execute.call_args.args[1][-1],
                str(bf.ROOT / "scripts/dev/mcp/server.py"),
            )

    def test_windows_preserves_stdio_arguments_environment_and_child_exit_code(self):
        with (
            patch.object(bf.sys, "platform", "win32"),
            patch.object(bf.os, "execve") as execute,
            patch.object(
                bf.subprocess, "run", return_value=SimpleNamespace(returncode=23)
            ) as run,
            patch.object(bf, "canonical_python", return_value=Path(sys.executable)),
            patch.dict(bf.os.environ, {key: "stale" for key in CLEAN_ENV}),
        ):
            arguments = ["client", "--game-dir", "path with spaces & literal"]
            with self.assertRaises(SystemExit) as exit:
                bf.main(arguments)
            self.assertEqual(exit.exception.code, 23)
            execute.assert_not_called()
            self.assertEqual(
                run.call_args.args[0],
                [sys.executable, "-I", "-X", "utf8", str(bf.DEVSTACK), *arguments],
            )
            self.assertEqual(set(run.call_args.kwargs), {"env"})
            self.assertFalse(set(CLEAN_ENV) & run.call_args.kwargs["env"].keys())

    def test_failed_python_verification_prevents_launch(self):
        with (
            patch.object(bf.os, "execve") as execute,
            patch.object(
                bf, "canonical_python", side_effect=RuntimeError("compressor mismatch")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "compressor mismatch"):
                bf.main(["setup"])
            execute.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX venv symlink regression")
    def test_quality_venv_recovers_after_checkout_moves(self):
        import toolchain

        run = subprocess.run
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            binary = cache / "venv" / "bin" / "python"
            binary.parent.mkdir(parents=True)
            binary.symlink_to(cache / "deleted-checkout" / "python")
            with (
                patch.object(toolchain, "CACHE", cache),
                patch.object(
                    toolchain, "canonical_python", return_value=sys.executable
                ),
                patch.object(
                    toolchain,
                    "call",
                    side_effect=lambda *args: run(
                        [*map(str, args), "--without-pip"], check=True, timeout=30
                    ),
                ),
                patch.object(
                    toolchain.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="ruff 0.15.7"),
                ),
            ):
                toolchain.tool("ruff")
            self.assertTrue(binary.is_file())
            run([str(binary), "-I", "-c", "pass"], check=True, timeout=10)

    def test_real_native_cli_and_mcp_preserve_paths_and_isolate_environment(self):
        from toolchain import tool

        python = str(bf.canonical_python())
        just = str(tool("just"))
        with tempfile.TemporaryDirectory(prefix="bf bootstrap ") as tmp:
            workspace = Path(tmp) / "workspace with spaces"
            workspace.mkdir()
            for name in (
                "bf.py",
                "justfile",
                ".mcp.json",
                "scripts/toolchain.py",
                "scripts/dev_cache.py",
                "scripts/terminal.py",
            ):
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bf.ROOT / name, target)
            for directory in (
                "scripts/dev",
                "scripts/dev/scenarios",
                "scripts/dev/mcp",
            ):
                target = workspace / directory
                target.mkdir(parents=True, exist_ok=True)
                for source in (bf.ROOT / directory).glob("*.py"):
                    shutil.copy2(source, target / source.name)
            env = {
                **os.environ,
                **{key: "stale environment" for key in CLEAN_ENV},
                "BF_DEV_HOME": str(workspace / "own stack with spaces"),
                "NO_COLOR": "1",
                "PATH": os.pathsep.join(
                    (
                        str(Path(just).parent),
                        str(Path(python).parent),
                        os.environ.get("PATH", ""),
                    )
                ),
            }
            missing = workspace / "missing game with spaces & literal"
            arguments = ["client", "--game-dir", str(missing), "--no-testbot"]
            for command, cwd in (
                (
                    [python, "-I", "-X", "utf8", str(workspace / "bf.py"), *arguments],
                    tmp,
                ),
                ([just, *arguments], workspace),
            ):
                result = subprocess.run(
                    command,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=45,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(str(missing) + " does not exist", result.stderr)
                self.assertNotIn("unrecognized arguments", result.stderr)
            config = json.loads((workspace / ".mcp.json").read_text())["mcpServers"][
                "blockfield-dev"
            ]
            self.assertEqual(config["command"], "just")
            self.assertEqual(config["args"], ["mcp"])
            executable = shutil.which(config["command"], path=env["PATH"])
            self.assertIsNotNone(executable)
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            result = subprocess.run(
                [executable, *config["args"]],
                cwd=workspace,
                env=env,
                input="\n".join(map(json.dumps, messages)) + "\n",
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=45,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([reply["id"] for reply in replies], [1, 2])
            self.assertEqual(
                replies[0]["result"]["serverInfo"]["name"], "blockfield-dev"
            )
            self.assertIn(
                "client_play", {tool["name"] for tool in replies[1]["result"]["tools"]}
            )
            self.assertFalse(missing.exists())


class PortablemcEnvironment(unittest.TestCase):
    def test_managed_jdk_validation_rejects_wrong_version_and_jre(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with tempfile.TemporaryDirectory() as tmp:
            import toolchain

            home = (
                Path(tmp).resolve()
                / "jdk"
                / (
                    "temurin-"
                    + toolchain.JAVA_VERSION
                    + "-"
                    + "-".join(toolchain.host())
                )
            )
            binary = home / "bin"
            binary.mkdir(parents=True)
            extension = ".exe" if sys.platform == "win32" else ""
            java = binary / ("java" + extension)
            java.touch()
            with (
                patch.dict(
                    os.environ,
                    {
                        "BF_DEV_HOME": tmp,
                        "JAVA_HOME": "/external/jdk",
                        "PATH": "other path",
                    },
                ),
                patch.object(devstack.subprocess, "run") as probe,
            ):
                probe.return_value = SimpleNamespace(
                    returncode=0, stderr='openjdk version "17.0.2"', stdout=""
                )
                for tool in ("javac", "jdb"):
                    (binary / (tool + extension)).touch()
                with self.assertRaisesRegex(SystemExit, "version mismatch"):
                    devstack.java_exe()
                probe.return_value.stderr = (
                    "OpenJDK Runtime Environment Temurin-" + toolchain.JAVA_VERSION
                )
                (binary / ("jdb" + extension)).unlink()
                with self.assertRaisesRegex(SystemExit, "complete managed JDK"):
                    devstack.java_exe()
                for tool in ("javac", "jdb"):
                    (binary / (tool + extension)).touch()
                java.chmod(0o755)
                self.assertEqual(devstack.java_exe(), str(java))
                self.assertEqual(
                    Path(os.environ["JAVA_HOME"]).resolve(), home.resolve()
                )
                self.assertEqual(
                    Path(os.environ["PATH"].split(os.pathsep)[0]).resolve(),
                    binary.resolve(),
                )

    def test_restart_replaces_external_java_in_saved_launch_command(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            receipt = home / "client.cmd"
            (home / "game").mkdir()
            receipt.write_text(
                json.dumps(
                    {
                        "command": ["/external/java", "--gameDir", str(home / "game")],
                        "game_dir": str(home / "game"),
                    }
                )
            )
            with (
                patch.object(devstack, "HOME", home),
                patch.object(devstack, "CLIENT_CMD_FILE", receipt),
                patch.object(
                    devstack, "java_exe", return_value="/managed/java"
                ) as java,
                patch.object(
                    devstack, "start_process", return_value=(123, None)
                ) as start,
                patch.object(devstack, "client_environment", return_value={}),
            ):
                with patch.object(devstack, "wait_client_ready") as ready:
                    devstack.client_restart(home / "game")
                ready.assert_called_once_with(123)
                java.assert_called_once_with()
                self.assertEqual(
                    start.call_args.args[0],
                    ["/managed/java", "--gameDir", str(home / "game")],
                )

    def test_server_start_parses_pose_debug(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with (
            patch.object(
                devstack.sys, "argv", ["devstack", "server", "start", "--pose-debug"]
            ),
            patch.object(devstack, "server_start", return_value=0) as start,
        ):
            with self.assertRaises(SystemExit) as stopped:
                devstack.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertTrue(start.call_args.args[0].pose_debug)

    def test_server_start_gate_keeps_the_production_login_gate(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        for gate, pose_debug in (
            (False, False),
            (True, False),
            (False, True),
            (True, True),
        ):
            with (
                self.subTest(gate=gate, pose_debug=pose_debug),
                tempfile.TemporaryDirectory() as tmp,
            ):
                home = Path(tmp)
                server = home / "server"
                server.mkdir()
                (server / "fabric-server-launch.jar").write_bytes(b"")
                log = server / "server.out"
                log.write_text("Done (1s)!")
                with (
                    patch.object(devstack, "SERVER", server),
                    patch.object(devstack, "LOG", log),
                    patch.object(devstack, "server_pid", side_effect=[None, 1]),
                    patch.object(devstack, "port_open", return_value=False),
                    patch.object(devstack, "java_exe", return_value="java"),
                    patch.dict(devstack.os.environ, {"BLOCKFIELD_DEV": "1"}),
                    patch.object(
                        devstack, "start_process", return_value=(1, None)
                    ) as start,
                ):
                    devstack.server_start(
                        SimpleNamespace(
                            xms="1G",
                            xmx="1G",
                            debug=False,
                            gate=gate,
                            pose_debug=pose_debug,
                        )
                    )
                command = start.call_args.args[0]
                self.assertEqual("-Dblockfield.poseDebug=true" in command, pose_debug)
                if pose_debug:
                    self.assertLess(
                        command.index("-Dblockfield.poseDebug=true"),
                        command.index("-jar"),
                    )
                env = start.call_args.kwargs["env"]
                self.assertEqual(env.get("BLOCKFIELD_DEV"), None if gate else "1")
                self.assertEqual(
                    env.get("BLOCKFIELD_BUILD_FILE"),
                    str(server / "build.json") if gate else None,
                )

    def test_watch_activates_java_after_ownership_check_before_rebuild(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack
        import watch

        args = SimpleNamespace(
            side="server", pos_side=None, port=None, game_dir=None, poll=0.1, once=True
        )
        order = []
        with (
            patch.object(devstack, "process_owner", return_value="own"),
            patch.object(
                devstack,
                "process_record",
                return_value={
                    "live": True,
                    "native": "linux",
                    "start_ticks": 42,
                    "owner": "own",
                    "pid": 123,
                    "cwd": str(devstack.SERVER),
                },
            ) as record,
            patch.object(devstack, "server_pid", return_value=123),
            patch.object(devstack, "process_runtime_matches", return_value=True),
            patch.object(devstack, "listener_owned", return_value=True),
            patch.object(
                devstack, "java_exe", side_effect=lambda: order.append("java")
            ) as java,
            patch.object(
                watch, "watch_loop", side_effect=lambda *a, **k: order.append("rebuild")
            ),
        ):
            devstack.watch(args)
            self.assertEqual(order, ["java", "rebuild"])
            java.reset_mock()
            record.return_value = {"live": True, "native": "linux", "owner": "other"}
            with self.assertRaisesRegex(SystemExit, "current session"):
                devstack.watch(args)
            java.assert_not_called()

    def test_watch_both_rejects_either_foreign_process_before_java(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack
        import watch

        args = SimpleNamespace(
            side="both", pos_side=None, port=None, game_dir=None, poll=0, once=True
        )
        for foreign in (devstack.PID_FILE, devstack.CLIENT_PID_FILE):

            def record(path):
                server = path == devstack.PID_FILE
                return {
                    "live": True,
                    "native": "linux",
                    "start_ticks": 42,
                    "owner": "other" if path == foreign else "own",
                    "pid": 123 if server else 456,
                    "cwd": str(devstack.SERVER if server else devstack.HOME / "play"),
                }

            with (
                self.subTest(foreign=foreign),
                patch.object(devstack, "process_owner", return_value="own"),
                patch.object(devstack, "process_record", side_effect=record),
                patch.object(devstack, "server_pid", return_value=123),
                patch.object(devstack, "client_pid", return_value=456),
                patch.object(devstack, "process_runtime_matches", return_value=True),
                patch.object(devstack, "listener_owned", return_value=True),
                patch.object(devstack, "java_exe") as java,
                patch.object(watch, "watch_loop") as loop,
            ):
                with self.assertRaisesRegex(SystemExit, "current session"):
                    devstack.watch(args)
                java.assert_not_called()
                loop.assert_not_called()

    def test_watch_both_rejects_foreign_jdwp_endpoint_before_java(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack
        import watch

        args = SimpleNamespace(
            side="both", pos_side=None, port=None, game_dir=None, poll=0, once=True
        )

        def record(path):
            server = path == devstack.PID_FILE
            return {
                "live": True,
                "native": "linux",
                "start_ticks": 42,
                "owner": "own",
                "pid": 123 if server else 456,
                "cwd": str(devstack.SERVER if server else devstack.HOME / "play"),
            }

        for foreign_port in (watch.SERVER_JDWP, watch.CLIENT_JDWP):
            with (
                self.subTest(port=foreign_port),
                patch.object(devstack, "process_owner", return_value="own"),
                patch.object(devstack, "process_record", side_effect=record),
                patch.object(devstack, "server_pid", return_value=123),
                patch.object(devstack, "client_pid", return_value=456),
                patch.object(devstack, "process_runtime_matches", return_value=True),
                patch.object(
                    devstack,
                    "listener_owned",
                    side_effect=lambda pid, port: port != foreign_port,
                ),
                patch.object(devstack, "java_exe") as java,
                patch.object(watch, "watch_loop") as loop,
            ):
                with self.assertRaisesRegex(SystemExit, "JDWP"):
                    devstack.watch(args)
                java.assert_not_called()
                loop.assert_not_called()

    def test_watch_both_rejects_a_shared_port_before_initialization(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        args = SimpleNamespace(
            side="both", pos_side=None, port=5007, game_dir=None, poll=0, once=True
        )
        with (
            patch.object(devstack, "process_record") as record,
            patch.object(devstack, "java_exe") as java,
        ):
            with self.assertRaisesRegex(SystemExit, "--port"):
                devstack.watch(args)
            record.assert_not_called()
            java.assert_not_called()

    def test_watch_both_once_restarts_each_side_with_the_exact_built_jar(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack
        import watch

        args = SimpleNamespace(
            side="both", pos_side=None, port=None, game_dir=None, poll=0, once=True
        )
        records = {
            path: {
                "live": True,
                "native": "linux",
                "start_ticks": pid,
                "owner": "own",
                "pid": pid,
                "cwd": str(directory),
            }
            for side, path, directory, pid in (
                ("server", devstack.PID_FILE, devstack.SERVER, 123),
                ("client", devstack.CLIENT_PID_FILE, devstack.HOME / "play", 456),
            )
        }
        events = []
        built = {side: Path(f"exact-{side}.jar") for side in ("server", "client")}

        def started(side):
            path = devstack.PID_FILE if side == "server" else devstack.CLIENT_PID_FILE
            records[path].update(
                start_ticks=records[path]["start_ticks"] + 1,
                pid=records[path]["pid"] + 1,
            )
            events.append(("start", side))

        def loop(side, port, mods, game, **kwargs):
            self.assertEqual((side, port), ("both", None))
            self.assertTrue(kwargs["once"])
            for selected in ("server", "client"):
                kwargs["restart"](selected, built[selected])
            return {selected: "restart" for selected in ("server", "client")}

        with (
            patch.object(devstack, "process_owner", return_value="own"),
            patch.object(devstack, "process_record", side_effect=records.get),
            patch.object(devstack, "process_runtime_matches", return_value=True),
            patch.object(devstack, "listener_owned", return_value=True),
            patch.object(devstack, "java_exe"),
            patch.object(
                devstack,
                "server_stop",
                side_effect=lambda _: events.append(("stop", "server")),
            ),
            patch.object(
                devstack,
                "client_stop",
                side_effect=lambda: events.append(("stop", "client")),
            ),
            patch.object(
                devstack, "server_start", side_effect=lambda _: started("server")
            ),
            patch.object(
                devstack, "client_restart", side_effect=lambda _: started("client")
            ),
            patch.object(
                watch,
                "deploy_built_jar",
                side_effect=lambda side, jar, mods, game: events.append(
                    ("deploy", side, jar)
                ),
            ) as deploy,
            patch.object(watch, "watch_loop", side_effect=loop),
        ):
            devstack.watch(args)
            self.assertEqual(deploy.call_count, 2)
            for call in deploy.call_args_list:
                side, jar, mods, game = call.args
                self.assertEqual(jar, built[side])
                self.assertEqual(mods, devstack.SERVER / "mods")
                self.assertEqual(game, devstack.HOME / "play")
        self.assertEqual(
            events,
            [
                ("stop", "server"),
                ("deploy", "server", built["server"]),
                ("start", "server"),
                ("stop", "client"),
                ("deploy", "client", built["client"]),
                ("start", "client"),
            ],
        )

    @unittest.skipUnless(sys.platform == "linux", "Linux /proc socket ownership")
    def test_listener_owned_matches_the_process_socket_inode(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(devstack.listener_owned(os.getpid(), port))
            self.assertFalse(devstack.listener_owned(2147483647, port))

    @unittest.skipUnless(sys.platform == "linux", "Linux /proc process identity")
    def test_runtime_matches_checks_actual_pid_birth_and_working_directory(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        process = Path("/proc") / str(os.getpid())
        cwd = process.joinpath("cwd").resolve()
        ticks = int((process / "stat").read_text().rsplit(")", 1)[1].split()[19])
        record = {"pid": os.getpid(), "cwd": str(cwd), "start_ticks": ticks}
        self.assertTrue(devstack.process_runtime_matches(record, cwd))
        self.assertFalse(
            devstack.process_runtime_matches({**record, "start_ticks": ticks + 1}, cwd)
        )
        self.assertFalse(devstack.process_runtime_matches(record, cwd / "foreign"))
        self.assertFalse(
            devstack.process_runtime_matches(
                {**record, "cwd": str(cwd / "foreign")}, cwd / "foreign"
            )
        )
        self.assertFalse(
            devstack.process_runtime_matches({**record, "pid": 2147483647}, cwd)
        )

    def test_distribution_uses_the_server_checkout_as_lobby_source(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack, "HOME", Path(tmp) / "own stack"),
            patch.object(devstack, "ensure_localmods"),
            patch.object(devstack, "fetch_mod_jars"),
            patch.object(
                devstack, "fetch_world_seed", return_value=Path(tmp) / "seed.zip"
            ),
            patch.object(devstack, "run_pack_build") as build,
        ):
            checkout = Path(tmp) / "server checkout"
            checkout.mkdir()
            devstack.build_distribution(checkout)
            command = build.call_args.args[0]
            self.assertEqual(
                command[command.index("--server-source") + 1],
                checkout.resolve() / "server",
            )
            self.assertEqual(build.call_args.kwargs["cwd"], checkout)

    def test_wrong_cached_version_is_rebuilt_using_this_python(self):
        sys.path.insert(0, str(bf.ROOT / "scripts/dev"))
        import devstack

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack, "HOME", Path(tmp)),
            patch.object(devstack.subprocess, "run") as probe,
            patch.object(devstack, "run") as build,
        ):
            executable = (
                Path(tmp)
                / "pmc"
                / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            )
            executable.parent.mkdir(parents=True)
            executable.touch()
            for version in ("4.4.0", "4.4.1"):
                build.reset_mock()
                probe.return_value = SimpleNamespace(
                    returncode=0, stdout=f"{sys.version.split()[0]} {version}\n"
                )
                self.assertEqual(devstack.pmc_python(), str(executable))
                if version == "4.4.1":
                    build.assert_not_called()
                else:
                    self.assertEqual(
                        build.call_args_list[0].args[0],
                        [
                            sys.executable,
                            "-I",
                            "-m",
                            "venv",
                            "--clear",
                            Path(tmp) / "pmc",
                        ],
                    )
                    self.assertEqual(
                        build.call_args_list[1].args[0][-1], "portablemc==4.4.1"
                    )
                    self.assertIn(
                        "--disable-pip-version-check", build.call_args_list[1].args[0]
                    )
            probe.return_value.stdout = "3.11.0 4.4.1\n"
            devstack.pmc_python()
            self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main()
