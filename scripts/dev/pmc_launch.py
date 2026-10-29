#!/usr/bin/env python3
"""Build the Minecraft client launch command with portablemc (runs inside the pmc venv).

Prints a JSON list: [java, jvm args..., main class, game args...] with the offline
username baked in and --quickPlayMultiplayer appended. Called by devstack.py play.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-dir", required=True)
    parser.add_argument("--game-dir", required=True)
    parser.add_argument("--natives-dir", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--minecraft", default="1.21.1")
    parser.add_argument("--fabric", default="0.19.3")
    parser.add_argument("--xmx", default="4G")
    parser.add_argument("--testbot-port", default=None)
    parser.add_argument("--debug-port", default=None)
    parser.add_argument("--resolution", default="1280x720")
    parser.add_argument("--server", default=None)
    args = parser.parse_args()

    from portablemc.standard import Context, replace_list_vars
    from portablemc.fabric import FabricVersion

    ctx = Context(Path(args.main_dir), Path(args.game_dir))
    version = FabricVersion.with_fabric(args.minecraft, args.fabric, context=ctx)
    java = shutil.which("java")
    if not java:
        parser.error("Java 21 is missing from PATH; launch through `just play`")
    version.jvm_path = Path(java)
    version.set_auth_offline(args.username, None)
    width, height = args.resolution.lower().split("x")
    version.resolution = (int(width), int(height))
    env = version.install()
    env.jvm_args[1:1] = [f"-Xmx{args.xmx}", "-XX:+UseG1GC"]
    if args.testbot_port:
        env.jvm_args.append(f"-Dbf.testbot.port={args.testbot_port}")
    if args.debug_port:
        env.jvm_args.append(
            f"-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=127.0.0.1:{args.debug_port}"
        )

    natives = Path(args.natives_dir)
    shutil.rmtree(natives, ignore_errors=True)
    natives.mkdir(parents=True)
    for src in env.native_libs:
        if src.name.endswith((".zip", ".jar")):
            with ZipFile(src) as archive:
                for item in archive.infolist():
                    if item.filename.endswith((".so", ".dll", ".dylib")):
                        (natives / item.filename.rsplit("/", 1)[-1]).write_bytes(
                            archive.read(item)
                        )
        else:
            name = src.name
            cut = name.rfind(".so")
            name = name[: cut + 3] if cut >= 0 else name
            shutil.copyfile(src, natives / name)
    rep = dict(env.args_replacements)
    rep["natives_directory"] = str(natives)
    command = [
        *replace_list_vars(env.jvm_args, rep),
        env.main_class,
        *replace_list_vars(env.game_args, rep),
    ]
    if args.server:
        # ponytail: vanilla expects --quickPlayMultiplayer=<addr> as a single argument
        command += [f"--quickPlayMultiplayer={args.server}"]
    json.dump(command, sys.stdout)


if __name__ == "__main__":
    main()
