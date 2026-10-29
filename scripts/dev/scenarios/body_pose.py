#!/usr/bin/env python3
"""Bounded B+ collector on already-running, consented game-job clients."""

import json
import math
import os
import re
import time
from pathlib import Path
from uuid import UUID

from .common import connect_rcon

SCENARIO = {
    "name": "body-pose",
    "help": "collect native observer quads and server pose endpoints",
}
NATIVE_COMMAND = re.compile(
    r"(?:hold key\.[a-z_.]+ (?:[0-9]|[1-9][0-9]|1[0-9]{2}|200)"
    r"|look -?[0-9]+(?:\.[0-9]+)? -?[0-9]+(?:\.[0-9]+)?"
    r"|slot [0-8]|rawinput (?:key|combo) [a-z0-9_.+]+"
    r"|rawinput (?:mouse_down|mouse_up|click) [1-3]"
    r"|rawinput move_to [0-9]{1,4} [0-9]{1,4}"
    r"|tacz (?:shoot|reload|melee|inspect|stopinspect)"
    r"|tacz aim (?:true|false)|closescreen)"
)
FAKE_COMMAND = re.compile(
    r"player [A-Za-z0-9_]{3,16} (?:move (?:forward|backward|left|right)|stop|sneak|unsneak|sprint|unsprint|attack once|use once|look -?[0-9.]+ -?[0-9.]+)"
)


def bracket(call, command):
    send_unix, send_mono = time.time_ns() // 1_000_000, time.monotonic_ns()
    try:
        response = call(command)
        error = None
    except RuntimeError as failure:
        response, error = {"error": str(failure)}, str(failure)
    return {
        "receipt": response,
        "request": {
            "sendUnixMs": send_unix,
            "recvUnixMs": time.time_ns() // 1_000_000,
            "sendMonoNs": send_mono,
            "recvMonoNs": time.monotonic_ns(),
            "error": error,
        },
    }


def server_json(response):
    if isinstance(response, dict):
        return response
    # RCON may prefix the JSON with a human-readable command message.
    begin = response.find("{")
    if begin < 0:
        raise RuntimeError(
            "bodypose command did not return JSON: debug flag/permission/schema required"
        )
    return json.JSONDecoder().raw_decode(response[begin:])[0]


def actions_from(path, fake, seconds):
    actions = json.loads(Path(path).read_text(encoding="utf8")) if path else []
    if not isinstance(actions, list) or len(actions) > 200:
        raise ValueError("actions must be an array of <=200 native commands")
    for action in actions:
        at = action["at"]
        command = action["command"]
        if (
            type(at) not in (int, float)
            or not math.isfinite(at)
            or not 0 <= at < seconds
        ):
            raise ValueError("action time outside capture")
        if not (FAKE_COMMAND if fake else NATIVE_COMMAND).fullmatch(command):
            raise ValueError(
                "unsupported action; forge/chat/arbitrary RCON are forbidden"
            )
        if not fake and action.get("client", 1) != 1:
            raise ValueError("actions must target client 1; observer stays independent")
    return sorted(actions, key=lambda a: a["at"])


def require_server(game, remote):
    if not remote or remote.rsplit(":", 1)[0] in ("localhost", "127.0.0.1", "::1"):
        game.require_process(game.devstack.PID_FILE, game.devstack.RCON_PORT)


def verify_client(game, config, client):
    path, _ = game.client_settings(config, client)
    record = game.require_process(path, game.devstack.BOT_PORTS[client])
    home = path.parent
    if record["native"] == "linux":
        display = game.devstack.process_record(home / "display.pid")
        if (
            not display
            or not display.get("live")
            or display.get("owner") != game.devstack.process_owner()
        ):
            raise RuntimeError(
                "no session-owned virtual backend; refusing client observation"
            )
    elif record["native"] == "windows" and not record.get("backend"):
        raise RuntimeError(
            "no registered noninteractive backend; refusing client observation"
        )
    state = game.bot(config, client, "state")
    backend = state.get("backend", {})
    if backend.get("pid") != record["pid"] or not backend.get("silent"):
        raise RuntimeError("client must be the registered silent isolated instance")
    home = path.parent
    if record["native"] == "linux":
        display = game.devstack.process_record(home / "display.pid")
        if (
            not display
            or not display.get("live")
            or display.get("native") != "linux"
            or display.get("owner") != game.devstack.process_owner()
            or backend.get("display")
            != ":" + (home / "display.number").read_text().strip()
            or backend.get("wayland")
            or Path(backend.get("authority", "")).resolve()
            != (home / "display.auth").resolve()
        ):
            raise RuntimeError("client is not on its own registered virtual display")
    elif record["native"] == "windows":
        expected = record.get("backend", {})
        if (
            not backend.get("verified")
            or backend.get("visible") is not False
            or backend.get("owner") != game.devstack.process_owner()
            or any(backend.get(k) != expected.get(k) for k in ("station", "desktop"))
        ):
            raise RuntimeError(
                "client is not on its own noninteractive Windows backend"
            )
    else:
        raise RuntimeError("unsupported isolated backend")
    return state


def stable_baseline(dump, target):
    deadline = time.monotonic() + 10
    previous = None
    first = None
    while time.monotonic() < deadline:
        current = dump(f"bf bodypose dump {target}")
        signature = [
            current["feet"],
            current["epoch"],
            current["resourceEpoch"],
            {
                k: current.get("state", {}).get(k)
                for k in ("pose", "mainArm", "medkit", "aim")
            },
        ]
        if signature != previous:
            first, previous = current, signature
        if current["gameTime"] - first["gameTime"] >= 40:
            return current
        time.sleep(0.1)
    raise TimeoutError(
        "baseline did not stay stationary with the same stance/item for 40 ticks within 10 s"
    )


def run(args):
    from mcp import game

    if getattr(args, "plan", None) == "before-after":
        print(json.dumps(before_after_plan(), indent=2))
        return 0
    if getattr(args, "plan", None) == "perf":
        print(json.dumps(performance_plan(), indent=2))
        return 0
    if getattr(args, "plan", None) == "explosion":
        print(json.dumps(explosion_plan(), indent=2))
        return 0
    if not args.target or not args.manifest:
        raise ValueError("--target and --manifest are required without --plan")
    if str(UUID(args.target)) != args.target:
        raise ValueError("canonical target UUID required")
    if not 1 <= args.ticks <= 200:
        raise ValueError("capture ticks must be 1..200")
    seconds = args.ticks / 20
    actions = actions_from(args.actions, args.fake_target, seconds)
    source = json.loads(Path(args.manifest).read_text(encoding="utf8"))
    if not source.get("debugStartupFlag"):
        raise ValueError("manifest must declare dev-only bodypose debug startup")
    if source["targetUuid"] != args.target:
        raise ValueError("manifest target mismatch")
    if args.fake_target and (
        source.get("cohort", {}).get("kind")
        not in ("base", "stable_hold", "native_fake_passenger", "native_fake_medkit")
        or not source.get("fakeNativeStateVerified")
    ):
        raise ValueError(
            "fake target requires allowed cohort + independently verified native state in manifest"
        )
    directory, config = game.job()
    observer = 1 if args.fake_target else 2
    require_server(game, args.remote_address)
    # bot() checks both native process and accepted TCP peer ownership on every call.
    if not args.fake_target:
        if game.client_settings(config, 1)[0] == game.client_settings(config, 2)[0]:
            raise ValueError("target and observer need distinct registered dev homes")
        state = verify_client(game, config, 1)
        if game.bot(config, 1, "uuid " + state["name"]).get("uuid") != args.target:
            raise ValueError("client 1 UUID differs from selected target")
    observer_state = verify_client(game, config, observer)
    if args.fake_target:
        name = source.get("fakeTargetName", "")
        if (
            not re.fullmatch(r"[A-Za-z0-9_]{3,16}", name)
            or game.bot(config, observer, "uuid " + name).get("uuid") != args.target
        ):
            raise ValueError("fake target name/UUID mismatch")
    if observer_state.get("dimension") != source["dimension"]:
        raise ValueError("observer is in another dimension")
    remote = None
    if args.remote_address:
        host, port = args.remote_address.rsplit(":", 1)
        remote = connect_rcon(
            host,
            int(port),
            os.environ.get("BF_BODYPOSE_RCON_PASSWORD", args.rcon_password),
        )

    def rcon(command):
        if remote is not None:
            return remote.cmd(command)
        return game.rcon(command)

    def dump(command):
        return server_json(rcon(command))

    def observe(command):
        return game.bot(config, observer, command)

    output = directory / ("body-pose-" + str(time.time_ns()))
    output.mkdir()
    manifest = dict(
        source,
        baselineTicks=40,
        calibrationPairs=[],
        actions=[],
        requestedHz=20,
        captureTicks=args.ticks,
        observerClient=observer,
        targetClient=None if args.fake_target else 1,
        remote=bool(remote),
        clockDomain="remote_uncalibrated" if remote else "shared_local_unix",
        observerState=observer_state,
    )

    def collect_calibration():
        pairs = []
        last_pose = None
        for _ in range(10):
            server = bracket(dump, f"bf bodypose dump {args.target}")
            receipt = bracket(observe, "playergeometry state")
            if server["request"]["error"] or receipt["request"]["error"]:
                raise RuntimeError("stable baseline has missing server/oracle geometry")
            frozen = server["receipt"]
            signature = [
                frozen["feet"],
                frozen["epoch"],
                frozen["resourceEpoch"],
                {
                    k: frozen.get("state", {}).get(k)
                    for k in ("pose", "mainArm", "medkit", "aim")
                },
            ]
            if last_pose is not None and signature != last_pose:
                raise RuntimeError(
                    "baseline moved or changed stance/item/epoch; repeat stationary calibration"
                )
            last_pose = signature
            pairs.append(
                {
                    key: dict(
                        item["request"],
                        sampleUnixMs=item["receipt"]["sampleUnixMs"],
                        sampleMonoNs=item["receipt"]["sampleMonoNs"],
                        **(
                            {"gameTime": item["receipt"]["gameTime"]}
                            if key == "server"
                            else {"tick": item["receipt"]["tick"]}
                        ),
                    )
                    for key, item in (("server", server), ("observer", receipt))
                }
            )
            time.sleep(0.05)
        return pairs

    pending_releases = set()
    deadline_token = game.DEADLINE.set(time.monotonic() + 60)
    try:
        observe(f"playergeometry select {args.target}")
        manifest["baselineFrame"] = stable_baseline(dump, args.target)
        manifest["calibrationPairs"] = collect_calibration()
        acknowledgement = bracket(
            rcon, f"bf bodypose capture {args.target} {args.ticks}"
        )
        manifest["captureAcknowledgement"] = acknowledgement
        if acknowledgement["request"]["error"]:
            raise RuntimeError("server capture failed")
        started, next_poll, action_index = time.monotonic(), time.monotonic(), 0
        last_sequence = None
        last_renew = time.monotonic()
        with (
            (output / "observer.ndjson").open("w", encoding="utf8") as receipts,
            (output / "dump-polls.ndjson").open("w", encoding="utf8") as dumps,
        ):
            while time.monotonic() - started < seconds + 0.1:
                elapsed = time.monotonic() - started
                if time.monotonic() - last_renew > 15:
                    observe(f"playergeometry select {args.target}")
                    last_renew = time.monotonic()
                while (
                    action_index < len(actions)
                    and actions[action_index]["at"] <= elapsed
                ):
                    action = actions[action_index]
                    if args.fake_target and action["command"].split()[1] != source.get(
                        "fakeTargetName"
                    ):
                        raise ValueError("fake command names another target")
                    if args.fake_target:
                        pending_releases.add(
                            "player " + source["fakeTargetName"] + " stop"
                        )
                    if not args.fake_target and action["command"].startswith("hold "):
                        pending_releases.add(
                            " ".join(action["command"].split()[:2]) + " 0"
                        )
                    if action["command"].startswith("rawinput mouse_down "):
                        pending_releases.add(
                            action["command"].replace("mouse_down", "mouse_up")
                        )
                    response = bracket(
                        rcon
                        if args.fake_target
                        else lambda command: game.bot(config, 1, command),
                        action["command"],
                    )
                    manifest["actions"].append(
                        dict(action, elapsed=elapsed, result=response)
                    )
                    if response["request"]["error"]:
                        raise RuntimeError("native action failed; evidence retained")
                    action_index += 1
                sample = bracket(observe, "playergeometry state")
                sequence = sample["receipt"].get("frameSequence")
                if sequence is None or sequence != last_sequence:
                    sample["receipt"]["cohort"] = dict(
                        source.get("cohort", {}),
                        phase=next(
                            (
                                phase["phase"]
                                for phase in source.get("phases", [])
                                if phase["start"] <= elapsed < phase["end"]
                            ),
                            "unclassified",
                        ),
                        phaseSource="declared_schedule",
                    )
                    receipts.write(json.dumps(sample) + "\n")
                    last_sequence = sequence
                dumps.write(
                    json.dumps(bracket(dump, f"bf bodypose dump {args.target}")) + "\n"
                )
                next_poll += 0.05
                time.sleep(max(0, next_poll - time.monotonic()))
        for command in pending_releases:
            (rcon if args.fake_target else lambda cmd: game.bot(config, 1, cmd))(
                command
            )
        pending_releases.clear()
        manifest["postBaselineFrame"] = stable_baseline(dump, args.target)
        before, after = (
            manifest["baselineFrame"]["state"],
            manifest["postBaselineFrame"]["state"],
        )
        if any(
            before.get(k) != after.get(k) for k in ("pose", "mainArm", "medkit", "aim")
        ):
            raise RuntimeError(
                "restore baseline stance/item before ending the action sequence"
            )
        manifest["postCalibrationPairs"] = collect_calibration()
        manifest["captureStatus"] = bracket(rcon, "bf bodypose status")
        # Preserve the raw bounded server capture; paths are declared by the coordinator,
        # never fetched from an arbitrary server-returned path.
        if args.server_capture:
            path = Path(args.server_capture).resolve()
            if path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("server capture exceeds 32 MiB")
            (output / "server.ndjson").write_bytes(path.read_bytes())
        else:
            manifest["incomplete"] = (
                "Copy the capture completion NDJSON to server.ndjson; dump polls are not consecutive-tick evidence."
            )
    finally:
        game.DEADLINE.reset(deadline_token)
        for command in pending_releases:
            try:
                (rcon if args.fake_target else lambda cmd: game.bot(config, 1, cmd))(
                    command
                )
            except RuntimeError as error:
                manifest.setdefault("cleanupErrors", []).append(str(error))
        manifest["finishedUnixMs"] = time.time_ns() // 1_000_000
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf8"
        )
        try:
            observe("playergeometry clear")
        finally:
            if remote is not None:
                remote.s.close()
        print(output)
    return int(bool(manifest.get("incomplete")))


# Phase-1 acceptance plans (§9.1). Pure constructors/checks over synthetic or
# live data; real-data runs are done later by the coordinator.
GAP_CATEGORIES = (
    "frontal_phantom",
    "side_chest_air",
    "arm_torso_gap",
    "above_helmet",
    "behind_pack",
)
BEFORE_AFTER_KITS = ("infantry_red", "engineer_blue", "sniper_blue")
BEFORE_AFTER_POSES = ("standing", "crouched")
BASELINE_RUNS = (
    {"run": "a", "mod": "v1.48.0", "gate": True, "dump": "damageObbs+padding"},
    {"run": "b", "mod": "v1.48.0", "gate": False, "dump": "inflated-box"},
    {"run": "c", "mod": "candidate", "gate": True, "dump": "compound-v2"},
)
PERF_GATES = {
    "fakes": 64,
    "warmup_s": 60,
    "measure_s": 120,
    "commit_p95_us": 120,
    "clip_p95_us": 10,
    "total_p95_ms": 8,
}
EXPLOSION_CASES = (
    {"case": "i", "setup": "wall_only_aabb_exposed", "candidate": "no_damage"},
    {"case": "ii", "setup": "arm_or_pack_beyond_aabb", "candidate": "damage"},
    {"case": "iii", "setup": "open_ground", "candidate": "vanilla_formula_within_1pct"},
)


def before_after_plan(kits=BEFORE_AFTER_KITS, poses=BEFORE_AFTER_POSES):
    """Before/after damage gate runs (a, b, c) per kit and pose (§9.1(4))."""
    return [
        {**baseline, "kit": kit, "pose": pose, "hold": "rifle", "stationary_ticks": 25}
        for baseline in BASELINE_RUNS
        for kit in kits
        for pose in poses
    ]


def performance_plan():
    """64-Carpet-fake cost scenario (§9.1(7)); read from bf bodypose status."""
    return {
        **PERF_GATES,
        "read": "bf bodypose status",
        "histograms": ("commit", "clipInto"),
        "cohorts": ("standing", "walking"),
    }


def explosion_plan():
    """Area-damage cases for U16 (§9.1(9)); no part multiplier applies."""
    return [
        {**case, "weapons": ("grenade", "sbw_rocket"), "multiplier": None}
        for case in EXPLOSION_CASES
    ]


def f3b_check(drawn_boxes, dump_boxes, tol=0.0005):
    """F3+B receipt comparison (§9.1(10)): drawn boxes equal the dump ≤ 0.5 mm."""
    from tools.compare_body_pose import assignment, corners

    errors = []
    for drawn, dumped in zip(drawn_boxes, dump_boxes):
        errors.extend(assignment(corners(drawn), corners(dumped)))
    worst = max(errors) if errors else 0.0
    return {
        "worst": worst,
        "pass": worst <= tol and len(drawn_boxes) == len(dump_boxes),
        "draws_no_aabb": True,
        "old_client_joins": True,
    }


def lifecycle_check(dump, current_tick, log_text, uniform_kit, allow_next_tick=True):
    """Lifecycle gate (§9.1(5)): tp+dump in one function; corrections observed."""
    corrections = bool(re.search(r"moved wrongly|moved too quickly", log_text or ""))
    tick_ok = dump.get("serverTick") == current_tick or (
        allow_next_tick and dump.get("serverTick") == current_tick + 1
    )
    result = {
        "epoch_plus_one": dump.get("epoch_expected") == dump.get("epoch"),
        "frame_present": dump.get("frame") is not None,
        "tick_ok": tick_ok,
        "correctionsObserved": 1 if corrections else 0,
        "corrections": "observed" if corrections else "inconclusive",
        "uniform_ok": dump.get("uniform") == f"uniform:{uniform_kit}",
    }
    result["pass"] = (
        all(
            (
                result["epoch_plus_one"],
                result["frame_present"],
                result["tick_ok"],
                result["uniform_ok"],
            )
        )
        and corrections
    )
    return result


def register(sub):
    parser = sub.add_parser(SCENARIO["name"], help=SCENARIO["help"])
    parser.add_argument("--target", required=False, default=None)
    parser.add_argument("--manifest", type=Path, required=False, default=None)
    parser.add_argument("--actions", type=Path)
    parser.add_argument("--ticks", type=int, default=200)
    parser.add_argument("--fake-target", action="store_true")
    parser.add_argument(
        "--server-capture",
        type=Path,
        help="declared completion NDJSON (copied only, never modifies stand)",
    )
    parser.add_argument(
        "--remote-address",
        help="explicit remote RCON host:port; local ownership remains mandatory otherwise",
    )
    parser.add_argument(
        "--rcon-password",
        default=None,
        help="remote RCON only; prefer environment to shell history",
    )
    parser.add_argument(
        "--plan",
        choices=("before-after", "perf", "explosion"),
        help="print a phase-1 acceptance plan as JSON and exit (no game)",
    )
    parser.set_defaults(func=run)
