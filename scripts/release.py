"""Release tooling: status, preflight, prepare, candidate, pin, ship.

Never creates or pushes tags and never starts Deploy, except through
`release ship` and `release rollback-client`: tags and Deploy start
production deploys and stay with the owner, so both refuse to run without
--confirmed-by-owner. Other mutating
commands go through mutate() and honour --dry-run; ship's own merges and tags
go through ship_mutate(), which has the same dry-run behaviour.

Game and Workshop deploy independently: a server release never stages, moves,
waits for or consults the Workshop, and ship never dispatches move-approved-tag.
A Workshop release is a separate explicit dispatch, one line, e.g.:
  gh api --method POST repos/Blockfield/blockfield-server/actions/workflows/move-approved-tag.yml/dispatches \
    -H 'X-GitHub-Api-Version: 2022-11-28' -f ref=main -F return_run_details=true \
    -f 'inputs[digest]=sha256:...'
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
SERVER_PIN = "pins/server.sha"
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
# A server release never stages, waits for or consults the Workshop: Game and
# Workshop deploy independently (the Workshop deploys via an explicit
# move-approved-tag dispatch). No workshop-first annotation exists anymore.
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
        if sides == ["server"]:
            # Server-only hotfix: tag, then the automatic tag-run Game deploy.
            # The Workshop is never staged or moved: it deploys separately via
            # an explicit move-approved-tag dispatch. No ("deploy", "server")
            # step: the dry-run text would otherwise instruct a manual
            # deploy.yml dispatch that real ship never performs.
            order = [("tag", "server")]
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
    # A shallow submodule clone answers merge-base backwards (next behind
    # main looks unrelated), so deepen it before is_ancestor / merge-tree.
    if git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
        git(repo, "fetch", "--unshallow", "-q", "origin", "main")
    else:
        git(repo, "fetch", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin", "next", check=False)


def has_next(repo):
    """True when the repo carries unreleased work on origin/next."""
    return bool(git(repo, "ls-remote", "--heads", "origin", "next").strip())


def integration_ref(repo):
    """Ref carrying unreleased work: origin/next when the repo has it, else origin/main."""
    return "origin/next" if has_next(repo) else "origin/main"


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
                # Contract-only preflight without Gradle (owner rule: no local
                # Gradle): the Game image jar is built from mod-server in CI
                # (:server:remapJar, wired in with --server-jar there).
                "--no-server-jar",
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
            # Same contract: server deploys and rolls back alone, no client_tag.
            res.append(("WARN", f"{gate}: same contract, deploys without client_tag"))
    return res


def show(res):
    for level, text in res:
        say(f"  [{level:<4}] {text}")
    return not any(level == "FAIL" for level, _ in res)


# RC record written by `just scenario rc --record <path>` (or by hand in the
# same shape while the battery has no --record writer; PR #168 is not merged).
RC_RECORD_FORMAT = "blockfield-rc-record/1"
# SKIP is allowed only for these checks (they need a second client) and only
# on server-only releases; every other SKIP fails the gate like a FAIL.
SECOND_CLIENT_CHECKS = ("long-range kill", "tdm death/respawn at BASE")


def load_rc_record(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise RuntimeError(f"RC record {path} is unreadable: {error}") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"RC record {path} is not a JSON object")
    return data


def check_rc_record(tags, record):
    """Validate an RC record against exactly the release builds. Returns [(level, text)]."""
    releases = record.get("releases") if isinstance(record, dict) else None
    if releases != tags:
        return [("FAIL", f"RC record covers {releases!r}, release needs {tags!r}")]
    checks = record.get("checks")
    if not isinstance(checks, list) or not checks:
        return [("FAIL", "RC record has no checks")]
    res, server_only = [], set(tags) == {"server"}
    for entry in checks:
        name = entry.get("name", "?") if isinstance(entry, dict) else "?"
        result = entry.get("result") if isinstance(entry, dict) else None
        result = str(result or "?").upper()
        detail = (entry.get("evidence") or "") if isinstance(entry, dict) else ""
        if result == "PASS":
            res.append(("ok", f"RC {name}: PASS"))
        elif (
            result == "SKIP"
            and server_only
            and str(name).startswith(SECOND_CLIENT_CHECKS)
        ):
            res.append(("ok", f"RC {name}: SKIP without a second client"))
        else:
            res.append(("FAIL", f"RC {name}: {result} {detail}"[:160]))
    return res


def require_rc_gate(tags, rc_record=None, gate_waiver=None):
    """G0: refuse to tag without a passing RC record; a waiver names the skipped gate."""
    if gate_waiver:
        say(f"  [WARN] release gate WAIVED: {gate_waiver}")
        return True
    if not rc_record:
        raise SystemExit(
            "refusing to tag: no RC record (--rc-record <path>, from `just scenario rc`); "
            'or pass --gate-waiver "<reason>" to waive visibly'
        )
    try:
        record = load_rc_record(rc_record)
    except RuntimeError as error:
        raise SystemExit(f"refusing to tag: {error}") from error
    res = check_rc_record(tags, record)
    if not show(res):
        raise SystemExit(
            f"refusing to tag: RC record {rc_record} does not pass the gate"
        )
    say(
        "  RC record passes for "
        + ", ".join(f"{side} {tag}" for side, tag in tags.items())
    )
    return True


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
    base = integration_ref(repo)
    tags = remote_tags(repo)
    last = newest(tags)
    version = next_version(last, show_pack(repo, base), args.bump)
    tag, branch = f"v{version}", pr_branch(side, f"v{version}")
    waiver = getattr(args, "gate_waiver", None)
    body = f"Release-gate waiver: {waiver}\n" if waiver else ""
    if base == "origin/next":
        # The release PR merges next into main: it must stay a merge commit
        # (never squash), and next is fast-forwarded to main right after.
        body += (
            "Merge with a merge commit (never squash); after the merge run: "
            f"git -C {side} fetch -q origin && "
            "git -C {side} push origin origin/main:refs/heads/next "
            "(fast-forward next to main)\n"
        )
    if git(repo, "ls-remote", "--heads", "origin", branch):
        raise SystemExit(
            f"{branch} already exists on origin; delete or reuse it manually"
        )
    say(f"== {side} {tag}")
    with worktree(repo, base, branch, side) as tree:
        files, parts = [], []
        if side == "server" and args.server_merge:
            for name in (piece.strip() for piece in args.server_merge.split(",")):
                if not name:
                    continue
                git(tree, "fetch", "-q", "origin", f"refs/heads/{name}")
                sha = git(tree, "rev-parse", "FETCH_HEAD")
                say(f"  merge server {name} ({sha}) into release branch")
                git(tree, "merge", "--no-ff", "--no-edit", sha)
                parts.append(f"merge {name} {sha[:7]}")
        if side == "client":
            files.append(pin_jar(tree, "mod", args.mod))
        else:
            run("sh", "scripts/bump-mod.sh", args.mod, cwd=tree)
        parts.append(f"mod {args.mod}")
        if args.decor:
            files.append(pin_jar(tree, "decor", args.decor))
            parts.append(f"decor {args.decor}")
        if not git(tree, "diff", base, "--name-only"):
            # Fork-pin-only path: mod/decor pins match the integration base, but the
            # tree may still carry unreleased content vs the newest tag
            # (client main, or the merged server branch). Release then.
            if not last:
                say("  pins unchanged: nothing to release on this side")
                return None
            # The tag object may be unknown locally (ls-remote only lists
            # it; a burnt tag can point at an unmerged release branch).
            git(tree, "fetch", "-q", "origin", "tag", last)
            if not git(tree, "log", "--no-merges", "--format=%h", f"{last}..HEAD"):
                say("  pins unchanged: nothing to release on this side")
                return None
            parts.append(f"fork pins since {last}")
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
            body,
        )
    return tag, meta


def tag_lines(order, tags, branch):
    lines = []
    for action, side in order:
        tag = tags.get(side)
        if action == "tag":
            lines.append(
                f"release gate: RC record for {side} {tag} passes "
                "(or a named --gate-waiver); release.push_tag refuses without one"
            )
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
            elif side == "server":
                # Server-only: the tag push run deploys Game on its own; real
                # ship performs no manual deploy.yml dispatch for this step.
                lines.append(
                    "watch the tag push run: its automatic Deploy ships "
                    f"{tags['server']} (no manual dispatch)"
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
                shlex.join(deploy_command(side, tags[side]))
                + "   # wait for this new dispatch run to succeed"
            )
    for action, side in order:
        if action == "tag":
            lines.append(
                f"after the {side} release merge: merge with a merge commit (never squash), then "
                f"git -C {side} fetch -q origin && git -C {side} push origin origin/main:refs/heads/next"
                "   # fast-forward next to main; hotfixes go via next too"
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
        expected = getattr(args, "expected_tree", None)
        if expected and git(tree, "rev-parse", "HEAD^{tree}") != expected:
            raise SystemExit(
                "candidate refused: server tree changed since resume guard"
            )
        set_candidate(tree, args.tag)
        tag = "v" + pack_version((tree / "pack.toml").read_text())
        res, _ = preflight("server", tag, tree)
        if not show(res):
            raise SystemExit("preflight failed: nothing was pushed")
        if expected:
            changed = git(tree, "diff", "--name-only", "HEAD").splitlines()
            before = json.loads(git(tree, "show", "HEAD:client-compatibility.json"))
            after = json.loads((tree / "client-compatibility.json").read_text())
            before.pop("client_candidate", None)
            after.pop("client_candidate", None)
            if changed != ["client-compatibility.json"] or before != after:
                raise SystemExit(
                    "candidate refused: changes extend beyond client_candidate"
                )
            say(git(tree, "diff", "--stat", "HEAD"))
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
    say("\nnext resync (a stale next makes the next prepare collide):")
    for side in SIDES:
        ff_next_to_main(side, False, report_only=True)
    changed, compat, broken = {}, {}, False
    mods = {}
    for side in SIDES:
        repo = repo_dir(side)
        fetch_main(repo)
        base = integration_ref(repo)
        tags, last = remote_tags(repo), published(side)
        log = (
            git(
                repo,
                "log",
                "--no-merges",
                "--format=%h %s",
                f"{tags[last]}..{base}",
            ).splitlines()
            if last in tags
            else []
        )
        changed[side] = bool(log)
        mods[side] = pins(repo, base)
        say(
            f"\n{side}: last published {last}, pack.toml on {base.removeprefix('origin/')} {show_pack(repo, base)}, {len(log)} unreleased commit(s)"
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
            base = integration_ref(repo)
            with worktree(repo, base, side=side) as tree:
                say(
                    f"\nbuilding {side} {base.removeprefix('origin/')} to read its contract ..."
                )
                try:
                    tag = "v" + show_pack(repo, base)
                    meta = build(side, tree, tag)
                    compat[side] = meta["compatibility"]
                    broken |= not show(contract_checks(side, meta, prod))
                except RuntimeError as error:
                    broken = True
                    say(
                        f"{side} {base.removeprefix('origin/')} does not build/pass its checks:\n{error}"
                    )
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
        if out.startswith("160000"):
            return out.split()[2]
        if path == "server":
            # The public workspace pins the private server by file, not gitlink.
            shown = git(ROOT, "show", f"{base}:{SERVER_PIN}", check=False)
            return (shown.returncode == 0 and shown.stdout.strip()) or None
        return None

    def advance(path, tag, label):
        repo = ROOT / path
        if not (repo / ".git").exists():
            if path not in skipped:
                say(f"skip {path}: submodule is not checked out")
            skipped.add(path)
            return
        current = changes.get(path, (pinned(path),))[0]
        if current is None:
            if path not in skipped:
                say(f"skip {path}: the workspace does not pin it")
            skipped.add(path)
            return
        sha = tag_commit(repo, tag)
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
            if path == "server" and not git(tree, "ls-tree", base, path):
                (tree / SERVER_PIN).write_text(sha + "\n", encoding="utf-8")
                git(tree, "add", SERVER_PIN)
                continue
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


def _check_success(check):
    """A completed SUCCESS CheckRun or a SUCCESS StatusContext; anything else
    (including SKIPPED) is not green."""
    if check.get("__typename") == "StatusContext":
        return check.get("state") == "SUCCESS"
    return check.get("status") == "COMPLETED" and check.get("conclusion") == "SUCCESS"


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
        or c.get("state") in ("FAILURE", "ERROR")
        for c in rollup
    ):
        return "red"
    for name in required_jobs:
        jobs = [c for c in rollup if (c.get("name") or c.get("context")) == name]
        if not jobs:
            return "pending"
        # A skip is never green and never vetoes: the job is satisfied by any
        # completed SUCCESS on this head, so a later skipped run (a
        # non-approving review after the job-level `if:` fix) can neither pass
        # nor mask an earlier green one.
        if any(_check_success(c) for c in jobs):
            continue
        return "pending"
    if all(
        c.get("state") == "SUCCESS"
        if c.get("__typename") == "StatusContext"
        else c.get("status") == "COMPLETED"
        and c.get("conclusion") in ("SUCCESS", "SKIPPED", "NEUTRAL")
        for c in rollup
    ) and any(
        # A commit status (e.g. bot-review) never proves CI ran: green needs a
        # successful CI check run on the head.
        c.get("__typename") != "StatusContext"
        and c.get("status") == "COMPLETED"
        and c.get("conclusion") == "SUCCESS"
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


def push_tag(
    side,
    tag,
    dry,
    expected_sha=None,
    rc_record=None,
    gate_waiver=None,
    release_tags=None,
):
    """Tag the merged origin/main and push it. Returns the tagged commit sha.

    release_tags: every side of this release; a pair RC record must match it, not one side.
    """
    if dry:
        say(
            f"+ git -C {side} fetch origin main && git -C {side} tag -a {tag} "
            f"-m {tag} origin/main && git -C {side} push origin {tag}"
        )
        return "(dry run)"
    require_rc_gate(release_tags or {side: tag}, rc_record, gate_waiver)
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


def wait_run_success(side, run_id, timeout_s, interval_s, repo=None):
    repo = repo or REPO[side]

    def ready():
        found = json.loads(
            gh(
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
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
    CACHE.mkdir(parents=True, exist_ok=True)
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


def verify_client_stage(tag, run_id):
    """Accept only a completed manual staging run, with promotion positively skipped."""
    data = json.loads(gh("api", f"repos/{REPO['client']}/actions/runs/{run_id}"))
    if (
        data.get("event") != "workflow_dispatch"
        or data.get("path") != ".github/workflows/deploy.yml"
        or data.get("head_branch") != "main"
        or data.get("status") != "completed"
        or data.get("conclusion") != "success"
    ):
        raise RuntimeError(
            "client stage evidence is not a successful main Deploy client dispatch"
        )
    jobs = json.loads(
        gh("run", "view", str(run_id), "--repo", REPO["client"], "--json", "jobs")
    )["jobs"]
    if not jobs or any(job.get("conclusion") != "success" for job in jobs):
        raise RuntimeError("client stage jobs did not succeed")
    promotion = [
        step
        for job in jobs
        for step in job.get("steps", [])
        if step.get("name") == "Mark the deployed client as latest"
    ]
    if len(promotion) != 1 or promotion[0].get("conclusion") != "skipped":
        raise RuntimeError("client stage evidence must skip Latest promotion")
    log = gh("run", "view", str(run_id), "--repo", REPO["client"], "--log")
    if f"Client {tag} staged invisibly;" not in log:
        raise RuntimeError("client stage evidence does not stage the requested tag")
    return True


def wait_publication(side, tag, run_id, timeout_s, interval_s, client_stage_run=None):
    """Pair tags publish first; their automatic Deploy cannot switch the old pair."""
    staged = (
        side == "client"
        and client_stage_run is not None
        and verify_client_stage(tag, client_stage_run)
    )
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
            allowed = (
                {"success"}
                if job["name"] in required
                else {"success", "skipped", "neutral"}
            )
            if job.get("conclusion") and job["conclusion"].lower() not in allowed:
                raise RuntimeError(
                    f"{side} publication job {job['name']} {job['conclusion']}: {data['url']}"
                )
        for job in deploy:
            conclusion = job.get("conclusion")
            if conclusion in (None, "", "success") or (
                staged and conclusion == "skipped"
            ):
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
            j.get("conclusion")
            not in (
                {"success"}
                if j["name"] in required
                else {"success", "skipped", "neutral"}
            )
            for j in jobs.values()
        ):
            if data["status"] == "completed":
                raise RuntimeError(
                    f"{side} publication jobs did not succeed: {data['url']}"
                )
            return False
        if side == "client" and (
            not deploy
            or any(
                j.get("conclusion")
                not in ({"success", "skipped"} if staged else {"success"})
                for j in deploy
            )
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


def deploy_repo(side):
    """Repo hosting side's Deploy workflow (client deploys from the server repo)."""
    return REPO["server"] if side == "client" else REPO[side]


def deploy_command(side, tag, client_tag=None):
    if side == "client":
        # The public client repo only publishes the release: its deploy.yml
        # is a stub with no blockfield-deploy runner. Staging, serving and
        # Latest run from the server repo's deploy-client.yml instead.
        return (
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{REPO['server']}/actions/workflows/deploy-client.yml/dispatches",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "-f",
            "ref=main",
            "-F",
            "return_run_details=true",
            "-f",
            f"inputs[tag]={tag}",
            "-f",
            "inputs[dry_run]=false",
        )
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
    wait_run_success(side, run_id, timeout_s, interval_s, deploy_repo(side))


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


def entry_probe(tag):
    """Synthetic entry check via the server checkout (no credentials): the
    proxy's composed gate (file + CP lease) must leave Game entry open, the
    RakNet listener must answer, and /api/rooms must serve the tag. The
    Workshop legs are skipped (Game-only gate): a down, asleep or
    mid-transition Workshop never fails a Game release. /api/rooms build is
    the server build, so a client leg passes no tag and checks the entry
    shape only (tag=None omits --tag, which the probe documents for exactly
    this: entry-shape checks when the Game build is unchanged)."""
    script = ROOT / "server" / "scripts" / "entry_liveness.py"
    if not script.is_file():
        return (
            "REQUIRED",
            "manual: proxy gate Game backend open — join via the Game entry "
            "(server checkout missing, probe skipped)",
        )
    probe = [
        sys.executable,
        "-I",
        str(script),
    ]
    if tag is not None:
        probe += ["--tag", tag]
    probe += [
        "--proxy-host",
        "play.blockfield.pro",
        "--workshop-host",
        "workshop.blockfield.pro",
    ]
    try:
        helps = subprocess.run(
            [sys.executable, "-I", str(script), "--help"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        helps = None
    if helps is not None and helps.returncode == 0 and "--game-only" in helps.stdout:
        # The pinned server knows the Game-only gate: skip the Workshop legs.
        # Older pins predate the flag and keep the full gate until the pin bump.
        probe.append("--game-only")
    try:
        result = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return (
            "REQUIRED",
            f"manual: entry probe could not run ({error}); join via the Game entry",
        )
    if result.returncode == 0:
        return (
            "ok",
            f"synthetic entry alive after {tag} (proxy gate + RakNet + rooms)"
            if tag is not None
            else "synthetic entry alive (proxy gate + RakNet + rooms shape, no build check)",
        )
    tail = (result.stdout + result.stderr).strip().splitlines()
    return (
        "FAIL",
        f"synthetic entry dead after {tag}: {'; '.join(tail[-4:])}"[:300]
        if tag is not None
        else f"synthetic entry shape dead: {'; '.join(tail[-4:])}"[:300],
    )


def post_deploy_probes(tags, client=None, timeout_s=1800, interval_s=5):
    """G5 probes after a deploy: automated where the tool has a read path,
    REQUIRED manual checks where it has none. Raises on automated failure."""
    want = dict(tags)

    def ready():
        try:
            prod = prod_state()
        except (OSError, ValueError):
            return None  # transient fetch failure: keep polling within the bound
        for side, tag in want.items():
            if prod.get("build" if side == "client" else "server_build") != tag:
                return None
        return prod

    prod = wait_until(
        "production running "
        + ", ".join(f"{side} {tag}" for side, tag in want.items()),
        timeout_s,
        interval_s,
        ready,
    )
    res = [
        (
            "ok",
            "/api/rooms serves "
            + ", ".join(f"{side} {tag}" for side, tag in want.items()),
        )
    ]
    if client is not None:
        res.append(
            (
                "ok"
                if prod.get("compatibility") == client.get("compatibility")
                else "FAIL",
                f"build.json contract {str(prod.get('compatibility'))[:12]} "
                f"{'==' if prod.get('compatibility') == client.get('compatibility') else '!='} "
                f"released {str(client.get('compatibility'))[:12]}",
            )
        )
    else:
        res.append(
            ("ok", f"build.json contract {str(prod.get('compatibility'))[:12]} live")
        )
    # No read path from this tool: print the exact check, mark it REQUIRED.
    if "server" in want:
        res.append(
            (
                "REQUIRED",
                "manual: prod server.properties view-distance/simulation-distance/"
                f"broadcast-* equal the intended values for {want['server']} "
                "(prod drifted silently in 2.79.0; compare the Dokploy Game "
                "service config against the tagged release)",
            )
        )
    res.append(
        # /api/rooms build is the server build: the client leg has no server
        # tag in want, so it checks the entry shape only (tag=None).
        entry_probe(tags.get("server")),
    )
    if not show(res):
        raise RuntimeError("post-deploy contract probe failed")
    return True


def ff_next_to_main(side, dry, report_only=False):
    """Fast-forward origin/next to origin/main after the release merge. Never forces.

    The release PR is cut from origin/next and merged into main with a merge
    commit; without this step the next prepare would cut from a stale base and
    collide on the bumped version line. Raises (moving nothing) when next
    holds commits main does not contain: fast-forward it by hand instead.
    With report_only (used by `release status`) nothing is pushed or raised:
    a stale next is reported with the exact command that moves it.
    """
    repo = repo_dir(side)
    if not has_next(repo):
        return False
    fetch_main(repo)
    main = git(repo, "rev-parse", "origin/main")
    nxt = git(repo, "rev-parse", "origin/next")
    if main == nxt:
        say(f"  {side}: origin/next already at {main[:12]}")
        return True
    if is_ancestor(repo, main, nxt):
        # Unreleased work on next ahead of main is the normal pre-release state.
        say(f"  {side}: origin/next ({nxt[:7]}) is ahead of origin/main ({main[:7]})")
        return True
    if not is_ancestor(repo, nxt, main):
        # PRs merged into next during a release window: sync main into next with
        # a merge commit (still a fast-forward of next). Never raise here, this
        # runs between the release merge and the tag.
        if report_only:
            say(
                f"  {side}: origin/next ({nxt[:7]}) and origin/main ({main[:7]}) diverged; ship merges main into next"
            )
            return False
        done = git(repo, "merge-tree", "--write-tree", nxt, main, check=False)
        if done.returncode:
            say(
                f"  WARNING {side}: merging origin/main into origin/next conflicts;"
                " merge it by hand in a PR into next (never force)"
            )
            return False
        tree = done.stdout.split()[0]
        if dry:
            say(
                f"  {side}: would merge origin/main into origin/next (tree {tree[:12]})"
            )
            return True
        commit = git(
            repo,
            "commit-tree",
            tree,
            "-p",
            nxt,
            "-p",
            main,
            "-m",
            "Merge main into next after release",
        )
        if not push_next(side, repo, dry, commit):
            return False
        say(f"  {side}: merged origin/main into origin/next at {commit[:12]}")
        return True
    if report_only:
        say(
            f"  {side}: origin/next ({nxt[:7]}) is behind origin/main"
            f" ({main[:7]}); run: git -C {repo} push origin"
            f" {main}:refs/heads/next"
        )
        return False
    if not push_next(side, repo, dry, main):
        return False
    say(f"  {side}: fast-forwarded origin/next to {main[:12]}")
    return True


def push_next(side, repo, dry, sha):
    """Move next without force. A rejected push (next moved meanwhile) only
    warns: this runs between the release merge and the tag."""
    try:
        ship_mutate(dry, "git", "push", "origin", f"{sha}:refs/heads/next", cwd=repo)
    except RuntimeError as error:
        say(
            f"  WARNING {side}: could not move origin/next ({error}); move next to main by hand (no force) before the next release"
        )
        return False
    return True


def ship_tag_side(
    args,
    side,
    tag,
    timeout_s,
    interval_s,
    publish_only=False,
    expected_head=None,
    release_tags=None,
):
    """Merge, preflight, tag and wait; pair publication returns the client manifest."""
    rc_record, gate_waiver = (
        getattr(args, "rc_record", None),
        getattr(args, "gate_waiver", None),
    )
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
    # A later gate may still fail: move next now, so a rerun never builds
    # on the stale base.
    ff_next_to_main(side, args.dry_run)
    sha = push_tag(
        side,
        tag,
        args.dry_run,
        merged,
        rc_record,
        gate_waiver,
        release_tags,
    )
    say(f"  {side} {tag} -> {sha[:7]}")
    found = wait_release_run(side, sha, timeout_s, interval_s, tag)
    if publish_only:
        return wait_publication(side, tag, found["databaseId"], timeout_s, interval_s)
    wait_run_success(side, found["databaseId"], timeout_s, interval_s)
    return sha


def resume_pair(args):
    """Resume only before candidate: immutable client tag and exact server RC tree."""
    client, server, client_tree, server_tree = args.resume_client_published
    for value in (client, server):
        if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", value):
            raise SystemExit("resume requires exact release tags")
    for value in (client_tree, server_tree):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise SystemExit("resume requires full expected tree SHAs")
    tags = {"client": client, "server": server}
    branch = pr_branch("server", server)
    for side, ref, expected in (
        ("client", f"refs/tags/{client}", client_tree),
        ("server", f"refs/heads/{branch}", server_tree),
    ):
        repo = repo_dir(side)
        git(repo, "fetch", "-q", "origin", ref)
        if git(repo, "rev-parse", "FETCH_HEAD^{tree}") != expected:
            raise SystemExit(
                f"resume refused: {side} tree differs from expected RC tree"
            )
    if git(repo_dir("server"), "ls-remote", "--tags", "origin", f"refs/tags/{server}"):
        raise SystemExit("resume refused: server tag already exists")
    return (
        tags,
        "pair",
        [
            ("candidate", "server"),
            ("tag", "server"),
            ("deploy-pair", "server"),
            ("deploy", "client"),
        ],
        branch,
    )


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
    resume = getattr(args, "resume_client_published", None)
    if getattr(args, "client_stage_run", None) and not resume:
        raise SystemExit("--client-stage-run requires --resume-client-published")
    if resume and (args.only or args.server_merge):
        raise SystemExit("resume cannot be combined with --only or --server-merge")
    # An earlier ship may have merged a release PR and died before moving next:
    # heal it before prepare cuts a release from a stale next. Nothing is
    # merged yet, so a next that cannot be synced stops the ship here.
    if not dry:
        for side in SIDES:
            if not ff_next_to_main(side, dry) and has_next(repo_dir(side)):
                raise SystemExit(
                    f"ship stopped: origin/next of {side} is not synced with origin/main; fix it first"
                )
    tags, kind, order, branch = resume_pair(args) if resume else prepare_all(args)
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
    require_rc_gate(
        tags, getattr(args, "rc_record", None), getattr(args, "gate_waiver", None)
    )
    if kind == "pair":
        if resume:
            sha = git(repo_dir("client"), "rev-parse", "FETCH_HEAD^{commit}")
            found = wait_release_run(
                "client", sha, timeout_s, interval_s, tags["client"]
            )
            client = wait_publication(
                "client",
                tags["client"],
                found["databaseId"],
                timeout_s,
                interval_s,
                client_stage_run=getattr(args, "client_stage_run", None),
            )
            check_client_release(tags["client"], True)
        else:
            client = ship_tag_side(
                args,
                "client",
                tags["client"],
                timeout_s,
                interval_s,
                publish_only=True,
                release_tags=tags,
            )
        candidate_sha = cmd_candidate(
            argparse.Namespace(
                tag=tags["client"],
                branch=pr_branch("server", tags["server"]),
                dry_run=dry,
                expected_tree=resume[3] if resume else None,
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
            release_tags=tags,
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
        post_deploy_probes({"server": tags["server"]}, None, timeout_s, interval_s)
        dispatch_deploy("client", tags["client"], timeout_s, interval_s, dry=dry)
        post_deploy_probes({"client": tags["client"]}, client, timeout_s, interval_s)
        verify_pair(tags, client, timeout_s, interval_s)
    elif kind == "server-only":
        # Server-only hotfix: a plain tag, then the automatic tag-run deploys
        # Game on its own. The Workshop is never staged, moved or consulted;
        # a Workshop release is a separate explicit move-approved-tag dispatch.
        ship_tag_side(
            args,
            "server",
            tags["server"],
            timeout_s,
            interval_s,
        )
        post_deploy_probes(tags, None, timeout_s, interval_s)
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
        # A later gate may still fail: move next now, so a rerun never
        # builds on the stale base.
        ff_next_to_main("server", dry)
        server_sha = push_tag(
            "server",
            tags["server"],
            dry,
            server_merged,
            getattr(args, "rc_record", None),
            getattr(args, "gate_waiver", None),
            tags,
        )
        number, head = wait_pr_green(
            REPO["client"], client_branch, timeout_s, interval_s
        )
        client_merged = merge_pr(
            REPO["client"],
            number,
            dry,
            head,
        )
        ff_next_to_main("client", dry)
        server_run = wait_release_run(
            "server", server_sha, timeout_s, interval_s, tags["server"]
        )
        wait_deploy_started("server", server_run["databaseId"], timeout_s, interval_s)
        client_sha = push_tag(
            "client",
            tags["client"],
            dry,
            client_merged,
            getattr(args, "rc_record", None),
            getattr(args, "gate_waiver", None),
            tags,
        )
        wait_run_success("server", server_run["databaseId"], timeout_s, interval_s)
        wait_run_success(
            "client",
            wait_release_run(
                "client", client_sha, timeout_s, interval_s, tags["client"]
            )["databaseId"],
            timeout_s,
            interval_s,
        )
        # The client tag run only publishes; serving runs from the server repo.
        dispatch_deploy("client", tags["client"], timeout_s, interval_s, dry=dry)
        post_deploy_probes(tags, None, timeout_s, interval_s)
    else:
        side = next(iter(tags))
        ship_tag_side(args, side, tags[side], timeout_s, interval_s)
        if side == "client":
            dispatch_deploy("client", tags["client"], timeout_s, interval_s, dry=dry)
        post_deploy_probes(tags, None, timeout_s, interval_s)
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
    # The release PRs were cut from origin/next and merged into main: move
    # next onto the release merge so the next prepare cuts from a fresh base.
    for side in tags:
        ff_next_to_main(side, dry)
    say(f"\nshipped {kind}: {', '.join(f'{s} {t}' for s, t in tags.items())}")
    return True


def cmd_rollback_client(args):
    """Roll the served client back to an older tag via the server workflow."""
    if not args.confirmed_by_owner and not args.dry_run:
        raise SystemExit(
            "refusing: `release rollback-client` switches production serving, "
            "and serving switches stay with the owner. "
            "Rerun with --confirmed-by-owner after the owner confirms this release explicitly in the conversation."
        )
    if not parse(args.tag):
        raise SystemExit(f"rollback-client requires a vX.Y.Z tag, got {args.tag!r}")
    dispatch_deploy("client", args.tag, args.timeout, args.interval, dry=args.dry_run)
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
    s.add_argument(
        "--rc-record",
        metavar="PATH",
        help="RC record JSON from `just scenario rc` covering exactly the release builds",
    )
    s.add_argument(
        "--gate-waiver",
        metavar='"reason"',
        help="waive the release gate visibly; recorded in the log and the PR body",
    )
    s.add_argument(
        "--resume-client-published",
        nargs=4,
        metavar=("CLIENT_TAG", "SERVER_TAG", "CLIENT_TREE", "SERVER_TREE"),
        help="resume a pair before candidate, guarded by the exact published client and server RC trees; never re-tag the client",
    )
    s.add_argument(
        "--client-stage-run",
        type=int,
        metavar="RUN_ID",
        help="successful main Deploy client staging run for resume when automatic tag Deploy was skipped",
    )
    s.set_defaults(run=cmd_ship)
    s = sub.add_parser(
        "rollback-client",
        help="roll the served client back to an older tag via the server deploy-client workflow",
    )
    s.add_argument("tag", metavar="vX.Y.Z")
    s.add_argument(
        "--confirmed-by-owner",
        action="store_true",
        help="the owner confirmed this release explicitly in the conversation (serving switches stay with the owner)",
    )
    s.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="seconds per wait: dispatch run and deploy gate (default 1800)",
    )
    s.add_argument(
        "--interval",
        type=int,
        default=5,
        help="seconds between status polls (default 5)",
    )
    s.set_defaults(run=cmd_rollback_client)
    for name in ("prepare", "ship"):
        sub.choices[name].add_argument(
            "--server-merge",
            metavar="BRANCH[,BRANCH]",
            help="merge origin server branch(es) into current main in the release PR "
            "before preflight, comma-separated in merge order (includes all their changes)",
        )
    for name in ("prepare", "candidate", "pin", "ship", "rollback-client"):
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
