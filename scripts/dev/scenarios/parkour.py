#!/usr/bin/env python3
"""Live ParCool check: play attempts.json moves through the real client and judge the landing.

    just scenario parkour attempts.json [--map id] [--limit N] [--out file]

Pure functions (judge, on_roof, start_reachable, load_nodes) are the rig originals;
the orchestration below talks to the devstack RCON + testbot instead of ~/.cache paths.
"""

import json
import random
import re
import time

from .common import Bot, connect_rcon, rcon_args, wait_for

SCENARIO = {
    "name": "parkour",
    "help": "play ParCool attempts through the testbot and judge landings",
}


def judge(start, target, ground):
    """ground — blocks the player stood on ("x z y"). reached: the target cell at its height;
    near: a neighbouring cell (not the start) no lower than target - 0.6; else no."""
    x, _, z = start
    tx, ty, tz = target
    stood = [
        (int(gx), int(gz), float(gy)) for gx, gz, gy in (g.split() for g in ground)
    ]
    if any((gx, gz) == (tx, tz) and abs(gy - ty) < 0.02 for gx, gz, gy in stood):
        return "reached"
    if any(
        max(abs(gx - tx), abs(gz - tz)) <= 1 and (gx, gz) != (x, z) and gy >= ty - 0.6
        for gx, gz, gy in stood
    ):
        return "near"
    return "no"


def load_nodes(path, key):
    """reach/roof from --attempts: {(x, z): [floors]}."""
    with open(path, encoding="utf-8") as handle:
        index = {}
        for x, z, top in json.load(handle).get(key, ()):
            index.setdefault((x, z), []).append(top)
    return index


def on_roof(ground, roof):
    """Player stood in a column that was roof on the open map, at its height or above."""
    hits = []
    for block in ground:
        gx, gz, gy = block.split()
        if any(float(gy) >= top - 0.1 for top in roof.get((int(gx), int(gz)), ())):
            hits.append(block)
    return hits


def start_reachable(start, reach):
    """Start is reachable from spawns on the closed map (reach): else a roof hit is not a leak."""
    x, y, z = start
    return any(abs(top - y) < 0.1 for top in reach.get((x, z), ()))


def settle(bot, x, y, z, timeout=3.0):
    matched, _, _ = wait_for(
        lambda remaining: bot("state", timeout=remaining),
        lambda s: (
            abs(s["pos"][0] - x) < 0.05
            and abs(s["pos"][1] - y) < 0.1
            and abs(s["pos"][2] - z) < 0.05
            and s.get("onGround")
        ),
        timeout,
    )
    return matched


def open_room(bot, rcon, get_log, map_id):
    """Deathmatch room with the map: close the open one, enter via the menu, wait for measure."""
    rooms = re.findall(r"Комната (\d+) открыта", get_log())
    if rooms:
        rcon.cmd(f"bf stop room {rooms[-1]}")
        time.sleep(2)
    mark = len(get_log().splitlines())
    bot("closescreen")
    bot(f"bfaction play:deathmatch:{map_id}")
    time.sleep(3)
    bot("bfaction class:assault:0")
    for _ in range(60):
        found = re.findall(
            rf"измерение ({re.escape(map_id)}_\d+) открыто",
            "\n".join(get_log().splitlines()[mark:]),
        )
        if found:
            return found[-1]
        time.sleep(1)
    raise SystemExit(f"room with {map_id} did not open")


def run_attempt(bot, rcon, dim, player, attempt):
    x, y, z = attempt["start"]
    rcon.cmd(
        f"execute in blockfield:{dim} run tp {player} {x + 0.5} {y} {z + 0.5} {attempt['yaw']} 0"
    )
    if not settle(bot, x + 0.5, y, z + 0.5):
        return {"result": "not-placed", "pos": bot("state")["pos"]}
    bot(f"attempt {attempt['kind']} {attempt['yaw']}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        time.sleep(0.25)
        reply = bot("attempt get")
        if reply["done"]:
            break
    else:
        raise TimeoutError("parkour attempt did not finish within 15 seconds")
    return {
        "result": judge(attempt["start"], attempt["target"], reply["ground"]),
        "cling": reply["clingAt"] >= 0,
        "climb": reply["climbAt"] >= 0,
        "crawl": reply["crawlTicks"] > 0,
        "maxY": reply["maxY"],
        "ground": reply["ground"],
    }


def recount(saved, attempts_path, reach_path=None):
    with open(saved, encoding="utf-8") as handle:
        done = json.load(handle)
    roof = load_nodes(attempts_path, "roof")
    reach = load_nodes(reach_path, "reach") if reach_path else None
    roofs = cut = 0
    for row in done["results"]:
        attempt, hits = row["attempt"], on_roof(row.get("ground", []), roof)
        if reach is not None and not start_reachable(attempt["start"], reach):
            cut += 1
            continue
        if hits:
            roofs += 1
            print(
                f"{attempt['kind']:5} start {attempt['start']} -> {attempt['target']}: ON ROOF {hits}"
            )
    print(
        f"{done['map']}: {len(done['results'])} attempts, start cut by barriers {cut}, on roof {roofs}"
    )


def run(args, get_log=None, bot_factory=Bot, rcon_factory=connect_rcon):
    if args.judge:
        recount(args.judge, args.attempts, args.reach)
        return 0
    with open(args.attempts, encoding="utf-8") as handle:
        data = json.load(handle)
    if args.previous:
        with open(args.previous, encoding="utf-8") as handle:
            keep = (
                {
                    json.dumps(r["attempt"], sort_keys=True)
                    for r in json.load(handle)["results"]
                    if r["result"] == args.only
                }
                if args.only
                else None
            )
    else:
        keep = None
    map_id = args.map or data["map"]
    kinds = set(args.kinds.split(","))
    todo = [a for a in data["attempts"] if a["kind"] in kinds]
    if keep is not None:
        todo = [a for a in todo if json.dumps(a, sort_keys=True) in keep]
    if args.limit and len(todo) > args.limit:
        todo = random.Random(args.seed).sample(todo, args.limit)
    rcon = rcon_factory(args.rcon_host, args.rcon_port, args.rcon_password)
    bot = bot_factory(args.bot_port)
    try:
        from devstack import LOG as dev_log

        logs = get_log or (
            lambda: dev_log.read_text(encoding="utf-8", errors="replace")
        )
    except ImportError:
        if get_log is None:
            raise SystemExit(
                "parkour needs the devstack server log; run as `just scenario parkour`"
            )
        logs = get_log
    player = bot("state")["name"]
    dim = open_room(bot, rcon, logs, map_id)
    rcon.cmd(f"gamemode adventure {player}")
    for effect in ("resistance", "saturation", "regeneration"):
        rcon.cmd(f"effect give {player} minecraft:{effect} infinite 255 true")
    results, counts = [], {}
    reach = load_nodes(args.reach, "reach") if args.reach else None
    roof = load_nodes(args.attempts, "roof")
    started = time.time()
    for i, attempt in enumerate(todo, 1):
        row = run_attempt(bot, rcon, dim, player, attempt)
        row["attempt"] = attempt
        if reach is not None and not start_reachable(attempt["start"], reach):
            row["start_cut"] = True
        elif hits := on_roof(row.get("ground", []), roof):
            row["roof"] = hits
            counts[("roof", "")] = counts.get(("roof", ""), 0) + 1
        results.append(row)
        key = (attempt["kind"], row["result"])
        counts[key] = counts.get(key, 0) + 1
        print(
            f"{i}/{len(todo)} {attempt['kind']:5} start {attempt['start']} -> {attempt['target']}: "
            f"{row['result']}",
            flush=True,
        )
    summary = {f"{k}:{v}": n for (k, v), n in sorted(counts.items())}
    print(
        f"{map_id} ({dim}), {len(todo)} attempts in {time.time() - started:.0f}s: {summary}"
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(
                {"map": map_id, "dim": dim, "summary": summary, "results": results},
                handle,
                indent=1,
            )
    return 0


def register(sub):
    parser = sub.add_parser("parkour", help=SCENARIO["help"])
    parser.add_argument("attempts")
    parser.add_argument("--map")
    parser.add_argument("--kinds", default="grab,gap,crawl")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--only", choices=["reached", "near"])
    parser.add_argument("--from", dest="previous")
    parser.add_argument("--out")
    parser.add_argument("--reach")
    parser.add_argument("--judge", help="recount saved results instead of playing")
    rcon_args(parser)
    parser.set_defaults(func=run)
