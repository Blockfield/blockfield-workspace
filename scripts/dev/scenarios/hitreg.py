#!/usr/bin/env python3
"""Deterministic, server-confirmed hit registration in an owned stand.

    just scenario hitreg setup <dimension> [--room 1]
    just scenario hitreg run --motion still --players 10
    just scenario hitreg run --motion sprint --mode track --players 10

Repeat with 20/30 players and direct/40 ms one-way delay_proxy connections.
Use --fps 20/30/60/144 to measure frame time independently of the cap; --resync
adds a deliberate clock desync and rejected probe before the measured shots.
Moving targets strafe across the line of fire (walk, sprint or sprint-jump);
"flick" aims each shot at the target where the last frame drew it, "track"
keeps the crosshair there every frame, as a player does. Each shot records
whether the client attached a render-time ticket (the server rewinds only
with one) and the case lists the server's rewind labels for this shooter
(those need `just server start --pose-debug`).
The setup equips the existing stand-only zero-spread/recoil AK, so each shot is
an aim-and-fire testbot call, independent of frame rate and mouse callbacks.
A server ammo decrement confirms acceptance; per-shot health samples judge hits.
The JSON records every shot, actual online count, ping and tick-query metrics.
"""

import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path

from .common import Bot, Rcon, connect_rcon, number, rcon_args, wait_while
from .stress import online, tick_query

SCENARIO = {
    "name": "hitreg",
    "help": "deterministic zero-spread shots with server acceptance and health receipts",
}

BOT_NAME, Y = "hitbot", 150
MOTIONS = {
    "still": (),
    "walk": ("move forward",),
    "sprint": ("move forward", "sprint"),
    "jump": ("move forward", "sprint", "jump continuous"),
}
SHOOTER = (0.5, Y + 1, -19.5)
GUN = "tacz:ak47_zero_spread"


def health(rcon, name=BOT_NAME):
    return number(rcon.cmd(f"data get entity {name} Health"))


def heal(rcon):
    rcon.cmd(f"effect give {BOT_NAME} minecraft:instant_health 1 10 true")
    time.sleep(0.1)
    # Instant-health is ticked repeatedly for its duration: a lingering effect hides hits.
    rcon.cmd(f"effect clear {BOT_NAME} minecraft:instant_health")


def setup(rcon, dim, room, client):
    if BOT_NAME not in online(rcon):
        rcon.cmd(f"player {BOT_NAME} spawn")
        if not wait_while(lambda: BOT_NAME in online(rcon), 30, 0.25):
            raise TimeoutError("hitbot did not connect")
    # Deploy on every run, not just the first: an online-but-undeployed bot
    # (creative, outside the match) takes no damage and voids the whole case.
    for command in (
        f"bf action {BOT_NAME} join:{room}",
        f"bf action {BOT_NAME} class:assault:0",
    ):
        rcon.cmd(command)
    for _ in range(20):
        rcon.cmd(f"bf action {BOT_NAME} spawn:any")
        time.sleep(1.5)
        if dim in (rcon.cmd(f"data get entity {BOT_NAME} Dimension") or ""):
            break
    else:
        raise TimeoutError("hitbot did not join the requested combat dimension")
    rcon.cmd(f"player {BOT_NAME} stop")
    rcon.cmd(f"execute in {dim} run fill -12 {Y} -24 12 {Y} 6 minecraft:barrier")
    rcon.cmd(f"execute in {dim} run fill -12 {Y + 1} -24 12 {Y + 4} 6 minecraft:air")
    for x1, z1, x2, z2 in ((-11, 2, 11, 2), (-11, -3, -11, 2), (11, -3, 11, 2)):
        rcon.cmd(
            f"execute in {dim} run fill {x1} {Y + 1} {z1} {x2} {Y + 3} {z2} minecraft:barrier"
        )
    rcon.cmd(
        f"execute in {dim} run tp {client} {SHOOTER[0]} {SHOOTER[1]} {SHOOTER[2]} 0 0"
    )
    rcon.cmd(f"execute in {dim} run tp {BOT_NAME} 0.5 {Y + 1} 0.5 90 0")
    for slot in ("armor.head", "armor.chest", "armor.legs", "armor.feet"):
        rcon.cmd(f"item replace entity {BOT_NAME} {slot} with minecraft:air")
    rcon.cmd(f"attribute {BOT_NAME} minecraft:generic.max_health base set 1000")
    rcon.cmd(f"effect give {BOT_NAME} minecraft:resistance infinite 0 true")
    rcon.cmd(
        f"item replace entity {client} hotbar.0 with tacz:modern_kinetic_gun"
        f'[minecraft:custom_data={{GunId:"{GUN}",GunCurrentAmmoCount:30000,'
        'HasBulletInBarrel:1b,GunFireMode:"SEMI"}]'
    )
    print(f"platform and zero-spread gun in {dim}; select hotbar slot 0")


def server_shot(rcon, client, path, before, timeout=1):
    after = before
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        after = number(rcon.cmd(f"data get entity {client} {path}"))
        if after != before:
            break
        time.sleep(0.05)
    if after == before:
        raise TimeoutError(f"server shot unconfirmed: {before} -> {after}")
    assert after == before - 1, f"uncontrolled fire: {before} -> {after}"
    return after


def frame_times(samples):
    timestamps = [
        int(row.split()[0])
        for row in samples
        if len(row.split()) >= 4 and row.split()[3] == "cam"
    ]
    intervals = sorted((b - a) / 1000 for a, b in zip(timestamps, timestamps[1:]))
    if not intervals:
        return {"frames": len(timestamps)}
    return {
        "frames": len(timestamps),
        "mean_fps": round(1000 * len(intervals) / sum(intervals), 2),
        **{
            key: round(
                intervals[min(len(intervals) - 1, math.ceil(q * len(intervals)) - 1)], 3
            )
            for key, q in (
                ("p50_ms", 0.5),
                ("p95_ms", 0.95),
                ("p99_ms", 0.99),
                ("max_ms", 1),
            )
        },
    }


def uuid(rcon, name):
    """Hyphenated UUID from the int-array NBT form."""
    ints = re.search(
        r"\[I;\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)\]",
        rcon.cmd(f"data get entity {name} UUID"),
    )
    if ints is None:
        return None
    text = "".join(f"{int(i) & 0xFFFFFFFF:08x}" for i in ints.groups())
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def rewind_labels(rcon, shooter):
    """Server resolution labels of this shooter's victims (view:exact = rewound)."""
    found = re.search(r"path=(\S+)", rcon.cmd("bf bodypose receipts") or "")
    if shooter is None or found is None or not Path(found.group(1)).is_file():
        return None
    rows = [json.loads(line) for line in Path(found.group(1)).read_text().splitlines()]
    return dict(Counter(row["label"] for row in rows if row.get("shooter") == shooter))


def run_shots(
    rcon,
    bot,
    shots,
    motion,
    mode,
    players=None,
    new_rcon=connect_rcon,
    fps=None,
    resync=False,
):
    assert shots > 0, "shots must be positive"
    assert motion in MOTIONS, f"motion must be one of {sorted(MOTIONS)}"
    if fps is not None:
        assert 10 <= fps <= 260, "FPS cap must be in Minecraft's 10..260 range"
        bot(f"opt fps {fps}")
    state = bot("state")
    initial_fps = state.get("fps")
    client = state["name"]
    path = f'Inventory[{{Slot:{state["slot"]}b}}].components."minecraft:custom_data"'
    assert GUN in rcon.cmd(f"data get entity {client} {path}.GunId"), (
        "Run hitreg setup and select its zero-spread gun first"
    )
    path += ".GunCurrentAmmoCount"
    stop, turner, rows = threading.Event(), None, []
    turn_errors = []
    rcon.cmd(f"player {BOT_NAME} stop")
    bot("closescreen")
    bot("tacz aim true")
    bot(f"track {BOT_NAME}" if mode == "track" else "track off")
    time.sleep(1)

    def turn():
        extra = None
        try:
            extra = new_rcon()
            while not stop.wait(1.4):
                extra.cmd(f"player {BOT_NAME} turn back")
        except Exception as error:
            turn_errors.append(error)
        finally:
            if extra is not None:
                extra.s.close()

    try:
        for action in MOTIONS[motion]:
            rcon.cmd(f"player {BOT_NAME} {action}")
        if MOTIONS[motion]:
            turner = threading.Thread(target=turn, daemon=True)
            turner.start()
        recovery = None
        if resync:
            ammo = number(rcon.cmd(f"data get entity {client} {path}"))
            seed = bot(f"aimshoot {BOT_NAME}")
            assert seed.get("result") == "SUCCESS", seed
            server_shot(rcon, client, path, ammo)
            bot("forge tacz_sync")
            time.sleep(0.2)
            probe = bot(f"aimshoot {BOT_NAME}")
            assert probe.get("result") == "SUCCESS", probe
            try:
                server_shot(rcon, client, path, ammo - 1)
                recovery = "probe_accepted_no_recovery_proven"
            except TimeoutError:
                recovery = "probe_unconfirmed_check_NETWORK_FAIL_log"
            time.sleep(1)
        bot("frames start")
        deadline = time.monotonic() + shots * 10 + 60
        while len(rows) < shots:
            if turn_errors:
                raise RuntimeError("hitreg strafe controller failed") from turn_errors[
                    0
                ]
            if time.monotonic() >= deadline:
                raise TimeoutError("hitreg shot budget exhausted")
            count = len(online(rcon))
            if players is not None and count != players:
                raise AssertionError(f"expected {players} players, found {count}")
            heal(rcon)
            before = health(rcon)
            assert before > 100, (
                "hitbot was not healed or max_health fixture is missing"
            )
            ammo = number(rcon.cmd(f"data get entity {client} {path}"))
            view = bot("combatview")
            result = bot(f"aimshoot {BOT_NAME}" if mode == "flick" else "tacz shoot")
            assert result.get("result") == "SUCCESS", result
            # The sequence advances only when the shot carried a render-time ticket.
            after_view = bot("combatview")
            confirmation = None
            try:
                accepted = server_shot(rcon, client, path, ammo)
            except TimeoutError as error:
                accepted = ammo
                confirmation = str(error)
            lowest = before
            until = time.monotonic() + 0.9
            while time.monotonic() < until:
                lowest = min(lowest, health(rcon))
                time.sleep(0.05)
            row = {
                "shot": len(rows) + 1,
                "ammo": [ammo, accepted],
                "health": [before, lowest],
                "confirmed": accepted == ammo - 1,
                "confirmation_error": confirmation,
                "hit": accepted == ammo - 1 and lowest < before,
                "aimAt": result.get("aimAt"),
                "ticket": after_view.get("sequence", 0) > view.get("sequence", 0),
                "ticket_unavailable": after_view.get("reportUnavailable"),
                "players": count,
            }
            rows.append(row)
            print(json.dumps({"sample": row}), flush=True)
    finally:
        stop.set()
        if turner is not None:
            turner.join(timeout=5)
            if turner.is_alive():
                raise TimeoutError("hitreg turner did not stop")
        rcon.cmd(f"player {BOT_NAME} stop")
        bot("track off")
        timing = frame_times(bot("frames get")["samples"])
    state = bot("state")
    hits = sum(row["hit"] for row in rows)
    report = {
        "mode": mode,
        "gun": GUN,
        "shots": len(rows),
        "confirmed": sum(row["confirmed"] for row in rows),
        "hits": hits,
        "rate": hits / len(rows),
        "tickets": sum(row["ticket"] for row in rows),
        # Since protocol 9 an incomplete report still sends an R-only ticket.
        "report_unavailable": dict(
            Counter(
                row["ticket_unavailable"] for row in rows if row["ticket_unavailable"]
            )
        ),
        "rewind_labels": rewind_labels(rcon, uuid(rcon, client)),
        "motion": motion,
        "players": len(online(rcon)),
        "client_ping_ms": state.get("ping"),
        "fps_cap": fps,
        "actual_fps_start": initial_fps,
        "actual_fps_end": state.get("fps"),
        "frame_time": timing,
        "recovery_probe": recovery,
        "server": state.get("server"),
        "tick_query": tick_query(rcon),
    }
    print(json.dumps(report), flush=True)
    return report


def run(args, rcon_factory=connect_rcon, bot_factory=Bot):
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        if args.action == "setup":
            client = args.client or bot("state")["name"]
            setup(rcon, args.dimension, args.room, client)
        else:
            run_shots(
                rcon,
                bot,
                args.shots,
                "still" if args.still else args.motion,
                args.mode,
                args.players,
                lambda: Rcon(
                    args.rcon_host, args.rcon_port, args.rcon_password, timeout=2
                ),
                args.fps,
                args.resync,
            )
    finally:
        bot.close()
        rcon.s.close()
    return 0


def register(sub):
    parser = sub.add_parser("hitreg", help=SCENARIO["help"])
    parser.add_argument("action", choices=("setup", "run"))
    parser.add_argument("dimension", nargs="?")
    parser.add_argument("--room", default="1")
    parser.add_argument(
        "--client", default=None, help="shooter (default: connected client)"
    )
    parser.add_argument("--shots", type=int, default=50)
    parser.add_argument("--still", action="store_true", help="same as --motion still")
    parser.add_argument("--motion", choices=tuple(MOTIONS), default="sprint")
    parser.add_argument(
        "--resync",
        action="store_true",
        help="measure recovery after a deliberate clock desync",
    )
    parser.add_argument(
        "--fps",
        type=int,
        choices=(20, 30, 60, 144),
        help="cap FPS and measure actual frame times",
    )
    parser.add_argument(
        "--players", type=int, help="fail if actual online count differs"
    )
    parser.add_argument("--mode", choices=("track", "flick"), default="flick")
    rcon_args(parser)
    parser.set_defaults(func=run)
