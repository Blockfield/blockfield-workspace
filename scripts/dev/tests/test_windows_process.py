"""Windows ownership guards; native API smoke runs only on a real Windows runner."""

import ctypes
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devstack  # noqa: E402
import windows_process  # noqa: E402


IDENTITY = {"pid": 321, "birth": 123456789, "exe": "C:\\JDK 21\\bin\\java.exe"}


class Ownership(unittest.TestCase):
    def test_play_stop_cli_does_not_prepare_or_start_a_client(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack, "HOME", Path(tmp)),
            patch.object(devstack.sys, "argv", ["devstack", "play", "--stop"]),
            patch.object(devstack, "client_stop") as stop,
            patch.object(devstack, "client_pid") as pid,
            patch.object(devstack, "build_client_pack") as build,
            patch.object(devstack, "start_process") as start,
            self.assertRaises(SystemExit) as exit_status,
        ):
            devstack.main()
        self.assertIsNone(exit_status.exception.code)
        stop.assert_called_once_with()
        pid.assert_not_called()
        build.assert_not_called()
        start.assert_not_called()

    def test_native_record_refuses_other_owner_and_reused_pid(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "win32"),
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(windows_process, "identity", return_value=IDENTITY) as query,
            patch.object(windows_process, "stop") as stop,
        ):
            path = Path(tmp) / "client.pid"
            record = {**IDENTITY, "native": "windows", "owner": "other"}
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(SystemExit, "another session"):
                devstack.stop_process(path)
            stop.assert_not_called()
            record["owner"] = "current"
            record["birth"] += 1
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(SystemExit, "no longer matches"):
                devstack.stop_process(path)
            stop.assert_not_called()
            record["birth"] = IDENTITY["birth"]
            record["exe"] = "C:\\unrelated.exe"
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(SystemExit, "no longer matches"):
                devstack.stop_process(path)
            stop.assert_not_called()
            record["exe"] = IDENTITY["exe"]
            path.write_text(json.dumps(record))
            devstack.stop_process(path)
            self.assertFalse(path.exists())
            stop.assert_called_once()
            self.assertIsNone(stop.call_args.kwargs["graceful"])
            query.assert_called_with(321)

    def test_windows_start_records_actual_child_identity_and_returns(self):
        process = Mock(pid=321)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "win32"),
            patch.object(
                devstack.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True
            ),
            patch.object(devstack.subprocess, "Popen", return_value=process),
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(windows_process, "identity", return_value=IDENTITY),
        ):
            directory = Path(tmp)
            path = directory / "client.pid"
            command = [IDENTITY["exe"], "-Dgame.dir=C:\\Game Dir"]
            self.assertEqual(
                devstack.start_process(command, directory, path, directory / "log"),
                (321, None),
            )
            record = json.loads(path.read_text())
            self.assertEqual(record["birth"], IDENTITY["birth"])
            self.assertEqual(record["exe"], IDENTITY["exe"])
            self.assertEqual(record["owner"], "current")
            self.assertEqual(record["command"], command)
            process.wait.assert_not_called()

    def test_server_rcon_requires_its_listener_after_identity_check(self):
        events = []

        def stop_verified(record, graceful, **_):
            events.append("handle verified")
            graceful()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "win32"),
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(windows_process, "identity", return_value=IDENTITY),
            patch.object(windows_process, "stop", side_effect=stop_verified),
            patch.object(
                windows_process, "owns_listener", return_value=False
            ) as listener,
            patch.object(devstack, "Rcon") as rcon,
        ):
            path = Path(tmp) / "server.pid"
            path.write_text(
                json.dumps({**IDENTITY, "native": "windows", "owner": "current"})
            )
            with patch.object(devstack, "PID_FILE", path):
                with self.assertRaisesRegex(SystemExit, "RCON listener"):
                    devstack.stop_process(path)
                rcon.assert_not_called()
                self.assertTrue(path.exists())
                listener.assert_called_with(321, devstack.RCON_PORT)
                listener.side_effect = [True, False]
                with self.assertRaisesRegex(SystemExit, "RCON connection"):
                    devstack.stop_process(path)
                rcon.return_value.cmd.assert_not_called()
                rcon.return_value.s.close.assert_called_once()
                self.assertTrue(path.exists())
                rcon.reset_mock()
                listener.side_effect = None
                listener.return_value = True
                devstack.stop_process(path)
                rcon.assert_called_once_with(
                    port=devstack.RCON_PORT, pw=devstack.RCON_PASSWORD, timeout=5
                )
                rcon.return_value.cmd.assert_called_once_with("stop")
                rcon.return_value.s.close.assert_called_once()
                self.assertEqual(events, ["handle verified"] * 3)


class NativeApi(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "win32", "requires actual Windows batch execution"
    )
    def test_real_windows_gradle_wrapper_in_path_with_spaces(self):
        source = devstack.ROOT / "scripts/dev/testbot"
        with tempfile.TemporaryDirectory(prefix="bf gradle wrapper ") as tmp:
            checkout = Path(tmp) / "wrapper checkout with spaces"
            for name in (
                "gradlew.bat",
                "gradle/wrapper/gradle-wrapper.jar",
                "gradle/wrapper/gradle-wrapper.properties",
            ):
                target = checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source / name, target)
            with patch.dict(os.environ):
                devstack.java_exe()
                result = devstack.run(
                    [checkout / "gradlew.bat", "--version", "--no-daemon"],
                    cwd=checkout,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=180,
                )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Gradle 8.14.2", result.stdout)

    def test_stop_rechecks_identity_on_the_handle_used_to_signal(self):
        api, handle = Mock(), object()
        api.WaitForSingleObject.return_value = 0

        @contextmanager
        def open_handle(*_, **__):
            yield api, handle

        with (
            patch.object(windows_process, "_handle", open_handle),
            patch.object(windows_process, "_identity", return_value=IDENTITY),
        ):
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                windows_process.stop({**IDENTITY, "birth": 9})
            api.TerminateProcess.assert_not_called()
            windows_process.stop(IDENTITY)
            api.TerminateProcess.assert_called_once_with(handle, 0)
            api.WaitForSingleObject.assert_called_once_with(handle, 30000)
            api.TerminateProcess.reset_mock()
            graceful = Mock()
            windows_process.stop(IDENTITY, graceful=graceful)
            graceful.assert_called_once()
            api.TerminateProcess.assert_not_called()

    def test_tcp_table_checks_pid_and_network_byte_order_port(self):
        data = struct.pack("<7I", 1, 2, 0, socket.htons(25575), 0, 0, 321)
        api = Mock()

        def table(buffer, size, _, family, *__):
            payload = data if family == 2 else struct.pack("<I", 0)
            size._obj.value = len(payload)
            if buffer is None:
                return 122
            ctypes.memmove(buffer, payload, len(payload))
            return 0

        api.GetExtendedTcpTable.side_effect = table
        with patch.object(ctypes, "WinDLL", return_value=api, create=True):
            self.assertTrue(windows_process.owns_listener(321, 25575))
            self.assertFalse(windows_process.owns_listener(322, 25575))
            self.assertFalse(windows_process.owns_listener(321, 25576))

    @unittest.skipUnless(sys.platform == "win32", "requires actual Windows native APIs")
    def test_real_windows_listener_owner(self):
        self.assertGreaterEqual(devstack.available_memory_gib(), 0)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(windows_process.owns_listener(os.getpid(), port))
            self.assertFalse(windows_process.owns_listener(os.getpid() + 1, port))
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                connection, _ = listener.accept()
                with connection:
                    self.assertTrue(
                        windows_process.owns_listener(
                            os.getpid(), port, peer_port=client.getsockname()[1]
                        )
                    )

    @unittest.skipUnless(sys.platform == "win32", "requires actual Windows native APIs")
    def test_real_windows_child_birth_executable_and_safe_stop(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        try:
            record = windows_process.identity(process.pid)
            self.assertIsNotNone(record)
            self.assertEqual(record["pid"], process.pid)
            self.assertGreater(record["birth"], 0)
            self.assertEqual(
                os.path.normcase(record["exe"]),
                os.path.normcase(str(Path(sys.executable).resolve())),
            )
            with self.assertRaises(RuntimeError):
                windows_process.stop({**record, "birth": record["birth"] + 1})
            self.assertIsNone(process.poll())
            windows_process.stop(record, timeout=5)
            self.assertEqual(process.wait(timeout=5), 0)
            self.assertIsNone(windows_process.identity(process.pid))
        finally:
            if process.poll() is None:
                # Only this Popen object's exact child handle is used for fixture cleanup.
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
