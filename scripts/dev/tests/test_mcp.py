"""Hermetic MCP checks: protocol handshake plus tool dispatch, no game."""

import json
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import server  # noqa: E402


class Protocol(unittest.TestCase):
    def test_initialize_ping_and_list(self):
        init = server.handle({"method": "initialize", "params": {}, "id": 1})
        self.assertEqual(init["result"]["protocolVersion"], server.PROTOCOL_VERSION)
        self.assertEqual(
            server.handle({"method": "ping", "id": 2}), {"result": {}, "id": 2}
        )
        names = [
            t["name"]
            for t in server.handle({"method": "tools/list", "id": 3})["result"]["tools"]
        ]
        self.assertEqual(
            names,
            [
                "status",
                "server_start",
                "server_stop",
                "server_rcon",
                "server_log",
                "client_prepare",
                "client_play",
                "client_stop",
                "bot_send",
                "logs_snapshot",
                "run_scenario",
            ]
            + list(server.game.TOOLS),
        )

    def test_unknown_method_and_tool(self):
        self.assertEqual(
            server.handle({"method": "nope", "id": 1})["error"]["code"], -32601
        )
        reply = server.handle(
            {
                "method": "tools/call",
                "id": 2,
                "params": {"name": "nope", "arguments": {}},
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)

    def test_notifications_are_silent(self):
        self.assertIsNone(
            server.handle({"method": "notifications/initialized", "id": 1})
        )


class Dispatch(unittest.TestCase):
    def test_linux_client_launch_waits_for_registered_devstack_start(self):
        with (
            patch.object(server.sys, "platform", "linux"),
            patch.object(server.devstack, "client_pid", return_value=None),
            patch.object(
                server.subprocess,
                "run",
                return_value=SimpleNamespace(stdout="client started\n"),
            ) as run,
        ):
            reply = server.tool_client_play({"mod": True})
        self.assertEqual(reply["content"][0]["text"], "client started")
        self.assertEqual(run.call_args.kwargs["timeout"], 600)
        self.assertIn("--mod", run.call_args.args[0])

    def test_tool_logs_do_not_corrupt_stdio_responses(self):
        requests = [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": "server_stop", "arguments": {}},
            },
            {"jsonrpc": "2.0", "method": "ping", "id": 2},
        ]
        output, logs = io.StringIO(), io.StringIO()
        with (
            patch.object(
                server.sys,
                "stdin",
                io.StringIO("\n".join(json.dumps(request) for request in requests)),
            ),
            patch.object(server.sys, "stdout", output),
            patch.object(server.sys, "stderr", logs),
            patch.object(
                server.devstack, "server_stop", side_effect=lambda _: print("stopped")
            ),
        ):
            server.main()
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertEqual(replies[0]["result"]["content"][0]["text"], "stopped")
        self.assertEqual(logs.getvalue(), "stopped\n")

    def test_status_reports_devstack_state(self):
        with (
            patch.object(server.devstack, "server_pid", return_value=None),
            patch.object(server.devstack, "port_open", return_value=False),
        ):
            reply = server.handle(
                {
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "status", "arguments": {}},
                }
            )
        state = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(
            state,
            {
                "stack": str(server.devstack.HOME),
                "server_pid": None,
                "server_port_open": False,
                "clients": {"1": False, "2": False},
            },
        )

    def test_run_scenario_rejects_unknown_names(self):
        reply = server.handle(
            {
                "method": "tools/call",
                "id": 1,
                "params": {"name": "run_scenario", "arguments": {"name": "nope"}},
            }
        )
        self.assertTrue(reply["result"]["isError"])

    def test_scenario_cli_arguments_and_failures_are_preserved(self):
        def run(args):
            self.assertEqual(
                (args.action, args.dimension), ("setup", "blockfield:audit")
            )
            print("FAIL scenario")
            return 1

        with patch.object(server.SCENARIOS["hitreg"], "run", side_effect=run):
            reply = server.handle(
                {
                    "method": "tools/call",
                    "id": 1,
                    "params": {
                        "name": "run_scenario",
                        "arguments": {
                            "name": "hitreg",
                            "arguments": ["setup", "blockfield:audit"],
                        },
                    },
                }
            )
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["content"][0]["text"], "FAIL scenario\n")

    def test_bot_send_errors_stay_in_band(self):
        with patch.object(server, "bot_send", side_effect=RuntimeError("down")):
            reply = server.handle(
                {
                    "method": "tools/call",
                    "id": 1,
                    "params": {"name": "bot_send", "arguments": {"command": ["state"]}},
                }
            )
        self.assertTrue(reply["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
