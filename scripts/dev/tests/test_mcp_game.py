"""Job-scoped game MCP behavior without a game, Docker or desktop access."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import server  # noqa: E402

game = server.game
NAMES = [
    "bf_state",
    "bf_messages",
    "bf_look",
    "bf_look_at",
    "bf_hold",
    "bf_press",
    "bf_slot",
    "bf_use_attack",
    "bf_weapon",
    "bf_menu_action",
    "bf_chat",
    "bf_screen",
    "bf_raw_input",
    "bf_screenshot",
    "bf_wait",
    "bf_server_query",
    "bf_server_log",
    "bf_objective",
    "bf_goto_point",
    "bf_report_bug",
    "bf_finish",
    "bf_sequence",
    "bf_render_probe",
    "bf_wait_state",
    "bf_capture",
]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
)


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class GameJob(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bf game job ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.job = self.root / "job"
        self.home = self.root / "stack"
        self.maps = self.home / "server" / "blockfield" / "maps"
        self.job.mkdir()
        self.maps.mkdir(parents=True)
        for target, value in (
            ("HOME", self.home),
            ("SERVER", self.home / "server"),
            ("LOG", self.home / "server.out"),
        ):
            p = patch.object(game.devstack, target, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(game.devstack, "process_owner", return_value="unit-session")
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"BF_GAME_JOB": str(self.job)})
        p.start()
        self.addCleanup(p.stop)

    def config(self, value):
        (self.job / "job.json").write_text(json.dumps(value), encoding="utf-8")

    def call(self, name, args=None):
        reply = server.handle(
            {
                "method": "tools/call",
                "id": 1,
                "params": {"name": name, "arguments": args or {}},
            },
            game.TOOLS,
        )
        return reply["result"]

    def calls(self):
        return [
            json.loads(line)
            for line in (self.job / "calls.jsonl").read_text().splitlines()
        ]

    def test_game_profile_keeps_all_harness_tools_and_hides_process_tools(self):
        self.assertEqual(list(game.TOOLS), NAMES)
        reply = server.handle({"method": "tools/list", "id": 1}, game.TOOLS)
        self.assertEqual([entry["name"] for entry in reply["result"]["tools"]], NAMES)
        reply = server.handle(
            {"method": "tools/call", "id": 2, "params": {"name": "server_stop"}},
            game.TOOLS,
        )
        self.assertEqual(reply["error"]["code"], -32602)

    def test_fixed_commands_and_decoded_results(self):
        cases = [
            ("bf_state", {}, "state"),
            ("bf_messages", {"count": 3}, "messages 3"),
            ("bf_look", {"yaw": -45, "pitch": 5}, "look -45 5"),
            ("bf_look_at", {"x": 1, "y": 2, "z": 3}, "lookat 1 2 3"),
            ("bf_hold", {"binding": "key.forward", "ticks": 3}, "hold key.forward 3"),
            ("bf_press", {"key": "key.keyboard.e"}, "press key.keyboard.e 2"),
            ("bf_slot", {"slot": 8}, "slot 8"),
            ("bf_use_attack", {"action": "attack"}, "attack"),
            ("bf_weapon", {"op": "aim", "aim": True}, "tacz aim true"),
            ("bf_weapon", {"op": "inspect"}, "tacz inspect"),
            (
                "bf_menu_action",
                {"action": "class:assault:0"},
                "bfaction class:assault:0",
            ),
            (
                "bf_menu_action",
                {"action": "join:00000000-0000-4000-8000-000000000001"},
                "bfaction join:00000000-0000-4000-8000-000000000001",
            ),
            ("bf_chat", {"text": "/ready on"}, "chat /ready on"),
            ("bf_screen", {"op": "escape"}, "rawinput key Escape"),
            ("bf_screen", {"op": "close"}, "closescreen"),
            ("bf_screen", {"op": "respawn"}, "respawn"),
            (
                "bf_sequence",
                {"actions": [{"tick": 0, "slot": 2}, {"tick": 4, "slot": 0}]},
                "sequence 0:2 4:0",
            ),
            ("bf_render_probe", {"op": "start"}, "renderprobe start"),
        ]
        with patch.object(game, "bot", return_value={"ok": True}) as bot:
            for name, args, command in cases:
                with self.subTest(name=name, command=command):
                    result = self.call(name, {"client": 1, **args})
                    self.assertNotIn("isError", result)
                    self.assertEqual(
                        json.loads(result["content"][0]["text"]), {"ok": True}
                    )
                    self.assertEqual(bot.call_args.args, ({}, 1, command))
        self.assertEqual(len(self.calls()), len(cases))
        self.assertTrue(all(call["ok"] for call in self.calls()))

    def test_raw_input_uses_testbot_on_linux_and_windows_without_desktop_helpers(self):
        cases = [
            ({"op": "key", "key": "Escape"}, "rawinput key Escape"),
            ({"op": "combo", "key": "F3+T"}, "rawinput combo F3+T"),
            ({"op": "mouse_down", "button": 3}, "rawinput mouse_down 3"),
            ({"op": "mouse_up", "button": 3}, "rawinput mouse_up 3"),
            ({"op": "click"}, "rawinput click 1"),
            ({"op": "move_to", "x": 1279, "y": 719}, "rawinput move_to 1279 719"),
        ]
        with patch.object(game, "bot", return_value={"ok": True}) as bot:
            for platform in ("linux", "win32"):
                with patch.object(game.sys, "platform", platform):
                    for args, command in cases:
                        with self.subTest(platform=platform, op=args["op"]):
                            result = self.call("bf_raw_input", {"client": 1, **args})
                            self.assertNotIn("isError", result)
                            self.assertEqual(bot.call_args.args[2], command)

    def test_invalid_arguments_fail_before_client_access_and_record_failures(self):
        cases = [
            ("bf_state", {"client": True}),
            ("bf_state", {"client": 3}),
            ("bf_slot", {"client": 1, "slot": 1.5}),
            ("bf_slot", {"client": 1, "slot": 9}),
            ("bf_slot", {"client": 1}),
            ("bf_slot", {"client": 1, "slot": 0, "command": "stop"}),
            ("bf_look", {"client": 1, "yaw": float("nan"), "pitch": 0}),
            ("bf_hold", {"client": 1, "binding": "key.forward", "ticks": 201}),
            ("bf_press", {"client": 1, "key": "key.keyboard.e\nstop"}),
            ("bf_weapon", {"client": 1, "op": "stop"}),
            ("bf_raw_input", {"client": 1, "op": "key"}),
            ("bf_raw_input", {"client": 1, "op": "key", "key": "Escape;stop"}),
            ("bf_raw_input", {"client": 1, "op": "combo"}),
            ("bf_raw_input", {"client": 1, "op": "combo", "key": "F3+T stop"}),
            ("bf_raw_input", {"client": 1, "op": "move_to", "x": 2}),
            ("bf_raw_input", {"client": 1, "op": "move_to", "x": -1, "y": 2}),
            ("bf_raw_input", {"client": 1, "op": "click", "button": 4}),
            ("bf_sequence", {"client": 1, "actions": []}),
            ("bf_sequence", {"client": 1, "actions": [{"tick": 201, "slot": 0}]}),
            ("bf_wait_state", {"client": 1, "expected": {}}),
            ("bf_wait_state", {"client": 1, "expected": {"connected": 1}}),
            (
                "bf_wait_state",
                {"client": 1, "expected": {"screen": None}, "timeout": 61},
            ),
            ("bf_capture", {"client": 1, "seconds": 21}),
            ("bf_capture", {"client": 1, "seconds": 1, "fps": 11}),
            ("bf_capture", {"client": 1, "seconds": 1, "label": "../outside"}),
            (
                "bf_finish",
                {
                    "summary": "x",
                    "checks": [{"check": "x", "verdict": "MAYBE", "evidence": "x"}],
                },
            ),
        ]
        with patch.object(game, "bot") as bot:
            for name, args in cases:
                with self.subTest(name=name, args=args):
                    self.assertTrue(self.call(name, args)["isError"])
            bot.assert_not_called()
        self.assertEqual(len(self.calls()), len(cases))
        self.assertFalse(any(call["ok"] for call in self.calls()))

    def test_job_configuration_fails_closed_and_needs_operator_selection(self):
        with patch.object(game, "bot", return_value={}) as bot:
            for config in (
                ["not an object"],
                {"owner": "another session"},
                {"rcon": "stop"},
                {"rcon": ["["]},
                {"allow_teleport": "true"},
            ):
                self.config(config)
                self.assertTrue(self.call("bf_state", {"client": 1})["isError"])
            (self.job / "job.json").write_text("{", encoding="utf-8")
            self.assertTrue(self.call("bf_state", {"client": 1})["isError"])
            with patch.dict(os.environ, {"BF_GAME_JOB": "", "BF_HARNESS_JOB": ""}):
                self.assertTrue(self.call("bf_state", {"client": 1})["isError"])
                (self.home / "current-job").write_text(str(self.job), encoding="utf-8")
                (self.job / "job.json").unlink()
                self.assertNotIn("isError", self.call("bf_state", {"client": 1}))
            self.assertEqual(bot.call_count, 1)

    def test_query_allowlists_do_not_accept_arbitrary_or_multiline_commands(self):
        self.config({"rcon": [r"time query daytime"], "chat_commands": [r"/example"]})
        with (
            patch.object(game, "rcon", return_value="12") as rcon,
            patch.object(game, "bot", return_value="ok") as bot,
        ):
            for command in ("list", "bf version", "time query daytime"):
                self.assertNotIn(
                    "isError", self.call("bf_server_query", {"command": command})
                )
            for command in ("stop", "op Owner", "list\nstop", "\nlist", "list\r"):
                self.assertTrue(
                    self.call("bf_server_query", {"command": command})["isError"]
                )
            self.assertEqual(rcon.call_count, 3)
            for text in ("/example", "Привет!"):
                self.assertNotIn(
                    "isError", self.call("bf_chat", {"client": 1, "text": text})
                )
            for text in (
                "/op Owner",
                "hello\rstop",
                "hello\nstop",
                "§khello",
                "\n/example",
            ):
                self.assertTrue(
                    self.call("bf_chat", {"client": 1, "text": text})["isError"]
                )
            self.assertEqual(bot.call_count, 2)

    def test_long_equipment_commands_still_need_a_single_line_allowlist(self):
        command = "give AgentPlayer blockfield:grenade" + "x" * 300
        self.config({"rcon": [r"give AgentPlayer blockfield:grenadex{300}"]})
        with patch.object(game, "rcon", return_value="given") as rcon:
            self.assertNotIn(
                "isError", self.call("bf_server_query", {"command": command})
            )
            for denied in (command + "\nstop", command + "x", "list" + " " * 4093):
                self.assertTrue(
                    self.call("bf_server_query", {"command": denied})["isError"]
                )
            rcon.assert_called_once_with(command)
        self.config({"rcon": [r"give AgentPlayer test:примеря{2048}"]})
        with patch.object(game, "rcon") as rcon:
            self.assertTrue(
                self.call(
                    "bf_server_query",
                    {"command": "give AgentPlayer test:пример" + "я" * 2048},
                )["isError"]
            )
            rcon.assert_not_called()

    def test_call_durations_include_failures_and_structured_error_results(self):
        clock = Clock()

        def run(*_):
            clock.sleep(0.25)
            raise RuntimeError("client timed out")

        with (
            patch.object(game.time, "monotonic", side_effect=clock.monotonic),
            patch.object(game, "bot", side_effect=run),
        ):
            self.assertTrue(self.call("bf_state", {"client": 1})["isError"])
            self.assertTrue(
                self.call(
                    "bf_wait_state", {"client": 1, "expected": {"connected": True}}
                )["isError"]
            )
        self.assertEqual([entry["duration_ms"] for entry in self.calls()], [250, 250])
        self.assertFalse(any(entry["ok"] for entry in self.calls()))

    def test_wait_state_stops_at_the_deadline_and_keeps_the_last_sample(self):
        clock = Clock()
        states = [{"connected": True}, {"connected": True, "screen": None}]
        with (
            patch.object(game.time, "monotonic", side_effect=clock.monotonic),
            patch.object(game.time, "sleep", side_effect=clock.sleep),
            patch.object(game, "bot", side_effect=states) as bot,
        ):
            result = self.call(
                "bf_wait_state", {"client": 1, "expected": {"screen": None}}
            )
        sample = json.loads(result["content"][0]["text"])
        self.assertEqual((sample["matched"], sample["attempts"]), (True, 2))
        self.assertEqual(bot.call_count, 2)
        self.assertEqual(sample["elapsed_seconds"], 0.2)
        self.assertIsNone(game.DEADLINE.get())

        clock = Clock()
        with (
            patch.object(game.time, "monotonic", side_effect=clock.monotonic),
            patch.object(game.time, "sleep", side_effect=clock.sleep),
            patch.object(game, "bot", return_value={"connected": False}),
        ):
            result = self.call(
                "bf_wait_state",
                {"client": 1, "expected": {"connected": True}, "timeout": 0.5},
            )
        sample = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertEqual(sample["state"], {"connected": False})
        self.assertEqual((sample["attempts"], sample["elapsed_seconds"]), (3, 0.5))
        self.assertFalse(self.calls()[-1]["ok"])

    def test_wait_state_accepts_stable_screen_kind_with_intermediary_class_names(self):
        state = {"screen": "class_442", "screenKind": "TitleScreen", "connected": False}
        with patch.object(game, "bot", return_value=state) as bot:
            result = self.call(
                "bf_wait_state",
                {
                    "client": 1,
                    "expected": {"screenKind": "TitleScreen", "connected": False},
                },
            )
        self.assertNotIn("isError", result)
        sample = json.loads(result["content"][0]["text"])
        self.assertTrue(sample["matched"])
        self.assertEqual(sample["state"], state)
        bot.assert_called_once()
        with patch.object(game, "bot") as bot:
            for kind in (1, "x" * 101):
                with self.subTest(kind=kind):
                    self.assertTrue(
                        self.call(
                            "bf_wait_state",
                            {"client": 1, "expected": {"screenKind": kind}},
                        )["isError"]
                    )
            bot.assert_not_called()

    def test_wait_state_requires_a_boolean_loading_overlay_state(self):
        clock = Clock()
        with (
            patch.object(game.time, "monotonic", side_effect=clock.monotonic),
            patch.object(game.time, "sleep", side_effect=clock.sleep),
            patch.object(
                game,
                "bot",
                side_effect=[{"loadingOverlay": True}, {"loadingOverlay": False}],
            ) as bot,
        ):
            result = self.call(
                "bf_wait_state", {"client": 1, "expected": {"loadingOverlay": False}}
            )
        self.assertNotIn("isError", result)
        sample = json.loads(result["content"][0]["text"])
        self.assertEqual((sample["matched"], sample["attempts"]), (True, 2))
        self.assertEqual(bot.call_count, 2)
        with patch.object(game, "bot") as bot:
            self.assertTrue(
                self.call(
                    "bf_wait_state", {"client": 1, "expected": {"loadingOverlay": 0}}
                )["isError"]
            )
            bot.assert_not_called()

    def test_render_probe_saves_full_events_without_the_response_text_limit(self):
        events = [{"event": "gunDraw", "detail": "x" * 9000}]
        with patch.object(game, "bot", return_value=events) as bot:
            result = self.call("bf_render_probe", {"client": 1, "op": "get"})
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["events"], 1)
        self.assertEqual(json.loads(Path(payload["evidence"]).read_text()), events)
        bot.assert_called_once_with({}, 1, "renderprobe get")

    def capture(self, arguments=None, command_hook=None, process_record=None):
        play = self.home / "play"
        (play / "screenshots").mkdir(parents=True, exist_ok=True)
        clock, commands = Clock(), []

        def send(_config, _client, command):
            commands.append((clock.monotonic(), command))
            if command_hook:
                command_hook(command)
            if command.startswith("screenshot "):
                (play / "screenshots" / command.split(" ", 1)[1]).write_bytes(PNG)
            return {"ok": True}

        with (
            patch.object(game.time, "monotonic", side_effect=clock.monotonic),
            patch.object(game.time, "sleep", side_effect=clock.sleep),
            patch.object(game, "bot", side_effect=send),
            patch.object(
                game,
                "require_process",
                return_value=process_record
                or {"pid": 123, "cwd": str(play), "owner": "unit-session"},
            ),
        ):
            result = self.call(
                "bf_capture", {"client": 1, "seconds": 1, **(arguments or {})}
            )
        payload = json.loads(result["content"][0]["text"])
        manifest = json.loads(Path(payload["manifest"]).read_text())
        return result, payload, manifest, commands

    def test_capture_executes_validated_tools_once_and_preserves_actual_frame_times(
        self,
    ):
        actions = [
            {"at": 0.4, "tool": "bf_slot", "arguments": {"client": 1, "slot": 2}},
            {
                "at": 0.1,
                "tool": "bf_weapon",
                "arguments": {"client": 1, "op": "reload"},
            },
        ]
        result, payload, manifest, commands = self.capture({"actions": actions})
        self.assertNotIn("isError", result)
        self.assertTrue(manifest["complete"])
        self.assertEqual(payload["frames"], 5)
        self.assertEqual(
            [
                (time, command)
                for time, command in commands
                if not command.startswith("screenshot")
            ],
            [(0.1, "tacz reload"), (0.4, "slot 2")],
        )
        for frame, expected in zip(
            manifest["frames"], (0, 0.2, 0.4, 0.6, 0.8), strict=True
        ):
            self.assertAlmostEqual(frame["at_seconds"], expected)
            self.assertEqual(frame["completed_seconds"], frame["at_seconds"])
        self.assertEqual(manifest["duration_seconds"], 1)
        self.assertEqual(manifest["process"]["pid"], 123)
        self.assertTrue(all(frame["saved"] for frame in manifest["frames"]))
        for frame in manifest["frames"]:
            self.assertEqual(
                (Path(payload["folder"]) / frame["file"]).read_bytes(), PNG
            )
        self.assertEqual(list((self.home / "play" / "screenshots").iterdir()), [])
        self.assertTrue(all(entry["ok"] for entry in self.calls()))
        self.assertEqual(self.calls()[-1]["duration_ms"], 1000)
        _, second, _, _ = self.capture()
        self.assertNotEqual(payload["folder"], second["folder"])

    def test_capture_keeps_request_timestamps_inside_the_duration_when_responses_finish_late(
        self,
    ):
        screenshots = 0

        def slow_reply(command):
            nonlocal screenshots
            if command.startswith("screenshot "):
                screenshots += 1
                game.time.sleep(0.7999998 if screenshots == 1 else 0.2)
                game.remaining_timeout(30)

        result, payload, manifest, commands = self.capture(command_hook=slow_reply)
        self.assertNotIn("isError", result)
        self.assertEqual(payload["frames"], 2)
        self.assertTrue(manifest["complete"])
        last = manifest["frames"][-1]
        self.assertLess(last["at_seconds"], manifest["requested_seconds"])
        self.assertGreater(last["completed_seconds"], manifest["requested_seconds"])
        self.assertGreater(last["completed_seconds"], last["at_seconds"])
        self.assertTrue(all(at < 1 for at, _command in commands))
        self.assertTrue(all(frame["saved"] for frame in manifest["frames"]))
        self.assertIsNone(game.DEADLINE.get())

    def test_capture_has_a_hard_completion_deadline_for_an_in_flight_frame(self):
        def stalled_reply(command):
            if command.startswith("screenshot "):
                game.time.sleep(game.remaining_timeout(30))
                game.remaining_timeout(30)

        result, payload, manifest, commands = self.capture(command_hook=stalled_reply)
        self.assertTrue(result["isError"])
        self.assertFalse(manifest["complete"])
        self.assertIn("deadline", manifest["error"])
        self.assertEqual(manifest["duration_seconds"], 16)
        self.assertEqual(len(commands), 1)
        self.assertEqual(payload["frames"], 0)
        self.assertIsNone(game.DEADLINE.get())

    def test_capture_does_not_admit_more_actions_after_a_running_action_reaches_the_end(
        self,
    ):
        actions = [
            {"at": 0.8, "tool": "bf_slot", "arguments": {"client": 1, "slot": 2}},
            {"at": 0.9, "tool": "bf_slot", "arguments": {"client": 1, "slot": 3}},
        ]

        def slow_action(command):
            if command == "slot 2":
                game.time.sleep(0.3)

        result, _payload, manifest, commands = self.capture(
            {"actions": actions}, slow_action
        )
        self.assertTrue(result["isError"])
        self.assertEqual(len(manifest["actions"]), 1)
        self.assertTrue(manifest["actions"][0]["ok"])
        self.assertTrue(all(at < 1 for at, _command in commands))
        self.assertFalse(any(command == "slot 3" for _at, command in commands))
        self.assertIsNone(game.DEADLINE.get())

    def test_capture_bounds_completion_of_an_action_admitted_before_the_end(self):
        actions = [
            {"at": 0.8, "tool": "bf_slot", "arguments": {"client": 1, "slot": 2}}
        ]
        for delay, complete in ((0.3, True), (15.2, False)):
            with self.subTest(delay=delay):

                def slow_action(command):
                    if command == "slot 2":
                        game.time.sleep(delay)
                        game.remaining_timeout(30)

                result, _payload, manifest, commands = self.capture(
                    {"actions": actions}, slow_action
                )
                self.assertEqual(manifest["complete"], complete)
                self.assertEqual(bool(result.get("isError")), not complete)
                self.assertAlmostEqual(manifest["duration_seconds"], 0.8 + delay)
                self.assertTrue(all(at < 1 for at, _command in commands))
                self.assertEqual(len(manifest["actions"]), 1)
                if not complete:
                    self.assertIn("deadline", manifest["error"])
                self.assertIsNone(game.DEADLINE.get())

    def test_capture_validates_all_actions_before_any_client_or_server_access(self):
        invalid = [
            {"at": 0.5, "tool": "bf_finish", "arguments": {}},
            {"at": 0.5, "tool": "bf_tacz", "arguments": {"client": 1}},
            {
                "at": 0.5,
                "tool": "bf_raw_input",
                "arguments": {"client": 1, "op": "key"},
            },
            {
                "at": 0.5,
                "tool": "bf_chat",
                "arguments": {"client": 1, "text": "/op Owner"},
            },
            {"at": 0.5, "tool": "bf_server_query", "arguments": {"command": "stop"}},
            {
                "at": 0.5,
                "tool": "bf_server_query",
                "arguments": {"command": "list\nstop"},
            },
            {
                "at": 0.5,
                "tool": "bf_sequence",
                "arguments": {"client": 1, "actions": []},
            },
            {"at": 0.5, "tool": "bf_slot", "arguments": {"client": 2, "slot": 1}},
            {"at": 1, "tool": "bf_slot", "arguments": {"client": 1, "slot": 1}},
        ]
        with patch.object(game, "bot") as bot, patch.object(game, "rcon") as rcon:
            for action in invalid:
                with self.subTest(action=action):
                    self.assertTrue(
                        self.call(
                            "bf_capture",
                            {
                                "client": 1,
                                "seconds": 1,
                                "actions": [
                                    {
                                        "at": 0,
                                        "tool": "bf_slot",
                                        "arguments": {"client": 1, "slot": 2},
                                    },
                                    action,
                                ],
                            },
                        )["isError"]
                    )
            bot.assert_not_called()
            rcon.assert_not_called()
        self.assertFalse((self.job / "evidence").exists())

    def test_capture_keeps_completed_frames_and_a_partial_report_when_an_action_fails(
        self,
    ):
        def fail(command):
            if command == "slot 2":
                raise RuntimeError("client socket deadline")

        result, payload, manifest, _ = self.capture(
            {
                "actions": [
                    {
                        "at": 0.5,
                        "tool": "bf_slot",
                        "arguments": {"client": 1, "slot": 2},
                    }
                ]
            },
            fail,
        )
        self.assertTrue(result["isError"])
        self.assertFalse(manifest["complete"])
        self.assertEqual(payload["frames"], 3)
        self.assertIn("client socket deadline", manifest["error"])
        self.assertFalse(manifest["actions"][0]["ok"])
        self.assertTrue(all(frame["saved"] for frame in manifest["frames"]))
        self.assertFalse(self.calls()[-1]["ok"])
        self.assertIsNone(game.DEADLINE.get())

    def test_capture_stops_if_the_job_finishes_or_the_registered_process_changes(self):
        def finish(command):
            if command.startswith("screenshot "):
                (self.job / "finished").touch()

        result, payload, manifest, commands = self.capture(command_hook=finish)
        self.assertTrue(result["isError"])
        self.assertIn("finished", manifest["error"])
        self.assertEqual(len(commands), 1)
        self.assertEqual(payload["frames"], 1)
        (self.job / "finished").unlink()

        process = {"pid": 123, "cwd": str(self.home / "play"), "owner": "unit-session"}

        def change_process(command):
            if command.startswith("screenshot "):
                process["pid"] = 456

        result, payload, manifest, commands = self.capture(
            command_hook=change_process, process_record=process
        )
        self.assertTrue(result["isError"])
        self.assertIn("registered client changed", manifest["error"])
        self.assertEqual((len(commands), payload["frames"]), (1, 1))
        self.assertEqual(manifest["process"]["pid"], 123)

    def test_capture_reports_unfinished_pngs_and_keeps_already_saved_evidence(self):
        with patch.object(
            game,
            "screenshot_bytes",
            side_effect=[PNG, RuntimeError("PNG write deadline")],
        ):
            result, payload, manifest, _ = self.capture()
        self.assertTrue(result["isError"])
        self.assertEqual(payload["frames"], 1)
        self.assertEqual(
            [frame["saved"] for frame in manifest["frames"]],
            [True, False, False, False, False],
        )
        self.assertIn("PNG write deadline", manifest["error"])
        self.assertFalse(Path(payload["manifest"]).with_suffix(".json.tmp").exists())

    def test_capture_handles_windows_locked_source_cleanup_without_losing_evidence(
        self,
    ):
        with patch.object(
            Path, "unlink", side_effect=PermissionError("file is still open")
        ):
            result, payload, manifest, _ = self.capture()
        self.assertNotIn("isError", result)
        self.assertEqual(payload["frames"], 5)
        self.assertTrue(manifest["complete"])
        self.assertTrue(
            all("source_cleanup_error" in frame for frame in manifest["frames"])
        )

    @unittest.skipIf(
        os.name == "nt", "Windows symlinks may require developer privileges"
    )
    def test_capture_and_screenshot_reject_evidence_symlinks_outside_the_job(self):
        outside = self.root / "foreign evidence"
        outside.mkdir()
        (self.job / "evidence").symlink_to(outside, target_is_directory=True)
        with (
            patch.object(
                game, "require_process", return_value={"cwd": str(self.home / "play")}
            ),
            patch.object(game, "bot") as bot,
        ):
            self.assertTrue(
                self.call("bf_capture", {"client": 1, "seconds": 1})["isError"]
            )
            bot.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_reconnect_uses_owned_loopback_server_and_closes_existing_connection(self):
        with (
            patch.object(game, "require_process") as require,
            patch.object(game, "bot", return_value="ok") as bot,
        ):
            self.assertNotIn(
                "isError", self.call("bf_screen", {"client": 1, "op": "reconnect"})
            )
        require.assert_called_once_with(game.devstack.PID_FILE, game.devstack.PORT)
        self.assertEqual(
            [call.args[2] for call in bot.call_args_list],
            ["disconnect", f"connect 127.0.0.1:{game.devstack.PORT}"],
        )

    def test_reconnect_rejects_foreign_server_before_disconnecting_client(self):
        with (
            patch.object(
                game, "require_process", side_effect=RuntimeError("foreign server")
            ),
            patch.object(game, "bot") as bot,
        ):
            self.assertTrue(
                self.call("bf_screen", {"client": 1, "op": "reconnect"})["isError"]
            )
        bot.assert_not_called()

    def test_legacy_record_cannot_authorize_gameplay_without_native_identity(self):
        record = {"live": True, "pid": 1, "owner": "unit-session", "id": "legacy"}
        with (
            patch.object(game.devstack, "process_record", return_value=record),
            patch.object(game, "owns_connection", return_value=True) as connection,
        ):
            with self.assertRaisesRegex(RuntimeError, "registered to this session"):
                game.require_process(game.devstack.CLIENT_PID_FILE, 47777)
        connection.assert_not_called()

    def test_second_client_requires_a_separate_registered_stack(self):
        with self.assertRaisesRegex(RuntimeError, "client 2"):
            game.client_settings({}, 2)
        second = self.root / "second stack"
        second.mkdir()
        (second / "client.cmd").write_text(
            json.dumps({"game_dir": str(second / "play")}), encoding="utf-8"
        )
        self.assertEqual(
            game.client_settings({"clients": {"2": {"home": str(second)}}}, 2),
            (second / "client.pid", second / "play"),
        )

    def test_objective_and_permission_scoped_teleport_use_real_map_data(self):
        point = {"name": "A", "x": 10, "y": 63, "z": 0}
        data = {
            "mode_data": {
                "TDM": {
                    "bases": {"TeamA": {"x": 0, "y": 63, "z": 0}},
                    "points": [point],
                },
                "other": {"points": [point]},
            }
        }
        (self.maps / "fields.json").write_text(json.dumps(data), encoding="utf-8")
        game.devstack.LOG.write_text(
            "old\nMatchTracker: active\nКомната: fields\n", encoding="utf-8"
        )
        state = {
            "name": "AgentPlayer",
            "dimension": "blockfield:fields_1",
            "pos": [0.5, 64, 0.5],
            "health": 20,
            "screen": None,
        }
        with (
            patch.object(
                game,
                "bot",
                side_effect=lambda _c, _n, command: (
                    state if command == "state" else ["Готовы?"]
                ),
            ),
            patch.object(game, "rcon", return_value="teleported") as rcon,
        ):
            objective = json.loads(
                self.call("bf_objective", {"client": 1})["content"][0]["text"]
            )
            self.assertEqual([p["name"] for p in objective["points"]], ["BASE_A", "A"])
            self.assertEqual(
                (
                    objective["points"][1]["distance"],
                    objective["points"][1]["face_yaw"],
                ),
                (10, -90),
            )
            self.assertIn("/ready", objective["next_step"])
            self.assertEqual(len(objective["server_log"]), 2)
            self.assertTrue(
                self.call("bf_goto_point", {"client": 1, "point": "A"})["isError"]
            )
            rcon.assert_not_called()
            self.config({"allow_teleport": True})
            self.assertNotIn(
                "isError", self.call("bf_goto_point", {"client": 1, "point": "a"})
            )
            rcon.assert_called_once_with(
                "execute in blockfield:fields_1 run tp AgentPlayer 10.5 64 0.5"
            )
            state["name"] = "AgentPlayer run stop"
            self.assertTrue(
                self.call("bf_goto_point", {"client": 1, "point": "a"})["isError"]
            )
            self.assertEqual(rcon.call_count, 1)
        self.assertEqual(game.map_points("blockfield:../fields_1"), [])

    def test_screenshot_returns_mcp_image_and_preserves_full_resolution_evidence(self):
        play = self.home / "play"
        (play / "screenshots").mkdir(parents=True)

        def take(_config, _client, command):
            (play / "screenshots" / command.split(" ", 1)[1]).write_bytes(PNG)
            return "ok"

        with (
            patch.object(game, "bot", side_effect=take),
            patch.object(
                game, "require_process", return_value={"cwd": str(play)}
            ) as verify,
        ):
            result = self.call("bf_screenshot", {"client": 1, "label": "before"})
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"][0]["type"], "image")
        self.assertEqual(base64.b64decode(result["content"][0]["data"]), PNG)
        evidence = list((self.job / "evidence").glob("*.png"))
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].read_bytes(), PNG)
        self.assertIn(str(evidence[0]), result["content"][1]["text"])
        verify.assert_called_once()
        self.assertTrue(self.calls()[0]["ok"])
        with (
            patch.object(game, "require_process", return_value={"cwd": str(self.root)}),
            patch.object(game, "bot") as bot,
        ):
            self.assertTrue(self.call("bf_screenshot", {"client": 1})["isError"])
            bot.assert_not_called()

    def test_incomplete_screenshot_has_a_hard_deadline(self):
        image = self.root / "partial.png"
        image.write_bytes(PNG[:24])
        with (
            patch.object(game.time, "monotonic", side_effect=[0, 0, 16]),
            patch.object(game.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                game.screenshot_bytes(image)
        sleep.assert_called_once_with(0.1)

    def test_report_and_finish_preserve_evidence_and_end_job_once(self):
        self.assertNotIn(
            "isError",
            self.call(
                "bf_report_bug",
                {
                    "title": "Renderer",
                    "steps": "Switch A→B→A",
                    "expected": "One gun",
                    "actual": "Two guns",
                    "evidence": "before.png",
                },
            ),
        )
        bug = json.loads((self.job / "bugs.jsonl").read_text())
        self.assertEqual(bug["evidence"], "before.png")
        result = self.call(
            "bf_finish",
            {
                "summary": "Verified on own stack",
                "checks": [
                    {
                        "check": "Draw | switch\nrapidly",
                        "verdict": "FAIL",
                        "evidence": "before.png",
                    }
                ],
            },
        )
        self.assertNotIn("isError", result)
        report = (self.job / "report.md").read_text(encoding="utf-8")
        self.assertIn("| Draw / switch rapidly | FAIL | before.png |", report)
        self.assertTrue((self.job / "finished").is_file())
        self.assertFalse((self.job / "report.md.tmp").exists())
        self.assertTrue(
            self.call("bf_finish", {"summary": "overwrite", "checks": []})["isError"]
        )
        with patch.object(game, "bot") as bot:
            self.assertTrue(self.call("bf_state", {"client": 1})["isError"])
            bot.assert_not_called()
        self.assertEqual((self.job / "report.md").read_text(encoding="utf-8"), report)


class Ownership(unittest.TestCase):
    def test_foreign_unregistered_dead_processes_never_receive_bot_input(self):
        for record in (
            None,
            {"live": False},
            {"live": True, "pid": 1, "owner": "unit"},
            {"live": True, "pid": 1, "owner": "foreign", "id": "registered"},
        ):
            with (
                self.subTest(record=record),
                patch.object(game.devstack, "process_record", return_value=record),
                patch.object(game.devstack, "process_owner", return_value="unit"),
                patch.object(game, "Bot") as bot,
            ):
                with self.assertRaisesRegex(RuntimeError, "registered"):
                    game.bot({}, 1, "state")
                bot.assert_not_called()

    def test_bot_and_rcon_verify_connected_peer_and_close_on_failure(self):
        for is_rcon in (False, True):
            connection = Mock()
            connection.sock.getsockname.return_value = ("127.0.0.1", 55555)
            connection.s.getsockname.return_value = ("127.0.0.1", 55555)
            factory = (
                patch.object(game.devstack, "Rcon", return_value=connection)
                if is_rcon
                else patch.object(game, "Bot", return_value=connection)
            )
            with (
                self.subTest(rcon=is_rcon),
                factory,
                patch.object(
                    game,
                    "require_process",
                    side_effect=[{}, RuntimeError("foreign peer")],
                ) as verify,
            ):
                with self.assertRaisesRegex(RuntimeError, "foreign peer"):
                    game.rcon("list") if is_rcon else game.bot({}, 1, "state")
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(verify.call_args.args[2], 55555)
            connection.cmd.assert_not_called()
            connection.assert_not_called()
            connection.s.close.assert_called_once() if is_rcon else connection.close.assert_called_once()

    def test_peer_accept_race_waits_within_the_operation_deadline_before_sending(self):
        clock, connection = Clock(), Mock()
        connection.sock.getsockname.return_value = ("127.0.0.1", 55555)
        token = game.DEADLINE.set(0.25)
        try:
            with (
                patch.object(game.time, "monotonic", side_effect=clock.monotonic),
                patch.object(game.time, "sleep", side_effect=clock.sleep),
                patch.object(game, "Bot", return_value=connection) as factory,
                patch.object(
                    game,
                    "require_process",
                    side_effect=[
                        {},
                        game.ConnectionOwnershipError("not accepted yet"),
                        game.ConnectionOwnershipError("not accepted yet"),
                        {},
                    ],
                ),
            ):
                game.bot({}, 1, "state")
            factory.assert_called_once_with(port=47777, timeout=0.25)
            # Fresh budget after slow ownership verification (~0.15 s left),
            # passed per call — never a reset of the socket timeout.
            connection.sock.settimeout.assert_not_called()
            self.assertEqual(connection.call_args.args, ("state",))
            self.assertAlmostEqual(connection.call_args.kwargs["timeout"], 0.15)
            connection.close.assert_called_once()
        finally:
            game.DEADLINE.reset(token)

        clock, connection = Clock(), Mock()
        connection.sock.getsockname.return_value = ("127.0.0.1", 55555)
        token = game.DEADLINE.set(0.1)
        try:
            with (
                patch.object(game.time, "monotonic", side_effect=clock.monotonic),
                patch.object(game.time, "sleep", side_effect=clock.sleep),
                patch.object(game, "Bot", return_value=connection),
                patch.object(
                    game,
                    "require_process",
                    side_effect=[{}]
                    + [game.ConnectionOwnershipError("foreign peer")] * 3,
                ),
                self.assertRaisesRegex(RuntimeError, "foreign peer"),
            ):
                game.bot({}, 1, "state")
            self.assertEqual(clock.monotonic(), 0.1)
            connection.assert_not_called()
            connection.close.assert_called_once()
        finally:
            game.DEADLINE.reset(token)

    def test_windows_socket_ownership_uses_existing_native_helper(self):
        helper = SimpleNamespace(owns_listener=Mock(return_value=True))
        with (
            patch.object(game.sys, "platform", "win32"),
            patch.dict(sys.modules, {"windows_process": helper}),
        ):
            self.assertTrue(game.owns_connection({"pid": 321}, 47777, 55555))
        helper.owns_listener.assert_called_once_with(321, 47777, 55555)

    @unittest.skipIf(os.name == "nt", "Linux proc fixture uses symbolic socket links")
    def test_linux_socket_ownership_matches_pid_inode_listener_and_connected_peer(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            process = proc / "321"
            (process / "fd").mkdir(parents=True)
            (process / "net").mkdir()
            (process / "fd" / "3").symlink_to("socket:[100]")
            header = "header\n"
            listener = "0: 0100007F:BAA1 00000000:0000 0A 0 0 0 0 0 100\n"
            established = "1: 0100007F:BAA1 0100007F:D903 01 0 0 0 0 0 100\n"
            other = "2: 0100007F:BAA2 00000000:0000 0A 0 0 0 0 0 999\n"
            (process / "net" / "tcp").write_text(
                header + listener + established + other
            )
            (process / "net" / "tcp6").write_text(header)
            with (
                patch.object(game.sys, "platform", "linux"),
                patch.object(
                    game,
                    "Path",
                    side_effect=lambda value: proc if value == "/proc" else Path(value),
                ),
            ):
                self.assertTrue(game.owns_connection({"pid": 321}, 47777))
                self.assertTrue(game.owns_connection({"pid": 321}, 47777, 55555))
                self.assertFalse(game.owns_connection({"pid": 321}, 47778))
                self.assertFalse(game.owns_connection({"pid": 321}, 47777, 55556))


class Stdio(unittest.TestCase):
    def test_isolated_native_cli_game_profile_survives_malformed_requests(self):
        requests = [
            [],
            {"method": "initialize", "id": 1},
            {"method": "tools/list", "id": 2},
            {"method": "ping", "id": 3},
        ]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                str(Path(server.__file__).resolve()),
                "--game",
            ],
            input="\n".join(json.dumps(request) for request in requests),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32600)
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "blockfield-game")
        self.assertEqual(
            [entry["name"] for entry in replies[2]["result"]["tools"]], NAMES
        )
        self.assertEqual(replies[3]["id"], 3)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
