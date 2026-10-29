#!/usr/bin/env python3
"""Blockfield dev MCP server (stdio, pure stdlib): JSON-RPC 2.0 over newline-delimited stdin/stdout.

Speaks only to devstack entities (.dev/server.pid, server.out, testbot TCP, RCON);
starts no background processes of its own. Server/client launches go through
devstack itself, and only on an explicit tool call.

    just mcp   # stdio; wire via .mcp.json
"""

import argparse
import contextlib
import io
import json
import socket
import subprocess
import sys
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEV))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import devstack  # noqa: E402
from scenarios import SCENARIOS  # noqa: E402
import game  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"


def text(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": value}]}


def bot_send(client, command, timeout):
    port = devstack.BOT_PORTS[client]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall((" ".join(command) + "\n").encode("utf-8"))
            sock.settimeout(timeout)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as error:
        raise RuntimeError(f"testbot on 127.0.0.1:{port} is not answering ({error})")
    return data.decode("utf-8", "replace").strip()


def tool_status(_):
    return text(
        {
            "stack": str(devstack.HOME),
            "server_pid": devstack.server_pid(),
            "server_port_open": devstack.port_open(devstack.PORT),
            "clients": {
                n: devstack.port_open(p) for n, p in (("1", 47777), ("2", 47778))
            },
        }
    )


def tool_server_start(args):
    devstack.server_start(
        argparse.Namespace(
            xms=args.get("xms", "1G"),
            xmx=args.get("xmx", "3G"),
            debug=bool(args.get("debug")),
        )
    )
    return text("server ready")


def tool_server_stop(_):
    devstack.server_stop(argparse.Namespace())
    return text("stopped")


def tool_server_rcon(args):
    try:
        client = devstack.Rcon(port=devstack.RCON_PORT, pw=devstack.RCON_PASSWORD)
    except OSError:
        raise RuntimeError("RCON is not answering: is the server running?")
    return text("\n".join(client.cmd(c) or "" for c in args["command"]))


def tool_server_log(args):
    try:
        lines = devstack.LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        raise RuntimeError(f"no log yet: {devstack.LOG}")
    return text("\n".join(lines[-int(args.get("lines", 40)) :]))


def tool_client_prepare(args):
    devstack.client_prepare(
        argparse.Namespace(
            game_dir=args.get("game_dir"),
            client=int(args.get("client", 1)),
            testbot=args.get("testbot"),
            mod_repo=args.get("mod_repo"),
            no_testbot=bool(args.get("no_testbot", False)),
            mod=bool(args.get("mod", False)),
            tools=bool(args.get("tools", False)),
            no_tools=bool(args.get("no_tools", False)),
        )
    )
    return text("game dir ready")


def tool_client_play(args):
    if devstack.client_pid():
        raise RuntimeError("this stack's client is already running")
    command = [sys.executable, str(devstack.ROOT / "bf.py"), "play"]
    for key, flag in (
        ("server", "--server"),
        ("name", "--name"),
        ("game_dir", "--game-dir"),
        ("testbot_port", "--testbot-port"),
    ):
        if args.get(key):
            command += [flag, str(args[key])]
    for flag in ("mod", "debug", "no_testbot"):
        if args.get(flag):
            command += ["--" + flag.replace("_", "-")]
    if sys.platform == "linux":
        try:
            result = subprocess.run(
                command,
                cwd=devstack.ROOT,
                capture_output=True,
                text=True,
                timeout=600,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            raise RuntimeError(error.stderr or error.stdout or str(error)) from error
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("client preparation exceeded 10 minutes") from error
        return text(
            result.stdout.strip() or f"client started (pid {devstack.client_pid()})"
        )
    devstack.HOME.mkdir(parents=True, exist_ok=True)
    log = open(devstack.HOME / "client-launch.out", "w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=devstack.ROOT,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=sys.platform != "win32",
    )
    return text(f"client starting (pid {process.pid})")


def tool_client_stop(_):
    devstack.client_stop()
    return text("stopped")


def tool_bot_send(args):
    return text(
        bot_send(
            int(args.get("client", 1)), args["command"], float(args.get("timeout", 60))
        )
    )


def tool_logs_snapshot(args):
    dest = devstack.snapshot_logs(
        args.get("tag", devstack.stamp()),
        args.get("game_dir") or devstack.HOME / "play",
    )
    devstack.rotate_temp()
    return text(str(dest))


def tool_run_scenario(args):
    name = args["name"]
    if name not in SCENARIOS:
        raise RuntimeError(f"unknown scenario {name}: {sorted(SCENARIOS)}")
    module = SCENARIOS[name]
    parser = argparse.ArgumentParser(prog="just scenario")
    sub = parser.add_subparsers()
    module.register(sub)
    namespace = parser.parse_args([name, *args.get("arguments", [])])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.run(namespace)
    result = text(buffer.getvalue() or "(no output)")
    if code:
        result["isError"] = True
    return result


TOOLS = {
    "status": (
        tool_status,
        "server, client and bot state (devstack status)",
        {"type": "object", "properties": {}},
    ),
    "server_start": (
        tool_server_start,
        "start the dev server (127.0.0.1:25599)",
        {
            "type": "object",
            "properties": {
                "xms": {"type": "string"},
                "xmx": {"type": "string"},
                "debug": {"type": "boolean", "description": "JDWP :5005 for hotswap"},
            },
        },
    ),
    "server_stop": (
        tool_server_stop,
        "stop the dev server",
        {"type": "object", "properties": {}},
    ),
    "server_rcon": (
        tool_server_rcon,
        "run RCON commands on the dev server",
        {
            "type": "object",
            "properties": {"command": {"type": "array", "items": {"type": "string"}}},
            "required": ["command"],
        },
    ),
    "server_log": (
        tool_server_log,
        "tail of server.out",
        {"type": "object", "properties": {"lines": {"type": "integer"}}},
    ),
    "client_prepare": (
        tool_client_prepare,
        "prepare the workspace client directory (explicit only)",
        {
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "client": {"type": "integer"},
                "testbot": {"type": "string"},
                "mod": {"type": "boolean"},
                "no_testbot": {"type": "boolean"},
                "tools": {"type": "boolean"},
            },
        },
    ),
    "client_play": (
        tool_client_play,
        "launch the direct client (explicit only, non-blocking)",
        {
            "type": "object",
            "properties": {
                "server": {"type": "string"},
                "name": {"type": "string"},
                "game_dir": {"type": "string"},
                "mod": {"type": "boolean"},
                "debug": {"type": "boolean"},
                "no_testbot": {"type": "boolean"},
            },
        },
    ),
    "client_stop": (
        tool_client_stop,
        "stop the direct client",
        {"type": "object", "properties": {}},
    ),
    "bot_send": (
        tool_bot_send,
        "send a command to the testbot (state, chat, press, slots, ...)",
        {
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "client": {"type": "integer"},
                "timeout": {"type": "number"},
            },
            "required": ["command"],
        },
    ),
    "logs_snapshot": (
        tool_logs_snapshot,
        "copy server + client logs into temp/<tag>-logs/",
        {
            "type": "object",
            "properties": {"tag": {"type": "string"}, "game_dir": {"type": "string"}},
        },
    ),
    "run_scenario": (
        tool_run_scenario,
        "run a ported rig scenario (parkour/hitreg/stress/dm/range/check-*)",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(SCENARIOS)},
                "arguments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scenario CLI arguments, e.g. ['setup', 'blockfield:dm_arena_1'] for hitreg",
                },
            },
            "required": ["name"],
        },
    ),
}
TOOLS.update(game.TOOLS)


def handle(request, tools=None):
    tools = TOOLS if tools is None else tools
    if not isinstance(request, dict) or not isinstance(request.get("method"), str):
        return {"error": {"code": -32600, "message": "invalid request"}, "id": None}
    method, params, req_id = (
        request.get("method"),
        request.get("params", {}),
        request.get("id"),
    )
    if method == "initialize":
        return {
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "blockfield-game"
                    if tools is game.TOOLS
                    else "blockfield-dev",
                    "version": "1.0.0",
                },
            },
            "id": req_id,
        }
    if method == "ping":
        return {"result": {}, "id": req_id}
    if method == "tools/list":
        return {
            "result": {
                "tools": [
                    {"name": n, "description": d, "inputSchema": s}
                    for n, (_, d, s) in tools.items()
                ]
            },
            "id": req_id,
        }
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if name not in tools:
            return {
                "error": {"code": -32602, "message": f"unknown tool {name}"},
                "id": req_id,
            }
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return {"result": tools[name][0](args), "id": req_id}
        except (Exception, SystemExit) as error:
            return {
                "result": {
                    "content": [{"type": "text", "text": f"error: {error}"}],
                    "isError": True,
                },
                "id": req_id,
            }
    if method.startswith("notifications/"):
        return None
    return {
        "error": {"code": -32601, "message": f"unknown method {method}"},
        "id": req_id,
    }


def main(arguments=()):
    parser = argparse.ArgumentParser(prog="just mcp", description=__doc__)
    parser.add_argument(
        "--game", action="store_true", help="expose only job-scoped game tools"
    )
    args = parser.parse_args(arguments)
    tools = game.TOOLS if args.game else TOOLS
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            sys.stdout.write(
                json.dumps(
                    {"error": {"code": -32700, "message": str(error)}, "id": None}
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        try:
            response = handle(request, tools)
        except Exception as error:  # never drop the session on a bad call
            response = {
                "error": {"code": -32603, "message": f"{error}"},
                "id": request.get("id") if isinstance(request, dict) else None,
            }
        if response is not None:
            response["jsonrpc"] = "2.0"
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
