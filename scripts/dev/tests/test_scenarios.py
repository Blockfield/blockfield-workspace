"""Hermetic checks for the ported scenarios; no game, no sockets, no ~/.cache."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from scenarios import (  # noqa: E402
    SCENARIOS,
    check_consent,
    common,
    body_pose,
    parkour,
    hitreg,
    stress,
    dm,
    range as firing_range,
)


class FakeRcon:
    def __init__(self):
        self.commands = []

    def cmd(self, command):
        self.commands.append(command)
        if command == "list":
            return "players: hitbot"
        if "Health" in command:
            return "hitbot has the following entity data: 20.0f"
        if command == "tick query":
            return (
                "Target tick rate: 20,0 per second.\n"
                "Average time per tick: 12,3ms (Target: 50,0ms)\n"
                "Percentiles: P50: 10,1ms P95: 20,5ms P99: 30,0ms, sample: 100"
            )
        return "ok"


class BodyPoseClock(unittest.TestCase):
    def test_request_bracket_preserves_cached_capture_times(self):
        cached = {"sampleUnixMs": 8500, "sampleMonoNs": 500_000_000}
        with (
            patch.object(
                body_pose.time, "time_ns", side_effect=[10_000_000_000, 10_020_000_000]
            ),
            patch.object(
                body_pose.time,
                "monotonic_ns",
                side_effect=[2_000_000_000, 2_020_000_000],
            ),
        ):
            result = body_pose.bracket(lambda _: cached, "playergeometry state")
        self.assertEqual(result["receipt"], cached)
        self.assertEqual(result["request"]["recvUnixMs"] - cached["sampleUnixMs"], 1520)
        self.assertEqual(
            result["request"]["recvMonoNs"] - result["request"]["sendMonoNs"],
            20_000_000,
        )


class ParkourJudge(unittest.TestCase):
    def test_stalled_attempt_has_a_deadline(self):
        attempt = {"start": [0, 64, 0], "kind": "grab", "yaw": 0}
        with (
            patch.object(parkour, "settle", return_value=True),
            patch.object(parkour.time, "monotonic", side_effect=[0, 1, 16]),
            patch.object(parkour.time, "sleep"),
            self.assertRaises(TimeoutError),
        ):
            parkour.run_attempt(
                lambda _: {"done": False}, FakeRcon(), "audit", "Player1", attempt
            )

    def test_reached_near_and_no(self):
        start, target = [0, 64.0, 0], [1, 67.0, 0]
        self.assertEqual(
            parkour.judge(start, target, ["0 0 64.0", "1 0 67.0"]), "reached"
        )
        self.assertEqual(parkour.judge(start, target, ["0 0 64.0", "1 1 66.5"]), "near")
        self.assertEqual(parkour.judge(start, target, ["0 0 64.0", "1 0 64.0"]), "no")
        self.assertEqual(parkour.judge(start, [0, 64.0, 1], ["0 0 64.0"]), "no")

    def test_roof_and_reach_helpers(self):
        roof = {(1, 0): [67.0]}
        self.assertEqual(parkour.on_roof(["1 0 67.0"], roof), ["1 0 67.0"])
        self.assertEqual(parkour.on_roof(["1 0 64.0"], roof), [])
        self.assertTrue(parkour.start_reachable([5, 64.0, 6], {(5, 6): [64.0]}))
        self.assertFalse(parkour.start_reachable([5, 64.0, 6], {(5, 6): [70.0]}))

    def test_load_nodes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"reach": [[1, 2, 64.0]]}, handle)
            path = handle.name
        self.assertEqual(parkour.load_nodes(path, "reach"), {(1, 2): [64.0]})


class Hitreg(unittest.TestCase):
    def test_health_parses_and_setup_builds_platform(self):
        rcon = FakeRcon()
        self.assertEqual(hitreg.health(rcon), 20.0)
        hitreg.setup(rcon, "blockfield:test_1", "1", "Player1")
        joined = [c for c in rcon.commands if "fill" in c or "tp Player1" in c]
        self.assertTrue(
            joined, "setup must build the barrier platform and place the shooter"
        )


class RoomAndInventory(unittest.TestCase):
    def test_room_log_and_empty_inventory_match_server_responses(self):
        class World:
            bot2 = None

            def mark(self):
                pass

            def bot(self, _):
                pass

            def log_since(self):
                return "Комната 3 открыта (Каждый сам за себя)"

        self.assertEqual(dm.open_room(World()), "3")
        rcon = FakeRcon()
        rcon.cmd = lambda _: "Test failed"
        self.assertEqual(
            firing_range.items(rcon, "Player1", "tacz:modern_kinetic_gun"), 0
        )
        rcon.cmd = lambda _: "Test passed, count: 2"
        self.assertEqual(
            firing_range.items(rcon, "Player1", "tacz:modern_kinetic_gun"), 2
        )


class LobbyScenarios(unittest.TestCase):
    NATIVE = "blockfield_lobby:12345678-1234-4567-89ab-123456789abc"

    def test_lobby_dimension_accepts_only_fallback_or_canonical_native_id(self):
        for dim in ("minecraft:overworld", self.NATIVE):
            with self.subTest(dim=dim):
                self.assertTrue(common.is_lobby_dimension(dim))
        for dim in (
            None,
            "",
            "blockfield:overpass_1",
            "blockfield_shared:respawn",
            "blockfield_lobby:overpass",
            self.NATIVE + "/combat",
            self.NATIVE.upper(),
            self.NATIVE + " run kill @a",
        ):
            with self.subTest(dim=dim):
                self.assertFalse(common.is_lobby_dimension(dim))

    def consent(self, dimensions, occupied=False):
        class Bot:
            def __init__(self, name, dim, pos):
                self.state = {
                    "name": name,
                    "dimension": dim,
                    "health": 20,
                    "pos": pos[:],
                }
                self.closed = False

            def __call__(self, command):
                return (
                    {**self.state, "pos": self.state["pos"][:]}
                    if command == "state"
                    else {}
                )

            def close(self):
                self.closed = True

        bots = {
            47777: Bot("Rig", dimensions[0], [10.5, 90, 20.5]),
            47778: Bot("RigPeer", dimensions[1], [11.5, 90, 21.5]),
        }
        rcon = FakeRcon()
        prefix = f"execute in {dimensions[0]} run "

        def command(text):
            rcon.commands.append(text)
            self.assertTrue(text.startswith(prefix), text)
            local = text.removeprefix(prefix)
            if local.startswith("execute if block "):
                return "ok" if occupied else "CONSENT_AIR"
            if local.startswith("tp "):
                _, name, *pos = local.split()
                next(bot for bot in bots.values() if bot.state["name"] == name).state[
                    "pos"
                ] = list(map(float, pos))
            return "ok"

        rcon.cmd = command
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
        )
        with patch.object(check_consent.time, "sleep"), patch("builtins.print"):
            try:
                result = check_consent.run(
                    args, rcon_factory=lambda *_: rcon, bot_factory=bots.__getitem__
                )
            except AssertionError as error:
                result = error
        self.assertTrue(all(bot.closed for bot in bots.values()))
        return result, rcon.commands, bots

    def test_consent_scopes_setup_and_restoration_to_captured_lobby(self):
        for dim in (self.NATIVE, "minecraft:overworld"):
            with self.subTest(dim=dim):
                result, commands, bots = self.consent((dim, dim))
                self.assertEqual(result, 0)
                checks = [c.split(" run ", 1)[1] for c in commands if "if block" in c]
                self.assertEqual(
                    checks,
                    [
                        f"execute if block {x} 0 {z} minecraft:air run say CONSENT_AIR"
                        for x in range(-256, -243)
                        for z in range(624, 637)
                    ],
                )
                self.assertIn(
                    f"execute in {dim} run fill -256 0 624 -244 0 636 minecraft:stone replace minecraft:air",
                    commands,
                )
                self.assertEqual(
                    commands[-1],
                    f"execute in {dim} run fill -256 0 624 -244 0 636 minecraft:air replace minecraft:stone",
                )
                kills = [c for c in commands if "kill @e" in c]
                self.assertTrue(kills)
                self.assertTrue(all("bf_consent_regression_" in c for c in kills))
                self.assertNotIn("execute at Rig", "\n".join(commands))
                self.assertEqual(bots[47777].state["pos"], [10.5, 90, 20.5])
                self.assertEqual(bots[47778].state["pos"], [11.5, 90, 21.5])

    def test_consent_refuses_cross_world_clients_before_rcon(self):
        for dim in (
            "minecraft:overworld",
            "blockfield:overpass_1",
            self.NATIVE.replace("12345678", "87654321"),
        ):
            with self.subTest(dim=dim):
                result, commands, _ = self.consent((self.NATIVE, dim))
                self.assertIsInstance(result, AssertionError)
                self.assertEqual(commands, [])

    def test_consent_preserves_occupied_platform(self):
        result, commands, bots = self.consent((self.NATIVE, self.NATIVE), occupied=True)
        self.assertIsInstance(result, AssertionError)
        self.assertEqual(len(commands), 1)
        self.assertEqual(bots[47777].state["pos"], [10.5, 90, 20.5])
        self.assertEqual(bots[47778].state["pos"], [11.5, 90, 21.5])


class StressMetrics(unittest.TestCase):
    def test_spawn_waits_for_late_profiles_before_tagging(self):
        class DelayedRcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.polls = 0

            def cmd(self, command):
                self.commands.append(command)
                if command == "list":
                    self.polls += 1
                    return "players: bot01" + (", bot07" if self.polls >= 4 else "")
                if command.endswith(" Tags"):
                    return 'data: ["bfbot"]'
                return "ok"

        rcon = DelayedRcon()
        with patch.object(common.time, "sleep"):
            stress.spawn(rcon, ["bot01", "bot07"])
        first_tag = rcon.commands.index("tag bot01 add bfbot")
        self.assertEqual(rcon.commands[:first_tag].count("list"), 4)
        self.assertIn("data get entity bot07 Tags", rcon.commands)

    def test_missing_profile_has_a_deadline_without_tagging(self):
        rcon = FakeRcon()
        with (
            patch.object(common.time, "monotonic", side_effect=[0, 31]),
            self.assertRaises(TimeoutError),
        ):
            stress.spawn(rcon, ["bot07"])
        self.assertFalse(any(command.startswith("tag ") for command in rcon.commands))

    def test_spawn_rejects_a_missing_or_different_tag(self):
        for tags in ('data: ["bfbot-other"]', 'data: ["other"]', ""):
            rcon = FakeRcon()
            rcon.cmd = lambda command: "players: bot01" if command == "list" else tags
            with self.subTest(tags=tags), self.assertRaises(RuntimeError):
                stress.spawn(rcon, ["bot01"])

    def test_gunner_requires_the_full_requested_load(self):
        rcon = FakeRcon()
        for reply in ("firing=7 SUCCESS=120", "", "firing=0"):
            rcon.cmd = lambda _: reply
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                stress.gunner_stats(rcon, 8)
        rcon.cmd = lambda _: "firing=8 SUCCESS=120"
        self.assertEqual(stress.gunner_stats(rcon, 8), "firing=8 SUCCESS=120")

    def test_sampling_failure_stops_the_background_controller(self):
        with (
            patch.object(stress.threading, "Event") as event,
            patch.object(stress.threading, "Thread") as thread,
            patch.object(stress, "spawn"),
            patch.object(stress, "walk"),
            patch.object(stress.time, "sleep"),
            patch.object(
                stress, "sample_window", side_effect=RuntimeError("audit failure")
            ),
        ):
            with self.assertRaises(RuntimeError):
                stress.hub(FakeRcon(), lambda: "", [1], 0, lambda: FakeRcon())
            event.return_value.set.assert_called_once()
            thread.return_value.join.assert_called_once_with(timeout=5)

    def test_tick_query_parses_decimal_commas(self):
        self.assertEqual(stress.tick_query(FakeRcon()), [12.3, 10.1, 20.5, 30.0])
        rcon = FakeRcon()
        rcon.cmd = lambda _: (
            "Average time per tick: 12.3ms (Target: 50.0ms)\n"
            "Percentiles: P50: 10.1ms P95: 20.5ms P99: 30.0ms, sample: 100"
        )
        self.assertEqual(stress.tick_query(rcon), [12.3, 10.1, 20.5, 30.0])

    def test_report_counts_kills_and_captures(self):
        row = stress.report(
            [[10.0, 8.0, 12.0, 14.0]],
            ["a убит b", "точка захвачена командой X", "shot rejected: lag"],
            scenario="hub",
        )
        self.assertEqual(
            (row["kills"], row["captures"], row["rejected_shots"]), (1, 1, 1)
        )
        self.assertEqual(row["mspt_avg"], 10.0)


class CarpetHygiene(unittest.TestCase):
    def test_park_kills_before_teleport_and_revive_respawns_after(self):
        rcon = FakeRcon()
        common.park_carpet_bots(rcon, ["DMVoteBot", "bot07"])
        common.revive_carpet_bots(rcon, ["DMVoteBot", "bot07"])
        self.assertEqual(
            rcon.commands,
            [
                "player DMVoteBot kill",
                "player bot07 kill",
                "player DMVoteBot spawn",
                "player bot07 spawn",
            ],
        )

    def test_suspicious_bot_name_sends_nothing(self):
        for bad in ("", "a", "x" * 17, "bot;kill", "bot\nkill", "../x"):
            rcon = FakeRcon()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                common.park_carpet_bots(rcon, [bad])
            self.assertEqual(rcon.commands, [])


class Registry(unittest.TestCase):
    def test_all_scenarios_registered(self):
        self.assertEqual(
            sorted(SCENARIOS),
            [
                "body-pose",
                "check-benelli",
                "check-consent",
                "check-gameplay",
                "dm",
                "hitreg",
                "parkour",
                "range",
                "stress",
            ],
        )


if __name__ == "__main__":
    unittest.main()
