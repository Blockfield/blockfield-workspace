"""Validated game-only tools for an operator-selected, session-owned dev stack."""

import base64
import contextvars
import io
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import devstack
from scenarios.common import Bot

TOOLS = {}
DEADLINE = contextvars.ContextVar("game_tool_deadline", default=None)
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
DIMENSION = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+")
CLIENT = {"client": {"type": "integer", "enum": [1, 2]}}
BASE_RCON = [
    r"list",
    r"bf version",
    r"bf maintenance status",
    r"data get entity [A-Za-z0-9_]{3,16}( [A-Za-z0-9_.\[\]]{1,64})?",
    r"attribute [A-Za-z0-9_]{3,16} minecraft:[a-z_.]+ get",
    r'execute (in [a-z0-9_:]+ )?if entity @[aep](\[[a-z_=0-9,:"!.-]{0,120}\])?',
]
BASE_CHAT_COMMANDS = [
    r"/ready( on| off)?",
    r"/pvp( on| off)?",
    r"/top",
    r"/leaderboard",
    r"/menu (class|spawn)",
    r"/bf version",
    r"/bf play (room|campaign|deathmatch)( [A-Za-z0-9_-]{1,32})?",
    r"/bf join [0-9]{1,3}",
]


def validate(value, schema, field="arguments"):
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "null": value is None,
    }
    if kind and not any(valid[k] for k in ([kind] if isinstance(kind, str) else kind)):
        raise ValueError(f"{field} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"invalid {field}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise ValueError(f"missing {field} fields: {schema['required']}")
        if not schema.get("additionalProperties", True) and value.keys() - props.keys():
            raise ValueError(f"unknown {field} fields")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{field}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"too few {field}")
        if len(value) > schema.get("maxItems", math.inf):
            raise ValueError(f"too many {field}")
        for index, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{field}[{index}]")
    elif isinstance(value, str):
        if len(value) > schema.get("maxLength", math.inf):
            raise ValueError(f"{field} is too long")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"invalid {field}")
    elif type(value) in (int, float):
        if (
            not schema.get("minimum", -math.inf)
            <= value
            <= schema.get("maximum", math.inf)
        ):
            raise ValueError(f"{field} is outside its allowed range")


def job():
    selected = os.environ.get("BF_GAME_JOB") or os.environ.get("BF_HARNESS_JOB")
    pointer = devstack.HOME / "current-job"
    if not selected and pointer.is_file():
        selected = pointer.read_text(encoding="utf-8").strip()
    if not selected:
        raise RuntimeError("no active game job: set BF_GAME_JOB or .dev/current-job")
    directory = Path(selected).expanduser().resolve()
    if not directory.is_dir():
        raise RuntimeError("game job directory does not exist")
    config_file = directory / "job.json"
    config = (
        json.loads(config_file.read_text(encoding="utf-8"))
        if config_file.exists()
        else {}
    )
    if not isinstance(config, dict):
        raise ValueError("job.json must be an object")
    if config.get("owner", devstack.process_owner()) != devstack.process_owner():
        raise RuntimeError("game job belongs to another session")
    if (directory / "finished").exists():
        raise RuntimeError("this game job is finished; select a new job")
    for key in ("rcon", "chat_commands"):
        patterns = config.get(key, [])
        if not isinstance(patterns, list) or any(
            not isinstance(pattern, str) or len(pattern) > 1000 for pattern in patterns
        ):
            raise ValueError(f"invalid job {key} allowlist")
        for pattern in patterns:
            re.compile(pattern)
    if "allow_teleport" in config and type(config["allow_teleport"]) is not bool:
        raise ValueError("allow_teleport must be boolean")
    return directory, config


def log_call(directory, name, args, ok, summary, duration_ms=None):
    with (directory / "calls.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "t": time.time(),
                    "tool": name,
                    "args": args,
                    "ok": ok,
                    "result": str(summary)[:500],
                    **({"duration_ms": duration_ms} if duration_ms is not None else {}),
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def tool(name, description, properties=None, required=()):
    schema = {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }

    def register(fn):
        def call(args):
            directory, config = job()
            started = time.monotonic()
            try:
                validate(args, schema)
                result = fn(args, directory, config)
                if isinstance(result, dict) and "content" in result:
                    summary = next(
                        (
                            item["text"]
                            for item in result["content"]
                            if item["type"] == "text"
                        ),
                        "image",
                    )
                    response = result
                else:
                    summary = (
                        result
                        if isinstance(result, str)
                        else json.dumps(result, ensure_ascii=False)
                    )
                    response = {"content": [{"type": "text", "text": summary[:8000]}]}
                log_call(
                    directory,
                    name,
                    args,
                    not response.get("isError", False),
                    summary,
                    round((time.monotonic() - started) * 1000, 3),
                )
                return response
            except (Exception, SystemExit) as error:
                log_call(
                    directory,
                    name,
                    args,
                    False,
                    str(error),
                    round((time.monotonic() - started) * 1000, 3),
                )
                raise RuntimeError(str(error) or type(error).__name__) from error

        TOOLS[name] = (call, description, schema)
        return fn

    return register


def owns_connection(record, port, peer_port=None):
    if sys.platform == "win32":
        import windows_process

        return windows_process.owns_listener(record["pid"], port, peer_port)
    if sys.platform != "linux":
        raise RuntimeError("game process verification supports Linux and Windows")
    process = Path("/proc") / str(record["pid"])
    inodes = set()
    for fd in (process / "fd").iterdir():
        try:
            target = fd.readlink().name
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    for table in ("tcp", "tcp6"):
        for line in (process / "net" / table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[9] not in inodes or int(fields[1].split(":")[1], 16) != port:
                continue
            if peer_port is None and fields[3] == "0A":
                return True
            if (
                peer_port is not None
                and fields[3] == "01"
                and int(fields[2].split(":")[1], 16) == peer_port
            ):
                return True
    return False


def client_settings(config, client):
    clients = config.get("clients", {})
    if not isinstance(clients, dict):
        raise ValueError("job clients must be an object")
    settings = clients.get(str(client), {})
    if not isinstance(settings, dict):
        raise ValueError("job client settings must be an object")
    if client == 2 and not settings.get("home"):
        raise RuntimeError("client 2 needs its own registered stack home in job.json")
    home = Path(settings.get("home", devstack.HOME)).expanduser().resolve()
    command_file = home / "client.cmd"
    command = (
        json.loads(command_file.read_text(encoding="utf-8"))
        if command_file.exists()
        else {}
    )
    game_dir = (
        Path(settings.get("game_dir", command.get("game_dir", home / "play")))
        .expanduser()
        .resolve()
    )
    return home / "client.pid", game_dir


class ConnectionOwnershipError(RuntimeError):
    pass


def require_process(path, port, peer_port=None):
    record = devstack.process_record(path)
    if (
        not record
        or not record.get("live")
        or record.get("native") not in ("linux", "windows")
        or record.get("owner") != devstack.process_owner()
    ):
        raise RuntimeError(
            "game tools require a live process registered to this session"
        )
    if not owns_connection(record, port, peer_port):
        raise ConnectionOwnershipError(
            "the requested TCP connection is not owned by this session's process"
        )
    return record


def remaining_timeout(maximum):
    deadline = DEADLINE.get()
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("game operation exceeded its deadline")
    return min(maximum, remaining)


def verify_peer(path, port, peer_port):
    # The listening process may not have accepted this socket when connect() returns.
    deadline = time.monotonic() + remaining_timeout(3)
    while True:
        try:
            return require_process(path, port, peer_port)
        except ConnectionOwnershipError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.05, remaining))


def bot(config, client, command):
    if "\n" in command or "\r" in command or "\x00" in command:
        raise ValueError("invalid testbot command")
    path, _ = client_settings(config, client)
    port = devstack.BOT_PORTS[client]
    require_process(path, port)
    connection = Bot(port=port, timeout=remaining_timeout(30))
    try:
        verify_peer(path, port, connection.sock.getsockname()[1])
        # Fresh remaining budget per call: Bot never raises the socket timeout
        # itself, so time spent in verify_peer stays accounted for.
        return connection(command, timeout=remaining_timeout(30))
    finally:
        connection.close()


def rcon(command):
    require_process(devstack.PID_FILE, devstack.RCON_PORT)
    connection = devstack.Rcon(
        port=devstack.RCON_PORT,
        pw=devstack.RCON_PASSWORD,
        timeout=remaining_timeout(30),
    )
    try:
        verify_peer(
            devstack.PID_FILE, devstack.RCON_PORT, connection.s.getsockname()[1]
        )
        connection.s.settimeout(remaining_timeout(30))
        return connection.cmd(command)
    finally:
        connection.s.close()


def allowed(command, patterns):
    return not any(c in command for c in "\r\n\x00") and any(
        re.fullmatch(pattern, command) for pattern in patterns
    )


def simple(name, description, command, properties=None, required=()):
    @tool(name, description, {**CLIENT, **(properties or {})}, ["client", *required])
    def send(args, _directory, config):
        return bot(config, args["client"], command(args))


simple(
    "bf_state",
    "Current player state, screen, dimension, health and held item.",
    lambda a: "state",
)
simple(
    "bf_messages",
    "Recent chat and action-bar messages.",
    lambda a: f"messages {a.get('count', 15)}",
    {"count": {"type": "integer", "minimum": 1, "maximum": 50}},
)
simple(
    "bf_look",
    "Turn the game camera in degrees.",
    lambda a: f"look {a['yaw']} {a['pitch']}",
    {
        "yaw": {"type": "number", "minimum": -360, "maximum": 360},
        "pitch": {"type": "number", "minimum": -90, "maximum": 90},
    },
    ["yaw", "pitch"],
)
simple(
    "bf_look_at",
    "Aim at world coordinates.",
    lambda a: "lookat " + " ".join(str(a[k]) for k in ("x", "y", "z")),
    {k: {"type": "number", "minimum": -3e7, "maximum": 3e7} for k in ("x", "y", "z")},
    ["x", "y", "z"],
)
simple(
    "bf_hold",
    "Hold a movement binding for 1–200 client ticks.",
    lambda a: f"hold {a['binding']} {a['ticks']}",
    {
        "binding": {
            "type": "string",
            "enum": [
                "key.forward",
                "key.back",
                "key.left",
                "key.right",
                "key.jump",
                "key.sneak",
                "key.sprint",
            ],
        },
        "ticks": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    ["binding", "ticks"],
)
simple(
    "bf_press",
    "Press an in-game binding; TACZ uses bf_weapon.",
    lambda a: f"press {a['key']} {a.get('ticks', 2)}",
    {
        "key": {
            "type": "string",
            "pattern": r"key\.(keyboard|mouse)\.[a-z0-9_.]{1,20}",
        },
        "ticks": {"type": "integer", "minimum": 1, "maximum": 40},
    },
    ["key"],
)
simple(
    "bf_slot",
    "Select hotbar slot 0–8.",
    lambda a: f"slot {a['slot']}",
    {"slot": {"type": "integer", "minimum": 0, "maximum": 8}},
    ["slot"],
)
simple(
    "bf_use_attack",
    "Vanilla use or attack; TACZ uses bf_weapon.",
    lambda a: a["action"],
    {"action": {"type": "string", "enum": ["use", "attack"]}},
    ["action"],
)
simple(
    "bf_weapon",
    "Operate the held TACZ gun without desktop input.",
    lambda a: (
        "tacz "
        + a["op"]
        + ((" true" if a.get("aim", False) else " false") if a["op"] == "aim" else "")
    ),
    {
        "op": {
            "type": "string",
            "enum": ["shoot", "reload", "aim", "inspect", "melee"],
        },
        "aim": {"type": "boolean"},
    },
    ["op"],
)
simple(
    "bf_menu_action",
    "Send a Blockfield menu action.",
    lambda a: "bfaction " + a["action"],
    {
        "action": {
            "type": "string",
            "pattern": r"[a-z_]{2,20}(:[A-Za-z0-9_.-]{1,36}){0,3}",
        }
    },
    ["action"],
)


def chat_value(args, config):
    value = args["text"].strip()
    if not value or any(c in args["text"] for c in "\r\n\x00§"):
        raise ValueError("empty or invalid chat text")
    if value.startswith("/") and not allowed(
        value, BASE_CHAT_COMMANDS + config.get("chat_commands", [])
    ):
        raise ValueError("this chat command is not allowed in the game job")
    return value


@tool(
    "bf_chat",
    "Chat or a job-allowed player command.",
    {**CLIENT, "text": {"type": "string", "maxLength": 120}},
    ["client", "text"],
)
def chat(args, _directory, config):
    return bot(config, args["client"], "chat " + chat_value(args, config))


@tool(
    "bf_screen",
    "Close, respawn or reconnect the isolated game client.",
    {
        **CLIENT,
        "op": {"type": "string", "enum": ["escape", "close", "respawn", "reconnect"]},
    },
    ["client", "op"],
)
def screen(args, _directory, config):
    operation = args["op"]
    if operation == "escape":
        return bot(config, args["client"], "rawinput key Escape")
    if operation == "close":
        return bot(config, args["client"], "closescreen")
    if operation == "respawn":
        return bot(config, args["client"], "respawn")
    require_process(devstack.PID_FILE, devstack.PORT)
    bot(config, args["client"], "disconnect")
    return bot(config, args["client"], f"connect 127.0.0.1:{devstack.PORT}")


@tool(
    "bf_raw_input",
    "Synthetic raw input inside the consented, isolated game client only.",
    {
        **CLIENT,
        "op": {
            "type": "string",
            "enum": ["key", "combo", "mouse_down", "mouse_up", "click", "move_to"],
        },
        # combo: 2-4 keys joined by +, pressed in order and released in reverse (F3+T).
        "key": {
            "type": "string",
            "pattern": r"[A-Za-z0-9_]{1,16}(\+[A-Za-z0-9_]{1,16}){0,3}",
        },
        "button": {"type": "integer", "enum": [1, 2, 3]},
        "x": {"type": "integer", "minimum": 0, "maximum": 32768},
        "y": {"type": "integer", "minimum": 0, "maximum": 32768},
    },
    ["client", "op"],
)
def raw_input(args, _directory, config):
    return bot(config, args["client"], raw_input_command(args))


def raw_input_command(args):
    operation = args["op"]
    if operation in ("key", "combo"):
        if "key" not in args:
            raise ValueError(f"rawinput {operation} requires key")
        suffix = args["key"]
    elif operation == "move_to":
        if "x" not in args or "y" not in args:
            raise ValueError("rawinput move_to requires x and y")
        suffix = f"{args['x']} {args['y']}"
    else:
        suffix = str(args.get("button", 1))
    return f"rawinput {operation} {suffix}"


def shrink(data):
    try:
        from PIL import Image
    except ImportError:
        return data
    with Image.open(io.BytesIO(data)) as image:
        if max(image.size) <= 1568:
            return data
        image.thumbnail((1568, 1568))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def screenshot_bytes(path, timeout=15):
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            size = path.stat().st_size
            if size > 32 * 1024 * 1024:
                raise RuntimeError("screenshot exceeds 32 MiB")
            data = path.read_bytes()
            if data.startswith(b"\x89PNG\r\n\x1a\n") and data.endswith(
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            ):
                return data
        except FileNotFoundError:
            pass
        time.sleep(min(0.1, remaining))
    raise RuntimeError(
        "the game did not finish writing its screenshot before the deadline"
    )


def screenshot_context(config, client):
    process_file, game_dir = client_settings(config, client)
    record = require_process(process_file, devstack.BOT_PORTS[client])
    if not record.get("cwd") or Path(record["cwd"]).resolve() != game_dir:
        raise RuntimeError(
            "screenshot directory must match the registered client's working directory"
        )
    return record, game_dir


def evidence_directory(directory):
    evidence = directory / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    if evidence.resolve().parent != directory.resolve():
        raise ValueError("evidence directory must stay inside the game job")
    return evidence


@tool(
    "bf_screenshot",
    "Capture only the isolated game and save PNG evidence; client consent is required.",
    {**CLIENT, "label": {"type": "string", "pattern": r"[a-z0-9_-]{1,40}"}},
    ["client"],
)
def screenshot(args, directory, config):
    _record, game_dir = screenshot_context(config, args["client"])
    filename = f"c{args['client']}-{time.time_ns()}-{args.get('label', 'shot')}.png"
    source = (game_dir / "screenshots" / filename).resolve()
    if source.parent != (game_dir / "screenshots").resolve():
        raise ValueError("invalid screenshot path")
    evidence = evidence_directory(directory)
    bot(config, args["client"], "screenshot " + filename)
    data = screenshot_bytes(source)
    destination = evidence / filename
    destination.write_bytes(data)
    return {
        "content": [
            {
                "type": "image",
                "data": base64.b64encode(shrink(data)).decode("ascii"),
                "mimeType": "image/png",
            },
            {"type": "text", "text": f"saved {destination}"},
        ]
    }


@tool(
    "bf_wait",
    "Wait 1–60 seconds while the game progresses.",
    {"seconds": {"type": "number", "minimum": 1, "maximum": 60}},
    ["seconds"],
)
def wait(args, _directory, _config):
    time.sleep(args["seconds"])
    return "waited"


@tool(
    "bf_server_query",
    "Run a read-only RCON query or a job-allowed command.",
    {"command": {"type": "string", "maxLength": 4096}},
    ["command"],
)
def query(args, _directory, config):
    return rcon(server_command(args, config))


def server_command(args, config):
    command = args["command"].strip()
    if (
        any(c in args["command"] for c in "\r\n\x00")
        or len(command.encode("utf-8")) > 4096
    ):
        raise ValueError(
            "server commands must be a single line of at most 4096 UTF-8 bytes"
        )
    if not allowed(command, BASE_RCON + config.get("rcon", [])):
        raise ValueError("this server command is not allowed in the game job")
    return command


def log_lines(count=40, contains=""):
    lines = devstack.LOG.read_text(encoding="utf-8", errors="replace").splitlines()[
        -2000:
    ]
    return [line for line in lines if contains in line][-count:]


@tool(
    "bf_server_log",
    "Recent dev server log lines, optionally filtered.",
    {
        "lines": {"type": "integer", "minimum": 1, "maximum": 200},
        "contains": {"type": "string", "maxLength": 60},
    },
)
def server_log(args, _directory, _config):
    return "\n".join(log_lines(args.get("lines", 40), args.get("contains", "")))


def map_points(dimension):
    if not DIMENSION.fullmatch(dimension):
        return []
    map_id = dimension.split(":", 1)[1].rsplit("_", 1)[0]
    if not re.fullmatch(r"[a-z0-9_.-]+", map_id):
        return []
    try:
        data = json.loads(
            (devstack.SERVER / "blockfield" / "maps" / f"{map_id}.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return []
    points = {}
    for mode in data.get("mode_data", {}).values():
        entries = [
            {"name": "BASE_" + name.replace("Team", "").upper(), **point}
            for name, point in mode.get("bases", {}).items()
        ]
        for point in [*entries, *mode.get("points", [])]:
            if (
                isinstance(point, dict)
                and isinstance(point.get("name"), str)
                and all(
                    type(point.get(k)) in (int, float) and math.isfinite(point[k])
                    for k in "xyz"
                )
            ):
                points.setdefault(
                    point["name"],
                    {"name": point["name"], **{k: point[k] for k in "xyz"}},
                )
    return list(points.values())


def next_step(state, messages):
    recent = " ".join(messages[-6:])
    if state.get("health", 20) <= 0 or "death" in str(state.get("screen", "")).lower():
        return "You are dead: bf_screen respawn, then bf_menu_action spawn:BASE."
    if state.get("dimension") == "minecraft:overworld":
        return "You are in the hub: create or join a room, or use range:enter."
    if "Готовы" in recent or "только во время сражения" in recent:
        return "Pre-battle readiness: /ready on every client in this room."
    if "До начала сражения" in recent:
        return "Battle countdown: wait, select a class and spawn:BASE."
    if state.get("screen"):
        return f"A screen is open ({state['screen']}): use it or bf_screen close."
    return "In battle: approach a capture point; bf_goto_point requires job permission."


@tool(
    "bf_objective",
    "Player state, latest messages, nearby capture points and the next step.",
    CLIENT,
    ["client"],
)
def objective(args, _directory, config):
    state = bot(config, args["client"], "state")
    messages = bot(config, args["client"], "messages 10")
    px, _, pz = state.get("pos", [0, 0, 0])
    points = []
    for point in map_points(state.get("dimension", "")):
        dx, dz = point["x"] + 0.5 - px, point["z"] + 0.5 - pz
        points.append(
            {
                **point,
                "distance": round(math.hypot(dx, dz)),
                "face_yaw": round(math.degrees(math.atan2(-dx, dz))),
            }
        )
    points.sort(key=lambda point: point["distance"])
    try:
        lines = [
            line
            for line in log_lines(2000)
            if "MatchTracker" in line or "Комната" in line
        ][-4:]
    except OSError:
        lines = []
    return {
        **{key: state.get(key) for key in ("dimension", "pos", "health", "screen")},
        "player": state.get("name"),
        "messages": messages[-6:],
        "points": points,
        "server_log": lines,
        "next_step": next_step(state, messages),
        "teleport_allowed": config.get("allow_teleport", False),
    }


@tool(
    "bf_goto_point",
    "Teleport to a map point only when this job allows it.",
    {**CLIENT, "point": {"type": "string", "pattern": r"[A-Za-z_]{1,32}"}},
    ["client", "point"],
)
def goto_point(args, _directory, config):
    if not config.get("allow_teleport", False):
        raise RuntimeError("teleport is not allowed in this game job")
    state = bot(config, args["client"], "state")
    name, dimension = state.get("name", ""), state.get("dimension", "")
    point = next(
        (
            p
            for p in map_points(dimension)
            if p["name"].lower() == args["point"].lower()
        ),
        None,
    )
    if not NAME.fullmatch(name) or point is None or not DIMENSION.fullmatch(dimension):
        raise ValueError("unknown player or point: call bf_objective")
    return rcon(
        f"execute in {dimension} run tp {name} {point['x'] + 0.5} {point['y'] + 1} {point['z'] + 0.5}"
    )


@tool(
    "bf_report_bug",
    "Record a reproducible bug and its concrete evidence.",
    {
        "title": {"type": "string", "maxLength": 120},
        "steps": {"type": "string", "maxLength": 2000},
        "expected": {"type": "string", "maxLength": 600},
        "actual": {"type": "string", "maxLength": 1200},
        "evidence": {"type": "string", "maxLength": 1000},
    },
    ["title", "steps", "expected", "actual"],
)
def report_bug(args, directory, _config):
    with (directory / "bugs.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"t": time.time(), **args}, ensure_ascii=False) + "\n")
    return "bug recorded"


@tool(
    "bf_finish",
    "Write the job report exactly once; further game calls are refused.",
    {
        "summary": {"type": "string", "maxLength": 4000},
        "checks": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "check": {"type": "string", "maxLength": 300},
                    "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
                    "evidence": {"type": "string", "maxLength": 1000},
                },
                "required": ["check", "verdict", "evidence"],
            },
        },
    },
    ["summary", "checks"],
)
def finish(args, directory, _config):
    lines = [
        "# Game test report",
        "",
        args["summary"],
        "",
        "| check | verdict | evidence |",
        "|---|---|---|",
    ]
    for check in args["checks"]:
        lines.append(
            "| "
            + " | ".join(
                check[key].replace("|", "/").replace("\n", " ")
                for key in ("check", "verdict", "evidence")
            )
            + " |"
        )
    temporary = directory / "report.md.tmp"
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(directory / "report.md")
    with (directory / "finished").open("x", encoding="utf-8") as marker:
        marker.write(str(time.time()) + "\n")
    return "report saved; this game job is finished"


@tool(
    "bf_sequence",
    "Schedule hotbar changes on client ticks, without repeated polling.",
    {
        **CLIENT,
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 40,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "tick": {"type": "integer", "minimum": 0, "maximum": 200},
                    "slot": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["tick", "slot"],
            },
        },
    },
    ["client", "actions"],
)
def sequence(args, _directory, config):
    command = "sequence " + " ".join(
        f"{action['tick']}:{action['slot']}" for action in args["actions"]
    )
    return bot(config, args["client"], command)


@tool(
    "bf_render_probe",
    "Start or retrieve gun render events; full results are saved as JSON evidence.",
    {
        **CLIENT,
        "op": {"type": "string", "enum": ["start", "get"]},
        "label": {"type": "string", "pattern": r"[a-z0-9_-]{1,40}"},
    },
    ["client", "op"],
)
def render_probe(args, directory, config):
    result = bot(config, args["client"], "renderprobe " + args["op"])
    if args["op"] == "start":
        return result
    if not isinstance(result, list):
        raise RuntimeError("renderprobe did not return an event list")
    destination = evidence_directory(directory) / (
        f"c{args['client']}-render-{time.time_ns()}-{args.get('label', 'probe')}.json"
    )
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"events": len(result), "evidence": str(destination)}


@tool(
    "bf_wait_state",
    "Wait for exact player state fields, with a deadline and the last observed state.",
    {
        **CLIENT,
        "expected": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                **{
                    key: {"type": "boolean"}
                    for key in (
                        "connected",
                        "loadingOverlay",
                        "sprinting",
                        "sneaking",
                        "onGround",
                        "usingItem",
                    )
                },
                "screen": {"type": ["string", "null"], "maxLength": 100},
                "screenKind": {"type": ["string", "null"], "maxLength": 100},
                "dimension": {"type": "string", "maxLength": 150},
                "held": {"type": "string", "maxLength": 150},
                "offhand": {"type": "string", "maxLength": 150},
                "name": {"type": "string", "pattern": NAME.pattern},
                "slot": {"type": "integer", "minimum": 0, "maximum": 8},
                "camera": {
                    "type": "string",
                    "enum": ["FIRST_PERSON", "THIRD_PERSON_BACK", "THIRD_PERSON_FRONT"],
                },
            },
        },
        "timeout": {"type": "number", "minimum": 0.1, "maximum": 60},
        "interval": {"type": "number", "minimum": 0.05, "maximum": 2},
    },
    ["client", "expected"],
)
def wait_state(args, _directory, config):
    if not args["expected"]:
        raise ValueError("expected state must contain at least one field")
    started = time.monotonic()
    deadline = started + args.get("timeout", 15)
    if DEADLINE.get() is not None:
        deadline = min(deadline, DEADLINE.get())
    token = DEADLINE.set(deadline)
    state, attempts, matched, error = None, 0, False, None
    last_tick = None
    try:
        while time.monotonic() < deadline:
            candidate = bot(config, args["client"], "state")
            if not isinstance(candidate, dict):
                raise RuntimeError("testbot did not return a player state object")
            # Seq-aware: only evaluate fresh ticks, so a match returns on the first
            # tick that holds it instead of after a fixed sleep (sleep-poll races).
            tick = candidate.get("tick")
            state = candidate
            if tick is None or tick != last_tick:
                last_tick = tick
                attempts += 1
                if all(
                    key in state and state[key] == value
                    for key, value in args["expected"].items()
                ):
                    # A reply that arrives after the deadline must not count,
                    # even if it matches (slow ownership checks eat the budget).
                    matched = time.monotonic() < deadline
                    break
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(args.get("interval", 0.2), remaining))
    except (Exception, SystemExit) as failure:
        error = str(failure) or type(failure).__name__
    finally:
        DEADLINE.reset(token)
    result = {
        "matched": matched,
        "state": state,
        "attempts": attempts,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }
    if matched:
        return result
    result["error"] = error or "the expected state was not reached before the deadline"
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "isError": True,
    }


CAPTURE_TOOLS = (
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
    "bf_server_query",
    "bf_sequence",
    "bf_render_probe",
)
PROCESS_FIELDS = ("pid", "start_ticks", "birth", "id", "exe", "cwd", "native", "owner")


def capture_actions(args, config):
    actions = args.get("actions", [])
    for action in actions:
        name, arguments = action["tool"], action["arguments"]
        if action["at"] >= args["seconds"]:
            raise ValueError("capture actions must occur before the requested duration")
        validate(arguments, TOOLS[name][2], f"{name} arguments")
        if "client" in arguments and arguments["client"] != args["client"]:
            raise ValueError("capture actions must target the selected client")
        if name == "bf_chat":
            chat_value(arguments, config)
        elif name == "bf_server_query":
            server_command(arguments, config)
        elif name == "bf_raw_input":
            raw_input_command(arguments)
    return sorted(actions, key=lambda action: action["at"])


@tool(
    "bf_capture",
    "Record isolated game frames and schedule existing validated tools in one call.",
    {
        **CLIENT,
        "label": {"type": "string", "pattern": r"[a-z0-9_-]{1,40}"},
        "seconds": {"type": "number", "minimum": 0.1, "maximum": 20},
        "fps": {"type": "integer", "minimum": 1, "maximum": 10},
        "actions": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "at": {"type": "number", "minimum": 0, "maximum": 20},
                    "tool": {"type": "string", "enum": list(CAPTURE_TOOLS)},
                    "arguments": {"type": "object"},
                },
                "required": ["at", "tool", "arguments"],
            },
        },
    },
    ["client", "seconds"],
)
def capture(args, directory, config):
    pending = capture_actions(args, config)
    record, game_dir = screenshot_context(config, args["client"])
    identity = {key: record[key] for key in PROCESS_FIELDS if key in record}
    folder = Path(
        tempfile.mkdtemp(
            prefix=f"c{args['client']}-{args.get('label', 'capture')}-",
            dir=evidence_directory(directory),
        )
    )
    started, fps = time.monotonic(), args.get("fps", 5)
    end = started + args["seconds"]
    manifest = {
        "version": 1,
        "client": args["client"],
        "platform": sys.platform,
        "owner": devstack.process_owner(),
        "process": identity,
        "start_unix": time.time(),
        "requested_seconds": args["seconds"],
        "fps": fps,
        "frames": [],
        "actions": [],
        "complete": False,
    }
    token = DEADLINE.set(end)
    error, next_frame = None, started

    def verify_context():
        current_directory, current_config = job()
        if current_directory != directory or current_config != config:
            raise RuntimeError("the selected game job changed during capture")
        current, _game_dir = screenshot_context(config, args["client"])
        if {key: current.get(key) for key in identity} != identity:
            raise RuntimeError("the registered client changed during capture")

    try:
        while time.monotonic() < end:
            while (
                pending
                and time.monotonic() < end
                and started + pending[0]["at"] <= time.monotonic()
            ):
                verify_context()
                if time.monotonic() >= end:
                    break
                action = pending.pop(0)
                entry = {
                    "scheduled_seconds": action["at"],
                    "at_seconds": round(time.monotonic() - started, 6),
                    "tool": action["tool"],
                    "arguments": action["arguments"],
                    "ok": False,
                }
                manifest["actions"].append(entry)
                # An admitted action shares the frame evidence completion deadline.
                action_token = DEADLINE.set(end + 15)
                try:
                    entry["result"] = TOOLS[action["tool"]][0](action["arguments"])
                except (Exception, SystemExit) as failure:
                    entry["error"] = str(failure) or type(failure).__name__
                    raise
                finally:
                    DEADLINE.reset(action_token)
                entry["ok"] = not entry["result"].get("isError", False)
                if not entry["ok"]:
                    raise RuntimeError("a scheduled game tool failed")
            now = time.monotonic()
            if now >= end:
                break
            if now >= next_frame:
                verify_context()
                index = len(manifest["frames"])
                filename = f"{folder.name}-frame-{index:04d}.png"
                source = (game_dir / "screenshots" / filename).resolve()
                if source.parent != (game_dir / "screenshots").resolve():
                    raise ValueError("invalid screenshot path")
                requested = time.monotonic()
                if requested >= end:
                    break
                # An admitted frame may finish within the existing evidence deadline.
                frame_token = DEADLINE.set(end + 15)
                try:
                    bot(config, args["client"], "screenshot " + filename)
                finally:
                    DEADLINE.reset(frame_token)
                captured = time.monotonic()
                manifest["frames"].append(
                    {
                        "file": f"frame-{index:04d}.png",
                        "source": filename,
                        "at_seconds": requested - started,
                        "completed_seconds": captured - started,
                        "saved": False,
                    }
                )
                next_frame = max(next_frame + 1 / fps, captured + 1 / fps)
            wake = min(next_frame, end)
            if pending:
                wake = min(wake, started + pending[0]["at"])
            time.sleep(max(0, wake - time.monotonic()))
        if pending or not manifest["frames"]:
            raise RuntimeError("capture ended before all actions and frames completed")
        verify_context()
    except (Exception, SystemExit) as failure:
        error = str(failure) or type(failure).__name__
    finally:
        manifest["recording_seconds"] = round(time.monotonic() - started, 6)
        DEADLINE.set(end + 15)
        total_bytes = 0
        try:
            for frame in manifest["frames"]:
                source = game_dir / "screenshots" / frame["source"]
                try:
                    if source.resolve().parent != (game_dir / "screenshots").resolve():
                        raise ValueError("invalid screenshot path")
                    data = screenshot_bytes(source, timeout=remaining_timeout(15))
                    total_bytes += len(data)
                    if total_bytes > 256 * 1024 * 1024:
                        raise RuntimeError("capture evidence exceeds 256 MiB")
                    (folder / frame["file"]).write_bytes(data)
                    frame["saved"] = True
                    try:
                        if source.read_bytes() == data:
                            source.unlink()
                    except OSError as failure:
                        frame["source_cleanup_error"] = str(failure)
                except (Exception, SystemExit) as failure:
                    error = error or str(failure) or type(failure).__name__
                    break
        finally:
            DEADLINE.reset(token)
    manifest["duration_seconds"] = round(time.monotonic() - started, 6)
    manifest["complete"] = error is None
    if error:
        manifest["error"] = error
    target = folder / "capture.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
    result = {
        "manifest": str(target),
        "folder": str(folder),
        "frames": sum(frame["saved"] for frame in manifest["frames"]),
        "duration_seconds": manifest["duration_seconds"],
        "complete": manifest["complete"],
    }
    if not error:
        return result
    result["error"] = error
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "isError": True,
    }
