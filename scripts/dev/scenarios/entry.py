#!/usr/bin/env python3
"""Entry through the stand proxy: RakNet join, both gate.json backends, reconnect.

Runbook (one part per client launch; the scenario never launches clients itself):
  just server start --via-proxy [--leak-paranoid]
  python -I scripts/dev/proxy_stand.py mint --game-dir "$BF_DEV_HOME/play" GameNick WorkshopNick
  GAME_DIAL=$(cat "$BF_DEV_HOME/proxy/game-dial.txt") # printed by proxy start
  just play --headless --server "$GAME_DIAL" --name GameNick
  just scenario entry --part join [--soak-secs 600]
  # workshop leg needs BOTH clients up (client 1 stays on the game backend):
  just client --client 2 --testbot
  python -I scripts/dev/proxy_stand.py mint --game-dir "$BF_DEV_HOME/play2" WorkshopNick
  WORKSHOP_DIAL=$(cat "$BF_DEV_HOME/proxy/workshop-dial.txt") # raknet;localhost:25567
  just play --client 2 --headless --server "$WORKSHOP_DIAL" --name WorkshopNick
  just scenario entry --part workshop
  just play --client 2 --stop
  just proxy restart [--paranoid]
  just play --headless --server "$GAME_DIAL" --name GameNick
  just scenario entry --part rejoin [--soak-secs 600]

Ported onto devstack RCON + the testbot (dm.py pattern: the workshop leg runs only
when a second bot port answers, else it is skipped with a note).
"""

import json
import time
from pathlib import Path

from .common import Bot, check, connect_rcon, rcon_args, tally

SCENARIO = {
    "name": "entry",
    "help": "stand proxy entry: RakNet join, gates, proxy-restart reconnect",
}


def gate_state(home, backend):
    gate = json.loads((Path(home) / "proxy" / "gate.json").read_text(encoding="utf-8"))
    return gate["backends"][backend]["gate"]


def online(rcon, name):
    """True once the backend RCON player list contains the nickname."""
    try:
        listed = rcon.cmd("list") or ""
    except (OSError, RuntimeError):
        return False
    return name in listed


def wait_online(rcon, name, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if online(rcon, name):
            return True
        time.sleep(5)
    return False


def scenario_join(world, home):
    """Game profile admitted through the proxy; Game gate open per gate.json."""
    import proxy_stand

    state = world.bot("state")
    name = state.get("name")
    out = [check("client connected to a server", bool(name), f"state={state!r}")]
    if not name:
        return out
    out.append(
        check(
            "game gate open per gate.json",
            gate_state(home, "game") == "public",
            "game backend not public",
        )
    )
    joined = wait_online(world.rcon, name)
    out.append(
        check(f"game profile {name} joined the backend through the proxy", joined, "")
    )
    root = proxy_stand.proxy_dir(home)
    evidence = []
    for log in (root / "velocity.out",):
        try:
            hits = [
                line
                for line in log.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if "RakNet" in line or "raknet" in line or name in line
            ]
        except OSError:
            hits = []
        evidence += hits[-3:]
    print("\n".join(evidence) or "(no RakNet/name lines in velocity.out yet)")
    return out


def scenario_workshop(world, home):
    """Workshop route closed per gate.json: nobody new reaches the backend.

    The denied client parks in proxy limbo (no server), so its testbot state
    has no player name; compare the backend roster instead of names.
    """
    out = []
    if world.bot2 is None:
        print("workshop leg needs a second client: skipped")
        return out
    try:
        world.bot2("state")
    except (OSError, RuntimeError) as error:
        return [check("workshop client answers on the testbot port", False, str(error))]
    out.append(check("workshop client answers on the testbot port", True, ""))
    out.append(
        check(
            "workshop route closed per gate.json",
            gate_state(home, "workshop") == "closed",
            "workshop backend not closed",
        )
    )
    before = world.rcon.cmd("list") or ""
    time.sleep(15)
    after = world.rcon.cmd("list") or ""
    out.append(
        check(
            "no new profile reached the backend while the workshop gate is closed",
            after == before,
            f"backend roster changed: {before!r} -> {after!r}",
        )
    )
    return out


def scenario_rejoin(world, home):
    """After a proxy restart the remembered session rejoins without a new login."""

    out = []
    name = world.bot("state").get("name")
    out.append(check("client connected to a server", bool(name), "state has no name"))
    if not name:
        return out
    joined = wait_online(world.rcon, name)
    out.append(
        check(f"game profile {name} rejoined after the proxy restart", joined, "")
    )
    out.append(
        check(
            "game gate still open per gate.json",
            gate_state(home, "game") == "public",
            "game backend not public after restart",
        )
    )
    return out


def soak(home, secs):
    """Idle window for the paranoid run, then the LEAK tally across proxy/server/client."""
    import proxy_stand
    from devstack import HOME, LOG

    if secs <= 0:
        return []
    print(f"soak {secs}s on the stand proxy path...")
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        left = int(deadline - time.monotonic())
        print(f"  ...{left}s left")
        time.sleep(min(60, left))
    leaks = proxy_stand.count_leaks(
        proxy_stand.proxy_dir(HOME) / "velocity.out", LOG, HOME / "client.out"
    )
    print(f"LEAK lines after {secs}s soak: {leaks}")
    return []


def run(
    args, rcon_factory=connect_rcon, bot_factory=Bot, get_log=None, server_dir=None
):
    from devstack import HOME

    _ = (get_log, server_dir)
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        try:
            bot2 = bot_factory(args.bot_port2)
            bot2("state")
        except (OSError, RuntimeError):
            bot2 = None
        world = type("World", (), {"rcon": rcon, "bot": bot, "bot2": bot2})
        out = []
        if args.part in ("join", "all"):
            out += scenario_join(world, HOME)
        if args.part in ("workshop", "all"):
            out += scenario_workshop(world, HOME)
        if args.part in ("rejoin", "all"):
            out += scenario_rejoin(world, HOME)
        out += soak(HOME, args.soak_secs)
        return tally(out) and 1 or 0
    finally:
        bot.close()


def register(sub):
    parser = sub.add_parser("entry", help=SCENARIO["help"])
    parser.add_argument(
        "--part", choices=("join", "workshop", "rejoin", "all"), default="join"
    )
    parser.add_argument("--bot-port2", type=int, default=47778)
    parser.add_argument(
        "--soak-secs",
        type=int,
        default=0,
        help="idle on the proxy path first, then count Netty LEAK lines (600 in the brief)",
    )
    rcon_args(parser)
    parser.set_defaults(func=run)
