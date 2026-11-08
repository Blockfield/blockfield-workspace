#!/usr/bin/env python3
"""First command of every agent unit: refuse the known stale-base traps.

- accepts linked T3 worktrees and task clones (never the main checkout)
- refuses mid-merge / mid-rebase states (boat_build cannot transfer those)
- compares the workspace (and repos passed as --repo) against their origin
  tip, and every other submodule against the gitlink recorded in
  origin/main of the workspace (the workspace pins its submodules, so
  their origin tips are supposed to be ahead)
- refuses when a task repo sits behind origin (stale base: fetch+rebase first)

Usage: just agent-preflight [--repo PATH ...] [--no-fetch]
       [--source REPO=REF ...] [--mode upstream-main|pinned-candidate|task-branch]
Default repos: the workspace plus every initialized submodule.
Exit 0 when every repo is current (or only ahead with local commits, which
boat_build ships); exit 1 listing each behind/diverged repo; exit 2 when run
outside a linked worktree.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def linked_worktree(repo):
    """True when the checkout is a linked worktree (own git dir apart)."""
    own = git(repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    common = git(
        repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ).stdout.strip()
    return Path(own) != Path(common)


def superproject(repo):
    """Working tree of the enclosing superproject, or None."""
    out = git(
        repo, "rev-parse", "--show-superproject-working-tree", check=False
    ).stdout.strip()
    return Path(out).resolve() if out else None


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}:\n{result.stdout}")
    return result


def assert_worktree(root):
    """Refuse the coordinator's main checkout; accept task-owned checkouts.

    Linked worktrees pass directly. Submodules follow their superproject's
    ownership. Only a standalone workspace repo is the protected main
    checkout; standalone clones of other repos are task checkouts.
    """
    root = Path(root).resolve()
    if linked_worktree(root):
        return
    sup = superproject(root)
    if sup is not None and sup != root:
        try:
            assert_worktree(sup)
        except SystemExit:
            raise SystemExit(
                f"BLOCKED: {root} lives in the main checkout, not a linked T3 "
                "worktree. Work only in your own T3 worktree; never switch "
                "branches or commit in the coordinator's checkout."
            )
        return
    if origin_is_workspace(root):
        raise SystemExit(
            f"BLOCKED: {root} is the main checkout, not a linked T3 worktree. "
            "Work only in your own T3 worktree; never switch branches or "
            "commit in the coordinator's checkout."
        )


def origin_is_workspace(repo):
    """Identify workspace origins without applying local mirror rewrites."""
    out = git(repo, "config", "--get", "remote.origin.url", check=False)
    url = out.stdout.strip().lower()
    if "://" not in url and ":" in url:
        url = "ssh://" + url.replace(":", "/", 1)
    parsed = urlsplit(url)
    return parsed.hostname in ("github.com", "ssh.github.com") and parsed.path.rstrip(
        "/"
    ).removesuffix(".git") in (
        "/blockfield/blockfield-workspace",
        "/blockfield/blockfield-workspace",
    )


def default_tip(repo):
    """Local commit of origin's default branch, or None when unknown."""
    head = git(
        repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False
    ).stdout.strip()
    candidates = (
        [head.split("refs/remotes/")[-1]] if head.startswith("refs/remotes/") else []
    )
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        out = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def workspace_tip_ref(root):
    """origin's default-branch ref (origin/HEAD first), or None when unknown."""
    head = git(
        root, "symbolic-ref", "refs/remotes/origin/HEAD", check=False
    ).stdout.strip()
    candidates = (
        [head.split("refs/remotes/")[-1]] if head.startswith("refs/remotes/") else []
    )
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        out = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if out.returncode == 0 and out.stdout.strip():
            return ref
    return None


def integration_base(repo):
    """Ref carrying unreleased work: origin/next when the repo has it, else the origin default.

    One place for the next-branch policy: pr_ship, boat helpers and release
    tooling resolve their base through this instead of hard-coding origin/main.
    """
    probe = git(
        repo, "rev-parse", "--verify", "refs/remotes/origin/next^{commit}", check=False
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return "origin/next"
    return workspace_tip_ref(repo) or "origin/main"


def submodules(root):
    out = git(
        root,
        "config",
        "-f",
        ".gitmodules",
        "--get-regexp",
        r"^submodule\..*\.path$",
        check=False,
    ).stdout
    repos = []
    for line in out.splitlines():
        rel = line.split(" ", 1)[1]
        if (root / rel / ".git").exists():
            repos.append(rel)
    return repos


def check_repo(root, rel, fetch, strict_tip=False):
    """(line, failed): staleness of one repo.

    The workspace root and explicitly requested task repos are compared
    against their origin tip (a task repo must sit at its origin tip or on
    a branch based on it). Other submodules are compared against the
    gitlink recorded in origin/main of the workspace: the workspace pins
    its submodules, so their origin tips are supposed to be ahead.
    """
    repo = root / rel if rel else root
    name = rel or "workspace"
    local = git(repo, "rev-parse", "HEAD").stdout.strip()
    if rel and not strict_tip:
        if fetch:
            out = git(root, "fetch", "origin", check=False)
            if out.returncode != 0:
                return (
                    f"{name}: FETCH FAILED (cannot read workspace pins)",
                    True,
                )
        ref = workspace_tip_ref(root)
        if ref is None:
            # Fail closed: without the workspace tip ref an empty pin lookup
            # would pass every stale submodule silently.
            return (
                f"{name}: local={local[:12]} "
                "(cannot read workspace pin ref; fetch origin first?)",
                True,
            )
        pin = git(root, "rev-parse", f"{ref}:{rel}", check=False).stdout.strip()
        if not pin:
            return f"{name}: local={local[:12]} (no recorded pin)", False
        if local == pin:
            return f"{name}: local={local[:12]} pin={pin[:12]} ok", False
        return (
            f"{name}: local={local[:12]} pin={pin[:12]} STALE "
            f"(differs from the origin/main gitlink; `git -C {repo} checkout {pin}`?)",
            True,
        )
    if rel:
        recorded = git(root, "rev-parse", f":{rel}", check=False).stdout.strip()
        pin = f" pin={recorded[:12]}" if recorded else ""
        drift = " DRIFT" if recorded and recorded != local else ""
    else:
        pin, drift = "", ""
    if fetch:
        out = git(repo, "fetch", "origin", check=False)
        if out.returncode != 0:
            return f"{name}: local={local[:12]} origin=? FETCH FAILED{pin}{drift}", True
    tip = default_tip(repo)
    if not tip:
        return f"{name}: local={local[:12]} origin=? (no origin tip){pin}{drift}", False
    if local == tip:
        return f"{name}: local={local[:12]} origin={tip[:12]} ok{pin}{drift}", False
    behind = git(
        repo, "rev-list", "--count", f"{local}..{tip}", check=False
    ).stdout.strip()
    ahead = git(
        repo, "rev-list", "--count", f"{tip}..{local}", check=False
    ).stdout.strip()
    if behind != "0" and ahead == "0":
        return (
            f"{name}: local={local[:12]} origin={tip[:12]} BEHIND by {behind} "
            f"(stale base; `git -C {repo} pull --rebase` first){pin}{drift}",
            True,
        )
    if behind != "0":
        return (
            f"{name}: local={local[:12]} origin={tip[:12]} DIVERGED "
            f"(behind {behind}, ahead {ahead}; rebase first){pin}{drift}",
            True,
        )
    return (
        f"{name}: local={local[:12]} origin={tip[:12]} ahead by {ahead} "
        f"(unpushed; boat_build ships it){pin}{drift}",
        False,
    )


def mid_flight(root, rel):
    repo = root / rel if rel else root
    flags = []
    if git(repo, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        flags.append("merge")
    git_dir = Path(
        git(repo, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
    )
    for state in ("rebase-merge", "rebase-apply"):
        path = Path(git(repo, "rev-parse", "--git-path", state).stdout.strip())
        if not path.is_absolute():
            path = git_dir / path
        if (path / "head-name").is_file() and "rebase" not in flags:
            flags.append("rebase")
    return flags


def source_identity(root, rel, fetch, mode, source=None):
    """Report source refs without changing the candidate checkout."""
    repo = root / rel if rel else root
    name = rel or "workspace"
    if fetch and git(repo, "fetch", "origin", check=False).returncode:
        return f"{name}: source FETCH FAILED", True
    pin_out = (
        git(root, "rev-parse", "--verify", f"HEAD:{rel}", check=False) if rel else None
    )
    pin = pin_out.stdout.strip() if pin_out and pin_out.returncode == 0 else None
    upstream = integration_base(repo)
    tip_out = git(repo, "rev-parse", "--verify", f"{upstream}^{{commit}}", check=False)
    tip = tip_out.stdout.strip() if tip_out.returncode == 0 else None
    ref = (
        source
        or (
            {
                "pinned-candidate": pin or "HEAD",
                "upstream-main": upstream,
                "task-branch": "HEAD",
            }[mode]
        )
    )
    resolved = git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{ref}^{{commit}}",
        check=False,
    )
    if resolved.returncode:
        return f"{name}: source {ref!r} does not resolve", True
    behind = "?"
    if pin and tip:
        count = git(repo, "rev-list", "--count", f"{pin}..{tip}", check=False)
        if count.returncode == 0:
            behind = count.stdout.strip()
    line = (
        f"{name}: mode={mode} source={ref} sha={resolved.stdout.strip()} "
        f"pin={pin or '-'} {upstream}={tip or '?'} behind={behind}"
    )
    if mode == "upstream-main" and behind not in ("0", "?"):
        line += (
            f"\nWARN: {name} pin is behind {upstream}; read current code with "
            f"git -C '{repo}' show {upstream}:<path>"
        )
    return line, False


def main(argv):
    repos, fetch = [], True
    sources, mode = {}, "upstream-main"
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--no-fetch":
            fetch = False
        elif arg == "--mode" and args:
            mode = args.pop(0)
            if mode not in ("pinned-candidate", "upstream-main", "task-branch"):
                raise SystemExit(f"invalid mode: {mode}")
        elif arg == "--source" and args:
            value = args.pop(0)
            if "=" not in value:
                raise SystemExit("--source requires repo=ref")
            repo, ref = value.split("=", 1)
            if not repo or not ref:
                raise SystemExit("--source requires nonempty repo=ref")
            sources["" if repo in (".", "workspace") else repo] = ref
        elif arg == "--repo" and args:
            repos.append(args.pop(0))
        elif arg.startswith("--repo="):
            repos.append(arg.split("=", 1)[1])
        else:
            raise SystemExit("usage: agent_preflight.py [--repo PATH ...] [--no-fetch]")
    root = Path(os.getcwd()).resolve()
    assert_worktree(root)
    if repos:
        repos = ["" if r in ("", ".") else r for r in repos]
        explicit = set(repos)
    else:
        repos = ["", *submodules(root)]
        explicit = set()
    repos = list(dict.fromkeys([*repos, *sources]))
    known = {"", *submodules(root)}
    failed = False
    if fetch and any(r and r not in explicit for r in repos):
        out = git(root, "fetch", "origin", check=False)
        if out.returncode != 0:
            print("workspace: FETCH FAILED (cannot verify submodule pins)")
            failed = True
    for rel in repos:
        strict = rel in explicit or rel not in known
        for flag in mid_flight(root, rel):
            print(f"{rel or 'workspace'}: IN-PROGRESS {flag} — finish it first")
            failed = True
        line, bad = check_repo(
            root, rel, fetch if (not rel or strict) else False, strict
        )
        print(line)
        failed = failed or bad
        if rel or rel in sources:
            identity, bad = source_identity(root, rel, fetch, mode, sources.get(rel))
            print(identity)
            failed = failed or bad
    if failed:
        raise SystemExit(1)
    print("preflight ok: linked worktree, no stale repos")


if __name__ == "__main__":
    main(sys.argv[1:])
