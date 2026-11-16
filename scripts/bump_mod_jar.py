"""Pin one fork mod JAR release in every holder of gameplay, blockfield-mod and client.

  just bump-mod-jar lr-tactical bf8              # rewrite the holders in gameplay/, localmods/blockfield-mod, client/
  just bump-mod-jar lr-tactical bf8 --dry-run    # list what would change, write nothing
  just bump-mod-jar lr-tactical --check          # every holder and the release it pins now
  just bump-mod-jar lr-tactical bf8 --only mod   # mod holders first (gameplay moves later, with the re-export)
  just bump-mod-jar superbwarfare-fabric bf28 --gameplay ../g --mod ../m --client ../c

The JAR is read from the fork's GitHub release and must match the asset digest GitHub
records plus every SHA256SUMS/SHA512SUMS asset and the BUILD-SHA256SUMS file at the tag
that names it. Old values are the releases the holders name now (each holder may lag on
its own). Nothing is written when a holder is missing or pins none of them, or when any
other tracked file in the rewritten trees would still hold an old hash. Gameplay
scripts/maps/block-registry.json records the pin set it was exported from: a bump
that writes gameplay holders while moving a recorded pin stops with the re-export
command instead of writing. The mod side moves first: `--only mod` rewrites just the
mod holders (the gameplay registry guard does not apply), then the mod release carries
the pins and the gameplay holders move with mod-server/common.properties and
`just export-registry` in one gameplay PR.
Commits and PRs stay with the caller.
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

sys.modules.setdefault("bump_mod_jar", sys.modules[__name__])

PKG = "com/lunares/blockfield"
# Release order (gameplay reads AnimationManifest.PINS out of the released mod
# common jar, so a fork bump cannot land gameplay-first): mod holders move first
# and the mod release carries the pins, then the gameplay holders move together
# with mod-server/common.properties (commonVersion/Url/Sha256) and the
# block-registry re-export in one gameplay PR, then the dedicated registry is
# re-vendored (every gameplay mod bump changes block-registry.json), then
# dedicated and client pack releases.
TREES = {
    "gameplay": release.ROOT / "gameplay",
    "mod": release.ROOT / "localmods/blockfield-mod",
    "client": release.ROOT / "client",
}
# Gameplay-tree file recording the pin set the registry was exported from
# (gameplay PR #41): a bump that moves a recorded pin leaves the blocks stale,
# and the export downloads every pinned jar, so it cannot run offline from here.
# The tool stops instead of rewriting the registry; see registry_stale.
REGISTRY = "scripts/maps/block-registry.json"
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


CHAIN = (
    "mod holders + mod release carrying the pins"
    " -> gameplay holders + mod-server/common.properties bump (commonVersion/Url/Sha256)"
    " + block-registry re-export (just export-registry) in one gameplay PR"
    " -> dedicated block-registry re-vendor"
    " (just vendor-block-registry <gameplay-sha>)"
    " -> dedicated release -> client pack release"
)


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


def download(repo, tag):
    """(name, data, expected checks) of the one mod JAR of release tag."""
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
    return name, data, expected


def fetch(repo, tag):
    """The one mod JAR of release tag, verified (needs gh read access to repo)."""
    name, data, expected = download(repo, tag)
    return verified(tag, name, data, expected)


def read_holders(mod, trees, only=None):
    """{(tree, path): text} of every holder in scope; a missing file is an error."""
    texts = {}
    for side, paths in MODS[mod][1].items():
        if only is not None and side != only:
            continue
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


def scan(mod, trees, fetch, only=None):
    """(holder texts, jars of the releases the holders pin, oldest first).

    A release counts as pinned only when a holder carries its hash: a bare jar name
    (a stale-pin guard in a test, a comment) is a reference and stays as it is.
    `only` limits the scan to one tree (a mod-only bump skips the other trees).
    """
    texts = read_holders(mod, trees, only)
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
        # Truncated hash prefixes in comments beside the pin (mod build.gradle
        # "oracle sha512 bb8b5ff2…") go stale with the pin, so they move with it.
        for algo in ("sha256", "sha512"):
            text = text.replace(
                f"{getattr(old, algo)[:8]}…", f"{getattr(new, algo)[:8]}…"
            )
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


def registry_stale(mod, trees, olds, new):
    """Why the gameplay block registry blocks this bump, or None.

    The registry records the pin set it was exported from, so moving a recorded
    pin would leave its blocks stale, as would a registry that is already
    behind the holders (an older recorded pin of this mod instead of the new
    one). The export downloads every pinned jar and
    cannot run offline from here, so a bump that writes gameplay holders stops
    with the order below. The mod side moves first and needs no re-export.
    A missing file predates the registry (gameplay PR #41) and is not a block.
    """
    path = Path(trees["gameplay"]) / REGISTRY
    if not path.is_file():
        return None
    try:
        recorded = json.loads(path.read_text()).get("pins", {})
    except (ValueError, AttributeError) as error:
        raise BumpError(f"gameplay {REGISTRY}: cannot read the recorded pins: {error}")
    if not isinstance(recorded, dict):
        raise BumpError(f"gameplay {REGISTRY}: recorded pins are not an object")
    recorded_hashes = {
        pin.get("hash") for pin in recorded.values() if isinstance(pin, dict)
    }
    stale = [
        jar.name
        for jar in olds
        if jar.name in recorded
        or jar.sha256 in recorded_hashes
        or jar.sha512 in recorded_hashes
    ]
    if not stale and new.name not in recorded and new.sha512 not in recorded_hashes:
        # Holders moved on without a re-export (e.g. they pin bf6 while the
        # registry still records bf5), so no bumped old is recorded but the
        # registry is still behind the new pin.
        stem = MODS[mod][0]
        stale = sorted(name for name in recorded if re.search(stem, name or ""))
    if not stale:
        return None
    return (
        f"gameplay {REGISTRY} pins {', '.join(stale)}: writing the gameplay"
        f" holders now would leave the registry stale. The mod side moves first"
        f" (`just bump-mod-jar {mod} {new.tag} --only mod`, then the mod release"
        " carrying the pins), then the gameplay holders move together with"
        " mod-server/common.properties (commonVersion/Url/Sha256 at the new mod"
        " release) and `just export-registry` in one gameplay PR: commit it, push"
        " and merge it into gameplay next, then"
        " `just vendor-block-registry <merge-sha>` for dedicated."
    )


def plan(mod, tag, trees, fetch, only=None):
    """(edits, new jar) for the holders that change; raises before any write.

    `only` limits the bump to one tree: a mod-only bump skips the gameplay
    holders and the registry guard, so the mod release can carry the pins first.
    """
    repo = f"{release.ORG}/{mod}"
    texts, jars = scan(mod, trees, fetch, only)
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
        if only is not None and side != only:
            continue  # other trees move in their own step of the chain
        for path in sorted(leftovers(trees[side], old_hashes)):
            if (side, path) == ("gameplay", REGISTRY):
                continue  # the registry has its own staleness check below
            if (side, path) not in texts:
                errors.append(
                    f"{side} {path}: holds an old hash but is not a known holder"
                )
    stale = (
        registry_stale(mod, trees, olds, new)
        if only is None or only == "gameplay"
        else None
    )
    if stale:
        errors.append(stale)
    if errors:
        raise BumpError("\n".join(errors))
    return edits, new


def common_version(gameplay):
    """commonVersion pinned in gameplay mod-server/common.properties, if any."""
    props = Path(gameplay) / "mod-server/common.properties"
    if not props.is_file():
        return None
    match = re.search(r"^commonVersion=(.+)$", props.read_text(), re.M)
    return match.group(1).strip() if match else None


def chain_note(trees):
    """Holder that only moves through a mod release: the common jar pin."""
    version = common_version(trees["gameplay"])
    at = f" (commonVersion={version})" if version else ""
    return (
        "note: gameplay AnimationManifest.PINS also arrive through the released"
        f" mod common jar (gameplay mod-server/common.properties{at}): a fork bump"
        f" needs {CHAIN}"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mod", choices=MODS)
    parser.add_argument("tag", nargs="?", help="fork release tag, e.g. bf8")
    parser.add_argument("--check", action="store_true", help="list holders and pins")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--only",
        choices=tuple(TREES),
        default=None,
        help="rewrite holders in one tree only (a mod-only bump skips the"
        " gameplay registry guard; the chain still applies)",
    )
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
            texts, jars = scan(args.mod, trees, fetch, args.only)
            for (side, path), text in texts.items():
                print(f"{side} {path}: {', '.join(pinned_tags(text, jars)) or 'none'}")
            print(chain_note(trees))
            return 0
        if not args.tag:
            parser.error("tag is required without --check")
        edits, new = plan(args.mod, args.tag, trees, fetch, args.only)
    except (BumpError, RuntimeError) as error:
        print(f"bump-mod-jar: {error}", file=sys.stderr)
        return 1
    if not args.dry_run:
        import mirror_mod_jar as mirror  # lazy: mirror imports this module

        try:
            mirror.require_mirrored(args.mod, args.tag, new)
        except (mirror.MirrorError, BumpError, RuntimeError) as error:
            print(f"bump-mod-jar: {error}", file=sys.stderr)
            return 1
    for side, path in edits:
        print(f"{'would change' if args.dry_run else 'changed'} {side} {path}")
    if args.only is not None:
        print(f"scope: --only {args.only} (other trees untouched)")
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
    print(f"remaining release chain for {args.mod} {args.tag}: {CHAIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
