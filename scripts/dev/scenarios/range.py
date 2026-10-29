#!/usr/bin/env python3
"""Firing-range checks on dev clients (partial range-test.sh port).

Entry/exit, kit, refill and no-stats checks run on one client. Reconnect-lands-in-hub
needs a second client. The PvP matrix is not covered here.
"""

import re
import time
from pathlib import Path

from .common import (
    Bot,
    check,
    connect_rcon,
    dimension,
    is_lobby_dimension,
    rcon_args,
    tally,
)

SCENARIO = {
    "name": "range",
    "help": "firing-range acceptance: entry, kit, refill, reconnect",
}
DIM = "blockfield_shared:firing_range"


def enter(bot, dim=DIM, timeout=20):
    bot("closescreen")
    bot("bfaction range:enter")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dimension(bot("state")) == dim:
            return True
        time.sleep(0.5)
    return False


def leave(bot):
    bot("bfaction pvp:off")
    bot("bfaction range:exit")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if is_lobby_dimension(dimension(bot("state"))):
            return True
        time.sleep(0.5)
    return False


def items(rcon, player, item):
    match = re.search(
        r"(\d+)$",
        rcon.cmd(f"execute if items entity {player} container.* {item}") or "",
    )
    return int(match.group(1)) if match else 0


def solo(world_rcon, bot, server_dir, stats_since):
    out, state = [], bot("state")
    player = state["name"]
    out.append(check("range entry", enter(bot), dimension(bot("state"))))
    bot("bfaction class:assault:0")
    time.sleep(1)
    guns_first = items(world_rcon, player, "tacz:modern_kinetic_gun")
    time.sleep(3.2)
    bot("bfaction range:refill")
    time.sleep(1)
    bot("bfaction range:refill")
    time.sleep(1)
    guns_second = items(world_rcon, player, "tacz:modern_kinetic_gun")
    time.sleep(3.2)
    bot("bfaction class:assault:0")
    time.sleep(1)
    guns_third = items(world_rcon, player, "tacz:modern_kinetic_gun")
    tablet = items(world_rcon, player, "blockfield:command_tablet")
    out.append(
        check(
            "kit/refill/reselect do not duplicate",
            guns_first >= 1 and guns_first == guns_second == guns_third and tablet == 1,
            f"{guns_first}/{guns_second}/{guns_third} tablet={tablet}",
        )
    )
    world_rcon.cmd(f"item replace entity {player} player.cursor with tacz:ammo 64")
    out.append(check("exit returns to hub", leave(bot), dimension(bot("state"))))
    cursor = world_rcon.cmd(f"execute if items entity {player} player.cursor *") or ""
    drops = world_rcon.cmd(f"execute in {DIM} if entity @e[type=item]") or ""
    guns = items(world_rcon, player, "tacz:modern_kinetic_gun")
    out.append(
        check(
            "no cursor item, no drop, no range gear in hub",
            "passed" not in cursor and "passed" not in drops and guns == 0,
            f"cursor={cursor[:60]} drops={drops[:60]} guns={guns}",
        )
    )
    fresh = [
        f
        for f in (Path(server_dir) / "blockfield" / "stats").rglob("*")
        if f.is_file() and f.stat().st_mtime > stats_since
    ]
    out.append(check("no stats files written", not fresh, str(fresh[:1])))
    return out


def duo(world_rcon, bot, bot2):
    out = []
    bot2("closescreen")
    bot2("bfaction range:enter")
    time.sleep(2)
    bot2("bfaction class:assault:0")
    time.sleep(1)
    bot2("disconnect")
    time.sleep(5)
    bot2("connect 127.0.0.1:25599")
    deadline = time.monotonic() + 80
    while time.monotonic() < deadline and not bot2("state").get("connected"):
        time.sleep(2)
    time.sleep(3)
    dim = dimension(bot2("state"))
    victim_guns = items(world_rcon, bot2("state")["name"], "tacz:modern_kinetic_gun")
    out.append(
        check(
            "reconnect from range lands in hub without gear",
            is_lobby_dimension(dim) and victim_guns == 0,
            f"{dim} guns={victim_guns}",
        )
    )
    return out


def run(args, rcon_factory=connect_rcon, bot_factory=Bot, server_dir=None):
    from devstack import SERVER

    server_dir = Path(server_dir or SERVER)
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        try:
            bot2 = bot_factory(args.bot_port2)
            bot2("state")
        except (OSError, RuntimeError):
            bot2 = None
            print("second client not answering: duo steps skipped")
        stats_since = time.time()
        out = solo(rcon, bot, server_dir, stats_since)
        if bot2:
            out += duo(rcon, bot, bot2)
            bot2.close()
        return tally(out) and 1 or 0
    finally:
        bot.close()


def register(sub):
    parser = sub.add_parser("range", help=SCENARIO["help"])
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
