#!/usr/bin/env python3
"""Prune finished task stands to evidence; dry-run unless --apply DIR... is given."""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from devstack import process_record  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = {
    ".md",
    ".json",
    ".jsonl",
    ".log",
    ".out",
    ".txt",
    ".csv",
    ".tsv",
    ".py",
    ".sh",
    ".patch",
    ".diff",
    ".html",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".mp4",
    ".webm",
}
RUNTIME = {
    "jdk",
    "mc",
    "pmc",
    "play-natives",
    "downloads",
    "libraries",
    "versions",
    ".fabric",
    "node_modules",
    ".gradle",
    "asset-venv",
}
PROTECTED = RUNTIME | {
    "play",
    "server",
    "release",
    "worktrees",
    "jobs",
    "dist",
    "coord",
    "coordinator",
}


def validate(stand):
    """Only direct task directories of this checkout's .dev may be pruned."""
    stand = Path(os.path.abspath(stand))
    dev = ROOT / ".dev"
    if (
        stand.parent != dev
        or stand.name in PROTECTED
        or stand.name.startswith(("coord-", "coordinator-"))
        or stand.is_symlink()
        or dev.is_symlink()
        or not stand.is_dir()
    ):
        raise ValueError(f"not a task stand under {dev}: {stand}")
    return stand


def size(path):
    return sum(
        (Path(base) / name).lstat().st_size
        for base, _, files in os.walk(path, followlinks=False)
        for name in files
    )


def live_users(stand):
    """Check each proc link independently: a disappearing fd must not hide cwd."""
    if not sys.platform.startswith("linux"):
        raise ValueError(
            "pruning requires Linux /proc to exclude unregistered processes"
        )
    users, skipped = [], []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        links = [proc / "cwd", proc / "exe"]
        try:
            links.extend((proc / "fd").iterdir())
        except FileNotFoundError:
            pass
        except PermissionError:
            # Non-dumpable or sandboxed processes (systemd --user, ssh-agent, flatpak, agent
            # sandboxes) hide their links even from their owner; stand JVMs/Xvfb never do.
            if proc.stat().st_uid == os.getuid():
                skipped.append(proc.name)
        for path in links:
            try:
                target = Path(os.readlink(path).removesuffix(" (deleted)"))
            except FileNotFoundError:
                continue
            except PermissionError:
                if proc.exists() and proc.stat().st_uid == os.getuid():
                    skipped.append(proc.name)
                    break
                continue
            if target == stand or stand in target.parents:
                users.append(proc.name)
                break
    for base, dirs, files in os.walk(stand, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
        for name in files:
            path = Path(base) / name
            if path.suffix != ".pid" or path.is_symlink():
                continue
            try:
                record = process_record(path)
            except SystemExit as error:
                raise ValueError(str(error)) from error
            if record and record.get("live"):
                if not record.get("native") and "start_ticks" in record:
                    from linux_process import identity

                    current = identity(record["pid"])
                    if not current or current["start_ticks"] != record["start_ticks"]:
                        continue
                users.append(f"{path.relative_to(stand)}:{record['pid']}")
    if skipped:
        print(
            f"note: {len(set(skipped))} own processes hide their /proc links and were not inspected",
            file=sys.stderr,
        )
    return users


def prune(stand):
    stand = validate(stand)
    if (stand / ".keep").exists() or (stand / ".keep").is_symlink():
        raise ValueError("kept (.keep)")
    users = live_users(stand)
    if users:
        raise ValueError("in use: " + ", ".join(users))
    removed = kept = 0
    for base, dirs, files in os.walk(stand, topdown=True, followlinks=False):
        here = Path(base)
        for name in list(dirs):
            target = here / name
            if target.is_symlink() or name in RUNTIME:
                if target.is_symlink():
                    target.unlink()
                else:
                    removed += size(target)
                    shutil.rmtree(target)
                dirs.remove(name)
        for name in files:
            path = here / name
            if not path.is_symlink() and (
                path.suffix.lower() in EVIDENCE or name == "SHA256SUMS"
            ):
                kept += path.lstat().st_size
            else:
                removed += path.lstat().st_size
                path.unlink()
    for base, _, _ in os.walk(stand, topdown=False):
        if Path(base) != stand and not os.listdir(base):
            os.rmdir(base)
    receipt = {
        "pruned": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "removed_bytes": removed,
        "kept_bytes": kept,
    }
    (stand / "PRUNED.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("stands", nargs="*", type=Path)
    args = parser.parse_args()
    if args.apply and not args.stands:
        parser.error("--apply needs explicit stand directories")
    dev = ROOT / ".dev"
    candidates = args.stands or (
        sorted(
            p
            for p in dev.iterdir()
            if p.is_dir()
            and p.name not in PROTECTED
            and not p.name.startswith(("coord-", "coordinator-"))
        )
        if dev.is_dir()
        else []
    )
    failed = False
    for stand in candidates:
        try:
            stand = validate(stand)
            if args.apply:
                receipt = prune(stand)
                print(
                    f"{stand}: removed {receipt['removed_bytes']} bytes; "
                    f"kept {receipt['kept_bytes']} bytes"
                )
            else:
                users = live_users(stand)
                state = (
                    "in use"
                    if users
                    else "kept (.keep)"
                    if (stand / ".keep").exists()
                    else "idle"
                )
                print(f"{size(stand) / 2**30:.2f} GiB  {state}  {stand}")
        except (ValueError, OSError) as error:
            failed = True
            print(f"refused {stand}: {error}", file=sys.stderr)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
