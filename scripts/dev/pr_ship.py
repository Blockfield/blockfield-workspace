#!/usr/bin/env python3
"""Ship one branch once: merge base into published history, refuse conflicts, push once,
print the PR URL, head SHA and current check/review state, exit 0. Never waits.

Re-running on an already pushed, unchanged branch pushes nothing: it just
reports the current state again. History rule: rebase only before publish
(no remote branch and no PR); a published branch gets a merge of the base,
never a rewrite. Force-push is never used.

Usage: just pr-ship <repo> <branch>
<repo> is the owning repo checkout (workspace or a submodule path),
<branch> the task branch. The branch must be checked out work complete and
committed (uncommitted changes are refused: a push would silently skip them).
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_preflight  # noqa: E402  (worktree + staleness checks)


def run(repo, *args, check=True):
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


def gh(repo, *args):
    result = subprocess.run(
        ["gh", *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(repo),
    )
    return result


def default_branch(repo):
    tip = agent_preflight.default_tip(repo)
    if not tip:
        raise SystemExit(f"{repo}: cannot find origin's default branch; fetch first")
    head = agent_preflight.git(
        repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False
    ).stdout.strip()
    if head.startswith("refs/remotes/"):
        return head.split("refs/remotes/")[-1]
    for ref in ("origin/main", "origin/master"):
        out = run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if out.returncode == 0 and out.stdout.strip() == tip:
            return ref
    return "origin/main"


def main(argv):
    if len(argv) != 2:
        raise SystemExit("usage: pr_ship.py <repo> <branch>")
    repo_arg = argv[0]
    repo = Path(repo_arg).resolve()
    branch = argv[1]
    if not (repo / ".git").exists():
        raise SystemExit(f"{repo} is not a repo checkout")
    agent_preflight.assert_worktree(repo)
    run(repo, "fetch", "origin")
    if agent_preflight.mid_flight(repo, ""):
        raise SystemExit(f"{repo}: finish the in-progress merge/rebase first")
    if run(repo, "status", "--porcelain").stdout.strip():
        raise SystemExit(
            f"{repo}: uncommitted changes — commit or stash first "
            "(a push would silently skip them)"
        )
    if run(repo, "rev-parse", "--verify", branch, check=False).returncode != 0:
        raise SystemExit(f"{repo}: no local branch {branch}")
    current = run(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != branch:
        run(repo, "checkout", branch)
    base = default_branch(repo)
    # Publication must be checked before any operation that rewrites history.
    remote_ref = f"refs/remotes/origin/{branch}"
    remote_out = run(
        repo, "rev-parse", "--verify", f"{remote_ref}^{{commit}}", check=False
    )
    remote = remote_out.stdout.strip() if remote_out.returncode == 0 else None
    view = gh(
        repo, "pr", "view", branch, "--json", "number,mergeable,mergeStateStatus,url"
    )
    number = url = None
    if view.returncode == 0:
        data = json.loads(view.stdout or "{}")
        number, url = data.get("number"), data.get("url", "")
    elif "no pull requests found" not in view.stderr.lower():
        raise SystemExit(
            f"cannot determine PR publication for {branch}: {view.stderr.strip()}"
        )
    if (
        remote
        and run(
            repo, "merge-base", "--is-ancestor", remote, "HEAD", check=False
        ).returncode
    ):
        raise SystemExit(
            f"DIVERGED: origin/{branch} is not an ancestor of HEAD; refusing to push"
        )
    behind = run(repo, "rev-list", "--count", f"HEAD..{base}").stdout.strip()
    if behind != "0":
        operation = "merge" if remote or number else "rebase"
        print(f"{branch} is {behind} behind {base}; {operation}", flush=True)
        options = ["--no-edit"] if operation == "merge" else []
        out = run(repo, operation, *options, base, check=False)
        if out.returncode != 0:
            files = run(
                repo, "diff", "--name-only", "--diff-filter=U"
            ).stdout.splitlines()
            run(repo, operation, "--abort", check=False)
            raise SystemExit(
                json.dumps(
                    {
                        "status": "CONFLICT",
                        "branch": branch,
                        "base": base,
                        "files": files,
                        "operation": operation,
                        "detail": out.stdout,
                    }
                )
            )
    elif number and data.get("mergeable") == "CONFLICTING":
        raise SystemExit(
            f"PR #{number} is CONFLICTING: merge {base} into {branch} before pushing"
        )
    head = run(repo, "rev-parse", "HEAD").stdout.strip()
    if remote != head:
        push = ["push", "--no-force", "-u", "origin", branch]
        print(f"pushing once: git {' '.join(push)}", flush=True)
        out = run(repo, *push, check=False)
        if out.returncode != 0:
            raise SystemExit(
                f"push failed (push only once, then report):\n{out.stdout}"
            )
        print(out.stdout.strip())
    else:
        print(f"{branch} already at {head[:12]} on origin; no push needed")
    if number is None:
        created = gh(repo, "pr", "create", "--fill")
        if created.returncode != 0:
            raise SystemExit(
                f"pushed {head[:12]} but no PR exists and creation failed:\n"
                f"{created.stderr.strip()}"
            )
        # gh pr create prints the new PR URL: keep it so a later lookup
        # failure cannot hide the PR identity.
        lines = created.stdout.strip().splitlines()
        if lines and lines[-1].strip():
            url = lines[-1].strip()
    # One non-blocking read of the currently known state; never wait on it.
    state = gh(
        repo,
        "pr",
        "view",
        branch,
        "--json",
        "number,url,headRefOid,mergeable,mergeStateStatus,reviewDecision,"
        "statusCheckRollup",
    )
    if state.returncode == 0:
        try:
            info = json.loads(state.stdout or "{}")
        except ValueError:
            info = {}
        number, url = info.get("number", number), info.get("url", url or "")
        rhead = info.get("headRefOid") or head
        checks = " ".join(
            f"{k}={info.get(k) or '-'}"
            for k in ("mergeable", "mergeStateStatus", "reviewDecision")
        )
        # per-check states: a failed and a pending bot-review share the
        # same aggregate merge state, so each check is listed individually.
        states = ",".join(
            f"{c.get('name') or c.get('context') or '?'}:"
            f"{c.get('conclusion') or c.get('state') or '?'}"
            for c in (info.get("statusCheckRollup") or [])
            if isinstance(c, dict)
        )
        checks += f" checks={states or '-'}"
    else:
        rhead, checks = head, "state unknown"
    if number is None:
        raise SystemExit(
            f"pushed {head[:12]} but the PR lookup failed "
            f"({state.stderr.strip() or 'no output'}); new PR URL from "
            f"creation: {url or 'unknown'} — rerun just pr-ship to report state"
        )
    print(f"PR #{number} {url or ''} head {rhead[:12]}".strip(), flush=True)
    print(f"checks and reviews: {checks}", flush=True)
    raise SystemExit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
