#!/usr/bin/env python3
"""One blocking wait with a hard deadline: 'wait inside the turn' as one command.

Exactly one mode:
  --pid N                 local PID (kill -0 poll)
  --boat VM COMMAND_ID    Boat exec command: poll Boat history until finishedAt
  --pr DIR NUMBER         gated PR wait: bot-review status, then CI, on the head
  --run DIR RUN_ID        gh workflow run until completed
Common: --timeout SEC (default 540, capped at 540), --interval SEC (default 15).

Every wait fits one shell call: the timeout never exceeds 540 s, so the
~10 min tool limit cannot hit mid-wait. On timeout await prints the exact
resume command and exits 75 (still pending): re-run it in the same turn.
Never copy this script elsewhere or sleep-poll its output. Transient gh
failures (rate limits, 5xx) are retried inside the budget, never fatal.

--pr first waits for the bot-review commit status on the head (the CI gate
only starts CI after the review App approves, so "no CI run" is never
reported while bot-review is pending or missing). On bot-review
failure/error it prints the bot's open [blocker]/[major] findings
(path:line + first line) and exits non-zero. On bot-review success it
waits for the gated CI run on that head and prints the verdict.
Exit 0 on success, 1 on a failed verdict or a refused wait, 75 on timeout.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import boat_build  # noqa: E402  (history_entries, history_output)

import re  # noqa: E402

MAX_TIMEOUT = 540.0  # one shell call must never hit the ~10 min tool limit
STILL_PENDING = 75  # timeout exit: re-run the printed resume command
BOT_REVIEW_CONTEXT = "bot-review"
FINDING_RE = re.compile(r"\*\*\[(blocker|major)\]\*\*", re.IGNORECASE)
TRANSIENT_GH_RE = re.compile(
    r"rate limit|HTTP 403|HTTP 5\d\d|timed out|Could not resolve host|"
    r"Connection (reset|refused|aborted)|Bad Gateway|Service Unavailable|Gateway Time",
    re.IGNORECASE,
)


def parse_timeout(value):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"--timeout must be seconds, got {value!r}")
    if timeout > MAX_TIMEOUT:
        print(
            f"--timeout {value} capped to {MAX_TIMEOUT:.0f}s: one wait must "
            f"fit a single shell call; re-run the resume command on exit "
            f"{STILL_PENDING}",
            flush=True,
        )
        timeout = MAX_TIMEOUT
    return timeout


def run(args, cwd=None):
    return subprocess.run(
        args,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )


def wait_pid(pid, deadline, interval):
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True  # not ours to signal; treat as gone
        if time.time() >= deadline:
            return False
        time.sleep(min(interval, max(1.0, deadline - time.time())))


def boat_wait(vm_id, command_id, deadline, interval):
    """Poll Boat history for command_id until finishedAt; (finished, rc, out)."""
    while True:
        for entry in boat_build.history_entries(vm_id, limit=10):
            if entry.get("commandId") != command_id:
                continue
            if entry.get("finishedAt") and entry.get("exitCode") is not None:
                try:
                    code = int(entry.get("exitCode", 1))
                except (TypeError, ValueError):
                    code = 1
                out = boat_build.history_output(vm_id, command_id)
                err = boat_build.history_output(vm_id, command_id, stderr=True)
                return True, code, (out + err)[-4000:]
            break
        if time.time() >= deadline:
            return False, None, ""
        time.sleep(min(interval, max(5.0, deadline - time.time())))


def gh_json(args, cwd):
    result = run(["gh", *args], cwd=cwd)
    if result.returncode != 0:
        raise SystemExit(f"gh {' '.join(args)} failed:\n{result.stderr.strip()}")
    try:
        return json.loads(result.stdout or "null")
    except ValueError:
        raise SystemExit(
            f"gh {' '.join(args)} printed non-JSON:\n{result.stdout[-500:]}"
        )


def pr_head_sha(repo_dir, number):
    data = gh_json(
        ["pr", "view", str(number), "--json", "headRefOid,mergeable,mergeStateStatus"],
        cwd=repo_dir,
    )
    if not isinstance(data, dict) or not data.get("headRefOid"):
        raise SystemExit(f"PR #{number} has no head SHA yet; push first, then wait")
    if data.get("mergeable") == "CONFLICTING":
        raise SystemExit(
            f"PR #{number} is CONFLICTING: CI never starts on it. "
            "Rebase on origin/main instead of waiting."
        )
    return data["headRefOid"]


def repo_slug(repo_dir):
    data = gh_json(["repo", "view", "--json", "nameWithOwner"], cwd=repo_dir)
    slug = data.get("nameWithOwner", "") if isinstance(data, dict) else ""
    if not slug or "/" not in slug:
        raise SystemExit(f"cannot read repo slug in {repo_dir}")
    return slug


def bot_review_state(slug, sha, repo_dir):
    """bot-review commit status on one head SHA.

    success/failure/error/pending, or None when the App has not posted yet.
    Missing is pending, never failure: gated CI cannot start before approval.
    """
    data = gh_json(["api", f"repos/{slug}/commits/{sha}/status"], cwd=repo_dir)
    statuses = data.get("statuses", []) if isinstance(data, dict) else []
    for entry in statuses:
        if isinstance(entry, dict) and entry.get("context") == BOT_REVIEW_CONTEXT:
            return entry.get("state")
    return None


def gh_api_list(args, cwd):
    result = run(["gh", *args], cwd=cwd)
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def bot_findings(slug, number, sha, repo_dir):
    """Bot [blocker]/[major] findings on this head: ['path:line: first line'].

    Only inline comments on the current head SHA count; the bot marks each
    finding **[blocker]**/**[major]**, which no human text uses. Capped:
    the full review is one click away on the PR.
    """
    out = []
    comments = gh_api_list(
        ["api", f"repos/{slug}/pulls/{number}/comments", "--paginate"], cwd=repo_dir
    )
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if comment.get("commit_id") != sha:
            continue  # finding on an older head, already superseded
        body = comment.get("body", "") or ""
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        if not FINDING_RE.search(first):
            continue
        where = comment.get("path", "?") or "?"
        line = comment.get("line") or comment.get("original_line")
        out.append(f"{where}:{line}: {first}" if line else f"{where}: {first}")
        if len(out) >= 20:
            break
    return out


def commit_runs(repo_dir, sha):
    """(pending, failed, passed, cancelled) workflow runs for one commit SHA."""
    result = run(
        [
            "gh",
            "run",
            "list",
            "--commit",
            sha,
            "--json",
            "status,conclusion,databaseId",
            "--limit",
            "50",
        ],
        cwd=repo_dir,
    )
    if result.returncode != 0:
        raise SystemExit(f"gh run list failed:\n{result.stderr.strip()}")
    try:
        runs = json.loads(result.stdout or "[]")
    except ValueError:
        raise SystemExit(f"gh run list printed non-JSON:\n{result.stdout[-500:]}")
    pending = failed = passed = cancelled = 0
    for item in runs:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "completed":
            pending += 1
        elif item.get("conclusion") in ("success", "skipped"):
            passed += 1
        elif item.get("conclusion") == "cancelled":
            cancelled += 1  # superseded duplicate, not a verdict: wait it out
        else:
            failed += 1
    return pending, failed, passed, cancelled


def wait_pr(repo_dir, number, deadline, interval):
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    slug = sha = None

    def current():
        """True when still on sha; otherwise follow the new head."""
        nonlocal sha
        now = pr_head_sha(repo_dir, number)
        if now != sha:
            print(f"head moved {sha[:12]} -> {now[:12]}; following", flush=True)
            sha = now
            return False
        return True

    def transient(exc):
        """A rate-limited or unavailable gh is retried, never fatal."""
        if not TRANSIENT_GH_RE.search(str(exc)):
            raise exc
        if time.time() >= deadline:
            return (
                False,
                False,
                f"PR #{number}: gh transient error, still pending ({exc})",
            ), True
        print(f"transient gh error, retrying: {exc}", flush=True)
        time.sleep(min(interval, max(10.0, deadline - time.time())))
        return None, False

    while True:
        try:
            if slug is None:
                slug = repo_slug(repo_dir)
            if sha is None:
                sha = pr_head_sha(repo_dir, number)
                print(
                    f"PR #{number} head {sha[:12]} (waiting since {started})",
                    flush=True,
                )
            if not current():
                continue
            gate = bot_review_state(slug, sha, repo_dir)
            if gate in ("failure", "error"):
                verdict = f"PR #{number} {sha[:12]}: bot-review {gate}"
                for finding in bot_findings(slug, number, sha, repo_dir):
                    verdict += f"\n  {finding}"
                if not current():  # head moved mid-verdict: old head's verdict
                    continue
                return True, False, verdict
            if gate != "success":
                # gated CI cannot start before approval: pending is never failure
                if time.time() >= deadline:
                    return (
                        False,
                        False,
                        (
                            f"PR #{number} {sha[:12]}: bot-review "
                            f"{gate or 'missing'} — gated CI cannot start yet"
                        ),
                    )
                time.sleep(min(interval, max(10.0, deadline - time.time())))
                continue
            pending, failed, passed, cancelled = commit_runs(repo_dir, sha)
            if failed:
                if not current():  # never report a verdict for a superseded head
                    continue
                return True, False, f"PR #{number} {sha[:12]}: {failed} run(s) FAILED"
            if pending:
                if time.time() >= deadline:
                    return (
                        False,
                        False,
                        f"PR #{number} {sha[:12]}: {pending} still pending",
                    )
                time.sleep(min(interval, max(10.0, deadline - time.time())))
                continue
            if cancelled:
                # settled with cancellations (e.g. a superseded duplicate run):
                # neither GREEN nor FAILED; a human checks the run list.
                if not current():
                    continue
                return (
                    True,
                    False,
                    f"PR #{number} {sha[:12]}: {cancelled} run(s) CANCELLED "
                    f"({passed} passed)",
                )
            if passed:
                if not current():  # never report a verdict for a superseded head
                    continue
                return True, True, f"PR #{number} {sha[:12]}: GREEN ({passed} run(s))"
            # gate just passed, CI not started yet: wait, never fail
            if time.time() >= deadline:
                return (
                    False,
                    False,
                    f"PR #{number} {sha[:12]}: bot-review success, no CI run started",
                )
            time.sleep(min(interval, max(10.0, deadline - time.time())))
        except SystemExit as exc:
            outcome, expired = transient(exc)
            if expired:
                return outcome


def wait_run(repo_dir, run_id, deadline, interval):
    while True:
        data = gh_json(
            ["run", "view", str(run_id), "--json", "status,conclusion,url"],
            cwd=repo_dir,
        )
        status = data.get("status")
        if status != "completed":
            if time.time() >= deadline:
                return False, False, f"run {run_id}: still {status}"
            time.sleep(min(interval, max(10.0, deadline - time.time())))
            continue
        conclusion = data.get("conclusion")
        url = data.get("url", "")
        ok = conclusion in ("success", "skipped")
        return True, ok, f"run {run_id}: {conclusion} {url}"


def main(argv):
    mode, target, timeout, interval = None, None, None, 15.0
    resume = ""
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("--pid", "--boat", "--pr", "--run") and args:
            if mode is not None:
                raise SystemExit("pass exactly one of --pid/--boat/--pr/--run")
            mode, target = arg, args.pop(0)
            if arg != "--pid" and args and not args[0].startswith("--"):
                target = (target, args.pop(0))
        elif arg == "--timeout" and args:
            timeout = parse_timeout(args.pop(0))
        elif arg == "--interval" and args:
            interval = float(args.pop(0))
        else:
            raise SystemExit(
                "usage: await.py (--pid N | --boat VM CMD | --pr DIR NUM | "
                "--run DIR ID) [--timeout SEC] [--interval SEC]"
            )
    if mode is None:
        raise SystemExit(
            "usage: await.py (--pid N | --boat VM CMD | --pr DIR NUM | "
            "--run DIR ID) [--timeout SEC] [--interval SEC]"
        )
    if timeout is None:
        timeout = MAX_TIMEOUT
    if mode == "--pid":
        resume = f"just await --pid {target}"
    elif mode == "--boat":
        resume = f"just await --boat {target[0]} {target[1]}"
    elif mode == "--pr":
        resume = f"just await --pr {target[0]} {target[1]}"
    else:
        resume = f"just await --run {target[0]} {target[1]}"
    deadline = time.time() + timeout
    if mode == "--pid":
        done = wait_pid(int(target), deadline, min(interval, 5.0))
        if not done:
            print(
                f"pid {target}: still running after {timeout:.0f}s "
                f"(exit {STILL_PENDING}). resume in the same turn: {resume}"
            )
            raise SystemExit(STILL_PENDING)
        print(f"pid {target}: exited")
        raise SystemExit(0)
    if mode == "--boat":
        vm_id, command_id = target
        done, code, out = boat_wait(vm_id, command_id, deadline, interval)
        if not done:
            print(
                f"boat {vm_id} {command_id}: RUNNING after {timeout:.0f}s "
                f"(exit {STILL_PENDING}). resume in the same turn: {resume}"
            )
            raise SystemExit(STILL_PENDING)
        print(f"boat {vm_id} {command_id}: rc={code}\n{out}")
        raise SystemExit(0 if code == 0 else 1)
    repo_dir, number = target
    if mode == "--pr":
        done, ok, verdict = wait_pr(str(repo_dir), str(number), deadline, interval)
    else:
        done, ok, verdict = wait_run(str(repo_dir), str(number), deadline, interval)
    print(verdict)
    if not done:
        print(
            f"still pending (exit {STILL_PENDING}). resume in the same turn: {resume}"
        )
        raise SystemExit(STILL_PENDING)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
