"""Hermetic checks for the ported scenarios; no game, no sockets, no ~/.cache."""

import itertools
import json
import os
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
    body_pose,
    common,
    entry,
    parkour,
    hitreg,
    hud_checks,
    stress,
    dm,
    duo,
    knife,
    rc,
    range as firing_range,
    readiness,
    seam,
    world_c2,
    weapons_sweep,
)


class FakeRcon:
    def __init__(self):
        self.commands = []

    def cmd(self, command):
        if " run damage " in command:
            return "Applied 1.0 damage to Bob"
        self.commands.append(command)
        if command == "list":
            return "players: hitbot"
        if "Dimension" in command:
            return '"blockfield:test_1"'
        if "Health" in command:
            return "hitbot has the following entity data: 20.0f"
        if command == "tick query":
            return (
                "Target tick rate: 20,0 per second.\n"
                "Average time per tick: 12,3ms (Target: 50,0ms)\n"
                "Percentiles: P50: 10,1ms P95: 20,5ms P99: 30,0ms, sample: 100"
            )
        return "ok"


class ZeroSpreadRcon(FakeRcon):
    """Stand echo for the duo/admin gun swap: the GunId reads back the swap."""

    def cmd(self, command):
        if command.startswith("data get") and "GunId" in command:
            self.commands.append(command)
            return 'Bot has the following entity data: "tacz:ak47_zero_spread"'
        return super().cmd(command)


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
    def test_client_success_without_server_acceptance_is_reported_separately(self):
        class World(FakeRcon):
            def cmd(self, command):
                if command.endswith("GunId"):
                    return hitreg.GUN
                if command.endswith("GunCurrentAmmoCount"):
                    return "data: 30"
                if command.endswith("Health"):
                    return "data: 1000.0f"
                return super().cmd(command)

        def bot(command):
            if command == "state":
                return {"name": "Player1", "slot": 0}
            if command == "frames get":
                return {"samples": []}
            return {"result": "SUCCESS"}

        clock = iter(range(100))
        with (
            patch.object(hitreg.time, "sleep"),
            patch.object(hitreg.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(
                hitreg, "server_shot", side_effect=TimeoutError("unconfirmed")
            ),
        ):
            report = hitreg.run_shots(World(), bot, 1, "still", "flick")
        self.assertEqual(report["shots"], 1)
        self.assertEqual(report["confirmed"], 0)
        self.assertEqual(report["hits"], 0)

    def test_uuid_reads_the_nbt_int_array(self):
        rcon = SimpleNamespace(
            cmd=lambda _: "Player1 has the following entity data: [I; 1, 2, -3, 4]"
        )
        self.assertEqual(
            hitreg.uuid(rcon, "Player1"), "00000001-0000-0002-ffff-fffd00000004"
        )
        self.assertIsNone(hitreg.uuid(SimpleNamespace(cmd=lambda _: ""), "Player1"))

    def test_frame_time_axis_uses_render_intervals_including_spikes(self):
        samples = [f"{t} 1 0.5 cam 0 0 0" for t in (100000, 150000, 200000, 1200000)]
        samples.insert(1, "100000 1 0.5 2 superbwarfare:drone 0 0 0")
        measured = hitreg.frame_times(samples)
        self.assertEqual(measured["frames"], 4)
        self.assertEqual(measured["p50_ms"], 50)
        self.assertEqual(measured["p99_ms"], 1000)
        self.assertEqual(hitreg.frame_times([]), {"frames": 0})

    def test_missing_target_is_an_error(self):
        with self.assertRaises(AssertionError):
            hitreg.health(SimpleNamespace(cmd=lambda _: "No entity was found"))

    def test_healing_is_removed_before_sampling(self):
        rcon = FakeRcon()
        with patch.object(hitreg.time, "sleep"):
            hitreg.heal(rcon)
        self.assertEqual(
            rcon.commands[-1], "effect clear hitbot minecraft:instant_health"
        )

    def test_server_ack_requires_exactly_one_round(self):
        rcon = SimpleNamespace(cmd=lambda _: "data: 30")
        with self.assertRaises(TimeoutError):
            hitreg.server_shot(rcon, "Player1", "ammo", 30, timeout=0)
        rcon.cmd = lambda _: "data: 28"
        with self.assertRaises(AssertionError):
            hitreg.server_shot(rcon, "Player1", "ammo", 30, timeout=1)
        rcon.cmd = lambda _: "data: 29"
        self.assertEqual(hitreg.server_shot(rcon, "Player1", "ammo", 30), 29)

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

    def test_report_splits_no_ammo_rejections(self):
        row = stress.report(
            [],
            [
                "shot rejected: bot01 NO_AMMO gun=tacz:ak47",
                "shot rejected: bot02 COOL_DOWN gun=tacz:ak47",
            ],
            scenario="combat",
            guns=True,
        )
        self.assertEqual(row["rejected_shots"], 2)
        self.assertEqual(row["rejected_no_ammo"], 1)


class GunRcon:
    """Stateful stand-in: spawn reconnects a missing bot, stats mirror arming."""

    def __init__(self):
        self.commands = []
        self.present = {"bot01", "bot02"}
        self.armed_override = None
        self.arm_on_deploy = False
        self.stats_calls = 0
        self.fail_stats_calls = set()

    def cmd(self, command):
        self.commands.append(command)
        if command == "list":
            return "players: " + ", ".join(sorted(self.present))
        if command.endswith(" Tags"):
            return '["bfbot"]'
        if command == "bf action @a spawn:BASE" and self.arm_on_deploy:
            self.armed_override = None  # deploy sticks once the match is live
            return "ok"
        if command == "bf bot stats":
            self.stats_calls += 1
            if self.stats_calls in self.fail_stats_calls:
                return "firing=1 SUCCESS=0"  # one casualty snapshot, then recovers
            armed = (
                self.armed_override
                if self.armed_override is not None
                else len(self.present)
            )
            return f"firing={armed} SUCCESS=9"
        if command.startswith("player ") and command.endswith(" spawn"):
            self.present.add(command.split()[1])
            return "ok"
        return "ok"


def gun_args(**over):
    params = {
        "campaign": 0,
        "dm": 2,
        "dm_map": "dust2",
        "campaign_map": "",
        "guns": True,
        "warmup": 0,
        "window": 0,
    }
    params.update(over)
    return SimpleNamespace(**params)


class StressGunSteadyState(unittest.TestCase):
    def run_combat(self, rcon, **patches):
        import contextlib

        args = gun_args()
        calls = []

        def get_log():
            calls.append(1)
            # the room opens after the play command, like a growing server log
            return "" if len(calls) < 2 else "Комната dm1 открыта"

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(stress.threading, "Event"))
            stack.enter_context(patch.object(stress.threading, "Thread"))
            stack.enter_context(patch.object(stress.time, "sleep"))
            for name, kw in patches.items():
                stack.enter_context(patch.object(stress, name, **kw))
            stress.combat(rcon, get_log, args, ".", lambda: rcon)
        return rcon.commands

    def test_rejoin_respawns_rejoins_and_refires_missing_bots(self):
        rcon = GunRcon()
        rcon.present = {"bot01"}
        missing = stress.rejoin(
            rcon,
            ["bot01", "bot02"],
            {"bot01": "dm1", "bot02": "dm1"},
            sprint=False,
            fire_secs=25,
        )
        self.assertEqual(missing, ["bot02"])
        for command in (
            "player bot02 spawn",
            "bf action bot02 join:dm1",
            "bf action bot02 class:assault:0",
            "execute as bot02 run ready",
            "player bot02 move forward",
            "bf action bot02 spawn:BASE",
            "bf bot fire bot02 25",
        ):
            self.assertIn(command, rcon.commands)

    def test_rejoin_without_fire_secs_skips_gun_commands(self):
        rcon = GunRcon()
        rcon.present = {"bot01"}
        self.assertEqual(
            stress.rejoin(
                rcon, ["bot01", "bot02"], {"bot02": "dm1"}, sprint=True, fire_secs=None
            ),
            ["bot02"],
        )
        self.assertTrue(all("bf bot fire" not in command for command in rcon.commands))
        self.assertTrue(all("spawn:BASE" not in command for command in rcon.commands))
        self.assertIn("player bot02 sprint", rcon.commands)

    def test_rejoin_failure_is_visible_but_not_fatal(self):
        rcon = GunRcon()
        rcon.present = {"bot01"}
        with patch.object(stress, "wait_while", return_value=False):
            # attempted, reported via print, no raise; the end gates decide
            self.assertEqual(
                stress.rejoin(
                    rcon,
                    ["bot01", "bot02"],
                    {"bot02": "dm1"},
                    sprint=False,
                    fire_secs=9,
                ),
                ["bot02"],
            )

    def test_mid_window_disconnect_recovers_before_the_strict_gates(self):
        rcon = GunRcon()
        real_window = stress.sample_window

        def flipping(window_rcon, get_log, seconds, interval=5):
            window_rcon.present = {"bot01"}
            return real_window(window_rcon, get_log, seconds, interval)

        commands = self.run_combat(rcon, sample_window={"side_effect": flipping})
        self.assertIn("player bot02 spawn", commands)
        self.assertTrue(
            any(c.startswith("bf bot fire bot02 ") for c in commands),
            commands,
        )
        self.assertTrue(
            any(c.startswith("bf bot fire @a[tag=bfbot]") for c in commands),
            commands,
        )

    def test_degraded_arming_fails_before_the_measurement_window(self):
        rcon = GunRcon()
        rcon.armed_override = 1  # present but gunless, e.g. campaign kit flow
        with (
            patch.object(stress, "ARM_WAIT_SECS", 0),
            self.assertRaises(RuntimeError),
        ):
            self.run_combat(
                rcon,
                sample_window={
                    "side_effect": AssertionError("window on degraded load")
                },
            )

    def test_arming_waits_for_match_start_then_measures(self):
        rcon = GunRcon()
        rcon.armed_override = 1  # tablets until the first deploy sticks
        rcon.arm_on_deploy = True
        commands = self.run_combat(rcon)
        self.assertIn("bf action @a spawn:BASE", commands)

    def test_final_sweep_retries_churn_then_holds(self):
        rcon = GunRcon()
        rcon.fail_stats_calls = {3}  # pre-window ok, drain ok, round 1 short
        self.run_combat(rcon)
        broadcasts = [
            c for c in rcon.commands if c.startswith("bf bot fire @a[tag=bfbot]")
        ]
        self.assertGreaterEqual(len(broadcasts), 2)

    def test_final_sweep_gives_up_loudly(self):
        rcon = GunRcon()
        rcon.armed_override = 1  # a bot that never comes back
        with (
            patch.object(stress, "ARM_WAIT_SECS", 0),
            patch.object(stress, "SWEEP_ROUNDS", 2),
            self.assertRaises(RuntimeError),
        ):
            self.run_combat(rcon)


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
                "duo",
                "entry",
                "hitreg",
                "hud-checks",
                "knife",
                "parkour",
                "pose-overlay",
                "range",
                "rc",
                "seam",
                "stress",
                "tablet",
                "weapons-sweep",
                "world-c2",
            ],
        )


class WorldC2(unittest.TestCase):
    def test_resolve_mirrors_pack_updates(self):
        local, contract = "a" * 64, "c" * 64
        self.assertEqual(
            world_c2.resolve_kind(local, contract, local, contract), "NONE"
        )
        self.assertEqual(
            world_c2.resolve_kind(local, contract, "b" * 64, "zz"), "MANDATORY"
        )
        self.assertEqual(
            world_c2.resolve_kind(local, contract, "b" * 64, contract), "OPTIONAL"
        )
        self.assertEqual(
            world_c2.resolve_kind(local, contract, "short", contract), "NONE"
        )
        self.assertEqual(world_c2.resolve_kind(local, "", "b" * 64, ""), "MANDATORY")

    def test_parse_range_open_closed_clamped(self):
        self.assertEqual(world_c2.parse_range("bytes=5-", 10), (5, 9))
        self.assertEqual(world_c2.parse_range("bytes=0-3", 10), (0, 3))
        self.assertEqual(world_c2.parse_range("bytes=0-1048575", 100), (0, 99))
        self.assertEqual(world_c2.parse_range("bytes=-3", 10), (7, 9))
        self.assertIsNone(world_c2.parse_range("bytes=abc", 10))
        self.assertIsNone(world_c2.parse_range("bytes=", 10))

    def test_none_run_catches_boot_time_marker(self):
        # A decision marker logged at boot sits before log_pos, so the
        # window grep (hits) misses it: the NONE gate must fail on the
        # full-log grep instead of passing quietly.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if True:
            game = Path(tmp.name) / "game"
            (game / "config").mkdir(parents=True)
            (game / "config" / "blockfield-pack-version.txt").write_text("d" * 64)
            (game / "config" / "blockfield-pack-contract.txt").write_text("c" * 64)
            log = game / "latest.log"
            log.write_text(
                "boot\nPackUpdates: optional pack update available: 2.78.1\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                expect="none",
                window=0,
                join=None,
                bot_port=1,
                client_dir=str(game),
                client_log=str(log),
            )

            class FakeBot:
                def __call__(self, cmd, timeout=None):
                    if cmd == "state":
                        return {"guiSize": [960, 540], "connected": True}
                    if cmd.startswith("screenshot"):
                        return {"file": "shot.png"}
                    return {}

                def close(self):
                    pass

            with (
                patch.object(
                    world_c2,
                    "fetch_build_json",
                    return_value={"client": "d" * 64, "compatibility": "c" * 64},
                ),
                patch.object(world_c2, "banner_pixels", return_value=(0, 10)),
                patch("builtins.print"),
            ):
                out = world_c2.run_pack(args, bot_factory=lambda port: FakeBot())
        # prediction, no-optional-line, no-decision, connected: only the
        # boot-marker row fails (tally returns the failure count).
        self.assertEqual(out, 1)

    def test_parse_room_open_reads_newest_room(self):
        self.assertEqual(world_c2.parse_room_open("no rooms here"), "")
        self.assertEqual(
            world_c2.parse_room_open(
                "Комната 1 открыта (Каждый сам за себя)\nКомната 2 открыта (x)"
            ),
            "2",
        )

    def test_enter_deathmatch_joins_spawns_arms(self):
        commands = []

        def bot(cmd, timeout=None):
            commands.append(cmd)
            if cmd == "state":
                return {"dimension": "blockfield:dm_arena_2"}
            return {"ok": True}

        room = world_c2.enter_deathmatch(
            bot, lambda: "Комната 2 открыта (Каждый сам за себя)", "dm_arena"
        )
        self.assertEqual(room, "2")
        self.assertEqual(
            commands,
            [
                "bfaction play:deathmatch:dm_arena",
                "bfaction join:2",
                "bfaction class:assault:0",
                "state",
                "slot 0",
            ],
        )

    def test_enter_deathmatch_empty_without_room(self):
        room = world_c2.enter_deathmatch(
            lambda cmd, timeout=None: {"ok": True},
            lambda: "quiet log",
            "dm_arena",
            timeout=0.1,
        )
        self.assertEqual(room, "")

    def test_banner_detector_round_trip(self):
        import struct
        import zlib

        width, height = 960, 540
        raw = bytearray()
        for y in range(height):
            raw.append(0)
            raw.extend(b"\x20\x20\x20" * width)
        for y in range(425, 435):
            for x in range(400, 560):
                off = y * (width * 3 + 1) + 1 + x * 3
                raw[off : off + 3] = bytes(world_c2.BANNER_TEXT)

        def chunk(kind, data):
            out = kind + data
            return (
                struct.pack(">I", len(data)) + out + struct.pack(">I", zlib.crc32(out))
            )

        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b"")
        )
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            shot.write_bytes(png)
            hits, total = world_c2.banner_pixels(str(shot), [960, 540])
        self.assertGreaterEqual(hits, 1000)
        self.assertGreater(total, 0)
        with tempfile.TemporaryDirectory() as tmp:
            plain = bytearray()
            for _ in range(height):
                plain.append(0)
                plain.extend(b"\x20\x20\x20" * width)
            blank = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(plain)))
                + chunk(b"IEND", b"")
            )
            shot = Path(tmp) / "blank.png"
            shot.write_bytes(blank)
            hits, _ = world_c2.banner_pixels(str(shot), [960, 540])
        self.assertEqual(hits, 0)

    def test_watchdog_mirror_entry_is_absolute_https(self):
        entry = world_c2.mirror_entry(18082)
        self.assertTrue(entry["url"].startswith("https://"), entry["url"])
        self.assertIn("{path}", entry["url"])
        self.assertNotIn("{base}", entry["url"])

    def test_watchdog_tls_round_trip(self):
        import shutil
        import socket
        import ssl
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        if shutil.which("openssl") is None or (
            shutil.which("keytool") is None
            and not Path(
                os.environ.get("JAVA_HOME", "/nonexistent"), "bin", "keytool"
            ).is_file()
        ):
            self.skipTest("watchdog TLS needs openssl + keytool")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b"probe"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with tempfile.TemporaryDirectory() as tmp:
            cert, key, store = world_c2.ensure_mirror_tls(tmp)
            self.assertTrue(store.is_file())
            # Second call reuses the generated set instead of shelling out.
            again = world_c2.ensure_mirror_tls(tmp)
            self.assertEqual((cert, key, store), again)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert), str(key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                client = ssl.create_default_context(cafile=str(cert))
                with socket.create_connection(
                    ("127.0.0.1", server.server_address[1]), timeout=10
                ) as raw:
                    with client.wrap_socket(raw, server_hostname="127.0.0.1") as tls:
                        self.assertEqual(tls.version()[:3], "TLS")
                        tls.sendall(b"GET /probe HTTP/1.0\r\nHost: x\r\n\r\n")
                        body = b""
                        while True:
                            chunk = tls.recv(4096)
                            if not chunk:
                                break
                            body += chunk
                        self.assertTrue(body.endswith(b"probe"))
            finally:
                server.shutdown()
                server.server_close()

    def test_stage_workshop_draft_copies_base_map_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            session = root / "drafts" / "session-1" / "dm_arena"
            (session / "world" / "region").mkdir(parents=True)
            base = server / "blockfield" / "maps"
            base.mkdir(parents=True)
            (base / "dm_arena.json").write_text(
                '{"id": "dm_arena", "kind": "arena"}', encoding="utf-8"
            )
            staged = world_c2.stage_workshop_draft(
                server, "dm_arena", drafts_root=root / "drafts"
            )
            self.assertEqual(staged["staged"], [str(session / "map.json")], staged)
            self.assertEqual(
                json.loads((session / "map.json").read_text(encoding="utf-8"))["id"],
                "dm_arena",
            )
            # Second run finds the copy ready and stages nothing.
            again = world_c2.stage_workshop_draft(
                server, "dm_arena", drafts_root=root / "drafts"
            )
            self.assertEqual(again["staged"], [])
            self.assertEqual(again["ready"], [str(session / "map.json")])
            self.assertEqual(again["reason"], "")

    def test_stage_workshop_draft_refuses_foreign_map_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = root / "server"
            session = root / "drafts" / "session-1" / "dm_arena"
            (session / "world" / "region").mkdir(parents=True)
            base = server / "blockfield" / "maps"
            base.mkdir(parents=True)
            (base / "dm_arena.json").write_text(
                '{"id": "mirage", "kind": "arena"}', encoding="utf-8"
            )
            staged = world_c2.stage_workshop_draft(
                server, "dm_arena", drafts_root=root / "drafts"
            )
            self.assertEqual(staged["staged"], [])
            self.assertIn("identity", staged["reason"])
            self.assertFalse((session / "map.json").exists())
            missing = world_c2.stage_workshop_draft(
                root / "empty-server", "dm_arena", drafts_root=root / "drafts"
            )
            self.assertEqual(missing["staged"], [])
            self.assertTrue(missing["reason"])


class WorldC2Pack(unittest.TestCase):
    def test_log_markers(self):
        self.assertTrue(
            world_c2.OPTIONAL_LOG_RE.search(
                "[Render thread/INFO] [PackUpdates]: "
                "PackUpdates: optional pack update available: 2.78.1"
            )
        )
        self.assertTrue(
            world_c2.MANDATORY_LOG_RE.search(
                "PackUpdates: mandatory pack update required: 2.79.0"
            )
        )
        self.assertTrue(
            world_c2.NOTIFY_OFF_RE.search(
                "PackUpdates: menu.vanilla dev mode, update notify off"
            )
        )

    def test_banner_pixels_on_synthetic_png(self):
        import binascii
        import struct
        import zlib

        def png(width, height, dot=None):
            def chunk(kind, payload):
                return (
                    struct.pack(">I", len(payload))
                    + kind
                    + payload
                    + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
                )

            raw = b""
            for y in range(height):
                raw += b"\x00"
                for x in range(width):
                    raw += (
                        bytes(world_c2.BANNER_TEXT)
                        if dot == (x, y)
                        else b"\x00\x00\x00"
                    )
            return (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b"")
            )

        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "shot.png"
            shot.write_bytes(png(200, 150, dot=(100, 30)))
            hits, total = world_c2.banner_pixels(str(shot), [200, 150])
            self.assertEqual((hits, total), (1, (62 - 17) * 140))
            shot.write_bytes(png(200, 150))
            self.assertEqual(world_c2.banner_pixels(str(shot), [200, 150])[0], 0)


class RcBattery(unittest.TestCase):
    def test_arena_bounds_reads_battery_map(self):
        """arena_bounds covers any deployed map config, not just dm_arena
        (RC final5 long-range FAIL: hamburg lanes had no bounds to clamp)."""
        with tempfile.TemporaryDirectory() as tmp:
            maps = Path(tmp) / "blockfield" / "maps"
            maps.mkdir(parents=True)
            (maps / "dm_arena.json").write_text(
                json.dumps(
                    {"bounds": {"minX": -32, "maxX": 31, "minZ": -32, "maxZ": 31}}
                )
            )
            (maps / "hamburg.json").write_text(
                json.dumps(
                    {
                        "bounds": {
                            "minX": -222,
                            "maxX": 57,
                            "minZ": -52,
                            "maxZ": 110,
                        }
                    }
                )
            )
            self.assertEqual(dm.arena_bounds(tmp), (-32, 31, -32, 31))
            self.assertEqual(dm.arena_bounds(tmp, "hamburg"), (-222, 57, -52, 110))
            self.assertIsNone(dm.arena_bounds(tmp, "no_such_map"))
            self.assertIsNone(dm.arena_bounds(None, "hamburg"))

    def test_pin_version_reads_filename(self):
        text = 'filename = "blockfield-client-1.55.3+mc1.21.1.jar"'
        self.assertEqual(rc.pin_version(text), "1.55.3")
        with self.assertRaises((KeyError, ValueError)):
            rc.pin_version('filename = "weird.jar"')

    def test_pin_version_rejects_mc_version_confusion(self):
        text = 'filename = "blockfield-server-1.57.0+mc1.21.1.jar"'
        self.assertEqual(rc.pin_version(text), "1.57.0")

    def test_server_mod_source_reads_mod_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            props = Path(tmp) / "mod-server"
            props.mkdir()
            (props / "gradle.properties").write_text(
                "minecraft_version=1.21.1\nmod_version=1.57.0\n",
                encoding="utf-8",
            )
            (props / "upstream.properties").write_text(
                "upstreamCommit=abc123\n", encoding="utf-8"
            )
            self.assertEqual(rc.server_mod_source(Path(tmp)), ("1.57.0", "abc123"))

    def test_server_mod_source_missing_upstream_stays_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            props = Path(tmp) / "mod-server"
            props.mkdir()
            (props / "gradle.properties").write_text(
                "mod_version=1.57.0\n", encoding="utf-8"
            )
            self.assertEqual(rc.server_mod_source(Path(tmp)), ("1.57.0", None))

    def test_server_mod_source_without_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                rc.server_mod_source(Path(tmp))

    def test_loaded_version_prefers_commit_pair(self):
        self.assertEqual(
            rc.loaded_version("§7[BlockField] 1.55.3 commit abc123"), "1.55.3"
        )
        self.assertEqual(
            rc.loaded_version("§7[BlockField] 1.57.0+mc1.21.1 commit 11fc09eb2f"),
            "1.57.0",
        )
        self.assertEqual(
            rc.loaded_version(
                "§7[BlockField] сервер v2.79.0, клиент v2.78.4, мод 1.57.0 commit x"
            ),
            "1.57.0",
        )
        with self.assertRaises(ValueError):
            rc.loaded_version("no version here")

    def test_log_mod_version_reads_fabric_list(self):
        self.assertEqual(rc.log_mod_version("\t- blockfield 1.55.3"), "1.55.3")

    def test_scan_errors_skips_clean_lines(self):
        self.assertEqual(rc.scan_errors("INFO all good\n"), [])
        hits = rc.scan_errors("[Server] ERROR: boom\nINFO fine\n")
        self.assertEqual(len(hits), 1)
        self.assertEqual(rc.scan_errors("[Server] ERROR: boom\n", allow=("boom",)), [])

    def test_scan_errors_judges_lines_by_log_level(self):
        # An INFO line only matches the error pattern via a quoted substring
        # (llvmpipe "type=ERROR" shader notice); it must never fail the scan,
        # while WARN/ERROR and level-less lines still do.
        info = (
            "[12:16:12] [Render thread/INFO]: OpenGL debug message: id=1,"
            " source=SHADER COMPILER, type=ERROR, severity=HIGH,"
            " message='0:1(10): error: GLSL 4.60 is not supported.'"
        )
        self.assertEqual(rc.scan_errors(info + "\n", rc.LOG_SCAN_ALLOW), [])
        self.assertEqual(rc.scan_errors("[main/INFO]: something FAILED badly\n"), [])
        warn = "[main/WARN]: something FAILED badly"
        self.assertEqual(rc.scan_errors(warn + "\n"), [warn])
        self.assertEqual(
            rc.scan_errors("[Server] ERROR: boom\n"), ["[Server] ERROR: boom"]
        )

    def test_scan_errors_ignores_rechiseled_jei_probes(self):
        # rc-final7 log-scan FAIL: Rechiseled's optional JEI-compat mixins
        # (mixin.jei.RecipeLayoutInputHandlerMixin +
        # RecipeLayoutUserInputHandlerMixin, both @Pseudo) probe the JEI
        # classes at startup; JEI is not installed, so Mixin logs one WARN
        # each and skips them. Same family as the JER/Quilt probes.
        for target in (
            "mezz/jei/library/gui/recipes/RecipeLayoutInputHandler",
            "mezz/jei/gui/recipes/RecipeLayoutWithButtons$RecipeLayoutUserInputHandler",
        ):
            line = (
                "[main/WARN]: Error loading class: "
                + target
                + f" (java.lang.ClassNotFoundException: {target})"
            )
            self.assertEqual(rc.scan_errors(line + "\n", rc.LOG_SCAN_ALLOW), [])

    def test_scan_errors_allowlists_benign_startup_signature(self):
        benign = (
            "[main/WARN]: Error loading class: net/minecraft/class_881 "
            "(java.lang.ClassNotFoundException: net/minecraft/class_881)"
        )
        self.assertEqual(rc.scan_errors(benign + "\n", rc.LOG_SCAN_ALLOW), [])
        hits = rc.scan_errors(
            benign + "\n[Server] ERROR: real problem\n", rc.LOG_SCAN_ALLOW
        )
        self.assertEqual(hits, ["[Server] ERROR: real problem"])

    def test_scan_errors_allowlists_exact_optional_integration_probes(self):
        # Each allowlist entry names the exact class: a failure for any other
        # class must still fail the scan.
        for allowed in rc.LOG_SCAN_ALLOW:
            if allowed == "Error loading class: net/minecraft/class_":
                continue
            if allowed.startswith("No data fixer registered for "):
                continue
            if "FabricService" in allowed:
                continue
            self.assertIn("Error loading class: ", allowed)
            self.assertFalse(allowed.endswith(("class_", "/")))
            line = f"[main/WARN]: {allowed} (java.lang.ClassNotFoundException: x)"
            self.assertEqual(rc.scan_errors(line + "\n", rc.LOG_SCAN_ALLOW), [])
        other = (
            "[main/WARN]: Error loading class: com/example/Broken"
            " (java.lang.ClassNotFoundException: com/example/Broken)"
        )
        self.assertEqual(rc.scan_errors(other + "\n", rc.LOG_SCAN_ALLOW), [other])

    def test_scan_errors_allowlists_exact_fabric_installer_fallback(self):
        # The fabric-server-launch.jar cold-install mirror fallback names the
        # exact service plus manifest: any other URL or a bare timeout fails.
        line = (
            "service FabricService{meta='https://meta.fabricmc.net/',"
            " maven='https://maven.fabricmc.net/'} failed:"
            " java.io.IOException: Request to"
            " https://maven.fabricmc.net/net/minecraft/experimental_versions.json"
            " failed: Connect timed out"
        )
        self.assertEqual(rc.scan_errors(line + "\n", rc.LOG_SCAN_ALLOW), [])
        other_url = line.replace(
            "experimental_versions.json", "version_manifest_v2.json"
        )
        self.assertEqual(
            rc.scan_errors(other_url + "\n", rc.LOG_SCAN_ALLOW), [other_url]
        )
        bare = "[main/ERROR]: Connect timed out"
        self.assertEqual(rc.scan_errors(bare + "\n", rc.LOG_SCAN_ALLOW), [bare])

    def test_scan_errors_allowlists_exact_datafixer_ids(self):
        # Each entry names the exact entity id whose build(String) call lives outside
        # our forks; a warning for any other id must still fail the scan, and a shorter
        # id must not match a longer one (pale_oak_boat vs pale_oak_chest_boat).
        ids = [
            allowed.removeprefix("No data fixer registered for ")
            for allowed in rc.LOG_SCAN_ALLOW
            if allowed.startswith("No data fixer registered for ")
        ]
        self.assertEqual(
            ids,
            [
                "copper_golem",
                "creaking",
                "happy_ghast",
                "pale_oak_boat",
                "pale_oak_chest_boat",
            ],
        )
        for entity in ids:
            self.assertTrue(entity and " " not in entity)
            line = f"[main/ERROR]: No data fixer registered for {entity}"
            self.assertEqual(rc.scan_errors(line + "\n", rc.LOG_SCAN_ALLOW), [])
        other = "[main/ERROR]: No data fixer registered for target_minecart"
        self.assertEqual(rc.scan_errors(other + "\n", rc.LOG_SCAN_ALLOW), [other])
        chest = "[main/ERROR]: No data fixer registered for pale_oak_chest_boat"
        self.assertEqual(
            rc.scan_errors(
                chest + "\n", ("No data fixer registered for pale_oak_boat",)
            ),
            [chest],
        )

    def test_scan_errors_ignores_raknetify_quilt_probe(self):
        # rc-final6 log-scan FAIL: raknetify-fabric's optional Quilt-compat mixin
        # (compat.qsl.MixinServerLoginNetworkAddon, @Pseudo target
        # org.quiltmc.qsl.networking.impl.server.ServerLoginNetworkAddon) probes
        # the Quilt class at startup; Quilt is not installed, so Mixin logs one
        # WARN and skips it. Third-party jar, no fork: exact line only.
        line = (
            "[main/WARN]: Error loading class: "
            "org/quiltmc/qsl/networking/impl/server/ServerLoginNetworkAddon "
            "(java.lang.ClassNotFoundException: "
            "org/quiltmc/qsl/networking/impl/server/ServerLoginNetworkAddon)"
        )
        self.assertEqual(rc.scan_errors(line + "\n", rc.LOG_SCAN_ALLOW), [])

    def test_scan_errors_allows_softpipe_gl_capability_on_softpipe_stand(self):
        # RC triage phase 4 log-scan FAIL: an OpenGL/GLSL capability notice at
        # ERROR on the software-GL stand (rc-final7 lines 73-76/112 show the
        # INFO twin of the same llvmpipe notice). Narrow and renderer-gated:
        # the same log must name the software renderer, and the line itself
        # must be an explicit-ERROR OpenGL/GLSL capability notice.
        renderer = "[12:16:12] [Render thread/INFO]: GL renderer: llvmpipe (LLVM 19)"
        info = (
            "[12:16:12] [Render thread/INFO]: OpenGL debug message: id=1,"
            " message='GLSL 4.60 is not supported.'"
        )
        error = (
            "[12:16:12] [Render thread/ERROR]: OpenGL capability check failed:"
            " GLSL 4.60 is not supported, falling back to software rendering."
        )
        log = "\n".join([renderer, info, error]) + "\n"
        self.assertEqual(rc.scan_errors(log, rc.LOG_SCAN_ALLOW), [])

    def test_scan_errors_keeps_gl_capability_error_without_softpipe_renderer(self):
        # Same capability ERROR on a hardware stand (no llvmpipe/softpipe in
        # the log) still fails: the gate never allows it blind.
        error = (
            "[12:16:12] [Render thread/ERROR]: OpenGL capability check failed:"
            " GLSL 4.60 is not supported, falling back to software rendering."
        )
        log = (
            "[12:16:12] [Render thread/INFO]: GL renderer: NVIDIA GeForce\n"
            + error
            + "\n"
        )
        self.assertEqual(rc.scan_errors(log, rc.LOG_SCAN_ALLOW), [error])

    def test_scan_errors_keeps_non_capability_softpipe_errors(self):
        # Renderer-gated does not mean renderer-blind: a llvmpipe crash that
        # is not an OpenGL/GLSL capability notice still fails the scan.
        log = (
            "[12:16:12] [Render thread/INFO]: GL renderer: llvmpipe (LLVM 19)\n"
            "[12:16:12] [Render thread/ERROR]: llvmpipe winsys context"
            " creation FAILED\n"
        )
        hits = rc.scan_errors(log, rc.LOG_SCAN_ALLOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("llvmpipe", hits[0])

    def test_scan_errors_keeps_context_creation_failure_on_softpipe_stand(self):
        # A context-creation failure must never hide behind the capability
        # exemption, even when it names llvmpipe and an unsupported feature.
        log = (
            "[12:16:12] [Render thread/INFO]: GL renderer: llvmpipe (LLVM 19)\n"
            "[12:16:12] [Render thread/ERROR]: Failed to create OpenGL"
            " context: required version unsupported\n"
        )
        hits = rc.scan_errors(log, rc.LOG_SCAN_ALLOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("Failed to create OpenGL context", hits[0])

    def test_scan_errors_keeps_level_less_capability_line(self):
        # Fail-safe: a capability line without an explicit ERROR level still
        # fails even on a softpipe stand.
        log = (
            "[12:16:12] [Render thread/INFO]: GL renderer: llvmpipe (LLVM 19)\n"
            "OpenGL capability check failed: GLSL 4.60 is not supported\n"
        )
        hits = rc.scan_errors(log, rc.LOG_SCAN_ALLOW)
        self.assertEqual(len(hits), 1)

    def test_run_refuses_without_second_client(self):
        """rc-final6: a bot-less second client degraded the RC to SKIP rows
        instead of failing loudly. The battery refuses to start when the
        second bot port does not answer, naming the exact play command."""
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
            hitreg_shots=3,
            game_dir=None,
            record=None,
            tag_client=None,
            tag_server=None,
        )

        class FakeBot:
            def __call__(self, command):
                return {"name": "Solo"}

            def close(self):
                pass

        def factory(port, *_, **__):
            if port == 47778:
                raise OSError("refused")
            return FakeBot()

        with patch("builtins.print") as shown:
            with self.assertRaises(SystemExit) as raised:
                rc.run(
                    args,
                    rcon_factory=lambda *_: FakeRcon(),
                    bot_factory=factory,
                    get_log=lambda: "",
                )
        self.assertEqual(raised.exception.code, readiness.INCONCLUSIVE)
        said = "\n".join(str(call.args[0]) for call in shown.call_args_list)
        self.assertIn("47778", said)
        self.assertIn("just play", said)

    def test_refused_run_writes_fail_record(self):
        """A refused run must not leave a stale --record behind for the
        release gate to reuse (blockfield-bot review on the rc-final7 PR)."""
        with tempfile.TemporaryDirectory() as tmp:
            record = str(Path(tmp) / "rc.json")
            Path(record).write_text(json.dumps({"stale": "PASS"}))
            args = SimpleNamespace(
                rcon_host="localhost",
                rcon_port=25575,
                rcon_password="test",
                bot_port=47777,
                bot_port2=47778,
                map="hamburg",
                hitreg_shots=3,
                game_dir=None,
                record=record,
                tag_client="v2.79.0",
                tag_server="v2.79.3",
            )

            class FakeBot:
                def __call__(self, command):
                    return {"name": "Solo"}

                def close(self):
                    pass

            def factory(port, *_, **__):
                if port == 47778:
                    raise OSError("refused")
                return FakeBot()

            with patch("builtins.print"):
                with self.assertRaises(SystemExit) as refused:
                    rc.run(
                        args,
                        rcon_factory=lambda *_: FakeRcon(),
                        bot_factory=factory,
                        get_log=lambda: "",
                    )
            self.assertEqual(refused.exception.code, readiness.INCONCLUSIVE)
            data = json.loads(Path(record).read_text())
            self.assertEqual([check["result"] for check in data["checks"]], ["FAIL"])

    def _not_ready_abort(self, version_ok, expected):
        """A readiness abort must not leave a PASS-only record behind for the
        release gate: the battery appends an explicit non-passing readiness
        row before writing, so exit 2 cannot leave acceptable evidence."""
        from scenarios import check_gameplay  # noqa: E402

        with tempfile.TemporaryDirectory() as tmp:
            record = str(Path(tmp) / "rc.json")
            args = SimpleNamespace(
                rcon_host="localhost",
                rcon_port=25575,
                rcon_password="test",
                bot_port=47777,
                bot_port2=47778,
                map="hamburg",
                hitreg_shots=3,
                game_dir=None,
                record=record,
                tag_client="v2.79.0",
                tag_server="v2.79.3",
            )

            class FakeBot:
                def __call__(self, command, *args, **kwargs):
                    if command == "state":
                        return {
                            "name": "Solo",
                            "dimension": "blockfield:hamburg_7",
                            "health": 20.0,
                        }
                    return {}

                def close(self):
                    pass

            def factory(port, *_, **__):
                return FakeBot()

            with (
                patch.object(
                    rc,
                    "version_gate",
                    return_value=(
                        [("versions loaded match pins", version_ok, "x")],
                        False,
                    ),
                ),
                patch.object(rc, "deploy", return_value=("3", "blockfield:hamburg_7")),
                patch.object(duo, "kit_or_raise"),
                patch.object(duo, "wait_arena"),
                patch.object(check_gameplay, "shot", return_value=None),
                patch.object(check_gameplay, "lobby", return_value=None),
                patch.object(
                    rc,
                    "long_range",
                    side_effect=readiness.NotReady("vulnerable", "still active"),
                ),
                patch("time.sleep"),
                patch("builtins.print"),
            ):
                self.assertEqual(
                    rc.run(
                        args,
                        rcon_factory=lambda *_: FakeRcon(),
                        bot_factory=factory,
                        get_log=lambda: "",
                        server_dir=tmp,
                    ),
                    expected,
                )
            data = json.loads(Path(record).read_text())
            gate = [c for c in data["checks"] if "readiness" in c["name"]]
            self.assertEqual([c["result"] for c in gate], ["FAIL"])

    def test_not_ready_abort_poisons_the_record(self):
        self._not_ready_abort(True, readiness.INCONCLUSIVE)

    def test_not_ready_abort_keeps_an_earlier_product_failure(self):
        # A product FAIL before the abort stays exit 1, never hidden as 2.
        self._not_ready_abort(False, 1)

    def test_version_gate_stops_on_mismatch(self):
        class Rcon:
            def cmd(self, command):
                return "§7[BlockField] 1.55.3 commit abc"

        def tree(version):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            client = Path(tmp.name) / "c.toml"
            client.write_text('filename = "blockfield-client-1.55.3+mc1.21.1.jar"')
            props = Path(tmp.name) / "server" / "mod-server"
            props.mkdir(parents=True)
            (props / "gradle.properties").write_text(
                f"mod_version={version}\n", encoding="utf-8"
            )
            return client, Path(tmp.name) / "server"

        client, server = tree("1.55.3")
        rows, stop = rc.version_gate(
            Rcon(), "1.55.3 commit abc", "- blockfield 1.55.3", client, server
        )
        self.assertFalse(stop)
        self.assertTrue(all(ok for _, ok, _ in rows))
        client, server = tree("1.57.0")
        rows, stop = rc.version_gate(Rcon(), "", "- blockfield 1.55.3", client, server)
        self.assertTrue(stop)
        self.assertTrue(any(not ok for _, ok, _ in rows))

    def test_version_gate_rejects_unreadable_server_source(self):
        class Rcon:
            def cmd(self, command):
                return "§7[BlockField] 1.55.3 commit abc"

        pin = 'filename = "blockfield-client-1.55.3+mc1.21.1.jar"'
        with tempfile.TemporaryDirectory() as tmp:
            client = Path(tmp) / "c.toml"
            client.write_text(pin)
            rows, stop = rc.version_gate(
                Rcon(),
                "1.55.3 commit abc",
                "- blockfield 1.55.3",
                client,
                Path(tmp) / "no-such-server",
            )
        self.assertTrue(stop)
        self.assertTrue(any(not ok for _, ok, _ in rows))

    def test_has_gun_reads_held_then_inventory(self):
        rcon = FakeRcon()

        def bot_with(state):
            return lambda command: dict(state) if command == "state" else {}

        armed = {"name": "Shooter", "held": "tacz:modern_kinetic_gun", "slot": 0}
        self.assertTrue(rc.has_gun(rcon, bot_with(armed)))
        kitless = {"name": "Kitless", "held": "blockfield:command_tablet", "slot": 8}
        self.assertFalse(rc.has_gun(rcon, bot_with(kitless)))
        rcon.cmd = lambda command: "container has 2" if "if items" in command else "ok"
        self.assertTrue(rc.has_gun(rcon, bot_with(kitless)))

    def test_duo_face_off_tries_next_lane_when_walled(self):
        """A walled first lane spends ammo with no HP move; the open lane passes."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        hits = {"count": 0}

        class Shooter:
            def __call__(self, command):
                if command == "state":
                    return {
                        "name": "Shooter",
                        "slot": 0,
                        "pos": [8.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                    }
                if command == "slot 0":
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                assert command.startswith("aimshoot ")
                hits["count"] += 1
                rcon.ammo -= 1
                return {"result": "SUCCESS"}

        class Victim:
            def __call__(self, command):
                assert command == "state"
                # Only the second lane (west offset) has line of sight.
                hp = 20.0 if hits["count"] < 6 else 19.0
                return {
                    "name": "Victim",
                    "pos": [0.0, 82.0, 0.0],
                    "dimension": "blockfield:hamburg_1",
                    "health": hp,
                }

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = duo.face_off(rcon, Shooter(), Victim())
        self.assertTrue(ok, detail)
        self.assertIn("lane (-8,0)", detail)
        tps = [c for c in rcon.commands if " run tp Shooter" in c]
        self.assertEqual(len(tps), 2)

    def test_duo_face_off_fails_after_all_lanes_walled(self):
        """Four walled lanes spend the mag with no HP move: fail, with the lane log."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        shots = {"count": 0}

        def bot(name):
            def call(command):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                if command == "slot 0":
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                assert command.startswith("aimshoot ")
                shots["count"] += 1
                rcon.ammo -= 1
                return {"result": "SUCCESS"}

            return call

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = duo.face_off(rcon, bot("Shooter"), bot("Victim"))
        self.assertFalse(ok)
        self.assertIn("4 lanes", detail)
        self.assertEqual(shots["count"], 16)
        tps = [c for c in rcon.commands if " run tp Shooter" in c]
        self.assertEqual(len(tps), 4)

    def test_duo_face_off_skips_bounds_shortened_lane(self):
        """A lane the map rect shortens below the requested range is never
        shot short: the (+8,0) lane off a victim one block from the upper X
        boundary clamps to ~4.5 m and is skipped, the (-8,0) lane passes."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        shots = {"count": 0}

        class Shooter:
            def __call__(self, command):
                if command == "state":
                    return {
                        "name": "Shooter",
                        "slot": 0,
                        "pos": [8.0, 82.0, 0.0],
                        "dimension": "blockfield:dm_arena_1",
                    }
                if command == "slot 0":
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                assert command.startswith("aimshoot ")
                shots["count"] += 1
                rcon.ammo -= 1
                return {"result": "SUCCESS"}

        class Victim:
            def __call__(self, command):
                assert command == "state"
                hp = 20.0 if shots["count"] < 1 else 19.0
                return {
                    "name": "Victim",
                    "pos": [28.5, 82.0, 0.0],
                    "dimension": "blockfield:dm_arena_1",
                    "health": hp,
                }

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = duo.face_off(
                rcon, Shooter(), Victim(), bounds=(0.0, 32.0, -32.0, 32.0)
            )
        self.assertTrue(ok, detail)
        self.assertIn("lane (-8,0)", detail)
        tps = [c for c in rcon.commands if " run tp Shooter" in c]
        self.assertEqual(len(tps), 1)

    def test_duo_face_off_fails_when_every_lane_is_shortened(self):
        """The review's +60 case: victim one block from the upper X boundary
        of a small rect, so every lane clamps far below range. Nothing is
        shot short; the detail says each lane was skipped."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        shots = {"count": 0}

        def bot(name):
            def call(command):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "pos": [28.5, 82.0, 0.0],
                        "dimension": "blockfield:dm_arena_1",
                        "health": 20.0,
                    }
                if command == "slot 0":
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                assert command.startswith("aimshoot ")
                shots["count"] += 1
                rcon.ammo -= 1
                return {"result": "SUCCESS"}

            return call

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = duo.face_off(
                rcon,
                bot("Shooter"),
                bot("Victim"),
                distance=60,
                bounds=(0.0, 32.0, -32.0, 32.0),
            )
        self.assertFalse(ok)
        self.assertIn("shortened, skipped", detail)
        self.assertEqual(shots["count"], 0)

    def test_duo_face_off_refuses_a_dry_mag(self):
        """Almost no ammo left: fail as not-loaded without firing a shot."""

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return "entity has the following entity data: 5"
                return "ok"

        fired = []

        def bot(name):
            def call(command):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                fired.append(command)
                return {"result": "SUCCESS"}

            return call

        with patch("time.sleep"):
            ok, detail = duo.face_off(Rcon(), bot("Shooter"), bot("Victim"))
        self.assertFalse(ok)
        self.assertIn("not loaded", detail)
        self.assertNotIn("aimshoot", " ".join(fired))

    def test_duo_skips_without_a_second_client(self):
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
        )

        class FakeBot:
            def __call__(self, command):
                return {"name": "Solo"}

            def close(self):
                pass

        def factory(port, *_, **__):
            if port == 47778:
                raise OSError("refused")
            return FakeBot()

        with patch("builtins.print"):
            # A solo run is an invalid setup, never a pass.
            self.assertEqual(
                duo.run(args, rcon_factory=lambda *_: FakeRcon(), bot_factory=factory),
                readiness.INCONCLUSIVE,
            )

    def test_duo_run_waits_out_spawn_protection(self):
        """The 5 s deploy shield is still active when the kit lands, so the
        smoke must wait it out like the RC long-range leg does; a single
        fail-closed probe reads INCONCLUSIVE on a healthy stand (1.59.1)."""
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
        )

        class FakeBot:
            def __call__(self, command):
                return {"name": "Bot"}

            def close(self):
                pass

        seen = {}
        with (
            patch("builtins.print"),
            patch.object(duo, "deploy", return_value=("1", "blockfield:hamburg_1")),
            patch.object(duo, "wait_arena"),
            patch.object(duo, "kit_or_raise"),
            patch.object(
                duo,
                "face_off",
                side_effect=lambda *a, **k: (
                    seen.update(k) or (True, "lane (8,0): open")
                ),
            ),
        ):
            self.assertEqual(
                duo.run(
                    args,
                    rcon_factory=lambda *_: ZeroSpreadRcon(),
                    bot_factory=lambda *_: FakeBot(),
                    get_log=lambda: "",
                ),
                0,
            )
        self.assertGreater(seen.get("protection_timeout", 0), 0)

    def test_duo_run_equips_zero_spread_gun(self):
        """Hip spread (7 for the kit gun) misses by design at lane range,
        so duo lanes shoot the stand-only zero-spread gun the hitreg/hud
        legs equip the same way."""
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
        )

        class FakeBot:
            def __call__(self, command):
                return {"name": "Bot"}

            def close(self):
                pass

        rcon = ZeroSpreadRcon()
        with (
            patch("builtins.print"),
            patch("time.sleep"),
            patch.object(duo, "deploy", return_value=("1", "blockfield:hamburg_1")),
            patch.object(duo, "wait_arena"),
            patch.object(duo, "kit_or_raise"),
            patch.object(duo, "face_off", return_value=(True, "lane (8,0): open")),
        ):
            self.assertEqual(
                duo.run(
                    args,
                    rcon_factory=lambda *_: rcon,
                    bot_factory=lambda *_: FakeBot(),
                    get_log=lambda: "",
                ),
                0,
            )
        replaced = [c for c in rcon.commands if "item replace" in c]
        self.assertEqual(len(replaced), 1)
        self.assertIn(hud_checks.ZERO_SPREAD, replaced[0])

    def test_duo_run_fails_when_admin_gun_missing(self):
        """A refused gun swap must fail loudly, never silently shoot the
        hip-spread kit gun and blame the product."""
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
        )

        class FakeBot:
            def __call__(self, command):
                return {"name": "Bot"}

            def close(self):
                pass

        class KitRcon(FakeRcon):
            def cmd(self, command):
                if "GunId" in command:
                    return "Bot has the following entity data: tacz:modern_kinetic_gun"
                return super().cmd(command)

        with (
            patch("builtins.print"),
            patch("time.sleep"),
            patch.object(duo, "deploy", return_value=("1", "blockfield:hamburg_1")),
            patch.object(duo, "wait_arena"),
            patch.object(duo, "kit_or_raise"),
            patch.object(
                duo,
                "face_off",
                side_effect=AssertionError("must not shoot the kit gun"),
            ),
        ):
            self.assertEqual(
                duo.run(
                    args,
                    rcon_factory=lambda *_: KitRcon(),
                    bot_factory=lambda *_: FakeBot(),
                    get_log=lambda: "",
                ),
                1,
            )

    def test_record_format_matches_release_gate(self):
        sys.path.insert(0, str(SCRIPTS.parent))
        import release  # noqa: E402 - gate lives alongside the dev directory.

        self.assertEqual(rc.RC_RECORD_FORMAT, release.RC_RECORD_FORMAT)

    def test_release_gate_accepts_record_rc_writes(self):
        sys.path.insert(0, str(SCRIPTS.parent))
        import release  # noqa: E402 - gate lives alongside the dev directory.

        tags = {"server": "v9.9.9"}
        rows = [
            ("versions loaded match pins", True, "server 1.57.0/1.57.0"),
            ("first-shot after deploy", True, "ammo -1, SUCCESS"),
            ("long-range kill hamburg", None, "no second client"),
            ("tdm death/respawn at BASE", None, "no second client"),
            ("log scan clean", True, "no errors"),
        ]
        record = rc.build_record(tags, rows)
        with patch.object(release, "say"):
            levels = [level for level, _ in release.check_rc_record(tags, record)]
        self.assertEqual(levels, ["ok"] * len(rows))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rc.json"
            path.write_text(json.dumps(record))
            with patch.object(release, "say"):
                loaded = release.load_rc_record(path)
            self.assertEqual(loaded["releases"], tags)

    def test_long_range_searches_lanes_instead_of_fixed_offset(self):
        """A walled +60 lane must not fail the leg when another lane is open.

        The battery reaches this leg with the command tablet in hand (slot 8,
        left by the lobby leg); the gun must be selected before the mag check,
        or the server has no ammo component to read.
        """

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    if "{Slot:0b}" not in command:
                        return "Found no elements matching Inventory"
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        hits = {"count": 0}
        slot = {"held": 8}

        aims = []

        def bot(name):
            def call(command):
                if command == "state":
                    hp = 20.0 if hits["count"] < 6 else 19.0
                    return {
                        "name": name,
                        "slot": slot["held"],
                        "held": "tacz:modern_kinetic_gun",
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": hp,
                    }
                if command == "slot 0":
                    slot["held"] = 0
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                if command.startswith("tacz aim "):
                    aims.append(command)
                    return {"ok": True}
                assert command.startswith("aimshoot "), command
                hits["count"] += 1
                rcon.ammo -= 1
                return {"result": "SUCCESS"}

            return call

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = rc.long_range(
                rcon, bot("Shooter"), bot("Victim"), "blockfield:hamburg_3"
            )
        self.assertTrue(ok, detail)
        self.assertIn("lane (-60,0)", detail)
        tps = [c for c in rcon.commands if " run tp Shooter" in c]
        self.assertGreater(len(tps), 1)
        # Range lanes fire aimed (hip spread misses at 60 m) and release after.
        self.assertEqual(aims[0], "tacz aim true")
        self.assertEqual(aims[-1], "tacz aim false")

    def test_long_range_repicks_class_when_kit_vanishes(self):
        """The kit can go missing between the grant wait and the first lane
        (a round end wipes it, RC final4 long-range FAIL): one class re-pick
        before the lane search, then a residual FAIL is a real kit defect."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                if "container.* tacz:modern_kinetic_gun" in command:
                    return (
                        "Found no elements matching Inventory"
                        if not kit["gun"]
                        else "1"
                    )
                return "ok"

        kit = {"gun": False}
        picks = []

        aims = []

        def bot(name):
            def call(command):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun" if kit["gun"] else "tablet",
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                if command == "slot 0":
                    return {}
                if command == "bfaction class:assault:0":
                    picks.append(name)
                    kit["gun"] = True
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                if command.startswith("tacz aim "):
                    aims.append(command)
                    return {"ok": True}
                assert command.startswith("aimshoot "), command
                return {"result": "SUCCESS"}

            return call

        rcon = Rcon()
        with (
            patch("time.sleep"),
            patch.object(
                duo, "face_off", return_value=(True, "lane (-60,0): open")
            ) as face_off,
        ):
            ok, detail = rc.long_range(
                rcon, bot("Shooter"), bot("Victim"), "blockfield:hamburg_3"
            )
        self.assertTrue(ok, detail)
        self.assertEqual(picks, ["Shooter"])
        face_off.assert_called_once()

    def test_long_range_passes_bounds_to_face_off(self):
        """The RC lane clamps inside the map rect like the HUD duel does
        (RC final5 long-range FAIL: lane (0,+60) parked the shooter outside
        the battle boundary -> genericKill, kit wiped)."""

        positions = {"Shooter": [0.0, 82.0, 0.0], "Victim": [0.0, 82.0, 0.0]}

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                parts = command.split()
                if parts[:2] == ["execute", "in"] and parts[3:5] == ["run", "tp"]:
                    positions[parts[5]] = [float(p) for p in parts[6:9]]
                return "ok"

        def bot(name):
            def call(command):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "pos": list(positions[name]),
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                return {}

            return call

        bounds = (-222, 57, -52, 110)
        rcon = Rcon()
        with (
            patch("time.sleep"),
            patch.object(
                duo, "face_off", return_value=(True, "lane clamped: open")
            ) as face_off,
        ):
            ok, detail = rc.long_range(
                rcon,
                bot("Shooter"),
                bot("Victim"),
                "blockfield:hamburg_3",
                bounds=bounds,
            )
        self.assertTrue(ok, detail)
        face_off.assert_called_once()
        self.assertEqual(face_off.call_args.kwargs.get("bounds"), bounds)
        self.assertEqual(face_off.call_args.kwargs.get("protection_timeout"), 120)
        # With bounds the victim is parked in a corner first, so the inward
        # lanes fit the full 60 m instead of clamping below range.
        tps = [c for c in rcon.commands if " run tp Victim " in c]
        self.assertIn(
            "execute in blockfield:hamburg_3 run tp Victim -220 82.0 -50 0 0",
            tps,
        )

    def test_long_range_waits_for_corner_before_face_off(self):
        """face_off anchors every lane on the victim position it reads first
        (and re-teleports the victim there per lane): starting before the
        corner teleport lands re-centers the victim and skips every 60 m
        lane, so long_range waits until bot2 reports the corner."""

        state = {"pos": [0.0, 82.0, 0.0], "target": None, "reads": 0}

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                parts = command.split()
                if parts[:2] == ["execute", "in"] and parts[3:5] == ["run", "tp"]:
                    if parts[5] == "Victim":
                        # The teleport packet takes a few client ticks to
                        # land: the first reads still report the old spot.
                        state["target"] = [float(p) for p in parts[6:9]]
                return "ok"

        def bot(name):
            def call(command):
                if command == "state" and name == "Victim" and state["target"]:
                    state["reads"] += 1
                    if state["reads"] >= 3:
                        state["pos"] = state["target"]
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "pos": list(state["pos"]),
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                return {}

            return call

        seen = {}

        def face_off(rcon, shooter, victim, **kwargs):
            seen["pos"] = victim("state")["pos"]
            return True, "lane (60,0): open"

        bounds = (-222, 57, -52, 110)
        rcon = Rcon()
        with (
            patch("time.sleep"),
            patch.object(duo, "face_off", side_effect=face_off),
        ):
            ok, detail = rc.long_range(
                rcon,
                bot("Shooter"),
                bot("Victim"),
                "blockfield:hamburg_3",
                bounds=bounds,
            )
        self.assertTrue(ok, detail)
        self.assertEqual(seen["pos"], [-220, 82.0, -50])

    def test_long_range_fails_when_corner_never_settles(self):
        """The corner teleport can stay unprocessed (a teleport back there
        would re-center the victim and skip every lane): on timeout the leg
        fails with that reason instead of running the lane search."""

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "ok"

        def bot(name):
            def call(command):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                return {}

            return call

        bounds = (-222, 57, -52, 110)
        rcon = Rcon()
        with (
            patch("time.sleep"),
            patch("time.monotonic", side_effect=itertools.count(0, 10)),
            patch.object(
                duo, "face_off", return_value=(True, "lane (60,0): open")
            ) as face_off,
        ):
            ok, detail = rc.long_range(
                rcon,
                bot("Shooter"),
                bot("Victim"),
                "blockfield:hamburg_3",
                bounds=bounds,
            )
        self.assertFalse(ok)
        self.assertIn("corner", detail)
        face_off.assert_not_called()

    def test_long_range_searches_diagonals_after_orthogonal_lanes_walled(self):
        """Hamburg blocks every orthogonal lane at range while the same gun
        kills in the open dm_arena (rc-triage): the four diagonals join the
        search, orthogonal first. Eight lanes times four shots does not fit
        the kit mag (30), hence three shots per lane."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        hits = {"count": 0}

        aims = []

        def bot(name):
            def call(command):
                if command == "state":
                    # Only the first diagonal lane moves HP (12 walled
                    # orthogonal shots, then the diagonal kills).
                    hp = 20.0 if hits["count"] < 14 else 19.0
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": hp,
                    }
                if command == "slot 0":
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                if command.startswith("tacz aim "):
                    aims.append(command)
                    return {"ok": True}
                assert command.startswith("aimshoot "), command
                hits["count"] += 1
                rcon.ammo -= 1
                return {"result": "SUCCESS"}

            return call

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = rc.long_range(
                rcon, bot("Shooter"), bot("Victim"), "blockfield:hamburg_3"
            )
        self.assertTrue(ok, detail)
        tps = [c for c in rcon.commands if " run tp Shooter" in c]
        self.assertEqual(len(tps), 5)

    def test_tdm_respawn_refuses_kitless_victim(self):
        """A victim that never deployed (no class/kit) fails fast instead of
        burning the whole timeout on invulnerable knife probes (RC final5
        tdm FAIL: 60 s of probes against a never-damageable victim)."""

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "ok"

        held = {"Shooter": "tacz:modern_kinetic_gun", "Victim": "tablet"}

        def bot(name):
            def call(command, *args):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": held[name],
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_7",
                        "health": 20.0,
                    }
                return {}

            return call

        rcon = Rcon()
        clock = {"t": 0.0}

        def fast_clock():
            clock["t"] += 10.0
            return clock["t"]

        with (
            patch("time.sleep"),
            patch.object(rc.time, "monotonic", side_effect=fast_clock),
        ):
            ok, detail = rc.tdm_respawn(
                rcon,
                bot("Shooter"),
                bot("Victim"),
                "blockfield:hamburg_7",
                get_log=lambda: "",
                timeout=60,
            )
        self.assertFalse(ok)
        self.assertIn("never equipped", detail)
        self.assertFalse(
            [c for c in rcon.commands if "run damage" in c],
            "kitless victim must fail before any knife probe",
        )

    def test_tdm_respawn_waits_out_slow_kit_grant(self):
        """The TDM kit lands a minute after the room opens on a slow stand
        (rc-triage: single spawn plus 30 s kit_or_raise never equipped either
        bot, repeated spawns equipped both near the minute mark with no
        re-pick and no kill). Keep offering spawn:BASE until both bots hold
        the kit gun instead of failing on the first quiet stretch."""

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "ok"

        clock = {"t": 0.0}
        killed = {"done": False}

        def fast_clock():
            # Small steps: the 5 s equip polls must get several polls in.
            clock["t"] += 2.0
            return clock["t"]

        def bot(name):
            def call(command, *args):
                if command == "state":
                    # Kits land 100 s after the room opens: after the old
                    # 30 s fast path, only the fallback loop gets there.
                    armed = clock["t"] >= 100.0
                    return {
                        "name": name,
                        "slot": 0,
                        "held": (
                            "tacz:modern_kinetic_gun"
                            if armed
                            else "blockfield:command_tablet"
                        ),
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_7",
                        "health": 20.0,
                    }
                if command == "bfaction kill":
                    killed["done"] = True
                return {}

            return call

        def log():
            return "Victim убит Shooter" if killed["done"] else ""

        rcon = Rcon()
        with (
            patch("time.sleep"),
            patch.object(rc.time, "monotonic", side_effect=fast_clock),
        ):
            ok, detail = rc.tdm_respawn(
                rcon,
                bot("Shooter"),
                bot("Victim"),
                "blockfield:hamburg_7",
                get_log=log,
                timeout=120,
            )
        # The leg proceeds past the slow equip into the kill and redeploy.
        self.assertTrue(ok, detail)

    def test_face_off_reports_gun_not_loaded_when_kitless(self):
        """A vanished kit at leg start reads as a FAIL row, never a crash
        (RC final4 long-range FAIL: AssertionError out of the mag read)."""

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "Found no elements matching Inventory[{Slot:0b}]"

        def bot(name):
            def call(command, *args):
                assert command == "state"
                return {
                    "name": name,
                    "slot": 0,
                    "pos": [0.0, 82.0, 0.0],
                    "dimension": "blockfield:hamburg_1",
                    "health": 20.0,
                }

            return call

        with patch("time.sleep"):
            ok, detail = duo.face_off(Rcon(), bot("Shooter"), bot("Victim"), distance=8)
        self.assertFalse(ok)
        self.assertIn("gun not loaded", detail)

    def test_face_off_lane_survives_mid_lane_kit_loss(self):
        """A kit wiped mid-leg fails its lane, not the scenario: later lanes
        still run and the leg reports instead of raising."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    if self.ammo < 0:
                        return "Found no elements matching Inventory[{Slot:0b}]"
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        aims = []

        def bot(name):
            def call(command, *args):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "pos": [0.0, 82.0, 0.0],
                        "dimension": "blockfield:hamburg_1",
                        "health": 20.0,
                    }
                if command == "slot 0":
                    return {}
                if command.startswith("entities"):
                    return {"entities": [{"type": "player"}]}
                if command.startswith("tacz aim "):
                    aims.append(command)
                    return {"ok": True}
                assert command.startswith("aimshoot "), command
                rcon.ammo -= 100
                return {"result": "SUCCESS"}

            return call

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = duo.face_off(
                rcon, bot("Shooter"), bot("Victim"), distance=8, shots_per_lane=1
            )
        self.assertFalse(ok)
        self.assertIn("no kill in 4 lanes", detail)
        self.assertIn("lane lost", detail)

    def test_wait_lobby_needs_both_bots_in_lobby(self):
        lobby = lambda command: (
            {"dimension": "minecraft:overworld"} if command == "state" else {}
        )
        arena = lambda command: (
            {"dimension": "blockfield:hamburg_1"} if command == "state" else {}
        )
        self.assertTrue(rc.wait_lobby(lobby, lobby, timeout=5))
        clock = iter(range(100))
        with (
            patch.object(rc.time, "sleep"),
            patch.object(rc.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            self.assertFalse(rc.wait_lobby(lobby, arena, timeout=5))

    def test_run_uses_tdm_map_and_independent_still_hitreg(self):
        """tdm deploys the battery map; every duo leg opens its own live room."""
        from scenarios import check_gameplay  # noqa: E402

        deploys, commands, setups, shots, arrivals = [], [], [], [], []
        dims = {"Bot1": "minecraft:overworld", "Bot2": "minecraft:overworld"}
        positions = {"Bot1": [0.0, 82.0, 0.0], "Bot2": [0.0, 82.0, 0.0]}
        spawned, arena_of, kits = set(), {"room": "blockfield:hamburg_7"}, {"n": 0}

        def fake_bot(name):
            def call(command, timeout=None):
                commands.append((name, command))
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "pos": list(positions[name]),
                        "dimension": dims[name],
                        "health": 20.0,
                    }
                if command == "bfaction leave":
                    dims[name] = "minecraft:overworld"
                    spawned.discard(name)
                elif command == "bfaction spawn:BASE":
                    spawned.add(name)
                    dims[name] = arena_of["room"]
                elif command == "bfaction kill":
                    # Unspawned players cannot die: the product no-ops.
                    if name in spawned:
                        dims[name] = "blockfield_shared:respawn"
                return {}

            call.close = lambda: None
            return call

        def factory(port, *_, **__):
            return fake_bot("Bot1" if port == 47777 else "Bot2")

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                if command == "save-all":
                    return "Saving the game (this may take a moment!)Saved"
                # Teleports move the named bot like the server would (the
                # long-range leg waits for the victim's corner teleport).
                parts = command.split()
                if parts[:2] == ["execute", "in"] and parts[3:5] == ["run", "tp"]:
                    positions[parts[5]] = [float(p) for p in parts[6:9]]
                return "ok"

        def fake_deploy(bot, get_log, mode, map_id, bot2=None, timeout=60):
            deploys.append((mode, map_id))
            arena_of["room"] = arena = f"blockfield:{map_id}_7"
            dims["Bot1"] = arena
            if bot2 is not None:
                dims["Bot2"] = arena
            spawned.clear()
            return ("7", arena)

        def fake_kit(bot, timeout=30):
            kits["n"] += 1
            if kits["n"] == 1:
                raise TimeoutError("t")
            return None

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "blockfield" / "maps").mkdir(parents=True)
            (Path(tmp) / "blockfield" / "maps" / "hamburg.json").write_text(
                json.dumps(
                    {
                        "bounds": {
                            "minX": -222,
                            "maxX": 57,
                            "minZ": -52,
                            "maxZ": 110,
                        }
                    }
                )
            )
            (Path(tmp) / "blockfield" / "maps" / "dm_arena.json").write_text(
                json.dumps(
                    {"bounds": {"minX": -32, "maxX": 31, "minZ": -32, "maxZ": 31}}
                )
            )
            args = SimpleNamespace(
                rcon_host="localhost",
                rcon_port=25575,
                rcon_password="test",
                bot_port=47777,
                bot_port2=47778,
                map="hamburg",
                hitreg_shots=3,
                game_dir=tmp,
                record=str(Path(tmp) / "rc.json"),
                tag_client="v2.79.1",
                tag_server="v2.79.4",
            )
            with (
                patch.object(
                    rc,
                    "version_gate",
                    return_value=([("versions loaded match pins", True, "x")], False),
                ),
                patch.object(rc, "deploy", side_effect=fake_deploy),
                patch.object(duo, "kit_or_raise", side_effect=fake_kit),
                patch.object(check_gameplay, "shot", return_value=None),
                patch.object(check_gameplay, "lobby", return_value=None),
                patch.object(
                    duo, "face_off", return_value=(True, "lane (-60,0): open")
                ) as face_off,
                patch.object(
                    duo,
                    "wait_arena",
                    side_effect=lambda bot, arena: arrivals.append(arena),
                ),
                patch.object(
                    hitreg,
                    "setup",
                    side_effect=lambda rcon, dim, room, client: setups.append(
                        (dim, room, client)
                    ),
                ),
                patch.object(
                    hitreg,
                    "run_shots",
                    side_effect=lambda rcon, bot, n, motion, mode: shots.append(
                        (n, motion, mode)
                    ),
                ),
                patch("time.sleep"),
                patch("builtins.print"),
            ):
                self.assertEqual(
                    rc.run(
                        args,
                        rcon_factory=lambda *_: Rcon(),
                        bot_factory=factory,
                        get_log=lambda: "",
                        server_dir=tmp,
                    ),
                    0,
                )
            # The long-range lane search runs inside its own open-map rect:
            # hamburg blocks every lane at range, so the leg probes dm_arena
            # (an unclamped 60 m lane parks the shooter outside the battle
            # boundary (genericKill, kit wiped)).
            face_off.assert_called_once()
            self.assertEqual(
                face_off.call_args.kwargs.get("bounds"), (-32, 31, -32, 31)
            )
            # First-shot retried the class pick once after the kit timeout.
            self.assertIn(("Bot1", "bfaction class:assault:0"), commands)
            # TDM runs on the battery map: dm_arena is deathmatch-only.
            self.assertNotIn(("tdm", "dm_arena"), deploys)
            # Every duo leg opens its own room: first-shot, long-range
            # (open map), tdm, hitreg smoke. A stale arena string would aim
            # the lane search at the lobby the tablet leg leaves the bots in.
            self.assertEqual(
                deploys,
                [
                    ("deathmatch", "hamburg"),
                    ("deathmatch", "dm_arena"),
                    ("tdm", "hamburg"),
                    ("deathmatch", "hamburg"),
                ],
            )
            # The trailer was awaited in its arena before each kill leg, and
            # the kit grant (GAME start) before every leg that reads it.
            # The long-range leg re-picks once: the run fake never holds the
            # gun, so kit_or_raise runs there too.
            self.assertEqual(
                arrivals,
                [
                    "blockfield:dm_arena_7",
                    "blockfield:hamburg_7",
                    "blockfield:hamburg_7",
                ],
            )
            self.assertEqual(kits["n"], 7)
            # TDM spawns before killing: an unspawned victim never respawns.
            self.assertIn(("Bot2", "bfaction spawn:BASE"), commands)
            # Hitreg opened its own deathmatch after the tdm leg, still target.
            self.assertEqual(deploys[-1], ("deathmatch", "hamburg"))
            self.assertEqual(
                [(room, client) for _, room, client in setups], [("7", "Bot1")]
            )
            self.assertEqual(shots, [(3, "still", "track")])
            # Gun selected before each lane read: first-shot, long-range
            # pre-check, hitreg smoke.
            self.assertEqual(commands.count(("Bot1", "slot 0")), 3)
            record = json.loads((Path(tmp) / "rc.json").read_text())
            self.assertTrue(
                all(row["result"] == "PASS" for row in record["checks"]), record
            )


class KnifeAcceptance(unittest.TestCase):
    ARENA = "blockfield:hamburg_3"

    class Bot:
        def __init__(self, test, name, pos, hp=20.0):
            self._test = test
            self.name = name
            self.pos = list(pos)
            self.hp = hp
            self.dim = test.ARENA
            self.held = knife.MELEE
            self.screen = ""
            self.deployed_flag = True
            self.spawn_offers = 0
            self.spawn_after = 1
            self.has_kit = True
            self.recover_on_class = False
            self.class_repicks = 0
            self.commands = []
            self.on_attack = lambda side: None
            self.attack_reply = {"accepted": True}
            self.tick = 0

        def __call__(self, command, *args, **kwargs):
            self.commands.append(command)
            if command == "state":
                self.tick += 1
                return {
                    "tick": self.tick,
                    "name": self.name,
                    "pos": list(self.pos),
                    "dimension": self.dim,
                    "health": self.hp,
                    "held": self.held,
                    "screen": self.screen,
                    "onGround": True,
                }
            if command == "markers":
                return {"deployed": self.deployed_flag}
            if command == "slot 2":
                self.held = knife.MELEE
            elif command == "slot 0":
                self.held = duo.GUN if self.has_kit else "minecraft:air"
            elif command == "bfaction class:assault:0":
                self.class_repicks += 1
                if self.recover_on_class:
                    self.has_kit = True
                    self.held = duo.GUN
            elif command == "respawn":
                self.hp = 20.0
                self.screen = ""
            elif command == "bfaction spawn:BASE":
                self.spawn_offers += 1
                if (
                    self.hp > 0
                    and self.screen != "DeathScreen"
                    and self.spawn_offers >= self.spawn_after
                ):
                    self.dim = self._test.ARENA
                    self.deployed_flag = True
                    self.has_kit = True
                    self.held = duo.GUN
            elif command.startswith("lr attack "):
                self.on_attack(command.split()[-1])
                return {"attack": command.split()[-1], **self.attack_reply}
            return {}

        def close(self):
            pass

    class Rcon(FakeRcon):
        def __init__(self, test):
            super().__init__()
            self._test = test
            self.probes = 0
            self.invulnerable_first = 0

        def cmd(self, command):
            self.commands.append(command)
            if command.endswith(" HurtTime"):
                return "entity has the following entity data: 0s"
            parts = command.split()
            if command.endswith(" get entity Bob Health"):
                return f"Bob has the following entity data: {self._test.victim.hp}f"
            # RCON 1-HP vulnerability probe: chips the victim like live.
            if " run damage " in command and "blockfield:knife" in command:
                self.probes += 1
                if self.probes <= self.invulnerable_first:
                    return "That target is invulnerable (spawn protection)"
                self._test.victim.hp -= 1.0
                return "Applied 1.0 damage to Bob"
            # Teleports move the named bot like the server would.
            if parts[:2] == ["execute", "in"] and parts[3:5] == ["run", "tp"]:
                name, x, y, z = (
                    parts[5],
                    float(parts[6]),
                    float(parts[7]),
                    float(parts[8]),
                )
                for bot in (self._test.shooter, self._test.victim):
                    if bot.name == name:
                        bot.pos = [x, y, z]
                return "ok"
            return "ok"

    def setUp(self):
        self.shooter = self.Bot(self, "Alice", [12.0, 82.0, 5.0])
        self.victim = self.Bot(self, "Bob", [10.0, 82.0, 5.0])
        self.rcon = self.Rcon(self)
        self.bounds = (0.0, 100.0, 0.0, 100.0)
        patches = [
            patch("time.sleep"),
            patch.object(duo, "rendered", return_value=True),
            patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def leg(self, key, side, dist, aim_off, expect_hit, nominal, logs=("",)):
        # leg() reads the log up to six times per attempt (probe mark +
        # probe-death scan + readiness snapshot + strike mark + strike scan +
        # post-read life bracket), and a strike now polls the log until its
        # effect resolves; pad so short fixtures never exhaust it.
        with patch.object(knife, "get_log", side_effect=list(logs) + [""] * 64):
            return knife.leg(
                self.shooter,
                self.victim,
                self.rcon,
                self.ARENA,
                self.bounds,
                key,
                side,
                dist,
                aim_off,
                expect_hit,
                nominal,
            )

    def test_strike_baseline_uses_server_hp_after_probe(self):
        self.victim.hp = 19.0
        victim = self.victim
        reads = {"n": 0}

        def delayed(command):
            state = victim(command)
            if command == "state":
                reads["n"] += 1
                if reads["n"] <= 1:
                    return {**state, "health": 20.0}
            return state

        with patch.object(knife, "get_log", return_value=""):
            _, before, after, _, _, _, _, _ = knife.strike(
                self.shooter, delayed, self.rcon, self.ARENA, "left"
            )
        self.assertEqual(before, 19.0)
        self.assertEqual(after, 19.0)

    def test_hit_leg_passes_on_same_life_hp_drop(self):
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 12.0)
        ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7)
        self.assertTrue(ok)
        self.assertIn("lr attack left", self.shooter.commands)

    def test_probe_knockback_does_not_change_strike_geometry(self):
        original = self.rcon.cmd

        def knockback(command):
            reply = original(command)
            if " run damage " in command:
                self.victim.pos[0] -= 0.96
                self.victim.pos[1] += 0.3
            return reply

        self.rcon.cmd = knockback

        def strike(side):
            self.assertAlmostEqual(self.shooter.pos[0] - self.victim.pos[0], 2.0)
            self.assertEqual(self.shooter.pos[1], self.victim.pos[1])
            self.victim.hp -= 7

        self.shooter.on_attack = strike
        self.assertTrue(self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7))

    def test_lethal_hit_counts_via_kill_line_despite_respawn(self):
        # rc-triage misread: a lethal hit plus the instant respawn reads HP
        # 20->20, so the kill line (not the delta) decides lethals.
        self.victim.hp = 20.0
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 20.0)
        kill = "Комната 3: Bob убит Alice (player)\n"
        ok = self.leg(
            "thrust-ahead-2m", "right", 2.0, 0, True, 13, logs=("", "", "", "", kill)
        )
        self.assertTrue(ok)

    def test_strike_unreadable_state_reports_unknown(self):
        # A server-health timeout after the baseline must not fabricate
        # damage: hp_after comes back None instead of 0.0.
        reads = {"n": 0}

        class FlakyRcon(self.Rcon):
            def cmd(inner, command):
                if command.endswith(" get entity Bob Health"):
                    reads["n"] += 1
                    if reads["n"] >= 2:
                        raise OSError("timeout")
                return super().cmd(command)

        with patch.object(knife, "get_log", return_value=""):
            killed, hp_before, hp_after, clip, rejected, _, _, _ = knife.strike(
                self.shooter, self.victim, FlakyRcon(self), self.ARENA, "left"
            )
        self.assertFalse(killed)
        self.assertEqual(hp_before, 20.0)
        self.assertIsNone(hp_after)
        self.assertFalse(rejected)

    def test_observed_drop_survives_a_failed_final_read(self):
        # The poll saw 20 -> 13; the next RCON read times out. The hit stands.
        class DropThenTimeout(self.Rcon):
            n = 0

            def cmd(inner, command):
                if command.endswith(" get entity Bob Health"):
                    DropThenTimeout.n += 1
                    if DropThenTimeout.n >= 3:
                        raise OSError("timeout")
                    hp = 20.0 if DropThenTimeout.n == 1 else 13.0
                    return f"Bob has the following entity data: {hp}f"
                return super().cmd(command)

        self.shooter.on_attack = lambda side: None
        self.rcon = DropThenTimeout(self)
        ok = self.leg("thrust-ahead-2m", "right", 2.0, 0, True, 13)
        self.assertTrue(ok)

    def test_hit_after_regen_is_scored(self):
        # Server HP 13 -> regen 20 -> hit back to 13: the must-hit leg passes.
        class RegenThenHit(self.Rcon):
            n = 0

            def cmd(inner, command):
                if command.endswith(" get entity Bob Health"):
                    RegenThenHit.n += 1
                    hp = (
                        13.0
                        if RegenThenHit.n == 1
                        else 20.0
                        if RegenThenHit.n == 2
                        else 13.0
                    )
                    return f"Bob has the following entity data: {hp}f"
                return super().cmd(command)

        self.shooter.on_attack = lambda side: None
        self.rcon = RegenThenHit(self)
        ok = self.leg("thrust-ahead-2m", "right", 2.0, 0, True, 13)
        self.assertTrue(ok)

    def test_verdict_uses_server_hp_when_client_lags(self):
        # Server registered a nonlethal hit (20 -> 13) but the stalled client
        # still shows 20: the must-hit leg passes on server health.
        class HitRcon(self.Rcon):
            n = 0

            def cmd(inner, command):
                if command.endswith(" get entity Bob Health"):
                    HitRcon.n += 1
                    hp = 20.0 if HitRcon.n == 1 else 13.0
                    return f"Bob has the following entity data: {hp}f"
                return super().cmd(command)

        self.shooter.on_attack = lambda side: None  # client never updates
        self.rcon = HitRcon(self)
        ok = self.leg("thrust-ahead-2m", "right", 2.0, 0, True, 13)
        self.assertTrue(ok)

    def test_leg_with_unreadable_post_strike_state_is_inconclusive(self):
        with patch.object(
            knife, "strike", return_value=(False, 20.0, None, None, False, 0, 0, 0)
        ):
            ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7)
        self.assertIsNone(ok)

    def test_probe_kill_with_instant_respawn_rebuilds_fixture(self):
        # The vuln probe is lethal on a 1-HP victim; the instant respawn
        # already restored full HP (with fresh protection) before the HP
        # read, so the leg must spot the probe kill on the kill line and
        # rebuild the fixture instead of striking a protected victim.
        self.victim.hp = 1.0
        kill = "Комната 3: Bob убит Alice (player)\n"
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 12.0)

        class RespawningRcon(self.Rcon):
            def cmd(inner, command):
                if " run damage " in command and "blockfield:knife" in command:
                    inner.probes += 1
                    self.victim.hp -= 1.0
                    if self.victim.hp <= 0:
                        self.victim.hp = 20.0
                    return "Applied 1.0 damage to Bob"
                return super().cmd(command)

        self.rcon = RespawningRcon(self)
        ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7, logs=("", kill))
        self.assertTrue(ok)
        self.assertGreaterEqual(self.rcon.probes, 2)

    def test_undeployed_victim_in_arena_is_redeployed(self):
        # Instant respawn can leave a healthy victim undeployed in the same
        # arena dimension: dimension alone must not pass the fixture.
        self.victim.deployed_flag = False
        self.assertTrue(knife.redeploy(self.victim, self.ARENA))
        self.assertIn("bfaction spawn:BASE", self.victim.commands)

    def test_redeploy_reoffers_until_deployed_in_same_dimension(self):
        self.victim.deployed_flag = False
        self.victim.spawn_after = 3
        self.assertTrue(knife.redeploy(self.victim, self.ARENA))
        self.assertTrue(self.victim.deployed_flag)
        self.assertEqual(self.victim.spawn_offers, 3)

    def test_redeploy_times_out_on_undeployed_player_in_same_dimension(self):
        self.victim.deployed_flag = False
        self.assertFalse(knife.redeploy(self.victim, self.ARENA, timeout=0))

    def test_redeploy_refuses_stale_deployed_marker_until_fresh_kit(self):
        self.victim.has_kit = False
        self.victim.held = "blockfield:command_tablet"
        self.victim.spawn_after = 3
        self.assertTrue(knife.redeploy(self.victim, self.ARENA))
        self.assertTrue(self.victim.has_kit)
        self.assertEqual(self.victim.held, duo.GUN)
        self.assertEqual(self.victim.spawn_offers, 3)

    def test_redeploy_recovers_wiped_kit_when_spawn_is_noop(self):
        self.shooter.has_kit = False
        self.shooter.recover_on_class = True
        self.shooter.spawn_after = 1_000_000_000
        self.assertTrue(knife.redeploy(self.shooter, self.ARENA, timeout=0.02))
        self.assertEqual(self.shooter.class_repicks, 1)
        self.assertEqual(self.shooter.held, duo.GUN)

    def test_redeploy_revives_dead_kitless_client_before_spawn(self):
        self.victim.hp = 0
        self.victim.screen = "DeathScreen"
        self.victim.has_kit = False
        self.victim.deployed_flag = False
        self.assertTrue(knife.redeploy(self.victim, self.ARENA, timeout=0.02))
        self.assertIn("respawn", self.victim.commands)
        self.assertEqual(self.victim.held, duo.GUN)

    def test_place_repeated_aim_after_tp_reaches_server(self):
        # Vanilla sends rotation only when it differs from its last movement tick.
        # A tp resets the camera without resetting that cache. Replaying the prior
        # leg's exact aim before a tick then leaves the server at the tp's yaw=0.
        prior_aim = (90.0, 11.859779)
        camera, sent, server_aim = [prior_aim], [prior_aim], [prior_aim]
        tick, tp_reads = [0], [None]

        def client_tick(*_):
            if camera[0] != sent[0]:
                server_aim[0] = sent[0] = camera[0]
            tick[0] += 1

        def shooter(command, *args, **kwargs):
            if command == "state":
                if tp_reads[0] is not None:
                    tp_reads[0] += 1
                    if tp_reads[0] > 1:
                        client_tick()
                return {**self.shooter(command), "tick": tick[0]}
            if command.startswith("lookat "):
                camera[0] = prior_aim
                return dict(zip(("yaw", "pitch"), prior_aim))
            return self.shooter(command)

        original = self.rcon.cmd

        def teleport(command):
            reply = original(command)
            if " run tp Alice " in command:
                camera[0] = server_aim[0] = (0.0, 0.0)
                tp_reads[0] = 0
            return reply

        self.rcon.cmd = teleport
        with patch("time.sleep", side_effect=client_tick):
            self.assertTrue(
                knife.place(shooter, self.victim, self.rcon, self.ARENA, 2.0, 0)
            )
        self.assertEqual(server_aim[0], prior_aim)

    def test_place_does_not_aim_when_client_tick_stalls(self):
        with patch.object(knife, "wait_for", return_value=(False, {}, 5)):
            self.assertFalse(
                knife.place(self.shooter, self.victim, self.rcon, self.ARENA, 2.0, 0)
            )
        self.assertFalse(
            any(command.startswith("lookat ") for command in self.shooter.commands)
        )

    def test_place_preserves_separation_at_boundary(self):
        # Victim one block from the upper X boundary: the shooter spot would
        # clamp, so the victim steps in and the 2.9 m separation stays exact
        # instead of testing ~1 m.
        self.victim.pos = [99.5, 82.0, 5.0]
        self.assertTrue(
            knife.place(
                self.shooter,
                self.victim,
                self.rcon,
                self.ARENA,
                2.9,
                0,
                (0.0, 100.0, 0.0, 100.0),
            )
        )
        self.assertAlmostEqual(self.victim.pos[0], 97.1)
        self.assertAlmostEqual(self.shooter.pos[0], 100.0)

    def test_place_brings_probe_displaced_victim_inside_lower_boundary(self):
        self.victim.pos = [-0.5, 82.0, -0.5]
        self.assertTrue(
            knife.place(
                self.shooter, self.victim, self.rcon, self.ARENA, 2.0, 0, self.bounds
            )
        )
        self.assertEqual(self.victim.pos, [1.0, 82.0, 1.0])
        self.assertEqual(self.shooter.pos, [3.0, 82.0, 1.0])

    def test_must_miss_leg_passes_quiet_and_fails_on_damage(self):
        ok = self.leg("swing-ahead-2.9m", "left", 2.9, 0, False, 0)
        self.assertTrue(ok)
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 7.0)
        ok = self.leg("thrust-offaxis-35deg-2m", "right", 2.0, 35, False, 0)
        self.assertFalse(ok)

    def test_rejected_attack_makes_must_miss_leg_inconclusive(self):
        # A swing the server intake refused never executed: unchanged HP is
        # no evidence of a miss, so the leg is inconclusive, never a pass.
        reject = "knife swing rejected: Alice tick=10 next=12\n"
        ok = self.leg(
            "swing-ahead-2.9m", "left", 2.9, 0, False, 0, logs=("", "", "", "", reject)
        )
        self.assertIsNone(ok)

    def test_unconfirmed_attack_makes_must_miss_leg_inconclusive(self):
        self.shooter.attack_reply = {}
        self.assertIsNone(self.leg("swing-ahead-2.9m", "left", 2.9, 0, False, 0))

    def test_refused_attack_reply_makes_leg_inconclusive(self):
        # The `lr attack` reply reports the intake refusal itself: a quiet
        # must-miss leg on a refused swing is inconclusive, never a pass.
        self.shooter.attack_reply = {"accepted": False}
        ok = self.leg("swing-ahead-2.9m", "left", 2.9, 0, False, 0)
        self.assertIsNone(ok)

    def test_must_hit_leg_at_thrust_reach_passes(self):
        # The 2.9 m pair discriminates the reaches at one distance: the
        # thrust lands inside HEAVY 2.8 m plus the eye-ray face margin.
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 7.0)
        ok = self.leg("thrust-ahead-2.9m", "right", 2.9, 0, True, 13)
        self.assertTrue(ok)

    def test_late_impact_kill_is_scored_not_a_miss(self):
        # A stalled stand resolves the impact after the old fixed sleep: the
        # kill line lands after the single scan, so the old window scores a
        # lethal thrust as a miss (mod-rc-1591 thrust-ahead-2m: accepted
        # strike, hp unchanged, no kill line yet). The strike must poll
        # until its effect is visible.
        self.shooter.on_attack = lambda side: None  # frozen server: nothing yet
        kill = "Комната 3: Bob убит Alice (player)\n"
        ok = self.leg(
            "thrust-ahead-2m", "right", 2.0, 0, True, 13, logs=("",) * 6 + (kill,)
        )
        self.assertTrue(ok)

    def test_true_miss_still_fails_after_full_window(self):
        # Fail-closed: no kill line ever, HP flat — polling must not turn a
        # genuine miss into a pass or an inconclusive.
        ok = self.leg("thrust-ahead-2m", "right", 2.0, 0, True, 13)
        self.assertFalse(ok)

    def test_stale_client_hp_does_not_end_the_window_early(self):
        # The client may still show pre-probe health while the server
        # baseline already moved: a change against an unobserved baseline
        # must not end the window, or a delayed strike is judged from stale
        # health instead of its own outcome.
        self.victim.hp = 21.0  # probe chips to a client 20 the server left

        class StaleRcon(self.Rcon):
            def cmd(inner, command):
                if command.endswith(" get entity Bob Health"):
                    return "Bob has the following entity data: 19.0f"
                return super().cmd(command)

        self.rcon = StaleRcon(self)
        kill = "Комната 3: Bob убит Alice (player)\n"
        ok = self.leg(
            "thrust-ahead-2m", "right", 2.0, 0, True, 13, logs=("",) * 6 + (kill,)
        )
        self.assertTrue(ok)

    def test_stale_low_client_hp_does_not_score_before_impact(self):
        # Server regen raised HP to 9 while the stalled client still shows 7:
        # client HP below the server baseline is not damage, so the window
        # must keep polling the server until the delayed kill lands.
        self.victim.hp = 7.0

        class RegenRcon(self.Rcon):
            def cmd(inner, command):
                if command.endswith(" get entity Bob Health"):
                    return "Bob has the following entity data: 9.0f"
                return super().cmd(command)

        self.rcon = RegenRcon(self)
        kill = "Комната 3: Bob убит Alice (player)\n"
        ok = self.leg(
            "thrust-ahead-2m", "right", 2.0, 0, True, 13, logs=("",) * 6 + (kill,)
        )
        self.assertTrue(ok)

    def test_kill_landing_with_hp_move_is_joined_not_inconclusive(self):
        # A lethal strike resolving between the log scan and the health read
        # must score from a fresh scan: the kill line belongs to this
        # attack, not to a respawn crossing.
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 20.0)
        kill = "Комната 3: Bob убит Alice (player)\n"
        ok = self.leg(
            "thrust-ahead-2m", "right", 2.0, 0, True, 13, logs=("",) * 5 + (kill,)
        )
        self.assertTrue(ok)

    def test_regen_before_impact_keeps_observing(self):
        # A regen rise between the baseline and a delayed impact must not
        # end the window: only a drop scores early, so the strike still gets
        # its verdict instead of an inconclusive regen read.
        self.victim.hp = 19.0
        kill = "Комната 3: Bob убит Alice (player)\n"
        reads = {"n": 0}
        victim = self.victim

        def regen_state(command, *args, **kwargs):
            if command == "state":
                reads["n"] += 1
                if reads["n"] >= 3:
                    victim.hp = 20.0
            return victim(command, *args, **kwargs)

        with patch.object(
            knife,
            "get_log",
            side_effect=["", "", "", "", "", "", "", kill] + [""] * 64,
        ):
            struck = knife.strike(
                self.shooter, regen_state, self.rcon, self.ARENA, "right"
            )
        self.assertTrue(struck[0])

    def test_dead_victim_is_revived_per_leg(self):
        self.victim.hp = 0.0
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 13.0)
        ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7)
        self.assertTrue(ok)
        self.assertIn("respawn", self.victim.commands)

    def test_probe_killed_victim_retries_revived(self):
        # The RCON vuln probe chips 1 HP; on a 1-HP victim that is lethal and
        # the leg must retry revived instead of scoring a 1->0 "hit".
        self.victim.hp = 1.0
        ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7)
        self.assertFalse(ok)
        self.assertIn("respawn", self.victim.commands)
        self.assertGreaterEqual(self.rcon.probes, 2)

    def test_kitless_shooter_is_inconclusive(self):
        with patch.object(knife, "knife_in_hand", return_value=False):
            ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7)
        self.assertIsNone(ok)

    def test_run_scores_every_leg(self):
        script = iter([7, 13, 7, 0, 13, 0])

        def attack(side):
            drop = next(script)
            if drop:
                self.victim.hp -= drop

        self.shooter.on_attack = attack
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
        )

        def factory(port, *_, **__):
            return self.shooter if port == 47777 else self.victim

        with tempfile.TemporaryDirectory() as stages:
            with (
                patch.object(knife, "deploy", return_value=("3", self.ARENA)),
                patch.object(knife, "arena_bounds", return_value=self.bounds),
                patch.object(duo, "kit_or_raise"),
                patch.object(knife, "get_log", return_value=""),
                patch.object(readiness, "stage_root", return_value=Path(stages)),
            ):
                rc_code = knife.run(
                    args,
                    rcon_factory=lambda *_: self.rcon,
                    bot_factory=factory,
                )
            self.assertEqual(len(list(Path(stages).rglob("*.json"))), 6)
        self.assertEqual(rc_code, 0)
        attacks = [c for c in self.shooter.commands if c.startswith("lr attack ")]
        self.assertEqual(
            [c.split()[-1] for c in attacks],
            ["left", "right", "left", "left", "right", "right"],
        )

    def test_hit_after_earlier_death_counts_life_once(self):
        # Live logs are cumulative: an earlier leg's kill line stays in the
        # full text. life_end must count it once, not stack it on life_before
        # (which would INCONCLUSIVE every leg after the first lethal one).
        history = ["Комната 3: Bob убит Alice (player)"]
        self.victim.hp = 19.0
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 12.0)
        with patch.object(knife, "get_log", side_effect=lambda: "\n".join(history)):
            struck = knife.strike(
                self.shooter, self.victim, self.rcon, self.ARENA, "left"
            )
        killed, before, after, _, rejected, lb, la, le = struck
        self.assertEqual((lb, la, le), (1, 1, 1))
        self.assertFalse(killed)
        self.assertFalse(rejected)
        verdict, _ = readiness.judge_damage(
            hp_before=before,
            hp_after=after,
            killed=killed,
            life_before=lb,
            life_after=la,
            life_end=le,
            expect_hit=True,
        )
        self.assertTrue(verdict)

    def test_respawn_during_baseline_read_is_not_a_hit(self):
        # Death+respawn between the life snapshot and the HP baseline: the old
        # life's HP must not be compared with the new life's lower HP.
        history = []
        base = self.rcon.cmd

        def cmd(command):
            reply = base(command)
            if command.endswith(" Health"):
                history.append("Комната 3: Bob убит Carol (player)")
            return reply

        self.victim.hp = 19.0
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 12.0)
        with (
            patch.object(self.rcon, "cmd", side_effect=cmd),
            patch.object(knife, "get_log", side_effect=lambda: "\n".join(history)),
        ):
            struck = knife.strike(
                self.shooter, self.victim, self.rcon, self.ARENA, "left"
            )
        killed, before, after, _, _, lb, la, le = struck
        verdict, _ = readiness.judge_damage(
            hp_before=before,
            hp_after=after,
            killed=killed,
            life_before=lb,
            life_after=la,
            life_end=le,
            expect_hit=True,
        )
        self.assertIsNone(verdict)

    def test_kill_count_matches_whole_names_only(self):
        log = "\n".join(
            [
                "Комната 3: SuperBob убит Alice (player)",
                "Комната 3: Bob_2 убит Alice (player)",
                "Комната 3: Bob убит Alice (player)",
            ]
        )
        self.assertEqual(readiness.kill_count(log, "Bob"), 1)

    def test_regen_rise_on_must_hit_leg_is_inconclusive(self):
        # A regen tick between baseline and read (raw 19->20) is never damage
        # and never negative damage: the leg retries with a fresh baseline
        # instead of scoring a product FAIL.
        self.shooter.on_attack = lambda side: setattr(self.victim, "hp", 20.0)
        ok = self.leg("swing-ahead-2m", "left", 2.0, 0, True, 7)
        self.assertIsNone(ok)

    def test_run_maps_inconclusive_legs_to_a_distinct_exit(self):
        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
        )

        def factory(port, *_, **__):
            return self.shooter if port == 47777 else self.victim

        def run_in(stages):
            with (
                patch.object(knife, "deploy", return_value=("3", self.ARENA)),
                patch.object(knife, "arena_bounds", return_value=self.bounds),
                patch.object(duo, "kit_or_raise"),
                patch.object(knife, "get_log", return_value=""),
                patch.object(readiness, "stage_root", return_value=Path(stages)),
                patch("builtins.print"),
            ):
                return knife.run(
                    args,
                    rcon_factory=lambda *_: self.rcon,
                    bot_factory=factory,
                )

        with tempfile.TemporaryDirectory() as stages:
            with patch.object(
                knife, "leg", side_effect=[True, None, True, True, True, True]
            ):
                self.assertEqual(
                    run_in(stages), readiness.INCONCLUSIVE, "one None, no FAIL -> 2"
                )
            with patch.object(knife, "leg", return_value=False):
                self.assertEqual(run_in(stages), 1, "any FAIL stays product FAIL")

    def test_run_resumes_passed_legs_by_inputs_hash(self):
        for bot in (self.shooter, self.victim):
            bot.has_kit = False
            bot.deployed_flag = False

        def equipped(bot):
            self.assertTrue(
                bot.has_kit, "resume must redeploy before its initial kit wait"
            )
            self.assertTrue(bot.deployed_flag)

        args = SimpleNamespace(
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
            map="hamburg",
            resume=True,
        )

        def factory(port, *_, **__):
            return self.shooter if port == 47777 else self.victim

        first = [key for key, _, _, _, _, _ in knife.LEGS][0]
        builds = ["b1", "b2", None]
        with tempfile.TemporaryDirectory() as stages:
            inputs = {
                "fixture": 3,
                "map": "hamburg",
                "arena": self.ARENA,
                "legs": [list(leg) for leg in knife.LEGS],
                "shooter": "Alice",
                "victim": "Bob",
                "server": "ok",
                "build": "b1",
            }
            readiness.save_stage(
                stages,
                readiness.inputs_hash(inputs),
                first,
                {"ok": True, "evidence": "expect_hit~7"},
                "",
            )
            with (
                patch.object(knife, "deploy", return_value=("3", self.ARENA)) as deploy,
                patch.object(knife, "arena_bounds", return_value=self.bounds),
                patch.object(duo, "kit_or_raise", side_effect=equipped),
                patch.object(knife, "get_log", return_value=""),
                patch.object(readiness, "stage_root", return_value=Path(stages)),
                patch.object(readiness, "server_build", side_effect=builds),
                patch.object(knife, "leg", return_value=True) as legs,
                patch("builtins.print"),
            ):
                for _ in builds:
                    self.assertEqual(
                        knife.run(
                            args,
                            rcon_factory=lambda *_: self.rcon,
                            bot_factory=factory,
                        ),
                        0,
                    )
            # b1 resumes the cached leg; a rebuilt (b2) or unknown build reruns all.
            self.assertEqual(legs.call_count, 3 * len(knife.LEGS) - 1)
            deploy.assert_not_called()


class ReadinessContract(unittest.TestCase):
    """Fail-before/pass-after for the readiness + verdict contract.

    kill->respawn, absent bot2, undeployed victim, reversed action labels,
    insufficient lane, rejected attack, missing verdict => INCONCLUSIVE;
    one healthy fixture passes; regen rises never score as damage.
    """

    ARENA = "blockfield:dm_arena_1"
    GUN = "tacz:modern_kinetic_gun"

    class Rcon:
        def __init__(self, vulnerable=True):
            self.commands = []
            self.vulnerable = vulnerable

        def cmd(self, command):
            self.commands.append(command)
            if command.endswith(" HurtTime"):
                return "Bob has the following entity data: 0s"
            if "GunCurrentAmmoCount" in command:
                return "entity has the following entity data: 30"
            if " run damage " in command and "knife" in command:
                return (
                    "Applied 1.0 damage to Bob"
                    if self.vulnerable
                    else "target is invulnerable"
                )
            return "ok"

    def make_bot(self, name, pos, held=None, hp=20.0, dim=None, deployed=True):
        state = {
            "name": name,
            "slot": 0,
            "pos": list(pos),
            "dimension": self.ARENA if dim is None else dim,
            "health": hp,
            "held": self.GUN if held is None else held,
            "screen": None,
        }
        flags = {"deployed": deployed}

        def bot(command, *args, **kwargs):
            if command == "state":
                return dict(state)
            if command == "markers":
                return {"deployed": flags["deployed"]}
            if command.startswith("entities"):
                return {"entities": [{"type": "player"}]}
            if command == "slot 0":
                state["held"] = self.GUN
            return {}

        bot.state, bot.flags = state, flags
        return bot

    def dead_bot(self):
        def bot(command, *args, **kwargs):
            raise OSError("refused")

        return bot

    def check(self, shooter, victim, rcon, log="", **kw):
        params = dict(arena=self.ARENA, kit=self.GUN, los=True, vulnerable=True)
        params.update(kw)
        return readiness.check_ready(shooter, victim, rcon, lambda: log, **params)

    def test_healthy_fixture_is_ready(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        ready, record, missing = self.check(shooter, victim, self.Rcon())
        self.assertTrue(ready, missing)
        self.assertEqual(missing, "")
        self.assertEqual(record["format"], "stand-readiness/1")
        self.assertEqual(record["verdict"], "READY")
        self.assertEqual(record["separation_m"], 8.0)
        self.assertEqual(record["life"], {"Alice": 0, "Bob": 0})
        self.assertTrue(all(c["ok"] for c in record["checks"].values()))

    def test_absent_bot2_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        ready, record, missing = self.check(shooter, self.dead_bot(), self.Rcon())
        self.assertFalse(ready)
        self.assertEqual(record["verdict"], "INCONCLUSIVE")
        self.assertTrue(missing.startswith("bot2 probe answered"), missing)

    def test_undeployed_victim_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0], deployed=False)
        ready, _, missing = self.check(shooter, victim, self.Rcon())
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("victim deployed"), missing)

    def test_wrong_dimension_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0], dim="blockfield:hamburg_9")
        ready, _, missing = self.check(shooter, victim, self.Rcon())
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("same dimension"), missing)

    def test_kitless_shooter_is_inconclusive(self):
        shooter = self.make_bot(
            "Alice", [8.0, 65.0, 0.0], held="blockfield:command_tablet"
        )
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        ready, _, missing = self.check(shooter, victim, self.Rcon())
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("shooter kit"), missing)

    def test_probe_waits_for_existing_and_own_hurt_cooldown(self):
        events = []
        ticks = iter([7, 2, 0, 10, 4, 0])

        class Rcon(self.Rcon):
            def cmd(self, command):
                if command.endswith(" HurtTime"):
                    tick = next(ticks)
                    events.append(tick)
                    return f"Bob has the following entity data: {tick}s"
                if " run damage " in command:
                    events.append("probe")
                return super().cmd(command)

        with patch("time.sleep"):
            self.assertTrue(
                readiness.probe_vulnerable(Rcon(), self.ARENA, "Bob", "Alice")
            )
        self.assertEqual(events, [7, 2, 0, "probe", 10, 4, 0])

    def test_probe_can_wait_out_fresh_spawn_protection(self):
        replies = iter(
            ["Target is invulnerable to the given damage type"] * 2
            + ["Applied 1.0 damage to Bob"]
        )
        rcon = self.Rcon()
        original = rcon.cmd

        def cmd(command):
            return next(replies) if " run damage " in command else original(command)

        rcon.cmd = cmd
        with patch("time.sleep"):
            self.assertTrue(
                readiness.probe_vulnerable(rcon, self.ARENA, "Bob", "Alice", timeout=5)
            )
        self.assertEqual(list(replies), [])

    def test_probe_refuses_unreadable_cooldown(self):
        rcon = self.Rcon()
        original = rcon.cmd
        rcon.cmd = lambda command: (
            "No entity was found"
            if command.endswith(" HurtTime")
            else original(command)
        )
        self.assertFalse(readiness.probe_vulnerable(rcon, self.ARENA, "Bob", "Alice"))
        self.assertFalse(any(" run damage " in c for c in rcon.commands))

    def test_probe_stuck_cooldown_times_out_without_damage(self):
        class Rcon(self.Rcon):
            def cmd(self, command):
                self.commands.append(command)
                return "Bob has the following entity data: 10s"

        rcon = Rcon()
        with patch("time.monotonic", side_effect=[0, 0, 11]), patch("time.sleep"):
            self.assertFalse(
                readiness.probe_vulnerable(rcon, self.ARENA, "Bob", "Alice")
            )
        self.assertFalse(any(" run damage " in c for c in rcon.commands))

    def test_probe_error_is_not_vulnerability(self):
        class ErrRcon(self.Rcon):
            def cmd(self, command):
                self.commands.append(command)
                if " run damage " in command:
                    return "ERROR: unknown damage type"
                return "ok"

        self.assertFalse(
            readiness.probe_vulnerable(ErrRcon(), self.ARENA, "Bob", "Alice")
        )
        for reply in ("", "No entity was found", "Could not damage Bob"):

            class Silent(self.Rcon):
                def cmd(self, command, reply=reply):
                    return reply

            self.assertFalse(
                readiness.probe_vulnerable(Silent(), self.ARENA, "Bob", "Alice"),
                reply,
            )
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        ready, _, missing = self.check(shooter, victim, ErrRcon(), vulnerable=None)
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("vulnerable"), missing)

    def test_protection_active_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        # vulnerable=None measures the RCON probe now instead of attesting it.
        ready, _, missing = self.check(
            shooter, victim, self.Rcon(vulnerable=False), vulnerable=None
        )
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("vulnerable"), missing)
        ready, _, _ = self.check(shooter, victim, self.Rcon(), vulnerable=None)
        self.assertTrue(ready)

    def test_unrendered_victim_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        ready, _, missing = self.check(shooter, victim, self.Rcon(), los=False)
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("line of sight"), missing)

    def test_unmeasured_los_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        ready, _, missing = self.check(shooter, victim, self.Rcon(), los=None)
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("line of sight"), missing)

    def test_los_probe_is_measured_when_given(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        seen = {}
        probe = lambda s, n, r: seen.setdefault("radius", r) or True  # noqa: E731
        ready, _, _ = self.check(
            shooter, victim, self.Rcon(), los=None, los_probe=probe
        )
        self.assertTrue(ready)
        self.assertAlmostEqual(seen["radius"], 8.0 + 16.0)
        ready, _, missing = self.check(
            shooter, victim, self.Rcon(), los=None, los_probe=lambda s, n, r: False
        )
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("line of sight"), missing)

    def test_mods_mismatch_is_inconclusive(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, 0.0])
        victim = self.make_bot("Bob", [0.0, 65.0, 0.0])
        good = {
            "server_loaded": "1.2.3",
            "server_pin": "1.2.3",
            "client_loaded": "1.2.3",
            "client_pin": "1.2.3",
        }
        ready, _, _ = self.check(shooter, victim, self.Rcon(), mods=good)
        self.assertTrue(ready)
        bad = dict(good, client_pin="9.9.9")
        ready, _, missing = self.check(shooter, victim, self.Rcon(), mods=bad)
        self.assertFalse(ready)
        self.assertTrue(missing.startswith("mods"), missing)

    def test_kill_across_respawn_is_not_damage(self):
        # rc-triage: a lethal hit plus instant respawn reads HP 20->7 across
        # two lives; without the life join that delta scores as damage.
        verdict, reason = readiness.judge_damage(
            hp_before=20.0,
            hp_after=7.0,
            killed=False,
            life_before=0,
            life_after=1,
            life_end=1,
            expect_hit=True,
        )
        self.assertIsNone(verdict)
        self.assertIn("respawn", reason)

    def test_regen_rise_is_never_negative_damage(self):
        verdict, reason = readiness.judge_damage(
            hp_before=19.0,
            hp_after=20.0,
            killed=False,
            life_before=0,
            life_after=0,
            life_end=0,
            expect_hit=True,
        )
        self.assertIsNone(verdict)
        self.assertIn("regen", reason)

    def test_reversed_labels_are_inconclusive(self):
        verdict, reason = readiness.judge_damage(
            hp_before=20.0,
            hp_after=20.0,
            killed=False,
            life_before=0,
            life_after=0,
            life_end=0,
            expect_hit=False,
            clip="melee_3",
            expect_clips=("melee_2", "melee_1"),
        )
        self.assertIsNone(verdict)
        self.assertIn("clip/side", reason)

    def test_insufficient_lane_is_inconclusive(self):
        kw = dict(
            hp_before=19.0,
            hp_after=19.0,
            killed=False,
            life_before=0,
            life_after=0,
            life_end=0,
            separation=2.9,
            reach=2.6,
        )
        verdict, _ = readiness.judge_damage(expect_hit=True, **kw)
        self.assertIsNone(verdict)
        verdict, _ = readiness.judge_damage(expect_hit=False, **kw)
        self.assertTrue(verdict)

    def test_kill_line_decides_lethals(self):
        verdict, _ = readiness.judge_damage(
            hp_before=20.0,
            hp_after=20.0,
            killed=True,
            life_before=0,
            life_after=1,
            life_end=1,
            expect_hit=True,
        )
        self.assertTrue(verdict)

    def test_wrong_clip_kill_is_inconclusive(self):
        # A must-hit swing that executes the wrong clip and kills a low-health
        # victim is harness mislabeling, never a product hit: the equivalent
        # nonlethal attack is inconclusive too.
        verdict, reason = readiness.judge_damage(
            hp_before=7.0,
            hp_after=20.0,
            killed=True,
            life_before=0,
            life_after=1,
            life_end=1,
            expect_hit=True,
            clip="melee_3",
            expect_clips=("melee_2", "melee_1"),
        )
        self.assertIsNone(verdict)
        self.assertIn("clip/side", reason)

    def test_death_between_scan_and_read_is_inconclusive(self):
        # The kill scan saw a quiet window, but a death plus instant respawn
        # landed before the final HP read: the delta spans two lives.
        verdict, reason = readiness.judge_damage(
            hp_before=19.0,
            hp_after=20.0,
            killed=False,
            life_before=0,
            life_after=0,
            life_end=1,
            expect_hit=True,
        )
        self.assertIsNone(verdict)
        self.assertIn("respawn", reason)

    def test_missing_verdict_state_is_inconclusive(self):
        verdict, _ = readiness.judge_damage(
            hp_before=20.0,
            hp_after=None,
            killed=False,
            life_before=0,
            life_after=0,
            life_end=0,
            expect_hit=True,
        )
        self.assertIsNone(verdict)
        verdict, _ = readiness.judge_damage(
            hp_before=20.0,
            hp_after=20.0,
            killed=False,
            life_before=0,
            life_after=0,
            life_end=0,
            expect_hit=True,
            rejected=True,
        )
        self.assertIsNone(verdict)

    def test_stage_record_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {"ok": True, "evidence": "quiet must-miss"}
            ih = readiness.inputs_hash({"map": "hamburg", "legs": ["a"]})
            self.assertEqual(
                ih, readiness.inputs_hash({"legs": ["a"], "map": "hamburg"})
            )
            readiness.save_stage(tmp, ih, "a", record, "log line\n")
            self.assertEqual(readiness.load_stage(tmp, ih, "a"), record)
            self.assertTrue((Path(tmp) / ih / "a.log").is_file())
            self.assertIsNone(readiness.load_stage(tmp, ih, "missing"))
            self.assertIsNone(readiness.load_stage(tmp, "nope", "a"))
            (Path(tmp) / ih / "a.json").write_text("corrupt{")
            self.assertIsNone(readiness.load_stage(tmp, ih, "a"))

    def test_face_off_all_blocked_lanes_raise(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, -14.5])
        victim = self.make_bot("Bob", [0.0, 65.0, -14.5], deployed=False)
        rcon = self.Rcon()
        with patch("time.sleep"):
            with self.assertRaises(readiness.NotReady) as blocked:
                duo.face_off(
                    rcon,
                    shooter,
                    victim,
                    get_log=lambda: "",
                    arena=self.ARENA,
                )
        self.assertIn("victim deployed", str(blocked.exception))
        self.assertNotIsInstance(blocked.exception, RuntimeError)

    def test_face_off_all_lanes_shortened_is_inconclusive(self):
        # Every lane clamped short before check_ready: nothing fired, so this
        # is a setup problem, never a product FAIL.
        shooter = self.make_bot("Alice", [8.0, 65.0, -14.5])
        victim = self.make_bot("Bob", [0.0, 65.0, -14.5])
        with patch("time.sleep"):
            with self.assertRaises(readiness.NotReady) as blocked:
                duo.face_off(
                    self.Rcon(),
                    shooter,
                    victim,
                    distance=60,
                    bounds=(-2.0, 2.0, -16.0, -13.0),
                    get_log=lambda: "",
                    arena=self.ARENA,
                )
        self.assertIn("shortened", str(blocked.exception))

    def test_face_off_gated_healthy_lane_passes(self):
        shooter = self.make_bot("Alice", [8.0, 65.0, -14.5])
        victim = self.make_bot("Bob", [0.0, 65.0, -14.5])
        dropped = {"done": False}

        def shooting_bot(command, *args, **kwargs):
            if command.startswith("aimshoot ") and not dropped["done"]:
                dropped["done"] = True
                victim.state["health"] = 13.0
                return {"result": "SUCCESS"}
            return shooter(command, *args, **kwargs)

        with patch("time.sleep"):
            ok, detail = duo.face_off(
                self.Rcon(),
                shooting_bot,
                victim,
                get_log=lambda: "",
                arena=self.ARENA,
            )
        self.assertTrue(ok, detail)

    def test_face_off_late_probe_update_is_not_a_hit(self):
        # Every bullet misses; the client only now reflects the pre-lane
        # probe's 1 HP. With a client baseline (20) that late 19 passed.
        shooter = self.make_bot("Alice", [8.0, 65.0, -14.5])
        victim = self.make_bot("Bob", [0.0, 65.0, -14.5])

        class Rcon(self.Rcon):
            def cmd(self, command):
                if command.endswith(" Bob Health"):
                    return "Bob has the following entity data: 19.0f"
                return super().cmd(command)

        def shooting_bot(command, *args, **kwargs):
            if command.startswith("aimshoot "):
                victim.state["health"] = 19.0
                return {"result": "SUCCESS"}
            return shooter(command, *args, **kwargs)

        with patch("time.sleep"):
            ok, _ = duo.face_off(
                Rcon(),
                shooting_bot,
                victim,
                lanes=[(8, 0, 90)],
                get_log=lambda: "",
                arena=self.ARENA,
            )
        self.assertFalse(ok)

    def test_dm_run_without_bot2_is_inconclusive(self):
        args = SimpleNamespace(
            part="basic",
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
        )

        def factory(port, *_, **__):
            if port == 47778:
                raise OSError("refused")

            def solo(command, *args, **kwargs):
                return {"name": "Solo"}

            solo.close = lambda: None
            return solo

        with patch("builtins.print"):
            self.assertEqual(
                dm.run(
                    args,
                    rcon_factory=lambda *_: self.Rcon(),
                    bot_factory=factory,
                    get_log=lambda: "",
                    server_dir="/nonexistent",
                ),
                readiness.INCONCLUSIVE,
            )

    def test_dm_abort_keeps_an_earlier_product_failure(self):
        # basic failed, then limits aborts on an invalid fixture:
        # the product FAIL stays exit 1 instead of becoming INCONCLUSIVE.
        args = SimpleNamespace(
            part="all",
            rcon_host="localhost",
            rcon_port=25575,
            rcon_password="test",
            bot_port=47777,
            bot_port2=47778,
        )

        def factory(port, *_, **__):
            def solo(command, *args, **kwargs):
                return {"name": "Solo"}

            solo.close = lambda: None
            return solo

        def basic_fails_then_aborts(world, server_dir, out):
            out.append(1)
            raise readiness.NotReady("victim deployed", "no")

        with (
            patch("builtins.print"),
            # Mid-scenario abort: basic records a FAIL, then its own gunfight
            # raises NotReady before returning.
            patch.object(dm, "scenario_basic", side_effect=basic_fails_then_aborts),
        ):
            self.assertEqual(
                dm.run(
                    args,
                    rcon_factory=lambda *_: self.Rcon(),
                    bot_factory=factory,
                    get_log=lambda: "",
                    server_dir="/nonexistent",
                ),
                1,
            )


class ProxyStandMint(unittest.TestCase):
    def test_offline_uuid_matches_velocity(self):
        sys.path.insert(0, str(SCRIPTS))
        import proxy_stand

        self.assertEqual(
            proxy_stand.offline_uuid("Player"), "a01e3843-e521-3998-958a-f459800e4d11"
        )

    def test_minted_session_passes_proxy_verification_shape(self):
        """Same checks Sessions.verify applies: header, signature, sub, uuid, key proof."""
        import base64
        import hashlib
        import hmac as hmac_module

        sys.path.insert(0, str(SCRIPTS))
        import proxy_stand

        secret = "stand-secret-for-tests"
        session = proxy_stand.mint_session(secret, "GameNick")
        header, payload, sig = session["token"].split(".")
        import base64 as b64_module

        self.assertEqual(
            header,
            b64_module.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}')
            .rstrip(b"=")
            .decode("ascii"),
        )
        inner = proxy_stand._session_secret(secret)
        expected = hmac_module.new(
            inner, f"{header}.{payload}".encode(), hashlib.sha256
        ).digest()
        self.assertEqual(base64.urlsafe_b64decode(sig + "=="), expected)
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual(claims["sub"], "GameNick")
        self.assertRegex(claims["uuid"], r"^[0-9a-f]{32}$")
        self.assertEqual(
            claims["uuid"], proxy_stand.offline_uuid("GameNick").replace("-", "")
        )
        key = bytes.fromhex(session["key"])
        proof = hmac_module.new(key, b"serverId", hashlib.sha256).hexdigest()
        self.assertEqual(len(proof), 64)

    def test_stand_gate_covers_both_backends(self):
        sys.path.insert(0, str(SCRIPTS))
        import proxy_stand

        with tempfile.TemporaryDirectory() as tmp:
            path = proxy_stand.write_gate(Path(tmp))
            gate = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(gate["schema"], 1)
        self.assertEqual(gate["backends"]["game"]["gate"], "public")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "closed")


class FakeEntryBot:
    def __init__(self, name):
        self._name = name

    def __call__(self, command):
        assert command == "state"
        return {"name": self._name}

    def close(self):
        pass


class EntryScenario(unittest.TestCase):
    def test_registered(self):
        self.assertIn("entry", SCENARIOS)

    def test_join_leg(self):
        class Rcon:
            def cmd(self, command):
                assert command == "list"
                return "There are 1 of a max of 20 players online: GameNick"

        world = SimpleNamespace(rcon=Rcon(), bot=FakeEntryBot("GameNick"), bot2=None)
        with tempfile.TemporaryDirectory() as tmp:
            sys.path.insert(0, str(SCRIPTS))
            import proxy_stand

            proxy_stand.write_gate(Path(tmp))
            with patch.object(entry, "gate_state", return_value="public"):
                out = entry.scenario_join(world, tmp)
        self.assertEqual(sum(out), 0)

    def test_workshop_leg_denies_closed_backend(self):
        class Rcon:
            def cmd(self, command):
                assert command == "list"
                return "There are 1 of a max of 20 players online: GameNick"

        world = SimpleNamespace(
            rcon=Rcon(),
            bot=FakeEntryBot("GameNick"),
            bot2=FakeEntryBot("WorkshopNick"),
        )
        with patch.object(entry, "gate_state", return_value="closed"):
            with patch("time.sleep", return_value=None):
                out = entry.scenario_workshop(world, "/nonexistent")
        self.assertEqual(sum(out), 0)


if __name__ == "__main__":
    unittest.main()


class SeamCheckList(unittest.TestCase):
    def test_malformed_pin_skips_no_crash_no_false_fail(self):
        # A malformed server mod source (no mod_version) must SKIP the
        # version rows, not crash with a traceback nor FAIL on pin=None.
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            props = root / "server" / "mod-server"
            props.mkdir(parents=True)
            (props / "gradle.properties").write_text(
                "minecraft_version=1.21.1\n", encoding="utf-8"
            )
            args = SimpleNamespace(
                rcon_host="127.0.0.1",
                rcon_port=1,
                rcon_password="pw",
                tag_prod=None,
            )

            class Rcon:
                def cmd(self, command):
                    return "1.57.0 commit abc"

            with patch.object(seam, "public_json", side_effect=OSError("offline")):
                rows = seam.version_rows(args, root, lambda *a: Rcon())
        self.assertEqual([ok for _, ok, _ in rows[:2]], [None, None], rows)

    def test_version_matches_mod_server_source(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            props = root / "server" / "mod-server"
            props.mkdir(parents=True)
            (props / "gradle.properties").write_text(
                "mod_version=1.57.0\n", encoding="utf-8"
            )
            (props / "upstream.properties").write_text(
                "upstreamCommit=abc123def456\n", encoding="utf-8"
            )
            args = SimpleNamespace(
                rcon_host="127.0.0.1",
                rcon_port=1,
                rcon_password="pw",
                tag_prod=None,
            )

            class Rcon:
                def cmd(self, command):
                    return "1.57.0 commit abc"

            with patch.object(seam, "public_json", return_value={"build": "v9.9.9"}):
                rows = seam.version_rows(args, root, lambda *a: Rcon())
        self.assertTrue(rows[0][1], rows)
        self.assertIn("1.57.0", rows[0][2])

    def fake_probe(self, motd="BLOCKFIELD"):
        return SimpleNamespace(
            CLOSED_MARKERS=("технические работы",),
            text=lambda node: node if isinstance(node, str) else "",
            status_ping=lambda *args: {"description": motd},
            raknet_ping=lambda *args: "MCPE;OPEN",
            gate_file_states=lambda path: {"game": "public", "workshop": "closed"},
        )

    def test_same_rows_both_targets(self):
        probe = self.fake_probe()
        stand = seam.entry_rows(
            probe, "stand", ("127.0.0.1", 1), None, None, None, "public", "closed"
        )
        prod = seam.entry_rows(
            probe,
            "prod",
            ("127.0.0.1", 1),
            ("127.0.0.1", 2),
            None,
            None,
            "public",
            "builders",
        )
        self.assertEqual(
            [name[len("stand ") :] for name, _, _ in stand],
            [name[len("prod ") :] for name, _, _ in prod],
        )
        self.assertTrue(all(ok for _, ok, _ in stand if ok is not None))

    def test_closed_game_fails(self):
        probe = self.fake_probe("На сервере технические работы")
        rows = seam.entry_rows(
            probe, "prod", ("127.0.0.1", 1), None, None, None, "public", "builders"
        )
        self.assertFalse(rows[0][1])

    def test_probe_errors_fail_not_crash(self):
        probe = self.fake_probe()
        probe.status_ping = lambda *args: (_ for _ in ()).throw(OSError("down"))
        probe.raknet_ping = lambda *args: (_ for _ in ()).throw(OSError("down"))
        probe.gate_file_states = lambda path: (_ for _ in ()).throw(
            ValueError("bad gate")
        )
        with tempfile.TemporaryDirectory() as tmp:
            gate = Path(tmp) / "gate.json"
            gate.write_text("not json")
            rows = seam.entry_rows(
                probe,
                "stand",
                ("127.0.0.1", 1),
                ("127.0.0.1", 2),
                "http://127.0.0.1:9/nope",
                gate,
                "public",
                "closed",
            )
        self.assertTrue(all(ok is False for _, ok, _ in rows))


class DmHarness(unittest.TestCase):
    ARENA = "blockfield:dm_arena_1"

    def bot(
        self,
        name,
        dim=None,
        held="blockfield:command_tablet",
        health=20.0,
        screen=None,
        states=None,
    ):
        """A fake bot: fixed state, or a per-call script for stateful flows."""
        calls = {"n": 0}

        def call(command, *args):
            calls["n"] += 1
            if states is not None:
                return states(calls["n"], command)
            if command == "state":
                return {
                    "name": name,
                    "dimension": self.ARENA if dim is None else dim,
                    "health": health,
                    "pos": [0.0, 65.0, -14.5],
                    "held": held,
                    "screen": screen,
                }
            return {}

        call.calls = calls
        return call

    def test_dead_in_arena_needs_arena_dim_and_death_signal(self):
        alive = {"dimension": self.ARENA, "health": 20.0, "screen": None}
        self.assertFalse(dm.dead_in_arena(alive, self.ARENA))
        self.assertTrue(dm.dead_in_arena({**alive, "health": 0.0}, self.ARENA))
        self.assertTrue(
            dm.dead_in_arena({**alive, "screen": "DeathScreen"}, self.ARENA)
        )
        # A death screen in another dimension is not the in-match wait.
        self.assertFalse(
            dm.dead_in_arena(
                {
                    "dimension": "minecraft:overworld",
                    "health": 0.0,
                    "screen": "DeathScreen",
                },
                self.ARENA,
            )
        )
        # No player yet (no health field) is not dead.
        self.assertFalse(dm.dead_in_arena({"dimension": self.ARENA}, self.ARENA))

    def test_world_waits_for_connecting_second_client(self):
        seen = {"n": 0}

        def bot2(command, *args):
            seen["n"] += 1
            if seen["n"] < 3:
                return {"connected": True}
            return {"name": "Victim", "dimension": self.ARENA}

        rcon = FakeRcon()
        with patch("time.sleep"):
            world = dm.World(rcon, self.bot("Shooter"), bot2, ["log"], name_timeout=5.0)
        self.assertTrue(world.duo)
        self.assertEqual(world.names, ["Shooter", "Victim"])

    def test_world_drops_a_second_client_that_never_connects(self):
        rcon = FakeRcon()

        def bot2(command, *args):
            return {"connected": True}

        with patch("time.sleep"):
            world = dm.World(
                rcon, self.bot("Shooter"), bot2, ["log"], name_timeout=0.05
            )
        self.assertFalse(world.duo)
        self.assertEqual(world.names, ["Shooter"])

    def test_wait_vulnerable_stops_probing_once_damage_lands(self):
        replies = iter(["invulnerable entity", "ok"])

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                if " run damage " in command:
                    return next(replies)
                return "ok"

        rcon = Rcon()
        with patch("time.sleep"):
            self.assertTrue(dm.wait_vulnerable(rcon, self.ARENA, "A", "V"))
        probes = [c for c in rcon.commands if " run damage " in c]
        self.assertEqual(len(probes), 2)
        self.assertIn("blockfield:knife", probes[0])

    def test_wait_vulnerable_times_out_under_protection(self):
        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "entity is invulnerable right now"

        with patch("time.sleep"):
            self.assertFalse(
                dm.wait_vulnerable(Rcon(), self.ARENA, "A", "V", timeout=0.05)
            )

    def test_face_off_kill_predicate_sees_death_in_arena(self):
        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        fired = {"n": 0}

        def shooter(command, *args):
            if command == "state":
                return {
                    "name": "Shooter",
                    "slot": 0,
                    "pos": [8.0, 82.0, 0.0],
                    "dimension": self.ARENA,
                }
            if command == "slot 0":
                return {}
            if command.startswith("entities"):
                return {"entities": [{"type": "player"}]}
            assert command.startswith("aimshoot ")
            fired["n"] += 1
            rcon.ammo -= 1
            return {"result": "SUCCESS"}

        def victim(command, *args):
            assert command == "state"
            dead = fired["n"] >= 2
            return {
                "name": "Victim",
                "pos": [0.0, 82.0, 0.0],
                "dimension": self.ARENA,
                "health": 0.0 if dead else 20.0,
                "screen": "DeathScreen" if dead else None,
            }

        rcon = Rcon()
        arena = self.ARENA
        with patch("time.sleep"):
            ok, detail = duo.face_off(
                rcon,
                shooter,
                victim,
                dead=lambda s: dm.dead_in_arena(s, arena),
            )
        self.assertTrue(ok, detail)
        self.assertIn(arena, detail)
        self.assertIn("lane (8,0)", detail)

    def test_basic_flows_two_kill_respawn_cycles_in_arena(self):
        """Scripted happy path: deploy, gun kill, redeploy, suicide, redeploy."""
        arena = self.ARENA
        gun = "tacz:modern_kinetic_gun"
        tablet = "blockfield:command_tablet"
        shots = {"n": 0}
        spawns = {"n": 0}

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                if command.endswith(" HurtTime"):
                    return "entity has the following entity data: 0s"
                if "GunCurrentAmmoCount" in command:
                    return "entity has the following entity data: 30"
                if "bf start deathmatch no_such_arena_xyz" in command:
                    return "Ошибка: нет такой арены"
                if " run damage " in command and "knife" in command:
                    return "Applied 1.0 damage to Bob"
                return "ok"

        def shooter(command, *args):
            if command.startswith("bfaction play:"):
                log.append("Комната 3 открыта (Каждый сам за себя)")
            if command == "markers":
                return {"deployed": True}
            if command == "state":
                return {
                    "name": "Shooter",
                    "slot": 0,
                    "pos": [8.0, 65.0, -14.5],
                    "dimension": arena,
                    "health": 20.0,
                    "held": gun,
                    "screen": None,
                }
            if command.startswith("entities"):
                return {"entities": [{"type": "player"}]}
            if command.startswith("aimshoot "):
                shots["n"] += 1
                return {"result": "SUCCESS"}
            return {}

        victim = {"gun": True}
        log = []

        def victim_bot(command, *args):
            if command == "markers":
                return {"deployed": True}
            if command == "bfaction kill":
                log.append("Комната 3: Victim убит - (outOfWorld)")
                victim["gun"] = False
                return {"sent": "kill"}
            if command == "respawn":
                return {"ok": True}
            if command == "bfaction spawn:BASE":
                spawns["n"] += 1
                victim["gun"] = True
                return {"sent": "spawn:BASE"}
            if command == "state":
                return {
                    "name": "Victim",
                    "slot": 0,
                    "pos": [0.0, 65.0, -14.5],
                    "dimension": arena,
                    "health": 20.0,
                    "held": gun if victim["gun"] else tablet,
                    "screen": None,
                }
            return {}

        # The gun kill lands once the lane search shoots: instant respawn, so
        # the client stays alive in the arena and only the server log changes.
        real_victim = victim_bot
        dropped = {"done": False}

        def killing_victim(command, *args):
            if command == "state" and shots["n"] >= 1 and not dropped["done"]:
                dropped["done"] = True
                log.append("Комната 3: Victim убит Shooter (player_attack)")
                victim["gun"] = False
            return real_victim(command, *args)

        rcon = Rcon()
        with tempfile.TemporaryDirectory() as tmp:
            maps = Path(tmp) / "blockfield" / "maps"
            maps.mkdir(parents=True)
            (maps / "dm_arena.json").write_text(
                json.dumps(
                    {"bounds": {"minX": -32, "maxX": 31, "minZ": -32, "maxZ": 31}}
                ),
                encoding="utf-8",
            )
            with patch("time.sleep"):
                world = dm.World(rcon, shooter, killing_victim, [])
                world.log_lines = log
                out = dm.scenario_basic(world, tmp)
            tps = [
                c.split(" run tp Shooter ")[1].split()[:3]
                for c in rcon.commands
                if " run tp Shooter " in c
            ]
            self.assertTrue(tps, "lanes teleport the shooter")
            for x, _y, z in tps:
                self.assertLessEqual(abs(float(x)), 32.0)
                self.assertLessEqual(abs(float(z)), 32.0)
        self.assertEqual(out, [0] * len(out), out)
        self.assertGreater(out, [], "basic must run its checks")
        self.assertGreaterEqual(shots["n"], 1)
        # Gun kill redeploy plus suicide redeploy: spawn:BASE polled to kit.
        self.assertGreaterEqual(spawns["n"], 2)

    def test_limits_keeps_single_player_waiting_in_arena(self):
        arena = self.ARENA
        gun = "tacz:modern_kinetic_gun"
        victim = {"gun": True}
        log = []

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.probes = 0

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command:
                    return "entity has the following entity data: 30"
                if " run damage " in command and "knife" in command:
                    self.probes += 1
                    return (
                        "entity is invulnerable right now"
                        if self.probes < 3
                        else "Applied 1.0 damage to Bob"
                    )
                if "minecraft:lava" in command:
                    log.append("Комната 3: Victim убит - (lava)")
                    victim["gun"] = False
                return "ok"

        def bot1(command, *args):
            if command.startswith("bfaction play:"):
                log.append("Комната 3 открыта (Каждый сам за себя)")
            if command == "state":
                return {
                    "name": "Solo",
                    "dimension": arena,
                    "health": 20.0,
                    "pos": [0.0, 65.0, 0.0],
                    "held": gun,
                    "screen": None,
                }
            if command == "messages 20":
                return ["Комната 3 открыта", "Нужно минимум 2 игрока, сейчас 1"]
            return {}

        victim = {"alive": True, "gun": True}

        def bot2(command, *args):
            if command == "bfaction kill":
                return {}
            if command == "respawn":
                return {}
            if command == "bfaction spawn:BASE":
                victim["gun"] = True
                return {}
            if command == "state":
                return {
                    "name": "Victim",
                    "dimension": arena,
                    "health": 20.0,
                    "pos": [0.0, 65.0, -14.5],
                    "held": gun if victim["gun"] else "blockfield:command_tablet",
                    "screen": None,
                }
            return {}

        rcon = Rcon()
        with tempfile.TemporaryDirectory() as tmp:
            rules = Path(tmp) / "deathmatch.json"
            with patch("time.sleep"):
                world = dm.World(rcon, bot1, bot2, [])
                world.log_lines = log
                out = dm.scenario_limits(world, rules)
            self.assertFalse(rules.exists(), "rules file is restored")
        self.assertEqual(out, [0] * len(out), out)

    def test_limits_no_room_failure_reaches_the_accumulator(self):
        # The caller ignores the return value: a room timeout must land in
        # the shared rows, or `--part limits` exits 0 on a failure.
        def bot(command, *args, **kwargs):
            return {"name": "Bot", "dimension": "minecraft:overworld"}

        out = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch("time.sleep"), patch.object(dm, "open_room", return_value=None):
                world = dm.World(FakeRcon(), bot, bot, [])
                world.log_lines = []
                dm.scenario_limits(world, Path(tmp) / "deathmatch.json", out)
        self.assertEqual(sum(out), 1, out)

    def test_face_off_clamps_corner_lanes_inside_bounds(self):
        """Corner deploy on dm_arena: the raw +8 lane exits (x 36.5), so it
        is skipped instead of shot short, and the kill lands on (-8,0)."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        fired = {"n": 0}

        def shooter(command, *args):
            if command == "state":
                return {
                    "name": "Shooter",
                    "slot": 0,
                    "pos": [28.5, 65.0, 28.5],
                    "dimension": self.ARENA,
                }
            if command in ("slot 0",):
                return {}
            if command.startswith("entities"):
                return {"entities": [{"type": "player"}]}
            assert command.startswith("aimshoot ")
            fired["n"] += 1
            rcon.ammo -= 1
            return {"result": "SUCCESS"}

        def victim(command, *args):
            assert command == "state"
            return {
                "name": "Victim",
                "pos": [28.5, 65.0, 28.5],
                "dimension": self.ARENA,
                "health": 0.0 if fired["n"] >= 1 else 20.0,
            }

        rcon = Rcon()
        with patch("time.sleep"):
            ok, detail = duo.face_off(
                rcon,
                shooter,
                victim,
                dead=lambda s: s.get("health", 20) <= 0,
                bounds=(-32, 31, -32, 31),
            )
        self.assertTrue(ok, detail)
        self.assertIn("lane (-8,0)", detail)
        tps = [
            c.split(" run tp Shooter ")[1].split()[:3]
            for c in rcon.commands
            if " run tp Shooter " in c
        ]
        # Only the surviving lane teleports: the shortened +8 lane is
        # skipped, never shot short.
        self.assertEqual(len(tps), 1)
        for x, _y, z in tps:
            self.assertLessEqual(float(x), 32.0)
            self.assertLessEqual(float(z), 32.0)

    def test_rendered_waits_for_the_victim_entity(self):
        """Render readiness is state-driven: no player entry, not rendered;
        a player entry, rendered at once."""

        def shooter(entries):
            def call(command, *args):
                assert command.startswith("entities")
                return {"entities": entries}

            return call

        with patch("time.sleep"):
            self.assertTrue(duo.rendered(shooter([{"type": "player"}])))
            self.assertFalse(duo.rendered(shooter([]), timeout=0.05))
            self.assertFalse(duo.rendered(shooter([{"type": "zombie"}]), timeout=0.05))

    def test_rendered_radius_covers_the_lane_distance(self):
        """A 60 m lane never appears in the default 32-block query (RC final3
        long-range FAIL): the wait must query distance + margin."""

        seen = {}

        def shooter(command, *args):
            seen["cmd"] = command
            return {"entities": [{"type": "player"}]}

        with patch("time.sleep"):
            self.assertTrue(duo.rendered(shooter, radius=60 + 16))
        self.assertEqual(seen["cmd"], "entities 76")

    def test_tdm_respawn_accepts_in_arena_death(self):
        """Dead members wait INSIDE the match dimension by design (RespawnRoom
        in-dim platform): the death screen at zero health in the arena counts
        as dead, not just the legacy shared-respawn dim (RC final3 tdm FAIL).
        """

        dims = {"arena": "blockfield:hamburg_4"}
        calls = {"kill": 0}

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "dealt 1"

        def bot(name):
            def call(command, timeout=None):
                if command == "state":
                    if calls["kill"]:
                        return {
                            "name": name,
                            "slot": 0,
                            "held": "tacz:modern_kinetic_gun",
                            "dimension": dims["arena"],
                            "health": 0.0,
                            "screen": "DeathScreen",
                        }
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "dimension": dims["arena"],
                        "health": 20.0,
                    }
                if command == "bfaction spawn:BASE":
                    return {}
                if command == "bfaction kill":
                    calls["kill"] += 1
                    return {}
                return {}

            return call

        with patch("time.sleep"):
            ok, detail = rc.tdm_respawn(
                Rcon(),
                bot("Bot1"),
                bot("Bot2"),
                dims["arena"],
                lambda: "",
                timeout=5,
            )
        self.assertTrue(ok, detail)

    def test_tdm_respawn_waits_out_spawn_protection(self):
        """A fresh spawn is shielded: the kill waits for vulnerability first
        (a knife probe still reporting invulnerable), like dm.py does — killing
        into the shield is a silent no-op (RC final4 tdm FAIL).
        """

        dims = {"arena": "blockfield:hamburg_4"}
        probes, kills = [], []
        log_lines = []

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                if " run damage " in command:
                    probes.append(command)
                    return (
                        "invulnerable"
                        if len(probes) < 3
                        else "Applied 1.0 damage to Bob"
                    )
                return "ok"

        def bot(name):
            def call(command, timeout=None):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "dimension": dims["arena"],
                        "health": 20.0,
                    }
                if command == "bfaction kill":
                    kills.append(name)
                    log_lines.append(f"Комната 7: {name} убит - (outOfWorld)")
                    dims["killed"] = True
                    return {}
                return {}

            call.close = lambda: None
            return call

        with patch("time.sleep"):
            ok, detail = rc.tdm_respawn(
                Rcon(),
                bot("Bot1"),
                bot("Bot2"),
                dims["arena"],
                lambda: "\n".join(log_lines),
                timeout=5,
            )
        self.assertTrue(ok, detail)
        # Two shielded probes, then the kill only after vulnerability.
        self.assertEqual(len(probes), 3)
        self.assertEqual(kills, ["Bot2"])

    def test_tdm_respawn_refuses_while_shielded(self):
        """A victim that never leaves spawn protection fails the leg instead
        of burning the kill into the shield.
        """

        class Rcon(FakeRcon):
            def cmd(self, command):
                self.commands.append(command)
                return "invulnerable" if " run damage " in command else "ok"

        def bot(name):
            def call(command, timeout=None):
                if command == "state":
                    return {
                        "name": name,
                        "slot": 0,
                        "held": "tacz:modern_kinetic_gun",
                        "dimension": "blockfield:hamburg_4",
                        "health": 20.0,
                    }
                if command == "bfaction kill":
                    raise AssertionError("kill must not fire into the shield")
                return {}

            return call

        clock = iter(range(100000))

        def fake_wait_dim(bot, wanted, timeout):
            return wanted

        with (
            patch.object(rc.time, "sleep"),
            patch.object(rc.time, "monotonic", side_effect=lambda: next(clock)),
            patch.object(rc, "wait_dim", side_effect=fake_wait_dim),
        ):
            ok, detail = rc.tdm_respawn(
                Rcon(), bot("Bot1"), bot("Bot2"), "blockfield:hamburg_4", lambda: ""
            )
        self.assertFalse(ok)
        self.assertIn("never vulnerable", detail)

    def test_face_off_until_dead_keeps_shooting_past_first_blood(self):
        """until_dead: a scratch does not pass; the third round kills."""

        class Rcon(FakeRcon):
            def __init__(self):
                super().__init__()
                self.ammo = 30

            def cmd(self, command):
                self.commands.append(command)
                if "GunCurrentAmmoCount" in command and "data get" in command:
                    return f"entity has the following entity data: {self.ammo}"
                return "ok"

        fired = {"n": 0}

        def shooter(command, *args):
            if command == "state":
                return {
                    "name": "Shooter",
                    "slot": 0,
                    "pos": [8.0, 65.0, 0.0],
                    "dimension": self.ARENA,
                }
            if command == "slot 0":
                return {}
            if command.startswith("entities"):
                return {"entities": [{"type": "player"}]}
            assert command.startswith("aimshoot ")
            fired["n"] += 1
            rcon.ammo -= 1
            return {"result": "SUCCESS"}

        def victim(command, *args):
            assert command == "state"
            return {
                "name": "Victim",
                "pos": [0.0, 65.0, 0.0],
                "dimension": self.ARENA,
                "health": {0: 20.0, 1: 15.0, 2: 10.0}.get(fired["n"], 0.0),
            }

        rcon = Rcon()
        with patch("time.sleep"):
            plain_ok, _ = duo.face_off(
                rcon, shooter, victim, dead=lambda s: s.get("health", 20) <= 0
            )
            self.assertTrue(plain_ok)
            self.assertEqual(fired["n"], 1)
            fired["n"] = 0
            rcon.ammo = 30
            dead_ok, detail = duo.face_off(
                rcon,
                shooter,
                victim,
                dead=lambda s: s.get("health", 20) <= 0,
                until_dead=True,
            )
        self.assertTrue(dead_ok, detail)
        self.assertEqual(fired["n"], 3)


class HudChecks(unittest.TestCase):
    def test_duel_corner_lanes_keep_kit_and_admin_shooters_in_bounds(self):
        arena = "blockfield:dm_arena_1"
        bounds = (-32, 31, -32, 31)
        shooter_id = hud_checks.offline_uuid("Shooter")
        entry = {"attacker": "Shooter", "target": "Victim"}
        for require_admin in (False, True):
            with self.subTest(require_admin=require_admin):
                state = {"dead": False, "admin": False, "x": 0}
                teleports = []

                def command(text):
                    if " run tp " in text:
                        target, x, _, z, *_ = text.split(" run tp ")[1].split()
                        x, z = float(x), float(z)
                        self.assertTrue(-32 <= x <= 32 and -32 <= z <= 32, text)
                        teleports.append((x, z))
                        if target == shooter_id:
                            state["x"] = x
                        return "Teleported"
                    if text.startswith("item replace"):
                        state["admin"] = True
                        return "Replaced"
                    if text.endswith(".GunCurrentAmmoCount"):
                        return "entity has the following entity data: 30"
                    if text.endswith(".GunId"):
                        return hud_checks.ZERO_SPREAD
                    raise AssertionError(text)

                def player(name):
                    def bot(text):
                        if text == "state":
                            return {
                                "name": name,
                                "pos": [-28.5, 65, -28.5],
                                "dimension": arena,
                                "held": hud_checks.KIT_GUN,
                                "health": 0 if state["dead"] else 20,
                            }
                        if text == "killfeed":
                            return {"entries": [entry] if state["dead"] else []}
                        if text.startswith("entities "):
                            return {"entities": [{"type": "player"}]}
                        if text.startswith("aimshoot "):
                            if state["x"] == -32 and (
                                not require_admin or state["admin"]
                            ):
                                state["dead"] = True
                            return {"result": "SUCCESS"}
                        if text == "slot 0":
                            return {}
                        raise AssertionError(text)

                    return bot

                with patch("time.sleep"), patch("builtins.print"):
                    out, killed = hud_checks.scenario_duel(
                        SimpleNamespace(cmd=command),
                        player("Shooter"),
                        player("Victim"),
                        arena,
                        bounds,
                    )
                self.assertEqual(out, [1, 0, 0, 0] if require_admin else [0, 0, 0])
                self.assertEqual(killed, entry)
                self.assertIn((-32, -28.5), teleports)
                self.assertEqual(state["admin"], require_admin)

    def png(self, rows, ctype=2):
        import struct
        import zlib

        raw = b"".join(b"\x00" + b"".join(bytes(px[:3]) for px in row) for row in rows)

        def chunk(kind, data):
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack(">IIBBBBB", len(rows[0]), len(rows), 8, ctype, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )

    def test_gun_replace_command(self):
        self.assertEqual(
            hud_checks.gun_replace_command("Player1", "elitex:mp5_classic"),
            "item replace entity Player1 hotbar.0 with tacz:modern_kinetic_gun"
            '[minecraft:custom_data={GunId:"elitex:mp5_classic",'
            'GunCurrentAmmoCount:30000,HasBulletInBarrel:1b,GunFireMode:"SEMI"}]',
        )
        for bad_name in ("", "a b", "x" * 17, "a;b", "<tag>Probe"):
            with self.subTest(bad_name=bad_name):
                self.assertRaises(
                    ValueError,
                    hud_checks.gun_replace_command,
                    bad_name,
                    "elitex:mp5_classic",
                )
        for bad_gun in ("", "mp5", "Elitex:mp5", "../x"):
            with self.subTest(bad_gun=bad_gun):
                self.assertRaises(
                    ValueError, hud_checks.gun_replace_command, "Player1", bad_gun
                )
        self.assertRaises(
            ValueError,
            hud_checks.gun_replace_command,
            "Player1",
            "elitex:mp5_classic",
            0,
        )

    def test_isolation_suicide_stays_in_started_room(self):
        arena = "blockfield:dm_arena_1"
        victim, observer = "Victim", "Observer"
        entry = {"attacker": victim, "target": victim}
        uid = hud_checks.offline_uuid(victim)
        for reply, leaked, want in (
            ("Applied 100.0 damage to Victim", False, [0] * 6),
            ("Applied 100.0 damage to Victim", True, [0, 0, 1, 0, 0, 0]),
            ("Target is invulnerable", False, [1]),
        ):
            with self.subTest(reply=reply, leaked=leaked):
                state = {"dead": False}
                cmds = []

                def command(text):
                    cmds.append(text)
                    if "Applied" in reply:
                        state["dead"] = True
                    return reply

                def member(text):
                    if text == "state":
                        return {"name": victim, "dimension": arena}
                    if text == "killfeed":
                        return {"entries": [entry] if state["dead"] else []}
                    if text == "markers":
                        if state["dead"]:
                            return {"deployed": False, "minimap": None}
                        return {
                            "deployed": True,
                            "minimap": {},
                            "playerTeam": "A",
                        }
                    if text == "bfaction spawn:BASE":
                        state["dead"] = False
                        return {}
                    raise AssertionError(text)

                def parked(text):
                    if text == "state":
                        return {"name": observer, "dimension": arena + "_2"}
                    if text == "killfeed":
                        return {
                            "entries": [entry] if (leaked and state["dead"]) else []
                        }
                    raise AssertionError(text)

                with patch("time.sleep"):
                    out = hud_checks.scenario_isolation(
                        SimpleNamespace(cmd=command), member, parked, "2"
                    )
                self.assertEqual(out, want)
                self.assertEqual(cmds[0].count(uid), 2)
                self.assertNotIn(observer, cmds[0])

    def test_isolation_minimap_waits_for_render(self):
        # The client hides and re-renders the minimap a few query ticks after
        # the deployed flag flips; instant reads flaked on slow stands.
        arena = "blockfield:dm_arena_1"
        victim, observer = "Victim", "Observer"
        entry = {"attacker": victim, "target": victim}
        state = {"dead": False, "reads": 0}

        def member(text):
            if text == "state":
                return {"name": victim, "dimension": arena}
            if text == "killfeed":
                return {"entries": [entry] if state["dead"] else []}
            if text == "markers":
                state["reads"] += 1
                if state["dead"]:
                    # deployed drops at once, the minimap hides two ticks later
                    return {
                        "deployed": False,
                        "minimap": None if state["reads"] > 2 else {},
                    }
                # respawn flips deployed at once, the minimap renders later
                minimap = {} if state.get("respawned") and state["reads"] > 2 else None
                return {"deployed": True, "minimap": minimap, "playerTeam": "A"}
            if text == "bfaction spawn:BASE":
                state["dead"] = False
                state["respawned"] = True
                state["reads"] = 0
                return {}
            raise AssertionError(text)

        def parked(text):
            if text == "state":
                return {"name": observer, "dimension": arena + "_2"}
            if text == "killfeed":
                return {"entries": []}
            raise AssertionError(text)

        def command(text):
            state["dead"] = True
            state["reads"] = 0
            return "Applied 100.0 damage to Victim"

        with patch("time.sleep"):
            out = hud_checks.scenario_isolation(
                SimpleNamespace(cmd=command), member, parked, "2"
            )
        self.assertEqual(out, [0] * 6, out)

    def test_parse_health_and_pos(self):
        self.assertEqual(hud_checks.parse_health("bot43 has Health: 20.0f"), 20.0)
        self.assertEqual(
            hud_checks.parse_health("Zombie has the following entity data: 7.5f"), 7.5
        )
        self.assertEqual(
            hud_checks.parse_pos("bot43 has Pos: [1.5d, 65.0d, -14.5d]"),
            (1.5, 65.0, -14.5),
        )
        self.assertEqual(
            hud_checks.parse_pos(
                "bot43 has the following entity data: [-127.7d, 86.0d, 15.29d]"
            ),
            (-127.7, 86.0, 15.29),
        )
        self.assertRaises(ValueError, hud_checks.parse_health, "no data")
        self.assertRaises(ValueError, hud_checks.parse_pos, "no data")

    def test_killfeed_match(self):
        entries = [
            {"attacker": "A", "target": "B"},
            {"attacker": "A", "target": "<tag>Probe"},
        ]
        self.assertEqual(
            hud_checks.killfeed_match(entries, target="<tag>Probe"), entries[1]
        )
        self.assertIsNone(hud_checks.killfeed_match(entries, target="nobody"))
        self.assertEqual(hud_checks.killfeed_match(entries, attacker="A"), entries[0])
        self.assertIsNone(hud_checks.killfeed_match([], target="B"))

    def test_isolation_empty_parked_feed_passes(self):
        # The parked client sees no line: match returns None, and the old
        # `not any(match)` raised TypeError on exactly this outcome.
        line = {"attacker": "Victim", "target": "Victim"}
        state = {"spawned": False, "killed": False}

        def victim(text):
            if text == "state":
                return {"name": "Victim", "dimension": "blockfield:arena1"}
            if text == "markers":
                if state["spawned"]:
                    return {
                        "deployed": True,
                        "minimap": {"x": 1},
                        "playerTeam": "red",
                    }
                return {"deployed": False, "minimap": None}
            if text == "killfeed":
                return {"entries": [line] if state["killed"] else []}
            if text == "bfaction spawn:BASE":
                state["spawned"] = True
                return {}
            raise AssertionError(text)

        def parked(text):
            if text == "state":
                return {"name": "Observer", "dimension": "minecraft:overworld"}
            if text == "killfeed":
                return {"entries": []}
            if text.startswith("bfaction"):
                return {}
            raise AssertionError(text)

        def damage(text):
            state["killed"] = True
            return "SUCCESS"

        rcon = SimpleNamespace(cmd=damage)
        with (
            patch("time.sleep"),
            patch.object(hud_checks, "wait_for", side_effect=lambda fn, *a: fn()),
            patch("builtins.print"),
        ):
            out = hud_checks.scenario_isolation(rcon, victim, parked, "room1")
        self.assertEqual(out, [0] * 6, out)

    def test_tag_exact_tricky_name(self):
        evil = "x_reset_x"

        def bot(command):
            assert command == "messages 30", command
            return ["Фраги: 1/30 · лидер: Player1103", "ГОТОВ К ВЫСАДКЕ!"]

        def bot2(command):
            assert command == "state", command
            return {"name": evil, "dimension": "blockfield:hamburg_2"}

        entry = {"attacker": "Player1103", "target": evil, "headshot": False}
        self.assertEqual(hud_checks.scenario_tag(None, bot, bot2, entry), [0, 0])

    def test_tag_requires_tricky_name(self):
        def bot2(command):
            assert command == "state", command
            return {"name": "Normal", "dimension": "blockfield:hamburg_2"}

        self.assertEqual(hud_checks.scenario_tag(None, lambda cmd: [], bot2, None), [1])

    def test_tricky_names_are_login_valid(self):
        for name in hud_checks.TRICKY_NAMES:
            with self.subTest(name=name):
                self.assertTrue(hud_checks.is_tricky_name(name))
                hud_checks.gun_replace_command(name, "elitex:mp5_classic")
        self.assertFalse(hud_checks.is_tricky_name("Normal"))
        self.assertFalse(hud_checks.is_tricky_name("<tag>Probe"))

    def test_magenta_fraction(self):
        black = self.png([[(0, 0, 0), (10, 20, 30)], [(1, 2, 3), (4, 5, 6)]])
        self.assertEqual(hud_checks.magenta_fraction_png(black), 0.0)
        one = self.png([[(255, 0, 255), (0, 0, 0)], [(0, 0, 0), (0, 0, 0)]])
        self.assertEqual(hud_checks.magenta_fraction_png(one), 0.25)
        dim = self.png([[(200, 80, 200)]])
        self.assertEqual(hud_checks.magenta_fraction_png(dim), 0.0)
        self.assertRaises(ValueError, hud_checks.magenta_fraction_png, b"nope")
        gray = self.png([[(7, 7, 7)]], ctype=0)
        self.assertRaises(ValueError, hud_checks.magenta_fraction_png, gray)


def _ammo_script(first, rest):
    """sbw_ammo stub: `first` for the top-up/settle reads, `rest` after.

    The victim leg polls the mag on its own clock, so a step list would be
    timing-fragile; a two-level script is not.
    """
    seen = {"n": 0}

    def fake_ammo(rcon, player, weapon=None):
        seen["n"] += 1
        return first if seen["n"] <= 4 else rest

    return fake_ammo


class WeaponsSweep(unittest.TestCase):
    def make_jar(self, directory, name, entries):
        import zipfile

        path = Path(directory) / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, payload in entries.items():
                archive.writestr(entry, payload)
        return path

    def test_tacz_inventory_reads_pack_gun_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_jar(
                tmp,
                "pack.jar",
                {
                    "assets/elitex/custom/elitex_quality_guns/data/elitex/data/guns/benelli_super_data.json": "{}",
                    "assets/tacz/custom/tacz_default_gun/data/tacz/data/guns/glock_17_data.json": "{}",
                    "assets/elitex/custom/elitex_quality_guns/assets/elitex/display/guns/benelli_super_display.json": "{}",
                },
            )
            self.make_jar(tmp, "empty.jar", {"readme.txt": "hi"})
            found = weapons_sweep.tacz_gun_ids(tmp)
            self.assertEqual(
                found,
                {
                    "elitex:benelli_super": "pack.jar",
                    "tacz:glock_17": "pack.jar",
                },
            )

    def test_tacz_inventory_ammo_reads_minigun(self):
        # Only inventory-reload guns are mapped: minigun's shots each need a
        # matching AmmoId stack, magazine guns never do.
        with tempfile.TemporaryDirectory() as tmp:
            self.make_jar(
                tmp,
                "pack.jar",
                {
                    "assets/tacz/custom/tacz_default_gun/data/tacz/data/guns/minigun_data.json": (
                        '{"ammo": "tacz:308",\n'
                        "// magazine is elfeed, manual, fuel or inventory\n"
                        '"reload": {"type": "inventory"}}'
                    ),
                    "assets/tacz/custom/tacz_default_gun/data/tacz/data/guns/glock_17_data.json": (
                        '{"ammo": "tacz:9mm", "reload": {"type": "magazine"}}'
                    ),
                },
            )
            self.assertEqual(
                weapons_sweep.tacz_inventory_ammo(tmp), {"tacz:minigun": "tacz:308"}
            )
            self.assertEqual(
                set(weapons_sweep.tacz_gun_ids(tmp)),
                {"tacz:minigun", "tacz:glock_17"},
            )

    def test_minigun_toggles_creative_and_restores(self):
        # The rig vacuums all loose ammo and TaCZ's per-shot scan sees only
        # the vanilla inventory: the minigun leg fires in creative (skips the
        # check) with the right ammo id given, then restores survival.
        rseen = []
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0), rseen=rseen
        )
        with patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon,
                bot,
                "hitbot",
                "tacz",
                "tacz:minigun",
                log_path,
                0,
                inv_ammo="tacz:308",
            )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("creative_for_minigun"))
        self.assertTrue(
            any(cmd.startswith("gamemode creative") for cmd in rseen),
            rseen,
        )
        self.assertTrue(
            any(cmd.startswith("gamemode survival") for cmd in rseen),
            rseen,
        )
        self.assertTrue(
            any('AmmoId:"tacz:308"' in cmd for cmd in rseen),
            rseen,
        )

    def test_sbw_inventory_reads_gun_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_jar(
                tmp,
                "sbw.jar",
                {
                    "assets/superbwarfare/sbw/guns/ak_12.json": "{}",
                    "assets/superbwarfare/sbw/guns/ammo_762x39.json": "{}",
                },
            )
            found, skipped = weapons_sweep.sbw_weapon_items(tmp)
            self.assertEqual(found, ["superbwarfare:ak_12"])
            self.assertEqual(skipped, ["superbwarfare:ammo_762x39"])

    def make_pair(
        self,
        log_lines=(),
        fail_shoot=False,
        ammo=(300, 299),
        victim_hp=(),
        have_item=True,
        shoot_result="SUCCESS",
        dies_on_shoot=False,
        player_pos="[-18.5d, 65.0d, 2.5d]",
        dimension="blockfield:range",
        seen=None,
        gun_nbt="ok",
        rseen=None,
    ):
        log_path = Path(tempfile.mkdtemp()) / "latest.log"
        log_path.write_text("".join(log_lines), encoding="utf-8")
        hp_calls = {"n": 0}
        shot = {"done": False}

        class Rcon:
            def cmd(self, command):
                if rseen is not None:
                    rseen.append(command)
                if command == "list":
                    if dies_on_shoot and shot["done"]:
                        raise OSError("rcon closed: server gone")
                    return "players: hitbot"
                if command.startswith("data get entity hitbot Dimension"):
                    # The server-side dimension agrees with the testbot
                    # cache: the range-enter path verifies here, so a fake
                    # stuck out of range would shadow every victim-leg
                    # behaviour with range-enter-failed.
                    return f'hitbot has the following entity data: "{dimension}"'
                if "execute if block" in command:
                    # The lane pad exists in a loaded chunk: the footing
                    # check passes at once, no fill loop.
                    return "Test passed"
                if "sweep_victim" in command or "name=A10a" in command:
                    if "Health" in command:
                        hp_calls["n"] += 1
                        if victim_hp:
                            hp = victim_hp[min(hp_calls["n"], len(victim_hp)) - 1]
                            if hp is None:
                                return "ok"
                            return f"hitbot has Health: {hp}f"
                        return "ok"
                    if "Pos" in command:
                        return "[10.0d, 64.0d, 10.0d]" if victim_hp else "ok"
                if "GunCurrentAmmoCount" in command:
                    return f"data: {ammo[0] if 'Hotbar' not in command else ammo[0]}"
                if "Inventory[{Slot:0b}]" in command:
                    return gun_nbt
                if command.startswith("data get entity hitbot Health"):
                    return "hitbot has the following entity data: 20.0f"
                if command == "data get entity hitbot Pos":
                    return f"hitbot has the following entity data: {player_pos}"
                if command == "kill hitbot":
                    return "Killed hitbot"
                if command.startswith("execute if items"):
                    if "give" in " ".join([]):
                        return "Test passed"
                    return f"Test passed, count: {1 if have_item else 0}"
                return "ok"

        def bot(command):
            if seen is not None:
                seen.append(command)
            if command == "state":
                return {
                    "name": "hitbot",
                    "held": "tacz:modern_kinetic_gun",
                    "dimension": dimension,
                }
            if command == "tacz shoot":
                if fail_shoot:
                    raise OSError("bot lost: server gone")
                shot["done"] = True
                return {"result": shoot_result}
            return {"ok": True}

        return Rcon(), bot, log_path

    def test_tacz_missing_client_gun_data_is_fail(self):
        # Fail-before for server#365 c5: the server gives a gun id the stand
        # client never loaded (elitex:ak47x class) - that row must FAIL, so a
        # sweep can never report it as inconclusive and move on.
        rcon, bot, log_path = self.make_pair(
            ammo=(300, 300), shoot_result="ID_NOT_EXIST"
        )
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:ak47x", log_path, 0
        )
        self.assertEqual(stop, "client-gun-data-missing")
        self.assertTrue(detail.get("gave"))
        self.assertFalse(detail.get("fired"))

    def test_tacz_missing_client_gun_data_reports_server_crash_first(self):
        # elitex:ak47x on an unfixed server: the client reports ID_NOT_EXIST
        # and the server dies; the crash must win over the data verdict.
        rcon, bot, log_path = self.make_pair(
            ammo=(300, 300), shoot_result="ID_NOT_EXIST", dies_on_shoot=True
        )
        stop, _, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:ak47x", log_path, 0
        )
        self.assertEqual(stop, "server-dead-after-single")

    def test_tacz_transient_unknown_fail_stays_inconclusive(self):
        # UNKNOWN_FAIL is client data lag, not a data mismatch: still
        # inconclusive when no round ever leaves the magazine. The stable
        # victim keeps the test on the UNKNOWN_FAIL question: without a
        # readable control the row now stops at control-unreadable.
        rcon, bot, log_path = self.make_pair(
            ammo=(300, 300),
            shoot_result="UNKNOWN_FAIL",
            victim_hp=(20.0, 20.0, 20.0, 20.0),
        )
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:ak47x", log_path, 0
        )
        self.assertEqual(stop, "inconclusive-no-shot")
        self.assertFalse(detail.get("fired"))

    def test_tacz_happy_path_fires_and_survives(self):
        # A PASS needs a positively read unfired control AND a positively
        # read change, so the happy path sets up a stable range target.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0),
        )
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
        )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("fired"))
        self.assertEqual(detail.get("victim_control"), [20.0, 20.0])

    def test_tacz_missing_control_never_passes(self):
        # Same rule on the TaCZ leg: no readable unfired control, no PASS,
        # even when the client reports a successful shot. The pre-fire drain
        # stays in the detail as evidence, but the row cannot pass on it.
        rcon, bot, log_path = self.make_pair()
        with (
            patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_TIMEOUT", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertEqual(stop, "control-unreadable")
        self.assertTrue(detail.get("victim_control_unreadable"))
        self.assertIsNone(detail.get("victim_target_settled"))

    def test_minigun_early_failure_restores_survival(self):
        # Review finding r4520378381: the creative leg must restore survival
        # on every exit, including an early give failure.
        rseen = []
        rcon, bot, log_path = self.make_pair(have_item=False, rseen=rseen)
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon,
            bot,
            "hitbot",
            "tacz",
            "tacz:minigun",
            log_path,
            0,
            inv_ammo="tacz:308",
        )
        self.assertEqual(stop, "server-give-failed")
        self.assertTrue(
            any(cmd.startswith("gamemode survival") for cmd in rseen),
            rseen,
        )

    def test_respawn_failure_is_fail(self):
        # Audit: the kill landed but the bot never came back with HP must
        # fail loudly, never fall through into a passing row.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0),
        )
        with (
            patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "RESPAWN_TIMEOUT", 0),
            patch.object(weapons_sweep, "health", lambda *a: 0.0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertEqual(stop, "respawn-failed")
        self.assertFalse(detail.get("respawned"))

    def test_sbw_unloaded_mag_missing_victim_is_control_unreadable(self):
        # Fail-before for the review finding: the old code hard-coded
        # fired=True, so this returned None (PASS) with no shot evidence.
        # The mag never loads here and the victim is missing, so the
        # unreadable control fails loudly instead of passing.
        rcon, bot, log_path = self.make_pair()
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertEqual(stop, "control-unreadable")
        self.assertFalse(detail.get("fired"))

    def test_sbw_missing_control_never_passes(self):
        # Review finding r4520378377: the old baseline returned stable with
        # a None victim, so a later magazine drain passed the row with no
        # unfired control. A PASS needs a positively read control AND a
        # positively read change.
        rcon, bot, log_path = self.make_pair(
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "SBW_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 25)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertEqual(stop, "control-unreadable")
        self.assertFalse(detail.get("fired"))

    def test_tacz_midshot_drop_confirms_hit(self):
        # The settle consumes the first two reads; the tuple leads with a
        # stable target so the control still lands on [20, 20].
        rcon, bot, log_path = self.make_pair(victim_hp=(20.0, 20.0, 20.0, 20.0, 15.0))
        with (
            patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("victim_hit"))
        self.assertEqual(detail.get("victim_hp"), [20.0, 15.0, 15.0])

    def test_tacz_flickering_target_settles_before_control(self):
        # Fail-before for server#365 stand365: the A10a husk flickers
        # present -> absent right after the range opens while the sweep reads
        # it immediately, so 91 TaCZ rows stopped control-unreadable with 0
        # crashes. The control now waits bounded for two equal reads first:
        # a target that settles reads a normal control and the row can pass.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(None, None, None, None, 20.0),
        )
        with (
            patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertEqual(detail.get("victim_target_settled"), 20.0)
        self.assertEqual(detail.get("victim_control"), [20.0, 20.0])

    def test_tacz_target_never_settling_is_control_unreadable(self):
        # A target that never stabilises within the bound stays loud
        # (control-unreadable), never a pass, even with a successful shot.
        rcon, bot, log_path = self.make_pair()
        with (
            patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_TIMEOUT", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertEqual(stop, "control-unreadable")
        self.assertTrue(detail.get("victim_control_unreadable"))
        self.assertIsNone(detail.get("victim_target_settled"))

    def test_run_start_target_check_waits_for_a_flickering_target(self):
        # stand365: right after the range opens the husk reads absent a few
        # times; the run-start check must settle, not abort the whole run.
        reads = iter([None, None, 20.0, 20.0])
        with (
            patch.object(weapons_sweep, "victim_health", lambda *a: next(reads)),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            self.assertTrue(weapons_sweep.require_target(None, "hitbot"))
        with (
            patch.object(weapons_sweep, "victim_health", lambda *a: None),
            patch.object(weapons_sweep, "TARGET_SETTLE_TIMEOUT", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            self.assertFalse(weapons_sweep.require_target(None, "hitbot"))

    def test_tacz_unfired_drop_fails_loudly(self):
        # The drop lands in the control window (after the settle): a target
        # that loses HP unfired still fails loudly, never passes.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 10.0, 20.0, 10.0)
        )
        with (
            patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0),
            patch.object(weapons_sweep, "TARGET_SETTLE_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertEqual(stop, "victim-died-unfired")

    def test_rcon_read_retries_transient(self):
        calls = {"n": 0}

        class Flaky:
            def cmd(self, command):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise OSError("lag spike")
                return "ok"

        with patch.object(weapons_sweep, "READ_RETRY_SLEEP", 0):
            self.assertEqual(weapons_sweep._rcon_read(Flaky(), "x"), "ok")
        self.assertEqual(calls["n"], 3)

    def test_rcon_read_gives_up(self):
        class Dead:
            def cmd(self, command):
                raise OSError("gone")

        with patch.object(weapons_sweep, "READ_RETRY_SLEEP", 0):
            self.assertRaises(OSError, weapons_sweep._rcon_read, Dead(), "x")

    def test_require_target(self):
        rcon, _, _ = self.make_pair(victim_hp=(20.0,))
        self.assertTrue(weapons_sweep.require_target(rcon, "hitbot"))
        rcon, _, _ = self.make_pair()
        self.assertFalse(weapons_sweep.require_target(rcon, "hitbot"))

    def test_sweep_takes_firing_position(self):
        # The TaCZ victim leg teleports onto the A10a lane before the shots.
        rcon, bot, log_path = self.make_pair(victim_hp=(20.0, 20.0, 20.0, 20.0))
        with patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertEqual(detail.get("victim_lane"), [-18.5, 65.0, 2.5])

    def test_unreachable_lane_fails_loudly(self):
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0),
            player_pos="[0.0d, 65.0d, 0.0d]",
        )
        with patch.object(weapons_sweep, "UNFIRED_CONTROL_SLEEP", 0):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
        self.assertEqual(stop, "victim-lane-unreachable")

    def test_pad_footing_uses_floor_coordinates_and_refills(self):
        # Review r4524578844: int(-18.5) truncates to -18 but the block
        # under the player is x=-19. The footing check must read the
        # floored block and refill the pad when that block is missing.
        seen = []
        reads = {"n": 0}

        class Rcon:
            def cmd(self, command):
                seen.append(command)
                if "execute if block" in command:
                    if "-19 64 2" not in command:
                        return "Test failed"
                    reads["n"] += 1
                    return "Test failed" if reads["n"] == 1 else "Test passed"
                return "ok"

        with patch("time.sleep", return_value=None):
            self.assertTrue(weapons_sweep.pad_ready(Rcon(), timeout=5.0))
        self.assertTrue(any("-19 64 2" in cmd for cmd in seen))
        self.assertFalse(any("-18 64 2" in cmd for cmd in seen))
        self.assertTrue(any(" fill " in cmd for cmd in seen))

    def test_range_enter_final_attempt_is_verified(self):
        # Review r4524578855: the retry loop's for/else returned failure
        # without reading the result of the last range:enter attempt.
        enters = {"n": 0}

        class Rcon:
            def cmd(self, command):
                if command.startswith("data get entity hitbot Dimension"):
                    if enters["n"] >= 6:
                        return (
                            'hitbot has the following entity data: "blockfield:range"'
                        )
                    return 'hitbot has the following entity data: "minecraft:overworld"'
                if command.startswith("data get entity hitbot Health"):
                    return "hitbot has the following entity data: 20.0f"
                return "ok"

        def bot(command):
            if command == "state":
                return {
                    "name": "hitbot",
                    "held": "tacz:modern_kinetic_gun",
                    "dimension": "minecraft:overworld",
                }
            if command == "bfaction range:enter":
                enters["n"] += 1
            return {"ok": True}

        with patch("time.sleep", return_value=None):
            self.assertTrue(weapons_sweep.ensure_in_range(bot, Rcon(), "hitbot"))
        self.assertEqual(enters["n"], 6)

    def test_victim_is_the_range_husk_target(
        self,
    ):  # Summoned zombies are deleted by the range within a second, so the
        # leg must use the range's own husk target, never a summon.
        self.assertEqual(
            weapons_sweep.VICTIM_SELECTOR,
            "@e[type=minecraft:husk,name=A10a,limit=1]",
        )

    def test_sweep_never_kills_pinned_range_target(self):
        # PR #72 review: clear_victims() used to `kill` the shared A10a husk
        # at run start/end. Only the per-run SBW victims (the sweep's own
        # summon tag) may be killed; the pinned fixture must never be.
        seen = []
        rcon, _, _ = self.make_pair(rseen=seen)
        weapons_sweep.clear_sbw_victims(rcon)
        kills = [c for c in seen if c.startswith("kill ")]
        self.assertEqual(kills, [f"kill {weapons_sweep.SBW_VICTIM_SELECTOR}"])
        self.assertNotIn("A10a", " ".join(kills))

    def test_sbw_hp_drop_without_drain_is_inconclusive(self):
        # Coordinator decision (server#365 c9): the fired verdict is the
        # server-side magazine drain with the server alive; a victim HP drop
        # with no drain is secondary evidence, never a PASS.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 10.0, 10.0),
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 30)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertEqual(stop, "inconclusive-no-shot")
        self.assertFalse(detail.get("fired"))
        self.assertTrue(detail.get("victim_hit"))
        self.assertEqual(weapons_sweep.SBW_VICTIM_AHEAD, 4)

    def test_sbw_unreadable_mag_is_not_drained(self):
        # Review finding r4520378372: `data get` timing out through the whole
        # victim leg while `list` still works must not read as "fired to
        # empty". Every leg read fails (gun_nbt has no entity data), so the
        # row fails loudly instead of passing.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0, 20.0),
            gun_nbt="ok",
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(1, None)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:bocek", log_path, 0
            )
        self.assertEqual(stop, "ammo-unreadable")
        self.assertFalse(detail.get("fired"))
        self.assertTrue(detail.get("ammo_unreadable"))

    def test_sbw_emptied_mag_counts_as_drained(self):
        # Stand shape (server#365 stand365, one-round SBW gun m_79 fired
        # once): the item keeps its GunData but the Ammo number is gone.
        # Fail-before: the old code read this as ammo-unreadable.
        emptied = (
            "hitbot has the following entity data: {count: 1, Slot: 0b, "
            'components: {"minecraft:custom_data": '
            "{GunData: {UUID: [I; 1, 2, 3, 4], CloseStrike: 1b}}}, "
            'id: "superbwarfare:m_79"}'
        )
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0, 20.0),
            gun_nbt=emptied,
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(1, None)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:m_79", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("fired"))
        self.assertTrue(detail.get("ammo_emptied"))

    def test_sbw_swapped_item_is_never_emptied(self):
        # The emptied shape counts only for the weapon under test: a
        # different item in the slot with no Ammo number is unknown, never
        # this gun's drain.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0, 20.0),
            gun_nbt='hitbot has the following entity data: {id: "minecraft:stick"}',
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(1, None)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:m_79", log_path, 0
            )
        self.assertEqual(stop, "ammo-unreadable")
        self.assertFalse(detail.get("fired"))
        self.assertTrue(detail.get("ammo_unreadable"))

    def test_sbw_emptied_after_loaded_mid_counts_as_drained(self):
        # Bot-review finding on PR #75: the emptied shape must also count
        # when an earlier victim-leg read still caught the loaded count
        # (mid sees the round, the shot lands after). Fail-before: the old
        # guard required no numeric leg read at all, so this stayed
        # inconclusive-no-shot.
        emptied = (
            "hitbot has the following entity data: {count: 1, Slot: 0b, "
            'components: {"minecraft:custom_data": '
            "{GunData: {UUID: [I; 1, 2, 3, 4], CloseStrike: 1b}}}, "
            'id: "superbwarfare:m_79"}'
        )
        seen = {"n": 0}

        def mag(rcon, player, weapon=None):
            seen["n"] += 1
            return 1 if seen["n"] <= 7 else None

        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0, 20.0),
            gun_nbt=emptied,
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", mag),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:m_79", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("fired"))
        self.assertTrue(detail.get("ammo_emptied"))

    def test_sbw_ammo_counts_only_the_weapon_under_test(self):
        # Review r4521765286: every read used for the verdict checks the
        # item id, so another gun's lower count is never this gun's drain.
        class Reply:
            def __init__(self, text):
                self.text = text

            def cmd(self, command):
                return self.text

        head = "hitbot has the following entity data: "
        own = Reply(head + '{id: "superbwarfare:ak_12", GunData: {Ammo: 30}}')
        other = Reply(head + '{id: "superbwarfare:glock_17", GunData: {Ammo: 3}}')
        stick = Reply(head + '{id: "minecraft:stick"}')
        ammo = weapons_sweep.sbw_ammo
        self.assertEqual(ammo(own, "hitbot", "superbwarfare:ak_12"), 30)
        self.assertIsNone(ammo(other, "hitbot", "superbwarfare:ak_12"))
        self.assertIsNone(ammo(stick, "hitbot", "superbwarfare:ak_12"))
        self.assertIsNone(ammo(Reply("ok"), "hitbot", "superbwarfare:ak_12"))

    def test_sbw_verdict_reads_pass_the_weapon_id(self):
        # The leg must hand the weapon id to every ammo read.
        seen = []

        def spy(rcon, player, weapon=None):
            seen.append(weapon)
            return 30

        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 20.0, 20.0),
            gun_nbt="ok",
            dimension="minecraft:overworld",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", spy),
        ):
            weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"superbwarfare:ak_12"})

    def test_passed_without_hit_names_unexercised_hit_rows(self):
        # Review r4521831498: a PASS proves give+fire with the server alive,
        # not that a shot reached the victim; those rows are named.
        rows = [
            {"weapon": "b", "stop": None, "victim_hit": False},
            {"weapon": "a", "stop": None},
            {"weapon": "c", "stop": None, "victim_hit": True},
            {"weapon": "d", "stop": "inconclusive-no-shot", "victim_hit": False},
        ]
        self.assertEqual(weapons_sweep.passed_without_hit(rows), ["a", "b"])

    def test_sbw_settle_waits_for_full_mag(self):
        # The top-up keeps filling after the first round lands; the
        # pre-shot snapshot must wait for two agreeing reads.
        reads = iter([15, 20, 25, 30, 30])
        with patch.object(weapons_sweep, "sbw_ammo", lambda *a: next(reads)):
            detail = {}
            self.assertEqual(weapons_sweep.sbw_settle_ammo(None, "hitbot", detail), 30)
            self.assertEqual(detail.get("settled_ammo"), 30)

    def test_sbw_mag_drain_confirms_shot(self):
        # Coordinator decision (server#365 c9): the magazine drain is the
        # fired verdict; the victim HP drop is recorded as secondary.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 10.0, 10.0),
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 25)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("fired"))
        self.assertEqual(
            [detail.get("ammo_before_victim"), detail.get("ammo_after_victim")],
            [30, 25],
        )
        self.assertEqual(detail.get("ammo_mid_victim"), 25)
        self.assertEqual(detail.get("ammo_low_victim"), 25)
        self.assertTrue(detail.get("victim_hit"))
        self.assertEqual(detail.get("victim_hp"), [20.0, 10.0, 10.0])
        self.assertEqual(detail.get("reloaded_ammo"), 30)

    def test_victim_killed_without_drain_is_inconclusive(self):
        # A dead victim with no magazine drain is secondary evidence only:
        # the row is inconclusive, never a PASS.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, None, None),
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 30)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertEqual(stop, "inconclusive-no-shot")
        self.assertFalse(detail.get("fired"))
        self.assertTrue(detail.get("victim_killed"))
        self.assertEqual(detail.get("victim_hp"), [20.0, None, None])

    def test_sbw_fires_through_rawinput(self):
        # SBW listens to raw press events (ClickEventHandler) and its own
        # FIRE mapping: KeyMapping-only attack/hold never reaches it (c7).
        seen = []
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 10.0, 10.0),
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
            seen=seen,
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 25)),
        ):
            stop, _, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertTrue(any("rawinput" in cmd for cmd in seen))
        self.assertFalse(any("hold key.attack" in cmd for cmd in seen))

    def test_sbw_unfired_hp_drop_is_fail(self):
        # HP loss with no shot must never count as fired evidence.
        rcon, bot, log_path = self.make_pair(victim_hp=(20.0, 20.0, 10.0))
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertEqual(stop, "victim-died-unfired")
        self.assertFalse(detail.get("fired"))

    def test_sbw_finds_flat_spot(self):
        # First candidate is flat (HP 20, level): no teleport needed.
        rcon, bot, _ = self.make_pair(
            victim_hp=(20.0, 20.0), dimension="minecraft:overworld"
        )
        found, detail = weapons_sweep.find_flat_spot(bot, rcon, "hitbot")
        self.assertTrue(found)
        self.assertEqual(len(detail["tried"]), 1)

    def test_sbw_recenters_before_victim(self):
        # The moving phase walks the bot away every weapon: the victim leg
        # teleports back onto the verified flat spot first, loudly on failure.
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 10.0, 10.0),
            dimension="minecraft:overworld",
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 25)),
        ):
            stop, detail, _ = weapons_sweep.sweep_weapon(
                rcon,
                bot,
                "hitbot",
                "sbw",
                "superbwarfare:ak_12",
                log_path,
                0,
                "",
                [-18.5, 65.0, 2.5],
            )
        self.assertIsNone(stop)
        self.assertTrue(detail.get("recentered"))

    def test_sbw_stays_out_of_range(self):
        # The SBW leg never enters the range: no lane teleport, no A10a leg.
        seen = []
        rcon, bot, log_path = self.make_pair(
            victim_hp=(20.0, 20.0, 20.0, 10.0, 10.0),
            dimension="minecraft:overworld",
            seen=seen,
            gun_nbt="hitbot has the following entity data: {GunData: {Ammo: 30}}",
        )
        with (
            patch.object(weapons_sweep, "SBW_RELOAD_TIMEOUT", 0),
            patch.object(weapons_sweep, "SBW_RELOAD_POLL", 0),
            patch.object(weapons_sweep, "sbw_ammo", _ammo_script(30, 25)),
        ):
            stop, _, _ = weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
            )
        self.assertIsNone(stop)
        self.assertFalse(any("range" in cmd for cmd in seen))

    def test_sbw_persistence_proof_blocks_deleted_mobs(self):
        # A dimension that deletes foreign mobs aborts the SBW leg loudly.
        old, weapons_sweep.SBW_PERSISTENCE_SLEEP = (
            weapons_sweep.SBW_PERSISTENCE_SLEEP,
            0,
        )
        try:
            rcon, bot, _ = self.make_pair(victim_hp=(20.0, None))
            ok, _ = weapons_sweep.verify_sbw_persistence(rcon, "hitbot")
            self.assertFalse(ok)
            rcon, bot, _ = self.make_pair(victim_hp=(20.0, 20.0, 20.0))
            ok, _ = weapons_sweep.verify_sbw_persistence(rcon, "hitbot")
            self.assertTrue(ok)
        finally:
            weapons_sweep.SBW_PERSISTENCE_SLEEP = old

    def test_resume_skips_only_passed_weapons(self):
        prev = [
            {"weapon": "a", "stop": None},
            {"weapon": "b", "stop": "inconclusive-no-shot"},
            {"weapon": "c", "stop": "server-log-exception"},
        ]
        self.assertEqual(weapons_sweep.passes_to_skip(prev), {"a"})
        merged, passed, inconclusive, failed = weapons_sweep.merged_verdict(
            prev, [{"weapon": "c", "stop": None}]
        )
        self.assertEqual((passed, inconclusive, failed), (2, 1, 0))
        self.assertEqual(len(merged), 3)

    def test_new_log_lines_unreadable(self):
        lines, pos = weapons_sweep.new_log_lines(
            Path(tempfile.mkdtemp()) / "missing" / "latest.log", 0
        )
        self.assertIsNone(lines)
        self.assertEqual(pos, 0)

    def test_unreadable_log_is_fail(self):
        log_path = Path(tempfile.mkdtemp()) / "missing" / "latest.log"
        rcon, bot, _ = self.make_pair(victim_hp=(20.0, 20.0, 20.0, 20.0))
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
        )
        self.assertEqual(stop, "server-log-unreadable")
        self.assertTrue(detail.get("log_unreadable"))

    def test_sbw_give_failure_is_fail(self):
        rcon, bot, log_path = self.make_pair(have_item=False)
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "sbw", "superbwarfare:ak_12", log_path, 0
        )
        self.assertEqual(stop, "server-give-failed")
        self.assertFalse(detail.get("gave"))

    def test_tacz_give_failure_is_fail(self):
        rcon, bot, log_path = self.make_pair(have_item=False)
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
        )
        self.assertEqual(stop, "server-give-failed")
        self.assertFalse(detail.get("gave"))

    def test_server_log_exception_stops_the_weapon(self):
        rcon, bot, log_path = self.make_pair(
            log_lines=["[Server thread/ERROR]: java.lang.RuntimeException: boom\n"],
            victim_hp=(20.0, 20.0, 20.0, 20.0),
        )
        stop, detail, _ = weapons_sweep.sweep_weapon(
            rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
        )
        self.assertEqual(stop, "server-log-exception")
        self.assertTrue(detail["log_hits"])

    def test_lost_bot_aborts_the_weapon_loudly(self):
        rcon, bot, log_path = self.make_pair(fail_shoot=True)
        with self.assertRaises(OSError):
            weapons_sweep.sweep_weapon(
                rcon, bot, "hitbot", "tacz", "elitex:benelli_super", log_path, 0
            )
