#!/usr/bin/env python3
"""Opt-in Async A/B: profile, own-stand staging, effective-config gate, metrics, verdict.

Never downloads, launches servers or touches the shared .dev stand.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[2]
PROFILE = Path(__file__).resolve().parent / "profiles/async-candidate.json"
CANDIDATE_HASH = "142dc48fc3f8d01aa8ba7b98f7177bda91abb1ffb846bc50081663cd04fa1db2"
PROTECTED = {"tacz:*", "lrtactical:*", "superbwarfare:*", "create:*", "fowlplay:*"}
VANILLA = {
    "minecraft:tnt",
    "minecraft:item",
    "minecraft:experience_orb",
    "minecraft:armor_stand",
}
ID = re.compile(r"[a-z0-9_.-]+:[a-z0-9_./-]+")


def prepare(profile, registry=None, jar=None):
    profile = copy.deepcopy(profile)
    if (
        profile.get("schema_version") != 1
        or profile.get("scope") != "offline-candidate-only"
        or profile.get("minecraft") != "1.21.1"
    ):
        raise ValueError(
            "Only the explicitly isolated 1.21.1 candidate profile is supported"
        )
    candidate = profile["candidate"]
    if (
        candidate.get("sha256") != CANDIDATE_HASH
        or candidate.get("version") != "0.2.0+alpha-1.21.1"
        or candidate.get("filename") != "async-fabric-0.2.0+alpha-1.21.1.jar"
        or candidate.get("source_commit") != "560b65f0578a4e809682c12b857c653abb1abf99"
    ):
        raise ValueError(
            "Candidate must match the reviewed immutable artifact and source reference"
        )
    config = profile["config"]
    if set(config) != {
        "disabled",
        "maxThreads",
        "enableAsyncSpawn",
        "enableAsyncRandomTicks",
        "synchronizedEntities",
    }:
        raise ValueError("Unexpected Async configuration keys")
    if (
        config["disabled"] is not False
        or config["enableAsyncSpawn"] is not False
        or config["enableAsyncRandomTicks"] is not False
        or type(config["maxThreads"]) is not int
        or config["maxThreads"] != 2
    ):
        raise ValueError(
            "Candidate requires two workers, synchronous spawn/despawn and random ticks"
        )
    entries = config["synchronizedEntities"]
    if (
        not isinstance(entries, list)
        or any(
            not isinstance(item, str) or not (ID.fullmatch(item) or item in PROTECTED)
            for item in entries
        )
        or not PROTECTED | VANILLA <= set(entries)
    ):
        raise ValueError("Required combat and vanilla exclusions must remain present")
    exact = None
    if registry is not None:
        ids = registry.get("entity_types")
        if (
            registry.get("complete") is not True
            or registry.get("minecraft") != "1.21.1"
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(item, str) or not ID.fullmatch(item) for item in ids)
            or not VANILLA <= set(ids)
        ):
            raise ValueError(
                "Registry must be a complete 1.21.1 entity-type list, not an entity census"
            )
        namespaces = {item[:-2] for item in entries if item.endswith(":*")}
        exact = sorted(
            {item for item in ids if item.split(":", 1)[0] in namespaces}
            | {item for item in entries if not item.endswith(":*")}
        )
        # Keep wildcards so a later registered type cannot silently bypass the safety profile.
    config["synchronizedEntities"] = sorted(set(entries))
    artifact_verified = False
    if jar is not None:
        with jar.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != CANDIDATE_HASH:
            raise ValueError("Async JAR hash does not match reviewed candidate")
        artifact_verified = True
    keys = (
        "disabled",
        "maxThreads",
        "enableAsyncSpawn",
        "enableAsyncRandomTicks",
        "synchronizedEntities",
    )
    toml = "\n".join(f"{key} = {json.dumps(config[key])}" for key in keys) + "\n"
    return {
        "profile": profile,
        "artifact_verified": artifact_verified,
        "registry_resolved_exclusions": exact,
        "config_toml": toml,
        "acceptance_required": [
            "Own isolated stand only; baseline and candidate use identical world/config/JVM/load.",
            "Pre-create TOML before first launch; require 'Configuration loaded.' and effective exclusion verification.",
            "ServerPlayer and Projectile guards do not accelerate weapon/match tick handlers.",
            "Verify rider/passenger ticks, team damage, grenades, vehicles, real ping and repeated map unload.",
            "Measure p50/p95/p99, fixed shot throughput, CPU and GC across paired repetitions; JFR samples are not entity wall time.",
            "No production activation or performance claim until correctness and useful gain are demonstrated.",
        ],
    }


def effective(prepared, variant, mods, toml_text, log_text, stats="", synced=""):
    """Prove what the running server uses; Async drops caches on several silent paths."""
    expected = tomllib.loads(prepared["config_toml"])
    candidate = prepared["profile"]["candidate"]
    jars = sorted(path for path in mods.glob("*.jar") if path.name.startswith("async"))
    if variant == "A":
        if jars or "Initializing Async" in log_text:
            raise ValueError("Variant A must run without Async")
    else:
        if [path.name for path in jars] != [candidate["filename"]]:
            raise ValueError("Variant B needs exactly the reviewed Async JAR")
        with jars[0].open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != CANDIDATE_HASH:
                raise ValueError("Installed Async JAR hash mismatch")
        start = log_text.rfind("Initializing Async Config...")
        boot = log_text[start:] if start >= 0 else ""
        # Defaults/error paths skip onConfigLoaded(): the exclusion caches stay empty.
        if "Configuration loaded." not in boot or any(
            marker in boot
            for marker in (
                "Configuration not found",
                "Error loading configuration",
                "Removed unused config key",
            )
        ):
            raise ValueError("Async did not load the pre-created TOML")
        threads = expected["maxThreads"]
        if f"Initialized Pool with {threads} threads" not in log_text:
            raise ValueError(f"Async pool is not {threads} threads")
        plain = re.sub("\u00a7.", "", stats)
        for line in (
            "Status: Enabled",
            "Async Spawn: Disabled",
            "Async Random Ticks: Disabled",
        ):
            if line not in plain:
                raise ValueError(f"/async stats lacks '{line}'")
        listed = set(re.findall(r"^- (\S+)$", re.sub("\u00a7.", "", synced), re.M))
        if listed != set(expected["synchronizedEntities"]):
            raise ValueError("/async config synchronizedEntities differs from profile")
    actual = tomllib.loads(toml_text)
    actual["synchronizedEntities"] = sorted(set(actual.get("synchronizedEntities", [])))
    if actual != expected:
        raise ValueError("config/async.toml differs from the reviewed profile")
    return {"variant": variant, "async_jar": [path.name for path in jars]}


def sample(record):
    """Process CPU seconds and stop-the-world GC from /proc and the stand's own jstat."""
    pid = record["pid"]
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    output = subprocess.run(
        [str(Path(record["exe"]).with_name("jstat")), "-gc", str(pid)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.split("\n")
    gc = dict(zip(output[0].split(), map(float, output[1].split())))
    return {
        "wall_s": time.monotonic(),
        "cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
        # Young + full pauses; G1 concurrent cycle time is not a pause.
        "gc_stw_ms": (gc["YGCT"] + gc["FGCT"]) * 1000,
        "gc_count": int(gc["YGC"] + gc["FGC"]),
    }


def case_metrics(row, before, after):
    windows = row["windows"]
    if len(windows) < 6:
        raise ValueError("Need at least six 100-tick windows")
    wall = after["wall_s"] - before["wall_s"]
    shots = re.search(r"SUCCESS=(\d+)", row.get("gunner", ""))
    return {
        **{
            f"window_{name}_median_ms": statistics.median(w[index] for w in windows)
            for index, name in ((1, "p50"), (2, "p95"), (3, "p99"))
        },
        "window_p99_max_ms": max(w[3] for w in windows),
        "cpu_percent_one_core": 100 * (after["cpu_s"] - before["cpu_s"]) / wall,
        "gc_stw_ms": after["gc_stw_ms"] - before["gc_stw_ms"],
        "gc_count": after["gc_count"] - before["gc_count"],
        "shots_success": int(shots.group(1)) if shots else None,
        "kills": row["kills"],
        "online": row.get("online"),
    }


METRICS = (
    "window_p50_median_ms",
    "window_p95_median_ms",
    "window_p99_median_ms",
    "window_p99_max_ms",
    "cpu_percent_one_core",
    "gc_stw_ms",
)


def compare(cases, tolerance=0.05):
    """Gain only when B beats every A repetition on a lower-is-better metric."""
    groups = {
        variant: [c for c in cases if c["variant"] == variant] for variant in "AB"
    }
    if min(map(len, groups.values())) < 3:
        raise ValueError("Need at least three repetitions per variant")
    if len({json.dumps(c["load"], sort_keys=True) for c in cases}) != 1:
        raise ValueError("Variants used different load arguments")
    if "--guns" in cases[0]["load"]:
        for c in cases:
            for name in ("shots_success", "online"):
                value = c["metrics"].get(name)
                if type(value) is not int or value <= 0:
                    raise ValueError(
                        f"{c['label']}: gun load requires positive integer {name}"
                    )
    reference = statistics.median(
        c["metrics"]["shots_success"] or 0 for c in groups["A"]
    )
    for c in cases:
        shots, online = c["metrics"]["shots_success"], c["metrics"]["online"]
        if reference and (
            shots is None or abs(shots - reference) > tolerance * reference
        ):
            raise ValueError(f"{c['label']}: shot throughput differs; load not equal")
        if online != groups["A"][0]["metrics"]["online"]:
            raise ValueError(f"{c['label']}: online player count differs")
    result = {}
    for name in METRICS:
        a = [c["metrics"][name] for c in groups["A"]]
        b = [c["metrics"][name] for c in groups["B"]]
        result[name] = {
            "A_median": statistics.median(a),
            "B_median": statistics.median(b),
            "delta_percent": 100 * (statistics.median(b) / statistics.median(a) - 1)
            if statistics.median(a)
            else None,
            "outcome": "better"
            if max(b) < min(a)
            else "worse"
            if min(b) > max(a)
            else "within-spread",
        }
    tails = ("window_p95_median_ms", "window_p99_median_ms")
    gain = any(result[n]["outcome"] == "better" for n in tails) and not any(
        r["outcome"] == "worse" for r in result.values()
    )
    return {
        "metrics": result,
        "verdict": "performance-gain-observed" if gain else "no-demonstrated-gain",
        "limits": "Window percentiles come from 100-tick /tick query samples; correctness and real clients are separate acceptance.",
    }


def stand():
    """Own BF_DEV_HOME stand only; the shared .dev stand belongs to whoever runs it."""
    home = os.environ.get("BF_DEV_HOME")
    if not home or Path(home).resolve() == (ROOT / ".dev").resolve():
        raise ValueError("Set BF_DEV_HOME to your own isolated stand")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import devstack

    return devstack


def stage(prepared, variant, jar):
    devstack = stand()
    if devstack.recorded_pid(devstack.PID_FILE):
        raise ValueError("Stop your stand before staging a variant")
    mods = devstack.SERVER / "mods"
    target = mods / prepared["profile"]["candidate"]["filename"]
    if variant == "B":
        if jar is None or not prepared["artifact_verified"]:
            raise ValueError("Variant B needs --jar with the reviewed checksum")
        shutil.copyfile(jar, target)
    elif target.exists():
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != CANDIDATE_HASH:
                raise ValueError(f"Refusing to remove unknown {target}")
        target.unlink()
    # Same TOML in both variants; it must exist before Async's first boot.
    (devstack.SERVER / "config").mkdir(exist_ok=True)
    (devstack.SERVER / "config/async.toml").write_text(prepared["config_toml"])
    return {"variant": variant, "server": str(devstack.SERVER)}


def run_case(prepared, variant, label, out, stress):
    endpoint_options = ("--rcon-host", "--rcon-port", "--rcon-password")
    if any(
        arg.startswith("--")
        and any(option.startswith(arg.split("=", 1)[0]) for option in endpoint_options)
        for arg in stress
    ):
        # argparse accepts unambiguous prefixes, so checking only full flags is insufficient.
        raise ValueError("Stress cannot override the owned RCON endpoint")
    devstack = stand()
    record = devstack.process_record(devstack.PID_FILE)
    if (
        not record
        or not record.get("live")
        or record.get("owner") != devstack.process_owner()
    ):
        raise ValueError("No live server owned by this session in BF_DEV_HOME")
    port = devstack.RCON_PORT
    if not devstack.listener_owned(record["pid"], port):
        raise ValueError("RCON listener is not owned by the recorded server process")
    server = devstack.SERVER
    texts = ("", "")
    if variant == "B":
        from scenarios.common import connect_rcon

        rcon = connect_rcon(
            host="127.0.0.1", port=port, password=devstack.RCON_PASSWORD
        )
        try:
            if not devstack.listener_owned(
                record["pid"], port, peer_port=rcon.s.getsockname()[1]
            ):
                raise ValueError(
                    "RCON connection is not owned by the recorded server process"
                )
            texts = (
                rcon.cmd("async stats"),
                rcon.cmd("async config synchronizedEntities"),
            )
        finally:
            rcon.s.close()
    verified = effective(
        prepared,
        variant,
        server / "mods",
        (server / "config/async.toml").read_text(),
        devstack.LOG.read_text(errors="replace"),
        *texts,
    )
    before = sample(record)
    if not devstack.listener_owned(record["pid"], port):
        raise ValueError("RCON listener is not owned by the recorded server process")
    output = subprocess.run(
        [
            sys.executable,
            str(ROOT / "bf.py"),
            "scenario",
            "stress",
            "--rcon-host",
            "127.0.0.1",
            "--rcon-port",
            str(port),
            "--rcon-password",
            devstack.RCON_PASSWORD,
            *stress,
        ],
        cwd=ROOT,
        env=dict(os.environ, BF_DEV_HOME=str(devstack.HOME), BF_RCON_PORT=str(port)),
        capture_output=True,
        text=True,
        check=True,
        timeout=900,
    ).stdout
    after = sample(record)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{label}-stress.log").write_text(output)
    row = next(
        json.loads(line)
        for line in reversed(output.splitlines())
        if line.startswith("{") and '"scenario"' in line
    )
    case = {
        "label": label,
        "variant": variant,
        "load": stress,
        "verification": verified,
        "metrics": case_metrics(row, before, after),
        "stress": row,
    }
    (out / f"{label}.json").write_text(json.dumps(case, indent=2) + "\n")
    return case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument(
        "--registry",
        type=Path,
        help="Complete registry JSON: complete, minecraft, entity_types",
    )
    parser.add_argument(
        "--jar",
        type=Path,
        help="Optional read-only candidate JAR for hash verification",
    )
    parser.add_argument(
        "--toml",
        action="store_true",
        help="Print only native config; no file or runtime mutation",
    )
    commands = parser.add_subparsers(dest="command")
    staging = commands.add_parser(
        "stage", help="Write TOML; add (B) or remove (A) the JAR"
    )
    staging.add_argument("variant", choices="AB")
    case = commands.add_parser(
        "case", help="Verify running variant, measure one stress run"
    )
    case.add_argument("variant", choices="AB")
    case.add_argument("label")
    case.add_argument("--out", type=Path, required=True)
    case.add_argument(
        "stress", nargs="+", help="after --: just scenario stress arguments"
    )
    report = commands.add_parser("compare", help="Same-load A/B verdict from case JSON")
    report.add_argument("cases", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        if args.command == "compare":
            result = compare([json.loads(path.read_text()) for path in args.cases])
            print(json.dumps(result, indent=2))
            return
        result = prepare(
            json.loads(args.profile.read_text()),
            json.loads(args.registry.read_text()) if args.registry else None,
            args.jar,
        )
        if args.command == "stage":
            result = stage(result, args.variant, args.jar)
        elif args.command == "case":
            result = run_case(result, args.variant, args.label, args.out, args.stress)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
        parser.exit(1, f"Profile rejected: {error}\n")
    except subprocess.SubprocessError as error:
        parser.exit(1, f"Measurement failed: {error}\n")
    print(
        result["config_toml"]
        if args.toml and not args.command
        else json.dumps(result, indent=2) + "\n",
        end="",
    )


if __name__ == "__main__":
    main()
