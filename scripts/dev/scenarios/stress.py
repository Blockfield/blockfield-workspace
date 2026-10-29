#!/usr/bin/env python3
"""Server stress with Carpet fake players against the devstack server.

    just scenario stress hub 10,25,50
    just scenario stress combat [--campaign 32] [--dm 18] [--guns] [--window 120]

Cross-platform load + tick metrics from RCON only (no /proc, no agent-process):
spawns Carpet bots, walks them through hub/combat rooms, reports mspt percentiles
from `tick query`. `just setup` installs pinned Carpet only in the test server;
JFR remains a manual step, printed on demand.
"""

import json
import random
import re
import threading
import time

from .common import connect_rcon, rcon_args, wait_while

SCENARIO = {"name": "stress", "help": "Carpet-bot load with tick-query metrics"}
BOT_TAG = "bfbot"


def online(rcon):
    return {
        n.strip().lower()
        for n in (rcon.cmd("list") or ":").split(":", 1)[1].split(",")
        if n.strip()
    }


def spawn(rcon, names):
    for name in names:
        rcon.cmd(f"player {name} spawn")
    required = {name.lower() for name in names}
    # Carpet profile lookup can outlast a fixed pause; an untagged online bot never fires.
    if not wait_while(lambda: required <= online(rcon), 30, 0.25):
        raise TimeoutError(f"Bots did not connect: {sorted(required - online(rcon))}")
    for name in names:
        rcon.cmd(f"tag {name} add {BOT_TAG}")
        tags = rcon.cmd(f"data get entity {name} Tags") or ""
        if not re.search(r'(?:\[|,)\s*(?:"bfbot"|bfbot)\s*(?=,|\])', tags):
            raise RuntimeError(f"Bot did not receive the stress tag: {name}")


def gunner_stats(rcon, expected):
    stats = (rcon.cmd("bf bot stats") or "").strip()
    match = re.search(r"\bfiring=(\d+)\b", stats)
    if not match or int(match.group(1)) != expected:
        raise RuntimeError(f"Expected {expected} armed bots: {stats}")
    return stats


def walk(rcon, name, sprint):
    for action in ("move forward", "jump interval 40", "attack interval 8") + (
        ("sprint",) if sprint else ()
    ):
        rcon.cmd(f"player {name} {action}")


def tick_query(rcon):
    """Vanilla 1.21.1 average/P50/P95/P99; exclude the target tick duration."""
    out = rcon.cmd("tick query") or ""
    found = [
        re.search(label + r".*?(\d+(?:[.,]\d+)?)ms", out)
        for label in ("Average time per tick:", "P50:", "P95:", "P99:")
    ]
    return [float(m.group(1).replace(",", ".")) for m in found] if all(found) else []


def sample_window(rcon, get_log, seconds, interval=5):
    samples, start, log_mark = [], time.time(), len(get_log().splitlines())
    while time.time() - start < seconds:
        if query := tick_query(rcon):
            samples.append(query)
        time.sleep(interval)
    return samples, get_log().splitlines()[log_mark:]


def report(samples, new_lines, **extra):
    base = [[-1] * 4] if not samples else samples
    row = dict(
        **extra,
        mspt_avg=round(sum(x[0] for x in base) / len(base), 1),
        p50=max(x[1] for x in base),
        p95=max(x[2] for x in base),
        p99=max(x[3] for x in base),
        kills=sum(" убит " in line for line in new_lines),
        captures=sum("захвачена командой" in line for line in new_lines),
        rejected_shots=sum("shot rejected" in line for line in new_lines),
        windows=samples,
    )
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return row


def hub(rcon, get_log, stages, settle, new_rcon):
    bots, stop = [], threading.Event()

    def turn():
        extra = new_rcon()
        while not stop.wait(3):
            for name in list(bots):
                extra.cmd(
                    f"player {name} turn {random.choice(['left', 'right', 'back'])}"
                )

    worker = threading.Thread(target=turn, daemon=True)
    worker.start()
    try:
        for count in stages:
            new = [f"bot{i:02d}" for i in range(len(bots) + 1, count + 1)]
            spawn(rcon, new)
            for name in new:
                walk(rcon, name, sprint=False)
            bots += new
            time.sleep(settle)
            samples, new_lines = sample_window(rcon, get_log, 20)
            report(samples, new_lines, scenario="hub", players=len(online(rcon)))
    finally:
        stop.set()
        worker.join(timeout=5)


def open_room(rcon, get_log, first, mode, map_id):
    before = len(get_log().splitlines())
    rcon.cmd(f"execute as {first} run bf play {mode} {map_id}".strip())
    for _ in range(40):
        for line in get_log().splitlines()[before:]:
            if match := re.search(r"Комната (\S+) открыта", line):
                return match.group(1)
        time.sleep(0.5)
    raise SystemExit(f"room {mode} {map_id} did not open")


def join(rcon, names, room):
    for name in names:
        rcon.cmd(f"bf action {name} join:{room}")
        rcon.cmd(f"bf action {name} class:assault:0")
        rcon.cmd(f"execute as {name} run ready")


def dimension(rcon, name):
    out = rcon.cmd(f"data get entity {name} Dimension") or ""
    match = re.search(r'"([^"]+)"', out)
    return match.group(1) if match else ""


def points_of(dim, maps_dir):
    """Capture points of the campaign map behind a room dimension like blockfield:berdichev_1."""
    from pathlib import Path

    map_id = re.sub(r"_\d+$", "", dim.split(":", 1)[-1])
    data = json.loads((Path(maps_dir) / f"{map_id}.json").read_text(encoding="utf-8"))
    return [(p["x"], p["y"], p["z"]) for p in data["mode_data"]["campaign"]["points"]]


def combat(rcon, get_log, args, maps_dir, new_rcon=None):
    new_rcon = new_rcon or connect_rcon
    camp = [f"bot{i:02d}" for i in range(1, args.campaign + 1)]
    dm = [f"bot{i:02d}" for i in range(args.campaign + 1, args.campaign + args.dm + 1)]
    have = online(rcon)
    spawn(rcon, [b for b in camp + dm if b not in have])
    rooms = {}
    if camp:
        rooms["camp"] = open_room(rcon, get_log, camp[0], "room", args.campaign_map)
        join(rcon, camp[1:], rooms["camp"])
        rcon.cmd(f"bf action {camp[0]} class:assault:0")
        rcon.cmd(f"execute as {camp[0]} run ready")
    if dm:
        rooms["dm"] = open_room(rcon, get_log, dm[0], "deathmatch", args.dm_map)
        join(rcon, dm[1:], rooms["dm"])
        rcon.cmd(f"bf action {dm[0]} class:assault:0")
    room_of = {
        **{b: rooms.get("camp") for b in camp},
        **{b: rooms.get("dm") for b in dm},
    }
    deadline = time.time() + 90
    camp_dim = ""
    while camp and time.time() < deadline and not camp_dim.startswith("blockfield:"):
        rcon.cmd("bf action @a spawn:BASE")
        camp_dim = dimension(rcon, camp[0])
        time.sleep(3)
    points = points_of(camp_dim, maps_dir) if camp_dim.startswith("blockfield:") else []
    print(
        f"rooms {rooms}, campaign in {camp_dim or '-'}, {len(points)} points",
        flush=True,
    )
    for name in camp + dm:
        walk(rcon, name, sprint=not args.guns)
    stop, rejoined = threading.Event(), [0]

    def loop():
        extra = new_rcon()
        n = 0
        while not stop.wait(1.5):
            n += 1
            extra.cmd(
                f"execute as @a[tag={BOT_TAG},team=RedTeam] at @s run tp @s ~ ~ ~ "
                f"facing entity @p[team=BlueTeam] feet"
            )
            extra.cmd(
                f"execute as @a[tag={BOT_TAG},team=BlueTeam] at @s run tp @s ~ ~ ~ "
                f"facing entity @p[team=RedTeam] feet"
            )
            if n % 2 == 0:
                extra.cmd("bf action @a spawn:BASE")
                extra.cmd("bf action @a spawn:any")
            if points and n % 7 == 0:
                x, y, z = random.choice(points)
                for name in random.sample(camp, max(1, len(camp) // 4)):
                    extra.cmd(
                        f"execute in {camp_dim} if entity {name} run tp {name} "
                        f"{x + random.uniform(-3, 3):.1f} {y + 1} {z + random.uniform(-3, 3):.1f}"
                    )
            if args.guns and n % 10 == 1:
                extra.cmd(f"bf bot fire @a[tag={BOT_TAG}] 20")
            if n % 10 == 0:
                missing = [b for b in camp + dm if b not in online(extra)]
                if missing:
                    spawn(extra, missing)
                    for name in missing:
                        join(extra, [name], room_of[name])
                        walk(extra, name, sprint=not args.guns)
                    rejoined[0] += len(missing)

    worker = threading.Thread(target=loop, daemon=True)
    worker.start()
    try:
        time.sleep(args.warmup)
        rejoined[0] = 0
        if args.guns:
            gunner_stats(rcon, len(camp + dm))  # reset counters to the window
        samples, new_lines = sample_window(rcon, get_log, args.window)
        gunner = gunner_stats(rcon, len(camp + dm)) if args.guns else ""
        if not {name.lower() for name in camp + dm} <= online(rcon):
            raise RuntimeError("Requested combat bots disconnected during the window")
    finally:
        stop.set()
        worker.join(timeout=5)
    report(
        samples,
        new_lines,
        scenario="combat",
        guns=args.guns,
        campaign=len(camp),
        deathmatch=len(dm),
        online=len(online(rcon)),
        rejoined=rejoined[0],
        gunner=gunner,
    )


def run(args, rcon_factory_override=None, get_log=None):
    from devstack import LOG as dev_log, SERVER

    logs = get_log or (lambda: dev_log.read_text(encoding="utf-8", errors="replace"))
    host, port, password = args.rcon_host, args.rcon_port, args.rcon_password
    new_rcon = rcon_factory_override or (lambda: connect_rcon(host, port, password))
    rcon = new_rcon()
    if not any((SERVER / "mods").glob("fabric-carpet-*.jar")):
        raise SystemExit(
            f"no Carpet in {SERVER / 'mods'}: stop your own test stack and run `just setup`"
        )
    if args.action == "hub":
        hub(rcon, logs, [int(x) for x in args.stages.split(",")], args.settle, new_rcon)
    else:
        combat(rcon, logs, args, SERVER / "blockfield" / "maps", new_rcon)
    return 0


def register(sub):
    parser = sub.add_parser("stress", help=SCENARIO["help"])
    parser.add_argument("action", choices=("hub", "combat"))
    parser.add_argument("stages", nargs="?", default="10,25,50")
    parser.add_argument("--settle", type=int, default=45)
    parser.add_argument("--campaign", type=int, default=32)
    parser.add_argument("--dm", type=int, default=18)
    parser.add_argument("--campaign-map", default="")
    parser.add_argument("--dm-map", default="dust2")
    parser.add_argument("--guns", action="store_true")
    parser.add_argument("--window", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=40)
    rcon_args(parser)
    parser.set_defaults(func=run)
