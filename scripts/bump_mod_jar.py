"""Pin one fork mod JAR release in every holder of gameplay, blockfield-mod and client.

  just bump-mod-jar lr-tactical bf8              # rewrite the holders in gameplay/, localmods/blockfield-mod, client/
  just bump-mod-jar lr-tactical bf8 --dry-run    # list what would change, write nothing
  just bump-mod-jar lr-tactical --check          # every holder and the release it pins now
  just bump-mod-jar superbwarfare-fabric bf28 --gameplay ../g --mod ../m --client ../c

The JAR is read from the fork's GitHub release and must match the asset digest GitHub
records plus every SHA256SUMS/SHA512SUMS asset and the BUILD-SHA256SUMS file at the tag
that names it. Old values are the releases the holders name now (each holder may lag on
its own). Nothing is written when a holder is missing or pins none of them, or when any
other tracked file in the three repos would still hold an old hash. Commits and PRs stay
with the caller.
"""

import argparse
import base64
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release  # noqa: E402

PKG = "com/lunares/blockfield"
TREES = {
    "gameplay": release.ROOT / "gameplay",
    "mod": release.ROOT / "localmods/blockfield-mod",
    "client": release.ROOT / "client",
}
# fork repo -> (jar name with its release tag as group 1, holders per tree). Hash-only
# holders (frozen test constants, AnimationManifest.PINS) are matched by the hashes of
# the releases the named holders pin.
# blockfield-mod build.gradle `lrTactical` is not a holder: it reads the JAR out of a
# client release archive, so it follows client releases, not this bump.
MODS = {
    "lr-tactical": (
        r"lrtactical-[\d.]+-[\d.]+-(bf\d+)\.jar",
        {
            "gameplay": (
                "mod-server/build.gradle",
                "mod-server/dev/oracle-mod-pins.gradle",
                f"mod-server/server/src/test/java/{PKG}/server/combat/LrAnimationReadLayoutTest.java",
                f"mod-server/server/src/test/java/{PKG}/server/combat/PaGraphBytecodeTest.java",
            ),
            "mod": (
                f"common/src/main/java/{PKG}/common/combat/AnimationManifest.java",
                f"common/src/test/java/{PKG}/common/combat/AnimationReleasePairPinTest.java",
                "dev/oracle-mod-pins.gradle",
            ),
            "client": ("mods/lrtactical-fabric.pw.toml",),
        },
    ),
    "superbwarfare-fabric": (
        r"superbwarfare-[\d.]+-mc[\d.]+-(bf\d+)\.jar",
        {
            "gameplay": (
                "mod-server/build.gradle",
                "mod-server/dev/oracle-mod-pins.gradle",
            ),
            "mod": (
                "build.gradle",
                "dev/oracle-mod-pins.gradle",
                f"common/src/main/java/{PKG}/common/combat/AnimationManifest.java",
            ),
            "client": ("mods/superbwarfare.pw.toml",),
        },
    ),
}


class BumpError(Exception):
    pass


class Jar(NamedTuple):
    tag: str
    name: str
    sha256: str
    sha512: str


def sums_for(text, name):
    """Hashes a `<hex>  <name>` sums file lists for name."""
    found = []
    for line in text.splitlines():
        digest, _, listed = line.strip().partition(" ")
        if listed.strip().lstrip("*") == name:
            found.append(digest.lower())
    return found


def verified(tag, name, data, expected):
    """Jar for data after every (algorithm, hash, source) in expected matched."""
    jar = Jar(
        tag, name, hashlib.sha256(data).hexdigest(), hashlib.sha512(data).hexdigest()
    )
    expected = [item for item in expected if item[1]]
    if not expected:
        raise BumpError(f"{tag} {name}: no digest or sums file to verify against")
    for algo, digest, source in expected:
        if getattr(jar, algo) != digest:
            raise BumpError(f"{tag} {name}: {algo} does not match {source}")
    return jar


def fetch(repo, tag):
    """The one mod JAR of release tag, verified (needs gh read access to repo)."""
    meta = json.loads(release.gh("api", f"repos/{repo}/releases/tags/{tag}"))
    assets = {asset["name"]: asset for asset in meta["assets"]}
    jars = [n for n in assets if n.endswith(".jar") and not n.endswith("-sources.jar")]
    if len(jars) != 1:
        raise BumpError(f"{repo} {tag}: expected one jar asset, got {jars}")
    name = jars[0]
    sums = [n for n in ("SHA256SUMS", "SHA512SUMS") if n in assets]
    expected = [
        (
            "sha256",
            assets[name].get("digest", "").removeprefix("sha256:"),
            "asset digest",
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        patterns = [arg for n in (name, *sums) for arg in ("-p", n)]
        release.gh("release", "download", tag, "--repo", repo, "-D", tmp, *patterns)
        data = Path(tmp, name).read_bytes()
        for n in sums:
            text = Path(tmp, n).read_text()
            expected += [(n[:6].lower(), h, n) for h in sums_for(text, name)]
    build = release.run(
        "gh", "api", f"repos/{repo}/contents/BUILD-SHA256SUMS?ref={tag}", check=False
    )
    if build.returncode == 0:
        text = base64.b64decode(json.loads(build.stdout)["content"]).decode()
        expected += [("sha256", h, "BUILD-SHA256SUMS") for h in sums_for(text, name)]
    elif "404" not in build.stderr:
        raise BumpError(f"{repo} {tag}: BUILD-SHA256SUMS: {build.stderr.strip()}")
    return verified(tag, name, data, expected)


def read_holders(mod, trees):
    """{(tree, path): text} of every holder; a missing file is an error."""
    texts = {}
    for side, paths in MODS[mod][1].items():
        for path in paths:
            file = Path(trees[side]) / path
            if not file.is_file():
                raise BumpError(f"{side} {path}: holder missing in {trees[side]}")
            texts[side, path] = file.read_bytes().decode()
    return texts


def pinned_tags(text, jars):
    return [
        jar.tag
        for jar in jars
        if jar.name in text or jar.sha256 in text or jar.sha512 in text
    ]


def scan(mod, trees, fetch):
    """(holder texts, jars of the releases the holders pin, oldest first).

    A release counts as pinned only when a holder carries its hash: a bare jar name
    (a stale-pin guard in a test, a comment) is a reference and stays as it is.
    """
    texts = read_holders(mod, trees)
    stem = MODS[mod][0]
    tags = {tag for text in texts.values() for tag in re.findall(stem, text)}
    order = sorted(tags, key=lambda tag: int(tag.removeprefix("bf")))
    jars = [fetch(f"{release.ORG}/{mod}", tag) for tag in order]
    held = "".join(texts.values())
    return texts, [jar for jar in jars if jar.sha256 in held or jar.sha512 in held]


def rewrite(text, repo, olds, new):
    for old in olds:
        entry = re.search(rf"archiveEntry:\s*'[^']*{re.escape(old.name)}'", text)
        if entry:
            raise BumpError(
                f"{entry.group(0)}: a client archive ships {old.name}, not {new.name};"
                " drop the archive fields or point them at a client release with the new jar"
            )
        for a, b in (
            (old.name, new.name),
            (
                f"{repo}/releases/download/{old.tag}/",
                f"{repo}/releases/download/{new.tag}/",
            ),
            (old.sha256, new.sha256),
            (old.sha512, new.sha512),
        ):
            text = text.replace(a, b)
    return text


def leftovers(tree, hashes):
    """Tracked files of tree that hold any of hashes."""
    if not hashes:
        return set()
    patterns = [arg for h in sorted(hashes) for arg in ("-e", h)]
    done = release.run("git", "-C", tree, "grep", "-lz", "-F", *patterns, check=False)
    if done.returncode > 1:
        raise BumpError(f"git grep in {tree}: {done.stderr.strip()}")
    return set(filter(None, done.stdout.split("\0")))


def plan(mod, tag, trees, fetch):
    """{(tree, path): new text} for the holders that change; raises before any write."""
    repo = f"{release.ORG}/{mod}"
    texts, jars = scan(mod, trees, fetch)
    new = next((jar for jar in jars if jar.tag == tag), None) or fetch(repo, tag)
    olds = [jar for jar in jars if jar.tag != tag]
    errors = []
    edits = {}
    for (side, path), text in texts.items():
        if not pinned_tags(text, [*olds, new]):
            errors.append(
                f"{side} {path}: pins none of {[j.tag for j in jars] or [tag]}"
            )
            continue
        updated = rewrite(text, repo, olds, new)
        if updated != text:
            edits[side, path] = updated
    old_hashes = {h for jar in olds for h in (jar.sha256, jar.sha512)}
    for side in MODS[mod][1]:
        for path in sorted(leftovers(trees[side], old_hashes)):
            if (side, path) not in texts:
                errors.append(
                    f"{side} {path}: holds an old hash but is not a known holder"
                )
    if errors:
        raise BumpError("\n".join(errors))
    return edits


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mod", choices=MODS)
    parser.add_argument("tag", nargs="?", help="fork release tag, e.g. bf8")
    parser.add_argument("--check", action="store_true", help="list holders and pins")
    parser.add_argument("--dry-run", action="store_true")
    for side, default in TREES.items():
        parser.add_argument(
            f"--{side}",
            dest=f"{side}_tree",
            type=Path,
            default=default,
            help=f"{side} checkout",
        )
    args = parser.parse_args(argv)
    trees = {side: getattr(args, f"{side}_tree") for side in TREES}
    try:
        if args.check:
            texts, jars = scan(args.mod, trees, fetch)
            for (side, path), text in texts.items():
                print(f"{side} {path}: {', '.join(pinned_tags(text, jars)) or 'none'}")
            return 0
        if not args.tag:
            parser.error("tag is required without --check")
        edits = plan(args.mod, args.tag, trees, fetch)
    except (BumpError, RuntimeError) as error:
        print(f"bump-mod-jar: {error}", file=sys.stderr)
        return 1
    for side, path in edits:
        print(f"{'would change' if args.dry_run else 'changed'} {side} {path}")
    if not edits:
        print(f"{args.mod} {args.tag}: every holder already pins it")
    if args.dry_run:
        return 0
    for (side, path), text in edits.items():
        (trees[side] / path).write_bytes(text.encode())
    client = [path for side, path in edits if side == "client"]
    if client:
        release.refresh_index(trees["client"], client)
        print("refreshed client index.toml and pack.toml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
