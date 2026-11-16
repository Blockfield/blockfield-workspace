"""Re-vendor the dedicated block registry from a gameplay commit in one step.

  just vendor-block-registry 9331f58    # copy scripts/maps/block-registry.json
                                         # at that commit into server/ and rewrite
                                         # block-registry.pin (SHA + sha256)

The source SHA must be a commit on gameplay origin/next or origin/main
(checked after a fetch, so a transposed SHA fails instead of shipping a
wrong-but-self-consistent pin). Gameplay checkouts live in gameplay/ beside
the workspace, dedicated in server/ (release.repo_dir layout). Commits and
PRs stay with the caller.
"""

import argparse
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

REGISTRY = Path("scripts/maps/block-registry.json")
PIN = Path("scripts/maps/block-registry.pin")
BRANCHES = ("origin/next", "origin/main")


class VendorError(Exception):
    pass


def guard_dedicated(dedicated):
    """Refuse a wrong-target or dirty dedicated checkout before any write."""
    try:
        url = release.git(dedicated, "config", "--get", "remote.origin.url")
    except RuntimeError:
        url = ""
    if f"{release.ORG}/blockfield-dedicated" not in url:
        raise VendorError(
            f"{dedicated}: not a {release.ORG}/blockfield-dedicated checkout"
        )
    try:
        dirty = release.git(
            dedicated,
            "status",
            "--porcelain",
            "--",
            REGISTRY.as_posix(),
            PIN.as_posix(),
        )
    except RuntimeError as error:
        raise VendorError(
            f"{dedicated}: cannot check for uncommitted changes: {error}"
        ) from error
    if dirty.strip():
        raise VendorError(
            f"{dedicated}: uncommitted changes in {REGISTRY} or {PIN}; commit or stash first"
        )


def run(sha, gameplay, dedicated, dry_run=False):
    """Copy the registry at sha into dedicated and rewrite the pin."""
    gameplay, dedicated = Path(gameplay), Path(dedicated)
    try:
        release.fetch_main(gameplay)
    except RuntimeError as error:
        raise VendorError(
            f"cannot fetch gameplay refs in {gameplay}: {error}"
        ) from error
    try:
        full = release.git(gameplay, "rev-parse", f"{sha}^{{commit}}").strip()
    except RuntimeError as error:
        raise VendorError(f"{sha}: not a commit in {gameplay}") from error
    branches = set(release.git(gameplay, "branch", "-r", "--contains", full).split())
    if not set(BRANCHES) & branches:
        raise VendorError(f"{full}: not on gameplay {' or '.join(BRANCHES)}")
    done = release.run(
        "git",
        "-C",
        str(gameplay),
        "show",
        f"{full}:{REGISTRY.as_posix()}",
        check=False,
    )
    if done.returncode:
        raise VendorError(
            f"{full}: cannot read {REGISTRY}: "
            f"{(done.stdout + done.stderr).strip()[-500:]}"
        )
    data = done.stdout.encode("utf-8")  # text=True decodes UTF-8; re-encoding
    digest = hashlib.sha256(data).hexdigest()  # restores the exact source bytes
    if not (dedicated / REGISTRY).parent.is_dir():
        raise VendorError(f"{dedicated / REGISTRY}: dedicated checkout missing it")
    guard_dedicated(dedicated)
    if not dry_run:
        (dedicated / REGISTRY).write_bytes(data)
        (dedicated / PIN).write_text(f"{full} {digest}\n")
    print(
        f"{'would vend' if dry_run else 'vendored'} dedicated {REGISTRY}"
        f" from gameplay {full} ({len(data)} bytes)"
    )
    print(
        f"{'would rewrite' if dry_run else 'rewrote'} dedicated {PIN}: {full} {digest}"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("sha", help="gameplay commit to vendor the registry from")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--gameplay",
        type=Path,
        default=release.ROOT / "gameplay",
        help="gameplay checkout",
    )
    parser.add_argument(
        "--dedicated",
        type=Path,
        default=release.ROOT / "server",
        help="dedicated checkout",
    )
    args = parser.parse_args(argv)
    try:
        return run(args.sha, args.gameplay, args.dedicated, args.dry_run)
    except (VendorError, RuntimeError) as error:
        print(f"vendor-block-registry: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
