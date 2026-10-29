#!/usr/bin/env python3
"""Deathmatch checks on two dev clients: entry, deploy, gun kill, respawn,
suicide, minimum roster and spawn protection.

Ported from dm-test.sh + dm-test2.sh onto devstack RCON + testbot (single client
in this iteration: the second player's steps run when a second bot port answers,
else they are skipped with a note).
"""

import json
import re
import time
from pathlib import Path

from .common import Bot, check, connect_rcon, dimension, rcon_args, tally

SCENARIO = {
    "name": "dm",
    "help": "deathmatch acceptance: kills, respawn, reconnect, limits",
}
RESPAWN = "blockfield_shared:respawn"


class World:
    def __init__(self, rcon, bot, bot2=None, log_lines=None):
        self.rcon, self.bot, self.bot2, self.log_lines = (
            rcon,
            bot,
            bot2,
            log_lines or [],
        )
        self.names = [bot("state")["name"]]
        if bot2:
            try:
                self.names.append(bot2("state")["name"])
            except (OSError, RuntimeError):
                self.bot2 = None

    @property
    def duo(self):
        return self.bot2 is not None

    def dim(self, client=1):
        bot = self.bot if client == 1 else self.bot2
        return dimension(bot("state"))

    def log_since(self):
        return "\n".join(self.log_lines[self.log_mark :])

    def mark(self):
        self.log_mark = len(self.log_lines)


def open_room(world, timeout=30):
    world.mark()
    world.bot("closescreen")
    if world.bot2:
        world.bot2("closescreen")
    world.bot("bfaction play:deathmatch:dm_arena")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        match = re.findall(
            r"Комната (\d+) открыта \(Каждый сам за себя\)", world.log_since()
        )
        if match:
            return match[-1]
        time.sleep(1)
    return ""


def scenario_basic(world):
    """Missing arena, real kill, redeploy and suicide."""
    out = []
    missing = world.rcon.cmd("bf start deathmatch no_such_arena_xyz") or ""
    out.append(check("missing arena rejected", "Ошибка" in missing, missing[:120]))
    room = open_room(world)
    out.append(
        check(f"hub button opened deathmatch room {room}", bool(room), "no room in log")
    )
    if not room:
        return out
    world.bot2("bfaction join:" + room)
    time.sleep(3)
    out.append(
        check(
            "both wait in respawn during preparation",
            world.dim(1) == RESPAWN and world.dim(2) == RESPAWN,
            f"{world.dim(1)} / {world.dim(2)}",
        )
    )
    world.bot("bfaction class:assault:0")
    world.bot2("bfaction class:assault:0")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (
        world.dim(1).startswith("blockfield:dm_arena")
        and world.dim(2).startswith("blockfield:dm_arena")
    ):
        time.sleep(1)
    arena = world.dim(1)
    deployed = arena.startswith("blockfield:dm_arena") and world.dim(2).startswith(
        "blockfield:dm_arena"
    )
    out.append(
        check(
            f"both deployed into {arena}", deployed, f"{world.dim(1)} / {world.dim(2)}"
        )
    )
    a, v = world.names[0], world.names[1]
    time.sleep(3)
    world.rcon.cmd(f"execute in {arena} run tp {a} 0.5 65 -20.5 0 0")
    world.rcon.cmd(f"execute in {arena} run tp {v} 0.5 65 -14.5 180 0")
    time.sleep(1.5)
    world.bot("slot 0")
    time.sleep(3)
    world.bot("lookat 0.5 66.1 -14.5")
    time.sleep(1)
    dead = False
    for _ in range(30):
        world.bot("tacz shoot")
        time.sleep(0.35)
        if world.dim(2) == RESPAWN:
            dead = True
            break
    out.append(
        check("real gun kill moves victim to respawn room", dead, "no kill in 30 shots")
    )
    time.sleep(4)
    world.bot2("bfaction spawn:BASE")
    time.sleep(2)
    out.append(
        check(
            "victim redeploys after respawn delay",
            world.dim(2).startswith("blockfield:dm_arena"),
            world.dim(2),
        )
    )
    time.sleep(3)
    world.bot2("bfaction kill")
    time.sleep(2)
    out.append(
        check(
            "suicide counted as death, victim waits",
            world.dim(2) == RESPAWN,
            world.dim(2),
        )
    )
    return out


def scenario_limits(world, rules_path):
    """Minimum roster and spawn protection."""
    out = []
    backup = rules_path.read_text(encoding="utf-8") if rules_path.is_file() else None
    try:
        rules_path.write_text(
            json.dumps(
                {
                    "kill_limit": 30,
                    "time_limit_seconds": 600,
                    "preparation_seconds": 5,
                    "minimum_players": 2,
                    "respawn_seconds": 30,
                    "protection_seconds": 6,
                }
            )
        )
        room = open_room(world)
        time.sleep(9)
        msgs = json.dumps(world.bot("messages 20"))
        out.append(
            check(
                "one player does not start the match",
                "Нужно минимум 2" in msgs and world.dim(1) == RESPAWN,
                world.dim(1),
            )
        )
        world.bot2("bfaction join:" + room)
        time.sleep(2)
        world.bot2("bfaction class:assault:0")
        arena_ok = world.dim(1).startswith("blockfield:dm_arena")
        out.append(
            check("match starts once the second player joins", arena_ok, world.dim(1))
        )
        if not arena_ok:
            return out
        arena = world.dim(1)
        a, v = world.names[0], world.names[1]
        blocked = (
            world.rcon.cmd(
                f"execute in {arena} run damage {v} 4 blockfield:knife by {a}"
            )
            or ""
        )
        out.append(
            check(
                "protected player takes no damage",
                "invulnerable" in blocked,
                blocked[:120],
            )
        )
        world.rcon.cmd(f"execute in {arena} run damage {v} 100 minecraft:lava")
        time.sleep(2)
        out.append(
            check(
                "environment death gives no frag", world.dim(2) == RESPAWN, world.dim(2)
            )
        )
    finally:
        if backup is not None:
            rules_path.write_text(backup)
        else:
            rules_path.unlink(missing_ok=True)
    return out


def run(
    args, rcon_factory=connect_rcon, bot_factory=Bot, get_log=None, server_dir=None
):
    from devstack import LOG as dev_log, SERVER

    logs = get_log or (lambda: dev_log.read_text(encoding="utf-8", errors="replace"))
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        try:
            bot2 = bot_factory(args.bot_port2)
            bot2("state")
        except (OSError, RuntimeError):
            bot2 = None
            print("second client not answering: duo steps skipped")
        world = World(rcon, bot, bot2, logs().splitlines())

        # Keep the log live: re-read on every access.
        def refresh():
            world.log_lines = logs().splitlines()

        world.mark = lambda: (
            refresh(),
            setattr(world, "log_mark", len(world.log_lines)),
        )
        world.log_since = lambda: (
            refresh(),
            "\n".join(world.log_lines[world.log_mark :]),
        )[1]
        world.log_mark = 0
        out = []
        if args.part in ("basic", "all"):
            if not world.duo:
                print("basic needs two clients: skipped")
            else:
                out += scenario_basic(world)
        if args.part in ("limits", "all"):
            if not world.duo:
                print("limits needs two clients: skipped")
            else:
                out += scenario_limits(
                    world,
                    Path(server_dir or SERVER)
                    / "blockfield"
                    / "modes"
                    / "deathmatch.json",
                )
        return tally(out) and 1 or 0
    finally:
        bot.close()


def register(sub):
    parser = sub.add_parser("dm", help=SCENARIO["help"])
    parser.add_argument("--part", choices=("basic", "limits", "all"), default="all")
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
