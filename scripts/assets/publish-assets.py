"""Publish an immutable resource version to R2 and verify every uploaded file."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

# Import, not runpy: a run_path copy of the core imports toolchain_local, which
# then imports the real core mid-cycle and leaves it with the fallback stubs.
SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
from toolchain import tool  # noqa: E402


def parse_sums(text):
    """Strict SHA256SUMS parse: one `<sha256>  <name>` line each.

    Deploy-time consumers (pack-swap, export-release-record, check-deployed)
    split on exactly two spaces, so anything else must fail here at publish,
    not mid-release with a confusing error.
    """
    expected = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        checksum, sep, name = line.partition("  ")
        if (
            not sep
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or not name
            or name in expected
        ):
            raise ValueError(f"Invalid or duplicate SHA256SUMS entry: {line!r}")
        expected[name] = checksum
    return expected


def write_sums(directory):
    """Write the canonical SHA256SUMS for the release artifacts (no upload).

    Split out for callers that must attach the sums file (GitHub release, PR)
    before the R2 upload happens last.
    """
    files = sorted(directory.iterdir())
    artifacts = [path for path in files if path.suffix in {".zip", ".jar"}]
    if not artifacts:
        raise ValueError("No resource artifact to publish")
    sums = directory / "SHA256SUMS"
    sums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in artifacts
        ),
        encoding="utf-8",
    )
    return sums


def publish(directory, prefix, *, dry_run=False):
    if not re.fullmatch(
        r"(?:world-seed|maps|schematics|music|gucci-vuitton-attachment)/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?",
        prefix,
    ):
        raise ValueError("Invalid resource version prefix")
    files = sorted(directory.iterdir())
    if not files or any(
        not path.is_file()
        or path.is_symlink()
        or not (
            path.suffix in {".zip", ".jar"}
            or path.name
            in {
                "SHA256SUMS",
                "release-notes.md",
                "github-release.json",
                "archive-release.json",
            }
        )
        for path in files
    ):
        raise ValueError("Publish only release artifacts, notes and checksums")
    artifacts = [path for path in files if path.suffix in {".zip", ".jar"}]
    if not artifacts:
        raise ValueError("No resource artifact to publish")
    sums = directory / "SHA256SUMS"
    if sums.exists():
        expected = parse_sums(sums.read_text(encoding="utf-8"))
        for path in artifacts:
            if expected.get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError(f"Release checksum mismatch: {path.name}")
    elif not dry_run:
        write_sums(directory)
    if dry_run:
        return f"s3://blockfield-assets/{prefix}"
    command = [
        str(tool("rclone")),
        "--config",
        os.environ.get(
            "BLOCKFIELD_R2_CONFIG", str(Path.home() / ".config/blockfield/r2.conf")
        ),
    ]
    subprocess.run(
        [
            *command,
            "copy",
            str(directory),
            f"r2_upload:blockfield-assets/{prefix}",
            "--immutable",
            "--checksum",
            "--retries",
            "1",
        ],
        check=True,
        timeout=600,
    )
    subprocess.run(
        [
            *command,
            "check",
            str(directory),
            f"r2_read:blockfield-assets/{prefix}",
            "--download",
            "--one-way",
            "--retries",
            "1",
        ],
        check=True,
        timeout=600,
    )
    return f"s3://blockfield-assets/{prefix}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate prefix, files and checksums without uploading to R2",
    )
    parser.add_argument("prefix")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(publish(args.directory, args.prefix, dry_run=args.dry_run))
