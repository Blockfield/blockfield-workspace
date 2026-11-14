"""Fetch the checksum-pinned world seed into the local build cache."""

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import tempfile


def fetch(root=None):
    root = Path(root) if root is not None else Path.cwd()
    pin = json.loads(
        (root / "distribution/assets.lock.json").read_text(encoding="utf-8")
    )
    target = root / "sources/world-seed.zip"
    if (
        target.is_file()
        and hashlib.sha256(target.read_bytes()).hexdigest() == pin["sha256"]
    ):
        return target
    get_bytes = runpy.run_path(str(root / "scripts/build-distribution.py"))["get_bytes"]
    data = get_bytes(pin.get("r2-url") or pin["url"])
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError("World template checksum mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(data)
    try:
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(fetch(args.root))
