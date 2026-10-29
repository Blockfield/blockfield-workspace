"""Compile testbot only when its inputs change; unknown comparisons run the check."""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = (
    "scripts/dev/testbot",
    "scripts/quality.py",
    "scripts/toolchain.py",
    "scripts/dev_cache.py",
    "scripts/ci_scope.py",
    "bf.py",
    "justfile",
    ".github",
    ".gitattributes",
    ".editorconfig",
)


def changed(base, root, paths):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
        return True
    return (
        subprocess.run(
            ["git", "diff", "--quiet", base, "HEAD", "--", *paths], cwd=root
        ).returncode
        != 0
    )


def needs_compile(base, root=ROOT):
    return changed(base, root, INPUTS)


if __name__ == "__main__":
    compile = str(needs_compile(os.environ.get("BASE_SHA", ""))).lower()
    with Path(os.environ["GITHUB_OUTPUT"]).open(
        "a", encoding="utf-8", newline="\n"
    ) as f:
        f.write(f"compile={compile}\n")
