"""Stand-tooling checks: seq-aware waits, settle/probe/label helpers, job record,
CI jar fetch, ffmpeg shim. No game, no sockets beyond loopback, no network."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "mcp"))
sys.path.insert(0, str(SCRIPTS / "tools"))
from scenarios import common, parkour  # noqa: E402

import game  # noqa: E402 (scripts/dev/mcp/game.py)
import fetch_ci_jars  # noqa: E402 (scripts/dev/tools/fetch_ci_jars.py)

import importlib.util  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "devstack_tooling", SCRIPTS / "devstack.py"
)
devstack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(devstack)


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def tick_states(*ticks, **fields):
    return [dict({"tick": tick}, **fields) for tick in ticks]


class WaitFor(unittest.TestCase):
    def test_returns_on_first_fresh_matching_tick(self):
        clock = Clock()
        states = tick_states(7, 7, 8, pos=[0, 0, 0], onGround=True)
        calls = []

        def get_state(remaining):
            calls.append(remaining)
            return states[min(len(calls) - 1, len(states) - 1)]

        with (
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
        ):
            matched, state, _ = common.wait_for(
                get_state, lambda s: s["tick"] >= 8, 5.0
            )
        self.assertTrue(matched)
        self.assertEqual(state["tick"], 8)
        self.assertEqual(len(calls), 3)  # duplicate tick 7 evaluated once

    def test_timeout_reports_last_state(self):
        clock = Clock()
        with (
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
        ):
            matched, state, elapsed = common.wait_for(
                lambda remaining: {"tick": 3}, lambda s: False, 0.5
            )
        self.assertFalse(matched)
        self.assertEqual(state, {"tick": 3})
        self.assertGreaterEqual(elapsed, 0.5)

    def test_late_match_after_deadline_is_rejected(self):
        clock = Clock()

        def stalled(remaining):
            clock.sleep(0.08)  # transport ignores its budget and answers late
            return {"tick": 1}

        with (
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
        ):
            matched, _, _ = common.wait_for(stalled, lambda s: True, 0.01)
        self.assertFalse(matched)

    def test_budget_forwarded_to_transport(self):
        clock = Clock()
        budgets = []

        def budgeted(remaining):
            budgets.append(remaining)
            return {"tick": 1}

        with (
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
        ):
            matched, _, _ = common.wait_for(budgeted, lambda s: False, 0.5)
        self.assertFalse(matched)
        self.assertGreater(len(budgets), 1)
        self.assertTrue(all(b <= 0.5 for b in budgets))
        self.assertEqual(budgets[0], 0.5)

    def test_zero_arg_getter_rejected_without_calling(self):
        calls = []

        def legacy():
            calls.append(1)
            return {"tick": 1}

        with self.assertRaises(TypeError):
            common.wait_for(legacy, lambda s: True, 1.0)
        self.assertEqual(calls, [])

    def _socketpair_bot(self):
        bot = common.Bot.__new__(common.Bot)
        bot.timeout = 40
        bot.sock, peer = socket.socketpair()
        bot._buf = bytearray()
        bot._owed = False
        bot._owed_timeouts = 0
        bot._dead = False
        return bot, peer

    def _close_bot(self, bot, peer):
        # Closes first, join (if any) afterwards via LIFO order.
        self.addCleanup(peer.close)
        self.addCleanup(bot.close)

    def _delayed_server(self, delay):
        """Loopback fake testbot answering every line after `delay` seconds
        with a per-reply counter; bounded to 30 requests, daemon thread."""
        bot, peer = self._socketpair_bot()

        def serve():
            buf = b""
            for n in range(1, 31):
                while b"\n" not in buf:
                    try:
                        chunk = peer.recv(1024)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buf += chunk
                _, _, buf = buf.partition(b"\n")
                time.sleep(delay)
                try:
                    peer.sendall(
                        b'{"tick": 1, "pos": [0, 0, 0], "onGround": false, '
                        b'"n": ' + str(n).encode() + b"}\n"
                    )
                except OSError:
                    return

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        self.addCleanup(server.join, 5)
        self._close_bot(bot, peer)
        return bot

    def test_silent_peer_timeout_keeps_connection_usable(self):
        bot, _peer = self._socketpair_bot()
        self._close_bot(bot, _peer)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.2)
        self.assertLess(time.monotonic() - started, 2.0)
        # No reply was owed, so the same connection serves the next call
        # (which times out again instead of raising RuntimeError).
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.2)
        self.assertFalse(bot._dead)

    def test_eof_invalidates_connection(self):
        bot, peer = self._socketpair_bot()
        self._close_bot(bot, peer)
        peer.close()
        with self.assertRaises(ConnectionError):
            bot("state", timeout=2)
        with self.assertRaises(RuntimeError):
            bot("state", timeout=2)

    def test_partial_line_without_newline_times_out(self):
        bot, peer = self._socketpair_bot()
        self._close_bot(bot, peer)
        peer.sendall(b'{"tick":')
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.2)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_drip_feed_cannot_outlive_deadline(self):
        bot, peer = self._socketpair_bot()
        self._close_bot(bot, peer)
        stop = threading.Event()

        def drip():
            # Bounded feeder: 25 bytes, then exits (no unbounded worker).
            for _ in range(25):
                if stop.is_set():
                    return
                try:
                    peer.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.02)

        feeder = threading.Thread(target=drip)
        feeder.start()
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                bot("state", timeout=0.05)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            stop.set()
            feeder.join(timeout=5)

    def test_explicit_timeout_never_raises_socket_timeout(self):
        bot, _peer = self._socketpair_bot()
        self._close_bot(bot, _peer)
        bot.sock.settimeout(0.01)  # pre-reduced budget, e.g. by MCP
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            bot("state")  # timeout=None: respects 0.01, never restores upward
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIsNotNone(bot.sock)  # silent timeout keeps the connection

    def test_unmatched_settle_keeps_bot_usable(self):
        # Regression: a wait whose last polls time out must not kill the
        # scenario connection — run_attempt records "not-placed" and continues.
        bot = self._delayed_server(delay=0.08)
        self.assertFalse(parkour.settle(bot, 9, 9, 9, timeout=0.3))
        reply = bot("state", timeout=2)
        # Fresh reply (late poll answers dropped), not a stale line or an error.
        self.assertEqual(reply["pos"], [0, 0, 0])
        self.assertGreaterEqual(reply["n"], 2)

    def test_three_silent_drains_invalidate_the_connection(self):
        # A testbot that silently drops one command leaves every later call
        # timing out within its own budget; after 3 consecutive failed drains
        # of the owed reply the connection is closed so the error says what
        # happened instead of timing out forever.
        bot, _peer = self._socketpair_bot()
        self._close_bot(bot, _peer)
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.05)
        self.assertTrue(bot._owed)
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.05)
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.05)
        self.assertFalse(bot._dead)
        with self.assertRaises(TimeoutError):
            bot("state", timeout=0.05)
        self.assertTrue(bot._dead)
        self.assertIsNone(bot.sock)
        with self.assertRaises(RuntimeError):
            bot("state", timeout=0.05)

    def test_tickless_states_evaluate_every_poll(self):
        clock = Clock()
        seen = []

        def predicate(state):
            seen.append(state)
            return len(seen) == 2

        with (
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
        ):
            matched, _, _ = common.wait_for(
                lambda remaining: {"pos": [1, 2, 3]}, predicate, 5.0
            )
        self.assertTrue(matched)
        self.assertEqual(len(seen), 2)


class SettleAfterTp(unittest.TestCase):
    def test_teleports_then_blocks_until_on_ground(self):
        commands = []
        positions = [[0.5, 151.0, 0.5], [32.5, 151.0, -19.5], [32.5, 151.0, -19.5]]
        grounds = [True, False, True]
        ticks = [41, 42, 43]

        class Bot:
            def __call__(self, command, timeout=None):
                self.last = command
                if command == "state":
                    i = min(Bot.n, 2)
                    Bot.n += 1
                    return {
                        "tick": ticks[i],
                        "dimension": "blockfield:vertigo",
                        "pos": positions[i],
                        "onGround": grounds[i],
                    }
                return {}

        Bot.n = 0

        class Rcon:
            def cmd(self, command):
                commands.append(command)
                return "ok"

        state = common.settle_after_tp(Bot(), Rcon(), "HitregA", 32.5, 151.0, -19.5)
        self.assertTrue(state["onGround"])
        self.assertTrue(commands[0].endswith("run tp HitregA 32.5 151.0 -19.5 0 0"))

    def test_timeout_raises(self):
        class Bot:
            def __call__(self, command, timeout=None):
                return {
                    "tick": 1,
                    "dimension": "d",
                    "pos": [0, 0, 0],
                    "onGround": False,
                }

        class Rcon:
            def cmd(self, command):
                return "ok"

        clock = Clock()
        with (
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
            self.assertRaises(RuntimeError),
        ):
            common.settle_after_tp(Bot(), Rcon(), "x", 9, 9, 9, timeout=0.3)


class ParkourSettle(unittest.TestCase):
    def test_settle_uses_fresh_ticks(self):
        states = [
            {"tick": 5, "pos": [1.0, 2.0, 3.0], "onGround": False},
            {"tick": 5, "pos": [1.0, 2.0, 3.0], "onGround": True},
            {"tick": 6, "pos": [1.0, 2.0, 3.0], "onGround": True},
        ]
        calls = []

        def bot(command, timeout=None):
            calls.append(command)
            return states[min(len(calls) - 1, 2)]

        self.assertTrue(parkour.settle(bot, 1.0, 2.0, 3.0))
        self.assertEqual(calls, ["state"] * 3)

    def test_settle_false_when_never_grounded(self):
        clock = Clock()
        with (
            patch.object(parkour.time, "monotonic", side_effect=clock.monotonic),
            patch.object(parkour.time, "sleep", side_effect=clock.sleep),
        ):
            self.assertFalse(
                parkour.settle(
                    lambda command, timeout=None: {
                        "pos": [0, 0, 0],
                        "onGround": False,
                    },
                    0,
                    0,
                    0,
                )
            )


class ReadinessHelpers(unittest.TestCase):
    def test_wait_for_port_true_on_listening_socket(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            self.assertTrue(
                common.wait_for_port("127.0.0.1", server.getsockname()[1], timeout=2)
            )
        finally:
            server.close()

    def test_wait_for_port_false_on_closed_port(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(common.wait_for_port("127.0.0.1", port, timeout=0.3))

    def test_port_probe_bounds_connect_and_sleep_by_remaining(self):
        clock = Clock()
        seen_timeouts = []

        def stalled(address, timeout=None):
            seen_timeouts.append(timeout)
            raise OSError("stalled")

        with (
            patch.object(common.socket, "create_connection", side_effect=stalled),
            patch.object(common.time, "monotonic", side_effect=clock.monotonic),
            patch.object(common.time, "sleep", side_effect=clock.sleep),
        ):
            self.assertFalse(common.wait_for_port("127.0.0.1", 1, timeout=1.0))
        self.assertGreater(len(seen_timeouts), 1)
        self.assertTrue(all(t is not None and t <= 1.0 for t in seen_timeouts))
        self.assertLessEqual(clock.now, 1.2)

    def test_reserve_label_refuses_completed_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "mx-1ms-c1-joined.json").write_text("{}")
            with self.assertRaises(RuntimeError):
                common.reserve_run_label(tmp, "mx-1ms-c1")
            common.reserve_run_label(tmp, "mx-1ms-c2")  # fresh label passes


class GameJobClient2(unittest.TestCase):
    def test_client2_home_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "stack"
            home.mkdir()
            client2 = home / "client2"
            client2.mkdir()
            with (
                patch.object(devstack, "HOME", home),
                patch.object(devstack, "process_owner", return_value="owner"),
            ):
                devstack.game_job(
                    SimpleNamespace(name="stand", client2_home=str(client2))
                )
            config = json.loads((home / "jobs" / "stand" / "job.json").read_text())
            self.assertEqual(config["clients"]["2"]["home"], str(client2.resolve()))

    def test_missing_client2_home_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "stack"
            home.mkdir()
            with (
                patch.object(devstack, "HOME", home),
                patch.object(devstack, "process_owner", return_value="owner"),
                self.assertRaises(SystemExit),
            ):
                devstack.game_job(
                    SimpleNamespace(name="stand", client2_home=str(home / "nope"))
                )


class FetchCiJars(unittest.TestCase):
    def test_runtime_jar_name(self):
        self.assertTrue(
            fetch_ci_jars.runtime_jar_name(
                "blockfield-client-1.56.1+mc1.21.1.jar", "client"
            )
        )
        self.assertFalse(
            fetch_ci_jars.runtime_jar_name(
                "blockfield-client-1.56.1+mc1.21.1.jar", "server"
            )
        )
        self.assertFalse(
            fetch_ci_jars.runtime_jar_name(
                "blockfield-client-1.0-sources.jar", "client"
            )
        )

    def test_tested_tree_lookup_uses_get_with_query(self):
        seen = []

        def fake_gh(*args):
            seen.append(args)
            return "1"

        with patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh):
            self.assertTrue(fetch_ci_jars.tested_tree_ok("R", "abc123"))
        # gh turns -F form fields into a POST, which this GET-only endpoint
        # rejects; the name must travel as a URL query instead.
        self.assertNotIn("-F", seen[0])
        self.assertTrue(
            any("actions/artifacts?name=tested-tree-abc123" in a for a in seen[0])
        )

    def test_release_commit_peels_annotated_tag(self):
        tag_obj, commit = "t" * 40, "c" * 40

        def fake_gh(*args):
            text = " ".join(args)
            if "matching-refs" in text:
                return json.dumps(
                    [
                        {
                            "ref": "refs/tags/v9",
                            "object": {"type": "commit", "sha": "x" * 40},
                        },
                        {
                            "ref": "refs/tags/v1.0",
                            "object": {"type": "tag", "sha": tag_obj},
                        },
                    ]
                )
            if "/git/tags/" in text:
                return commit
            raise AssertionError(text)

        with patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh):
            self.assertEqual(fetch_ci_jars.release_commit("R", "v1.0"), commit)

    def test_release_match_ignores_target_metadata_and_prefixes(self):
        peeled = "d" * 40

        def fake_gh(*args):
            text = " ".join(args)
            if "release list" in text:
                return "v9\n"
            if "matching-refs" in text:
                # Release created against the pre-existing tag with target main:
                # only the peeled tag commit counts, never target_commitish.
                return json.dumps(
                    [
                        {
                            "ref": "refs/tags/v9",
                            "object": {"type": "commit", "sha": peeled},
                        }
                    ]
                )
            if "releases/tags" in text:
                raise AssertionError("must not consult target_commitish")
            raise AssertionError(text)

        with patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh):
            self.assertEqual(fetch_ci_jars.find_release_for_sha("R", peeled), "v9")
            self.assertIsNone(fetch_ci_jars.find_release_for_sha("R", "e" * 40))
            # A shared prefix is not an identity proof.
            self.assertIsNone(
                fetch_ci_jars.find_release_for_sha("R", peeled[:-1] + "0")
            )

    def test_resolve_version_uses_peeled_tag_commit(self):
        peeled = "f" * 40

        def fake_gh(*args):
            text = " ".join(args)
            if "matching-refs" in text:
                return json.dumps(
                    [
                        {
                            "ref": "refs/tags/v1.0",
                            "object": {"type": "commit", "sha": peeled},
                        }
                    ]
                )
            raise AssertionError(text)

        with patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh):
            self.assertEqual(
                fetch_ci_jars.resolve_sha("R", version="v1.0"), (peeled, "v1.0")
            )

    def test_download_and_verify_installs_without_overwrite(self):
        payload = b"fake-jar-bytes"
        digest = hashlib.sha256(payload).hexdigest()

        def fake_gh(*args):
            args = list(args)
            dest = Path(args[args.index("-D") + 1])
            (dest / "blockfield-client-1.0+mc.jar").write_bytes(payload)
            (dest / "SHA256SUMS").write_text(
                f"{digest}  blockfield-client-1.0+mc.jar\n"
            )
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "files" / "mods"
            with patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh):
                installed = fetch_ci_jars.download_and_verify(
                    "R", "v1.0", dest, sides=("client",)
                )
            self.assertEqual(len(installed), 1)
            self.assertTrue((dest / "blockfield-client-1.0+mc.jar").is_file())
            # Identical reinstall is a no-op, a different same-side jar refuses.
            with patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh):
                fetch_ci_jars.download_and_verify("R", "v1.0", dest, sides=("client",))
            # Same filename but different bytes (truncated install / local build)
            # must fail closed, never pass as the verified input.
            (dest / "blockfield-client-1.0+mc.jar").write_bytes(b"corrupt")
            with (
                patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh),
                self.assertRaises(RuntimeError),
            ):
                fetch_ci_jars.download_and_verify("R", "v1.0", dest, sides=("client",))
            self.assertEqual(
                (dest / "blockfield-client-1.0+mc.jar").read_bytes(), b"corrupt"
            )
            (dest / "blockfield-client-9.9+mc.jar").write_bytes(b"other")
            with (
                patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh),
                self.assertRaises(RuntimeError),
            ):
                fetch_ci_jars.download_and_verify("R", "v1.0", dest, sides=("client",))

    def test_download_refuses_checksum_mismatch(self):
        def fake_gh(*args):
            args = list(args)
            dest = Path(args[args.index("-D") + 1])
            (dest / "blockfield-server-1.0+mc.jar").write_bytes(b"tampered")
            (dest / "SHA256SUMS").write_text(
                "0" * 64 + "  blockfield-server-1.0+mc.jar\n"
            )
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(fetch_ci_jars, "run_gh", side_effect=fake_gh),
                self.assertRaises(RuntimeError),
            ):
                fetch_ci_jars.download_and_verify(
                    "R", "v1.0", Path(tmp), sides=("server",)
                )


class FfmpegShim(unittest.TestCase):
    SHIM = SCRIPTS / "shims" / "ffmpeg"

    def run_shim(self, path, *args):
        env = {"PATH": path}
        return subprocess.run(
            [str(self.SHIM), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_prefers_vorbis_capable_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain, capable = Path(tmp) / "plain", Path(tmp) / "capable"
            plain.mkdir()
            capable.mkdir()
            (plain / "ffmpeg").write_text("#!/bin/sh\necho PLAIN\n")
            (capable / "ffmpeg").write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-hide_banner" ]; then echo " A....D libvorbis stub"; exit 0; fi\n'
                'echo CAPABLE "$@"\n'
            )
            for p in (plain / "ffmpeg", capable / "ffmpeg"):
                p.chmod(0o755)
            path = f"{self.SHIM.parent}:{plain}:{capable}:/usr/bin:/bin"
            self.assertEqual(
                self.run_shim(path, "hello").stdout.strip(), "CAPABLE hello"
            )

    def test_falls_back_without_vorbis(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            fakebin = Path(tmp) / "fakebin"
            plain.mkdir()
            fakebin.mkdir()
            (plain / "ffmpeg").write_text("#!/bin/sh\necho PLAIN\n")
            (plain / "ffmpeg").chmod(0o755)
            # No libvorbis anywhere: stub grep(1) to never match.
            (fakebin / "grep").write_text("#!/bin/sh\nexit 1\n")
            (fakebin / "grep").chmod(0o755)
            path = f"{self.SHIM.parent}:{plain}:{fakebin}"
            self.assertEqual(self.run_shim(path, "hello").stdout.strip(), "PLAIN")

    def test_missing_binary_exits_127(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            result = self.run_shim(f"{self.SHIM.parent}:{empty}", "hi")
            self.assertEqual(result.returncode, 127)
            self.assertIn("no ffmpeg", result.stderr)

    def test_aliased_self_terminates(self):
        with tempfile.TemporaryDirectory() as tmp:
            alias = Path(tmp) / "alias"
            alias.mkdir()
            os.symlink(self.SHIM, alias / "ffmpeg")
            result = self.run_shim(f"{self.SHIM.parent}:{alias}", "hi")
            self.assertEqual(result.returncode, 127)


class McpWaitStateSeq(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bf game job ")
        self.addCleanup(temporary.cleanup)
        self.job = Path(temporary.name).resolve() / "job"
        self.job.mkdir()
        self.env = patch.dict(sys.modules["os"].environ, {"BF_GAME_JOB": str(self.job)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, name, args):
        return game.TOOLS[name][0](args)

    def test_duplicate_ticks_evaluated_once(self):
        states = [
            {"tick": 10, "connected": False},
            {"tick": 10, "connected": True},
            {"tick": 11, "connected": True},
        ]
        with patch.object(game, "bot", side_effect=states) as bot:
            result = self.call(
                "bf_wait_state", {"client": 1, "expected": {"connected": True}}
            )
        sample = json.loads(result["content"][0]["text"])
        self.assertTrue(sample["matched"])
        self.assertEqual(sample["attempts"], 2)  # tick 10 once, tick 11 once
        self.assertEqual(bot.call_count, 3)
        self.assertEqual(sample["state"]["tick"], 11)

    def test_transport_uses_fresh_budget_after_slow_verify(self):
        constructed, called = [], []
        clock = Clock()

        class FakeSock:
            def getsockname(self):
                return ("127.0.0.1", 47777)

        class FakeConn:
            def __init__(self, port=None, timeout=None):
                constructed.append(timeout)
                self.sock = FakeSock()

            def __call__(self, command, timeout=None):
                called.append(timeout)
                raise RuntimeError("no game")

            def close(self):
                pass

        def slow_verify(*args):
            clock.sleep(0.4)

        token = game.DEADLINE.set(clock.monotonic() + 0.5)
        try:
            with (
                patch.object(game, "client_settings", return_value=("pid", "dir")),
                patch.object(game, "require_process", return_value=None),
                patch.object(game, "verify_peer", side_effect=slow_verify),
                patch.object(game, "Bot", side_effect=FakeConn),
                patch.object(game.time, "monotonic", side_effect=clock.monotonic),
                patch.object(game.time, "sleep", side_effect=clock.sleep),
            ):
                with self.assertRaises(RuntimeError):
                    game.bot({}, 1, "state")
        finally:
            game.DEADLINE.reset(token)
        # Construction saw ~0.5 s; the call must use the budget remaining
        # after verify_peer (~0.1 s), never the construction value.
        self.assertEqual(len(constructed), 1)
        self.assertEqual(len(called), 1)
        self.assertLess(called[0], constructed[0])
        self.assertLessEqual(called[0], 0.11)

    def test_match_after_deadline_is_rejected(self):
        clock = Clock()
        states = [{"tick": 1, "connected": True}]

        def late_bot(config, client, command):
            clock.sleep(0.6)  # reply arrives after the 0.5 s tool deadline
            return states[0]

        with (
            patch.object(game, "bot", side_effect=late_bot),
            patch.object(game.time, "monotonic", side_effect=clock.monotonic),
            patch.object(game.time, "sleep", side_effect=clock.sleep),
        ):
            result = self.call(
                "bf_wait_state",
                {"client": 1, "expected": {"connected": True}, "timeout": 0.5},
            )
        sample = json.loads(result["content"][0]["text"])
        self.assertTrue(result["isError"])
        self.assertFalse(sample["matched"])


if __name__ == "__main__":
    unittest.main()
