"""Verify isolation before resuming children; native probes run only on Windows."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import windows_headless  # noqa: E402
import windows_headless_probe  # noqa: E402
import windows_process  # noqa: E402


class Launcher(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            patch.object(windows_headless.ctypes, "set_last_error", create=True)
        )
        self.last_error = self.enterContext(
            patch.object(
                windows_headless.ctypes, "get_last_error", return_value=0, create=True
            )
        )
        self.enterContext(
            patch.object(
                windows_headless.ctypes,
                "WinError",
                side_effect=lambda error: OSError(f"Windows error {error}"),
                create=True,
            )
        )
        self.user, self.kernel = Mock(), Mock()
        self.user.CreateWindowStationW.return_value = 101
        self.user.CreateDesktopW.return_value = 202
        self.user.GetProcessWindowStation.return_value = 99
        self.user.GetThreadDesktop.return_value = 202
        self.kernel.WaitForSingleObject.return_value = 0
        self.kernel.ResumeThread.return_value = 1
        self.events = []
        self.station = "Blockfield-token"
        self.desktop = "Blockfield-token-game"
        self.names = {101: self.station, 202: self.desktop}
        self.user.GetUserObjectInformationW.side_effect = self.object_info
        self.kernel.CreateProcessW.side_effect = self.create
        self.kernel.InitializeProcThreadAttributeList.side_effect = self.attributes
        self.kernel.DuplicateHandle.side_effect = self.duplicate
        self.identity = {"pid": 321, "birth": 1234, "exe": "python.exe"}
        self.child_desktop = 202

    def object_info(self, handle, index, output, _size, _needed):
        if index == 2:
            output.value = self.names[handle]
        else:
            output._obj.dwFlags = 0
        return True

    def attributes(self, buffer, _count, _flags, size):
        size._obj.value = 64
        return buffer is not None

    def duplicate(self, _source, handle, _target, output, *_):
        output._obj.value = handle + 1000
        return True

    def create(
        self,
        application,
        arguments,
        _process_security,
        _thread_security,
        inherit,
        flags,
        env,
        cwd,
        startup,
        information,
    ):
        self.events.append("created suspended")
        self.assertTrue(inherit)
        self.assertEqual(flags, 0x08080604)
        self.assertEqual(
            startup._obj.StartupInfo.lpDesktop, self.station + "\\" + self.desktop
        )
        self.assertEqual(startup._obj.StartupInfo.hStdInput, 1001)
        self.assertEqual(startup._obj.StartupInfo.hStdOutput, 1002)
        self.assertEqual(startup._obj.StartupInfo.hStdError, 1002)
        self.assertEqual(application, "python.exe")
        self.assertEqual(arguments.value, 'python.exe "path with spaces & literal.py"')
        self.assertEqual(cwd, "working directory & space")
        environment = env[:]
        self.assertIn("BF_HEADLESS_STATION=" + self.station + "\0", environment)
        self.assertIn("BF_HEADLESS_OWNER=current\0", environment)
        self.assertIn("ALSOFT_DRIVERS=null\0", environment)
        self.assertNotIn("DISPLAY=", environment)
        information._obj.hProcess = 303
        information._obj.hThread = 404
        information._obj.dwProcessId = 321
        information._obj.dwThreadId = 654
        return True

    def launch(self):
        self.user.GetThreadDesktop.side_effect = lambda thread: (
            self.events.append("checked child desktop") or self.child_desktop
        )
        self.kernel.ResumeThread.side_effect = lambda handle: (
            self.events.append("resumed") or 1
        )
        with (
            patch.object(
                windows_headless, "_api", return_value=(self.user, self.kernel)
            ),
            patch.object(
                windows_headless.uuid, "uuid4", return_value=Mock(hex="token")
            ),
            patch.object(windows_headless, "_file_handle", side_effect=[1, 2]),
            patch.object(windows_process, "_api", return_value=self.kernel),
            patch.object(
                windows_process, "_identity", return_value=self.identity
            ) as identity,
        ):
            process = windows_headless.launch(
                ["python.exe", "path with spaces & literal.py"],
                "working directory & space",
                {
                    "DISPLAY": ":0",
                    "WAYLAND_DISPLAY": "wayland-0",
                    "ALSOFT_DRIVERS": "wasapi",
                },
                Mock(),
                "current",
            )
        identity.assert_called_once_with(self.kernel, 303, 321)
        return process

    def test_explicit_noninteractive_station_and_desktop_before_resume(self):
        process = self.launch()
        self.assertEqual(
            self.events, ["created suspended", "checked child desktop", "resumed"]
        )
        self.assertEqual(process.pid, 321)
        self.assertEqual(process.identity, self.identity)
        self.assertEqual(process.backend["owner"], "current")
        self.assertTrue(process.backend["noninteractive"])
        self.assertTrue(process.backend["child_desktop_verified"])
        self.assertEqual(process.backend["assignment"], "lpDesktop")
        self.user.SetProcessWindowStation.assert_has_calls(
            [unittest.mock.call(101), unittest.mock.call(99)]
        )
        self.user.CreateWindowStationW.assert_called_once_with(
            self.station, 1, 0x037F, None
        )
        self.user.CreateDesktopW.assert_called_once_with(
            self.desktop, None, None, 0, 0x00C7, None
        )
        self.assertEqual(
            self.kernel.UpdateProcThreadAttribute.call_args.args[2], 0x20002
        )
        self.assertEqual(
            list(self.kernel.UpdateProcThreadAttribute.call_args.args[3]), [1001, 1002]
        )
        self.kernel.TerminateProcess.assert_not_called()
        self.user.CloseDesktop.assert_not_called()
        self.user.CloseWindowStation.assert_not_called()
        process.close()
        process.close()
        self.user.CloseDesktop.assert_called_once_with(202)
        self.user.CloseWindowStation.assert_called_once_with(101)

    def test_visible_backend_fails_before_child_creation(self):
        original = self.object_info

        def visible(handle, index, output, *args):
            result = original(handle, index, output, *args)
            if index == 1:
                output._obj.dwFlags = 1
            return result

        self.user.GetUserObjectInformationW.side_effect = visible
        with self.assertRaisesRegex(RuntimeError, "noninteractive"):
            self.launch()
        self.kernel.CreateProcessW.assert_not_called()
        self.user.CloseWindowStation.assert_called_once_with(101)

    def test_wrong_child_desktop_terminates_exact_suspended_child(self):
        self.names[202] = "Default"
        original = self.object_info
        calls = 0

        def name(handle, index, output, *args):
            nonlocal calls
            result = original(handle, index, output, *args)
            if index == 2 and handle == 202:
                calls += 1
                if calls == 1:
                    output.value = self.desktop
            return result

        self.user.GetUserObjectInformationW.side_effect = name
        with self.assertRaisesRegex(RuntimeError, "suspended Windows child"):
            self.launch()
        self.kernel.ResumeThread.assert_not_called()
        self.kernel.TerminateProcess.assert_called_once_with(303, 1)
        self.kernel.WaitForSingleObject.assert_called_once_with(303, 5000)
        self.user.CloseDesktop.assert_called_once_with(202)
        self.user.CloseWindowStation.assert_called_once_with(101)

    def test_created_child_cannot_run_without_native_identity(self):
        self.identity = None
        with self.assertRaisesRegex(RuntimeError, "before registration"):
            self.launch()
        self.kernel.ResumeThread.assert_not_called()
        self.kernel.TerminateProcess.assert_called_once_with(303, 1)

    def test_unconnected_suspended_thread_retains_explicit_private_assignment(self):
        self.child_desktop = None
        process = self.launch()
        self.assertFalse(process.backend["child_desktop_verified"])
        self.assertTrue(process.backend["noninteractive"])
        self.assertEqual(process.backend["assignment"], "lpDesktop")
        self.kernel.ResumeThread.assert_called_once_with(404)
        process.close()

    def test_suspended_desktop_api_error_cannot_fall_back(self):
        self.child_desktop = None
        self.last_error.return_value = 5
        with self.assertRaisesRegex(OSError, "Windows error 5"):
            self.launch()
        self.kernel.ResumeThread.assert_not_called()
        self.kernel.TerminateProcess.assert_called_once_with(303, 1)

    def test_wait_deadline_and_termination_use_the_retained_native_handle(self):
        process = self.launch()
        self.kernel.WaitForSingleObject.return_value = 258
        self.assertIsNone(process.poll())
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
        with self.assertRaises(ValueError):
            process.wait(timeout=None)
        with self.assertRaises(ValueError):
            process.wait(timeout=float("inf"))
        self.kernel.WaitForSingleObject.side_effect = [258, 0, 0]
        self.kernel.GetExitCodeProcess.side_effect = lambda _handle, code: (
            setattr(code._obj, "value", 1) or True
        )
        process.terminate()
        self.kernel.TerminateProcess.assert_called_once_with(303, 1)
        process.close()

    def test_probe_refuses_a_missing_receipt_before_querying_station(self):
        with (
            patch.object(
                windows_headless, "_api", return_value=(self.user, self.kernel)
            ),
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RuntimeError, "receipt is missing"),
        ):
            windows_headless.current_backend()
        self.user.GetProcessWindowStation.assert_not_called()

    def test_probe_checks_its_exact_station_desktop_and_visibility(self):
        self.user.GetProcessWindowStation.return_value = 101
        with (
            patch.object(
                windows_headless, "_api", return_value=(self.user, self.kernel)
            ),
            patch.dict(
                os.environ,
                {
                    "BF_HEADLESS_STATION": self.station,
                    "BF_HEADLESS_DESKTOP": self.desktop,
                    "BF_HEADLESS_OWNER": "current",
                },
            ),
        ):
            proof = windows_headless.current_backend()
            self.assertEqual(
                proof,
                {
                    "station": self.station,
                    "desktop": self.desktop,
                    "owner": "current",
                    "visible": False,
                },
            )
            self.names[202] = "Default"
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                windows_headless.current_backend()


class WglProbe(unittest.TestCase):
    def setUp(self):
        self.user, self.gdi, self.gl = Mock(), Mock(), Mock()
        self.user.GetDC.return_value = 42
        self.gdi.ChoosePixelFormat.return_value = 7
        self.gl.wglCreateContext.return_value = 43
        self.gl.glGetString.side_effect = [
            b"4.6 (Compatibility Profile) Mesa 26.2.2",
            b"llvmpipe (LLVM 22.1.0, 256 bits)",
            b"Mesa",
        ]
        self.gl.glGetError.return_value = 0
        self.pixel = [64, 128, 191, 255]
        self.gl.glReadPixels.side_effect = self.read_pixel

    def read_pixel(self, *_args):
        for index, channel in enumerate(self.pixel):
            _args[-1][index] = channel

    def render(self, path):
        with (
            patch.object(windows_headless_probe, "current_backend") as verify,
            patch.object(
                windows_headless_probe.os,
                "add_dll_directory",
                return_value=nullcontext(),
                create=True,
            ) as directory,
            patch.object(
                windows_headless_probe.ctypes,
                "WinDLL",
                side_effect=[self.gl, self.gdi],
                create=True,
            ) as load,
        ):
            details = windows_headless_probe.render_wgl(self.user, 41, path)
        verify.assert_called_once_with()
        directory.assert_called_once_with(str(path.parent))
        load.assert_has_calls(
            [
                unittest.mock.call(str(path), use_last_error=True),
                unittest.mock.call("gdi32", use_last_error=True),
            ]
        )
        return details

    def test_software_render_proof_reads_the_known_pixel_and_releases_only_its_dc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opengl32.dll"
            path.touch()
            details = self.render(path)
        self.assertIn("llvmpipe", details["renderer"])
        self.assertEqual(details["pixel"], self.pixel)
        self.assertEqual(
            windows_headless_probe.ctypes.sizeof(
                windows_headless_probe.PixelFormatDescriptor
            ),
            40,
        )
        self.gdi.SetPixelFormat.assert_called_once()
        self.gl.wglMakeCurrent.assert_has_calls(
            [unittest.mock.call(42, 43), unittest.mock.call(None, None)]
        )
        self.gl.wglDeleteContext.assert_called_once_with(43)
        self.user.ReleaseDC.assert_called_once_with(41, 42)

    def test_pixel_failure_cleans_up_the_owned_context_and_dc(self):
        self.pixel = [0, 0, 0, 255]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opengl32.dll"
            path.touch()
            with self.assertRaisesRegex(RuntimeError, "pixel proof failed"):
                self.render(path)
        self.gl.wglDeleteContext.assert_called_once_with(43)
        self.user.ReleaseDC.assert_called_once_with(41, 42)

    def test_backend_proof_precedes_library_loading_and_window_access(self):
        with (
            patch.object(
                windows_headless_probe,
                "current_backend",
                side_effect=RuntimeError("unverified station"),
            ),
            patch.object(windows_headless_probe.ctypes, "WinDLL", create=True) as load,
            self.assertRaisesRegex(RuntimeError, "unverified station"),
        ):
            windows_headless_probe.render_wgl(self.user, 41, "C:\\Mesa\\opengl32.dll")
        load.assert_not_called()
        self.user.GetDC.assert_not_called()


class HandoffProbe(unittest.TestCase):
    def test_handoff_marker_cannot_leave_its_owned_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                patch.object(Path, "cwd", return_value=home),
                self.assertRaisesRegex(ValueError, "child-owned working directory"),
            ):
                windows_headless_probe.wait_parent_release(
                    {}, home.parent / "outside-proof.json", home / "release"
                )
            self.assertEqual(list(home.iterdir()), [])

    def test_release_wait_has_a_hard_deadline_and_preserves_ready_proof(self):
        proof = {"station": "Blockfield-test", "visible": False, "window_created": True}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            ready = home / "window-proof.json"
            with (
                patch.object(Path, "cwd", return_value=home),
                patch.object(
                    windows_headless_probe.time, "monotonic", side_effect=[100, 116]
                ),
                self.assertRaisesRegex(TimeoutError, "15 seconds"),
            ):
                windows_headless_probe.wait_parent_release(
                    proof, ready, home / "release"
                )
            self.assertEqual(json.loads(ready.read_text()), proof)
            self.assertFalse(ready.with_suffix(".tmp").exists())

    def test_own_window_remains_alive_through_handoff_and_backend_recheck(self):
        backend = {
            "station": "Blockfield-test",
            "desktop": "Blockfield-game",
            "visible": False,
        }
        user = Mock()
        user.CreateWindowExW.return_value = 43
        user.IsWindow.return_value = True

        def release(proof, ready, gate):
            self.assertTrue(proof["window_created"])
            self.assertEqual((ready, gate), ("ready.json", "release"))
            user.DestroyWindow.assert_not_called()

        with (
            patch.object(
                windows_headless_probe, "current_backend", return_value=backend
            ) as verify,
            patch.object(windows_headless_probe, "_api", return_value=(user, Mock())),
            patch.object(
                windows_headless_probe, "wait_parent_release", side_effect=release
            ),
            patch.object(
                sys,
                "argv",
                ["probe", "--ready-file", "ready.json", "--release-file", "release"],
            ),
            patch("builtins.print") as output,
        ):
            windows_headless_probe.main()
        self.assertEqual(verify.call_count, 3)
        user.IsWindow.assert_called_once_with(43)
        user.DestroyWindow.assert_called_once_with(43)
        self.assertTrue(
            json.loads(output.call_args.args[0])["window_survived_parent_close"]
        )


class NativeLauncher(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "win32", "requires an actual Windows isolated backend"
    )
    def test_parent_retains_backend_until_child_window_is_ready_then_closes(self):
        probe = Path(windows_headless.__file__).with_name("windows_headless_probe.py")
        with tempfile.TemporaryDirectory(
            prefix="bf verified child connection & "
        ) as tmp:
            home = Path(tmp)
            gate = home / "parent-handles-closed"
            ready = home / "child-window-proof.json"
            log_path = home / "retained backend child.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = windows_headless.launch(
                    [
                        sys.executable,
                        "-I",
                        "-X",
                        "utf8",
                        str(probe),
                        "--window",
                        "--ready-file",
                        str(ready),
                        "--release-file",
                        str(gate),
                    ],
                    home,
                    os.environ,
                    log,
                    "native-retained-fixture",
                )
            try:
                with windows_process._handle(process.pid, terminate=True) as (
                    api,
                    handle,
                ):
                    self.assertTrue(
                        windows_process.matches(
                            process.identity,
                            windows_process._identity(api, handle, process.pid),
                        )
                    )
                    try:
                        deadline = time.monotonic() + 15
                        while not ready.exists():
                            self.assertEqual(
                                api.WaitForSingleObject(handle, 0),
                                258,
                                log_path.read_text(),
                            )
                            self.assertLess(
                                time.monotonic(), deadline, "child readiness deadline"
                            )
                            time.sleep(0.01)
                        initial = json.loads(ready.read_text())
                        self.assertEqual(initial["station"], process.backend["station"])
                        self.assertEqual(initial["desktop"], process.backend["desktop"])
                        self.assertEqual(initial["owner"], "native-retained-fixture")
                        self.assertFalse(initial["visible"])
                        self.assertTrue(initial["window_created"])
                        process.close()
                        gate.touch()
                        self.assertEqual(
                            api.WaitForSingleObject(handle, 15000),
                            0,
                            log_path.read_text(),
                        )
                        code = windows_headless.wintypes.DWORD()
                        windows_headless._check(
                            process._kernel.GetExitCodeProcess(
                                handle, windows_headless.ctypes.byref(code)
                            )
                        )
                        self.assertEqual(code.value, 0, log_path.read_text())
                        proof = json.loads(log_path.read_text())
                        self.assertEqual(proof["station"], process.backend["station"])
                        self.assertEqual(proof["desktop"], process.backend["desktop"])
                        self.assertEqual(proof["owner"], "native-retained-fixture")
                        self.assertFalse(proof["visible"])
                        self.assertTrue(proof["window_created"])
                        self.assertTrue(proof["window_survived_parent_close"])
                    finally:
                        if api.WaitForSingleObject(handle, 0) == 258:
                            windows_headless._check(api.TerminateProcess(handle, 1))
                            self.assertEqual(api.WaitForSingleObject(handle, 5000), 0)
            finally:
                if process._handle and process.poll() is None:
                    process.terminate()
                process.close()

    @unittest.skipUnless(
        sys.platform == "win32", "requires an actual Windows isolated backend"
    )
    def test_owned_child_proves_station_desktop_and_its_own_window(self):
        probe = Path(windows_headless.__file__).with_name("windows_headless_probe.py")
        with tempfile.TemporaryDirectory(prefix="bf isolated station & ") as tmp:
            home = Path(tmp)
            log_path = home / "isolated child & proof.log"
            with log_path.open("w", encoding="utf-8") as log:
                process = windows_headless.launch(
                    [sys.executable, "-I", "-X", "utf8", str(probe), "--window"],
                    home,
                    {**os.environ, "DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
                    log,
                    "native-fixture",
                )
            try:
                self.assertEqual(process.wait(timeout=15), 0, log_path.read_text())
                proof = json.loads(log_path.read_text())
                self.assertEqual(proof["station"], process.backend["station"])
                self.assertEqual(proof["desktop"], process.backend["desktop"])
                self.assertEqual(proof["owner"], "native-fixture")
                self.assertFalse(proof["visible"])
                self.assertTrue(proof["window_created"])
                self.assertIsNone(windows_process.identity(process.pid))
            finally:
                if process.poll() is None:
                    process.terminate()
                process.close()


if __name__ == "__main__":
    unittest.main()
