"""Linux native ownership guards and bounded smoke checks on an exact child PID."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devstack  # noqa: E402
import linux_process  # noqa: E402


IDENTITY = {"pid": 321, "start_ticks": 42, "exe": "/jdk/bin/java", "cwd": "/stack"}


class Ownership(unittest.TestCase):
    def test_launch_records_native_identity_without_external_registration(self):
        process = Mock(pid=321)
        process.poll.return_value = None
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "linux"),
            patch.object(devstack.subprocess, "Popen", return_value=process) as launch,
            patch.object(devstack.subprocess, "run") as external,
            patch.object(devstack.shutil, "which") as lookup,
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(linux_process, "identity", return_value=IDENTITY),
        ):
            directory = Path(tmp)
            path, log = directory / "client.pid", directory / "client.log"
            command = [IDENTITY["exe"], "-Dgame.dir=/Game Dir"]
            self.assertEqual(
                devstack.start_process(command, directory, path, log), (321, None)
            )
            record = json.loads(path.read_text())
            for field in ("pid", "start_ticks", "exe"):
                self.assertEqual(record[field], IDENTITY[field])
            self.assertEqual(record["native"], "linux")
            self.assertEqual(record["owner"], "current")
            self.assertEqual(record["command"], command)
            self.assertEqual(record["log"], str(log))
            self.assertTrue(launch.call_args.kwargs["start_new_session"])
            self.assertEqual(launch.call_args.kwargs["cwd"], directory)
            process.wait.assert_not_called()
            external.assert_not_called()
            lookup.assert_not_called()

    def test_stop_preserves_foreign_or_changed_identity_records(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "linux"),
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(linux_process, "identity", return_value=IDENTITY),
            patch.object(linux_process, "stop") as stop,
            patch.object(devstack.subprocess, "run") as external,
        ):
            path = Path(tmp) / "client.pid"
            valid = {**IDENTITY, "native": "linux", "owner": "current"}
            for changed, message in (
                ({"owner": "other"}, "another session"),
                ({"start_ticks": 43}, "no longer matches"),
                ({"exe": "/unrelated"}, "no longer matches"),
                ({"cwd": "/unrelated"}, "no longer matches"),
            ):
                with self.subTest(changed=changed):
                    path.write_text(json.dumps({**valid, **changed}))
                    original = path.read_bytes()
                    with self.assertRaisesRegex(SystemExit, message):
                        devstack.stop_process(path)
                    self.assertEqual(path.read_bytes(), original)
                    stop.assert_not_called()
            path.write_text(json.dumps(valid))
            devstack.stop_process(path)
            stop.assert_called_once()
            self.assertIsNone(stop.call_args.kwargs["graceful"])
            self.assertFalse(path.exists())
            external.assert_not_called()

    def test_rcon_checks_listener_and_exact_connection_before_stop(self):
        def verified_stop(record, graceful, **_):
            graceful()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(devstack.sys, "platform", "linux"),
            patch.object(devstack, "process_owner", return_value="current"),
            patch.object(linux_process, "identity", return_value=IDENTITY),
            patch.object(linux_process, "stop", side_effect=verified_stop),
            patch.object(devstack, "listener_owned") as owned,
            patch.object(devstack, "Rcon") as rcon,
        ):
            path = Path(tmp) / "server.pid"
            path.write_text(
                json.dumps({**IDENTITY, "native": "linux", "owner": "current"})
            )
            with patch.object(devstack, "PID_FILE", path):
                owned.return_value = False
                with self.assertRaisesRegex(SystemExit, "RCON listener"):
                    devstack.stop_process(path)
                rcon.assert_not_called()
                owned.assert_called_with(321, devstack.RCON_PORT)
                owned.side_effect = [True, False]
                with self.assertRaisesRegex(SystemExit, "RCON connection"):
                    devstack.stop_process(path)
                rcon.return_value.cmd.assert_not_called()
                rcon.return_value.s.close.assert_called_once()
                self.assertTrue(path.exists())
                rcon.reset_mock()
                owned.side_effect = None
                owned.return_value = True
                devstack.stop_process(path)
                rcon.return_value.cmd.assert_called_once_with("stop")
                rcon.return_value.s.close.assert_called_once()
                self.assertFalse(path.exists())


@unittest.skipUnless(sys.platform == "linux", "requires Linux /proc and pidfd APIs")
class NativeApi(unittest.TestCase):
    def test_stop_validates_every_identity_field_before_graceful_or_signal(self):
        poll = Mock()
        poll.poll.return_value = []
        graceful = Mock()
        with (
            patch.object(linux_process.os, "pidfd_open", return_value=9) as opened,
            patch.object(linux_process.os, "close") as closed,
            patch.object(linux_process.select, "poll", return_value=poll),
            patch.object(linux_process, "identity", return_value=IDENTITY),
            patch.object(linux_process.signal, "pidfd_send_signal") as sent,
        ):
            for field, wrong in (
                ("pid", 322),
                ("start_ticks", 43),
                ("exe", "/unrelated"),
                ("cwd", "/unrelated"),
            ):
                with self.subTest(field=field):
                    record = {**IDENTITY, field: wrong}
                    with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                        linux_process.stop(record, graceful=graceful)
                    opened.assert_called_with(record["pid"])
                    closed.assert_called_with(9)
                    graceful.assert_not_called()
                    sent.assert_not_called()

    def test_owned_child_identity_refusals_timeout_and_pidfd_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(15)"],
                cwd=tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            handle = os.pidfd_open(process.pid)
            try:
                record = linux_process.identity(process.pid)
                self.assertIsNotNone(record)
                self.assertEqual(record["pid"], process.pid)
                self.assertGreater(record["start_ticks"], 0)
                self.assertEqual(record["exe"], str(Path(sys.executable).resolve()))
                self.assertEqual(record["cwd"], str(Path(tmp).resolve()))
                registry = Path(tmp) / "child.json"
                registry.write_text(
                    json.dumps({**record, "owner": "test", "native": "linux"})
                )
                graceful = Mock()
                for field, wrong in (
                    ("start_ticks", record["start_ticks"] + 1),
                    ("exe", "/unrelated"),
                    ("cwd", tmp + "/unrelated"),
                ):
                    with self.subTest(field=field):
                        with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                            linux_process.stop(
                                {**record, field: wrong}, graceful=graceful, timeout=1
                            )
                        graceful.assert_not_called()
                        self.assertIsNone(process.poll())
                self.assertFalse(
                    linux_process.matches(record, {**record, "pid": record["pid"] + 1})
                )
                with self.assertRaises(TimeoutError):
                    linux_process.stop(record, graceful=graceful, timeout=0)
                graceful.assert_called_once_with()
                self.assertIsNone(process.poll())
                linux_process.stop(record, timeout=5)
                self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
                self.assertIsNone(linux_process.identity(process.pid))
            finally:
                if process.poll() is None:
                    signal.pidfd_send_signal(handle, signal.SIGTERM)
                    process.wait(timeout=5)
                os.close(handle)

    def test_signal_stop_escalates_when_sigterm_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal, sys, time;"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                    "print(flush=True); time.sleep(30)",
                ],
                cwd=tmp,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                process.stdout.readline()
                record = linux_process.identity(process.pid)
                linux_process.stop(record, timeout=0.5)
                self.assertEqual(process.wait(timeout=5), -signal.SIGKILL)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()

    def test_listener_and_connection_require_exact_process_socket(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertTrue(devstack.listener_owned(os.getpid(), port))
            self.assertFalse(devstack.listener_owned(2147483647, port))
            with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                connection, _ = listener.accept()
                with connection:
                    peer = client.getsockname()[1]
                    self.assertTrue(devstack.listener_owned(os.getpid(), port, peer))
                    self.assertFalse(devstack.listener_owned(os.getpid(), port, port))


if __name__ == "__main__":
    unittest.main()
