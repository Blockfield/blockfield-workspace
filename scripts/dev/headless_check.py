"""Finite public Minecraft engine acceptance on an exclusively owned native stack."""

import argparse
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

import devstack
from media import encode_capture, encoder_info, ffmpeg
from mcp import game
from toolchain import download

FABRIC_API = (
    "fabric-api-0.116.15+1.21.1.jar",
    "https://cdn.modrinth.com/data/P7dR8mSH/versions/Nlt8gI9z/fabric-api-0.116.15%2B1.21.1.jar",
    "a61a10f730ab8aa45ff42486ee65699e6e51c28a0c168deb161e7d4029473aa3",
)
RELEASED_PACK = (
    "Blockfield-client-v2.63.0.zip",
    "https://github.com/Blockfield/blockfield-releases/releases/download/v2.63.0/Blockfield-client.zip",
    "edf28a08ec29b853e194d4a27ae892d8125306a4b8031642b573422e864ff33e",
)


def claim_stack():
    home, owner = devstack.HOME.resolve(), devstack.process_owner()
    selected = os.environ.get("BF_DEV_HOME")
    if (
        not selected
        or home != Path(selected).resolve()
        or home == (devstack.ROOT / ".dev").resolve()
    ):
        raise RuntimeError("--probe requires its own explicit BF_DEV_HOME")
    for path in (devstack.PID_FILE, devstack.CLIENT_PID_FILE, home / "display.pid"):
        record = devstack.process_record(path)
        if record and (record.get("live") or record.get("owner") != owner):
            raise RuntimeError(
                f"--probe refuses an existing or foreign process record: {path}"
            )
    ports = (devstack.PORT, devstack.RCON_PORT, *devstack.BOT_PORTS.values())
    if any(devstack.port_open(port) for port in ports):
        raise RuntimeError(
            "--probe needs an unused stand; inspect the existing port owners"
        )
    game_dir = home / "play"
    if any(
        path.is_symlink()
        for path in (
            game_dir,
            game_dir / "mods",
            game_dir / "options.txt",
            game_dir / "mods" / "bftestbot.jar",
            game_dir / "mods" / FABRIC_API[0],
        )
    ):
        raise RuntimeError("--probe refuses symlink game destinations")
    marker = home / "headless-probe-owner.json"
    if marker.exists():
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        if receipt.get("owner") != owner:
            raise RuntimeError("--probe stack belongs to another session")
    else:
        if any(
            path.exists()
            for path in (
                home / "play",
                home / "server",
                devstack.CLIENT_CMD_FILE,
                home / "current-job",
            )
        ):
            raise RuntimeError("--probe cannot adopt an existing game directory")
        home.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(
                {"owner": owner, "minecraft": "1.21.1", "fabric": "0.19.3"}, stream
            )
            stream.write("\n")
    return home, owner


@contextmanager
def phase(result, name):
    started = time.monotonic()
    try:
        yield
    finally:
        result["phases_seconds"][name] = round(time.monotonic() - started, 3)


def extract_released_pack(archive, game_dir):
    root = game_dir.resolve()
    if game_dir.is_symlink():
        raise RuntimeError("released-pack game directory must not be a symlink")
    with zipfile.ZipFile(archive) as source:
        entries, names = [], set()
        for item in source.infolist():
            name = item.filename
            path, windows_path = PurePosixPath(name), PureWindowsPath(name)
            parts = name.rstrip("/").split("/")
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (
                not name
                or item.orig_filename != name
                or "\\" in name
                or ":" in name
                or path.is_absolute()
                or windows_path.drive
                or ".." in path.parts
                or any(
                    part in ("", ".", "..")
                    or part.endswith((".", " "))
                    or PureWindowsPath(part).is_reserved()
                    or any(char in part for char in '<>"|?*')
                    for part in parts
                )
                or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
            ):
                raise RuntimeError(f"unsafe released-pack archive entry: {name!r}")
            target = game_dir.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root):
                raise RuntimeError(
                    "released-pack extraction would leave its game directory"
                )
            for parent in (target, *target.parents):
                if parent.is_symlink():
                    raise RuntimeError(
                        "released-pack extraction refuses symlink destinations"
                    )
                if parent == game_dir:
                    break
            key = "/".join(path.parts).casefold()
            if key in names:
                raise RuntimeError("released-pack contains duplicate portable paths")
            names.add(key)
            entries.append((item, target))
        mods = sum(
            not item.is_dir()
            and PurePosixPath(item.filename).parts[0] == "mods"
            and item.filename.endswith(".jar")
            for item, _ in entries
        )
        if not mods:
            raise RuntimeError("released-pack archive contains no client mods")
        if (game_dir / "mods").exists():
            shutil.rmtree(game_dir / "mods")
        for item, target in entries:
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(item) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
    return {"mods": mods, "files": sum(not item.is_dir() for item, _ in entries)}


def tool_value(name, arguments):
    response = game.TOOLS[name][0](arguments)
    text = next(
        (block["text"] for block in response["content"] if block["type"] == "text"), ""
    )
    if response.get("isError"):
        raise RuntimeError(text or f"{name} failed")
    return json.loads(text)


def screen_kind(state):
    return state.get("screenKind") or state.get("screen")


def wait_menu():
    deadline = time.monotonic() + 45
    token = game.DEADLINE.set(deadline)
    last = None
    try:
        while time.monotonic() < deadline:
            record = devstack.process_record(devstack.CLIENT_PID_FILE)
            if (
                not record
                or not record.get("live")
                or record.get("owner") != devstack.process_owner()
            ):
                raise RuntimeError(
                    "probe client exited or changed owner before its menu was ready"
                )
            if devstack.port_open(devstack.BOT_PORTS[1]):
                last = tool_value("bf_state", {"client": 1})
                if (
                    screen_kind(last) == "TitleScreen"
                    and last.get("connected") is False
                    and last.get("loadingOverlay") is False
                ):
                    return last
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    finally:
        game.DEADLINE.reset(token)
    raise RuntimeError(f"Minecraft title menu was not ready within 45 seconds: {last}")


def verify_backend(state):
    backend = state.get("backend", {})
    record = devstack.process_record(devstack.CLIENT_PID_FILE)
    if (
        not record
        or not record.get("live")
        or record.get("owner") != devstack.process_owner()
        or record.get("native")
        != {"win32": "windows", "linux": "linux"}.get(sys.platform)
        or type(record.get("pid")) is not int
        or record["pid"] <= 0
    ):
        raise RuntimeError("probe client native ownership is not verified")
    version = re.match(r"(\d+)\.(\d+)", str(backend.get("version", "")))
    if (
        backend.get("pid") != record["pid"]
        or backend.get("platform") != sys.platform
        or backend.get("silent") is not True
        or backend.get("renderThread") != "Render thread"
        or not version
        or tuple(map(int, version.groups())) < (3, 2)
        or not backend.get("glsl")
    ):
        raise RuntimeError(
            f"Minecraft render-thread or OpenGL proof is incomplete: {backend}"
        )
    identity = {key: record[key] for key in game.PROCESS_FIELDS if key in record}
    result = {"client": identity, "backend": backend}
    if sys.platform == "win32":
        expected = record.get("backend", {})
        if (
            backend.get("verified") is not True
            or backend.get("visible") is not False
            or backend.get("owner") != devstack.process_owner()
            or backend.get("station") != expected.get("station")
            or backend.get("desktop") != expected.get("desktop")
            or not str(expected.get("station", "")).startswith("Blockfield-")
            or not str(expected.get("desktop", "")).startswith("Blockfield-")
            or "llvmpipe" not in str(backend.get("renderer", "")).lower()
        ):
            raise RuntimeError(
                "Minecraft did not render on its assigned noninteractive Windows backend"
            )
    elif sys.platform == "linux":
        display = devstack.process_record(devstack.HOME / "display.pid")
        number = (devstack.HOME / "display.number").read_text(encoding="utf-8").strip()
        if (
            not display
            or not display.get("live")
            or display.get("owner") != devstack.process_owner()
            or display.get("native") != "linux"
            or type(display.get("pid")) is not int
            or display["pid"] <= 0
            or backend.get("display") != ":" + number
            or backend.get("wayland")
            or Path(backend.get("authority", "")).resolve()
            != (devstack.HOME / "display.auth").resolve()
        ):
            raise RuntimeError(
                "Minecraft did not render on this session's registered Linux virtual display"
            )
        result["display"] = {
            key: display[key] for key in game.PROCESS_FIELDS if key in display
        }
    else:
        raise RuntimeError("native headless acceptance supports Windows and Linux")
    return result


def evidence(label, job, encoder, result):
    screenshot = game.TOOLS["bf_screenshot"][0]({"client": 1, "label": label})
    saved = next(
        (
            block["text"][6:]
            for block in screenshot["content"]
            if block["type"] == "text" and block["text"].startswith("saved ")
        ),
        None,
    )
    if not saved or Path(saved).resolve().parent != (job / "evidence").resolve():
        raise RuntimeError(
            "Minecraft screenshot evidence is missing or outside its game job"
        )
    result["screenshot"] = saved
    _, config = game.job()
    game.bot(config, 1, "screen options")
    before = tool_value("bf_state", {"client": 1})
    result["input"] = {"before": screen_kind(before), "key": "Escape"}
    if (
        screen_kind(before) != "OptionsScreen"
        or before.get("loadingOverlay") is not False
    ):
        raise RuntimeError("probe did not open its own options screen")
    captured = tool_value(
        "bf_capture",
        {
            "client": 1,
            "label": label,
            "seconds": 2,
            "fps": 3,
            "actions": [
                {
                    "at": 0.7,
                    "tool": "bf_raw_input",
                    "arguments": {
                        "client": 1,
                        "op": "key",
                        "key": "Escape",
                    },
                }
            ],
        },
    )
    result["capture"] = captured
    after = tool_value("bf_state", {"client": 1})
    result["input"]["after"] = screen_kind(after)
    if screen_kind(after) != "TitleScreen" or after.get("loadingOverlay") is not False:
        raise RuntimeError(
            "synthetic Escape did not return the isolated client to its title screen"
        )
    video = encode_capture(captured["manifest"], encoder)
    result["video"] = str(video)
    return result


def run(args):
    home, owner = claim_stack()
    game_dir = home / "play"
    result = {
        "complete": False,
        "platform": sys.platform,
        "owner": owner,
        "stack": str(home),
        "minecraft": "1.21.1",
        "fabric": "0.19.3",
        "runs": [],
        "phases_seconds": {},
    }
    previous_job = os.environ.get("BF_GAME_JOB")
    started = time.monotonic()
    error = None
    try:
        with phase(result, "prepare_encoder"):
            encoder = ffmpeg()
            result["encoder"] = encoder_info(encoder)
        with phase(result, "prepare_engine"):
            devstack.java_exe()
            game_dir.mkdir(exist_ok=True)
            mods = game_dir / "mods"
            if getattr(args, "released_pack", False):
                with phase(result, "prepare_released_pack"):
                    installed = extract_released_pack(
                        download(
                            *RELEASED_PACK,
                            cache=devstack.HOME / "mc" / "released-packs",
                        ),
                        game_dir,
                    )
                    if installed["mods"] != 81:
                        raise RuntimeError(
                            "pinned released pack does not contain its expected 81 mods"
                        )
                    result["released_pack"] = {
                        "version": "2.63.0",
                        "url": RELEASED_PACK[1],
                        "sha256": RELEASED_PACK[2],
                        **installed,
                    }
            else:
                mods.mkdir(exist_ok=True)
                for old in mods.iterdir():
                    if old.name not in ("bftestbot.jar", FABRIC_API[0]):
                        raise RuntimeError(
                            "public headless probe contains an unexpected mod"
                        )
                shutil.copy2(download(*FABRIC_API), mods / FABRIC_API[0])
            launch_args = argparse.Namespace(
                testbot=getattr(args, "testbot", None) or True,
                no_testbot=False,
                testbot_port=str(devstack.BOT_PORTS[1]),
                server="",
                debug=False,
                xmx=getattr(args, "xmx", "4G"),
                resolution="640x360",
            )
            devstack.install_testbot(launch_args, game_dir)
            devstack.patch_client_settings(game_dir)
            (game_dir / "options.txt").write_text(
                "onboardAccessibility:false\ntutorialStep:none\nmaxFps:30\nrenderDistance:2\n"
                "simulationDistance:2\nfullscreen:false\npauseOnLostFocus:false\n",
                encoding="utf-8",
            )
            devstack.patch_headless_options(game_dir)
            pack = home / "probe-pack"
            pack.mkdir(exist_ok=True)
            (pack / "pack.toml").write_text(
                '[versions]\nminecraft = "1.21.1"\nfabric = "0.19.3"\n',
                encoding="utf-8",
            )
            command = list(
                map(
                    str,
                    devstack.client_launch_command(
                        launch_args,
                        game_dir,
                        "HeadlessProbe",
                        pack,
                        devstack.BOT_PORTS[1],
                    ),
                )
            )
            devstack.CLIENT_CMD_FILE.write_text(
                json.dumps(
                    {
                        "command": command,
                        "game_dir": str(game_dir),
                        "headless": True,
                        "gpu": False,
                        "name": "HeadlessProbe",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        devstack.game_job(
            argparse.Namespace(name="headless-probe-" + uuid.uuid4().hex[:12])
        )
        job = Path((home / "current-job").read_text(encoding="utf-8").strip())
        os.environ["BF_GAME_JOB"] = str(job)
        result["job"] = str(job)
        with phase(result, "prepare_backend"):
            environment = devstack.client_environment(True)
        with phase(result, "first_launch"):
            pid, _ = devstack.start_process(
                command,
                game_dir,
                devstack.CLIENT_PID_FILE,
                home / "client.out",
                env=environment,
                headless=True,
            )
            devstack.wait_client_ready(pid)
            first = verify_backend(wait_menu())
            result["runs"].append(first)
        with phase(result, "first_capture"):
            first["evidence"] = {}
            evidence("first-launch", job, encoder, first["evidence"])
        shutil.copy2(home / "client.out", job / "first-client.out")
        with phase(result, "restart"):
            devstack.client_stop()
            devstack.client_restart(game_dir)
            second = verify_backend(wait_menu())
            result["runs"].append(second)
        if all(
            first["client"].get(key) == second["client"].get(key)
            for key in ("pid", "birth", "start_ticks")
        ):
            raise RuntimeError(
                "client restart did not create a new native process identity"
            )
        if (
            sys.platform == "win32"
            and first["backend"]["station"] == second["backend"]["station"]
        ):
            raise RuntimeError("Windows restart reused the previous backend identity")
        if sys.platform == "linux" and all(
            first["display"].get(key) == second["display"].get(key)
            for key in ("pid", "start_ticks")
        ):
            raise RuntimeError(
                "Linux restart did not create a new owned virtual display"
            )
        with phase(result, "second_capture"):
            second["evidence"] = {}
            evidence("after-restart", job, encoder, second["evidence"])
        shutil.copy2(home / "client.out", job / "second-client.out")
        result["complete"] = True
    except (Exception, SystemExit) as failure:
        error = str(failure) or type(failure).__name__
        result["error"] = error
    finally:
        cleanup_errors = []
        with phase(result, "cleanup"):
            for path in (devstack.CLIENT_PID_FILE, home / "display.pid"):
                try:
                    record = devstack.process_record(path)
                    if record and record.get("owner") == owner:
                        devstack.stop_process(path)
                    elif record and record.get("live"):
                        cleanup_errors.append(f"foreign live record retained: {path}")
                except (Exception, SystemExit) as failure:
                    cleanup_errors.append(str(failure) or type(failure).__name__)
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result["complete"] = False
            error = error or "; ".join(cleanup_errors)
        if previous_job is None:
            os.environ.pop("BF_GAME_JOB", None)
        else:
            os.environ["BF_GAME_JOB"] = previous_job
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        manifest = home / "headless-proof.json"
        manifest.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"manifest": str(manifest), **result}, indent=2), flush=True)
    if error:
        raise RuntimeError(
            f"headless Minecraft acceptance failed: {error}; evidence: {manifest}"
        )
    return result
