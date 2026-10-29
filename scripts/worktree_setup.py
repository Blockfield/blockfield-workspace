"""Prepare submodules of a T3 worktree; a no-op when they are already checked out.

T3 runs `git submodule update --init --recursive` with its 30 s git timeout. Seventeen
submodules do not fit, so the killed checkout leaves indexes whose files are missing
(every file shows as a staged deletion). This repairs that state and initializes the rest.
"""

import os
import subprocess
import sys
from pathlib import Path


def git(root, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {root}:\n{result.stderr}")
    return result


def paths(root):
    out = git(
        root, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$"
    ).stdout
    return [line.split(" ", 1)[1] for line in out.splitlines()]


def state(root, path):
    """Only staged deletions identify a checkout we may repair; preserve other work."""
    module = root / path
    if (
        not (module / ".git").exists()
        or git(module, "rev-parse", "HEAD", check=False).returncode
    ):
        return "missing"
    changes = git(module, "status", "--porcelain").stdout.splitlines()
    if changes and all(line[:2] == "D " for line in changes):
        return "broken"
    recorded = git(root, "rev-parse", f":{path}").stdout.strip()
    if changes or git(module, "rev-parse", "HEAD").stdout.strip() != recorded:
        return "preserved"
    return "ready"


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()).resolve()
    common = Path(
        git(
            root, "rev-parse", "--path-format=absolute", "--git-common-dir"
        ).stdout.strip()
    )
    own = Path(
        git(root, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    )
    if own == common:
        raise SystemExit(
            f"{root} is the main checkout, not a worktree; refusing to touch its submodules"
        )
    states = {path: state(root, path) for path in paths(root)}
    for path in (p for p, s in states.items() if s == "preserved"):
        print(f"warning: leaving {path}: local changes or moved HEAD", file=sys.stderr)
    for path in (p for p, s in states.items() if s == "broken"):
        print(f"repair {path}: interrupted checkout")
        git(root, "submodule", "update", "--init", "--force", "--", path)
    missing = [p for p, s in states.items() if s == "missing"]
    if missing:
        print("init " + " ".join(missing))
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "submodule",
                "update",
                "--init",
                "--recursive",
                "--jobs",
                "8",
                "--",
                *missing,
            ],
            check=True,
        )
    left = []
    for path, initial in states.items():
        if initial == "preserved" or state(root, path) == "ready":
            continue
        module = root / path
        if (
            git(module, "rev-parse", "HEAD").stdout.strip()
            == git(root, "rev-parse", f":{path}").stdout.strip()
            and git(
                module, "diff", "--ignore-cr-at-eol", "--quiet", check=False
            ).returncode
            == 0
            and git(
                module, "diff", "--cached", "--ignore-cr-at-eol", "--quiet", check=False
            ).returncode
            == 0
            and not git(module, "ls-files", "--others", "--exclude-standard").stdout
        ):
            print(f"warning: {path}: line-ending-only changes", file=sys.stderr)
        else:
            left.append(path)
    if left:
        raise SystemExit("submodules still not ready: " + " ".join(left))
    print(
        "worktree setup complete"
        if "preserved" in states.values()
        else "worktree ready"
        if missing or "broken" in states.values()
        else "worktree already ready"
    )


if __name__ == "__main__":
    main()
