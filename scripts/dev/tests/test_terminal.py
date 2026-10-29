"""Progress must finish, preserve errors and leave machine output untouched."""

import contextlib
import io
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import devstack  # noqa: E402
from terminal import progress  # noqa: E402


class TerminalOutput(io.StringIO):
    encoding = "utf-8"

    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty
        self.updated = threading.Event()
        self.frames = 0

    def isatty(self):
        return self.tty

    def write(self, text):
        result = super().write(text)
        self.frames += 1
        if self.frames > 1:
            self.updated.set()
        return result


class Terminal(unittest.TestCase):
    def test_build_updates_one_line_and_replays_logs_after_success_or_failure(self):
        for exit_code in (0, 7):
            output, logs = TerminalOutput(tty=True), io.StringIO()

            def build(command, **kwargs):
                self.assertEqual(kwargs["stderr"], devstack.subprocess.STDOUT)
                kwargs["stdout"].write("build details\n")
                self.assertTrue(output.updated.wait(timeout=2))
                self.assertEqual(logs.getvalue(), "")
                return SimpleNamespace(returncode=exit_code)

            with (
                self.subTest(exit_code=exit_code),
                patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""}),
                contextlib.redirect_stderr(output),
                contextlib.redirect_stdout(logs),
                patch.object(devstack.subprocess, "run", side_effect=build),
                patch(
                    "terminal.time.monotonic", side_effect=lambda: 100 + output.frames
                ),
                patch(
                    "terminal.shutil.get_terminal_size",
                    return_value=os.terminal_size((44, 24)),
                ),
            ):

                def operation():
                    return devstack.run(
                        ["python", "builder.py"],
                        message="Building client pack with a very long path",
                    )

                if exit_code:
                    with self.assertRaisesRegex(SystemExit, r"command failed \(7\)"):
                        operation()
                else:
                    self.assertEqual(operation().returncode, 0)
            text = output.getvalue()
            self.assertGreaterEqual(text.count("\r"), 3)
            self.assertEqual(text.count("\n"), 1)
            self.assertIn("⠙", text)
            self.assertIn("1s", text)
            self.assertNotIn("\033", text)
            self.assertEqual(logs.getvalue(), "build details\n")
            self.assertFalse(
                any(t.name == "cli-progress" for t in threading.enumerate())
            )

    def test_progress_animates_only_in_terminal_and_cleans_up_on_every_exit(self):
        for tty, term, failure in (
            (False, "xterm", None),
            (True, "dumb", None),
            (True, "xterm", None),
            (True, "xterm", RuntimeError("offline")),
            (True, "xterm", KeyboardInterrupt()),
        ):
            output, machine = TerminalOutput(tty), io.StringIO()
            with (
                self.subTest(tty=tty, term=term, failure=failure),
                patch.dict(os.environ, {"TERM": term, "NO_COLOR": ""}),
                contextlib.redirect_stderr(output),
                contextlib.redirect_stdout(machine),
            ):

                def operation():
                    with progress("Downloading assets"):
                        if tty and term != "dumb":
                            self.assertTrue(output.updated.wait(timeout=2))
                        print('{"ready": true}')
                        if failure:
                            raise failure

                if failure:
                    with self.assertRaises(type(failure)):
                        operation()
                else:
                    operation()
                text = output.getvalue()
                self.assertIn("Downloading assets", text)
                self.assertIn("s\n", text)
                self.assertIn(
                    "!"
                    if isinstance(failure, KeyboardInterrupt)
                    else "✗"
                    if failure
                    else "✓",
                    text,
                )
                self.assertNotIn("\033", text)
                self.assertEqual("\r" in text, tty and term != "dumb")
                self.assertEqual(machine.getvalue(), '{"ready": true}\n')
                self.assertFalse(
                    any(t.name == "cli-progress" for t in threading.enumerate())
                )

    def test_captured_command_failure_keeps_diagnostics_and_exit_status(self):
        output = TerminalOutput()
        result = SimpleNamespace(
            returncode=7, stdout="partial output\n", stderr="network unavailable\n"
        )
        with (
            contextlib.redirect_stderr(output),
            patch.object(devstack.subprocess, "run", return_value=result),
        ):
            with self.assertRaisesRegex(SystemExit, r"command failed \(7\)"):
                devstack.run(
                    ["python", "installer.py"],
                    message="Preparing assets",
                    capture_output=True,
                    text=True,
                )
        self.assertIn("partial output", output.getvalue())
        self.assertIn("network unavailable", output.getvalue())
        self.assertIn("✗ Preparing assets", output.getvalue())


if __name__ == "__main__":
    unittest.main()
