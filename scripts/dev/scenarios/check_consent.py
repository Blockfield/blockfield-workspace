#!/usr/bin/env python3
"""PvP-consent regression: a passive player keeps HP and position through mine and vehicle hits.

Needs two clients (deferred until the second dev client lands): with one bot the
module reports the skip instead of failing.
"""

import json
import re
import time
from uuid import uuid4

from .common import Bot, connect_rcon, dimension, is_lobby_dimension, rcon_args

SCENARIO = {
    "name": "check-consent",
    "help": "passive player keeps HP/pos through mine + vehicle",
}
TAG = "bf_consent_regression"


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    first = bot_factory(args.bot_port)
    try:
        try:
            second = bot_factory(args.bot_port2)
            second("state")
        except (OSError, RuntimeError):
            print("needs two clients: skipped until the second dev client lands")
            return 0
        try:
            players = [first("state"), second("state")]
            lobby_dim = dimension(players[0])
            for state in players:
                assert is_lobby_dimension(dimension(state)), state
                assert dimension(state) == lobby_dim, (
                    "Both clients must share one lobby"
                )
                assert state["health"] == 20, "Heal both players first"
                assert re.fullmatch(r"[A-Za-z0-9_]{1,16}", state["name"])
            victim, driver = (s["name"] for s in players)
            scoped = lambda command: rcon.cmd(f"execute in {lobby_dim} run {command}")
            tag = f"{TAG}_{uuid4().hex}"
            floor = "-256 0 624 -244 0 636"
            for x in range(-256, -243):
                for z in range(624, 637):
                    assert "CONSENT_AIR" in scoped(
                        f"execute if block {x} 0 {z} minecraft:air run say CONSENT_AIR"
                    ), f"Consent floor is occupied at {x} 0 {z}"
            position = [-250.5, 1.0, 630.5]
            evidence = {}
            try:
                scoped(f"fill {floor} minecraft:stone replace minecraft:air")
                scoped(f"tp {victim} -250.5 1 630.5")
                scoped(f"tp {driver} -255.5 1 625.5")
                first("bfaction pvp:off")
                time.sleep(1)
                for scenario, entity, offset in (
                    ("mine", "claymore", -1.5),
                    ("vehicle", "lav_150", -3),
                ):
                    scoped(
                        f"summon superbwarfare:{entity} {position[0]} {position[1]} {position[2] + offset} "
                        f'{{Tags:["{tag}"],Rotation:[0f,0f]}}'
                    )
                    if scenario == "vehicle":
                        scoped(f"ride {driver} mount @e[tag={tag},limit=1]")
                    samples = []
                    for i in range(25):
                        if scenario == "vehicle" and i < 5:
                            scoped(
                                f"data merge entity @e[tag={tag},limit=1] {{Motion:[0d,0d,0.8d]}}"
                            )
                        time.sleep(0.15)
                        samples.append(first("state"))
                    evidence[scenario] = samples
                    if scenario == "mine":
                        assert "FOUND" not in scoped(
                            f"execute if entity @e[tag={tag}] run say FOUND"
                        ), "Mine did not detonate"
                    assert all(
                        s["health"] == 20 and s["pos"] == position for s in samples
                    ), scenario
                    scoped(f"ride {driver} dismount")
                    scoped(f"kill @e[tag={tag}]")
                print(
                    "PASS: passive player retains 20 HP and exact position after mine and occupied vehicle impacts"
                )
            finally:
                print(json.dumps(evidence))
                scoped(f"ride {driver} dismount")
                scoped(f"kill @e[tag={tag}]")
                for state in players:
                    scoped(f"tp {state['name']} " + " ".join(map(str, state["pos"])))
                scoped(f"fill {floor} minecraft:air replace minecraft:stone")
        finally:
            second.close()
    finally:
        first.close()
    return 0


def register(sub):
    parser = sub.add_parser("check-consent", help=SCENARIO["help"])
    parser.add_argument("--bot-port2", type=int, default=47778)
    rcon_args(parser)
    parser.set_defaults(func=run)
