#!/usr/bin/env python3
"""Benelli shell-reload: functional ammo check; frames only with a GUI client.

Without a visible client the `screenshot` bot command captures nothing to review,
so frames are collected only when --frames is passed (GUI session) and the JSON
always flags whether visual review happened.
"""

import json
import re
import time

from .common import Bot, connect_rcon, rcon_args

SCENARIO = {
    "name": "check-benelli",
    "help": "Benelli reload: ammo count (+ frames with --frames)",
}


def count(response):
    match = re.search(r"data: (\d+)$", response or "")
    if not match:
        raise AssertionError(response)
    return int(match.group(1))


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        state = bot("state")
        player = state["name"]
        assert re.fullmatch(r"[A-Za-z0-9_]{1,16}", player)
        inventory = rcon.cmd(f"data get entity {player} Inventory") or ""
        assert not re.search(r'id: "tacz:(ammo|ammo_box)"', inventory), (
            "Move reserve ammunition into the vest"
        )
        gun_path = (
            f'Inventory[{{Slot:{state["slot"]}b}}].components."minecraft:custom_data"'
        )
        assert "elitex:benelli_super" in rcon.cmd(
            f"data get entity {player} {gun_path}.GunId"
        )
        before = count(
            rcon.cmd(f"data get entity {player} {gun_path}.GunCurrentAmmoCount")
        )
        assert 0 <= before <= 3, (
            "Use a magazine with at most three rounds for a visible loop sequence"
        )
        frames = []
        bot("closescreen")
        try:
            bot("hold key.forward 80")
            bot("hold key.sprint 80")
            time.sleep(0.4)
            assert bot("state")["sprinting"], (
                "Choose a clear, level area before this check"
            )
            bot("tacz reload")
            started = time.monotonic()
            if args.frames:
                prefix = f"benelli-reload-{time.time_ns()}"
                for index, delay in enumerate((0.15, 0.45, 0.85, 1.25, 1.65, 2.05)):
                    time.sleep(max(0, started + delay - time.monotonic()))
                    bot(f"screenshot {prefix}-{index}.png")
                    sample = bot("state")
                    assert sample["sprinting"], ("Sprint stopped during reload", sample)
                    frames.append(
                        {
                            "file": f"{prefix}-{index}.png",
                            "elapsed": round(time.monotonic() - started, 2),
                        }
                    )
            else:
                time.sleep(2.5)
        finally:
            bot("hold key.forward 0")
            bot("hold key.sprint 0")
        deadline, after = time.monotonic() + 6, before
        while time.monotonic() < deadline:
            after = count(
                rcon.cmd(f"data get entity {player} {gun_path}.GunCurrentAmmoCount")
            )
            if after == 8:
                break
            time.sleep(0.2)
        assert after > before, (before, after)
        print(
            json.dumps(
                {
                    "functional_reload": [before, after],
                    "frames": frames,
                    "visual_review": "pending" if frames else "skipped (no GUI client)",
                },
                indent=2,
            )
        )
    finally:
        bot.close()
    return 0


def register(sub):
    parser = sub.add_parser("check-benelli", help=SCENARIO["help"])
    parser.add_argument(
        "--frames", action="store_true", help="capture reload frames (GUI client only)"
    )
    rcon_args(parser)
    parser.set_defaults(func=run)
