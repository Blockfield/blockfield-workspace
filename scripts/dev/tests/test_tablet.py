"""Tablet diagnostic must preserve failures and restore the owned stand's settings."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scenarios.tablet import Diagnostic, SIZES, SPAR_BOT, matches, wait_for_battle  # noqa: E402


class Matrix(unittest.TestCase):
    def diagnostic(self, directory):
        self.view = {
            "open": True,
            "screen": "TabletScreen",
            "tab": "MENU",
            "menuId": "vote",
            "window": [1280, 720],
            "guiScale": 2,
        }
        self.commands = []

        def bot(command, timeout=None):
            self.commands.append(command)
            parts = command.split()
            if parts[0] == "uisize":
                self.view["window"] = list(map(int, parts[1:]))
            if parts[0] == "uiscale":
                self.view["guiScale"] = int(parts[1])
            return dict(self.view)

        self.rcon = Mock()
        return Diagnostic(bot, self.rcon, Path(directory) / "captures")

    def test_matrix_captures_every_requested_size_scale_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = self.diagnostic(directory)
            diagnostic.shot = Mock()
            diagnostic.capture_matrix("vote", {"menuId": "vote"})
            self.assertEqual(diagnostic.shot.call_count, 24)
            names = {call.args[0] for call in diagnostic.shot.call_args_list}
            self.assertEqual(
                names, {f"vote-{w}x{h}-s{s}" for w, h in SIZES for s in range(1, 5)}
            )
            self.assertEqual(self.view["window"], [1280, 720])
            self.assertEqual(self.view["guiScale"], 2)
            self.assertEqual(
                [call.args[0] for call in self.rcon.call_args_list],
                ["tick freeze", "tick unfreeze"],
            )

    def test_failed_capture_still_restores_resolution_scale_and_server_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = self.diagnostic(directory)
            diagnostic.shot = Mock(side_effect=AssertionError("wrong screen"))
            with self.assertRaisesRegex(AssertionError, "wrong screen"):
                diagnostic.capture_matrix("vote", {"menuId": "vote"})
            self.assertEqual(self.commands[-2:], ["uisize 1280 720", "uiscale 2"])
            self.rcon.assert_called_with("tick unfreeze")

    def test_auto_gui_scale_restores_effective_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = self.diagnostic(directory)
            diagnostic.shot = Mock()
            self.view["guiScale"] = 0
            self.view["actualScale"] = 3.0
            diagnostic.capture_matrix("vote", {"menuId": "vote"})
            self.assertEqual(self.commands[-2:], ["uisize 1280 720", "uiscale 3"])

    def test_battle_waits_for_auto_deploy_and_marks_both_ready(self):
        views = [
            {"deployment": "SpawnSelectionScreen", "booting": False},
            {"deployment": "SpawnSelectionScreen", "booting": False},
            {"deployment": None, "booting": False},
        ]
        rcon_cmds = []

        def bot(command, timeout=None):
            return views.pop(0)

        def rcon(command):
            rcon_cmds.append(command)
            if command == "list":
                return "There are 2 players online: Hero, TabletBot"
            return "ok"

        wait_for_battle(bot, rcon, "7", "Hero", timeout=30)
        self.assertIn(f"player {SPAR_BOT} spawn", rcon_cmds)
        self.assertIn(f"execute as {SPAR_BOT} run bf join 7", rcon_cmds)
        self.assertIn("execute as Hero run ready on", rcon_cmds)
        self.assertIn(f"execute as {SPAR_BOT} run ready on", rcon_cmds)

    def test_battle_rejects_a_closed_room(self):
        def closed(cmd):
            if cmd == "list":
                return "There are 2 players online: Hero, TabletBot"
            return "Этот матч уже закрыт"

        with self.assertRaisesRegex(AssertionError, "could not join room"):
            wait_for_battle(Mock(), closed, "7", "Hero", timeout=5)

    def test_matrix_subset_only_captures_listed_states(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostic = self.diagnostic(directory)
            diagnostic.matrix = True
            diagnostic.only = {"post-match-vote"}

            def fake_key(name):
                if name == "Escape":
                    self.view.update(open=False, screen=None)
                elif name == "m":
                    self.view.update(open=True, screen="TabletScreen")

            diagnostic.wait = Mock()
            diagnostic.shot = Mock()
            diagnostic.key = Mock(side_effect=fake_key)
            diagnostic.tab = Mock()
            diagnostic.capture_matrix = Mock()
            diagnostic.cycle("post-match-vote", {"menuId": "vote"})
            diagnostic.cycle("lobby", {"menuId": "hub"})
            self.assertEqual(
                [call.args[0] for call in diagnostic.capture_matrix.call_args_list],
                ["post-match-vote"],
            )

    def test_closed_tablet_and_stale_match_are_not_a_reopened_vote(self):
        vote = {"open": True, "screen": "TabletScreen", "tab": "MENU", "menuId": "vote"}
        self.assertTrue(matches(vote, {"tab": "MENU", "menuId": "vote"}))
        self.assertFalse(matches(vote | {"open": False}, {"menuId": "vote"}))
        self.assertFalse(matches(vote | {"menuId": "match"}, {"menuId": "vote"}))
        self.assertFalse(matches(vote | {"screen": "PauseScreen"}, {"menuId": "vote"}))


if __name__ == "__main__":
    unittest.main()
