"""Build a development pack with local JARs, without changing its repository."""

import argparse
import importlib.util
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from modmap import ROOT, local_jars  # noqa: E402


def load_build(root, side):
    filename = "build-client.py" if side == "client" else "build-distribution.py"
    spec = importlib.util.spec_from_file_location(
        "workspace_pack", root / "scripts" / filename
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    library = sys.modules["packlib"] if side == "client" else module
    return module, library


def overlay(module, library, jars):
    resolve = module.resolve_mod

    def resolve_local(root, item, built):
        name, mod = item
        jar = jars.get(name) or jars.get(PurePosixPath(name).name)
        if jar is None:
            return resolve(root, item, built)
        target = str(PurePosixPath(name).parent / library.safe_path(mod["filename"]))
        data = jar.read_bytes()
        if not data.startswith(b"PK"):
            raise ValueError(f"Not a ZIP archive: {jar}")
        return name, mod, target, data

    module.resolve_mod = resolve_local
    if hasattr(module, "check_client"):
        check = module.check_client

        def check_local(policy, contract, published):
            # A dev pack uses its own contract as the login marker (setup copies it to the
            # client), even all-pin: server pins may be ahead of the published client.
            # Production keeps the published client check in the standalone build script.
            published.update(client=contract, compatibility=contract)
            check(policy, contract, published)

        module.check_client = check_local


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side", choices=("client", "server"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--public-base", default="https://blockfield.pro")
    parser.add_argument("--server-source", type=Path)
    parser.add_argument("--world-seed", type=Path)
    parser.add_argument("--client-manifest")
    parser.add_argument("--build-id", default="dev")
    args = parser.parse_args(argv)
    args.root = (args.root or ROOT / args.side).resolve()
    args.server_source = args.server_source or args.root / "server"
    if args.side == "server" and args.world_seed is None:
        parser.error("server requires --world-seed")
    module, library = load_build(args.root, args.side)
    jars = {
        name: path
        for name, path in local_jars().items()
        if (args.root / "mods" / name).is_file()
    }
    overlay(module, library, jars)
    module.build(args)


if __name__ == "__main__":
    main()
