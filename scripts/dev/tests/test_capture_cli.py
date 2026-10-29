"""Capture CLI ordering and job boundaries, without game or encoder processes."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

DEV = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEV))
sys.path.insert(0, str(DEV / "mcp"))
import devstack  # noqa: E402
import game  # noqa: E402
import media  # noqa: E402


class CaptureCli(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bf capture cli ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "stack"
        self.home.mkdir()
        self.binary = self.root / "encoder fixture.exe"
        self.binary.write_bytes(b"encoder fixture")
        self.stages = []
        self.out, self.err = io.StringIO(), io.StringIO()
        self.partial = False
        for module in dict.fromkeys((devstack, game.devstack)):
            for name, value in (
                ("HOME", self.home),
                ("process_owner", Mock(return_value="capture-unit-session")),
            ):
                setting = patch.object(module, name, value)
                setting.start()
                self.addCleanup(setting.stop)
        environment = patch.dict(os.environ, {"BF_GAME_JOB": "", "BF_HARNESS_JOB": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.tool = Mock(side_effect=self.capture_result)
        original = game.TOOLS["bf_capture"]
        registry = patch.dict(game.TOOLS, {"bf_capture": (self.tool, *original[1:])})
        registry.start()
        self.addCleanup(registry.stop)
        for name, value in (
            ("ffmpeg", Mock(side_effect=self.resolve_encoder)),
            ("encoder_info", Mock(side_effect=self.probe_encoder)),
            ("encode_capture", Mock(side_effect=self.encode)),
        ):
            setting = patch.object(media, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def arguments(self, **values):
        return SimpleNamespace(
            **{
                "client": 1,
                "label": "reload",
                "seconds": 1,
                "fps": 5,
                "actions": None,
                "video": False,
                **values,
            }
        )

    def invoke(self, arguments=None):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            devstack.capture(arguments or self.arguments())
        return json.loads(self.out.getvalue())

    def select(self, name="existing", owner="capture-unit-session", finished=False):
        directory = self.root / name
        directory.mkdir()
        (directory / "job.json").write_text(
            json.dumps({"owner": owner, "rcon": ["time query daytime"]}),
            encoding="utf-8",
        )
        if finished:
            (directory / "finished").touch()
        return directory

    def action_file(self, value):
        path = self.root / "scheduled actions.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def resolve_encoder(self):
        self.stages.append("resolve")
        return self.binary

    def probe_encoder(self, binary):
        self.assertEqual(binary, self.binary)
        self.stages.append("probe")
        return {"version": "fixture", "sha256": "f" * 64}

    def capture_result(self, arguments):
        directory, _config = game.job()
        self.stages.append("capture")
        folder = directory / "evidence" / "capture-fixture"
        folder.mkdir(parents=True)
        manifest = folder / "capture.json"
        manifest.write_text(
            json.dumps(
                {
                    "requested_seconds": arguments["seconds"],
                    "complete": not self.partial,
                }
            ),
            encoding="utf-8",
        )
        metadata = {
            "manifest": str(manifest),
            "folder": str(folder),
            "frames": 0 if self.partial else 5,
            "duration_seconds": 1,
            "complete": not self.partial,
        }
        if self.partial:
            metadata["error"] = "client deadline; partial evidence saved"
        result = {"content": [{"type": "text", "text": json.dumps(metadata)}]}
        if self.partial:
            result["isError"] = True
        return result

    def encode(self, manifest, binary):
        self.assertEqual(binary, self.binary)
        self.stages.append("encode")
        output = Path(manifest).with_name("capture.mp4")
        output.write_bytes(b"fixture encoded video")
        return output

    def assert_no_work(self):
        media.ffmpeg.assert_not_called()
        media.encoder_info.assert_not_called()
        media.encode_capture.assert_not_called()
        self.tool.assert_not_called()
        self.assertFalse((self.home / "jobs").exists())
        self.assertFalse((self.home / "current-job").exists())

    def test_invalid_capture_arguments_fail_before_encoder_or_job_creation(self):
        for values in (
            {"seconds": 0},
            {"seconds": 21},
            {"seconds": float("nan")},
            {"fps": 0},
            {"fps": 11},
            {"fps": 2.5},
            {"label": "../outside"},
            {"client": 3},
        ):
            with (
                self.subTest(values=values),
                self.assertRaises((ValueError, RuntimeError, SystemExit)),
            ):
                self.invoke(self.arguments(video=True, **values))
            self.assert_no_work()

    def test_all_scheduled_tool_arguments_are_validated_before_any_encoder_or_job(self):
        valid = {"at": 0, "tool": "bf_slot", "arguments": {"client": 1, "slot": 2}}
        invalid = [
            {"at": 0.5, "tool": "bf_tacz", "arguments": {}},
            {"at": 0.5, "tool": "bf_slot", "arguments": {"client": 1, "slot": 9}},
            {"at": 0.5, "tool": "bf_slot", "arguments": {"client": 2, "slot": 2}},
            {"at": 1, "tool": "bf_slot", "arguments": {"client": 1, "slot": 2}},
            {
                "at": 0.5,
                "tool": "bf_raw_input",
                "arguments": {"client": 1, "op": "key"},
            },
            {"at": 0.5, "tool": "bf_server_query", "arguments": {"command": "stop"}},
            {
                "at": 0.5,
                "tool": "bf_chat",
                "arguments": {"client": 1, "text": "/op Owner"},
            },
        ]
        for action in invalid:
            with (
                self.subTest(action=action),
                self.assertRaises((ValueError, RuntimeError, SystemExit)),
            ):
                self.invoke(
                    self.arguments(
                        actions=self.action_file([valid, action]), video=True
                    )
                )
            self.assert_no_work()

    def test_invalid_actions_files_fail_without_encoder_or_game_access(self):
        for value in ({"actions": []}, "not an array", ["not an action"]):
            with (
                self.subTest(value=value),
                self.assertRaises((ValueError, RuntimeError, SystemExit)),
            ):
                self.invoke(self.arguments(actions=self.action_file(value), video=True))
            self.assert_no_work()
        path = self.root / "scheduled actions.json"
        for data in ("{malformed", " " * 65537):
            path.write_text(data, encoding="utf-8")
            with (
                self.subTest(data_length=len(data)),
                self.assertRaises((ValueError, RuntimeError, SystemExit)),
            ):
                self.invoke(self.arguments(actions=str(path), video=True))
            self.assert_no_work()

    def test_encoder_preflight_failure_never_creates_a_job_or_sends_actions(self):
        actions = self.action_file(
            [{"at": 0, "tool": "bf_slot", "arguments": {"client": 1, "slot": 2}}]
        )
        media.encoder_info.side_effect = RuntimeError("libx264 unavailable")
        with self.assertRaisesRegex(RuntimeError, "libx264"):
            self.invoke(self.arguments(actions=actions, video=True))
        self.assertEqual(self.stages, ["resolve"])
        media.encoder_info.assert_called_once_with(self.binary)
        media.encode_capture.assert_not_called()
        self.tool.assert_not_called()
        self.assertFalse((self.home / "jobs").exists())
        self.assertFalse((self.home / "current-job").exists())

    def test_automatic_job_is_created_after_codec_preflight_and_stdout_is_single_json(
        self,
    ):
        original = devstack.game_job

        def create_after_preflight(args):
            self.assertEqual(self.stages, ["resolve", "probe"])
            return original(args)

        with patch.object(
            devstack, "game_job", side_effect=create_after_preflight
        ) as create:
            metadata = self.invoke(self.arguments(video=True))
        create.assert_called_once()
        self.assertEqual(self.stages, ["resolve", "probe", "capture", "encode"])
        directory = Path(
            (self.home / "current-job").read_text(encoding="utf-8").strip()
        )
        self.assertEqual(
            json.loads((directory / "job.json").read_text())["owner"],
            "capture-unit-session",
        )
        self.assertTrue(metadata["complete"])
        self.assertEqual(metadata["frames"], 5)
        self.assertEqual(Path(metadata["folder"]).parent, directory / "evidence")
        self.assertEqual(Path(metadata["video"]).read_bytes(), b"fixture encoded video")
        self.assertTrue(Path(metadata["manifest"]).is_file())
        self.assertIn("game job", self.err.getvalue())

    def test_existing_environment_or_pointer_jobs_keep_their_configuration_and_selection(
        self,
    ):
        for selection in ("pointer", "BF_GAME_JOB", "BF_HARNESS_JOB"):
            with self.subTest(selection=selection):
                self.out = io.StringIO()
                directory = self.select(selection)
                pointer = self.home / "current-job"
                pointer.write_text(str(directory) + "\n", encoding="utf-8")
                if selection != "pointer":
                    os.environ[selection] = str(directory)
                before = (directory / "job.json").read_bytes(), pointer.read_bytes()
                actions = self.action_file(
                    [
                        {
                            "at": 0,
                            "tool": "bf_server_query",
                            "arguments": {"command": "time query daytime"},
                        }
                    ]
                )
                with patch.object(devstack, "game_job") as create:
                    metadata = self.invoke(self.arguments(actions=actions))
                create.assert_not_called()
                self.assertEqual((directory / "job.json").read_bytes(), before[0])
                self.assertEqual(pointer.read_bytes(), before[1])
                self.assertEqual(
                    Path(metadata["folder"]).parent, directory / "evidence"
                )
                self.assertEqual(
                    self.tool.call_args.args[0]["actions"][0]["arguments"]["command"],
                    "time query daytime",
                )
                os.environ["BF_GAME_JOB"] = os.environ["BF_HARNESS_JOB"] = ""
        media.ffmpeg.assert_not_called()

    def test_explicit_job_takes_precedence_over_a_foreign_pointer_without_overwriting_it(
        self,
    ):
        foreign = self.select("foreign", owner="other-session")
        selected = self.select("selected")
        pointer = self.home / "current-job"
        pointer.write_text(str(foreign) + "\n", encoding="utf-8")
        os.environ["BF_GAME_JOB"] = str(selected)
        with patch.object(devstack, "game_job") as create:
            metadata = self.invoke()
        create.assert_not_called()
        self.assertEqual(Path(metadata["folder"]).parent, selected / "evidence")
        self.assertEqual(pointer.read_text().strip(), str(foreign))
        self.assertEqual(
            json.loads((foreign / "job.json").read_text())["owner"], "other-session"
        )

    def test_foreign_or_finished_jobs_fail_before_encoder_setup_and_are_never_replaced(
        self,
    ):
        for name, owner, finished in (
            ("foreign", "other-session", False),
            ("finished", "capture-unit-session", True),
        ):
            with self.subTest(name=name):
                directory = self.select(name, owner, finished)
                pointer = self.home / "current-job"
                pointer.write_text(str(directory) + "\n", encoding="utf-8")
                original = pointer.read_bytes(), (directory / "job.json").read_bytes()
                with (
                    patch.object(devstack, "game_job") as create,
                    self.assertRaises((RuntimeError, SystemExit)),
                ):
                    self.invoke(self.arguments(video=True))
                create.assert_not_called()
                self.tool.assert_not_called()
                media.ffmpeg.assert_not_called()
                self.assertEqual(pointer.read_bytes(), original[0])
                self.assertEqual((directory / "job.json").read_bytes(), original[1])

    def test_partial_capture_reports_failure_and_never_attempts_video_encoding(self):
        directory = self.select()
        os.environ["BF_GAME_JOB"] = str(directory)
        self.partial = True
        with self.assertRaises(SystemExit) as failure:
            self.invoke(self.arguments(video=True))
        self.assertEqual(self.stages, ["resolve", "probe", "capture"])
        media.encode_capture.assert_not_called()
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("client deadline", str(failure.exception))
        self.assertFalse(
            (directory / "evidence" / "capture-fixture" / "capture.mp4").exists()
        )

    def test_native_parser_forwards_literal_action_file_paths_and_numeric_options(self):
        directory = self.select()
        os.environ["BF_GAME_JOB"] = str(directory)
        action_path = self.action_file([])
        with (
            patch.object(
                sys,
                "argv",
                [
                    "devstack.py",
                    "capture",
                    "--seconds",
                    "1.5",
                    "--fps",
                    "7",
                    "--actions",
                    action_path,
                ],
            ),
            contextlib.redirect_stdout(self.out),
            contextlib.redirect_stderr(self.err),
            self.assertRaises(SystemExit) as exited,
        ):
            devstack.main()
        self.assertIn(exited.exception.code, (None, 0))
        self.assertEqual(self.tool.call_args.args[0]["seconds"], 1.5)
        self.assertEqual(self.tool.call_args.args[0]["fps"], 7)
        self.assertEqual(self.tool.call_args.args[0]["actions"], [])
        self.assertEqual(json.loads(self.out.getvalue())["frames"], 5)


if __name__ == "__main__":
    unittest.main()
