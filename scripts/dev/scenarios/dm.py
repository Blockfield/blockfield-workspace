#!/usr/bin/env python3
"""Deathmatch checks on two dev clients: entry, deploy, gun kill, respawn,
suicide, minimum roster and spawn protection.

Waiting players stand INSIDE the match dimension by design (the respawn
platform is built in the open match world, RespawnRoom in-dim destination),
so every wait asserts the dm_arena dimension, never blockfield_shared:respawn.
Teleports and aiming happen only after GAME start + deploy (the deploy moves
players), and the deploy wait is state-driven: both bots hold the kit gun.
Kills are confirmed against the server log (`Комната N: X убит ...`): the
stand sets instant respawn, so the victim's client never shows a death
screen and health-0 is a one-tick flicker no poll can rely on.
Needs both dev clients (testbot 47777 + 47778): with one bot the parts end
INCONCLUSIVE (exit 2), never a pass.
"""

import json
import re
import time
from pathlib import Path

from . import readiness as _readiness
from .common import Bot, check, connect_rcon, dimension, rcon_args, tally, wait_for
from .duo import GUN, face_off, kit_or_raise

SCENARIO = {
    "name": "dm",
    "help": "deathmatch acceptance: kills, respawn, reconnect, limits",
}
ARENA_PREFIX = "blockfield:dm_arena"
DEATH_SCREEN = "DeathScreen"
MIN_PLAYERS_MSG = "Нужно минимум 2"


def dead_in_arena(state, arena):
    """A kill from the bot's point of view: still in the match dimension
    (the in-match wait), reached via the death screen or zero health."""
    return dimension(state) == arena and (
        (state.get("screen") or "") == DEATH_SCREEN or state.get("health", 20) <= 0
    )


class World:
    def __init__(self, rcon, bot, bot2=None, log_lines=None, name_timeout=30.0):
        self.rcon, self.bot, self.bot2, self.log_lines = (
            rcon,
            bot,
            bot2,
            log_lines or [],
        )
        self.names = [bot("state")["name"]]
        if bot2 is not None:
            # The second client may still be connecting (no player yet, so no
            # "name" in state): wait for it instead of KeyError'ing.
            matched, state, _ = wait_for(
                lambda remaining: bot2("state", remaining),
                lambda s: "name" in s,
                name_timeout,
            )
            if matched:
                self.names.append(state["name"])
            else:
                close = getattr(bot2, "close", None)
                if close is not None:
                    try:
                        close()
                    except (OSError, RuntimeError):
                        pass
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
    # dm_arena is a product map now (deathmatch enabled, pinned template).
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


def wait_arena(world, client=1, timeout=60.0):
    """Block until the bot stands in the match dimension (in-dim wait from
    join on); returns (matched, dimension)."""
    bot = world.bot if client == 1 else world.bot2
    matched, state, _ = wait_for(
        lambda remaining: bot("state", remaining),
        lambda s: dimension(s).startswith(ARENA_PREFIX),
        timeout,
    )
    return matched, dimension(state) if isinstance(state, dict) else ""


def wait_deployed(world, timeout=90.0):
    """Block until GAME start deployed both bots: each holds the kit gun.

    Class picks land during PREPARING; whoever deploys first does so in
    onStart, whoever picks after GAME start deploys via spawn:BASE (a no-op
    while it is refused), so keep offering it until the kits show up.
    """
    for bot in (world.bot, world.bot2):
        bot("slot 0")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for bot in (world.bot, world.bot2):
            try:
                bot("bfaction spawn:BASE")
            except (OSError, RuntimeError):
                pass
        try:
            states = (world.bot("state"), world.bot2("state"))
        except (OSError, RuntimeError):
            states = ()
        if (
            len(states) == 2
            and all(s.get("held") == GUN for s in states)
            and all(dimension(s).startswith(ARENA_PREFIX) for s in states)
        ):
            return True
        time.sleep(1)
    return False


def wait_vulnerable(rcon, arena, attacker, victim, timeout=60.0):
    """Spawn protection ticks down on GAME ticks: probe with 1 knife damage
    until the server stops reporting invulnerable. Chips 1 HP off the victim
    (callers re-baseline HP afterwards); never spends gun ammo."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = (
            rcon.cmd(
                f"execute in {arena} run damage {victim} 1 blockfield:knife by {attacker}"
            )
            or ""
        )
        if "invulnerable" not in reply:
            return True
        time.sleep(1)
    return False


def wait_log(world, pattern, timeout=30.0):
    """Block until the server log since mark matches; returns (matched, line).

    Deaths are confirmed here, not on the victim's client: with instant
    respawn the death screen never opens and health-0 lasts a tick.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in world.log_since().splitlines():
            if pattern.search(line):
                return True, line.strip()
        time.sleep(0.5)
    return False, ""


def wait_messages(bot, needle, timeout=30.0):
    """Block until the bot's chat log contains the needle (raw lines, not
    json.dumps: the dump ASCII-escapes Cyrillic so a Cyrillic needle would
    never match)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = bot("messages 20") or []
        except (OSError, RuntimeError):
            lines = []
        text = "\n".join(lines) if isinstance(lines, list) else str(lines)
        if needle in text:
            return True
        time.sleep(1)
    return False


def redeploy(world, client, timeout=60.0):
    """One respawn cycle: the replacement entity is already back (instant
    respawn; the `respawn` request only goes out when the client still shows
    the death screen), wait out the respawn timer via spawn:BASE polling and
    confirm the kit is back (the inventory is wiped on death, so held gun
    proves the new deploy)."""
    bot = world.bot if client == 1 else world.bot2
    try:
        state = bot("state")
    except (OSError, RuntimeError):
        state = {}
    if (state.get("screen") or "") == DEATH_SCREEN or state.get("health", 20) <= 0:
        bot("respawn")
    matched, _, _ = wait_for(
        lambda remaining: bot("state", remaining),
        lambda s: dimension(s).startswith(ARENA_PREFIX) and (s.get("health") or 0) > 0,
        timeout / 2,
    )
    if not matched:
        return False
    deadline = time.monotonic() + timeout / 2
    while time.monotonic() < deadline:
        try:
            bot("bfaction spawn:BASE")
        except (OSError, RuntimeError):
            pass
        try:
            kit_or_raise(bot, timeout=3.0)
            return True
        except (TimeoutError, OSError, RuntimeError):
            time.sleep(1)
    return False


def arena_bounds(server_dir, map_id="dm_arena"):
    """(minX, maxX, minZ, maxZ) from the deployed map config, or None.

    The gunfight clamps its lanes inside these so a corner deploy spot never
    parks the shooter outside the battle boundary (which kills outsiders).
    """
    if not server_dir:
        return None
    try:
        raw = json.loads(
            (Path(server_dir) / "blockfield" / "maps" / f"{map_id}.json").read_text(
                encoding="utf-8"
            )
        )
        bounds = raw["bounds"]
        return (
            bounds["minX"],
            bounds["maxX"],
            bounds["minZ"],
            bounds["maxZ"],
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def scenario_basic(world, server_dir=None, out=None):
    """Entry, deploy, gun kill, two kill->respawn cycles.

    `out` is the caller's accumulator: rows survive a mid-scenario NotReady.
    """
    out = [] if out is None else out
    if not world.duo:
        raise _readiness.NotReady(
            "bot2 probe answered", "basic needs two clients: second not answering"
        )
    missing = world.rcon.cmd("bf start deathmatch no_such_arena_xyz") or ""
    out.append(check("missing arena rejected", "Ошибка" in missing, missing[:120]))
    room = open_room(world)
    out.append(
        check(f"hub button opened deathmatch room {room}", bool(room), "no room in log")
    )
    if not room:
        return out
    world.bot2("bfaction join:" + room)
    match1, dim1 = wait_arena(world, 1)
    match2, dim2 = wait_arena(world, 2)
    out.append(
        check(
            "both wait in dm_arena during preparation",
            match1 and match2,
            f"{dim1} / {dim2}",
        )
    )
    if not (match1 and match2):
        return out
    world.bot("bfaction class:assault:0")
    world.bot2("bfaction class:assault:0")
    deployed = wait_deployed(world)
    arena = world.dim(1)
    out.append(
        check(
            "both deployed with kit after GAME start",
            deployed and arena.startswith(ARENA_PREFIX),
            f"{world.dim(1)} / {world.dim(2)}",
        )
    )
    if not deployed:
        return out
    a, v = world.names[0], world.names[1]
    out.append(
        check(
            "spawn protection lapses before the gunfight",
            wait_vulnerable(world.rcon, arena, a, v),
            arena,
        )
    )

    # A counted death wipes the victim's kit (clearInventoryAndCurios) and
    # the replacement redeploys kitless: held gun -> held anything else is
    # the kill signal. Gun kills leave no "убит" server-log line, and with
    # instant respawn the death screen never opens, so neither is usable.
    def kit_wiped(state):
        return dimension(state) == arena and state.get("held") != GUN

    ok, detail = face_off(
        world.rcon,
        world.bot,
        world.bot2,
        dead=kit_wiped,
        bounds=arena_bounds(server_dir),
        until_dead=True,
        get_log=lambda: "\n".join(world.log_lines),
        arena=arena,
    )
    out.append(check("real gun kill moves victim to the in-match wait", ok, detail))
    if not ok:
        return out
    out.append(
        check(
            "victim waits in the match dimension",
            world.dim(2) == arena,
            world.dim(2),
        )
    )
    out.append(
        check(
            "victim redeploys after respawn delay",
            redeploy(world, 2),
            world.dim(2),
        )
    )
    if out[-1]:
        return out
    arena = world.dim(1)
    out.append(
        check(
            "spawn protection lapses before the suicide",
            wait_vulnerable(world.rcon, arena, a, v),
            arena,
        )
    )
    world.mark()
    world.bot2("bfaction kill")
    dead, line = wait_log(
        world, re.compile(rf"{re.escape(v)} убит - \(outOfWorld\)"), timeout=30.0
    )
    out.append(
        check(
            "suicide counted as death, victim waits in match",
            dead,
            line or world.dim(2),
        )
    )
    if not dead:
        return out
    out.append(
        check(
            "victim waits in the match dimension",
            world.dim(2) == arena,
            world.dim(2),
        )
    )
    out.append(
        check(
            "victim redeploys after suicide",
            redeploy(world, 2),
            world.dim(2),
        )
    )
    return out


def scenario_limits(world, rules_path, out=None):
    """Minimum roster and spawn protection (`out` as in scenario_basic)."""
    out = [] if out is None else out
    if not world.duo:
        raise _readiness.NotReady(
            "bot2 probe answered", "limits needs two clients: second not answering"
        )
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
        if not room:
            out.append(
                check("hub button opened deathmatch room", False, "no room in log")
            )
            return out
        match1, dim1 = wait_arena(world, 1)
        gated = match1 and wait_messages(world.bot, MIN_PLAYERS_MSG, timeout=30.0)
        out.append(
            check(
                "one player does not start the match",
                gated and world.dim(1).startswith(ARENA_PREFIX),
                dim1,
            )
        )
        if not gated:
            return out
        world.bot2("bfaction join:" + room)
        match2, _ = wait_arena(world, 2)
        if not match2:
            out.append(check("second player reaches the arena", False, world.dim(2)))
            return out
        world.bot("bfaction class:assault:0")
        world.bot2("bfaction class:assault:0")
        deployed = wait_deployed(world)
        arena = world.dim(1)
        out.append(
            check(
                "match starts once the second player joins",
                deployed and arena.startswith(ARENA_PREFIX),
                f"{world.dim(1)} / {world.dim(2)}",
            )
        )
        if not deployed:
            return out
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
        out.append(
            check(
                "spawn protection lapses after deploy",
                wait_vulnerable(world.rcon, arena, a, v),
                arena,
            )
        )
        world.mark()
        world.rcon.cmd(f"execute in {arena} run damage {v} 100 minecraft:lava")
        dead, line = wait_log(
            world, re.compile(rf"{re.escape(v)} убит - \(lava\)"), timeout=30.0
        )
        out.append(
            check(
                "environment death gives no frag, victim waits in match",
                dead,
                line or world.dim(2),
            )
        )
        if not dead:
            return out
        out.append(
            check(
                "victim redeploys after respawn delay",
                redeploy(world, 2, timeout=90.0),
                world.dim(2),
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
    bot2 = None
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
        try:
            if args.part in ("basic", "all"):
                if not world.duo:
                    raise _readiness.NotReady(
                        "bot2 probe answered",
                        "basic needs two clients: second not answering",
                    )
                scenario_basic(world, server_dir or SERVER, out)
            if args.part in ("limits", "all"):
                if not world.duo:
                    raise _readiness.NotReady(
                        "bot2 probe answered",
                        "limits needs two clients: second not answering",
                    )
                scenario_limits(
                    world,
                    Path(server_dir or SERVER)
                    / "blockfield"
                    / "modes"
                    / "deathmatch.json",
                    out,
                )
        except _readiness.NotReady as error:
            print(f"INCONCLUSIVE dm: {error}", flush=True)
            # Product failures recorded before the abort keep exit 1.
            return 1 if tally(out) else _readiness.INCONCLUSIVE
        return tally(out) and 1 or 0
    finally:
        bot.close()
        if bot2 is not None:
            try:
                bot2.close()
            except (OSError, RuntimeError):
                pass


def register(sub):
    parser = sub.add_parser("dm", help=SCENARIO["help"])
    parser.add_argument("--part", choices=("basic", "limits", "all"), default="all")
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
