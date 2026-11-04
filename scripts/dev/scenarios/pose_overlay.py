#!/usr/bin/env python3
"""Server position next to the drawn model, with a target really running on its own client.

    just server start --pose-debug
    just scenario pose-overlay setup --map dust2
    just scenario pose-overlay run --fps 60 --distance 25 --gait sprint --out .dev/po/cell
    just scenario pose-overlay connect --port 25600   # both clients through delay_proxy
    just scenario pose-overlay shots --leads 0,1 --distance 60 --out .dev/po/shots

Client 1 (47777) watches, client 2 (47778) runs across its line of sight through real key
mappings. `bf bodypose overlay` feeds both clients' PoseDebugOverlay: its per-frame log is the
evidence and its boxes are in the screenshots. Offsets are metres along the run direction,
positive ahead of the drawn model: `offImpact` is the lead a shot fired that frame would need.
`shots` is the short confirmation only, never the primary measure.
"""

import json
import math
import re
import threading
import time
from collections import Counter
from pathlib import Path

from .common import Bot, connect_rcon, dimension, number, rcon_args
from .hitreg import GUN, server_shot

SCENARIO = {
    "name": "pose-overlay",
    "help": "per-frame server-vs-drawn offsets of a really running client",
}

Y, LANE = 150, 20
# Settle/verify budgets: a spawn:any sent while the respawn timer still runs lands
# only after its countdown, so blind sleeps cannot prove the lane is safe.
SETTLE_TIMEOUT_S, SETTLE_STABLE_M, PLACE_VERIFY_S = 30.0, 0.3, 2.0
GAITS = {
    "walk": ("key.forward",),
    "sprint": ("key.forward", "key.sprint"),
    "fastrun": ("key.forward", "key.sprint", "key.parcool.FastRun"),
}
HOLD_TICKS = 72000
# A reversal is not a steady run: drop frames this close to a change of direction.
TURN_GUARD_NS = 400_000_000


def quantiles(values):
    if not values:
        return None
    ordered = sorted(values)
    at = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]
    return {
        "n": len(ordered),
        "p5": round(at(0.05), 3),
        "median": round(at(0.5), 3),
        "p95": round(at(0.95), 3),
    }


def steady_directions(times, xs):
    """Run direction per frame (+1/-1 along X), None within TURN_GUARD_NS of a reversal."""
    n = len(times)
    moves, previous = [0] * n, None
    for i in range(1, n):
        delta = xs[i] - xs[i - 1]
        # Sub-millimetre steps (frames between samples) keep the running direction.
        moves[i] = (1 if delta > 0 else -1) if abs(delta) > 1e-3 else (previous or 0)
        previous = moves[i] or previous
    turns = [times[i] for i in range(2, n) if moves[i] and moves[i] == -moves[i - 1]]
    out = []
    for i in range(n):
        near = any(abs(times[i] - turn) < TURN_GUARD_NS for turn in turns)
        out.append(None if near or not moves[i] else moves[i])
    return out


def fps_of(frames):
    gaps = sorted(f["dtMs"] for f in frames if f.get("dtMs"))
    if not gaps:
        return {}
    return {
        "fps": round(1000 / gaps[len(gaps) // 2], 1),
        "p95_ms": round(gaps[int(0.95 * len(gaps))], 1),
        "max_ms": round(gaps[-1], 1),
    }


def summarize_target(frames, target_id=None):
    """Along-run offsets of one remote player from the observer's frame log."""
    rows = [
        (f, t)
        for f in frames
        for t in f.get("targets", ())
        if target_id is None or t["id"] == target_id
    ]
    times = [f["ns"] for f, _ in rows]
    xs = [t["drawn"][0] for _, t in rows]
    directions = steady_directions(times, xs)
    live, start, impact, lag, speed = [], [], [], [], []
    labels, flights = Counter(), Counter()
    for i, (frame, target) in enumerate(rows):
        direction = directions[i]
        if direction is None:
            continue
        if "live" in target:
            live.append((target["live"][0] - target["drawn"][0]) * direction)
        if "offStart" in target:
            start.append(target["offStart"][0] * direction)
            impact.append(target["offImpact"][0] * direction)
            labels[target["labelImpact"]] += 1
            flights[target["flight"]] += 1
        if "r" in frame:
            lag.append(frame["h"] - frame["r"])
        if i and times[i] > times[i - 1]:
            speed.append(abs(xs[i] - xs[i - 1]) / ((times[i] - times[i - 1]) / 1e9))
    return {
        "frames": len(frames),
        **fps_of(frames),
        "speed_mps": quantiles(speed),
        "h_minus_r_ticks": quantiles(lag),
        "servo_rate": quantiles([f["rate"] for f in frames if "rate" in f]),
        "live_ahead_of_drawn_m": quantiles(live),
        "rewind_start_ahead_of_drawn_m": quantiles(start),
        "rewind_impact_ahead_of_drawn_m": quantiles(impact),
        "flight_ticks": dict(flights),
        "labels": dict(labels),
        "unavailable": sum(1 for f in frames if f.get("unavailable")),
    }


def summarize_self(frames, position_packets=0):
    """The runner's own frame log: predicted feet, camera and server position along the run."""
    rows = [f for f in frames if "live" in f["self"]]
    times = [f["ns"] for f in rows]
    directions = steady_directions(times, [f["self"]["pred"][0] for f in rows])
    behind, camera, pred_back, cam_back, camera_jump = [], [], [], [], []
    previous = None
    for frame, direction in zip(rows, directions):
        own = frame["self"]
        if direction is not None:
            behind.append((own["pred"][0] - own["live"][0]) * direction)
            camera.append((own["cam"][0] - own["pred"][0]) * direction)
            if previous is not None and previous[2] == direction:
                pred_step = (own["pred"][0] - previous[0]) * direction
                cam_step = (own["cam"][0] - previous[1]) * direction
                if pred_step < -1e-3:
                    pred_back.append(-pred_step)
                if cam_step < -1e-3:
                    cam_back.append(-cam_step)
                camera_jump.append(abs(cam_step - pred_step))
        previous = (own["pred"][0], own["cam"][0], direction)
    teleports = [f["self"]["teleports"] for f in rows]
    return {
        "frames": len(frames),
        **fps_of(frames),
        "pred_ahead_of_server_live_m": quantiles(behind),
        "camera_ahead_of_pred_m": quantiles(camera),
        "camera_jump_per_frame_m": quantiles(camera_jump),
        "pred_backsteps": {
            "count": len(pred_back),
            "max_m": round(max(pred_back, default=0), 3),
        },
        "camera_backsteps": {
            "count": len(cam_back),
            "max_m": round(max(cam_back, default=0), 3),
        },
        "position_packets": position_packets,
        "server_corrections_or_teleports": (
            teleports[-1] - teleports[0] if teleports else 0
        ),
    }


def wilson(hits, n, z=1.96):
    if not n:
        return None
    p = hits / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [round(centre - margin, 3), round(centre + margin, 3)]


def loaded_mod(text):
    """The blockfield version Fabric Loader itself listed at startup, or None."""
    found = re.findall(r"^\s*-\s*blockfield\s+(\S+)\s*$", text, re.MULTILINE)
    return found[-1] if found else None


def require_mod(expected, logs):
    """Stop before measuring a stale jar: a file's name does not prove what it contains."""
    loaded = {
        str(path): loaded_mod(Path(path).read_text(errors="replace")) for path in logs
    }
    wrong = {path: found for path, found in loaded.items() if found != expected}
    if wrong:
        raise SystemExit(f"loaded mod differs from {expected}: {wrong}")
    return loaded


def deployed(state):
    dim = dimension(state)
    return dim.startswith("blockfield:") and "respawn" not in dim


def server_pos(rcon, name):
    """Authoritative server position of a player, None when unreadable."""
    match = re.search(
        r"\[(-?\d+(?:\.\d+)?)d?,\s*(-?\d+(?:\.\d+)?)d?,\s*(-?\d+(?:\.\d+)?)d?\]",
        rcon.cmd(f"data get entity {name} Pos") or "",
    )
    if match is None:
        return None
    return [float(match.group(1)), float(match.group(2)), float(match.group(3))]


def wait_settled(rcon, shooter, runner, names, timeout=SETTLE_TIMEOUT_S, interval=0.5):
    """Block until both players sit still server-side in the match dimension.

    The dimension check alone can pass while a spawn teleport is still in
    flight; quiescence (the same server pos twice) proves none is pending.
    Returns the settled {name: pos}. Raises TimeoutError past the deadline.
    """
    previous = {}
    deadline = time.monotonic() + timeout
    while True:
        states = shooter("state"), runner("state")
        if all(deployed(state) for state in states):
            current = {name: server_pos(rcon, name) for name in names}
            if all(
                current[name] is not None
                and previous.get(name) is not None
                and math.dist(previous[name], current[name]) < SETTLE_STABLE_M
                for name in names
            ):
                return current
            previous = current
        else:
            previous = {}
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    raise TimeoutError(f"players never settled: {names}")


def on_lane(pos, centre, z):
    """X inside the run span, Z on the lane, Y at the platform: spawn passes X alone."""
    return (
        pos is not None
        and abs(pos[0] - centre) <= LANE + 2
        and abs(pos[2] - z) <= 6
        and abs(pos[1] - (Y + 1)) <= 2
    )


def ensure_placed(place, get_positions, lanes, timeout=PLACE_VERIFY_S, interval=0.25):
    """Server-side check that place() landed both players; re-place ONCE if not.

    place: zero-arg callable redoing the teleports. get_positions: zero-arg
    callable returning {name: [x, y, z]} from the server. lanes: {name: (centre, z)}
    expected lane per player (the shooter watches from z_far, the runner runs at
    runner_z). True when both are on the lane, False after one re-place still off
    (the caller rejects loudly).
    """
    for attempt in (0, 1):
        deadline = time.monotonic() + timeout
        while True:
            if all(on_lane(get_positions().get(name), *lanes[name]) for name in lanes):
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(interval)
        if attempt == 0:
            place()
    return False


def check_mod(args):
    """require_mod result, or a SKIP note when --expect-mod is unset.

    Without the flag there is nothing to compare the loaded mod against
    (requiring None would abort every run before the room opens).
    """
    if getattr(args, "expect_mod", None):
        return require_mod(
            args.expect_mod, [args.server_log, *args.client_logs.split(",")]
        )
    return {"mod": "unchecked (no --expect-mod)"}


def setup(rcon, shooter, runner, args):
    print(json.dumps(check_mod(args)), flush=True)
    log = Path(args.server_log)
    names = shooter("state")["name"], runner("state")["name"]
    before = len(log.read_text(errors="replace").splitlines())
    rcon.cmd(f"execute as {names[1]} run bf play deathmatch {args.map}")
    room = None
    deadline = time.monotonic() + 30
    while room is None and time.monotonic() < deadline:
        lines = log.read_text(errors="replace").splitlines()[before:]
        found = re.findall(r"Комната (\S+) открыта", "\n".join(lines))
        room = found[-1] if found else None
        time.sleep(0.5)
    if room is None:
        raise TimeoutError("room did not open")
    Path(args.room_file).write_text(room)
    deploy(rcon, shooter, runner, room)


def deploy(rcon, shooter, runner, room):
    names = shooter("state")["name"], runner("state")["name"]
    for name in names:
        rcon.cmd(f"bf action {name} join:{room}")
        rcon.cmd(f"bf action {name} class:assault:0")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        states = shooter("state"), runner("state")
        if all(deployed(state) for state in states):
            break
        # Only the undeployed get another spawn:any: re-sending to an already
        # deployed player stacks deploys whose late teleports undo place().
        for name, state in zip(names, states):
            if not deployed(state):
                rcon.cmd(f"bf action {name} spawn:any")
        time.sleep(1.5)
    else:
        raise TimeoutError(f"not deployed: {[dimension(s) for s in states]}")
    dim = dimension(states[0])
    assert dim == dimension(states[1]), "clients in different worlds"
    # Quiescence, not a blind sleep: a pending spawn countdown would move them.
    wait_settled(rcon, shooter, runner, names)
    for bot in (shooter, runner):
        # Agent clients are only the silent isolated instances, never a desktop fallback.
        if not bot("state").get("backend", {}).get("silent"):
            raise SystemExit("client is not a registered silent isolated instance")
        bot("closescreen")
        bot("camera FIRST_PERSON")
        bot("opt render 8")
    shooter("hitboxes on")
    for name in names:
        # A jar without the overlay answers "unknown command" here, before any measurement.
        answer = rcon.cmd(f"bf bodypose overlay {name} on") or ""
        if f"overlay {name}=on" not in answer:
            raise SystemExit(f"this server has no pose overlay: {answer!r}")
        rcon.cmd(f"effect give {name} minecraft:saturation infinite 1 true")
        rcon.cmd(f"attribute {name} minecraft:generic.max_health base set 1000")
        rcon.cmd(f"effect give {name} minecraft:resistance infinite 0 true")
        for slot in ("head", "chest", "legs", "feet"):
            rcon.cmd(f"item replace entity {name} armor.{slot} with minecraft:air")
        rcon.cmd(
            f"item replace entity {name} hotbar.0 with tacz:modern_kinetic_gun"
            f'[minecraft:custom_data={{GunId:"{GUN}",GunCurrentAmmoCount:30000,'
            'HasBulletInBarrel:1b,GunFireMode:"SEMI"}]'
        )
    for bot in (shooter, runner):
        bot("slot 0")
    return dim, names


def lane_of(match_map, distance):
    """The lane's centre X, the runner's Z and the watcher's Z, inside the match bounds."""
    centre = (match_map["minX"] + match_map["maxX"]) // 2 + 0.5
    runner_z = match_map["maxZ"] - 9.5
    watcher_z = runner_z - distance
    if (
        centre - LANE - 6 < match_map["minX"]
        or centre + LANE + 6 > match_map["maxX"]
        or watcher_z - 3 < match_map["minZ"]
    ):
        raise SystemExit(f"{distance} m lane does not fit {match_map['id']}")
    return centre, runner_z, watcher_z


def place(rcon, shooter, runner, distance):
    state = shooter("state")
    dim = dimension(state)
    names = state["name"], runner("state")["name"]
    centre, runner_z, z_far = lane_of(state["matchMap"], distance)
    x1, x2 = int(centre) - LANE - 6, int(centre) + LANE + 6
    z1, z2 = int(z_far) - 3, int(runner_z) + 4
    rcon.cmd(f"execute in {dim} run forceload add {x1} {z1} {x2} {z2}")
    rcon.cmd(f"execute in {dim} run fill {x1} {Y} {z1} {x2} {Y} {z2} minecraft:barrier")
    rcon.cmd(
        f"execute in {dim} run fill {x1} {Y + 1} {z1} {x2} {Y + 4} {z2} minecraft:air"
    )
    rcon.cmd(f"execute in {dim} run tp {names[0]} {centre} {Y + 1} {z_far} 0 0")
    rcon.cmd(f"execute in {dim} run tp {names[1]} {centre} {Y + 1} {runner_z} -90 0")
    return names, centre, runner_z, z_far


class Shared:
    """One testbot connection for two threads: the testbot serves one client at a time."""

    def __init__(self, bot):
        self.bot = bot
        self.lock = threading.Lock()

    def __call__(self, command):
        with self.lock:
            return self.bot(command)

    def close(self):
        self.bot.close()


class Runner(threading.Thread):
    """Real input only: held key mappings, and a look flip at each lane end."""

    def __init__(self, bot, gait, centre):
        super().__init__(daemon=True)
        self.bot = bot
        self.gait = gait
        self.centre = centre
        self.halt = threading.Event()
        self.error = None
        self.turns = 0

    def run(self):
        try:
            yaw = -90
            self.bot("look -90 0")
            for key in GAITS[self.gait]:
                self.bot(f"hold {key} {HOLD_TICKS}")
            while not self.halt.wait(0.05):
                x = self.bot("state")["pos"][0] - self.centre
                wanted = 90 if x > LANE else -90 if x < -LANE else yaw
                if wanted != yaw:
                    yaw = wanted
                    self.turns += 1
                    self.bot(f"look {yaw} 0")
        except Exception as error:  # noqa: BLE001  (reported by the caller)
            self.error = error
        finally:
            for key in GAITS[self.gait]:
                self.bot(f"hold {key} 0")


def require_running_lane(runner, centre, z, label, settle_s=1.0):
    """Refuse a cell whose runner is not on the lane and moving when capture starts.

    A late spawn:any can park both clients at spawn: capturing then measures a
    stationary runner against a wall (before/rtt0-fps20). Fail loudly instead.
    """
    before = runner("state")["pos"]
    time.sleep(settle_s)
    pos = runner("state")["pos"]
    moved = math.dist(pos, before)
    if not on_lane(pos, centre, z) or moved < 0.5:
        raise SystemExit(
            f"{label}: runner not running the lane (pos={pos}, 1 s move={moved:.2f} m)"
        )


# Below a walk: the runner stopped (or never ran) mid-capture.
RUN_MIN_SPEED_MPS = 2.0


def lane_end_error(label, pos, centre, z, speed_median=None, teleports=0):
    """Why a finished cell is unusable, None when the runner ran the whole lane.

    The start-of-capture lane check cannot catch a teleport or stall minutes
    later; the end check fails the cell instead of measuring a parked runner.
    """
    if not on_lane(pos, centre, z):
        return f"{label}: runner off the lane at capture end (pos={pos})"
    if speed_median is not None and speed_median < RUN_MIN_SPEED_MPS:
        return f"{label}: runner stopped mid-cell (speed_mps median={speed_median})"
    if teleports:
        return (
            f"{label}: runner teleported mid-cell "
            f"(server_corrections_or_teleports={teleports})"
        )
    return None


def run_cell(rcon, shooter, runner, args, out):
    out.mkdir(parents=True, exist_ok=True)
    names = shooter("state")["name"], runner("state")["name"]
    # Setup ran in another process minutes ago: quiesce first, so a late spawn
    # never fires between place() and capture.
    wait_settled(rcon, shooter, runner, names)
    names, centre, runner_z, z_far = place(rcon, shooter, runner, args.distance)
    lanes = {names[0]: (centre, z_far), names[1]: (centre, runner_z)}
    if not ensure_placed(
        lambda: place(rcon, shooter, runner, args.distance),
        lambda: {name: server_pos(rcon, name) for name in names},
        lanes,
    ):
        raise SystemExit(f"{out.name}: place() never landed both players on the lane")
    time.sleep(2)
    if args.runner_fps or args.fps:
        runner(f"opt fps {args.runner_fps or args.fps}")
    if args.fps:
        shooter(f"opt fps {args.fps}")
    shooter(f"track {names[1]}")
    moving = Runner(runner, args.gait, centre)
    moving.start()
    try:
        time.sleep(args.warm)
        require_running_lane(runner, centre, runner_z, out.name)
        for bot, name in ((shooter, "warm-shooter"), (runner, "warm-runner")):
            bot(f"poseoverlay dump {out.resolve() / (name + '.ndjson')}")
        runner("positions")
        time.sleep(args.seconds / 2)
        shot_name = f"{out.name}-{args.fps or 'nocap'}"
        shooter(f"screenshot {shot_name}")
        time.sleep(args.seconds / 2)
    finally:
        moving.halt.set()
        moving.join(timeout=10)
        shooter("track off")
    if moving.error is not None:
        raise moving.error
    packets = runner("positions")
    logs = {}
    for bot, name in ((shooter, "shooter"), (runner, "runner")):
        path = out.resolve() / f"{name}.ndjson"
        assert bot(f"poseoverlay dump {path}")["rows"] >= 0, f"{name} log overflowed"
        logs[name] = [json.loads(line) for line in path.read_text().splitlines()]
        if not logs[name]:
            raise SystemExit(
                f"{name}: no overlay frames, is the client jar this build?"
            )
    report = {
        "label": out.name,
        "gait": args.gait,
        "fps_cap": args.fps,
        "runner_fps_cap": args.runner_fps or args.fps,
        "distance": args.distance,
        "shooter_ping_ms": shooter("state").get("ping"),
        "runner_ping_ms": runner("state").get("ping"),
        "turns": moving.turns,
        "target": summarize_target(logs["shooter"]),
        "self": summarize_self(logs["runner"], len(packets["log"])),
        "position_log": packets["log"],
        "screenshot": shot_name,
    }
    error = lane_end_error(
        out.name,
        server_pos(rcon, names[1]),
        centre,
        runner_z,
        (report["target"]["speed_mps"] or {}).get("median"),
        report["self"]["server_corrections_or_teleports"],
    )
    if error is not None:
        raise SystemExit(error)
    (out / "summary.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps({k: v for k, v in report.items() if k != "position_log"}), flush=True
    )
    return report


def run_shots(rcon, shooter, runner, args, out):
    """Short confirmation: fire at the drawn model plus each lead, hit = server health drop."""
    out.mkdir(parents=True, exist_ok=True)
    names = shooter("state")["name"], runner("state")["name"]
    wait_settled(rcon, shooter, runner, names)
    names, centre, runner_z, z_far = place(rcon, shooter, runner, args.distance)
    lanes = {names[0]: (centre, z_far), names[1]: (centre, runner_z)}
    if not ensure_placed(
        lambda: place(rcon, shooter, runner, args.distance),
        lambda: {name: server_pos(rcon, name) for name in names},
        lanes,
    ):
        raise SystemExit(f"{out.name}: place() never landed both players on the lane")
    time.sleep(2)
    state = shooter("state")
    path = (
        f'Inventory[{{Slot:{state["slot"]}b}}].components."minecraft:custom_data"'
        ".GunCurrentAmmoCount"
    )
    leads = [float(x) for x in args.leads.split(",")]
    moving = Runner(runner, args.gait, centre)
    moving.start()
    rows = []
    shooter("tacz aim true")
    try:
        time.sleep(args.warm)
        require_running_lane(runner, centre, runner_z, out.name)
        for index in range(args.shots * len(leads)):
            lead = leads[index % len(leads)]
            rcon.cmd(f"effect give {names[1]} minecraft:instant_health 1 10 true")
            time.sleep(0.1)
            rcon.cmd(f"effect clear {names[1]} minecraft:instant_health")
            before = number(rcon.cmd(f"data get entity {names[1]} Health"))
            ammo = number(rcon.cmd(f"data get entity {names[0]} {path}"))
            view = shooter("combatview")
            fired = shooter(f"aimshoot {names[1]} {lead}")
            assert fired.get("result") == "SUCCESS", fired
            after_view = shooter("combatview")
            accepted = True
            try:
                server_shot(rcon, names[0], path, ammo)
            except TimeoutError:
                accepted = False
            lowest, until = before, time.monotonic() + 0.9
            while time.monotonic() < until:
                lowest = min(
                    lowest, number(rcon.cmd(f"data get entity {names[1]} Health"))
                )
                time.sleep(0.05)
            rows.append(
                {
                    "lead": lead,
                    "accepted": accepted,
                    "hit": accepted and lowest < before,
                    "ticket": after_view["sequence"] > view["sequence"],
                    "unavailable": after_view.get("reportUnavailable"),
                }
            )
            print(json.dumps(rows[-1]), flush=True)
    finally:
        moving.halt.set()
        moving.join(timeout=10)
        shooter("tacz aim false")
    if moving.error is not None:
        raise moving.error
    error = lane_end_error(out.name, server_pos(rcon, names[1]), centre, runner_z)
    if error is not None:
        raise SystemExit(error)
    report = {"distance": args.distance, "gait": args.gait, "leads": {}}
    for lead in leads:
        cell = [r for r in rows if r["lead"] == lead and r["accepted"]]
        hits = sum(r["hit"] for r in cell)
        report["leads"][str(lead)] = {
            "n": len(cell),
            "hits": hits,
            "wilson": wilson(hits, len(cell)),
            "tickets": sum(r["ticket"] for r in cell),
        }
    found = re.search(r"path=(\S+)", rcon.cmd("bf bodypose receipts") or "")
    if found and Path(found.group(1)).is_file():
        receipts = [
            json.loads(x) for x in Path(found.group(1)).read_text().splitlines()
        ]
        report["labels"] = dict(Counter(r["label"] for r in receipts if "label" in r))
    (out / "shots.json").write_text(
        json.dumps({"rows": rows, "report": report}, indent=2)
    )
    print(json.dumps(report), flush=True)
    return report


def run(args):
    rcon = connect_rcon(args.rcon_host, args.rcon_port, args.rcon_password)
    shooter, runner = Bot(args.bot_port), Shared(Bot(args.runner_port))
    try:
        if args.action == "setup":
            setup(rcon, shooter, runner, args)
        elif args.action == "connect":
            for bot in (shooter, runner):
                if bot("state").get("connected"):
                    bot("disconnect")
                    time.sleep(1)
                bot(f"connect 127.0.0.1:{args.port}")
            deadline = time.monotonic() + 90
            while not all(b("state").get("connected") for b in (shooter, runner)):
                if time.monotonic() > deadline:
                    raise TimeoutError("clients did not reconnect")
                time.sleep(0.5)
            # The first connect comes before `setup` opens the room.
            if Path(args.room_file).exists():
                time.sleep(8)
                deploy(rcon, shooter, runner, Path(args.room_file).read_text().strip())
        elif args.action == "shots":
            run_shots(rcon, shooter, runner, args, Path(args.out))
        else:
            run_cell(rcon, shooter, runner, args, Path(args.out))
    finally:
        shooter.close()
        runner.close()
        rcon.s.close()
    return 0


def register(sub):
    parser = sub.add_parser("pose-overlay", help=SCENARIO["help"])
    parser.add_argument("action", choices=("setup", "connect", "run", "shots"))
    parser.add_argument("--map", default="dust2")
    parser.add_argument("--port", type=int, default=25599)
    parser.add_argument("--fps", type=int, help="FPS cap of both clients")
    parser.add_argument("--runner-fps", type=int, help="overrides --fps for the runner")
    parser.add_argument("--distance", type=float, default=25)
    parser.add_argument("--gait", choices=tuple(GAITS), default="sprint")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--warm", type=float, default=5)
    parser.add_argument("--out", default=".dev/pose-overlay/cell")
    parser.add_argument("--leads", default="0")
    parser.add_argument("--shots", type=int, default=15, help="shots per lead")
    parser.add_argument("--runner-port", type=int, default=47778)
    parser.add_argument("--server-log", default=".dev/server/server.out")
    parser.add_argument("--client-logs", default=".dev/play/logs/latest.log")
    parser.add_argument("--expect-mod", help="blockfield version the logs must show")
    parser.add_argument("--room-file", default=".dev/pose-overlay-room")
    rcon_args(parser)
    parser.set_defaults(func=run)
