#!/usr/bin/env python3
"""Hit registration vs ping: the dev client shoots a Carpet bot sprinting across its line of fire.

    just scenario hitreg setup <dimension> [--room 1]
    just scenario hitreg run [--shots 40] [--still] [--mode track|flick]

A hit = the bot lost health; prints the hit rate as one JSON line.
"""

import json
import re
import time

from .common import Bot, connect_rcon, rcon_args

SCENARIO = {
    "name": "hitreg",
    "help": "shoot a sprinting Carpet bot, report the hit rate",
}

BOT_NAME, CLIENT, Y = "hitbot", None, 150
SHOOTER = (0.5, Y + 1, -19.5)
LANE = (-8, 8)


def health(rcon, name=BOT_NAME):
    match = re.search(r"([\d.]+)f", rcon.cmd(f"data get entity {name} Health") or "")
    return float(match.group(1)) if match else -1.0


def setup(rcon, dim, room, client):
    if BOT_NAME not in (rcon.cmd("list") or "").lower():
        rcon.cmd(f"player {BOT_NAME} spawn")
        time.sleep(3)
        rcon.cmd(f"bf action {BOT_NAME} join:{room}")
        rcon.cmd(f"bf action {BOT_NAME} class:assault:0")
        for _ in range(20):
            rcon.cmd(f"bf action {BOT_NAME} spawn:any")
            time.sleep(1.5)
            if dim in (rcon.cmd(f"data get entity {BOT_NAME} Dimension") or ""):
                break
    rcon.cmd(f"execute in {dim} run fill -12 {Y} -24 12 {Y} 6 minecraft:barrier")
    rcon.cmd(f"execute in {dim} run fill -12 {Y + 1} -24 12 {Y + 4} 6 minecraft:air")
    rcon.cmd(f"execute in {dim} run fill -11 {Y + 1} 2 11 {Y + 3} 2 minecraft:barrier")
    rcon.cmd(
        f"execute in {dim} run fill -11 {Y + 1} -3 -11 {Y + 3} 2 minecraft:barrier"
    )
    rcon.cmd(f"execute in {dim} run fill 11 {Y + 1} -3 11 {Y + 3} 2 minecraft:barrier")
    rcon.cmd(
        f"execute in {dim} run tp {client} {SHOOTER[0]} {SHOOTER[1]} {SHOOTER[2]} 0 0"
    )
    rcon.cmd(f"execute in {dim} run tp {BOT_NAME} 0.5 {Y + 1} 0.5 90 0")
    for slot in ("armor.head", "armor.chest", "armor.legs", "armor.feet"):
        rcon.cmd(f"item replace entity {BOT_NAME} {slot} with minecraft:air")
    rcon.cmd(f"effect give {BOT_NAME} minecraft:resistance infinite 0 true")
    print(f"platform in {dim}; hold ADS before run")


def run_shots(rcon, bot, shots, still, mode):
    rcon.cmd(f"player {BOT_NAME} stop")
    if not still:
        rcon.cmd(f"player {BOT_NAME} move forward")
        rcon.cmd(f"player {BOT_NAME} sprint")
    hits, fired, misfires, last_turn = 0, 0, 0, time.time()
    bot(f"track {BOT_NAME}" if mode == "track" else "track off")
    if mode == "track":
        time.sleep(1)
    while fired < shots:
        if not still and time.time() - last_turn > 1.4:
            rcon.cmd(f"player {BOT_NAME} turn back")
            last_turn = time.time()
        rcon.cmd(f"effect give {BOT_NAME} minecraft:instant_health 1 5 true")
        time.sleep(0.4)
        before = health(rcon)
        res = bot(f"aimshoot {BOT_NAME}") if mode == "flick" else bot("tacz shoot")
        if res.get("result") != "SUCCESS":
            misfires += 1
            if misfires > 20:
                raise SystemExit(f"aimshoot keeps failing: {res}")
            if res.get("result") == "NO_AMMO":
                bot("tacz reload")
                time.sleep(3.5)
            time.sleep(0.5)
            continue
        fired += 1
        time.sleep(0.9)
        if health(rcon) < before:
            hits += 1
    rcon.cmd(f"player {BOT_NAME} stop")
    bot("track off")
    state = bot("state")
    print(
        json.dumps(
            {
                "mode": mode,
                "shots": fired,
                "hits": hits,
                "rate": round(hits / max(fired, 1), 2),
                "still": still,
                "client_ping_ms": state.get("ping"),
                "server": state.get("server"),
            }
        ),
        flush=True,
    )


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        if args.action == "setup":
            client = args.client or bot("state")["name"]
            setup(rcon, args.dimension, args.room, client)
        else:
            run_shots(rcon, bot, args.shots, args.still, args.mode)
    finally:
        bot.close()
    return 0


def register(sub):
    parser = sub.add_parser("hitreg", help=SCENARIO["help"])
    parser.add_argument("action", choices=("setup", "run"))
    parser.add_argument("dimension", nargs="?")
    parser.add_argument("--room", default="1")
    parser.add_argument(
        "--client",
        default=None,
        help="shooter name (default: the connected bot client)",
    )
    parser.add_argument("--shots", type=int, default=40)
    parser.add_argument("--still", action="store_true")
    parser.add_argument("--mode", choices=("track", "flick"), default="track")
    rcon_args(parser)
    parser.set_defaults(func=run)
