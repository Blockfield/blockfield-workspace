#!/usr/bin/env python3
"""Local Blockfield stack for developers on Windows or Linux: no containers, no homeserver.

    just setup                    # prepare server, client, Java, assets and testbot
    just server start             # 127.0.0.1:25599, RCON 25575, log in the stack dir
    just server rcon "list"
    just server stop
    just mod                      # install a locally built blockfield-server jar
    just play                     # launch the client directly (random name, no launcher)
    just client                   # prepare the workspace client directory
    just bot state                # talk to the testbot inside a running client

Everything lives in one stack directory: .dev/ inside this repository (gitignored,
override with BF_DEV_HOME for a legacy ~/.blockfield-dev). `just setup` needs `gh` logged into
Blockfield; the mod source repository is only needed for --mod flows (BF_MOD_REPO or a
localmods/ checkout). Nothing here touches production or the owner's rig. Deleting .dev/ wipes
the local world, clients and logs. Keep the directory until useful evidence and worlds are saved.

Linux and Windows servers and clients use verified native process handles.
"""

import argparse
import errno
import getpass
import hashlib
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import urllib.parse
import urllib.request
import zipfile
from contextlib import redirect_stdout
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modmap import BuildError, classify, decisions, ensure_built  # noqa: E402
from modmap import load_mapping, local_jars, newest_jar  # noqa: E402  (localmods/ mapping)
from modmap import runtime_jar_name, write_stamp  # noqa: E402  (localmods/ mapping)
from rcon import Rcon  # noqa: E402  (scripts/dev/rcon.py, plain stdlib)
from toolchain import deb_libraries, download, native, tool  # noqa: E402
from terminal import progress  # noqa: E402
from dev_cache import CACHE as INPUT_CACHE, clone_tree, materialize  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
CLIENT_PACK = ROOT / "client"  # blockfield-client submodule: pins, client archives
SERVER_PACK = (
    ROOT / "server"
)  # blockfield-server submodule: pins, world seed, build-distribution.py
LOCALMODS = (
    ROOT / "localmods"
)  # submodules of blockfield-mod + forks, see localmods/mods.json.example
PORT = int(os.environ.get("BF_SERVER_PORT", "25599"))
RCON_PORT = int(os.environ.get("BF_RCON_PORT", "25575"))
if not (1 <= PORT <= 65535 and 1 <= RCON_PORT <= 65535) or PORT == RCON_PORT:
    raise ValueError(
        "BF_SERVER_PORT and BF_RCON_PORT must be distinct TCP ports in 1..65535"
    )
RCON_PASSWORD = "leaks"
BOT_PORTS = {1: 47777, 2: 47778}
PROXY_ONLY_MODS = ("FabricProxy-Lite", "crossstitch")
CARPET_ASSET = (
    "fabric-carpet-1.21-1.4.147+v240613.jar",
    "https://cdn.modrinth.com/data/TQTTVgYE/versions/f2mvlGrg/fabric-carpet-1.21-1.4.147%2Bv240613.jar",
    "079fc8a4e073eb24b010ffcc588e59f93b984143e17d8ef19fbb0b6aff0f19d9",
)
SERVER_PROPERTIES = {
    "server-ip": "127.0.0.1",
    "server-port": str(PORT),
    "enable-rcon": "true",
    "rcon.port": str(RCON_PORT),
    "rcon.password": RCON_PASSWORD,
    "online-mode": "false",
    "spawn-protection": "0",
    "view-distance": "8",
    "sync-chunk-writes": "false",
}


def stack_home():
    if os.environ.get("BF_DEV_HOME"):
        return Path(os.environ["BF_DEV_HOME"])
    return ROOT / ".dev"


HOME = stack_home()
SERVER = HOME / "server"
PID_FILE = HOME / "server.pid"
CLIENT_PID_FILE = HOME / "client.pid"
CLIENT_CMD_FILE = HOME / "client.cmd"
LOG = (
    SERVER / "server.out"
)  # same layout as the rig, so the harness needs one path only


# ── terminal UI (Claude Code-ish: quiet by default, color only on a TTY) ──────────


def _tty(stream="stdout"):
    try:
        target = sys.stdout if stream == "stdout" else sys.stderr
        return target.isatty()
    except Exception:
        return False


def _use_color(stream="stdout"):
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return _tty(stream)


def _sym(stream="stdout"):
    enc = ""
    try:
        enc = (sys.stdout if stream == "stdout" else sys.stderr).encoding or ""
    except Exception:
        pass
    if "utf" in enc.lower():
        return {"ok": "✓", "err": "✗", "arrow": "▸", "warn": "!", "dot": "●"}
    return {"ok": "ok", "err": "x", "arrow": ">", "warn": "!", "dot": "*"}


def _paint(text, code, stream="stdout"):
    if not _use_color(stream):
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(text):
    return _paint(text, "1")


def _dim(text):
    return _paint(text, "2")


def _green(text):
    return _paint(text, "32")


def _red(text, stream="stderr"):
    return _paint(text, "31", stream)


def _yellow(text):
    return _paint(text, "33")


def _cyan(text):
    return _paint(text, "36")


def step(message):
    print(f"{_cyan(_sym()['arrow'])} {message}", flush=True)


def ok(message):
    print(f"{_green(_sym()['ok'])} {message}", flush=True)


def warn(message):
    print(f"{_yellow(_sym()['warn'])} {message}")


def die(message):
    mark = _red(f"{_sym('stderr')['err']} just", stream="stderr")
    raise SystemExit(f"{mark}: {message}")


def java_exe():
    """Bootstrap this stack's pinned JDK and activate its Java tools."""
    try:
        return native("java")
    except (OSError, RuntimeError) as error:
        die(str(error))


def modpack_dir(argument=None, side="server"):
    """CLI, side-specific environment override, then the workspace pack checkout."""
    default = CLIENT_PACK if side == "client" else SERVER_PACK
    override = os.environ.get("BF_CLIENT_MODPACK" if side == "client" else "BF_MODPACK")
    candidates = (
        ([Path(argument)] if argument else [])
        + ([Path(override)] if override else [])
        + [default]
    )
    for path in candidates:
        if (path / "pack.toml").is_file():
            return path.resolve()
    die(
        f"{side} pack not found: run from the blockfield-workspace checkout or pass --modpack"
    )


def mod_repo_dir(argument=None):
    """Checkout of blockfield-mod sources; needed only for --mod flows, never for setup."""
    for candidate in filter(
        None,
        [
            argument,
            os.environ.get("BF_MOD_REPO"),
            LOCALMODS / "blockfield-mod",
        ],
    ):
        path = Path(candidate)
        if (path / "settings.gradle").is_file():
            return path.resolve()
    die(
        "blockfield-mod repository not found: git submodule update --init localmods/blockfield-mod or pass --mod-repo"
    )


REFRESH_STACK = (
    "stop this stack (just server stop, just play --stop), run just setup --force,"
    " then just server start"
)


def testbot_jar(args):
    """The explicit testbot JAR or the single workspace implementation."""
    explicit = getattr(args, "testbot", None)
    if explicit and explicit is not True:
        jar = Path(explicit)
        return jar if jar.is_file() else None
    return newest_jar(
        ROOT / "scripts" / "dev" / "testbot" / "build" / "libs", "bftestbot-"
    )


def testbot_enabled(args, game_dir):
    return not getattr(args, "no_testbot", False) and bool(
        getattr(args, "testbot", None)
        or getattr(args, "testbot_port", None)
        or (game_dir / "mods/bftestbot.jar").is_file()
    )


def install_testbot(args, game_dir):
    destination = game_dir / "mods/bftestbot.jar"
    if not testbot_enabled(args, game_dir):
        if getattr(args, "no_testbot", False):
            destination.unlink(missing_ok=True)
        return None
    explicit = getattr(args, "testbot", None)
    jar = destination if not explicit and destination.is_file() else testbot_jar(args)
    if not jar and explicit in (None, True):
        build_testbot(args)
        jar = testbot_jar(args)
    if not jar or not jar.is_file():
        die("no testbot jar: run `just build-testbot` or pass --testbot JAR")
    if jar.resolve() != destination.resolve():
        materialize(jar, destination)
    return getattr(args, "testbot_port", None) or "47777"


def build_testbot(args):
    """./gradlew build for the workspace testbot (scripts/dev/testbot)."""
    java_exe()
    directory = ROOT / "scripts" / "dev" / "testbot"
    if not (directory / "settings.gradle").is_file():
        die("testbot sources missing in scripts/dev/testbot")
    wrapper = directory / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
    command = ([str(wrapper)] if sys.platform == "win32" else ["sh", str(wrapper)]) + [
        "build",
        "-Dorg.gradle.daemon.idletimeout=600000",
        "--max-workers=2",
    ]
    run(command, cwd=directory, message="Building testbot")
    jar = newest_jar(directory / "build" / "libs", "bftestbot-")
    ok(f"built: {jar.name}" if jar else "built (no jar found)")


def run_pack_build(arguments, cwd, side):
    """Pack build with localmods/ jars overlaid: built wins, pins stay as fallback."""
    ensure_localmods()
    jars = local_jars(ROOT)
    if jars:
        step(f"localmods/: {len(jars)} jar(s) override pins")
    run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            ROOT / "scripts/dev/pack_build.py",
            side,
            *arguments,
        ],
        cwd=cwd,
        message=f"Building {side} pack",
    )


def ensure_localmods(names=None):
    """Assemble LOCAL localmods/ jars with a stale stamp; PIN mods need nothing."""
    java_exe()
    try:
        report = ensure_built(ROOT, load_mapping(), HOME, names)
    except BuildError as error:
        die(str(error))
    for dirname, source, action in report:
        if action != "fresh":
            step(f"localmods/{dirname}: {source} ({action})")


def run(command, *, message=None, **kwargs):
    command = [str(c) for c in command]
    buffered = (
        _tty("stderr")
        and os.environ.get("TERM") != "dumb"
        and not any(key in kwargs for key in ("capture_output", "stdout", "stderr"))
    )
    try:
        with tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as log:
            if buffered:
                kwargs.update(stdout=log, stderr=subprocess.STDOUT)
            try:
                with progress(message or f"Running {Path(command[0]).name}"):
                    result = subprocess.run(command, **kwargs)
                    if result.returncode != 0:
                        raise subprocess.CalledProcessError(
                            result.returncode,
                            command,
                            output=getattr(result, "stdout", None),
                            stderr=getattr(result, "stderr", None),
                        )
            finally:
                if buffered:
                    log.seek(0)
                    shutil.copyfileobj(log, sys.stdout)
                    sys.stdout.flush()
    except subprocess.CalledProcessError as error:
        for output in (error.stdout, error.stderr):
            if output:
                sys.stderr.write(
                    output.decode("utf-8", "replace")
                    if isinstance(output, bytes)
                    else output
                )
        die(f"command failed ({error.returncode}): {' '.join(command)}")
    return result


# ── setup ──────────────────────────────────────────────────────────────────────────────────────


def pinned_jar_name(modpack, side):
    return metafile_jar_name(modpack / "mods" / f"blockfield-{side}.pw.toml")


def metafile_jar_name(metadata):
    filename = tomllib.loads(metadata.read_text(encoding="utf-8"))["filename"]
    if (
        not isinstance(filename, str)
        or PureWindowsPath(filename).name != filename
        or PureWindowsPath(filename).drive
        or not filename.endswith(".jar")
    ):
        die(f"invalid mod filename in {metadata}: {filename!r}")
    return filename


def fetch_mod_jars(modpack, client_pack=None):
    """Same pins as CI's fetch-mod-release.sh, without needing a POSIX shell.

    Server pack owns files/mods/; per-side metafiles live in their submodules.
    Metafiles covered by a localmods/ build are skipped: the jar comes from sources."""
    client_pack = client_pack or CLIENT_PACK
    local = local_jars(ROOT)
    (modpack / "files" / "mods").mkdir(parents=True, exist_ok=True)
    for side, pack in (("client", client_pack), ("server", modpack)):
        if f"blockfield-{side}.pw.toml" in local:
            continue
        name = pinned_jar_name(pack, side)
        if (modpack / "files" / "mods" / name).is_file():
            continue
        tag = "v" + name.removeprefix(f"blockfield-{side}-").split("+")[0].removesuffix(
            ".jar"
        )
        # Релизы до 2026-09-27 остались в архивном форке netherg-io/blockfield-mod.
        for repo in ("Blockfield/blockfield-mod", "netherg-io/blockfield-mod"):
            try:
                run(
                    [
                        "gh",
                        "release",
                        "download",
                        tag,
                        "--repo",
                        repo,
                        "-p",
                        name,
                        "-D",
                        "files/mods",
                        "--clobber",
                    ],
                    cwd=modpack,
                    message=f"Downloading {name} from {repo}",
                )
            except SystemExit:
                continue
            break
        else:
            raise SystemExit(f"{name}: release {tag} not found")
    name = pinned_jar_name(client_pack, "music")
    if not (modpack / "files" / "mods" / name).is_file():
        run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                str(client_pack / "scripts/toolchain.py"),
                "-c",
                "import sys,tomllib; from pathlib import Path; "
                "root=Path(sys.argv[1]); sys.path.insert(0,str(root/'scripts')); "
                "import packlib; "
                "meta=tomllib.loads((root/'mods/blockfield-music.pw.toml').read_text()); "
                "data=packlib.resolve_mod(root,('mods/blockfield-music.pw.toml',meta),{})[-1]; "
                "Path(sys.argv[2]).write_bytes(data)",
                str(client_pack),
                str(modpack / "files" / "mods" / name),
            ],
            cwd=modpack,
            message="Downloading music pack",
        )


def fetch_world_seed(modpack):
    """The release and digest come from the pack's assets.lock.json, so a cached older seed is replaced."""
    seed = modpack / "sources" / "world-seed.zip"
    lock = json.loads(
        (modpack / "distribution" / "assets.lock.json").read_text(encoding="utf-8")
    )
    if (
        not seed.is_file()
        or hashlib.sha256(seed.read_bytes()).hexdigest() != lock["sha256"]
    ):
        run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                str(modpack / "scripts/toolchain.py"),
                str(modpack / "scripts/fetch-world-seed.py"),
            ],
            cwd=modpack,
            message="Downloading world seed",
        )
    return seed


def patch_server_properties():
    path = SERVER / "server.properties"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in SERVER_PROPERTIES:
            out.append(f"{key}={SERVER_PROPERTIES[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in SERVER_PROPERTIES.items() if k not in seen]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def disable_proxy_mods():
    """Behind Velocity in production; with them a direct connection to 25599 is refused."""
    off = SERVER / "mods-off"
    off.mkdir(exist_ok=True)
    for jar in (SERVER / "mods").glob("*.jar"):
        if jar.name.startswith(PROXY_ONLY_MODS):
            jar.replace(off / jar.name)


def build_distribution(modpack, client_pack=None):
    """Same build CI runs: mods, configs and a complete server tree."""
    ensure_localmods()
    if client_pack is None:
        fetch_mod_jars(modpack)
    else:
        fetch_mod_jars(modpack, client_pack)
    seed = fetch_world_seed(modpack)
    out = HOME / "dist"
    shutil.rmtree(out, ignore_errors=True)
    HOME.mkdir(parents=True, exist_ok=True)
    run_pack_build(
        [
            "--root",
            modpack,
            "--out",
            out,
            "--server-source",
            modpack.resolve() / "server",
            "--world-seed",
            seed,
            "--build-id",
            "dev",
        ],
        cwd=modpack,
        side="server",
    )
    return out


def server_runtime_ready():
    if not all(
        (SERVER / name).exists()
        for name in ("fabric-server-launch.jar", "mods", "config", "build.json")
    ):
        return False
    try:
        manifest = (SERVER / "build.json").read_bytes()
        entries = json.loads(manifest)["mods"]
        override = {}
        receipt = HOME / "watch-receipt.json"
        if receipt.is_file():
            try:
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                side = saved["sides"]["server"]
                if (
                    saved.get("schema") == 1
                    and side["identity"]["runtime_manifest_sha256"]
                    == hashlib.sha256(manifest).hexdigest()
                ):
                    if isinstance(side.get("jar"), dict):
                        override = side["jar"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if not isinstance(entries, list) or not entries:
            return False
        for entry in entries:
            path = Path(entry["path"])
            if (
                path.is_absolute()
                or PureWindowsPath(path).drive
                or ".." in path.parts
                or path.parts[0] != "mods"
            ):
                return False
            jar = SERVER / path
            if not jar.is_file() and path.name.startswith(PROXY_ONLY_MODS):
                jar = SERVER / "mods-off" / path.name
            if not jar.is_file():
                return False
            actual = hashlib.sha256(jar.read_bytes()).hexdigest()
            if actual != entry["sha256"] and not (
                path.name.startswith("blockfield-server-")
                and not jar.is_symlink()
                and jar.resolve().parent == (SERVER / "mods").resolve()
                and override.get("path") == str(jar.resolve())
                and override.get("sha256") == actual
            ):
                return False
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return False
    return True


def install_carpet():
    jar = download(*CARPET_ASSET, cache=INPUT_CACHE / "downloads")
    destination = SERVER / "mods" / jar.name
    materialize(jar, destination)
    for old in destination.parent.glob("fabric-carpet-*.jar"):
        if old != destination:
            old.unlink()


ZERO_SPREAD_PACK = ROOT / "scripts" / "dev" / "standpacks" / "tacz_zero_spread"


def install_zero_spread_pack():
    """Stand-only zero-spread TaCZ test gun; TaCZ syncs the data to clients."""
    for src in sorted(ZERO_SPREAD_PACK.rglob("*")):
        if src.is_file():
            destination = (
                SERVER
                / "tacz"
                / ZERO_SPREAD_PACK.name
                / src.relative_to(ZERO_SPREAD_PACK)
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            materialize(src, destination)


def _restore_missing(src, dst):
    """Copy built entries absent from an existing server tree; never overwrite."""
    if dst.is_symlink() or dst.is_file():
        return
    if src.is_dir():
        if not dst.is_dir():
            shutil.copytree(src, dst, copy_function=materialize)
        else:
            for child in src.iterdir():
                _restore_missing(child, dst / child.name)
    elif not dst.exists():
        materialize(src, dst)


def setup(args):
    if server_pid() or client_pid():
        die("server or client is running; inspect it before preparing this stack")
    for path in (PID_FILE, CLIENT_PID_FILE):
        record = process_record(path)
        if record and record.get("owner") not in (None, process_owner()):
            die(f"process record belongs to another session: {path}")
    ports = (PORT, RCON_PORT, *BOT_PORTS.values(), 5005, 5006, 5007)
    if any(port_open(port) for port in ports):
        die("stack port is busy; inspect the existing process before preparing files")
    print(f"\n{_bold('Blockfield')}  {_dim('Prepare local stack')}\n", flush=True)
    step("1/4  Java runtime")
    java_exe()
    modpack = modpack_dir(args.modpack)
    client_pack = modpack_dir(getattr(args, "client_modpack", None), side="client")
    step("2/4  Server pack")
    with progress("Checking installed server files"):
        ready = not args.force and server_runtime_ready()
    if not ready:
        out = build_distribution(modpack, client_pack)
        built = out / "server"
        if not built.is_dir():
            die(f"build-distribution.py produced no server tree in {out}")
        if not (out / "build.json").is_file():
            die(f"build-distribution.py produced no build.json in {out}")
        if SERVER.exists():
            # A rebuild replaces runtime files; worlds and map templates stay.
            for name in ("mods", "config", "defaultconfigs"):
                shutil.rmtree(SERVER / name, ignore_errors=True)
                if (built / name).is_dir():
                    shutil.copytree(
                        built / name, SERVER / name, copy_function=materialize
                    )
            for item in built.iterdir():
                if item.is_file():
                    materialize(item, SERVER / item.name)
                elif item.name not in ("mods", "config", "defaultconfigs"):
                    # Prune may leave a half dir (e.g. maps/ with only logs);
                    # restore absent templates without touching existing worlds.
                    _restore_missing(item, SERVER / item.name)
        else:
            shutil.copytree(built, SERVER, copy_function=materialize)
        shutil.copy2(out / "build.json", SERVER / "build.json")
        shutil.rmtree(out, ignore_errors=True)
        (SERVER / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        patch_server_properties()
        disable_proxy_mods()
    install_carpet()
    install_zero_spread_pack()
    step("3/4  Testbot")
    if args.no_testbot:
        step("Removing an installed testbot")
    elif args.testbot is True:
        build_testbot(args)
    elif not args.testbot:
        step("Unchanged; add with --testbot, remove with --no-testbot")
    step("4/4  Client pack and Minecraft assets")
    game_dir, _command = prepare_client(
        args, force=args.force, repair=True, sync_marker=True
    )
    ok(f"server ready: {SERVER}")
    ok(f"client ready: {game_dir}")
    print(f"  {_dim('next:')} just server start; just play")
    print(
        f"  {_dim('hot reload:')} README.md#hot-reload-hmr (build local Java sources first)"
    )
    print("    just server start --debug; just play --mod --debug; just watch both")
    print("    or watch one side: just watch server / just watch client")


def blockfield_jar(side):
    """Built blockfield-{side} jar: localmods/ first, None when not built."""
    jar = local_jars(ROOT).get(f"blockfield-{side}.pw.toml")
    return jar if jar and jar.is_file() else None


def client_mod_jar(mod_repo=None):
    """blockfield-client jar for --mod: localmods/ first, legacy --mod-repo as fallback."""
    jar = blockfield_jar("client")
    if not jar and mod_repo:
        jar = newest_jar(
            mod_repo_dir(mod_repo) / "client/build/libs", "blockfield-client-"
        )
    if not jar:
        die("--mod: no client jar, run `just build-mods blockfield-mod` first")
    return jar


def install_client_mod(game_dir, mod, mod_repo=None, sync_marker=True):
    """--mod: the local blockfield-client jar replaces the published one (one mod id loads once).

    Then the local server's marker goes into the game dir: PackVersionGate compares it at
    login, and digests of locally built trees never equal the published or archive ones."""
    if mod:
        jar = client_mod_jar(mod_repo)
        if not (
            jar.name.startswith("blockfield-client-")
            and jar.name.endswith(".jar")
            and runtime_jar_name(jar.name)
        ):
            die(f"not a client runtime jar: {jar.name}")
        (game_dir / "mods").mkdir(parents=True, exist_ok=True)
        for old in (game_dir / "mods").glob("blockfield-client-*.jar"):
            old.unlink()
        shutil.copy2(jar, game_dir / "mods" / jar.name)
        ok(f"installed {jar.name}")
    server_marker = SERVER / "config" / "blockfield-pack-version.txt"
    if sync_marker and server_marker.is_file():
        marker = game_dir / "config" / server_marker.name
        marker.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(server_marker, marker)


def install_client_localmods(game_dir, client_pack):
    """Refresh the same local overrides used by the client pack build."""
    mods = game_dir / "mods"
    selected = []
    server_marker = SERVER / "config" / "blockfield-pack-version.txt"
    for metafile, jar in local_jars(ROOT).items():
        if metafile == "blockfield-client.pw.toml":
            continue  # The explicit --mod flow owns this side-specific jar.
        metadata = client_pack / "mods" / metafile
        if not metadata.is_file():
            continue
        filename = metafile_jar_name(metadata)
        if not zipfile.is_zipfile(jar):
            die(f"not a JAR archive: {jar}")
        server_metadata = SERVER_PACK / "mods" / metafile
        if server_marker.is_file() and server_metadata.is_file():
            server_jar = SERVER / "mods" / metafile_jar_name(server_metadata)
            if (
                not server_jar.is_file()
                or hashlib.sha256(server_jar.read_bytes()).digest()
                != hashlib.sha256(jar.read_bytes()).digest()
            ):
                die(
                    f"{metafile}: the localmods/ JAR was rebuilt after `just setup` and the"
                    f" local server still runs the old one; {REFRESH_STACK}"
                )
        selected.append((jar, filename))
    mods.mkdir(parents=True, exist_ok=True)
    for jar, filename in selected:
        destination = mods / filename
        with tempfile.NamedTemporaryFile(
            dir=mods, prefix=".localmod-", delete=False
        ) as file:
            temporary = Path(file.name)
        try:
            shutil.copyfile(jar, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        incoming_name = mods / jar.name
        if incoming_name != destination:
            incoming_name.unlink(missing_ok=True)
        ok(f"installed {jar.name} as {filename}")


def build_mods(args):
    """Build Gradle submodules or exact recipe outputs in localmods/."""
    java_exe()
    mapping = load_mapping()
    names = args.names or [d for d in mapping if not d.startswith("_")]
    for name in names:
        if name not in mapping or name.startswith("_"):
            die(
                f"unknown localmod {name}: {[d for d in mapping if not d.startswith('_')]}"
            )
    if getattr(args, "plan", False):
        for dirname, source, reason in decisions(ROOT, mapping, names):
            print(f"{dirname}: {source} ({reason})", flush=True)
        return
    built = []
    for name in names:
        directory = LOCALMODS / name
        if not directory.is_dir():
            die(f"{directory} missing: git submodule update --init localmods/{name}")
        source, reason = classify(ROOT, name, mapping[name])
        if source == "pin":
            step(f"{name}: using published pin ({reason})")
            continue
        if "jar" in mapping[name]:
            # Recipe builds write into gitignored dirs (e.g. tacz files/mods/) that a
            # fresh worktree lacks; create the declared output dir before building.
            jar_path = directory / mapping[name]["jar"]
            jar_path.parent.mkdir(parents=True, exist_ok=True)
            # Recipe builds shell out to bare `ffmpeg`; Homebrew ffmpeg dropped
            # libvorbis (TaCZ animsync needs -c:a libvorbis), so prefer a
            # vorbis-capable binary via the shim first on PATH.
            recipe_env = dict(
                os.environ,
                PATH=str(ROOT / "scripts" / "dev" / "shims")
                + os.pathsep
                + os.environ.get("PATH", ""),
            )
            run(
                ["just", "build"],
                cwd=directory,
                message=f"Building {name}",
                env=recipe_env,
            )
            if not local_jars(ROOT, {name: mapping[name]}):
                die(f"{name}: build did not produce {mapping[name]['jar']}")
        else:
            wrapper = directory / (
                "gradlew.bat" if sys.platform == "win32" else "gradlew"
            )
            command = (
                [str(wrapper)] if sys.platform == "win32" else ["sh", str(wrapper)]
            ) + [
                "build",
                "-Dorg.gradle.daemon.idletimeout=600000",
                "--max-workers=2",
                "-Dorg.gradle.jvmargs=-Xmx2G",
            ]
            run(command, cwd=directory, message=f"Building {name}")
        built.append(name)
        try:
            write_stamp(HOME, name, directory, local_jars(ROOT, {name: mapping[name]}))
        except (BuildError, OSError) as error:
            warn(f"{name}: built, but the stamp was not written ({error})")
    ok(f"built: {', '.join(built)}" if built else "nothing to build: all use pins")


def install_mod(args):
    jar = Path(args.jar) if args.jar else blockfield_jar("server")
    if (not jar or not jar.is_file()) and (args.jar or args.mod_repo):
        # Legacy explicit repo (BF_MOD_REPO/--mod-repo) when localmods/ has no build.
        jar = newest_jar(
            mod_repo_dir(args.mod_repo) / "server/build/libs", "blockfield-server-"
        )
    if not jar or not jar.is_file():
        die(
            "no server jar: run `just build-mods blockfield-mod` first, or pass the jar path"
        )
    if not (
        jar.name.startswith("blockfield-server-")
        and jar.name.endswith(".jar")
        and runtime_jar_name(jar.name)
    ):
        die(f"not a server runtime jar: {jar.name}")
    if server_pid():
        die("stop the server first: replacing an open jar breaks class loading")
    mods, destination = SERVER / "mods", SERVER / "mods" / jar.name
    mods.mkdir(parents=True, exist_ok=True)
    if not destination.exists() or not jar.samefile(destination):
        # Copy fully before replacing anything: a failed copy must not lose the old jar.
        with tempfile.NamedTemporaryFile(
            dir=mods, prefix=".blockfield-install-", delete=False
        ) as file:
            temporary = Path(file.name)
        try:
            shutil.copyfile(jar, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    for old in mods.glob("blockfield-server-*.jar"):
        if old != destination:
            old.unlink()
    ok(f"installed {jar.name}")


# ── server process ─────────────────────────────────────────────────────────────────────────────


def port_open(port, host="127.0.0.1"):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def process_owner():
    for key in (
        "AI_AGENT_SESSION_ID",
        "CODEX_THREAD_ID",
        "CODEX_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "GEMINI_SESSION_ID",
        "OPENCODE_SESSION_ID",
    ):
        if os.environ.get(key):
            return os.environ[key]
    user = os.getuid() if hasattr(os, "getuid") else getpass.getuser()
    return f"developer:{user}:{ROOT.resolve()}"


def process_record(path):
    try:
        record = json.loads(path.read_text())
        if isinstance(record, int):
            record = {"pid": record}
        pid = record["pid"]
        if type(pid) is not int or pid <= 0:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if record.get("native") == "linux":
        if not sys.platform.startswith("linux"):
            die(f"Linux process record cannot be verified on this platform: {path}")
        import linux_process

        try:
            current = linux_process.identity(pid)
            if current and not linux_process.matches(record, current):
                die(
                    f"Linux process birth/executable/runtime directory no longer matches {path}"
                )
        except OSError as error:
            die(f"cannot verify Linux process in {path}: {error}")
        return {**record, "live": current is not None}
    if record.get("native") == "windows":
        if sys.platform != "win32":
            die(f"Windows process record cannot be verified on this platform: {path}")
        import windows_process

        try:
            current = windows_process.identity(pid)
            if current and not windows_process.matches(record, current):
                die(f"Windows process birth/executable no longer matches {path}")
        except OSError as error:
            die(f"cannot verify Windows process in {path}: {error}")
        return {**record, "live": current is not None}
    try:
        if sys.platform == "win32":
            alive = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
            )
            record["live"] = str(pid) in alive.stdout
        else:
            os.kill(pid, 0)
            record["live"] = True
    except (OSError, subprocess.SubprocessError):
        record["live"] = False
    return record


def recorded_pid(path):
    record = process_record(path)
    return record["pid"] if record and record.get("live") else None


def start_process(command, cwd, path, log_path, env=None, *, headless=False):
    if recorded_pid(path):
        die(f"process already running: {path}")
    log_path.write_text("", encoding="utf-8")
    if headless and sys.platform not in ("linux", "win32"):
        die(
            "verified headless Minecraft backend is only available on Linux; desktop fallback is forbidden"
        )
    with log_path.open("a", encoding="utf-8") as log:
        if headless and sys.platform == "win32":
            import windows_headless

            command = list(map(str, command))
            if not env or not env.get("BF_HEADLESS_JAVA"):
                die(
                    "Windows headless renderer is not verified; desktop fallback is forbidden"
                )
            command[0] = env["BF_HEADLESS_JAVA"]
            process = windows_headless.launch(command, cwd, env, log, process_owner())
        else:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                if sys.platform == "win32"
                else 0,
                start_new_session=sys.platform != "win32",
            )
    if sys.platform.startswith("linux"):
        import linux_process

        try:
            identity = linux_process.identity(process.pid)
            if identity is None:
                process.wait(timeout=1)
                die(f"process exited before registration: {log_path}")
            path.write_text(
                json.dumps(
                    {
                        **identity,
                        "native": "linux",
                        "owner": process_owner(),
                        "command": list(map(str, command)),
                        "log": str(log_path),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            # Popen retains this unreaped child while registration is attempted.
            process.terminate()
            process.wait(timeout=5)
            raise
        return process.pid, None
    if sys.platform == "win32":
        import windows_process

        try:
            identity = (
                process.identity if headless else windows_process.identity(process.pid)
            )
            if identity is None:
                process.wait(timeout=1)
                die(f"process exited before registration: {log_path}")
            path.write_text(
                json.dumps(
                    {
                        **identity,
                        "native": "windows",
                        "owner": process_owner(),
                        "command": list(map(str, command)),
                        "cwd": str(cwd),
                        "log": str(log_path),
                        **({"backend": process.backend} if headless else {}),
                    }
                ),
                encoding="utf-8",
            )
            if headless:
                try:
                    wait_client_ready(process.pid)
                except BaseException:
                    process.terminate()
                    process.wait(timeout=5)
                    path.unlink(missing_ok=True)
                    raise
        except OSError:
            # Popen holds the handle we just created, even if registry writing fails.
            process.terminate()
            process.wait(timeout=5)
            raise
        finally:
            if headless:
                process.close()
        return process.pid, None
    path.write_text(str(process.pid))
    return process.pid, process


def stop_process(path):
    record = process_record(path)
    if not record:
        return
    if record.get("native") in ("linux", "windows"):
        if record["native"] == "linux":
            import linux_process as native_process
        else:
            import windows_process as native_process

        if record.get("owner") != process_owner():
            die(f"process belongs to another session: {record.get('owner')}")

        def server_shutdown():
            if not listener_owned(record["pid"], RCON_PORT):
                die("RCON listener is not owned by the recorded server process")
            client = Rcon(port=RCON_PORT, pw=RCON_PASSWORD, timeout=5)
            try:
                if not listener_owned(
                    record["pid"], RCON_PORT, peer_port=client.s.getsockname()[1]
                ):
                    die("RCON connection is not owned by the recorded server process")
                try:
                    client.cmd("stop")
                # The server may close the socket before the boundary packet is sent.
                except (EOFError, OSError):
                    pass
            finally:
                client.s.close()

        try:
            native_process.stop(
                record, graceful=server_shutdown if path == PID_FILE else None
            )
        except (OSError, RuntimeError) as error:
            die(f"{record['native'].capitalize()} process stop refused: {error}")
        path.unlink(missing_ok=True)
        return
    die(f"{path} has no process ownership record; stop that process manually")


def server_pid():
    return recorded_pid(PID_FILE)


def server_start(args):
    if server_pid():
        die(
            "this stack's server is already running (just server stop, or just server log)"
        )
    debug_port = int(getattr(args, "debug_port", 5005))
    if (
        port_open(PORT)
        or port_open(RCON_PORT)
        or (args.debug and port_open(debug_port))
    ):
        die(
            "server/RCON/debug port is busy: inspect the existing process before starting another server"
        )
    if not (SERVER / "fabric-server-launch.jar").is_file():
        die(f"no server in {SERVER}: run `just setup` first")
    command = [java_exe(), f"-Xms{args.xms}", f"-Xmx{args.xmx}", "-XX:+UseG1GC"]
    if getattr(args, "pose_debug", False):
        command.append("-Dblockfield.poseDebug=true")
    if args.debug:
        command.append(
            f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:{debug_port}"
        )
    maps_dir = os.environ.get("BLOCKFIELD_MAPS_DIR", "maps")
    command += [
        f"-DBLOCKFIELD_MAPS_DIR={maps_dir}",
        "-jar",
        "fabric-server-launch.jar",
        "nogui",
    ]
    env = dict(os.environ, BLOCKFIELD_MAPS_DIR=maps_dir)
    if getattr(args, "gate", False):
        env.pop("BLOCKFIELD_DEV", None)
        env["BLOCKFIELD_BUILD_FILE"] = str(SERVER / "build.json")
    else:
        env["BLOCKFIELD_DEV"] = "1"
    pid, _ = start_process(command, SERVER, PID_FILE, LOG, env=env)
    print(f"  {_dim('pid')} {pid}  {_dim('log')} {LOG}")
    with progress(f"Starting server on 127.0.0.1:{PORT}"):
        for _ in range(120):
            if not server_pid():
                die(f"server exited early, see {LOG}")
            if LOG.is_file() and "Done (" in LOG.read_text(
                encoding="utf-8", errors="replace"
            ):
                break
            time.sleep(3)
        else:
            die(f"server did not report Done in 6 minutes, see {LOG}")
    ok(f"server ready: 127.0.0.1:{PORT}")


def server_stop(_args):
    stop_process(PID_FILE)
    ok("stopped")


def client_pid():
    return recorded_pid(CLIENT_PID_FILE)


def client_stop():
    stop_process(CLIENT_PID_FILE)
    stop_process(HOME / "display.pid")


def gpu_render_node():
    return sys.platform == "linux" and any(
        os.access(node, os.R_OK | os.W_OK) for node in Path("/dev/dri").glob("renderD*")
    )


def client_environment(headless, gpu=False):
    environment = dict(os.environ)
    if not headless:
        if not process_owner().startswith("developer:"):
            die("agent Minecraft clients require headless mode")
        return environment
    environment.pop("DISPLAY", None)
    environment.pop("WAYLAND_DISPLAY", None)
    environment["ALSOFT_DRIVERS"] = "null"
    if sys.platform == "win32":
        if gpu:
            die("--gpu requires the Linux VirtualGL backend")
        from media import windows_java

        executable = windows_java(Path(java_exe()).parent)
        environment.update(
            GALLIUM_DRIVER="llvmpipe",
            LP_NUM_THREADS="2",
            BF_HEADLESS_JAVA=str(executable),
        )
        verify_windows_backend(environment)
        return environment
    if sys.platform != "linux":
        die(
            "verified headless Minecraft backend is only available on Linux; desktop fallback is forbidden"
        )
    environment.update(
        LIBGL_ALWAYS_SOFTWARE="1", LP_NUM_THREADS="2", SDL_VIDEODRIVER="x11"
    )
    if Path("/usr/lib64/dri/swrast_dri.so").is_file():
        environment["LIBGL_DRIVERS_PATH"] = "/usr/lib64/dri"
    path = HOME / "display.pid"
    if recorded_pid(path):
        record = process_record(path)
        if record.get("owner") != process_owner():
            die("virtual display belongs to another session")
    else:
        cookie = os.urandom(16)
        authority = HOME / "display.auth"

        def authorize(number):
            fields = (b"", number.encode(), b"MIT-MAGIC-COOKIE-1", cookie)
            authority.write_bytes(
                struct.pack(">H", 65535)
                + b"".join(struct.pack(">H", len(value)) + value for value in fields)
            )
            authority.chmod(0o600)

        authorize("")
        log = HOME / "display.out"
        pid, _ = start_process(
            [
                str(tool("Xvfb")),
                "-displayfd",
                "1",
                "-screen",
                "0",
                "1280x720x24",
                "-nolisten",
                "tcp",
                "-auth",
                str(authority),
                "-noreset",
            ],
            HOME,
            path,
            log,
            env=environment,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if recorded_pid(path) != pid:
                die(f"virtual display exited; see {log}")
            number = next(
                (line for line in log.read_text().splitlines() if line.isdigit()), None
            )
            if number is not None:
                authorize(number)
                (HOME / "display.number").write_text(number)
                break
            time.sleep(0.1)
        else:
            stop_process(path)
            die(f"virtual display not ready within 15 seconds; see {log}")
    environment.update(
        DISPLAY=":" + (HOME / "display.number").read_text().strip(),
        XAUTHORITY=str(HOME / "display.auth"),
        LIBGL_ALWAYS_SOFTWARE="1",
        SDL_VIDEODRIVER="x11",
    )
    if gpu:
        # VirtualGL renders the client's GLX on the first EGL device and copies each frame into
        # the private Xvfb, which stays the only display: no host X server or compositor.
        if not gpu_render_node():
            die("--gpu needs a readable and writable /dev/dri/renderD* node")
        for name in ("LIBGL_ALWAYS_SOFTWARE", "LP_NUM_THREADS", "LIBGL_DRIVERS_PATH"):
            environment.pop(name, None)
        faker = deb_libraries(
            "virtualgl", "usr/lib/libdlfaker.so", "usr/lib/libvglfaker.so"
        )
        # LD_PRELOAD splits on spaces, which project paths may contain: preload by name.
        search = [str(faker[0].parent), environment.get("LD_LIBRARY_PATH", "")]
        environment.update(
            LD_LIBRARY_PATH=":".join(filter(None, search)),
            LD_PRELOAD=":".join(path.name for path in faker),
            VGL_DISPLAY="egl",
        )
    return environment


def verify_windows_backend(environment):
    """Prove isolation and real software GL before creating a Minecraft JVM."""
    import windows_headless

    HOME.mkdir(parents=True, exist_ok=True)
    output = HOME / "windows-backend.out"
    probe = Path(__file__).with_name("windows_headless_probe.py")
    renderer = Path(environment["BF_HEADLESS_JAVA"]).with_name("opengl32.dll")
    with output.open("w", encoding="utf-8") as log:
        process = windows_headless.launch(
            [sys.executable, "-I", "-X", "utf8", str(probe), "--wgl", str(renderer)],
            HOME,
            environment,
            log,
            process_owner(),
        )
    try:
        if process.wait(timeout=30):
            die(
                f"isolated Windows renderer proof failed; see {output}; desktop fallback is forbidden"
            )
        proof = json.loads(output.read_text(encoding="utf-8"))
        if (
            proof.get("station") != process.backend["station"]
            or proof.get("desktop") != process.backend["desktop"]
            or proof.get("visible") is not False
            or "llvmpipe" not in proof.get("gl", {}).get("renderer", "").lower()
        ):
            die("isolated Windows renderer identity is not verified")
        (HOME / "windows-backend.json").write_text(
            json.dumps(
                {"owner": process_owner(), "identity": process.identity, **proof},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        if process.poll() is None:
            process.terminate()
        process.close()


def client_restart(game_dir):
    """Restart client using the saved launch command from play."""
    if not CLIENT_CMD_FILE.is_file():
        warn(
            "no saved client launch command found: restart client with `just play --mod --debug`"
        )
        return
    try:
        info = json.loads(CLIENT_CMD_FILE.read_text(encoding="utf-8"))
        command = info["command"]
        command[0] = java_exe()
        check_agent_server(
            next(
                (
                    arg.split("=", 1)[1]
                    for arg in command
                    if arg.startswith("--quickPlayMultiplayer=")
                ),
                None,
            )
        )
        cwd = Path(info.get("game_dir", str(game_dir)))
        headless = info.get("headless", False) or not process_owner().startswith(
            "developer:"
        )
        if headless:
            patch_headless_options(cwd)
        pid, _ = start_process(
            command,
            cwd,
            CLIENT_PID_FILE,
            HOME / "client.out",
            env=client_environment(headless, info.get("gpu", False)),
            headless=headless,
        )
        wait_client_ready(pid)
        ok(f"restarted client (pid {pid})")
    except (OSError, KeyError, json.JSONDecodeError) as e:
        warn(f"failed to restart client ({e}): launch with `just play --mod --debug`")


def server_rcon(args):
    try:
        client = Rcon(port=RCON_PORT, pw=RCON_PASSWORD)
    except OSError:
        die("RCON is not answering: is the server running?")
    for command in args.command:
        print(client.cmd(command))


def server_log(args):
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        die(f"no log yet: {LOG}")
    print("\n".join(lines[-args.lines :]))


def available_memory_gib():
    if sys.platform.startswith("linux"):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return round(int(line.split()[1]) / 1024**2, 1)
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total_physical",
                    "available_physical",
                    "total_page_file",
                    "available_page_file",
                    "total_virtual",
                    "available_virtual",
                    "available_extended_virtual",
                )
            ]

        value = MemoryStatus()
        value.length = ctypes.sizeof(value)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
            return round(value.available_physical / 1024**3, 1)
        return None
    try:
        return round(
            os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3, 1
        )
    except (OSError, ValueError):
        return None


def status(_args, as_json=False):
    data = {
        "stack": str(HOME),
        "server_pid": server_pid(),
        "server_port_open": port_open(PORT),
        "memory_available_gib": available_memory_gib(),
        "clients": {
            name: port_open(port)
            for name, port in (("1", BOT_PORTS[1]), ("2", BOT_PORTS[2]))
        },
    }
    if as_json or not _tty():
        print(json.dumps(data, indent=2))
        return
    print(_bold("Blockfield dev stack"))
    print(f"  {_dim('dir:')}    {data['stack']}")
    if data["memory_available_gib"] is not None:
        print(f"  {_dim('free RAM:')} {data['memory_available_gib']:.1f} GiB")
    running = data["server_pid"] is not None
    state = _green("running") if running else _dim("stopped")
    extra = f" {_dim('(pid ' + str(data['server_pid']) + ')')}" if running else ""
    print(f"  {_dim('server:')} 127.0.0.1:{PORT} {state}{extra}")
    for name, up in data["clients"].items():
        dot = _green(_sym()["dot"]) if up else _dim("○")
        label = _green("up") if up else _dim("down")
        print(
            f"  {_dim(f'bot {name}:')} {dot} {label} {_dim(f'(port {BOT_PORTS[int(name)]})')}"
        )


# ── map-maker tools ────────────────────────────────────────────────────────────────────────────

# Not part of the pack: installed into a dev game directory only, so a map maker gets an editor and a
# profiler without the published client changing. Latest 1.21.1 Fabric build of each, hash-checked.
DEV_TOOLS = ("worldedit", "litematica", "malilib", "spark")
MODRINTH = "https://api.modrinth.com/v2/project/{}/version?game_versions=%5B%221.21.1%22%5D&loaders=%5B%22fabric%22%5D"


def modrinth_file(slug):
    with urllib.request.urlopen(
        MODRINTH.format(urllib.parse.quote(slug)), timeout=30
    ) as response:
        versions = json.load(response)
    for version in versions:
        for file in version.get("files", []):
            if file.get("primary") and file["filename"].endswith(".jar"):
                return file["filename"], file["url"], file["hashes"]["sha512"]
    die(f"no 1.21.1 Fabric build of {slug} on Modrinth")


def install_tools(game_dir, remove=False):
    marker = game_dir / "mods" / "devtools.json"
    installed = json.loads(marker.read_text()) if marker.is_file() else []
    for name in installed:
        (game_dir / "mods" / name).unlink(missing_ok=True)
    marker.unlink(missing_ok=True)
    if remove:
        ok(f"removed {len(installed)} dev tools")
        return
    names = []
    for slug in DEV_TOOLS:
        filename, url, sha512 = modrinth_file(slug)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if hashlib.sha512(data).hexdigest() != sha512:
            die(f"{filename}: checksum mismatch, refusing to install")
        (game_dir / "mods" / filename).write_bytes(data)
        names.append(filename)
        print(f"  {filename}")
    marker.write_text(json.dumps(names, indent=2))
    ok(f"installed {len(names)} dev tools into {game_dir / 'mods'}")


# ── workspace client settings ────────────────────────────────────────────────────────────────────────

DEV_SETTINGS = "menu.vanilla=true\n"


def patch_headless_options(game_dir):
    # Fullscreen changes physical monitor modes even on a hidden Windows desktop.
    path = game_dir / "options.txt"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    lines = [
        line
        for line in lines
        if line.split(":", 1)[0] not in ("fullscreen", "pauseOnLostFocus")
    ]
    path.write_text(
        "\n".join([*lines, "fullscreen:false", "pauseOnLostFocus:false"]) + "\n",
        encoding="utf-8",
    )


def patch_client_settings(game_dir):
    settings = game_dir / "config" / "blockfield-settings.properties"
    settings.parent.mkdir(parents=True, exist_ok=True)
    text = settings.read_text(encoding="utf-8") if settings.is_file() else ""
    # The final property wins, including files with a commented or disabled dev flag.
    lines = text.splitlines()
    if not lines or lines[-1] != DEV_SETTINGS.strip():
        settings.write_text(
            text.rstrip("\r\n") + ("\n" if text else "") + DEV_SETTINGS,
            encoding="utf-8",
        )


def build_client_pack(client_pack, out):
    """Same client build CI runs: Blockfield-client.zip + Blockfield-prism.zip in out/."""
    out.mkdir(parents=True, exist_ok=True)
    run_pack_build(
        [
            "--root",
            client_pack,
            "--out",
            out,
        ],
        cwd=client_pack,
        side="client",
    )


# ── client ─────────────────────────────────────────────────────────────────────────────────────


def default_game_dir():
    return HOME / "play"


def client_prepare(args):
    game_dir = Path(args.game_dir) if args.game_dir else default_game_dir()
    if not game_dir.is_dir():
        die(
            f"{game_dir} does not exist: run `just setup` to install the workspace client, "
            f"or pass --game-dir"
        )
    if client_pid():
        die("stop this stack's client before preparing its files")
    patch_client_settings(game_dir)
    (game_dir / "mods").mkdir(parents=True, exist_ok=True)
    install_testbot(args, game_dir)
    if args.tools or args.no_tools:
        install_tools(game_dir, remove=args.no_tools)
    install_client_localmods(game_dir, CLIENT_PACK)
    install_client_mod(game_dir, args.mod, args.mod_repo)
    ok(f"workspace client ready: {game_dir}; start with `just play --game-dir <path>`")


# ── temp/: session logs (gitignored, rotated) ─────────────────────────────────────────

TEMP_KEEP = 3


def temp_dir():
    path = ROOT / "temp"
    path.mkdir(exist_ok=True)
    return path


def stamp():
    return time.strftime("%Y%m%d-%H%M%S")


def rotate_temp(keep=TEMP_KEEP):
    """Keep the newest `keep` session prefixes (YYYYMMDD-HHMMSS-*); drop older ones."""
    try:
        entries = sorted(
            temp_dir().glob("*-*"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return
    seen, doomed = set(), []
    for entry in entries:
        prefix = entry.name[:15]
        if prefix in seen:
            continue
        seen.add(prefix)
        if len(seen) > keep:
            doomed.append(prefix)
    for prefix in doomed:
        for entry in temp_dir().glob(f"{prefix}*"):
            try:
                entry.unlink() if entry.is_file() else shutil.rmtree(entry)
            except OSError:
                pass


def snapshot_logs(tag, game_dir):
    """Copy server + client logs and crash-reports into temp/<tag>-logs/ (logs trimmed to 2k lines)."""
    dest = temp_dir() / f"{tag}-logs"
    dest.mkdir(exist_ok=True)
    if LOG.is_file():
        shutil.copy2(LOG, dest / "server.out")
    game_dir = Path(game_dir)
    client_log = game_dir / "logs" / "latest.log"
    if client_log.is_file():
        lines = client_log.read_text(encoding="utf-8", errors="replace").splitlines()
        (dest / "client-latest.log").write_text(
            "\n".join(lines[-2000:]) + "\n", encoding="utf-8"
        )
    reports = game_dir / "crash-reports"
    if reports.is_dir() and any(reports.iterdir()):
        shutil.copytree(reports, dest / "crash-reports", dirs_exist_ok=True)
    return dest


# ── play: direct client launch, no launcher ────────────────────────────────────────────


def pmc_python():
    """Own venv with the same Python as this command and the pinned portablemc."""
    venv = (
        HOME
        / "pmc"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    valid = False
    if venv.is_file():
        try:
            probe = subprocess.run(
                [
                    str(venv),
                    "-I",
                    "-c",
                    "import sys; from importlib.metadata import version; "
                    "print(sys.version.split()[0], version('portablemc'))",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            valid = probe.returncode == 0 and probe.stdout.strip() == (
                f"{sys.version.split()[0]} 4.4.1"
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if not valid:
        HOME.mkdir(parents=True, exist_ok=True)
        run(
            [sys.executable, "-I", "-m", "venv", "--clear", HOME / "pmc"],
            message="Creating portablemc environment",
            capture_output=True,
            text=True,
        )
        run(
            [
                str(venv),
                "-I",
                "-m",
                "pip",
                "--disable-pip-version-check",
                "-q",
                "install",
                "portablemc==4.4.1",
            ],
            message="Installing portablemc 4.4.1",
            capture_output=True,
            text=True,
        )
    return str(venv)


def prepare_client(
    args, *, force=False, repair=False, sync_marker=False, name="PlayerSetup"
):
    """Install client files and assets, returning a command without launching Java."""
    java_exe()
    game_dir = (
        Path(args.game_dir) if getattr(args, "game_dir", None) else default_game_dir()
    )
    client_pack = modpack_dir(
        getattr(args, "client_modpack", getattr(args, "modpack", None)), side="client"
    )
    if (
        force
        or repair
        or not (
            (game_dir / "mods").is_dir()
            and (game_dir / "config/blockfield-pack-version.txt").is_file()
        )
    ):
        HOME.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="client-pack-", dir=HOME) as temporary:
            dist = Path(temporary)
            build_client_pack(client_pack, dist)
            refresh = force
            if repair:
                server_build = json.loads(
                    (SERVER / "build.json").read_text(encoding="utf-8")
                )
                client_build = json.loads(
                    (dist / "web/build.json").read_text(encoding="utf-8")
                )
                if server_build["compatibility"] != client_build["compatibility"]:
                    die(f"client pack differs from this server; {REFRESH_STACK}")
                receipt = game_dir / "build.json"
                if not receipt.is_file():
                    refresh = True
                elif not force:
                    installed = json.loads(receipt.read_text(encoding="utf-8"))
                    if installed.get("client") != client_build["client"]:
                        die(f"client pack changed since `just setup`; {REFRESH_STACK}")
            with (
                progress("Installing client files"),
                zipfile.ZipFile(dist / "Blockfield-client.zip") as source,
            ):
                bot = game_dir / "mods/bftestbot.jar"
                kept_bot = bot.read_bytes() if refresh and bot.is_file() else None
                if refresh:
                    for directory in (
                        "mods",
                        "config",
                        "defaultconfigs",
                        "tacz",
                        "resourcepacks",
                        "shaderpacks",
                    ):
                        shutil.rmtree(game_dir / directory, ignore_errors=True)
                for item in source.infolist():
                    top = item.filename.split("/", 1)[0]
                    if top in ("logs", "screenshots", "saves") or (
                        top == "options.txt" and (game_dir / top).exists()
                    ):
                        continue
                    if refresh or not (game_dir / item.filename).exists():
                        with tempfile.TemporaryDirectory(dir=HOME) as unpacked:
                            path = Path(source.extract(item, unpacked))
                            target = game_dir / path.relative_to(unpacked)
                            if item.is_dir():
                                target.mkdir(parents=True, exist_ok=True)
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                materialize(path, target)
                if kept_bot:
                    # A refresh keeps the chosen kit; only --no-testbot removes the bot.
                    bot.write_bytes(kept_bot)
            shutil.copy2(dist / "web/build.json", game_dir / "build.json")
    patch_client_settings(game_dir)
    if sync_marker:
        install_client_localmods(game_dir, client_pack)
    install_client_mod(
        game_dir,
        getattr(args, "mod", False),
        getattr(args, "mod_repo", None),
        sync_marker=sync_marker,
    )
    testbot_port = install_testbot(args, game_dir)
    return game_dir, client_launch_command(
        args, game_dir, name, client_pack, testbot_port
    )


def client_natives_dir():
    directory = HOME / "play-natives"
    if sys.platform == "linux" and any(c.isspace() for c in str(directory.resolve())):
        # libffi parses /proc/maps paths with %s, truncating names at whitespace.
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        ).resolve()
        try:
            identity = runtime.stat()
        except OSError as error:
            die(f"native libraries require a private runtime directory: {error}")
        if (
            not runtime.is_dir()
            or identity.st_uid != os.getuid()
            or identity.st_mode & 0o077
            or any(c.isspace() for c in str(runtime))
        ):
            die(
                "native libraries require a private runtime directory without whitespace"
            )
        key = hashlib.sha256(
            f"{HOME.resolve()}\0{process_owner()}".encode()
        ).hexdigest()[:24]
        directory = runtime / f"blockfield-natives-{key}"
    return directory


def client_launch_command(args, game_dir, name, client_pack, testbot_port):
    versions = tomllib.loads((client_pack / "pack.toml").read_text(encoding="utf-8"))[
        "versions"
    ]
    python = pmc_python()
    cached_mc = (
        INPUT_CACHE
        / "minecraft"
        / (f"{versions['minecraft']}-{versions['fabric']}-{sys.platform}")
    )
    if not (HOME / "mc").exists() and cached_mc.is_dir():
        # Clone via a temporary name so an interrupted copy is never mistaken
        # for a complete snapshot on the next run.
        with tempfile.TemporaryDirectory(dir=HOME) as temporary:
            pending = Path(temporary) / "mc"
            clone_tree(cached_mc, pending)
            try:
                pending.rename(HOME / "mc")
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    command = json.loads(
        run(
            [
                python,
                "-I",
                "-X",
                "utf8",
                Path(__file__).resolve().parent / "pmc_launch.py",
                "--main-dir",
                HOME / "mc",
                "--game-dir",
                game_dir,
                "--natives-dir",
                client_natives_dir(),
                "--username",
                name,
                "--minecraft",
                versions["minecraft"],
                "--fabric",
                versions["fabric"],
                "--xmx",
                getattr(args, "xmx", "4G"),
                "--resolution",
                getattr(args, "resolution", "1280x720"),
                *([f"--testbot-port={testbot_port}"] if testbot_port else []),
                *(
                    [f"--debug-port={args.debug_port}"]
                    if getattr(args, "debug", False)
                    else []
                ),
                *([f"--server={args.server}"] if getattr(args, "server", None) else []),
            ],
            message="Preparing Minecraft, Fabric, assets and libraries",
            capture_output=True,
            text=True,
        ).stdout
    )
    if (HOME / "mc").is_dir() and not cached_mc.exists():
        cached_mc.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cached_mc.parent) as temporary:
            pending = Path(temporary) / "mc"
            clone_tree(HOME / "mc", pending)
            try:
                pending.rename(cached_mc)
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    java = command[0]
    if (
        Path(java).name in ("java", "java.exe")
        and len(Path(java).parts) < 3
        and java_exe()
    ):
        command[0] = java_exe()
    return command


def check_agent_server(server):
    local = (server or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
    if not local or process_owner().startswith("developer:"):
        return
    record = process_record(PID_FILE)
    if (
        not record
        or not record.get("live")
        or record.get("native") not in ("linux", "windows")
        or record.get("owner") != process_owner()
        or server.rsplit(":", 1)[1] != str(PORT)
        or not listener_owned(record["pid"], PORT)
    ):
        die(
            "agent client requires its own local server; use --server '' for menu-only checks"
        )


def play(args):
    if getattr(args, "released_pack", False) and not getattr(args, "probe", False):
        die("--released-pack requires --probe")
    if getattr(args, "probe", False):
        if not args.headless:
            die("--probe requires --headless")
        from headless_check import run as headless_check

        headless_check(args)
        return
    if getattr(args, "stop", False):
        client_stop()
        return
    if client_pid():
        die("this stack's client is already running: stop it before changing its files")
    game_dir = Path(args.game_dir) if args.game_dir else default_game_dir()
    ports = (
        [int(args.testbot_port or "47777")] if testbot_enabled(args, game_dir) else []
    )
    if args.debug:
        ports.append(int(args.debug_port))
    if any(port_open(port) for port in ports):
        die(
            "client testbot/debug port is busy: inspect the existing client before starting another"
        )
    agent = not process_owner().startswith("developer:")
    headless = getattr(args, "headless", False) or agent
    name = args.name or f"Player{random.randint(1000, 9999)}"
    # The local server owns its marker; remote play keeps the archive's marker.
    local = (args.server or "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost")
    check_agent_server(args.server)
    game_dir, command = prepare_client(args, sync_marker=local, name=name)
    if headless:
        patch_headless_options(game_dir)
    gpu = getattr(args, "gpu", None)
    if gpu and not headless:
        die("--gpu applies to the headless client only")
    if gpu is None:
        # Software Mesa costs ~2.3 cores for a third of the frame rate, so a usable render
        # node wins by default; the private Xvfb stays the only display either way.
        gpu = headless and gpu_render_node()
    environment = client_environment(headless, gpu)
    step(f"starting as {_bold(name)} in {game_dir}")
    CLIENT_CMD_FILE.write_text(
        json.dumps(
            {
                "command": [str(c) for c in command],
                "game_dir": str(game_dir),
                "name": name,
                "headless": headless,
                "gpu": gpu,
            }
        )
    )
    pid, process = start_process(
        [str(c) for c in command],
        game_dir,
        CLIENT_PID_FILE,
        HOME / "client.out",
        env=environment,
        headless=headless,
    )
    if process is None:
        wait_client_ready(pid)
        ok(f"client renderer ready (pid {pid}); log: {HOME / 'client.out'}")
        return
    try:
        code = process.wait()
    finally:
        CLIENT_PID_FILE.unlink(missing_ok=True)
    logs = snapshot_logs(f"{stamp()}-{name}", game_dir)
    rotate_temp()
    ok(f"saved {logs.name}/")
    raise SystemExit(code)


def wait_client_ready(pid):
    log = HOME / "client.out"
    deadline = time.monotonic() + 360
    with (
        progress("Starting Minecraft client"),
        log.open(encoding="utf-8", errors="replace") as source,
    ):
        pending = ""
        while time.monotonic() < deadline:
            if client_pid() != pid:
                die(f"client exited before its renderer was ready, see {log}")
            pending += source.read()
            if re.search(
                r"Created: \d+x\d+(?:x\d+)? minecraft:textures/atlas/gui\.png-atlas",
                pending,
            ):
                if client_pid() != pid:
                    die(f"client exited before its renderer was ready, see {log}")
                return
            pending = pending[-512:]
            time.sleep(0.5)
        die(f"client renderer was not ready in 6 minutes; inspect pid {pid} and {log}")


def process_runtime_matches(record, directory):
    try:
        if Path(record["cwd"]).resolve() != directory.resolve():
            return False
        if sys.platform.startswith("linux"):
            proc = Path("/proc") / str(record["pid"])
            started = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
            return (
                started == str(record["start_ticks"])
                and proc.joinpath("cwd").resolve(strict=True) == directory.resolve()
            )
        return sys.platform == "win32"
    except (OSError, KeyError, TypeError, ValueError, IndexError):
        return False


def listener_owned(pid, port, peer_port=None):
    if sys.platform == "win32":
        import windows_process

        if peer_port is None:
            return windows_process.owns_listener(pid, port)
        return windows_process.owns_listener(pid, port, peer_port=peer_port)
    if not sys.platform.startswith("linux"):
        return False
    try:
        sockets = set()
        for fd in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                sockets.add(os.readlink(fd))
            except FileNotFoundError:
                continue
        for family in ("tcp", "tcp6"):
            table = Path("/proc") / str(pid) / "net" / family
            if not table.exists():
                continue
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                if (
                    fields[3] == ("0A" if peer_port is None else "01")
                    and int(fields[1].rsplit(":", 1)[1], 16) == port
                    and (
                        peer_port is None
                        or int(fields[2].rsplit(":", 1)[1], 16) == peer_port
                    )
                    and f"socket:[{fields[9]}]" in sockets
                ):
                    return True
    except (OSError, ValueError, IndexError):
        pass
    return False


def watch(args):
    """Hot reload one or both owned JVMs, with serial builds and restarts."""
    from watch import CLIENT_JDWP, SERVER_JDWP, WatchError, deploy_built_jar, watch_loop

    side = args.side or args.pos_side or "server"
    if side == "both" and args.port:
        die("just watch both uses ports 5005/5006; --port selects a single side")
    sides = ("server", "client") if side == "both" else (side,)
    ports = {"server": SERVER_JDWP, "client": CLIENT_JDWP}
    if args.port:
        ports[side] = int(args.port)
    game_dir = HOME / "play" if args.game_dir is None else Path(args.game_dir)
    paths = {"server": PID_FILE, "client": CLIENT_PID_FILE}
    directories = {"server": SERVER, "client": game_dir}
    targets = {}

    def guard(_side, restarting=None):
        for selected in sides:
            if selected == restarting:
                continue
            record = process_record(paths[selected])
            if (
                not record
                or not record.get("live")
                or record.get("native") not in ("linux", "windows")
                or record.get("owner") != process_owner()
            ):
                die(
                    "watch requires a running process registered to the current session"
                )
            if not process_runtime_matches(record, directories[selected]):
                die(
                    f"watch {selected}: process birth or runtime directory does not match"
                )
            identity = (
                record["pid"],
                record.get("birth"),
                record.get("start_ticks"),
            )
            if selected in targets and identity != targets[selected]:
                die(
                    f"watch {selected}: process identity changed; start the watcher again"
                )
            if not listener_owned(record["pid"], ports[selected]):
                die(
                    f"watch {selected}: JDWP listener is not owned by the recorded process"
                )
            targets[selected] = identity

    guard(side)
    java_exe()
    step(f"watching {side} sources (JDWP {ports}, Ctrl+C to stop)")

    def restart(selected, built):
        guard(selected)
        warn(f"restarting the {selected} with the rebuilt jar")
        if selected == "server":
            server_stop(args)
        else:
            client_stop()
        guard(selected, restarting=selected)
        deploy_built_jar(selected, built, SERVER / "mods", game_dir)
        if selected == "server":
            server_start(
                argparse.Namespace(
                    xms="1G", xmx="3G", debug=True, debug_port=ports[selected]
                )
            )
        else:
            client_restart(game_dir)
        record = process_record(paths[selected])
        if (
            not record
            or not record.get("live")
            or record.get("owner") != process_owner()
            or not process_runtime_matches(record, directories[selected])
        ):
            die(f"watch {selected}: restarted process did not stay running")
        targets[selected] = (
            record["pid"],
            record.get("birth"),
            record.get("start_ticks"),
        )
        deadline = time.monotonic() + 30
        while not listener_owned(record["pid"], ports[selected]):
            current = process_record(paths[selected])
            if (
                not current
                or not current.get("live")
                or current.get("pid") != record["pid"]
                or current.get("owner") != process_owner()
                or not process_runtime_matches(current, directories[selected])
            ):
                die(f"watch {selected}: restarted process exited before JDWP was ready")
            if time.monotonic() >= deadline:
                die(f"watch {selected}: JDWP was not ready within 30 seconds")
            time.sleep(0.1)
        guard(selected)

    def iteration_identity(selected):
        manifest = (
            SERVER / "build.json" if selected == "server" else game_dir / "build.json"
        )
        return {
            "process": targets[selected],
            "runtime_manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest()
            if manifest.is_file()
            else None,
        }

    try:
        watch_loop(
            side,
            ports.get(side),
            SERVER / "mods",
            game_dir,
            poll=args.poll,
            once=args.once,
            guard=guard,
            restart=restart,
            receipt_path=HOME / "watch-receipt.json",
            identity=iteration_identity,
        )
    except WatchError as error:
        die(str(error))
    except KeyboardInterrupt:
        print("\nwatch stopped")


def scenario(args):
    """just scenario <name>: ported rig checks on top of RCON + the testbot (see scenarios/)."""
    from scenarios import SCENARIOS

    name = getattr(args, "name", None)
    if not name:
        for module_name, module in SCENARIOS.items():
            print(f"  {module_name:<15} {module.SCENARIO['help']}")
        return
    return SCENARIOS[name].run(args)


def bot(args):
    port = BOT_PORTS[args.client]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall((" ".join(args.command) + "\n").encode("utf-8"))
            sock.settimeout(args.timeout)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as error:
        die(
            f"testbot on 127.0.0.1:{port} is not answering ({error}): is the client running with "
            f"-Dbf.testbot.port={port} and bftestbot.jar in mods?"
        )
    print(data.decode("utf-8", "replace").strip())


COMMANDS = {
    "capture": "record a bounded game scenario; --video saves a portable MP4",
    "setup": "prepare server, client and Java; --testbot adds the optional bot",
    "build-mods": "build localmods/ sources (all, or the named submodules)",
    "build-testbot": "build the workspace testbot jar",
    "mod": "install a locally built blockfield-server jar",
    "watch": "rebuild + hotswap localmods/ sources into the running stack (server|client)",
    "server": "run the local server (start|stop|rcon|log)",
    "client": "prepare the workspace client directory",
    "play": "launch the client directly, no launcher, random offline name",
    "bot": "send a command to the testbot inside a running client",
    "scenario": "run a ported rig scenario (parkour/hitreg/stress/dm/range/check-*)",
    "status": "what is running",
    "complete": "shell completion (bash/zsh/powershell)",
    "check": "run all code checks",
    "lint": "check code and workflows",
    "format-check": "check formatting",
    "format": "format code and documentation",
    "test": "run automated tests",
    "compile-check": "check compilation",
    "dev": "developer commands, including prune, game-job and completion",
    "mcp": "start the stdio developer or game MCP tools",
    "assets": "manage pinned Blockfield assets",
    "rclone": "run the project-local rclone",
}


def print_help():
    print(f"\n{_bold('Blockfield')}  {_dim('Local development')}\n")
    print("usage: just <recipe> [options]")
    groups = {
        "Prepare": ("setup", "build-mods", "build-testbot", "client", "mod"),
        "Run": ("server", "play", "watch"),
        "Inspect": ("status", "bot", "scenario"),
        "Quality": ("check", "lint", "format-check", "format", "test", "compile-check"),
        "Tools": ("dev", "mcp", "assets", "rclone"),
    }
    width = max(len("just " + name) for names in groups.values() for name in names)
    for title, names in groups.items():
        print(f"\n{_bold(title)}")
        for name in names:
            lines = textwrap.wrap(
                COMMANDS[name],
                width=max(20, shutil.get_terminal_size((80, 24)).columns - width - 4),
            )
            print(f"  {_cyan(('just ' + name).ljust(width))}  {lines[0]}")
            for line in lines[1:]:
                print(f"{' ' * (width + 4)}{line}")
    print(f"\n{_dim('Start here')}  just setup → just server start → just play")
    print(
        "\njust <recipe> --help for options; just --completions <shell> for shell completion"
    )


def print_completion(args):
    """Forward completion generation to Just's native implementation."""
    if len(args) == 1 and args[0] in (
        "bash",
        "elvish",
        "fish",
        "nushell",
        "powershell",
        "zsh",
    ):
        subprocess.run([native("just"), "--completions", args[0]], check=True)
    else:
        print("usage: just --completions <bash|elvish|fish|nushell|powershell|zsh>")


def capture(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent / "mcp"))
    import game

    actions = []
    if args.actions:
        path = Path(args.actions)
        if path.stat().st_size > 65536:
            die("capture action file exceeds 64 KiB")
        actions = json.loads(path.read_text(encoding="utf-8"))
    arguments = {
        "client": args.client,
        "label": args.label,
        "seconds": args.seconds,
        "fps": args.fps,
        "actions": actions,
    }
    game.validate(arguments, game.TOOLS["bf_capture"][2])
    selected = bool(
        os.environ.get("BF_GAME_JOB")
        or os.environ.get("BF_HARNESS_JOB")
        or (HOME / "current-job").is_file()
    )
    config = game.job()[1] if selected else {}
    game.capture_actions(arguments, config)
    encoder = None
    if args.video:
        from media import encoder_info, ffmpeg

        encoder = ffmpeg()
        encoder_info(encoder)
    if not selected:
        with redirect_stdout(sys.stderr):
            game_job(
                argparse.Namespace(name="capture-" + time.strftime("%Y%m%d-%H%M%S"))
            )
    result = game.TOOLS["bf_capture"][0](arguments)
    metadata = json.loads(result["content"][0]["text"])
    if result.get("isError"):
        die(json.dumps(metadata, ensure_ascii=False))
    if encoder:
        from media import encode_capture

        metadata["video"] = str(encode_capture(metadata["manifest"], encoder))
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


def game_job(args):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", args.name):
        die("job name must contain 1–64 lowercase letters, digits, - or _")
    pointer = HOME / "current-job"
    if pointer.is_file():
        previous = Path(pointer.read_text(encoding="utf-8").strip())
        config_file = previous / "job.json"
        if config_file.is_file() and not (previous / "finished").exists():
            config = json.loads(config_file.read_text(encoding="utf-8"))
            if config.get("owner") != process_owner():
                die("the active game job belongs to another session")
    directory = HOME / "jobs" / args.name
    directory.mkdir(parents=True)
    config = {"owner": process_owner(), "allow_teleport": False}
    client2_home = getattr(args, "client2_home", None)
    if client2_home:
        # Stand drivers used to hand-write clients.2.home into job.json (bot client 2
        # refuses to start without its own registered stack home); record it here.
        home = Path(client2_home).expanduser().resolve()
        if not home.is_dir():
            die(f"client2 home does not exist: {home}")
        config["clients"] = {"2": {"home": str(home)}}
    (directory / "job.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    pointer.write_text(str(directory.resolve()) + "\n", encoding="utf-8")
    ok(f"game job: {directory}; MCP: just mcp --game")


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print_help()
        return
    if sys.argv[1] == "complete":
        print_completion(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(
        prog="just dev",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, prog="just")

    p = sub.add_parser("setup", help=COMMANDS["setup"])
    p.add_argument("--modpack", help="server pack checkout (default server/)")
    p.add_argument("--client-modpack", help="client pack checkout (default client/)")
    p.add_argument("--game-dir", help="client game directory (default .dev/play)")
    testbot_options = p.add_mutually_exclusive_group()
    testbot_options.add_argument(
        "--testbot",
        nargs="?",
        const=True,
        metavar="JAR",
        help="build and install testbot, or install the given JAR (default: keep the installed one)",
    )
    testbot_options.add_argument(
        "--no-testbot", action="store_true", help="remove an installed testbot"
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="refresh runtime files, preserving worlds and client user data",
    )
    p.set_defaults(func=setup)

    p = sub.add_parser("mod", help="install a locally built blockfield-server jar")
    p.add_argument("jar", nargs="?")
    p.add_argument("--mod-repo", default=None)
    p.set_defaults(func=install_mod)

    p = sub.add_parser(
        "build-mods", help="build localmods/ sources (all, or the named submodules)"
    )
    p.add_argument("names", nargs="*")
    p.add_argument(
        "--plan",
        action="store_true",
        help="print the pin/local source per mod without building",
    )
    p.set_defaults(func=build_mods)

    p = sub.add_parser(
        "build-testbot", help="build the workspace testbot jar (scripts/dev/testbot)"
    )
    p.set_defaults(func=build_testbot)

    p = sub.add_parser("server", help="run the local server")
    server_sub = p.add_subparsers(dest="action", required=True)
    start = server_sub.add_parser("start")
    start.add_argument("--xmx", default="3G")
    start.add_argument("--xms", default="1G")
    start.add_argument(
        "--debug", action="store_true", help="JDWP on 127.0.0.1:5005 for hotswap"
    )
    start.add_argument(
        "--gate",
        action="store_true",
        help="keep the production login gate (pack contract/hash) instead of the dev bypass",
    )
    start.add_argument("--pose-debug", action="store_true")
    start.set_defaults(func=server_start)
    server_sub.add_parser("stop").set_defaults(func=server_stop)
    rcon_parser = server_sub.add_parser("rcon")
    rcon_parser.add_argument("command", nargs="+")
    rcon_parser.set_defaults(func=server_rcon)
    log_parser = server_sub.add_parser("log")
    log_parser.add_argument("lines", nargs="?", type=int, default=40)
    log_parser.set_defaults(func=server_log)

    p = sub.add_parser("client", help="prepare the workspace client directory")
    p.add_argument("--game-dir")
    p.add_argument("--client", type=int, choices=(1, 2), default=1)
    p.add_argument("--testbot", nargs="?", const=True, metavar="JAR")
    p.add_argument("--mod-repo", default=None)
    p.add_argument("--no-testbot", action="store_true")
    p.add_argument(
        "--mod",
        action="store_true",
        help="also install the locally built blockfield-client jar",
    )
    p.add_argument(
        "--tools",
        action="store_true",
        help=f"install map-maker tools ({', '.join(DEV_TOOLS)})",
    )
    p.add_argument(
        "--no-tools", action="store_true", help="remove the map-maker tools again"
    )
    p.set_defaults(func=client_prepare)

    p = sub.add_parser(
        "play", help="launch the client directly, no launcher, random offline name"
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="verify the isolated Minecraft renderer, capture and restart without a private pack",
    )
    p.add_argument(
        "--released-pack",
        action="store_true",
        help="run --probe with the checksum-pinned full released Blockfield client pack",
    )
    p.add_argument("--stop", action="store_true", help="stop this session's own client")
    p.add_argument("--server", default=f"127.0.0.1:{PORT}")
    p.add_argument("--name", default=None, help="offline nickname (default PlayerNNNN)")
    p.add_argument("--game-dir", default=None)
    p.add_argument("--modpack")
    p.add_argument("--mod-repo", default=None)
    p.add_argument("--xmx", default="4G")
    p.add_argument("--resolution", default="1280x720")
    p.add_argument(
        "--headless",
        action="store_true",
        help="isolated virtual display (mandatory for agents)",
    )
    p.add_argument(
        "--gpu",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="render the headless client on the host GPU through VirtualGL (Linux; "
        "default: when a /dev/dri/renderD* node is usable; --no-gpu forces software Mesa)",
    )
    p.add_argument(
        "--testbot-port",
        default=None,
        help="enable testbot on this TCP port (default when installed: 47777)",
    )
    p.add_argument(
        "--testbot",
        nargs="?",
        const=True,
        metavar="JAR",
        help="build and install testbot, or install the given JAR",
    )
    p.add_argument("--no-testbot", action="store_true", help="do not enable testbot")
    p.add_argument(
        "--mod", action="store_true", help="use the locally built blockfield-client jar"
    )
    p.add_argument(
        "--debug", action="store_true", help="JDWP on 127.0.0.1:5006 for hotswap"
    )
    p.add_argument("--debug-port", default="5006", help="JDWP port for --debug")
    p.set_defaults(func=play)

    p = sub.add_parser(
        "watch", help="rebuild + hotswap localmods/ sources into the running stack"
    )
    p.add_argument(
        "--side",
        choices=("server", "client", "both"),
        default=None,
        help="server, client or both (default server)",
    )
    p.add_argument(
        "pos_side",
        nargs="?",
        default=None,
        choices=("server", "client", "both"),
        metavar="side",
        help="server, client or both (default server)",
    )
    p.add_argument(
        "--port",
        default=None,
        type=int,
        help="single-side JDWP port override (default: server=5005, client=5006)",
    )
    p.add_argument(
        "--game-dir", default=None, help="client game dir (default .dev/play)"
    )
    p.add_argument(
        "--poll", type=float, default=2.0, help="poll interval in seconds (default 2.0)"
    )
    p.add_argument("--once", action="store_true", help="run a single pass and exit")
    p.set_defaults(func=watch)

    p = sub.add_parser("capture", help=COMMANDS["capture"])
    p.add_argument("--client", type=int, choices=(1, 2), default=1)
    p.add_argument("--label", default="capture")
    p.add_argument("--seconds", type=float, default=3)
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--actions", help="JSON array of scheduled bf_* actions")
    p.add_argument(
        "--video", action="store_true", help="encode MP4 with pinned libx264"
    )
    p.set_defaults(func=capture)

    p = sub.add_parser(
        "bot", help="send a command to the testbot inside a running client"
    )
    p.add_argument("--client", type=int, choices=(1, 2), default=1)
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("command", nargs="+")
    p.set_defaults(func=bot)

    p = sub.add_parser(
        "scenario", help="run a ported rig scenario (no args lists them)"
    )
    from scenarios import register as register_scenarios

    scenario_sub = p.add_subparsers(dest="name")
    register_scenarios(scenario_sub)
    p.set_defaults(func=scenario)

    p = sub.add_parser("status", help="what is running")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=lambda args: status(args, as_json=args.json))

    p = sub.add_parser(
        "game-job",
        prog="just dev game-job",
        help="create and select an isolated game evidence job",
    )
    p.add_argument("name")
    p.add_argument(
        "--client2-home",
        default=None,
        help="second stack home for bot client 2 (recorded as clients.2.home)",
    )
    p.set_defaults(func=game_job)

    args = parser.parse_args()
    if args.command != "setup":
        HOME.mkdir(parents=True, exist_ok=True)
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
