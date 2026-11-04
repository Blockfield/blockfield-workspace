#!/usr/bin/env python3
"""One blocking wait with a hard deadline for local waits.

Exactly one mode:
  --pid N                 local PID (kill -0 poll)
  --boat VM COMMAND_ID    Boat exec command: poll Boat history until finishedAt
  --run DIR RUN_ID        gh workflow run until completed
Common: --timeout SEC (default 540, capped at 540), --interval SEC (default 15).

Every wait fits one shell call: the timeout never exceeds 540 s, so the
~10 min tool limit cannot hit mid-wait. On timeout await prints the exact
resume command and exits 75 (still pending). Transient gh
failures (rate limits, 5xx) are retried inside the budget, never fatal.

Exit 0 on success, 1 on a failed verdict or a refused wait, 75 on timeout.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import boat_build  # noqa: E402  (history_entries, history_output)

MAX_TIMEOUT = 540.0  # one shell call must never hit the ~10 min tool limit
STILL_PENDING = 75  # timeout exit: re-run the printed resume command


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
        if arg in ("--pid", "--boat", "--run") and args:
            if mode is not None:
                raise SystemExit("pass exactly one of --pid/--boat/--run")
            mode, target = arg, args.pop(0)
            if arg != "--pid" and args and not args[0].startswith("--"):
                target = (target, args.pop(0))
        elif arg == "--timeout" and args:
            timeout = parse_timeout(args.pop(0))
        elif arg == "--interval" and args:
            interval = float(args.pop(0))
        else:
            raise SystemExit(
                "usage: await.py (--pid N | --boat VM CMD | "
                "--run DIR ID) [--timeout SEC] [--interval SEC]"
            )
    if mode is None:
        raise SystemExit(
            "usage: await.py (--pid N | --boat VM CMD | "
            "--run DIR ID) [--timeout SEC] [--interval SEC]"
        )
    if timeout is None:
        timeout = MAX_TIMEOUT
    if mode == "--pid":
        resume = f"just await --pid {target}"
    elif mode == "--boat":
        resume = f"just await --boat {target[0]} {target[1]}"
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
