#!/usr/bin/env python3
"""Run the dev stack with literal arguments and the canonical packaging Python."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEVSTACK = ROOT / "scripts" / "dev" / "devstack.py"
sys.path.insert(0, str(ROOT / "scripts"))
from toolchain import canonical_python, child_env  # noqa: E402


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    python = str(canonical_python())
    target = DEVSTACK
    if arguments[:1] == ["prune"]:
        target = ROOT / "scripts" / "dev" / "prune_dev.py"
        arguments = arguments[1:]
    elif arguments[:1] == ["mcp"]:
        target = ROOT / "scripts" / "dev" / "mcp" / "server.py"
        arguments = arguments[1:]
    command = [python, "-I", "-X", "utf8", str(target), *arguments]
    environment = child_env()
    if sys.platform == "win32":
        # Windows UCRT execve crashes with inherited pipes (CPython #143327).
        raise SystemExit(subprocess.run(command, env=environment).returncode)
    os.execve(python, command, environment)


if __name__ == "__main__":
    main()
