"""Materialize the pack-root and lobby-source checkouts pinned by pins/.

The public workspace tracks neither repo as a submodule: dedicated (the
server pack root) is a separate public repo and gameplay (the lobby sources)
is private. Developers materialize both beside this checkout, detached at
the recorded SHAs: `just server-checkout`. Idempotent; fails loudly without
access.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEDICATED_URL = "https://github.com/Blockfield/blockfield-dedicated.git"
GAMEPLAY_URL = "https://github.com/Blockfield/blockfield-gameplay.git"

# dirname beside the workspace root, pin file, clone URL. The dedicated
# checkout keeps the historical `server/` path, so it stays the pack root.
CHECKOUTS = (
    ("server", "pins/dedicated.sha", DEDICATED_URL),
    ("gameplay", "pins/gameplay.sha", GAMEPLAY_URL),
)


def die(message):
    raise SystemExit(f"server-checkout: {message}")


def pinned_sha(root=ROOT, pin="pins/dedicated.sha"):
    try:
        sha = (root / pin).read_text(encoding="utf-8").strip()
    except OSError:
        die(f"{pin} is missing: restore it from the workspace checkout")
    if not sha:
        die(f"{pin} is empty")
    return sha


def run_git(*args, cwd):
    try:
        subprocess.run(["git", *args], cwd=cwd, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        die(f"git {' '.join(args)} failed in {cwd}: {error}")


def git_output(*args, cwd):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as error:
        die(f"git {' '.join(args)} failed in {cwd}: {error}")


def checkout_repo(root, dirname, url, pin):
    """Clone the checkout beside the workspace and pin it detached at the pin."""
    sha = pinned_sha(root, pin)
    target = root / dirname
    if target.is_dir() and not (target / ".git").exists():
        die(f"{target} exists but is not a git checkout: move it aside first")
    if not target.exists():
        run_git("clone", url, str(target), cwd=root)
    run_git("fetch", "origin", cwd=target)
    if git_output("status", "--porcelain", "--untracked-files=no", cwd=target):
        die(f"{target} has local changes: stash, commit or reset them first")
    run_git("checkout", "--detach", sha, cwd=target)
    if git_output("rev-parse", "HEAD", cwd=target) != sha:
        die(f"{target} is not at {sha} after checkout")
    # A fresh clone starts with hooks unwired: point it at the versioned
    # hooks when they exist and no explicit choice was made. Never fatal.
    # Same rules as every `just` call: never override hooks git already runs.
    sys.path.insert(0, str(ROOT / "scripts"))
    from quality import wire_hooks

    wire_hooks(target)
    return target


def checkout_pair(root=ROOT):
    """Check out dedicated (pack root) and gameplay (lobby source)."""
    return [checkout_repo(root, dirname, url, pin) for dirname, pin, url in CHECKOUTS]


def main():
    dedicated, gameplay = checkout_pair()
    print(
        f"dedicated ready at {pinned_sha(pin='pins/dedicated.sha')} (server/), "
        f"gameplay ready at {pinned_sha(pin='pins/gameplay.sha')} (gameplay/)"
    )
    return dedicated, gameplay


if __name__ == "__main__":
    main()
