"""Upload one fork mod JAR release to the pack mirror under its sha512 name.

  just mirror-mod-jar superbwarfare-fabric bf27   # verify, upload, print the URL
  just mirror-mod-jar lr-tactical bf7             # already there: skip

Release order: fork release -> mirror-mod-jar -> bump-mod-jar (`bump-mod-jar`
refuses pins whose jar is not in the mirror yet, so mod CI never 404s).
Idempotent: an asset with the same name and digest is skipped; a same-named
asset that differs is an error, never replaced. Runs locally with the
releaser's own gh auth; no CI credential.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bump_mod_jar as bump  # noqa: E402
import release  # noqa: E402

MIRROR_REPO = "Blockfield/blockfield-pack-mirror"
MIRROR_TAG = "files"


class MirrorError(Exception):
    pass


def url(name):
    return f"https://github.com/{MIRROR_REPO}/releases/download/{MIRROR_TAG}/{name}"


def assets():
    """{asset name: (size, digest)} of the mirror release (needs gh read access)."""
    meta = json.loads(
        release.gh("api", f"repos/{MIRROR_REPO}/releases/tags/{MIRROR_TAG}")
    )
    return {a["name"]: (a["size"], a.get("digest", "")) for a in meta["assets"]}


def mirrored(jar, existing, size=None):
    """True when the mirror holds jar under its sha512 name with the same digest.

    size (the local jar bytes, when the caller kept them) must match as well;
    None skips the size check. A same-named asset that differs is an error.
    """
    if jar.sha512 not in existing:
        return False
    have_size, have_digest = existing[jar.sha512]
    if have_digest != f"sha256:{jar.sha256}" or (
        size is not None and have_size != size
    ):
        raise MirrorError(
            f"{MIRROR_TAG}/{jar.sha512}: differs from {jar.tag} {jar.name};"
            " refusing to touch it"
        )
    return True


def require_mirrored(mod, tag, jar):
    """Raise unless the mirror holds jar; names the command that uploads it."""
    try:
        found = mirrored(jar, assets())
    except MirrorError as error:
        raise bump.BumpError(str(error)) from error
    if not found:
        raise bump.BumpError(
            f"{tag} {jar.name}: not in the mirror yet;"
            f" run just mirror-mod-jar {mod} {tag} first"
        )


def upload(file):
    release.gh("release", "upload", MIRROR_TAG, str(file), "--repo", MIRROR_REPO)


def run(mod, tag, download=None, existing=None, send=None):
    """Verify the fork release jar and mirror it; returns a process exit code."""
    repo = f"{release.ORG}/{mod}"
    name, data, expected = (download or bump.download)(repo, tag)
    jar = bump.verified(tag, name, data, expected)
    if existing is None:
        existing = assets()
    if mirrored(jar, existing, len(data)):
        print(f"{mod} {tag}: already mirrored as {jar.sha512}")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp, jar.sha512)
            file.write_bytes(data)
            (send or upload)(file)
        print(f"{mod} {tag}: mirrored {name} as {jar.sha512}")
    print(url(jar.sha512))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mod", choices=bump.MODS)
    parser.add_argument("tag", help="fork release tag, e.g. bf27")
    args = parser.parse_args(argv)
    try:
        return run(args.mod, args.tag)
    except (MirrorError, bump.BumpError, RuntimeError) as error:
        print(f"mirror-mod-jar: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
