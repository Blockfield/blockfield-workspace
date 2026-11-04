"""Hermetic checks of the pose overlay's offset arithmetic; no game, no sockets."""

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from scenarios import pose_overlay  # noqa: E402

FRAME_NS = 20_000_000


def lane(speed_per_frame=0.1, frames=200, turn=None):
    """Frames of a target moving along X, reversing at frame `turn`."""
    out, x, direction = [], 0.0, 1
    for i in range(frames):
        if i == turn:
            direction = -1
        x += speed_per_frame * direction
        out.append((i * FRAME_NS, x, direction))
    return out


class DirectionTest(unittest.TestCase):
    def test_steady_run_has_its_sign(self):
        rows = lane()
        directions = pose_overlay.steady_directions(
            [t for t, _, _ in rows], [x for _, x, _ in rows]
        )
        self.assertEqual(set(directions[1:]), {1})

    def test_frames_near_a_reversal_are_dropped(self):
        rows = lane(turn=100)
        directions = pose_overlay.steady_directions(
            [t for t, _, _ in rows], [x for _, x, _ in rows]
        )
        guard = pose_overlay.TURN_GUARD_NS // FRAME_NS
        self.assertIsNone(directions[100])
        self.assertIsNone(directions[100 - guard + 1])
        self.assertEqual(directions[100 + guard + 2], -1)
        self.assertEqual(directions[50], 1)


class TargetSummaryTest(unittest.TestCase):
    def frames(self, turn=None):
        frames = []
        for t, x, direction in lane(turn=turn):
            frames.append(
                {
                    "ns": t,
                    "dtMs": FRAME_NS / 1e6,
                    "r": 100.0,
                    "h": 103,
                    "targets": [
                        {
                            "id": 7,
                            "drawn": [x, 0, 0],
                            "live": [x + 1.1 * direction, 0, 0],
                            "offStart": [0.0, 0, 0],
                            "offImpact": [0.56 * direction, 0, 0],
                            "labelImpact": "view:rewound:incomplete",
                            "flight": 2,
                        }
                    ],
                }
            )
        return frames

    def test_offsets_are_signed_along_the_run_both_ways(self):
        for turn in (None, 100):
            report = pose_overlay.summarize_target(self.frames(turn))
            self.assertEqual(report["live_ahead_of_drawn_m"]["median"], 1.1)
            self.assertEqual(report["rewind_start_ahead_of_drawn_m"]["median"], 0)
            self.assertEqual(report["rewind_impact_ahead_of_drawn_m"]["median"], 0.56)
            self.assertEqual(report["flight_ticks"].keys(), {2})
            self.assertEqual(report["fps"], 50)
            self.assertEqual(report["h_minus_r_ticks"]["median"], 3)


class SelfSummaryTest(unittest.TestCase):
    def test_a_backward_step_of_the_camera_is_counted(self):
        frames = []
        for i, (t, x, _) in enumerate(lane()):
            jolt = -0.25 if i == 100 else 0
            frames.append(
                {
                    "ns": t,
                    "dtMs": 20,
                    "self": {
                        "pred": [x, 0, 0],
                        "cam": [x + jolt, 1.6, 0],
                        "live": [x - 0.2, 0, 0],
                        "teleports": 3,
                    },
                }
            )
        report = pose_overlay.summarize_self(frames, position_packets=0)
        self.assertEqual(report["pred_backsteps"]["count"], 0)
        self.assertEqual(report["camera_backsteps"]["count"], 1)
        self.assertAlmostEqual(report["camera_backsteps"]["max_m"], 0.15, places=2)
        self.assertEqual(report["pred_ahead_of_server_live_m"]["median"], 0.2)
        self.assertEqual(report["server_corrections_or_teleports"], 0)


class LoadedModTest(unittest.TestCase):
    LOG = (
        "[00:23:24] [main/INFO]: Loading 162 mods:\n"
        "\t- accessories 1.1.0-beta.53+1.21.1\n"
        "\t- blockfield 1.57.0+mc1.21.1\n"
        "\t   |-- fantasy 0.6.4+1.21\n"
        "\t- blockfield_decor 1.2.1+mc1.21.1\n"
    )

    def test_reads_the_version_the_loader_listed(self):
        self.assertEqual(pose_overlay.loaded_mod(self.LOG), "1.57.0+mc1.21.1")
        self.assertIsNone(pose_overlay.loaded_mod("no mod list yet"))

    def test_a_stale_jar_stops_the_stand(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory) / "server.log"
            client = Path(directory) / "client.log"
            server.write_text(self.LOG)
            client.write_text(self.LOG.replace("1.57.0", "1.55.3"))
            loaded = pose_overlay.require_mod("1.57.0+mc1.21.1", [server])
            self.assertEqual(loaded[str(server)], "1.57.0+mc1.21.1")
            with self.assertRaises(SystemExit) as stopped:
                pose_overlay.require_mod("1.57.0+mc1.21.1", [server, client])
            self.assertIn("1.55.3", str(stopped.exception))
            self.assertNotIn(str(server), str(stopped.exception))

    def test_a_missing_list_also_stops_it(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "empty.log"
            log.write_text("")
            with self.assertRaises(SystemExit):
                pose_overlay.require_mod("1.57.0+mc1.21.1", [log])

    def test_unset_expect_mod_skips_the_guard(self):
        # The documented `setup --map dust2` (no --expect-mod) used to abort
        # with "loaded mod differs from None" before the room opened.
        args = SimpleNamespace(expect_mod=None, server_log="s", client_logs="c")
        self.assertEqual(
            pose_overlay.check_mod(args), {"mod": "unchecked (no --expect-mod)"}
        )

    def test_set_expect_mod_still_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            server = Path(directory) / "server.log"
            server.write_text(LoadedModTest.LOG)
            args = SimpleNamespace(
                expect_mod="1.57.0+mc1.21.1",
                server_log=str(server),
                client_logs=str(server),
            )
            self.assertEqual(
                pose_overlay.check_mod(args), {str(server): "1.57.0+mc1.21.1"}
            )


class SharedBotTest(unittest.TestCase):
    def test_two_threads_never_overlap_on_the_single_connection(self):
        active, peak = [0], [0]

        def bot(_command):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.001)
            active[0] -= 1
            return {}

        shared = pose_overlay.Shared(bot)
        threads = [
            threading.Thread(target=lambda: [shared("state") for _ in range(20)])
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(peak[0], 1)


class LaneTest(unittest.TestCase):
    DUST2 = {"id": "dust2", "minX": -79, "maxX": 44, "minZ": -20, "maxZ": 109}

    def test_the_lane_stays_inside_the_match_bounds(self):
        centre, runner_z, watcher_z = pose_overlay.lane_of(self.DUST2, 60)
        self.assertGreaterEqual(centre - 26, -79)
        self.assertLessEqual(centre + 26, 44)
        self.assertEqual(runner_z - watcher_z, 60)
        self.assertGreaterEqual(watcher_z - 3, -20)

    def test_a_lane_longer_than_the_map_stops_the_run(self):
        with self.assertRaises(SystemExit):
            pose_overlay.lane_of(self.DUST2, 120)


class RunningLaneTest(unittest.TestCase):
    CENTRE, Z = 10.0, 99.5

    def bot_at(self, positions):
        seen = iter(positions)
        return lambda command: {"pos": next(seen)}

    def test_a_stationary_runner_rejects_the_cell(self):
        with self.assertRaises(SystemExit):
            pose_overlay.require_running_lane(
                self.bot_at([[10, 151, 99.5], [10, 151, 99.5]]),
                self.CENTRE,
                self.Z,
                "cell",
                settle_s=0,
            )

    def test_a_runner_parked_off_the_lane_rejects_the_cell(self):
        with self.assertRaises(SystemExit):
            pose_overlay.require_running_lane(
                self.bot_at([[200, 64, 200], [206, 64, 200]]),
                self.CENTRE,
                self.Z,
                "cell",
                settle_s=0,
            )

    def test_a_runner_at_spawn_with_matching_x_rejects_the_cell(self):
        # Dust2 spawn can share the lane's X band: Z/Y must reject it.
        with self.assertRaises(SystemExit):
            pose_overlay.require_running_lane(
                self.bot_at([[10, 64, 200], [16, 64, 200]]),
                self.CENTRE,
                self.Z,
                "cell",
                settle_s=0,
            )

    def test_a_running_runner_passes(self):
        pose_overlay.require_running_lane(
            self.bot_at([[8, 151, 99.5], [14, 151, 99.5]]),
            self.CENTRE,
            self.Z,
            "cell",
            settle_s=0,
        )


class ServerPosTest(unittest.TestCase):
    def reply(self, text):
        class FakeRcon:
            def cmd(self, command):
                self.sent = command
                return text

        return FakeRcon()

    def test_parses_doubles(self):
        rcon = self.reply(
            "HrRunner has the following entity data: [12.5d, 151.0d, -3.25d]"
        )
        self.assertEqual(
            pose_overlay.server_pos(rcon, "HrRunner"), [12.5, 151.0, -3.25]
        )

    def test_parses_plain_ints(self):
        rcon = self.reply("e has the following entity data: [12, 65, -3]")
        self.assertEqual(pose_overlay.server_pos(rcon, "e"), [12.0, 65.0, -3.0])

    def test_missing_entity_is_none(self):
        rcon = self.reply("No entity was found")
        self.assertIsNone(pose_overlay.server_pos(rcon, "ghost"))


class EnsurePlacedTest(unittest.TestCase):
    LANES = {"shooter": (10.0, -20.5), "runner": (10.0, 39.5)}

    def test_already_on_lane_never_replaces(self):
        places = []
        get = lambda: {"shooter": [10.0, 151, -20.5], "runner": [9.0, 151, 39.5]}
        self.assertTrue(
            pose_overlay.ensure_placed(
                lambda: places.append(1),
                get,
                self.LANES,
                timeout=0,
                interval=0,
            )
        )
        self.assertEqual(places, [])

    def test_matching_x_at_spawn_still_replaces(self):
        places = []
        get = lambda: {"shooter": [10.0, 151, -20.5], "runner": [9.0, 64, 200.0]}
        self.assertFalse(
            pose_overlay.ensure_placed(
                lambda: places.append(1),
                get,
                self.LANES,
                timeout=0,
                interval=0,
            )
        )
        self.assertEqual(len(places), 1)

    def test_off_lane_replaces_once_then_passes(self):
        places = []
        on = {"value": False}

        def place():
            places.append(1)
            on["value"] = True

        def get():
            x = 9.0 if on["value"] else 200.0
            return {"shooter": [10.0, 151, -20.5], "runner": [x, 151, 39.5]}

        self.assertTrue(
            pose_overlay.ensure_placed(place, get, self.LANES, timeout=0, interval=0)
        )
        self.assertEqual(len(places), 1)

    def test_still_off_lane_after_replace_fails(self):
        places = []
        get = lambda: {"shooter": [10.0, 151, -20.5], "runner": [200.0, 64, 200.0]}
        self.assertFalse(
            pose_overlay.ensure_placed(
                lambda: places.append(1),
                get,
                self.LANES,
                timeout=0,
                interval=0,
            )
        )
        self.assertEqual(len(places), 1)


class WaitSettledTest(unittest.TestCase):
    DIM = "blockfield:dust2"

    def test_returns_once_server_positions_stop_moving(self):
        states = lambda command: {"dimension": self.DIM}  # noqa: E731
        script = {
            "shooter": [[0.0, 65, 0.0], [5.0, 65, 0.0]],
            "runner": [[1.0, 65, 0.0]],
        }

        class ScriptRcon:
            def cmd(self, command):
                name = command.split("entity ")[1].split(" Pos")[0]
                positions = script[name]
                pos = positions.pop(0) if len(positions) > 1 else positions[0]
                return f"{name} has the following entity data: [{pos[0]}d, {pos[1]}d, {pos[2]}d]"

        settled = pose_overlay.wait_settled(
            ScriptRcon(), states, states, ("shooter", "runner"), timeout=5, interval=0
        )
        self.assertEqual(settled["shooter"], [5.0, 65.0, 0.0])

    def test_pending_teleport_keeps_waiting(self):
        states = lambda command: {"dimension": self.DIM}  # noqa: E731
        calls = {"n": 0}

        class FlipRcon:
            def cmd(self, command):
                # A teleport still in flight: the server pos never repeats.
                calls["n"] += 1
                return (
                    f"e has the following entity data: [{calls['n'] * 10.0}d, 65d, 0d]"
                )

        with self.assertRaises(TimeoutError):
            pose_overlay.wait_settled(
                FlipRcon(), states, states, ("a", "b"), timeout=0.2, interval=0
            )

    def test_undeployed_waits_for_the_match_dimension(self):
        overworld = lambda command: {"dimension": "minecraft:overworld"}  # noqa: E731

        class UnusedRcon:
            def cmd(self, command):
                raise AssertionError("no server query while undeployed")

        with self.assertRaises(TimeoutError):
            pose_overlay.wait_settled(
                UnusedRcon(), overworld, overworld, ("a", "b"), timeout=0.2, interval=0
            )


class LaneEndTest(unittest.TestCase):
    CENTRE, Z = 10.0, 99.5

    def test_a_clean_run_passes(self):
        self.assertIsNone(
            pose_overlay.lane_end_error(
                "cell", [9.0, 151, 99.5], self.CENTRE, self.Z, 5.6, 0
            )
        )

    def test_a_runner_teleported_off_the_lane_fails(self):
        error = pose_overlay.lane_end_error(
            "cell", [10.0, 64, 200.0], self.CENTRE, self.Z, 5.6, 0
        )
        self.assertIn("off the lane", error)

    def test_a_runner_stopped_mid_cell_fails(self):
        error = pose_overlay.lane_end_error(
            "cell", [9.0, 151, 99.5], self.CENTRE, self.Z, 0.1, 0
        )
        self.assertIn("stopped", error)

    def test_a_runner_corrected_mid_cell_fails(self):
        error = pose_overlay.lane_end_error(
            "cell", [9.0, 151, 99.5], self.CENTRE, self.Z, 5.6, 2
        )
        self.assertIn("teleported", error)

    def test_missing_speed_is_not_a_failure(self):
        # Shots cells collect no frame log: only the position can fail them.
        self.assertIsNone(
            pose_overlay.lane_end_error("shots", [9.0, 151, 99.5], self.CENTRE, self.Z)
        )


class WilsonTest(unittest.TestCase):
    def test_bounds(self):
        self.assertIsNone(pose_overlay.wilson(0, 0))
        low, high = pose_overlay.wilson(30, 30)
        self.assertGreater(low, 0.88)
        self.assertEqual(high, 1.0)


if __name__ == "__main__":
    unittest.main()
