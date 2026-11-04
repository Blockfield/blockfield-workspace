#!/usr/bin/env python3
"""Ship one branch once: merge base into published history, refuse conflicts, push once,
wait for CI, print the verdict. Enforces the one-push rule.

Re-running on an already pushed, unchanged branch pushes nothing: it just
resumes waiting, so a timed-out wait (exit 75) is resumed with the exact
command printed, never with a copy of this script. Every wait fits one
shell call (timeout capped at 540 s). The wait is gate-aware: first the
bot-review commit status on the head, then the gated CI run.

Usage: just pr-ship <repo> <branch> [--timeout SEC]
<repo> is the owning repo checkout (workspace or a submodule path),
<branch> the task branch. The branch must be checked out work complete and
committed (uncommitted changes are refused: a push would silently skip them).
Exit 75 means still pending: re-run the printed resume command.
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_preflight  # noqa: E402  (worktree + staleness checks)

MAX_TIMEOUT = 540.0  # keep in sync with await.py: one wait per shell call
STILL_PENDING = 75  # keep in sync with await.py: re-run the resume command


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
    if len(argv) < 2:
        raise SystemExit("usage: pr_ship.py <repo> <branch> [--timeout SEC]")
    repo_arg = argv[0]
    repo = Path(repo_arg).resolve()
    branch = argv[1]
    timeout = "540"
    rest = argv[2:]
    while rest:
        arg = rest.pop(0)
        if arg == "--timeout" and rest:
            timeout = rest.pop(0)
        else:
            raise SystemExit("usage: pr_ship.py <repo> <branch> [--timeout SEC]")
    try:
        if float(timeout) > MAX_TIMEOUT:
            print(
                f"--timeout {timeout} capped to {MAX_TIMEOUT:.0f}s: one wait "
                f"must fit a single shell call; re-run the resume command "
                f"on exit {STILL_PENDING}",
                flush=True,
            )
            timeout = f"{MAX_TIMEOUT:.0f}"
    except ValueError:
        raise SystemExit(f"--timeout must be seconds, got {timeout!r}")
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
            f"PR #{number} is CONFLICTING: merge {base} into {branch} before pushing or waiting"
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
        view = gh(repo, "pr", "view", branch, "--json", "number,url")
        data = json.loads(view.stdout or "{}")
        number, url = data.get("number"), data.get("url", "")
    print(f"waiting on PR #{number} {url or ''} head {head[:12]}", flush=True)
    awaited = subprocess.run(
        [
            sys.executable,
            "-I",
            str(HERE / "await.py"),
            "--pr",
            str(repo),
            str(number),
            "--timeout",
            timeout,
        ],
        check=False,
    )
    verdict = "GREEN" if awaited.returncode == 0 else "NOT GREEN"
    print(f"{verdict}: PR #{number} {url or ''} head {head[:12]}")
    if awaited.returncode == STILL_PENDING:  # unchanged branch: resume, no re-push
        print(
            f"still pending (exit {STILL_PENDING}): resume in the same turn: "
            f"just pr-ship {repo_arg} {branch} --timeout {timeout}",
            flush=True,
        )
    raise SystemExit(awaited.returncode)


if __name__ == "__main__":
    main(sys.argv[1:])
