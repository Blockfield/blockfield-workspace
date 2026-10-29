#!/usr/bin/env python3
"""Gameplay regression: lobby kit, authoritative shot, explosion vs armor plate."""

import re
import time

from .common import Bot, connect_rcon, dimension, is_lobby_dimension, number, rcon_args

SCENARIO = {
    "name": "check-gameplay",
    "help": "lobby kit, authoritative shot, armor plate checks",
}


def valid_name(player):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
        raise SystemExit(f"bad player name: {player}")


def lobby(rcon, bot):
    bot("bfaction leave")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = bot("state")
        if (
            is_lobby_dimension(dimension(state))
            and state.get("held") == "blockfield:command_tablet"
        ):
            break
        time.sleep(0.25)
    else:
        raise AssertionError(f"never reached lobby: {state}")
    _player, data = (
        state["name"],
        lambda path: rcon.cmd(f"data get entity {state['name']} {path}"),
    )
    assert state["slot"] == number(data("SelectedItemSlot")) == 8, state
    inventory = data("Inventory")
    assert re.findall(r'id: "([^"]+)"', inventory) == ["blockfield:command_tablet"], (
        inventory
    )
    rig = data(
        '"fabric:attachments"."accessories:inventory_holder".accessories_containers.rig.items'
    )
    assert rig.count('id: "blockfield:chest_rig"') == 1, rig
    assert 'id: "tacz:ammo"' not in rig, rig
    print("PASS lobby: synchronized tablet, no combat gear, one empty vest")


def shot(rcon, bot):
    state = bot("state")
    assert state["held"] == "tacz:modern_kinetic_gun", (
        "Select a loaded gun before checking"
    )
    path = f'Inventory[{{Slot:{state["slot"]}b}}].components."minecraft:custom_data".GunCurrentAmmoCount'
    before = number(rcon.cmd(f"data get entity {state['name']} {path}"))
    bot("closescreen")
    bot("look 0 -60")
    result = bot("tacz shoot")
    assert result.get("result") == "SUCCESS", result
    deadline, after = time.monotonic() + 3, before
    while after == before and time.monotonic() < deadline:
        time.sleep(0.1)
        after = number(rcon.cmd(f"data get entity {state['name']} {path}"))
    assert after == before - 1, (before, after)
    print(f"PASS authoritative shot: {before:g} -> {after:g}")


def armor(rcon, bot):
    state = bot("state")
    path = 'Inventory[{Slot:102b}].components."minecraft:custom_data".ArmorPlate'
    before = number(rcon.cmd(f"data get entity {state['name']} {path}"))
    assert before >= 5, "Wear armor with at least five plate points"
    health = state["health"]
    assert health > 5, "Heal before checking explosion damage"
    response = rcon.cmd(
        f"execute in {state['dimension']} run damage {state['name']} 5 minecraft:explosion"
    )
    assert "damage" in response.lower(), response
    time.sleep(0.3)
    after = number(rcon.cmd(f"data get entity {state['name']} {path}"))
    assert after == before - 5, (before, after)
    assert bot("state")["health"] < health, (
        "Explosion was incorrectly absorbed by the plate"
    )
    print(f"PASS explosion: plate {before:g} -> {after:g}, health also decreased")


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        state = bot("state")
        valid_name(state["name"])
        {"lobby": lobby, "shot": shot, "armor": armor}[args.check](rcon, bot)
    finally:
        bot.close()
    return 0


def register(sub):
    parser = sub.add_parser("check-gameplay", help=SCENARIO["help"])
    parser.add_argument(
        "check", choices=("lobby", "shot", "armor"), nargs="?", default="lobby"
    )
    rcon_args(parser)
    parser.set_defaults(func=run)
