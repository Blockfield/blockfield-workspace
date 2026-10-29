#!/usr/bin/env python3
"""just watch: rebuild + hotswap loop over localmods/ sources (stdlib only).

Cycle: source scan under the side's localmods/ dirs -> `gradlew
:side:remapJar` -> diff class bytes against the deployed jar -> `jdb redefine`
over JDWP. Only method bodies survive a stock JVM redefine; anything else
(schema change, new class, mixin) falls back to a restart automatically.

Ports mirror devstack: server JDWP :5005, client :5006 (BOT_PORTS[1] client only).
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOCALMODS = ROOT / "localmods"
SERVER_JDWP = 5005
CLIENT_JDWP = 5006

# jdb prints no marker on success but an error line on failure.
REDEFINE_FAILED = re.compile(
    r"error|exception|not implemented|schema|no class|unable|failed", re.I
)

# Mixin classes are invisible to stock redefine: without JBR swap them via restart.
MIXIN_HINT = re.compile(r"mixin", re.I)


class WatchError(RuntimeError):
    pass


def side_dirs(side):
    """localmods/ dirs owning code for a side: blockfield-mod only in the first iteration
    (forks change rarely; rebuild them with `just build-mods`)."""
    from modmap import load_mapping, metafiles

    mapping = load_mapping()
    entry = mapping.get("blockfield-mod")
    if entry and (LOCALMODS / "blockfield-mod").is_dir() and metafiles(entry, side):
        return [LOCALMODS / "blockfield-mod"]
    return []


def source_roots(side):
    """*/src trees to watch for a side (blockfield-mod: side + common)."""
    roots = []
    for directory in side_dirs(side):
        candidates = [
            directory / "src",
            directory / side / "src",
            directory / "common" / "src",
        ]
        roots.extend(p for p in candidates if p.is_dir())
    return roots


def newest_source_mtime(side):
    """Max mtime under the side's source roots, or None when nothing is checked out."""
    latest = None
    for root in source_roots(side):
        for path in root.rglob("*.java"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if latest is None or mtime > latest:
                latest = mtime
    return latest


def source_fingerprints(side):
    supported, unsupported = {}, {}
    for directory in side_dirs(side):
        for root in (
            directory / "src",
            directory / side / "src",
            directory / "common" / "src",
        ):
            if root.is_dir():
                for path in root.rglob("*"):
                    if path.is_file():
                        files = supported if path.suffix == ".java" else unsupported
                        files[str(path.resolve())] = hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
        for relative in (
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradle.properties",
            "gradlew",
            "gradlew.bat",
            "common/build.gradle",
            "common/build.gradle.kts",
            f"{side}/build.gradle",
            f"{side}/build.gradle.kts",
        ):
            path = directory / relative
            if path.is_file():
                unsupported[str(path.resolve())] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        for path in (directory / "gradle").rglob("*"):
            if path.is_file():
                unsupported[str(path.resolve())] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return tuple(
        hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        for files in (supported, unsupported)
    )


def jar_fingerprint(path):
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def read_receipt(path):
    if path is None:
        return {}
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
        if receipt.get("schema") == 1 and isinstance(receipt.get("sides"), dict):
            return {
                side: value
                for side, value in receipt["sides"].items()
                if isinstance(value, dict)
                and all(
                    isinstance(value.get(key), str)
                    and re.fullmatch(r"[0-9a-f]{64}", value[key])
                    for key in ("source", "unsupported")
                )
                and isinstance(value.get("jar"), dict)
                and isinstance(value["jar"].get("path"), str)
                and isinstance(value["jar"].get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", value["jar"]["sha256"])
                and "identity" in value
            }
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def write_receipt(path, sides):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".bf-watch-receipt-", delete=False
    ) as file:
        temporary = Path(file.name)
    try:
        temporary.write_text(
            json.dumps({"schema": 1, "sides": sides}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def deployed_jar(side, server_mods, game_dir):
    """Jar currently loaded in the JVM: server mods/ or client game dir."""
    base = server_mods if side == "server" else Path(game_dir) / "mods"
    jars = sorted(
        base.glob(f"blockfield-{side}-*.jar"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return jars[0] if jars else None


def deploy_built_jar(side, built_jar, server_mods, game_dir):
    """Copy the rebuilt jar into the active mods dir so disk matches JVM memory."""
    target_dir = server_mods if side == "server" else Path(game_dir) / "mods"
    if not target_dir.is_dir():
        return
    current = deployed_jar(side, server_mods, game_dir)
    target = target_dir / (current.name if current else built_jar.name)
    with tempfile.NamedTemporaryFile(
        dir=target_dir, prefix=".bf-watch-", delete=False
    ) as file:
        temporary = Path(file.name)
    try:
        shutil.copy2(built_jar, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    for old in target_dir.glob(f"blockfield-{side}-*.jar"):
        if old != target:
            old.unlink(missing_ok=True)


def remap_jar(mod_dir, side, java_home=None):
    """`gradlew :side:remapJar` in a submodule; returns the built jar or None."""
    wrapper = mod_dir / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
    task = f":{side}:remapJar"
    cmd = (
        [str(wrapper), task] if sys.platform == "win32" else ["sh", str(wrapper), task]
    )
    cmd += ["-Dorg.gradle.daemon.idletimeout=600000", "--max-workers=2"]
    env = dict(os.environ)
    if java_home:
        env["JAVA_HOME"] = java_home
        env["PATH"] = str(Path(java_home) / "bin") + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        cmd, cwd=mod_dir, capture_output=True, text=True, env=env, timeout=600
    )
    if result.returncode != 0:
        print(result.stdout[-2000:] + result.stderr[-2000:])
        return None
    jars = [
        p
        for p in (mod_dir / side / "build" / "libs").glob(f"blockfield-{side}-*.jar")
        if "sources" not in p.name and "dev" not in p.name
    ]
    jars += [
        p
        for p in (mod_dir / "build" / "libs").glob(f"blockfield-{side}-*.jar")
        if "sources" not in p.name and "dev" not in p.name
    ]
    return max(jars, key=lambda p: p.stat().st_mtime, default=None)


def class_entries(jar):
    """{class path: bytes} for a jar (all packages, not just ours)."""
    with zipfile.ZipFile(jar) as archive:
        return {
            name: archive.read(name)
            for name in archive.namelist()
            if name.endswith(".class")
        }


def changed_classes(old_jar, new_jar):
    """[(dotted name, class path)] whose bytes differ, including removals."""
    old = class_entries(old_jar)
    new = class_entries(new_jar)
    return [
        (name[: -len(".class")].replace("/", "."), name)
        for name in sorted(old.keys() | new.keys())
        if old.get(name) != new.get(name)
    ]


def jdb_path(java_home=None):
    """jdb next to the running java (JAVA_HOME wins, else PATH)."""
    home = java_home or os.environ.get("JAVA_HOME")
    if home:
        jdb = Path(home) / "bin" / ("jdb.exe" if sys.platform == "win32" else "jdb")
        if jdb.is_file():
            return str(jdb)
    from devstack import java_exe

    try:
        j = java_exe()
        if j:
            candidate = Path(j).parent / (
                "jdb.exe" if sys.platform == "win32" else "jdb"
            )
            if candidate.is_file():
                return str(candidate)
    except Exception:
        pass
    return shutil.which("jdb") or "jdb"


def redefine(port, changes, new_jar, java_home=None):
    """jdb redefine of changed classes; (ok, output). New files are read from the jar."""
    tmp = Path(tempfile.mkdtemp(prefix="bf-watch-"))
    try:
        with zipfile.ZipFile(new_jar) as archive:
            paths = {}
            for dotted, name in changes:
                try:
                    data = archive.read(name)
                except KeyError:
                    return (
                        False,
                        f"new class {dotted}: stock redefine cannot add classes, restart",
                    )
                target = tmp / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                paths[dotted] = target
        script = (
            "".join(
                f"redefine {dotted} {paths[dotted].relative_to(tmp).as_posix()}\n"
                for dotted, _ in changes
            )
            + "quit\n"
        )
        proc = subprocess.run(
            [jdb_path(java_home), "-attach", f"127.0.0.1:{port}"],
            input=script,
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=120,
        )
        combined = f"{proc.stdout}\n{proc.stderr}"
        lines = [re.sub(r"^\s*>\s*", "", line) for line in combined.splitlines()]
        body = "\n".join(
            line
            for line in lines
            if line.strip()
            and not line.startswith(
                ("Initializing jdb", "Set uncaught", "Set deferred")
            )
        )
        failed = (
            proc.returncode != 0 or bool(REDEFINE_FAILED.search(body)) or not changes
        )
        return not failed, body
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        return False, str(error)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def needs_restart(changes, old_jar=None, new_jar=None):
    """Stock redefine cannot apply class additions/removals, mixins or resources."""
    if any(MIXIN_HINT.search(dotted) for dotted, _ in changes):
        return True
    if old_jar:
        try:
            with zipfile.ZipFile(old_jar) as archive:
                names = set(archive.namelist())
                if any(path not in names for _, path in changes):
                    return True
                if new_jar:
                    with zipfile.ZipFile(new_jar) as rebuilt:
                        if any(path not in rebuilt.namelist() for _, path in changes):
                            return True
                        resources = {
                            name: archive.read(name)
                            for name in names
                            if not name.endswith(".class")
                        }
                        if resources != {
                            name: rebuilt.read(name)
                            for name in rebuilt.namelist()
                            if not name.endswith(".class")
                        }:
                            return True
        except (OSError, zipfile.BadZipFile):
            return True
    return False


def watch_loop(
    side,
    port,
    server_mods,
    game_dir,
    java_home=None,
    poll=2.0,
    once=False,
    rebuild=remap_jar,
    hotswap=redefine,
    guard=None,
    restart=None,
    receipt_path=None,
    identity=None,
):
    """Watch one or both sides serially; restart(side, jar) handles owned restarts.

    guard(side) verifies all selected processes before build, attach or runtime writes.
    Single-side results remain strings; both returns a per-side result for --once.
    """
    sides = ("server", "client") if side == "both" else (side,)
    ports = (
        {"server": SERVER_JDWP, "client": CLIENT_JDWP}
        if side == "both"
        else {side: port}
    )
    receipts = read_receipt(receipt_path)

    def snapshot(selected, jar):
        source, unsupported = source_fingerprints(selected)
        return {
            "source": source,
            "unsupported": unsupported,
            "jar": jar_fingerprint(jar),
            "identity": json.loads(json.dumps(identity(selected)))
            if identity
            else None,
        }

    states = {}
    unsupported_baselines = {}
    for selected in sides:
        baseline = newest_source_mtime(selected)
        current = deployed_jar(selected, server_mods, game_dir)
        if not side_dirs(selected) or current is None:
            message = f"watch: no sources or deployed jar for {selected}"
            if once:
                raise WatchError(message)
            print(message)
            return {name: "idle" for name in sides} if side == "both" else "idle"
        states[selected] = [baseline, current, snapshot(selected, current)]
        unsupported_baselines[selected] = receipts.get(selected, states[selected][2])[
            "unsupported"
        ]
    while True:
        time.sleep(0 if once else poll)
        results = dict.fromkeys(sides, "idle")
        pending_restart = False
        pending = {}
        try:
            for selected in sides:
                baseline, _, previous = states[selected]
                current = deployed_jar(selected, server_mods, game_dir)
                if current is None:
                    raise WatchError(f"watch: deployed {selected} jar disappeared")
                state = snapshot(selected, current)
                mtime = newest_source_mtime(selected)
                if once and receipt_path is not None:
                    if receipts.get(selected) == state:
                        if guard:
                            guard(selected)
                        continue
                elif (
                    not once
                    and state == previous
                    and (mtime is None or baseline is None or mtime <= baseline)
                ):
                    continue
                states[selected] = [mtime, current, state]
                if unsupported_baselines[selected] != state["unsupported"]:
                    raise WatchError(
                        f"watch: {selected} resources, dependencies or Gradle inputs changed; "
                        "use build-mods and setup on the stopped stack"
                    )
                if guard:
                    guard(selected)
                print(f"watch: sources changed, rebuilding {selected}...")
                built = None
                for mod_dir in side_dirs(selected):
                    candidate = rebuild(mod_dir, selected, java_home)
                    if candidate and (
                        built is None
                        or candidate.stat().st_mtime > built.stat().st_mtime
                    ):
                        built = candidate
                if built is None:
                    raise WatchError(f"watch: {selected} build failed")
                changed_classes(current, built)
                saved = receipts.get(selected, {})
                previous_identity = saved.get("identity")
                current_identity = state["identity"]
                if isinstance(previous_identity, dict):
                    previous_identity = previous_identity.get("process")
                if isinstance(current_identity, dict):
                    current_identity = current_identity.get("process")
                disk_changed = (
                    previous_identity is not None
                    and previous_identity == current_identity
                    and saved.get("jar") != state["jar"]
                )
                # Replaced disk bytes cannot prove what this unchanged JVM has loaded.
                pending[selected] = (current, built, state, disk_changed)

            updated = dict(receipts)
            for selected, (current, _, state, _) in pending.items():
                if guard:
                    guard(selected)
                if snapshot(selected, current) != state:
                    raise WatchError(
                        f"watch: {selected} inputs changed during the build"
                    )
            for selected, (current, built, state, disk_changed) in pending.items():
                changes = changed_classes(current, built)
                if not disk_changed and (
                    jar_fingerprint(built)["sha256"] == state["jar"]["sha256"]
                ):
                    updated[selected] = state
                    continue
                result = "restart"
                if (
                    not disk_changed
                    and changes
                    and not needs_restart(changes, current, built)
                ):
                    if guard:
                        guard(selected)
                    ok, out = hotswap(ports[selected], changes, built, java_home)
                    for line in out.splitlines():
                        print(f"  jdb: {line}")
                    if ok:
                        if guard:
                            guard(selected)
                        try:
                            deploy_built_jar(selected, built, server_mods, game_dir)
                            result = "redefine"
                        except PermissionError:
                            print(
                                f"watch: running JVM holds the jar open, restart the {selected}"
                            )
                results[selected] = result
                if result == "restart":
                    if restart is None:
                        if receipt_path is not None and once:
                            raise WatchError(f"watch: {selected} requires a restart")
                        pending_restart = True
                        continue
                    if guard:
                        guard(selected)
                    restart(selected, built)
                if guard:
                    guard(selected)
                current = deployed_jar(selected, server_mods, game_dir)
                if current is None or (
                    jar_fingerprint(current)["sha256"]
                    != jar_fingerprint(built)["sha256"]
                ):
                    raise WatchError(f"watch: {selected} rebuilt jar was not installed")
                applied = snapshot(selected, current)
                if (
                    applied["source"] != state["source"]
                    or applied["unsupported"] != state["unsupported"]
                ):
                    raise WatchError(
                        f"watch: {selected} inputs changed during the build"
                    )
                states[selected] = [states[selected][0], current, applied]
                updated[selected] = applied
            if receipt_path is not None and pending and not pending_restart:
                write_receipt(receipt_path, updated)
                receipts = updated
        except (
            WatchError,
            OSError,
            ValueError,
            zipfile.BadZipFile,
            subprocess.SubprocessError,
        ) as error:
            if once:
                raise WatchError(str(error)) from error
            print(f"watch: {error}; waiting for the next change")
            continue
        if once or pending_restart:
            return results if side == "both" else results[side]
