#!/usr/bin/env python3
"""Fetch CI-built mod jars for a stand A/B block instead of local remapJar.

Given a mod PR, commit SHA or release version, verify the exact tree passed CI
(the `tested-tree-<tree>` artifact PR runs upload) and, when the tree was
released, download the release jars and verify SHA256SUMS. CI keeps no jars for
unreleased trees, so those fail closed with the exact local build command.

Read-only until the final install; never overwrites a different jar.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

REPO = "Blockfield/blockfield-mod"
SIDES = ("client", "server")


def run_gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_sha(repo, pr=None, sha=None, version=None):
    """Exact commit SHA for --pr (head), --sha (as-is) or --version (peeled tag)."""
    if version:
        return release_commit(repo, version), version
    if pr:
        return run_gh(
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "headRefOid",
            "--jq",
            ".headRefOid",
        ), None
    if sha:
        full = run_gh("api", f"repos/{repo}/commits/{sha}", "--jq", ".sha")
        return full, None
    raise ValueError("need one of --pr, --sha, --version")


def release_commit(repo, tag):
    """Peeled full commit SHA for a release tag.

    target_commitish is creation metadata (a branch, a short SHA, or a target
    that predates the tag move) — not proof of the tag commit — so resolve the
    tag ref itself and peel annotated tags instead of trusting that field."""
    quoted = urllib.parse.quote(tag, safe="")
    refs = json.loads(run_gh("api", f"repos/{repo}/git/matching-refs/tags/{quoted}"))
    exact = [r for r in refs if r.get("ref") == f"refs/tags/{tag}"]
    if len(exact) != 1:
        raise RuntimeError(f"{tag}: tag ref not found or ambiguous: {exact}")
    obj = exact[0]["object"]
    if obj["type"] == "commit":
        return obj["sha"]
    if obj["type"] == "tag":
        return run_gh(
            "api",
            f"repos/{repo}/git/tags/{obj['sha']}",
            "--jq",
            ".object.sha",
        )
    raise RuntimeError(f"{tag}: unexpected tag object type: {obj['type']}")


def commit_tree(repo, sha):
    return run_gh("api", f"repos/{repo}/git/commits/{sha}", "--jq", ".tree.sha")


def tested_tree_ok(repo, tree):
    """True when CI uploaded a fresh tested-tree marker for this exact tree."""
    try:
        # Plain URL query: gh turns -F form fields into a POST, which this
        # GET-only endpoint rejects (and the failure would read as "no marker").
        out = run_gh(
            "api",
            f"repos/{repo}/actions/artifacts?name=tested-tree-{tree}",
            "--jq",
            "[.artifacts[] | select(.expired == false)] | length",
        )
    except RuntimeError:
        return False
    return out.strip() not in ("", "0")


def find_release_for_sha(repo, sha, limit=30):
    """Release tag whose peeled tag commit is exactly `sha`, else None.

    Compares full SHAs from peeled tag refs only; a shared prefix never matches,
    and the release `target_commitish` metadata is not consulted."""
    out = run_gh(
        "release",
        "list",
        "--repo",
        repo,
        "--limit",
        str(limit),
        "--json",
        "tagName",
        "--jq",
        ".[].tagName",
    )
    for tag in out.splitlines():
        tag = tag.strip()
        if not tag:
            continue
        try:
            if release_commit(repo, tag) == sha:
                return tag
        except RuntimeError:
            continue
    return None


def runtime_jar_name(filename, side):
    return (
        filename.startswith(f"blockfield-{side}-")
        and filename.endswith(".jar")
        and not filename.endswith(("-sources.jar", "-dev.jar"))
    )


def download_and_verify(repo, tag, dest, sides=SIDES):
    """Download release jars + SHA256SUMS into `dest`; verify, then install without
    overwriting a different jar. Returns [(side, path)]."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        run_gh(
            "release",
            "download",
            tag,
            "--repo",
            repo,
            "-p",
            "blockfield-*.jar",
            "-p",
            "SHA256SUMS",
            "-D",
            str(staged),
        )
        sums = (staged / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        expected = {
            line.split()[-1]: line.split()[0] for line in sums if len(line.split()) == 2
        }
        installed = []
        for side in sides:
            jars = sorted(
                p
                for p in staged.iterdir()
                if p.is_file() and runtime_jar_name(p.name, side)
            )
            if len(jars) != 1:
                raise RuntimeError(
                    f"{tag}: expected one {side} runtime jar, found: {jars}"
                )
            jar = jars[0]
            digest = hashlib.sha256(jar.read_bytes()).hexdigest()
            if expected.get(jar.name) != digest:
                raise RuntimeError(
                    f"{jar.name}: checksum mismatch, refusing to install"
                )
            target = dest / jar.name
            if target.is_file():
                # A truncated earlier install or local build under the same name
                # must never pass as the verified input: hash and refuse.
                existing = hashlib.sha256(target.read_bytes()).hexdigest()
                if existing != digest:
                    raise RuntimeError(
                        f"{target.name}: existing file does not match the verified "
                        f"{tag} digest {digest}; refusing to use it"
                    )
            for old in dest.glob(f"blockfield-{side}-*.jar"):
                if old != target:
                    if old.is_file():
                        raise RuntimeError(
                            f"refusing to overwrite a different {side} jar: {old.name}"
                        )
            if not target.is_file():
                target.write_bytes(jar.read_bytes())
            installed.append((side, str(target)))
    return installed


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fetch_ci_jars.py", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int, help="mod PR number (uses its head SHA)")
    group.add_argument("--sha", help="mod commit SHA")
    group.add_argument("--version", help="release tag, e.g. v1.56.1")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--dest", required=True, help="files/mods dir to install into")
    parser.add_argument("--side", choices=[*SIDES, "both"], default="both")
    args = parser.parse_args(argv)

    sha, version = resolve_sha(args.repo, args.pr, args.sha, args.version)
    tree = commit_tree(args.repo, sha)
    if not tested_tree_ok(args.repo, tree):
        # A release tag implies green release CI, so only gate unreleased trees.
        version = version or find_release_for_sha(args.repo, sha)
        if version is None:
            raise SystemExit(
                f"{sha}: no fresh tested-tree marker (tree {tree}); CI never proved this "
                f"tree green. Build locally instead: just build-mods blockfield-mod"
            )
    version = version or find_release_for_sha(args.repo, sha)
    if version is None:
        raise SystemExit(
            f"{sha}: tree {tree} passed CI but was never released; CI keeps no jars "
            f"for unreleased trees. Build locally: just build-mods blockfield-mod"
        )
    sides = SIDES if args.side == "both" else (args.side,)
    installed = download_and_verify(args.repo, version, args.dest, sides)
    print(
        json.dumps(
            {"sha": sha, "tree": tree, "version": version, "installed": installed}
        )
    )


if __name__ == "__main__":
    main()
