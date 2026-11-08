"""Vendor the shared toolchain core into sibling repo checkouts.

The single pin set lives here: scripts/tools.lock.json plus the core
scripts/toolchain.py. Each repo vendors byte-identical copies next to its
own repo-specific module; its drift check diffs the vendored hashes
against the public workspace origin/main (no private-repo token needed).

Usage: python scripts/sync_tools.py <sibling-checkout> [--check]
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ("scripts/toolchain.py", "scripts/tools.lock.json")


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def drift(dest):
    """Vendored files whose bytes differ from this checkout."""
    dest = Path(dest)
    return [
        relative
        for relative in VENDORED
        if not (dest / relative).is_file()
        or _hash(dest / relative) != _hash(ROOT / relative)
    ]


def sync(dest):
    """Copy the shared files byte-identical into the sibling checkout."""
    dest = Path(dest)
    for relative in VENDORED:
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    return drift(dest)


def main(argv):
    check_only = "--check" in argv
    args = [arg for arg in argv if arg != "--check"]
    if len(args) != 1 or not Path(args[0]).is_dir():
        print("usage: sync_tools.py <sibling-checkout> [--check]", file=sys.stderr)
        return 2
    if not check_only:
        sync(args[0])
    drifted = drift(args[0])
    if drifted:
        for relative in drifted:
            print(f"drift: {relative}", file=sys.stderr)
        return 1
    print("in sync: " + ", ".join(VENDORED))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
