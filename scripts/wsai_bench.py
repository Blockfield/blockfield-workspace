"""Reusable Workshop AI benchmark harness: stand + matrix runner + metrics.

Runs model generations on an isolated headless stand (own BF_DEV_HOME),
records submit/preview/apply timings, per-phase model calls, tokens and cost,
takes fixed-camera screenshots and writes results.json + results.csv.

Derived from the proven .dev/jobs/wsai-ui-3 acceptance harness
(start-workshop.py, drive.py, ui.py, stand-cycle-v2.sh, memory-guard.py).
Stdlib only.
"""

import argparse
import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / ".dev"
ACCEPT = DEV / "wsai-accept"
SECRETS_KEY = DEV / "secrets" / "openrouter-eval.key"
GRADLE_SLOT = ROOT / ".dev" / "coord-20261009" / "gradle-slot"
JDK = DEV / "jdk" / "temurin-21.0.12+8-linux-amd64"
SERVER_PORTS = (25599, 25575, 47777, 5005, 5006)
SERVER_ADDR = "127.0.0.1:25599"
UTC3 = datetime.timezone(datetime.timedelta(hours=3))

# Reserve-side tariff (USD per million tokens) keyed by model id, verified
# against OpenRouter's public model pages on 2026-10-22. Actual spend stays the
# receipts' costUsd; the table only sizes the reserve/estimate.
MODEL_TARIFF_USD_PER_M = {
    "meta/muse-spark-1.3-contributor": (0.10, 0.20),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "deepseek/deepseek-v4.1-flash-1017": (0.15, 0.60),
    "minimax/minimax-m3.1-flash:free": (0.0, 0.0),
    "openai/gpt-6-luna": (0.10, 0.50),
}
# Conservative fallback for models outside the matrix: the highest known tariff.
UNKNOWN_MODEL_TARIFF = (0.15, 0.60)
# Default estimate tariff ($0.10/M prompt, $0.50/M completion); kept for callers
# that do not pass an explicit tariff.
USD_PER_M = {"prompt": 0.10, "completion": 0.50}
# Conservative headroom for one unknown job: full-context generation bound
# plus parallel parts, repairs and a tool loop, rounded up.
EST_JOB_MICROUSD = 600_000


def devstack():
    sys.path.insert(0, str(ROOT / "scripts" / "dev"))
    import devstack as stack

    return stack


def home():
    return Path(os.environ["BF_DEV_HOME"])


def log_call(row):
    path = Path(
        os.environ.get("WSAI_BENCH_CALLS", DEV / "jobs" / "model-ab" / "calls.jsonl")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def bot(command, timeout=30):
    stack = devstack()
    record = stack.process_record(home() / "client.pid")
    assert record and record.get("live") and record["owner"] == stack.process_owner()
    out = io.StringIO()
    started = time.time()
    with redirect_stdout(out):
        stack.bot(
            argparse.Namespace(client=1, command=command.split(" "), timeout=timeout)
        )
    text = out.getvalue().strip()
    log_call({"at": started, "bot": command, "out": text[:4000]})
    try:
        return json.loads(text)
    except ValueError:
        return text


def bot_full(command, timeout=60):
    stack = devstack()
    record = stack.process_record(home() / "client.pid")
    assert record and record.get("live") and record["owner"] == stack.process_owner()
    out = io.StringIO()
    started = time.time()
    with redirect_stdout(out):
        stack.bot(
            argparse.Namespace(client=1, command=command.split(" "), timeout=timeout)
        )
    text = out.getvalue().strip()
    log_call({"at": started, "bot": command, "out": text[:4000]})
    try:
        return json.loads(text)
    except ValueError:
        return text


def rcon(command, timeout=120):
    started = time.time()
    proc = subprocess.run(
        ["just", "server", "rcon", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    log_call(
        {
            "at": started,
            "rcon": command,
            "out": proc.stdout[-2000:],
            "exit": proc.returncode,
        }
    )
    return proc.stdout


def snap(label, out_path, width=1600):
    src = home() / "play" / "screenshots" / (label + ".png")
    src.unlink(missing_ok=True)
    bot("screenshot " + label + ".png")
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if (
            src.exists()
            and src.stat().st_size > 12
            and src.read_bytes()[-8:] == b"IEND\xaeB`\x82"
        ):
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("screenshot " + label)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "magick",
            str(src),
            "-resize",
            f"{width}x{width}>",
            "-quality",
            "85",
            str(out_path),
        ],
        check=True,
        timeout=60,
    )
    src.unlink()
    return out_path


# ---------------------------------------------------------------- matrix ---


def load_matrix(path):
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    defaults = raw.get("defaults", {})
    jobs = raw.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("matrix needs a non-empty jobs list")
    merged = []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"job {index} is not an object")
        item = dict(defaults)
        item.update(job)
        merged.append(validate_job(item, index))
    return merged


def validate_job(job, index):
    prompt = job.get("prompt")
    if not prompt or not isinstance(prompt, str) or len(prompt) > 4096:
        raise ValueError(f"job {index}: prompt must be 1..4096 characters")
    mode = job.get("mode", "fast")
    if mode not in ("fast", "quality"):
        raise ValueError(f"job {index}: mode must be fast or quality")
    size = job.get("size")
    if (
        not isinstance(size, list)
        or len(size) != 3
        or any(not isinstance(side, int) or side < 1 or side > 128 for side in size)
    ):
        raise ValueError(f"job {index}: size must be [W,H,D] with sides 1..128")
    model = job.get("model")
    if not model or not isinstance(model, str):
        raise ValueError(f"job {index}: model must be set")
    origin = job.get("origin")
    if origin is not None and (
        not isinstance(origin, list)
        or len(origin) != 3
        or any(not isinstance(v, int) for v in origin)
    ):
        raise ValueError(f"job {index}: origin must be [x,y,z]")
    return {
        "prompt": prompt,
        "mode": mode,
        "model": model,
        "size": list(size),
        "origin": origin,
    }


def grid_origin(index, base, stride, cols):
    bx, bz = base
    return [bx + (index % cols) * stride, None, bz + (index // cols) * stride]


def select_corners(origin, size):
    x, y, z = origin
    w, h, d = size
    return (x, y, z), (x + w - 1, y + h - 1, z + d - 1)


# ----------------------------------------------------------------- spend ---


def confirmed_microusd(stand):
    usage = json.loads(
        (stand / "workshop-admin" / "ai-usage.json").read_text(encoding="utf-8")
    )
    accounts = usage.get("accounts", {})
    return sum(v for v in accounts.values() if isinstance(v, (int, float)))


def spend_guard_ok(confirmed, estimate_microusd, cap_microusd):
    return confirmed + estimate_microusd <= cap_microusd


def tariff_for(model):
    return MODEL_TARIFF_USD_PER_M.get(model, UNKNOWN_MODEL_TARIFF)


def reserve_tariff(models):
    """Element-wise max tariff over a matrix: the reserve covers the priciest model."""
    prompt = max(tariff_for(model)[0] for model in models)
    completion = max(tariff_for(model)[1] for model in models)
    return (prompt, completion)


def _tariff_rate(usd_per_m):
    return int(round(usd_per_m * 100))


def call_bound_microusd(input_tokens, max_tokens, tariff=None):
    # Mirrors WorkshopAiProvider.callBoundFor at the given tariff: prompt $x/M
    # (x100), completion $y/M (x100), ceiling division by 100. Defaults to the
    # single test tariff ($0.10/$0.50).
    prompt, completion = tariff or (USD_PER_M["prompt"], USD_PER_M["completion"])
    return (
        (input_tokens + 2048) * _tariff_rate(prompt)
        + max_tokens * _tariff_rate(completion)
        + 99
    ) // 100


def generation_bound_microusd(tokens=64000, tariff=None):
    prompt, completion = tariff or (USD_PER_M["prompt"], USD_PER_M["completion"])
    return (
        1_048_576 * _tariff_rate(prompt) + tokens * _tariff_rate(completion) + 99
    ) // 100


# ------------------------------------------------------------------- log ---

AI_USAGE_RE = re.compile(r"AI usage (\{.*\})")
AI_CALL_RE = re.compile(r"AI call phase=(\S+) mode=(\S+) effort=(\S+) maxTokens=(\d+)")
AI_STRATEGY_RE = re.compile(r"AI strategy class=(\S+) mode=(\S+)")
AI_ETA_RE = re.compile(
    r"AI eta job=(\S+) key=(\S+) expectedMs=(\d+) budgetSeconds=(\d+)"
)
AI_QUALITY_RE = re.compile(r"AI quality job=(\S+) before=(\{.*\}) after=(\{.*\})")


def parse_ai_quality(line):
    match = AI_QUALITY_RE.search(line)
    if not match:
        return None
    try:
        return {
            "job": match.group(1),
            "before": json.loads(match.group(2)),
            "after": json.loads(match.group(3)),
            "line": line[-500:],
        }
    except ValueError:
        return None


AI_FAILURE_RES = [
    re.compile(r"AI provider failure: (phase=\S+ category=\S+)"),
    re.compile(r"AI repair phase=(\S+) result=rejected category=(\S+) reason=(.*)"),
    re.compile(r"AI empty generation response, retrying once job=(\S+) phase=(\S+)"),
    re.compile(r"AI polish attempt=(\d+) fallback=(\S+) category=(\S+)"),
]

# OpenRouter rate-limit signatures (HTTP 429 / "rate limit" variants). The free
# MiniMax tier hits these on parallel parts; a job that stays limited is marked
# rate_limited, never a quality failure.
RATE_LIMIT_RES = [
    re.compile(p, re.IGNORECASE)
    for p in (r"\b429\b", r"rate.?limit", r"rate_limited", r"too many requests")
]
# Bounded resubmission per job: at most this many extra attempts, waiting the
# matching backoff first. The loop below never retries beyond the list, so a
# persistently limited run always terminates.
RATE_LIMIT_MAX_RETRIES = 2
RATE_LIMIT_BACKOFF_S = (5, 15)


def is_rate_limit_line(line):
    return any(pattern.search(line) for pattern in RATE_LIMIT_RES)


def rate_limit_hits(lines):
    return sum(1 for line in lines if is_rate_limit_line(line))


def rate_limit_backoff_s(attempt):
    """Bounded wait before retry `attempt` (0-based); None when no retry is left."""
    if attempt < 0 or attempt >= len(RATE_LIMIT_BACKOFF_S):
        return None
    return RATE_LIMIT_BACKOFF_S[attempt]


def classify_error(failures, rate_hits):
    """rate_limited iff limits were seen and no non-limit failure explains the error."""
    if rate_hits > 0 and not any(not is_rate_limit_line(f) for f in failures):
        return "rate_limited"
    return "error"


def parse_ai_usage(line):
    match = AI_USAGE_RE.search(line)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def parse_ai_call(line):
    match = AI_CALL_RE.search(line)
    if not match:
        return None
    phase, mode, effort, cap = match.groups()
    return {"phase": phase, "mode": mode, "effort": effort, "maxTokens": int(cap)}


def parse_generation_mode(lines):
    for line in lines:
        parsed = parse_ai_call(line)
        if parsed and parsed["phase"] == "generation":
            return parsed["mode"]
    return None


def parse_strategy_volume(lines):
    for line in lines:
        match = AI_STRATEGY_RE.search(line)
        if match:
            volume = re.search(r"volume=(\d+)", line)
            if volume:
                return int(volume.group(1))
    return None


def log_stamp(line):
    match = re.match(r"\[(\d\d):(\d\d):(\d\d)\]", line)
    if not match:
        return None
    hh, mm, ss = (int(v) for v in match.groups())
    return hh * 3600 + mm * 60 + ss


def phase_latency(call_stamp, usage_stamp):
    if call_stamp is None or usage_stamp is None:
        return None
    delta = usage_stamp - call_stamp
    if delta < 0:
        delta += 86400
    return delta


# ------------------------------------------------------------------ plan ---


def op_volume(op):
    a, b = op["from"], op["to"]
    sides = [abs(b[i] - a[i]) + 1 for i in range(3)]
    volume = sides[0] * sides[1] * sides[2]
    if op.get("hollow"):
        inner = max(0, sides[0] - 2) * max(0, sides[1] - 2) * max(0, sides[2] - 2)
        return volume - inner
    return volume


def block_name(block):
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        mix = block.get("mix")
        if isinstance(mix, list) and mix:
            return max(mix, key=lambda entry: entry[1])[0]
    return "unknown"


def iter_leaves(ops):
    for op in ops:
        if not isinstance(op, dict):
            continue
        nested = op.get("operations")
        if isinstance(nested, list) and "from" not in op:
            yield from iter_leaves(nested)
        elif all(k in op for k in ("from", "to", "block")):
            yield op


def plan_stats(plan):
    if isinstance(plan, str):
        plan = json.loads(plan)
    ops = plan.get("operations", []) if isinstance(plan, dict) else []
    blocks = 0
    kinds = {}
    lo = [10**9, 10**9, 10**9]
    hi = [-(10**9), -(10**9), -(10**9)]
    solid = 0
    for op in iter_leaves(ops):
        size = op_volume(op)
        blocks += size
        solid += 1
        name = block_name(op["block"])
        kinds[name] = kinds.get(name, 0) + size
        for i in range(3):
            lo[i] = min(lo[i], op["from"][i], op["to"][i])
            hi[i] = max(hi[i], op["from"][i], op["to"][i])
    ranked = sorted(kinds.items(), key=lambda item: -item[1])
    return {
        "operations": len(ops),
        "solidOperations": solid,
        "blocksPlaced": blocks,
        "distinctBlocks": len(kinds),
        "topBlocks": [{"block": name, "cells": count} for name, count in ranked[:8]],
        "footprint": {"min": lo if solid else None, "max": hi if solid else None},
    }


def footprint_vs_selection(stats, size):
    fp = stats.get("footprint")
    if not fp or not fp["min"]:
        return "empty"
    sides = [fp["max"][i] - fp["min"][i] + 1 for i in range(3)]
    if sides == size:
        return "exact"
    if all(sides[i] <= size[i] for i in range(3)):
        return "inside"
    return "overflow"


# ---------------------------------------------------------------- camera ---


def camera_for_plot(origin, size, shot):
    import math

    x0, y0, z0 = origin
    w, h, d = size
    cx, cy, cz = x0 + w / 2, y0 + h / 2, z0 + d / 2
    side = max(size)
    if shot == "a":
        dist, bearing, lift = 1.6 * side, math.radians(45), 0.92 * side
    elif shot == "b":
        dist, bearing, lift = 1.6 * side, math.radians(225), 0.92 * side
    else:
        dist, bearing, lift = 0.8 * side, math.radians(180), 0.30 * side
    px = cx + dist * math.cos(bearing)
    pz = cz + dist * math.sin(bearing)
    return (px, cy + lift, pz), (cx, cy, cz)


def apply_rate(blocks, seconds):
    if not seconds or seconds <= 0:
        return None
    return round(blocks / seconds, 1)


# ----------------------------------------------------------------- stand ---


def mem_available_kb():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise RuntimeError("no MemAvailable in /proc/meminfo")


def memory_gate(min_kb=4_500_000, tries=11, wait=60):
    for step in range(tries):
        available = mem_available_kb()
        print(f"MemAvailable={available} kB required={min_kb}", flush=True)
        if available >= min_kb:
            return
        if step == tries - 1:
            raise SystemExit("BLOCKED: MemAvailable stayed below the gate")
        time.sleep(wait)


def ports_free():
    busy = []
    for port in SERVER_PORTS:
        sock = socket.socket()
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            busy.append(port)
        sock.close()
    return busy


def wait_ports_free(deadline_s=1800):
    end = time.monotonic() + deadline_s
    while True:
        busy = ports_free()
        if not busy:
            return
        print("stand ports occupied (TCP only, no probing):", busy, flush=True)
        subprocess.run(["just", "status"], cwd=ROOT)
        if time.monotonic() >= end:
            raise SystemExit("BLOCKED: another stand stayed registered for 30 min")
        time.sleep(60)


def guard_loop(stop, abort, result, deadline_s=2700):
    def oom_kills():
        for line in Path("/proc/vmstat").read_text().splitlines():
            if line.startswith("oom_kill "):
                return int(line.split()[1])
        return 0

    initial = oom_kills()
    low_since = None
    lowest = mem_available_kb()
    end = time.monotonic() + deadline_s
    while not stop.is_set() and time.monotonic() < end:
        mem = mem_available_kb()
        lowest = min(lowest, mem)
        if oom_kills() > initial:
            result["abort"] = "OOM kill counter increased"
            break
        if mem < 300_000:
            if low_since is None:
                low_since = time.monotonic()
            elif time.monotonic() - low_since > 120:
                result["abort"] = (
                    "MemAvailable below 300000 kB for more than 120 seconds"
                )
                break
        else:
            low_since = None
        time.sleep(2)
    result["lowest_available_kb"] = lowest
    if result.get("abort"):
        abort.set()


def read_key(key_file):
    fd = os.open(key_file, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 2048
        with os.fdopen(fd, "r", encoding="ascii", closefd=False) as stream:
            key = stream.read(2049).strip()
    finally:
        os.close(fd)
    assert key and len(key) <= 2048 and not any(c.isspace() for c in key)
    return key


def prepare_stand(
    name, client_jar, server_jar, overrides, session_limit, palette, reset_usage=True
):
    stand = DEV / name
    if stand.exists():
        raise SystemExit(f"stand {stand} already exists; cleanup first")
    subprocess.run(
        ["cp", "-a", "--reflink=always", str(ACCEPT), str(stand)],
        check=True,
        timeout=600,
    )
    (stand / ".keep").unlink(missing_ok=True)
    return configure_stand(
        stand, client_jar, server_jar, overrides, session_limit, palette, reset_usage
    )


def configure_stand(
    stand, client_jar, server_jar, overrides, session_limit, palette, reset_usage=True
):
    rows = []
    for side, folder, source in (
        ("client", "play", client_jar),
        ("server", "server", server_jar),
    ):
        targets = list((stand / folder / "mods").glob(f"blockfield-{side}-*.jar"))
        assert len(targets) == 1, (side, targets)
        shutil.copyfile(source, targets[0])
        digest = hashlib.sha256(targets[0].read_bytes()).hexdigest()
        rows.append(
            {
                "side": side,
                "source": str(source),
                "installed": str(targets[0]),
                "sha256": digest,
            }
        )
    config_path = stand / "server" / "config" / "blockfield" / "workshop-ai.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(overrides)
    if not palette:
        config.pop("palette", None)
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    session_id = json.loads((stand / "workshop-admin" / "session.json").read_text())[
        "id"
    ]
    limits_path = stand / "workshop-admin" / "ai-limits.json"
    limits = json.loads(limits_path.read_text(encoding="utf-8"))
    limits.setdefault("sessions", {})[session_id] = session_limit
    limits_path.write_text(json.dumps(limits, indent=2), encoding="utf-8")
    if reset_usage:
        (stand / "workshop-admin" / "ai-usage.json").write_text(
            json.dumps(
                {"schema": 1, "currency": "USD", "blocked": False, "accounts": {}}
            ),
            encoding="utf-8",
        )
    return stand, rows


def start_server(stand, key):
    stack = devstack()
    assert stack.HOME.resolve() == stand.resolve()
    assert not stack.server_pid() and not stack.recorded_pid(stack.CLIENT_PID_FILE)
    assert not stack.port_open(stack.PORT) and not stack.port_open(stack.RCON_PORT)
    admin = stand / "workshop-admin"
    env = dict(os.environ)
    env.update(
        {
            "BLOCKFIELD_PROFILE": "workshop",
            "BLOCKFIELD_MEMBERS_FILE": str(admin / "members.json"),
            "BLOCKFIELD_BACKEND_PROFILE_FILE": str(admin / "profile.json"),
            "BLOCKFIELD_WORKSHOP_SESSION_FILE": str(admin / "session.json"),
            "BLOCKFIELD_WORKSHOP_CANDIDATES_FILE": str(admin / "candidates.json"),
            "BLOCKFIELD_WORKSHOP_AI_LIMITS_FILE": str(admin / "ai-limits.json"),
            "BLOCKFIELD_WORKSHOP_AI_USAGE_FILE": str(admin / "ai-usage.json"),
            "BLOCKFIELD_WORKSHOP_AI_SKETCHES_DIR": str(admin / "ai-sketches"),
            "BLOCKFIELD_ADMIN_SETTINGS_FILE": str(
                stand / "server" / "blockfield-admin" / "settings.json"
            ),
            "BLOCKFIELD_WORKSHOP_AI_OPENROUTER_KEY": key,
        }
    )
    proc = subprocess.run(
        ["just", "server", "start", "--gate", "--xms", "512M", "--xmx", "3G"],
        cwd=ROOT,
        env=env,
        timeout=390,
    )
    env.pop("BLOCKFIELD_WORKSHOP_AI_OPENROUTER_KEY", None)
    assert proc.returncode == 0, "own server start failed"


def wait_ai_enabled(stand, timeout=300):
    end = time.monotonic() + timeout
    marker = "AI-мастерская включена"
    while time.monotonic() < end:
        try:
            if marker in (stand / "server" / "server.out").read_text(
                encoding="utf-8", errors="replace"
            ):
                return
        except FileNotFoundError:
            pass
        time.sleep(5)
    raise RuntimeError("AI backend never enabled")


def start_client(player, resolution):
    proc = subprocess.run(
        [
            "just",
            "play",
            "--headless",
            "--xmx",
            "3G",
            "--resolution",
            resolution,
            "--server",
            SERVER_ADDR,
            "--name",
            player,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


def wait_ready(timeout=180):
    stack = devstack()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = bot("state")
        if (
            isinstance(state, dict)
            and state.get("connected")
            and not state.get("loadingOverlay")
            and state.get("dimension")
        ):
            backend = state["backend"]
            assert backend["silent"] and backend["voiceChatBlocked"]
            assert backend["wayland"] is None
            assert backend["authority"].startswith(str(home()))
            record = stack.process_record(home() / "display.pid")
            assert (
                record
                and record.get("live")
                and record["owner"] == stack.process_owner()
            )
            return state
        time.sleep(0.5)
    raise RuntimeError("client render-ready deadline expired")


def stop_all():
    for args in (["just", "play", "--stop"], ["just", "server", "stop"]):
        try:
            subprocess.run(args, cwd=ROOT, capture_output=True, timeout=120)
        except Exception:
            pass


def cleanup_stand(name):
    stop_all()
    proc = subprocess.run(
        ["just", "dev", "prune", "--apply", f".dev/{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise SystemExit(f"prune refused .dev/{name}: {proc.stdout}{proc.stderr}")


# ------------------------------------------------------------------- run ---


def find_ground(x, z, lo=40, hi=200, step=8):
    top = None
    y = hi
    while y >= lo:
        block = bot(f"block {x} {y} {z}")
        solid = isinstance(block, dict) and not block.get("collisionEmpty", True)
        if solid:
            top = y
            break
        y -= step
    if top is None:
        raise RuntimeError(f"no ground at {x},{z} in {lo}..{hi}")
    for y in range(min(top + step - 1, hi), top, -1):
        block = bot(f"block {x} {y} {z}")
        if isinstance(block, dict) and not block.get("collisionEmpty", True):
            return y
    return top


def plot_ground(origin_x, origin_z, size):
    w, _, d = size
    points = [
        (origin_x, origin_z),
        (origin_x + w - 1, origin_z),
        (origin_x, origin_z + d - 1),
        (origin_x + w - 1, origin_z + d - 1),
        (origin_x + w // 2, origin_z + d // 2),
    ]
    tops = [find_ground(x, z) for x, z in points]
    return max(tops), max(tops) - min(tops)


def read_ai_fast(props_path):
    if not props_path.is_file():
        return None
    for line in props_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("workshop.aiFast="):
            return line.split("=", 1)[1].strip().lower() == "true"
    return None


def set_mode(mode):
    props_path = home() / "play" / "config" / "blockfield-settings.properties"
    want_fast = mode == "fast"
    current = read_ai_fast(props_path)
    if current is None or current != want_fast:
        bot("screen workshopai")
        time.sleep(0.5)
        widgets = bot("widgets")
        widget = next(w for w in widgets if w["index"] == 2)
        click_widget(widget, 0.25 if want_fast else 0.75)
        time.sleep(0.5)
        bot("closescreen")
        current = read_ai_fast(props_path)
    if current != want_fast:
        raise RuntimeError(f"speed toggle did not stick (want {mode})")
    return current


def click_widget(widget, fx, fy=0.5):
    import math as m

    state = bot("state")
    w, h = state["guiSize"]
    width = max(300, min(340, m.floor(w * 0.42 + 0.5)))
    pad = max(36, min(88, min(h - 16, 264) - 176))
    height = 176 + pad
    zoom = max(0.01, min(1, (w - 16) / width, (h - 16) / height))
    if state.get("screen") not in ("WorkshopAiScreen", "WorkshopAiLibraryScreen"):
        ox, oy, zoom = 0, 0, 1
    else:
        ox = max(0, w - 8 - m.floor(width * zoom + 0.5))
        oy = max(0, (h - m.floor(height * zoom + 0.5)) // 2)
    return bot(
        f"uiclick {ox + (widget['x'] + widget['width'] * fx) * zoom} "
        f"{oy + (widget['y'] + widget['height'] * fy) * zoom}"
    )


def wait_stage(want, timeout, poll=5, abort=None, terminal=("cancelled",)):
    end = time.monotonic() + timeout
    state = None
    while time.monotonic() < end:
        if abort is not None and abort.is_set():
            raise RuntimeError("memory guard aborted the stand")
        state = bot("workshopai state")
        if state.get("stage") in want or state.get("stage") in terminal:
            return state
        time.sleep(poll)
    raise TimeoutError(f"stage {state} not in {want}")


def wait_idle(timeout, abort=None):
    # NOTE: bare "workshopai state" reads the client cache, which goes stale
    # while idle (the server only pushes during active jobs). "status" forces
    # a server roundtrip, so this gate sees the real stage.
    end = time.monotonic() + timeout
    state = None
    while time.monotonic() < end:
        if abort is not None and abort.is_set():
            raise RuntimeError("memory guard aborted the stand")
        state = bot("workshopai status")
        if state.get("stage") == "idle":
            return state
        time.sleep(5)
    raise TimeoutError(f"stage {state} not idle")


def enter_map(short, full, tries=4, timeout=240):
    bot(f"chat /bf workshop map {short}")
    end = time.monotonic() + timeout
    sent = 1
    while time.monotonic() < end:
        state = bot("state")
        if (
            isinstance(state, dict)
            and state.get("connected")
            and state.get("dimension") == full
        ):
            return
        if sent < tries and time.monotonic() > end - timeout + sent * 30:
            bot(f"chat /bf workshop map {short}")
            sent += 1
        time.sleep(5)
    raise RuntimeError(f"never entered {full}")


def hide_hud(hide):
    del hide
    bot("rawinput key F1")
    time.sleep(0.5)


def server_log_lines(stand):
    # The console can omit the usage logger; the file log retains paid receipts.
    path = stand / "server" / "logs" / "latest.log"
    if not path.is_file():
        path = stand / "server" / "server.out"
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def in_window(stamp, lo, hi):
    if stamp is None:
        return False
    if lo <= hi:
        return lo <= stamp <= hi
    return stamp >= lo or stamp <= hi


def hms_of(wall):
    return int((wall + UTC3.utcoffset(None).total_seconds()) % 86400)


def collect_metrics(lines, job_id, t0_wall, t1_wall):
    lo, hi = hms_of(t0_wall) - 15, hms_of(t1_wall) + 15
    window = [line for line in lines if in_window(log_stamp(line), lo, hi)]
    calls, usages, failures, strategies, etas, quality = [], [], [], [], [], []
    for line in window:
        if "AI call phase=" in line:
            parsed = parse_ai_call(line)
            if parsed:
                parsed["stamp"] = log_stamp(line)
                calls.append(parsed)
        elif "AI usage {" in line:
            receipt = parse_ai_usage(line)
            if receipt:
                receipt["stamp"] = log_stamp(line)
                usages.append(receipt)
        elif "AI strategy " in line:
            strategies.append(line[-400:])
        elif "AI eta job=" in line:
            etas.append(line[-300:])
        elif "AI quality job=" in line:
            parsed = parse_ai_quality(line)
            if parsed:
                quality.append(parsed)
        for pattern in AI_FAILURE_RES:
            if pattern.search(line):
                failures.append(line[-400:])
                break
    by_phase = {}
    for call in calls:
        by_phase.setdefault(call["phase"], []).append(call)
    receipts_by_phase = {}
    for receipt in usages:
        receipts_by_phase.setdefault(receipt.get("phase"), []).append(receipt)
    latency_by_phase = {}
    for phase, items in by_phase.items():
        receipts = receipts_by_phase.get(phase, [])
        latencies = []
        for call, receipt in zip(items, receipts):
            latency = phase_latency(call.get("stamp"), receipt.get("stamp"))
            if latency is not None:
                latencies.append(latency)
        if latencies:
            latency_by_phase[phase] = latencies
    cost_by_phase, tokens_by_phase = {}, {}
    for receipt in usages:
        phase = receipt.get("phase")
        cost_by_phase[phase] = cost_by_phase.get(phase, 0.0) + float(
            receipt.get("costUsd") or 0
        )
        sums = tokens_by_phase.setdefault(phase, {})
        # "in" includes "cached"; the provider bills cached input at its cache rate.
        for key, field in (
            ("in", "inputTokens"),
            ("cached", "cachedTokens"),
            ("out", "outputTokens"),
            ("reasoning", "reasoningTokens"),
        ):
            sums[key] = sums.get(key, 0) + int(receipt.get(field) or 0)
    token_totals = {
        key: sum(t[key] for t in tokens_by_phase.values())
        for key in ("in", "cached", "out", "reasoning")
    }
    return {
        "calls": calls,
        "callsByPhase": {phase: len(items) for phase, items in by_phase.items()},
        "latencyByPhaseS": latency_by_phase,
        "rateLimitHits": rate_limit_hits(window),
        "receipts": [
            {
                "phase": r.get("phase"),
                "in": r.get("inputTokens"),
                "out": r.get("outputTokens"),
                "reasoning": r.get("reasoningTokens"),
                "cached": r.get("cachedTokens"),
                "costUsd": r.get("costUsd"),
                "finishReason": r.get("finishReason"),
                "callSlot": r.get("callSlot"),
            }
            for r in usages
        ],
        "costByPhase": cost_by_phase,
        "tokensByPhase": tokens_by_phase,
        "tokens": token_totals,
        "totalCostUsd": round(sum(cost_by_phase.values()), 6),
        "failures": failures[:20],
        "strategy": strategies[-3:],
        "eta": etas[-3:],
        "quality": quality[-3:],
        "logExcerpt": [line[-300:] for line in window][-40:],
    }


def run_job(job, index, stand, out_dir, opts, abort, state):
    size = job["size"]
    if job.get("origin"):
        origin = list(job["origin"])
        variance = 0
    else:
        gx, _, gz = state["grid"](index)
        gy, variance = plot_ground(gx, gz, size)
        origin = [gx, gy + 1, gz]
    (x1, y1, z1), (x2, y2, z2) = select_corners(origin, size)
    confirmed = confirmed_microusd(stand)
    if not spend_guard_ok(confirmed, opts["est_job"], opts["spend_cap"]):
        return {
            "index": index,
            "status": "skipped-spend-guard",
            "confirmedMicrousd": confirmed,
        }
    set_config_model(stand, job["model"])
    set_mode(job["mode"])
    bot(f"chat //pos1 {x1},{y1},{z1}")
    time.sleep(1)
    bot(f"chat //pos2 {x2},{y2},{z2}")
    time.sleep(2)
    record = {
        "index": index,
        "prompt": job["prompt"],
        "model": job["model"],
        "mode": job["mode"],
        "selection": {"origin": origin, "size": size, "groundVariance": variance},
        "startLocal": datetime.datetime.now(UTC3).isoformat(timespec="seconds"),
        "confirmedMicrousdBefore": confirmed,
    }
    slug = (
        re.sub(r"[^a-z0-9]+", "-", job["prompt"].lower())[:24].strip("-")
        or f"job{index}"
    )
    tag = f"{index:02d}-{slug}-{short_model(job['model'])}-{job['mode']}"
    try:
        pre = wait_idle(180, abort=abort)
    except TimeoutError as exc:
        record.update(
            {"status": "harness-error", "error": f"stand not idle: {exc}"[:200]}
        )
        return record
    if pre.get("jobId") not in (None, "00000000-0000-0000-0000-000000000000"):
        record["previousJobId"] = pre.get("jobId")
    rate_limit = {"retries": 0, "hits": 0}
    attempt = 0
    while True:
        bot("workshopai draft " + job["prompt"])
        log_mark = len(server_log_lines(stand))
        t0 = time.time()
        response = bot("workshopai generate")
        resends = 0
        while (
            isinstance(response, dict)
            and not response.get("sent", True)
            and resends < 10
        ):
            # The idle gate polls via `status`, which sends and arms the client's
            # 250 ms action spacing; a generate in the same window is dropped
            # client-side (sent=false), so wait out the spacing and resend.
            time.sleep(0.5)
            response = bot("workshopai generate")
            resends += 1
        if isinstance(response, dict) and not response.get("sent", True):
            bot("workshopai cancel")
            record.update(
                {
                    "status": "no-worker-start",
                    "submitToPreviewS": round(time.time() - t0, 1),
                    "error": "generate not sent (client throttle)",
                }
            )
            record["rateLimit"] = dict(rate_limit)
            return record
        try:
            started = wait_stage(("planning", "preview", "error"), 120, abort=abort)
        except TimeoutError:
            bot("workshopai cancel")
            record.update(
                {
                    "status": "no-worker-start",
                    "submitToPreviewS": round(time.time() - t0, 1),
                }
            )
            record["rateLimit"] = dict(rate_limit)
            return record
        if started.get("stage") not in ("planning", "preview", "error"):
            bot("workshopai cancel")
            record.update(
                {
                    "status": "no-worker-start",
                    "submitToPreviewS": round(time.time() - t0, 1),
                }
            )
            record["rateLimit"] = dict(rate_limit)
            return record
        try:
            final = wait_stage(
                ("preview", "error"), opts["job_timeout"](job), poll=1, abort=abort
            )
        except TimeoutError:
            bot("workshopai cancel")
            record.update(
                {"status": "timeout", "submitToPreviewS": round(time.time() - t0, 1)}
            )
            try:
                record["postCancelStage"] = wait_idle(120, abort=abort).get("stage")
            except TimeoutError:
                record["postCancelStage"] = "stuck"
            record["rateLimit"] = dict(rate_limit)
            return record
        if final.get("stage") == "cancelled":
            record.update(
                {
                    "status": "error-cancelled",
                    "submitToPreviewS": round(time.time() - t0, 1),
                    "message": str(final.get("message") or "")[:500],
                }
            )
            record["metrics"] = collect_metrics(
                server_log_lines(stand)[log_mark:], record.get("jobId"), t0, time.time()
            )
            record["rateLimit"] = dict(rate_limit)
            return record
        submit_to_preview = round(time.time() - t0, 1)
        record["submitToPreviewS"] = submit_to_preview
        record["jobId"] = final.get("jobId")
        fresh = server_log_lines(stand)[log_mark:]
        record["selectionVolumeSeen"] = parse_strategy_volume(fresh)
        expected_volume = size[0] * size[1] * size[2]
        if record["selectionVolumeSeen"] not in (None, expected_volume):
            bot("workshopai cancel")
            record.update(
                {
                    "status": "selection-mismatch",
                    "error": f"strategy volume {record['selectionVolumeSeen']} != selection {expected_volume}",
                }
            )
            record["rateLimit"] = dict(rate_limit)
            return record
        record["modeSeen"] = parse_generation_mode(fresh)
        if record["modeSeen"] is not None and record["modeSeen"] != job["mode"]:
            record["modeMismatch"] = True
        if final.get("stage") == "error" or final.get("error"):
            metrics = collect_metrics(
                server_log_lines(stand)[log_mark:], record.get("jobId"), t0, time.time()
            )
            rate_hits = rate_limit_hits(fresh)
            rate_limit["hits"] += rate_hits
            status = classify_error(metrics["failures"], rate_hits)
            if status == "rate_limited" and attempt < RATE_LIMIT_MAX_RETRIES:
                # Bounded resubmission: back off, cancel, and redraft once more.
                wait_s = rate_limit_backoff_s(attempt)
                bot("workshopai cancel")
                time.sleep(wait_s)
                wait_idle(180, abort=abort)
                attempt += 1
                rate_limit["retries"] = attempt
                continue
            record.update(
                {
                    "status": status,
                    "error": str(
                        final.get("error")
                        or final.get("errorCode")
                        or final.get("stage")
                    ),
                    "message": str(final.get("message") or "")[:500],
                }
            )
            record["rateLimit"] = dict(rate_limit)
            if status == "error":
                snap_panel(out_dir / f"{tag}-error.jpg")
            record["metrics"] = metrics
            return record
        record["rateLimit"] = dict(rate_limit)
        break
    record["metrics"] = collect_metrics(
        server_log_lines(stand)[log_mark:], record.get("jobId"), t0, time.time()
    )
    try:
        blueprint = bot_full("workshopai blueprint", timeout=60)
        (out_dir / f"{tag}-blueprint.json").write_text(
            json.dumps(blueprint, ensure_ascii=False), encoding="utf-8"
        )
        stats = plan_stats(blueprint.get("plan", {}))
    except Exception as exc:
        stats = {"error": str(exc)[:200]}
    stats["footprintVsSelection"] = footprint_vs_selection(stats, size)
    record["plan"] = stats
    t_apply = time.time()
    bot("workshopai apply")
    try:
        wait_stage(("complete", "error"), opts["apply_timeout"], abort=abort)
        apply_s = round(time.time() - t_apply, 1)
        record["applyS"] = apply_s
        record["blocksPerSecond"] = apply_rate(stats.get("blocksPlaced", 0), apply_s)
        record["status"] = "preview"
        if bot("workshopai state").get("stage") == "error":
            record["status"] = "apply-error"
    except TimeoutError:
        record.update(
            {"status": "apply-timeout", "applyS": round(time.time() - t_apply, 1)}
        )
        return record
    hide_hud(True)
    try:
        for shot in ("a", "b", "c"):
            pos, look = camera_for_plot(origin, size, shot)
            rcon(
                f"execute in {opts['map']} run tp {opts['player']} "
                f"{pos[0]:.1f} {pos[1]:.1f} {pos[2]:.1f} 0 0"
            )
            time.sleep(1.0)
            bot(f"lookat {look[0]:.1f} {look[1]:.1f} {look[2]:.1f}")
            time.sleep(0.5)
            snap(f"wsai-{tag}-{shot}", out_dir / f"{tag}-{shot}.jpg")
    finally:
        hide_hud(False)
    bot("workshopai undo")
    time.sleep(5)
    undo_end = time.monotonic() + 600
    last = None
    while time.monotonic() < undo_end:
        if abort is not None and abort.is_set():
            raise RuntimeError("memory guard aborted the stand")
        last = bot("workshopai state").get("stage")
        if last != "undoing":
            break
        time.sleep(5)
    else:
        record["undoTimeout"] = True
    record["undoEndStage"] = last
    return record


def short_model(model):
    low = model.lower()
    if "muse-spark" in low or "muse" in low:
        return "muse"
    if "haiku" in low:
        return "haiku"
    return re.sub(r"[^a-z0-9]+", "", low.split("/")[-1].lower())[:10]


def set_config_model(stand, model):
    path = stand / "server" / "config" / "blockfield" / "workshop-ai.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("generationModel") != model:
        config["generationModel"] = model
        path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        time.sleep(6)


def snap_panel(path):
    try:
        bot("screen workshopai")
        time.sleep(1.0)
        snap("wsai-panel", path)
    finally:
        try:
            bot("closescreen")
        except Exception:
            pass


CSV_COLUMNS = [
    "index",
    "status",
    "prompt",
    "model",
    "mode",
    "origin",
    "size",
    "startLocal",
    "submitToPreviewS",
    "applyS",
    "blocksPerSecond",
    "inputTokens",
    "cachedTokens",
    "outputTokens",
    "reasoningTokens",
    "costUsd",
    "totalCostUsd",
    "calls",
    "phaseTimingsS",
    "failures",
    "retries",
    "blocksPlaced",
    "distinctBlocks",
    "footprintVsSelection",
    "jobId",
    "error",
]


def flatten(record):
    metrics = record.get("metrics", {})
    plan = record.get("plan", {})
    tokens = metrics.get("tokens", {}) if isinstance(metrics, dict) else {}
    failures = metrics.get("failures", []) if isinstance(metrics, dict) else []
    top = (
        ";".join(f"{b['block']}:{b['cells']}" for b in plan.get("topBlocks", []))
        if isinstance(plan, dict)
        else ""
    )
    return {
        "index": record.get("index"),
        "status": record.get("status"),
        "prompt": record.get("prompt"),
        "model": record.get("model"),
        "mode": record.get("mode"),
        "origin": record.get("selection", {}).get("origin"),
        "size": record.get("selection", {}).get("size"),
        "startLocal": record.get("startLocal"),
        "submitToPreviewS": record.get("submitToPreviewS"),
        "applyS": record.get("applyS"),
        "blocksPerSecond": record.get("blocksPerSecond"),
        "inputTokens": tokens.get("in"),
        "cachedTokens": tokens.get("cached"),
        "outputTokens": tokens.get("out"),
        "reasoningTokens": tokens.get("reasoning"),
        "costUsd": metrics.get("totalCostUsd"),
        "totalCostUsd": metrics.get("totalCostUsd"),
        "calls": json.dumps(metrics.get("callsByPhase", {}), ensure_ascii=False),
        "phaseTimingsS": json.dumps(
            metrics.get("latencyByPhaseS", {}), ensure_ascii=False
        ),
        "failures": " | ".join(failures[:3])[:600],
        "retries": len(failures),
        "blocksPlaced": plan.get("blocksPlaced") if isinstance(plan, dict) else None,
        "distinctBlocks": plan.get("distinctBlocks")
        if isinstance(plan, dict)
        else None,
        "topBlocks": top,
        "footprintVsSelection": plan.get("footprintVsSelection")
        if isinstance(plan, dict)
        else None,
        "jobId": record.get("jobId"),
        "error": record.get("error"),
    }


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def summarize_by_model(records):
    """Per-model totals: median submit->preview wall, median cost, success rate."""
    groups = {}
    for record in records:
        groups.setdefault(record.get("model"), []).append(record)
    summary = {}
    for model, items in groups.items():
        walls = [
            r["submitToPreviewS"]
            for r in items
            if isinstance(r.get("submitToPreviewS"), (int, float))
        ]
        costs = [
            r.get("metrics", {}).get("totalCostUsd")
            for r in items
            if isinstance(r.get("metrics", {}).get("totalCostUsd"), (int, float))
        ]
        ok = sum(1 for r in items if r.get("status") == "preview")
        summary[model] = {
            "jobs": len(items),
            "ok": ok,
            "rateLimited": sum(1 for r in items if r.get("status") == "rate_limited"),
            "successRate": round(ok / len(items), 3) if items else 0,
            "medianWallS": _median(walls),
            "medianCostUsd": _median(costs),
            "totalCostUsd": round(sum(costs), 6),
        }
    return summary


def spend_per_model(records):
    out = {}
    for record in records:
        cost = record.get("metrics", {}).get("totalCostUsd")
        if isinstance(cost, (int, float)):
            out[record.get("model")] = round(
                out.get(record.get("model"), 0.0) + cost, 6
            )
    return out


def save_results(out_dir, records):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (out_dir / "results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=[*CSV_COLUMNS, "topBlocks"])
        writer.writeheader()
        for record in records:
            row = flatten(record)
            writer.writerow({key: row.get(key) for key in [*CSV_COLUMNS, "topBlocks"]})
    (out_dir / "summary.json").write_text(
        json.dumps(
            {"byModel": summarize_by_model(records)}, indent=2, ensure_ascii=False
        ),
        encoding="utf-8",
    )


PAUSE_FILENAME = "pause.json"


def should_pause(confirmed_microusd, cap_microusd):
    return confirmed_microusd >= cap_microusd


def write_pause_state(out_dir, records, remaining, confirmed, cap, reason):
    state = {
        "paused": True,
        "reason": reason,
        "atLocal": datetime.datetime.now(UTC3).isoformat(timespec="seconds"),
        "spendCapMicrousd": cap,
        "confirmedMicrousd": confirmed,
        "spendPerModelUsd": spend_per_model(records),
        "completed": [r.get("index") for r in records],
        "remaining": list(remaining),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PAUSE_FILENAME).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return state


def load_completed_indices(out_dir):
    """Indices already run in an earlier batch: resume skips exactly these."""
    path = out_dir / "results.json"
    if not path.is_file():
        return set()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return set()
    return {
        r.get("index")
        for r in records
        if isinstance(r, dict) and isinstance(r.get("index"), int)
    }


def cmd_build(args):
    src = Path(args.src)
    ref = args.ref
    if ref:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=src,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
        if dirty:
            raise SystemExit(f"{src} has local changes; use a clean worktree for --ref")
        subprocess.run(["git", "fetch", "origin"], cwd=src, check=True, timeout=300)
        subprocess.run(
            ["git", "checkout", "--detach", ref], cwd=src, check=True, timeout=120
        )
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JDK)
    env["PATH"] = str(JDK / "bin") + os.pathsep + env.get("PATH", "")
    slot = [str(GRADLE_SLOT), "--"] if not args.no_slot else []
    subprocess.run(
        [
            *slot,
            f"{src}/gradlew",
            "--no-daemon",
            "--max-workers=2",
            "-Dorg.gradle.jvmargs=-Xmx2G -XX:MaxMetaspaceSize=512m",
            ":client:remapJar",
            ":server:remapJar",
        ],
        cwd=src,
        env=env,
        check=True,
        timeout=2400,
    )
    jars = {}
    for side in ("client", "server"):
        found = [
            p
            for p in (src / side / "build" / "libs").glob("*.jar")
            if not any(x in p.name for x in ("sources", "javadoc", "-dev"))
        ]
        assert len(found) == 1, (side, found)
        jars[side] = str(found[0])
    print(json.dumps(jars, indent=2))


def cmd_run(args):
    if os.environ.get("WSAI_BENCH_HELD") != "1":
        slot = [str(GRADLE_SLOT), "--exclusive", "--"]
        env = dict(os.environ, WSAI_BENCH_HELD="1")
        proc = subprocess.run(
            [*slot, sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=ROOT,
            env=env,
        )
        raise SystemExit(proc.returncode)
    matrix = load_matrix(args.matrix)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stand_name = args.stand
    key = read_key(args.key_file)
    memory_gate()
    wait_ports_free()
    overrides = {"generationModel": matrix[0]["model"]}
    if args.reuse_stand:
        stand = DEV / stand_name
        if not stand.is_dir():
            raise SystemExit(f"stand {stand} does not exist for --reuse-stand")
        stand, installed = configure_stand(
            stand,
            Path(args.client_jar),
            Path(args.server_jar),
            overrides,
            args.session_limit,
            args.palette == "on",
            reset_usage=args.reset_usage,
        )
    else:
        stand, installed = prepare_stand(
            stand_name,
            Path(args.client_jar),
            Path(args.server_jar),
            overrides,
            args.session_limit,
            args.palette == "on",
            reset_usage=True,
        )
    (out_dir / "installed-jars.json").write_text(
        json.dumps(installed, indent=2), encoding="utf-8"
    )
    os.environ["BF_DEV_HOME"] = str(stand)
    os.environ["WSAI_BENCH_CALLS"] = str(out_dir / "calls.jsonl")
    stop = threading.Event()
    abort = threading.Event()
    guard_result = {}
    guard = threading.Thread(
        target=guard_loop, args=(stop, abort, guard_result), daemon=True
    )
    records = []
    try:
        start_server(stand, key)
        wait_ai_enabled(stand)
        start_client(args.player, args.resolution)
        ready_state = wait_ready()
        (out_dir / "ready.json").write_text(
            json.dumps(ready_state, indent=2)[:4000], encoding="utf-8"
        )
        guard.start()
        enter_map(args.map_short, args.map)
        if args.forceload:
            area = args.forceload.replace(",", " ")
            rcon(f"execute in {args.map} run forceload add {area}")
            time.sleep(5)
        base = [int(v) for v in args.base.split(",")]
        if args.ground_auto:
            grid = lambda index: grid_origin(index, base, args.stride, args.cols)  # noqa: E731
        else:
            grid = lambda index: [  # noqa: E731
                grid_origin(index, base, args.stride, args.cols)[0],
                args.ground_y,
                grid_origin(index, base, args.stride, args.cols)[2],
            ]
        grid_state = {"grid": grid}
        # The reserve covers the priciest matrix model (per-model tariff table).
        reserve = reserve_tariff([job["model"] for job in matrix])
        opts = {
            "est_job": max(args.est_job, generation_bound_microusd(tariff=reserve)),
            "spend_cap": args.spend_cap,
            "job_timeout": lambda job: (
                args.quality_timeout if job["mode"] == "quality" else args.fast_timeout
            ),  # noqa: E731
            "apply_timeout": args.apply_timeout,
            "map": args.map,
            "player": args.player,
        }
        loop_end = time.monotonic() + args.loop_seconds
        done = set()
        if args.resume:
            done = load_completed_indices(out_dir)
            if done:
                records = json.loads(
                    (out_dir / "results.json").read_text(encoding="utf-8")
                )
                print(f"resume: keeping {len(records)} completed jobs", flush=True)
        for index, job in enumerate(matrix):
            if index in done:
                continue
            if time.monotonic() >= loop_end:
                records.append({"index": index, "status": "skipped-loop-deadline"})
                continue
            if abort.is_set():
                records.append({"index": index, "status": "skipped-memory-abort"})
                break
            if should_pause(confirmed_microusd(stand), args.spend_cap):
                confirmed = confirmed_microusd(stand)
                remaining = [
                    i
                    for i in range(len(matrix))
                    if i not in {r.get("index") for r in records}
                ]
                write_pause_state(
                    out_dir,
                    records,
                    remaining,
                    confirmed,
                    args.spend_cap,
                    "cap-reached",
                )
                print(
                    f"PAUSED at ${confirmed / 1_000_000:.4f}: "
                    f"{len(records)} done, {len(remaining)} remaining; "
                    f"rerun with --resume to continue",
                    flush=True,
                )
                break
            try:
                record = run_job(job, index, stand, out_dir, opts, abort, grid_state)
            except AssertionError as exc:
                record = {
                    "index": index,
                    "status": "harness-error",
                    "error": str(exc)[:300],
                }
            except Exception as exc:
                record = {
                    "index": index,
                    "status": "harness-error",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            records.append(record)
            save_results(out_dir, records)
            if record.get("status") == "skipped-spend-guard":
                # Never mark the unstarted job completed: pause without appending
                # it, so --resume runs it.
                records.pop()
                save_results(out_dir, records)
                confirmed = record.get("confirmedMicrousd", confirmed_microusd(stand))
                remaining = [
                    i
                    for i in range(len(matrix))
                    if i not in {r.get("index") for r in records}
                ]
                write_pause_state(
                    out_dir,
                    records,
                    remaining,
                    confirmed,
                    args.spend_cap,
                    "spend-guard",
                )
                print(
                    f"PAUSED at ${confirmed / 1_000_000:.4f}: "
                    f"{len(records)} done, {len(remaining)} remaining; "
                    f"rerun with --resume to continue",
                    flush=True,
                )
                break
    finally:
        stop.set()
        (out_dir / "guard.json").write_text(
            json.dumps(guard_result, indent=2), encoding="utf-8"
        )
        if args.forceload:
            try:
                rcon(f"execute in {args.map} run forceload remove all")
            except Exception:
                pass
        if not args.no_cleanup:
            cleanup_stand(stand_name)
        else:
            stop_all()
    save_results(out_dir, records)


def cmd_cleanup(args):
    os.environ["BF_DEV_HOME"] = str(DEV / args.stand)
    cleanup_stand(args.stand)


def build_parser():
    parser = argparse.ArgumentParser(description="Workshop AI benchmark harness")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build client+server jars from a mod checkout")
    build.add_argument("--src", required=True)
    build.add_argument("--ref", default=None)
    build.add_argument("--no-slot", action="store_true")
    run = sub.add_parser("run", help="full stand session under one exclusive slot")
    run.add_argument("--stand", required=True)
    run.add_argument("--matrix", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--client-jar", required=True)
    run.add_argument("--server-jar", required=True)
    run.add_argument("--key-file", default=str(SECRETS_KEY))
    run.add_argument("--session-limit", type=int, default=3_000_000)
    run.add_argument("--palette", choices=["on", "off"], default="off")
    run.add_argument("--spend-cap", type=int, default=2_700_000)
    run.add_argument("--est-job", type=int, default=EST_JOB_MICROUSD)
    run.add_argument("--map", default="blockfield_workshop:ancient")
    run.add_argument("--map-short", default="ancient")
    run.add_argument("--player", default="WorkshopAccept")
    run.add_argument("--resolution", default="1920x1080")
    run.add_argument("--base", default="0,0")
    run.add_argument("--stride", type=int, default=160)
    run.add_argument("--cols", type=int, default=4)
    run.add_argument("--ground-y", type=int, default=100)
    run.add_argument("--ground-auto", action="store_true")
    run.add_argument("--reuse-stand", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.add_argument(
        "--reset-usage",
        action="store_true",
        help="with --reuse-stand: zero the spend ledger (default keeps it across batches)",
    )
    run.add_argument("--forceload", default=None)
    run.add_argument("--fast-timeout", type=int, default=900)
    run.add_argument("--quality-timeout", type=int, default=1500)
    run.add_argument("--apply-timeout", type=int, default=1800)
    run.add_argument("--loop-seconds", type=int, default=14400)
    run.add_argument("--no-cleanup", action="store_true")
    clean = sub.add_parser("cleanup", help="stop processes and remove a stand")
    clean.add_argument("--stand", required=True)
    return parser


def main():
    args = build_parser().parse_args()
    if args.command == "build":
        cmd_build(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)


if __name__ == "__main__":
    main()
