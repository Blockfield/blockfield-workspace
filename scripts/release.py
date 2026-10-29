"""Release tooling: status, preflight, prepare, candidate, pin, ship.

Never creates or pushes tags and never starts Deploy, except through
`release ship`: tags and Deploy start production deploys and stay with the
owner, so ship refuses to run without --confirmed-by-owner. Other mutating
commands go through mutate() and honour --dry-run; ship's own merges and tags
go through ship_mutate(), which has the same dry-run behaviour.
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from toolchain import canonical_python, child_env, tool  # noqa: E402

ORG = "Blockfield"
MOD_REPO = f"{ORG}/blockfield-mod"
WORKSPACE_REPO = f"{ORG}/blockfield-workspace"
SIDES = ("client", "server")
REPO = {"client": f"{ORG}/blockfield-client", "server": f"{ORG}/blockfield-server"}
# Where the published release of each side lives (the client publishes into blockfield-releases).
RELEASES = {
    "client": f"{ORG}/blockfield-releases",
    "server": f"{ORG}/blockfield-server",
}
WORKFLOW = {"client": "client.yml", "server": "release.yml"}
PROD_BUILD = "https://blockfield.pro/build.json"
PROD_ROOMS = "https://blockfield.pro/api/rooms"
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
CACHE = Path(os.environ.get("BF_DEV_HOME") or ROOT / ".dev") / "release"
# Gitignored build inputs of a repo that a throwaway worktree must not download again.
SHARED = {"client": (".cache",), "server": (".cache", "files", "sources")}
# --mod / --decor: (repo, asset stem, metafile, title, side, hash algorithm, percent-encode '+' in url)
PINS = {
    "mod": (
        "blockfield-mod",
        "blockfield-client",
        "blockfield-client.pw.toml",
        "blockfield-client {tag}",
        "client",
        "sha1",
        False,
    ),
    "decor": (
        "blockfield-decor",
        "blockfield-decor",
        "blockfield-decor.pw.toml",
        "Blockfield Decor {tag}",
        "both",
        "sha512",
        True,
    ),
}


def parse(tag):
    match = TAG.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def pack_version(text):
    return tomllib.loads(text)["version"]


def newest(tags):
    return max((t for t in tags if parse(t)), key=parse, default=None)


def next_version(last_tag, version, kind):
    # A burnt tag above pack.toml must not be reused, so the larger of the two is the base.
    major, minor, patch = max(parse(last_tag or "v0.0.0"), parse(f"v{version}"))
    return (
        f"{major}.{minor + 1}.0" if kind == "minor" else f"{major}.{minor}.{patch + 1}"
    )


def check_tag(tag, version, existing):
    if not parse(tag):
        return [("FAIL", f"{tag!r} is not a vX.Y.Z tag")]
    same = tag == f"v{version}"
    out = [
        (
            "ok" if same else "FAIL",
            f"tag {tag} {'==' if same else '!='} pack.toml version {version}",
        )
    ]
    if tag in existing:
        return [*out, ("FAIL", f"tag {tag} already exists on origin")]
    top = newest(existing)
    above = top is None or parse(tag) > parse(top)
    return [
        *out,
        (
            "ok" if above else "FAIL",
            f"{tag} is greater than the highest existing tag {top}",
        ),
    ]


def plan(changed, compat, prod):
    """Release kind and the required order. compat holds the built contract of each changed side."""
    sides = [s for s in SIDES if changed.get(s)]
    if not sides:
        return "none", []
    moved = {s for s in sides if compat.get(s) != prod}
    if not moved:
        order = [("tag", s) for s in ("server", "client") if s in sides]
        return ("independent" if len(sides) == 2 else f"{sides[0]}-only"), order
    if moved == set(SIDES) and compat["client"] == compat["server"]:
        return "pair", [
            ("tag", "client"),
            ("candidate", "server"),
            ("tag", "server"),
            ("deploy-pair", "server"),
            ("deploy", "client"),
        ]
    return "mismatch", []


def run(*cmd, cwd=None, env=None, check=True):
    cmd = [str(c) for c in cmd]
    done = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
    if check and done.returncode:
        raise RuntimeError(
            f"{shlex.join(cmd)}\n{(done.stdout + done.stderr).strip()[-1500:]}"
        )
    return done.stdout.strip() if check else done


def git(repo, *args, **kw):
    return run("git", "-C", repo, *args, **kw)


def is_ancestor(repo, current, sha):
    """True when sha descends from current; any other git failure raises."""
    done = run(
        "git", "-C", repo, "merge-base", "--is-ancestor", current, sha, check=False
    )
    if done.returncode == 1:
        return False
    if done.returncode:
        raise RuntimeError(
            f"git -C {repo} merge-base --is-ancestor {current[:7]} {sha[:7]} failed:\n"
            f"{(done.stdout + done.stderr).strip()[-1500:]}"
        )
    return True


def gh(*args):
    return run("gh", *args)


def say(text):
    print(text, flush=True)


HELD_LABEL = "next-release"


def held_issues():
    """Open client/server issues with work held back for the next release (pins, tags, checks)."""
    found = []
    for side in SIDES:
        done = run(
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{REPO[side]}/issues?state=open&labels={HELD_LABEL}&per_page=100",
            check=False,
        )
        if done.returncode:
            reason = (getattr(done, "stderr", "") or "").strip().splitlines()
            hint = f": {reason[0]}" if reason else ""
            found.append(
                f"  {REPO[side]}: could not list `{HELD_LABEL}` issues, check by hand{hint}"
            )
            continue
        found += [
            f"  {REPO[side]}#{i['number']} {i['title']} {i['html_url']}"
            for page in json.loads(done.stdout or "[]")
            for i in page
            if "pull_request" not in i
        ]
    return found


def say_held():
    held = held_issues()
    if held:
        say(f"\nheld for this release (label `{HELD_LABEL}`, do them before tagging):")
        for line in held:
            say(line)


def mutate(dry, *cmd, cwd=None):
    """The only place that pushes branches or opens PRs. Refuses anything that could tag or deploy."""
    bad = ("--tags", "--follow-tags", "--mirror", "--all")
    if (
        tuple(cmd[:2]) != ("git", "push") and tuple(cmd[:3]) != ("gh", "pr", "create")
    ) or any(a in bad or "refs/tags" in a or parse(a) for a in cmd[2:]):
        raise ValueError(f"refusing to run {shlex.join(cmd)}")
    say(f"+ {shlex.join(cmd)}" + (f"   [in {cwd}]" if cwd else ""))
    return "(dry run)" if dry else run(*cmd, cwd=cwd)


def get_json(url):
    request = urllib.request.Request(
        url, headers={"Cache-Control": "no-cache", "User-Agent": "blockfield-release"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def prod_state():
    return get_json(PROD_BUILD) | {"server_build": get_json(PROD_ROOMS).get("build")}


def repo_dir(side):
    path = ROOT / side
    if not (path / ".git").exists():
        raise SystemExit(
            f"{path} is not checked out: git submodule update --init {side}"
        )
    return path


def fetch_main(repo):
    git(repo, "fetch", "-q", "origin", "main")


def remote_tags(repo):
    tags = {}
    for line in git(repo, "ls-remote", "--tags", "origin", "refs/tags/v*").splitlines():
        sha, ref = line.split("\t")
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        if parse(name):
            tags[name] = (
                sha  # the peeled commit of an annotated tag follows its tag object
            )
    return tags


def published(side):
    out = gh(
        "release",
        "list",
        "--repo",
        RELEASES[side],
        "--limit",
        "100",
        "--json",
        "tagName",
    )
    return newest(r["tagName"] for r in json.loads(out))


def show_pack(repo, ref):
    return pack_version(git(repo, "show", f"{ref}:pack.toml"))


@contextlib.contextmanager
def worktree(repo, ref, branch=None, side=None):
    CACHE.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f"{side or 'ws'}-", dir=CACHE))
    created = False
    try:
        git(
            repo,
            "worktree",
            "add",
            *(("-b", branch, "--no-track") if branch else ("--detach",)),
            path,
            ref,
        )
        created = bool(branch)
        for name in SHARED.get(side, ()):
            target = CACHE / "shared" / side / name
            target.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                (path / name).symlink_to(target, target_is_directory=True)
        yield path
    finally:
        git(repo, "worktree", "remove", "--force", path, check=False)
        if created:
            git(repo, "branch", "-D", branch, check=False)
        shutil.rmtree(path, ignore_errors=True)


def just(tree, *args):
    """Run a repo recipe with the pinned just and the canonical python as `python` (the justfiles' script interpreter)."""
    shim = CACHE / "bin"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "python"
    if link.resolve() != canonical_python().resolve():
        link.unlink(missing_ok=True)
        link.symlink_to(canonical_python())
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
    env["PATH"] = os.pathsep.join(
        [str(shim), str(Path(str(tool("just"))).parent), env["PATH"]]
    )
    return run("just", *args, cwd=tree, env=env)


def build(side, tree, tag, manifest=None):
    """Run the repo's own `just build` (it resolves and checksum-verifies every pin) and return build.json."""
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as out:
        out = Path(out) / "dist"
        steps = []
        if side == "server":
            steps = [
                ["python", "scripts/fetch-mods.py"],
                ["python", "scripts/maps/workshop_source.py"],
                ["python", "scripts/fetch-world-seed.py"],
                ["python", "scripts/maps/release_maps.py"],
            ]
            final = [
                "build",
                "--out",
                out,
                "--server-source",
                "server",
                "--world-seed",
                "sources/world-seed.zip",
            ]
            final += [
                "--build-id",
                tag,
                *(["--client-manifest", manifest] if manifest else []),
            ]
        else:
            final = ["build", "--out", out]
        for step in [*steps, final]:
            say(f"  {side}: just {' '.join(map(str, step[:2]))} ...")
            just(tree, *step)
        return json.loads(
            (out / ("web/build.json" if side == "client" else "build.json")).read_text()
        )


def preflight(side, tag, tree, manifest=None):
    """Checks that must pass before the tag. Returns (results, build.json or None)."""
    res = []
    repo = repo_dir(side)
    version = pack_version((tree / "pack.toml").read_text())
    existing = remote_tags(repo)
    published_tag = published(side)
    res += check_tag(
        tag, version, {*existing, *([published_tag] if published_tag else [])}
    )
    workflow = (tree / ".github/workflows" / WORKFLOW[side]).read_text()
    if "release-notes-tool.py check" in workflow:
        notes = tree / "release-notes" / f"{tag}.md"
        if not notes.is_file():
            res.append(
                (
                    "FAIL",
                    f"release-notes/{tag}.md missing (the tag workflow requires it)",
                )
            )
        else:
            try:
                just(
                    tree,
                    "python",
                    "scripts/release-notes-tool.py",
                    "check",
                    notes,
                    "--repository",
                    RELEASES[side],
                    "--version",
                    tag,
                    "--kind",
                    side,
                )
                res.append(("ok", f"release-notes/{tag}.md valid"))
            except RuntimeError as error:
                res.append(("FAIL", f"release notes: {error}"))
    say(f"building {side} {tag} (downloads and verifies every pin) ...")
    try:
        meta = build(side, tree, tag, manifest)
    except RuntimeError as error:
        return [*res, ("FAIL", f"just build failed:\n{error}")], None
    res.append(
        ("ok", f"just build verified every pin; contract {meta['compatibility'][:12]}")
    )
    return [*res, *contract_checks(side, meta, prod_state())], meta


def contract_checks(side, meta, prod):
    moved = meta["compatibility"] != prod["compatibility"]
    if not moved:
        res = [("ok", "contract unchanged vs production")]
    elif side == "client":
        res = [
            (
                "WARN",
                "contract differs from production: pair release, see order in `release status`",
            )
        ]
        res.append(
            (
                "WARN",
                "Deploy client stages invisibly until the server accepts this contract",
            )
        )
    else:
        res = [
            (
                "WARN",
                "contract differs from production: pair release, see order in `release status`",
            )
        ]
    if side == "server" and meta["client"] != prod["client"]:
        gate = f"server gates on client v{meta['client_version']}, production serves {prod['build']}"
        if moved:
            res.append(
                (
                    "WARN",
                    f"{gate}: Deploy server needs client_tag=v{meta['client_version']}",
                )
            )
        else:
            res.append(
                ("FAIL", f"{gate}: auto-deploy would stop; repin client_candidate")
            )
    return res


def show(res):
    for level, text in res:
        say(f"  [{level:<4}] {text}")
    return not any(level == "FAIL" for level, _ in res)


def pin_jar(tree, key, tag):
    repo, stem, meta, title, side, algo, plus = PINS[key]
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        gh(
            "release",
            "download",
            tag,
            "--repo",
            f"{ORG}/{repo}",
            "-p",
            f"{stem}-{tag.removeprefix('v')}*.jar",
            "-D",
            tmp,
        )
        jars = [
            p for p in Path(tmp).glob("*.jar") if not p.name.endswith("-sources.jar")
        ]
        if len(jars) != 1:
            raise RuntimeError(
                f"{repo} {tag}: expected one {stem} jar, got {[p.name for p in jars]}"
            )
        name, data = jars[0].name, jars[0].read_bytes()
    url = f"https://github.com/{ORG}/{repo}/releases/download/{tag}/{quote(name) if plus else name}"
    (tree / "mods" / meta).write_text(
        f'name = "{title.format(tag=tag)}"\nfilename = "{name}"\nside = "{side}"\n\n[download]\nurl = "{url}"\n'
        f'hash-format = "{algo}"\nhash = "{hashlib.new(algo, data).hexdigest()}"\n',
        newline="\n",
    )
    return f"mods/{meta}"


def refresh_index(tree, files):
    """What `packwiz refresh` would change for edited metafiles; the client build verifies the result."""
    index = tree / "index.toml"
    if not index.is_file():
        return
    text = index.read_text()
    for name in files:
        digest = hashlib.sha256((tree / name).read_bytes()).hexdigest()
        text, count = re.subn(
            rf'(file = "{re.escape(name)}"\nhash = ")[0-9a-f]+', rf"\g<1>{digest}", text
        )
        if count != 1:
            raise RuntimeError(f"{name} is not listed in index.toml")
    index.write_text(text, newline="\n")
    pack = tree / "pack.toml"
    pack.write_text(
        re.sub(
            r'(?m)^hash = ".*"$',
            f'hash = "{hashlib.sha256(index.read_bytes()).hexdigest()}"',
            pack.read_text(),
        ),
        newline="\n",
    )


def set_version(tree, version):
    pack = tree / "pack.toml"
    pack.write_text(
        re.sub(
            r'(?m)^version = ".*"$', f'version = "{version}"', pack.read_text(), count=1
        ),
        newline="\n",
    )


def set_candidate(tree, tag):
    """Pin the published client release the server gates on (version + SHA256 of its build.json)."""
    url = f"https://github.com/{ORG}/blockfield-releases/releases/download/{tag}/build.json"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if json.loads(data).get("build") != tag:
        raise RuntimeError(f"{url} does not report build {tag}")
    path = tree / "client-compatibility.json"
    policy = json.loads(path.read_text())
    policy["client_candidate"] = {
        "version": tag,
        "manifest_sha256": hashlib.sha256(data).hexdigest(),
    }
    path.write_text(json.dumps(policy, indent=2) + "\n", newline="\n")


def open_pr(dry, repo, tree, branch, title, message, body=""):
    git(tree, "add", "-u")
    say(git(tree, "diff", "--cached", "--stat"))
    say(f"+ git commit -m {shlex.quote(message)}   [in {tree}]")
    git(tree, "commit", "-q", "-m", message)
    mutate(dry, "git", "push", "-u", "origin", branch, cwd=tree)
    url = mutate(
        dry,
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
        cwd=tree,
    )
    say(f"  PR: {url}")
    return url


def pr_branch(side, tag):
    return f"release/{side}-{tag.removeprefix('v')}"


def prepare_side(side, args, prod, manifest, repin):
    repo = repo_dir(side)
    fetch_main(repo)
    version = next_version(
        newest(remote_tags(repo)), show_pack(repo, "origin/main"), args.bump
    )
    tag, branch = f"v{version}", pr_branch(side, f"v{version}")
    if git(repo, "ls-remote", "--heads", "origin", branch):
        raise SystemExit(
            f"{branch} already exists on origin; delete or reuse it manually"
        )
    say(f"== {side} {tag}")
    with worktree(repo, "origin/main", branch, side) as tree:
        files, parts = [], []
        if side == "server" and args.server_merge:
            git(tree, "fetch", "-q", "origin", f"refs/heads/{args.server_merge}")
            sha = git(tree, "rev-parse", "FETCH_HEAD")
            say(f"  merge server {args.server_merge} ({sha}) into release branch")
            git(tree, "merge", "--no-ff", "--no-edit", sha)
            parts.append(f"merge {args.server_merge} {sha[:7]}")
        if side == "client":
            files.append(pin_jar(tree, "mod", args.mod))
        else:
            run("sh", "scripts/bump-mod.sh", args.mod, cwd=tree)
        parts.append(f"mod {args.mod}")
        if args.decor:
            files.append(pin_jar(tree, "decor", args.decor))
            parts.append(f"decor {args.decor}")
        if not git(tree, "diff", "origin/main", "--name-only"):
            say("  pins unchanged: nothing to release on this side")
            return None
        if repin:
            set_candidate(tree, "v" + prod["client_version"])
            parts.append(f"client candidate v{prod['client_version']}")
        set_version(tree, version)
        refresh_index(tree, files)
        res, meta = preflight(side, tag, tree, manifest)
        if not show(res):
            raise SystemExit(f"preflight failed for {side} {tag}: nothing was pushed")
        open_pr(
            args.dry_run,
            REPO[side],
            tree,
            branch,
            f"Release {side} {version}",
            f"Release {side} {version}: {', '.join(parts)}",
        )
    return tag, meta


def tag_lines(order, tags, branch):
    lines = []
    for action, side in order:
        tag = tags.get(side)
        if action == "tag":
            lines.append(
                f"wait for green {side} PR checks at pinned head"
                + (" including image SUCCESS" if side == "server" else "")
                + f"; merge that head; preflight {side} {tag} at merged SHA"
            )
            lines.append(
                f'git -C {side} fetch origin main && git -C {side} tag -a {tag} -m {tag} "$MERGED_SHA" && git -C {side} push origin {tag}'
                "   # MERGED_SHA is the exact release-PR merge commit"
            )
            if any(a == "candidate" for a, _ in order):
                jobs = (
                    "check (includes publish)"
                    if side == "client"
                    else "prepare + release"
                )
                lines.append(
                    f"wait for {side} tag push run: {jobs} SUCCESS; verify published assets; "
                    + (
                        "automatic Deploy SUCCESS (staged); client remains prerelease"
                        if side == "client"
                        else "continue without waiting for automatic Deploy; any observed red must match the known compatibility refusal"
                    )
                )
        elif action == "candidate":
            lines.append(
                f"just release candidate {tags['client']} --branch {branch}   # preflight, push; wait for NEW server CI"
            )
        elif action == "deploy-pair":
            lines.append(
                f"assert client {tags['client']} is still a prerelease before the server switch"
            )
            lines.append(
                shlex.join(deploy_command("server", tags["server"], tags["client"]))
                + "   # wait for this new dispatch run to succeed"
            )
        else:
            lines.append(
                shlex.join(deploy_command("client", tags["client"]))
                + "   # wait for this new dispatch run to succeed"
            )
    return lines


def prepare_all(args):
    """Run prepare and return (tags, kind, order, server_branch). Shared by prepare and ship."""
    if args.only == "client" and args.server_merge:
        raise ValueError("--server-merge cannot be used with --only client")
    prod = prod_state()
    say_held()
    sides = [args.only] if args.only else list(SIDES)
    results, manifest = {}, None
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=CACHE) as scratch:
        for side in sides:
            moved_pair = manifest is not None
            got = prepare_side(
                side, args, prod, manifest, repin=side == "server" and not moved_pair
            )
            if got and side == "client":
                tag, meta = got
                if meta["compatibility"] != prod["compatibility"]:
                    path = Path(scratch) / "client-build.json"
                    path.write_text(json.dumps(meta))
                    manifest = path.as_uri()
            if got:
                results[side] = got
    tags = {s: r[0] for s, r in results.items()}
    compat = {s: r[1]["compatibility"] for s, r in results.items()}
    kind, order = plan({s: True for s in results}, compat, prod["compatibility"])
    branch = pr_branch("server", tags["server"]) if "server" in tags else ""
    return tags, kind, order, branch


def cmd_prepare(args):
    tags, kind, order, branch = prepare_all(args)
    say(f"\nrelease kind: {kind}")
    if kind == "mismatch":
        say(
            "client and server contracts do not match each other or production: fix before tagging"
        )
        return False
    say(
        "Next steps. Tags and Deploy start production: run each only after the owner confirms it explicitly in the conversation."
    )
    for number, line in enumerate(tag_lines(order, tags, branch), 1):
        say(f"  {number}. {line}")
    if kind == "pair":
        say(
            "The server PR stays red until the candidate step pins the published client."
        )
    return True


def cmd_candidate(args):
    repo = repo_dir("server")
    git(repo, "fetch", "-q", "origin", args.branch)
    with worktree(repo, f"origin/{args.branch}", args.branch, "server") as tree:
        set_candidate(tree, args.tag)
        tag = "v" + pack_version((tree / "pack.toml").read_text())
        res, _ = preflight("server", tag, tree)
        if not show(res):
            raise SystemExit("preflight failed: nothing was pushed")
        git(tree, "add", "-u")
        say(f"+ git commit -m 'Pin client candidate {args.tag}'   [in {tree}]")
        git(tree, "commit", "-q", "-m", f"Pin client candidate {args.tag}")
        sha = git(tree, "rev-parse", "HEAD")
        mutate(args.dry_run, "git", "push", "origin", args.branch, cwd=tree)
    return sha


def cmd_preflight(args):
    repo = repo_dir(args.side)
    fetch_main(repo)
    with worktree(repo, args.ref, side=args.side) as tree:
        res, _ = preflight(args.side, args.tag, tree)
    return show(res)


PIN_URL = re.compile(
    r'^[^:]+:mods/([^:]+)\.pw\.toml:url = "https://github\.com/(Blockfield/[^/]+)/releases/download/([^/"]+)/'
)


def pins(repo, ref):
    out = git(
        repo,
        "grep",
        "-I",
        "--no-color",
        "-e",
        "^url = ",
        ref,
        "--",
        "mods",
        check=False,
    ).stdout
    return [
        (m[1], m[2], unquote(m[3])) for m in map(PIN_URL.match, out.splitlines()) if m
    ]


def latest_tag(repo):
    out = json.loads(
        gh("release", "list", "--repo", repo, "--limit", "1", "--json", "tagName")
    )
    return out[0]["tagName"] if out else None


def cmd_status(args):
    prod = prod_state()
    say(
        f"production: client {prod['build']}, server {prod['server_build']}, contract {prod['compatibility'][:12]}"
    )
    say_held()
    changed, compat, broken = {}, {}, False
    mods = {}
    for side in SIDES:
        repo = repo_dir(side)
        fetch_main(repo)
        tags, last = remote_tags(repo), published(side)
        log = (
            git(
                repo,
                "log",
                "--no-merges",
                "--format=%h %s",
                f"{tags[last]}..origin/main",
            ).splitlines()
            if last in tags
            else []
        )
        changed[side] = bool(log)
        mods[side] = pins(repo, "origin/main")
        say(
            f"\n{side}: last published {last}, pack.toml on main {show_pack(repo, 'origin/main')}, {len(log)} unreleased commit(s)"
        )
        for line in log[:8]:
            say(f"  {line}")
    repos = sorted({repo for side in SIDES for _, repo, _ in mods[side]})
    with ThreadPoolExecutor(8) as pool:
        latest = dict(zip(repos, pool.map(latest_tag, repos)))
    for side in SIDES:
        behind = [
            (n, t, latest[r]) for n, r, t in mods[side] if latest[r] not in (None, t)
        ]
        say(
            f"\n{side} pins: {len(mods[side]) - len(behind)} of {len(mods[side])} Blockfield-hosted pins at their latest release"
        )
        for name, tag, new in behind:
            say(f"  behind: {name} {tag} -> {new}")
    for side in SIDES:
        if changed[side]:
            repo = repo_dir(side)
            with worktree(repo, "origin/main", side=side) as tree:
                say(f"\nbuilding {side} main to read its contract ...")
                try:
                    tag = "v" + show_pack(repo, "origin/main")
                    meta = build(side, tree, tag)
                    compat[side] = meta["compatibility"]
                    broken |= not show(contract_checks(side, meta, prod))
                except RuntimeError as error:
                    broken = True
                    say(f"{side} main does not build/pass its checks:\n{error}")
    if broken:
        say("\nrelease kind: blocked by the failures above")
        return False
    kind, order = plan(changed, compat, prod["compatibility"])
    say(f"\nrelease kind: {kind}")
    if kind == "mismatch":
        say(
            "contract moved on one side only, or the sides disagree: the pair cannot be released as is"
        )
    for number, (action, side) in enumerate(order, 1):
        say(f"  {number}. {action} {side}")
    return kind != "mismatch"


def tag_commit(repo, tag):
    found = None
    for line in git(repo, "ls-remote", "origin", f"refs/tags/{tag}*").splitlines():
        sha, ref = line.split("\t")
        if ref in (f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"):
            found = sha  # the peeled line of an annotated tag comes last
    if not found:
        raise RuntimeError(f"tag {tag} does not exist in {repo}")
    return found


def cmd_pin(args):
    prod = prod_state()
    want = {
        "client": args.client or prod["build"],
        "server": args.server or prod["server_build"],
    }
    git(ROOT, "fetch", "-q", "origin", "main")
    base = args.base
    changes, skipped = {}, set()

    def pinned(path):
        out = git(ROOT, "ls-tree", base, path)
        return out.split()[2] if out.startswith("160000") else None

    def advance(path, tag, label):
        repo = ROOT / path
        if not (repo / ".git").exists():
            if path not in skipped:
                say(f"skip {path}: submodule is not checked out")
            skipped.add(path)
            return
        sha = tag_commit(repo, tag)
        current = changes.get(path, (pinned(path),))[0]
        if sha == current:
            return
        git(repo, "fetch", "-q", "origin", sha)
        if not is_ancestor(repo, current, sha):
            say(f"skip {path}: {sha[:7]} is not ahead of the pinned {current[:7]}")
            return
        if path in changes and changes[path][0] != sha:
            say(f"warning: {path}: {label} overrides {changes[path][1]}")
        changes[path] = (sha, label)

    for side in SIDES:
        tag = want[side]
        if tag != prod["build" if side == "client" else "server_build"]:
            say(f"warning: {side} {tag} is not what production runs")
        advance(side, tag, f"{side} {tag.removeprefix('v')}")
        for _, repo_name, mod_tag in pins(
            repo_dir(side), tag_commit(repo_dir(side), tag)
        ):
            path = f"localmods/{repo_name.split('/')[1]}"
            if pinned(path):
                short = path.split("/")[1].removeprefix("blockfield-").split("-")[0]
                advance(path, mod_tag, f"{short} {mod_tag.removeprefix('v')}")
    if not changes:
        say("nothing to pin: the workspace already pins what production runs")
        return True
    labels = [label for _, label in changes.values()]
    title = "Pin released " + ", ".join(labels)
    branch = "release/pins-" + re.sub(r"[^\w.]+", "-", "-".join(labels))
    body = "Pins the released and deployed versions:\n" + "".join(
        f"- {label} `{sha[:7]}`\n" for sha, label in changes.values()
    )
    with worktree(ROOT, base, branch) as tree:
        for path, (sha, _) in changes.items():
            git(tree, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")
        say(git(tree, "diff", "--cached", "--stat"))
        say(f"+ git commit -m {shlex.quote(title)}   [in {tree}]")
        git(tree, "commit", "-q", "-m", title)
        mutate(args.dry_run, "git", "push", "-u", "origin", branch, cwd=tree)
        say(
            "  PR: "
            + mutate(
                args.dry_run,
                "gh",
                "pr",
                "create",
                "--repo",
                f"{ORG}/blockfield-workspace",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
                cwd=tree,
            )
        )
    return branch


def wait_until(label, timeout_s, interval_s, ready):
    """Poll ready() until it returns truthy; return its value. Bounded; raises on timeout."""
    start = time.monotonic()
    while True:
        got = ready()
        if got:
            return got
        if time.monotonic() - start >= timeout_s:
            raise RuntimeError(f"timed out waiting for {label} after {timeout_s}s")
        time.sleep(interval_s)


def mod_published(tag):
    done = run("gh", "release", "view", tag, "--repo", MOD_REPO, check=False)
    if done.returncode == 0:
        return True
    err = (done.stderr or "").lower()
    if "not found" in err or "no release" in err:
        return False
    raise RuntimeError(
        f"gh release view {tag} failed:\n{(done.stdout + done.stderr).strip()[-1500:]}"
    )


def pr_number(repo, branch):
    items = json.loads(
        gh(
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--limit",
            "1",
            "--json",
            "number,url",
        )
        or "[]"
    )
    return items[0]["number"] if items else None


def pr_checks(repo, number, expected_head=None, required_jobs=()):
    """'green' when every check succeeded, 'red' on any failure, else 'pending'."""
    data = json.loads(
        gh(
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "headRefOid,statusCheckRollup",
        )
        or "{}"
    )
    if expected_head and data.get("headRefOid") != expected_head:
        return "pending"
    rollup = data.get("statusCheckRollup", [])
    if not rollup:
        return "pending"
    if any(
        c.get("conclusion") in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED")
        for c in rollup
    ):
        return "red"
    for name in required_jobs:
        jobs = [c for c in rollup if c.get("name") == name]
        if not jobs:
            return "pending"
        if any(c.get("conclusion") in ("SKIPPED", "NEUTRAL") for c in jobs):
            return "red"
        if not all(
            c.get("status") == "COMPLETED" and c.get("conclusion") == "SUCCESS"
            for c in jobs
        ):
            return "pending"
    if all(
        c.get("status") == "COMPLETED"
        and c.get("conclusion") in ("SUCCESS", "SKIPPED", "NEUTRAL")
        for c in rollup
    ):
        return "green"
    return "pending"


def wait_pr_green(repo, branch, timeout_s, interval_s, expected_head=None):
    number = pr_number(repo, branch)
    if number is None:
        raise RuntimeError(f"no open PR for {repo} branch {branch}")
    if expected_head is None:
        expected_head = json.loads(
            gh("pr", "view", str(number), "--repo", repo, "--json", "headRefOid")
        ).get("headRefOid")
    if not isinstance(expected_head, str) or not re.fullmatch(
        r"[0-9a-f]{40}", expected_head
    ):
        raise RuntimeError(f"invalid PR head for {repo} #{number}")

    def ready():
        state = pr_checks(
            repo,
            number,
            expected_head,
            required_jobs=("image",) if repo == REPO["server"] else (),
        )
        if state == "red":
            raise RuntimeError(
                f"checks are red on {repo} PR #{number} ({branch}): fix before shipping"
            )
        return state == "green"

    label = f"green checks on {repo} PR #{number}" + (
        " including image SUCCESS on the final head" if repo == REPO["server"] else ""
    )
    wait_until(label, timeout_s, interval_s, ready)
    return number, expected_head


def ship_mutate(dry, *cmd, cwd=None):
    """ship's own merges and tags (mutate() refuses those). Only called with --confirmed-by-owner."""
    say(f"+ {shlex.join(cmd)}" + (f"   [in {cwd}]" if cwd else ""))
    return "(dry run)" if dry else run(*cmd, cwd=cwd)


def merge_pr(repo, number, dry, expected_head=None):
    ship_mutate(
        dry,
        "gh",
        "pr",
        "merge",
        str(number),
        "--repo",
        repo,
        "--merge",
        "--delete-branch",
        *(["--match-head-commit", expected_head] if expected_head else []),
    )
    if dry:
        return "(dry run)"
    data = json.loads(
        gh(
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            "headRefOid,mergeCommit,state",
        )
    )
    if data.get("state") != "MERGED" or (
        expected_head and data.get("headRefOid") != expected_head
    ):
        raise RuntimeError(
            f"{repo} #{number} did not merge the checked head; nothing was tagged"
        )
    sha = (data.get("mergeCommit") or {}).get("oid")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"no merged commit for {repo} #{number}; nothing was tagged")
    return sha


def push_tag(side, tag, dry, expected_sha=None):
    """Tag the merged origin/main and push it. Returns the tagged commit sha."""
    if dry:
        say(
            f"+ git -C {side} fetch origin main && git -C {side} tag -a {tag} "
            f"-m {tag} origin/main && git -C {side} push origin {tag}"
        )
        return "(dry run)"
    repo = repo_dir(side)
    git(repo, "fetch", "-q", "origin", "main")
    sha = git(repo, "rev-parse", "origin/main")
    if expected_sha and sha != expected_sha:
        raise RuntimeError(
            f"{side} main differs from the merged commit; nothing was tagged"
        )
    with worktree(repo, sha, side=side) as tree:
        res, _ = preflight(side, tag, tree)
        if not show(res):
            raise RuntimeError(f"preflight failed before tagging {side} {tag}")
    fetch_main(repo)
    if git(repo, "rev-parse", "origin/main") != sha:
        raise RuntimeError(f"{side} main moved during preflight; nothing was tagged")
    git(repo, "tag", "-a", tag, "-m", tag, sha)
    git(repo, "push", "origin", tag)
    return sha


def release_run(repo, workflow, sha, tag):
    items = json.loads(
        gh(
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--branch",
            tag,
            "--event",
            "push",
            "--limit",
            "20",
            "--json",
            "databaseId,headSha,status,conclusion,url",
        )
        or "[]"
    )
    return next((r for r in items if r.get("headSha") == sha), None)


def wait_release_run(side, sha, timeout_s, interval_s, tag):
    found = wait_until(
        f"release run for {side} {sha[:7]}",
        timeout_s,
        interval_s,
        lambda: release_run(REPO[side], WORKFLOW[side], sha, tag),
    )
    say(f"  {side} run: {found['url']}")
    return found


def wait_run_success(side, run_id, timeout_s, interval_s):
    def ready():
        found = json.loads(
            gh(
                "run",
                "view",
                str(run_id),
                "--repo",
                REPO[side],
                "--json",
                "status,conclusion,url",
            )
            or "{}"
        )
        conclusion = (found.get("conclusion") or "").upper()
        if conclusion == "SUCCESS":
            return found
        if conclusion:
            raise RuntimeError(f"{side} release run {conclusion}: {found.get('url')}")
        return None

    wait_until(f"successful {side} release run {run_id}", timeout_s, interval_s, ready)


def deploy_jobs(repo, run_id):
    data = json.loads(
        gh("api", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100") or "{}"
    )
    return [
        (j.get("name"), j.get("status"), j.get("conclusion"))
        for j in data.get("jobs", [])
    ]


def wait_deploy_started(side, run_id, timeout_s, interval_s):
    """Wait until the tag run's deploy job is in_progress (or done): only then is it safe to tag the other side."""

    def ready():
        jobs = deploy_jobs(REPO[side], run_id)
        deploy = [j for j in jobs if "deploy" in (j[0] or "").lower()]
        if any(c in ("FAILURE", "CANCELLED", "TIMED_OUT") for _, _, c in deploy):
            raise RuntimeError(
                f"{side} deploy job failed before the client tag: see the run URL above"
            )
        return any(s in ("in_progress", "completed") for _, s, _ in deploy)

    wait_until(f"{side} deploy job started", timeout_s, interval_s, ready)


def check_client_release(tag, prerelease, latest=False):
    base = f"repos/{RELEASES['client']}/releases"
    data = json.loads(gh("api", f"{base}/tags/{tag}"))
    if (
        data.get("tag_name") != tag
        or data.get("draft") is not False
        or data.get("prerelease") is not prerelease
    ):
        raise RuntimeError(f"client {tag}: expected published prerelease={prerelease}")
    if latest:
        current = json.loads(gh("api", f"{base}/latest"))
        if (
            current.get("tag_name") != tag
            or current.get("prerelease") is not False
            or current.get("draft") is not False
        ):
            raise RuntimeError(f"client {tag} is not the promoted Latest release")
    return data


def verify_publication(side, tag):
    data = (
        check_client_release(tag, True)
        if side == "client"
        else json.loads(gh("api", f"repos/{RELEASES[side]}/releases/tags/{tag}"))
    )
    if data["draft"] or data["tag_name"] != tag:
        raise RuntimeError(f"{side} {tag}: release is not published")
    assets = {a["name"]: a for a in data["assets"]}
    required = {"release-notes.md"}
    if side == "client":
        required |= {
            "build.json",
            "SHA256SUMS",
            "Blockfield-client.zip",
            "Blockfield-prism.zip",
            "Blockfield.mrpack",
            "Blockfield-web.tar",
        }
    if required - assets.keys():
        raise RuntimeError(
            f"{side} {tag}: missing assets {sorted(required - assets.keys())}"
        )
    if side == "server":
        return
    with tempfile.TemporaryDirectory(dir=CACHE) as tmp:
        gh(
            "release",
            "download",
            tag,
            "--repo",
            RELEASES[side],
            "-p",
            "build.json",
            "-p",
            "SHA256SUMS",
            "-D",
            tmp,
        )
        sums = dict(
            line.split("  ", 1)[::-1]
            for line in (Path(tmp) / "SHA256SUMS").read_text().splitlines()
        )
        for name in required - {"SHA256SUMS", "release-notes.md"}:
            digest = sums.get(name)
            if not digest or assets[name].get("digest") != f"sha256:{digest}":
                raise RuntimeError(
                    f"{tag}: {name} differs from SHA256SUMS / GitHub asset digest"
                )
        manifest = (Path(tmp) / "build.json").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != sums["build.json"]:
            raise RuntimeError(f"{tag}: downloaded build.json differs from SHA256SUMS")
        meta = json.loads(manifest)
        if meta.get("build") != tag:
            raise RuntimeError(f"{tag}: build.json reports a different release")
        return meta


def wait_publication(side, tag, run_id, timeout_s, interval_s):
    """Pair tags publish first; their automatic Deploy cannot switch the old pair."""
    required = {"check"} if side == "client" else {"prepare", "release"}
    refusal_step = "Validate target and remember previous pair"
    refusal = "Server gates on another client than the served one: rerun Deploy server with client_tag"

    def ready():
        data = json.loads(
            gh(
                "run",
                "view",
                str(run_id),
                "--repo",
                REPO[side],
                "--json",
                "jobs,status,conclusion,url",
            )
        )
        deploy = [
            j
            for j in data.get("jobs", [])
            if j["name"].split("/")[0].strip() == "deploy"
        ]
        jobs = {j["name"]: j for j in data.get("jobs", []) if j not in deploy}
        for job in jobs.values():
            if job.get("conclusion") and job["conclusion"].lower() != "success":
                raise RuntimeError(
                    f"{side} publication job {job['name']} {job['conclusion']}: {data['url']}"
                )
        for job in deploy:
            conclusion = job.get("conclusion")
            if conclusion in (None, "", "success"):
                continue
            if side == "client":
                raise RuntimeError(
                    f"client automatic Deploy must stage successfully: {data['url']}"
                )
            failed_steps = [
                s["name"]
                for s in job.get("steps", [])
                if s.get("conclusion") == "failure"
            ]
            if conclusion != "failure" or failed_steps != [refusal_step]:
                raise RuntimeError(
                    f"{side} automatic Deploy failed outside the expected compatibility refusal: {data['url']}"
                )
            if data["status"] != "completed":
                return False  # gh exposes --log-failed only after the run finishes.
            if refusal not in gh(
                "run", "view", str(run_id), "--repo", REPO[side], "--log-failed"
            ):
                raise RuntimeError(
                    f"{side} automatic Deploy failed outside the expected compatibility refusal: {data['url']}"
                )
        if data.get("conclusion") not in (None, "", "success", "failure"):
            raise RuntimeError(f"{side} tag run {data['conclusion']}: {data['url']}")
        if not required <= jobs.keys() or any(
            j.get("conclusion") != "success" for j in jobs.values()
        ):
            if data["status"] == "completed":
                raise RuntimeError(
                    f"{side} publication jobs did not succeed: {data['url']}"
                )
            return False
        if side == "client" and (
            not deploy or any(j.get("conclusion") != "success" for j in deploy)
        ):
            if data["status"] == "completed":
                raise RuntimeError(
                    f"client automatic Deploy must stage successfully: {data['url']}"
                )
            return False
        if data.get("conclusion") == "failure" and (
            side == "client"
            or not any(j.get("conclusion") == "failure" for j in deploy)
        ):
            raise RuntimeError(f"{side} tag run failed: {data['url']}")
        say(
            f"  {side} publication jobs succeeded; automatic Deploy result: {data.get('conclusion')}"
        )
        return True

    wait_until(
        f"{side} publication jobs",
        timeout_s,
        interval_s,
        ready,
    )
    return verify_publication(side, tag)


def deploy_command(side, tag, client_tag=None):
    return (
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{REPO[side]}/actions/workflows/deploy.yml/dispatches",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-f",
        "ref=main",
        "-F",
        "return_run_details=true",
        "-f",
        f"inputs[tag]={tag}",
        *(["-f", f"inputs[client_tag]={client_tag}"] if client_tag else []),
    )


def dispatch_deploy(side, tag, timeout_s, interval_s, client_tag=None, dry=False):
    # Ask GitHub for the dispatched run ID: a list-by-SHA can select someone else's deploy.
    result = ship_mutate(dry, *deploy_command(side, tag, client_tag))
    if dry:
        return
    data = json.loads(result or "{}")
    run_id = data.get("workflow_run_id")
    if not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError(
            f"{side} Deploy dispatched without a run ID; inspect Actions, do not retry"
        )
    say(f"  Deploy {side}: {data['html_url']}")
    wait_run_success(side, run_id, timeout_s, interval_s)


def verify_pair(tags, client, timeout_s, interval_s):
    def ready():
        prod = prod_state()
        return (
            prod.get("build") == tags["client"]
            and prod.get("server_build") == tags["server"]
            and prod.get("client") == client["client"]
            and prod.get("compatibility") == client["compatibility"]
        )

    wait_until(
        "production pair versions, client digest and contract",
        timeout_s,
        interval_s,
        ready,
    )
    check_client_release(tags["client"], False, latest=True)
    say("  production pair verified (public build.json and /api/rooms)")


def ship_tag_side(
    args, side, tag, timeout_s, interval_s, publish_only=False, expected_head=None
):
    """Merge, preflight, tag and wait; pair publication returns the client manifest."""
    if args.dry_run:
        say(
            f"dry run: merge/preflight/tag {side} {tag}; wait for publication"
            if publish_only
            else f"dry run: merge/preflight/tag {side} {tag}; wait for full tag run"
        )
        return
    branch = pr_branch(side, tag)
    number, head = wait_pr_green(
        REPO[side], branch, timeout_s, interval_s, expected_head
    )
    merged = merge_pr(
        REPO[side],
        number,
        args.dry_run,
        head,
    )
    sha = push_tag(side, tag, args.dry_run, merged)
    say(f"  {side} {tag} -> {sha[:7]}")
    found = wait_release_run(side, sha, timeout_s, interval_s, tag)
    if publish_only:
        return wait_publication(side, tag, found["databaseId"], timeout_s, interval_s)
    wait_run_success(side, found["databaseId"], timeout_s, interval_s)
    return sha


def cmd_ship(args):
    if not args.confirmed_by_owner and not args.dry_run:
        raise SystemExit(
            "refusing: `release ship` pushes tags, and tags start production deploys. "
            "Rerun with --confirmed-by-owner after the owner confirms this release explicitly in the conversation."
        )
    dry, timeout_s, interval_s = args.dry_run, args.timeout, args.interval
    say(f"== ship {args.mod}" + (f" ({args.only})" if args.only else ""))
    wait_until(
        f"published mod release {args.mod}",
        timeout_s,
        interval_s,
        lambda: mod_published(args.mod),
    )
    say(f"  mod {args.mod} is published")
    tags, kind, order, branch = prepare_all(args)
    if kind in ("none", "mismatch") or not tags:
        raise SystemExit(f"ship stopped: release kind is {kind}")
    say(f"\nrelease kind: {kind}")
    if dry:
        lines = tag_lines(order, tags, branch)
        if kind == "pair":
            lines.append(
                "verify public client/server tags, client digest and compatibility; client non-prerelease + Latest after both guarded Deploys"
            )
        lines += ["just release pin; wait for green workspace pin-PR CI; merge pin PR"]
        for number, line in enumerate(lines, 1):
            say(f"  {number}. {line}")
        say(
            "dry run: prepare pushed nothing, so merge/tag/watch/pin steps above are not executed"
        )
        return True
    if kind == "pair":
        client = ship_tag_side(
            args, "client", tags["client"], timeout_s, interval_s, publish_only=True
        )
        candidate_sha = cmd_candidate(
            argparse.Namespace(
                tag=tags["client"],
                branch=pr_branch("server", tags["server"]),
                dry_run=dry,
            )
        )
        ship_tag_side(
            args,
            "server",
            tags["server"],
            timeout_s,
            interval_s,
            publish_only=True,
            expected_head=candidate_sha,
        )
        check_client_release(tags["client"], True)
        dispatch_deploy(
            "server",
            tags["server"],
            timeout_s,
            interval_s,
            client_tag=tags["client"],
            dry=dry,
        )
        dispatch_deploy("client", tags["client"], timeout_s, interval_s, dry=dry)
        verify_pair(tags, client, timeout_s, interval_s)
    elif len(tags) == 2:
        # Independent: server first; hold the client tag until the server
        # deploy job runs, so old clients stay locked out for less time.
        server_branch, client_branch = (
            pr_branch("server", tags["server"]),
            pr_branch("client", tags["client"]),
        )
        number, head = wait_pr_green(
            REPO["server"], server_branch, timeout_s, interval_s
        )
        server_merged = merge_pr(
            REPO["server"],
            number,
            dry,
            head,
        )
        server_sha = push_tag("server", tags["server"], dry, server_merged)
        number, head = wait_pr_green(
            REPO["client"], client_branch, timeout_s, interval_s
        )
        client_merged = merge_pr(
            REPO["client"],
            number,
            dry,
            head,
        )
        server_run = wait_release_run(
            "server", server_sha, timeout_s, interval_s, tags["server"]
        )
        wait_deploy_started("server", server_run["databaseId"], timeout_s, interval_s)
        client_sha = push_tag("client", tags["client"], dry, client_merged)
        wait_run_success("server", server_run["databaseId"], timeout_s, interval_s)
        wait_run_success(
            "client",
            wait_release_run(
                "client", client_sha, timeout_s, interval_s, tags["client"]
            )["databaseId"],
            timeout_s,
            interval_s,
        )
    else:
        side = next(iter(tags))
        ship_tag_side(args, side, tags[side], timeout_s, interval_s)
    pin = cmd_pin(
        argparse.Namespace(
            client=tags.get("client"),
            server=tags.get("server"),
            base="origin/main",
            dry_run=dry,
        )
    )
    if pin is True:
        say("nothing to pin: the workspace already pins what production runs")
        return True
    number, head = wait_pr_green(WORKSPACE_REPO, pin, timeout_s, interval_s)
    merge_pr(WORKSPACE_REPO, number, dry, head)
    say(f"\nshipped {kind}: {', '.join(f'{s} {t}' for s, t in tags.items())}")
    return True


def parser():
    p = argparse.ArgumentParser(
        prog="just release", description=__doc__.split("\n\n")[0]
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "status", help="pins, unreleased commits, contract vs production, tag order"
    ).set_defaults(run=cmd_status)
    s = sub.add_parser(
        "preflight", help="checks before tagging; non-zero exit on failure"
    )
    s.add_argument("side", choices=SIDES)
    s.add_argument("tag")
    s.add_argument(
        "--ref",
        default="origin/main",
        help="what the tag will point to (default origin/main)",
    )
    s.set_defaults(run=cmd_preflight)
    s = sub.add_parser(
        "prepare",
        help="bump pins and version, preflight, push branches, open release PRs",
    )
    s.add_argument(
        "--mod", required=True, metavar="vX.Y.Z", help="blockfield-mod release"
    )
    s.add_argument("--decor", metavar="vX.Y.Z", help="blockfield-decor release")
    s.add_argument("--bump", choices=("patch", "minor"), default="patch")
    s.add_argument("--only", choices=SIDES)
    s.set_defaults(run=cmd_prepare)
    s = sub.add_parser(
        "candidate",
        help="pin a published client release as the server's client_candidate",
    )
    s.add_argument("tag", metavar="vX.Y.Z")
    s.add_argument("--branch", required=True, help="the server release branch")
    s.set_defaults(run=cmd_candidate)
    s = sub.add_parser(
        "pin",
        help="pin released client/server (and their mods) in the workspace and open the PR",
    )
    s.add_argument("--client", metavar="vX.Y.Z")
    s.add_argument("--server", metavar="vX.Y.Z")
    s.add_argument("--base", default="origin/main")
    s.set_defaults(run=cmd_pin)
    s = sub.add_parser(
        "ship",
        help="end to end after owner confirmation: prepare, merge, tag, watch, pin",
    )
    s.add_argument(
        "--mod", required=True, metavar="vX.Y.Z", help="blockfield-mod release"
    )
    s.add_argument("--decor", metavar="vX.Y.Z", help="blockfield-decor release")
    s.add_argument("--bump", choices=("patch", "minor"), default="patch")
    s.add_argument("--only", choices=SIDES)
    s.add_argument(
        "--confirmed-by-owner",
        action="store_true",
        help="the owner confirmed this release explicitly in the conversation (tags start prod deploys)",
    )
    s.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="seconds per wait: mod publish, PR checks, runs, deploy gate (default 1800)",
    )
    s.add_argument(
        "--interval",
        type=int,
        default=5,
        help="seconds between status polls (default 5)",
    )
    s.set_defaults(run=cmd_ship)
    for name in ("prepare", "ship"):
        sub.choices[name].add_argument(
            "--server-merge",
            metavar="BRANCH",
            help="merge this origin server branch into current main in the release PR before preflight (includes all its changes)",
        )
    for name in ("prepare", "candidate", "pin", "ship"):
        sub.choices[name].add_argument(
            "--dry-run", action="store_true", help="print push/PR commands, run none"
        )
    return p


def main():
    python = canonical_python()
    if Path(sys.executable).resolve() != python.resolve():
        raise SystemExit(
            subprocess.run(
                [str(python), "-I", "-X", "utf8", __file__, *sys.argv[1:]],
                env=child_env(),
            ).returncode
        )
    args = parser().parse_args()
    try:
        ok = args.run(args)
    except (RuntimeError, ValueError, OSError) as error:
        raise SystemExit(f"error: {error}") from None
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
