"""Materialize the private server checkout pinned by pins/server.sha.

The public workspace cannot track the private server as a submodule, so
developers with access clone it beside this checkout, detached at the
recorded SHA: `just server-checkout`. Idempotent; fails loudly without access.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_URL = "https://github.com/Blockfield/blockfield-server.git"


def die(message):
    raise SystemExit(f"server-checkout: {message}")


def pinned_sha(root=ROOT):
    try:
        sha = (root / "pins" / "server.sha").read_text(encoding="utf-8").strip()
    except OSError:
        die("pins/server.sha is missing: restore it from the workspace checkout")
    if not sha:
        die("pins/server.sha is empty")
    return sha


def run_git(*args, cwd):
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        die(f"git {' '.join(args)} failed in {cwd}: {error}")


def checkout_server(root=ROOT, url=SERVER_URL):
    """Clone server/ beside the workspace and pin it detached at pins/server.sha."""
    sha = pinned_sha(root)
    target = root / "server"
    if target.is_dir() and not (target / ".git").exists():
        die(f"{target} exists but is not a git checkout: move it aside first")
    if not target.exists():
        run_git("clone", url, str(target), cwd=root)
    run_git("fetch", "origin", cwd=target)
    run_git("checkout", "--detach", sha, cwd=target)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != sha:
        die(f"{target} is at {head}, expected {sha}")
    return target


def main():
    checkout_server()
    print(f"server ready at {pinned_sha()} (server/)")


if __name__ == "__main__":
    main()
