#!/usr/bin/env python3
"""One-command remote build on a Boat VM: resume/fork, transfer exact commits
(without pushing to origin), ship the dirty worktree as a patch, optionally
install locally built fork jars, run a command with a hard timeout, fetch logs,
and stop the VM only after a terminal verdict and preserved evidence.

Example:
  just boat-build --name bf-task --branch my-branch --timeout 1800 -- \
      'cd localmods/blockfield-mod && ./gradlew :server:test --max-workers=2'
  (--name alone resumes the own stopped VM of that name, else forks the
  newest golden; --vm/--fork-from pin the source explicitly.)
  just boat-build --vm bx_mine --local-jar tacz-refabricated=/tmp/TACZ.jar -- \\
      'just setup --force'
"""

import argparse
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


# The Boat environment clones the workspace under its GitHub repo name; older VMs keep the pre-rename name.
REMOTE_REPOS = ("blockfield-workspace", "blockfield-workspace")


TASK = "boat-build"


def run_boat(*args, check=True):
    sys.path.insert(0, str(ROOT / "scripts"))
    from toolchain import child_env, tool

    command = [str(tool("boat")), "--no-update", *args]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=child_env(),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # exec streams and file transfers can run for minutes; 540 s stays inside one resumable call.
            timeout=540 if args and args[0] in ("exec", "push", "scp") else 60,
        )
    except subprocess.TimeoutExpired as exc:
        result = subprocess.CompletedProcess(
            command,
            75,
            (exc.stdout or b"").decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            "Boat stream deadline reached",
        )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"boat {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )
    return result


def run_git(*args, cwd=ROOT, check=True):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=False)
    target.add_argument(
        "--resume-run", help="resume the saved run without dispatching again"
    )
    target.add_argument("--vm", help="resume this stopped VM (with --no-wait)")
    target.add_argument("--fork-from", help="fork this VM's latest snapshot")
    parser.add_argument(
        "--name",
        help="task VM name: with --fork-from it names the fork; alone it "
        "resumes the own stopped VM of that name, else forks the newest "
        "golden (required with --fork-from)",
    )
    parser.add_argument(
        "--type", choices=("small", "default"), help="machine size for resume/fork"
    )
    parser.add_argument(
        "--workspace-sha", default=None, help="workspace commit (default: local HEAD)"
    )
    parser.add_argument(
        "--submodule",
        action="append",
        default=[],
        metavar="PATH=SHA",
        help="submodule commit (repeatable)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="take workspace+submodule SHAs from this local branch",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="hard timeout in seconds for the remote command",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the VM running when the command finishes (default: "
        "stop after copying evidence and print the billed VM-minutes)",
    )
    parser.add_argument(
        "--out", default=None, help="local dir for build.log and fetched reports"
    )
    parser.add_argument(
        "--task", default="boat-build", help="task name for BF_DEV_HOME and session id"
    )
    parser.add_argument("--evidence", action="append", default=[], metavar="GLOB")
    parser.add_argument(
        "--fetch",
        action="append",
        default=[],
        metavar="REMOTE_PATH",
        help="remote path to copy back (repeatable)",
    )
    parser.add_argument(
        "--stage-dir",
        default=None,
        help="local dir for transfer bundles (default: fresh temp dir); "
        "must be outside the repo: `boat push` skips git-ignored files",
    )
    parser.add_argument(
        "--with-worktree",
        dest="with_worktree",
        action="store_true",
        default=True,
        help="ship `git diff HEAD` + untracked files of the touched repos "
        "as a patch applied on the VM (default: on)",
    )
    parser.add_argument(
        "--no-worktree",
        dest="with_worktree",
        action="store_false",
        help="skip the worktree patch; transfer commits only",
    )
    parser.add_argument(
        "--local-jar",
        action="append",
        default=[],
        metavar="DIR=PATH",
        help="locally built fork jar to install on the VM (repeatable); DIR is a "
        "localmods/ dirname or a pack metafile it owns, PATH a local .jar. "
        "The mod must classify as LOCAL (see devstack `just build-mods --plan`).",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="command to run on the VM (after --)"
    )
    args = parser.parse_args(argv)
    if args.fork_from and not args.name:
        parser.error("--name is required with --fork-from")
    if not args.resume_run and not args.vm and not args.fork_from and not args.name:
        parser.error("need --resume-run, --vm, --fork-from, or --name")
    command = [c for c in args.command if c != "--"]
    if not command and not args.resume_run:
        parser.error("no command to run (append -- <command> [args...])")
    args.command = command
    return args


def local_repo_present(rel):
    """True when the local checkout for a wanted repo exists and is a repo.

    Git resolves a cwd without its own repository by walking upward, so any
    local access (diff, rev-parse, bundle) to a missing or uninitialized
    submodule checkout would silently operate on the parent workspace.
    """
    return (ROOT / rel / ".git").exists() if rel else True


def local_shas(args):
    """Resolve wanted SHAs: {repo_relpath: sha}, '' is the workspace itself."""
    if args.branch:
        ws = run_git("rev-parse", f"{args.branch}^{{commit}}", cwd=ROOT).stdout.strip()
        wanted = {"": ws}
        # Recursive: nested-path submodules (localmods/*) are gitlinks below
        # a tree, invisible to a top-level ls-tree, and would silently keep
        # the golden's pins on the VM while the task changes them.
        out = run_git("ls-tree", "-r", args.branch, cwd=ROOT).stdout.splitlines()
        for line in out:
            mode, kind, sha, name = line.split(None, 3)
            if kind == "commit":
                wanted[name] = sha
    else:
        if args.workspace_sha:
            ws = run_git(
                "rev-parse", f"{args.workspace_sha}^{{commit}}", cwd=ROOT
            ).stdout.strip()
        else:
            ws = run_git("rev-parse", "HEAD", cwd=ROOT).stdout.strip()
        wanted = {"": ws}
        for line in run_git("submodule", "status", cwd=ROOT).stdout.splitlines():
            line = line.strip().lstrip(" +-")
            if line:
                sha, name = line.split()[:2]
                wanted[name] = sha
    for item in args.submodule:
        name, _, sha = item.partition("=")
        if not name or not sha:
            raise SystemExit(f"bad --submodule {item!r}, want PATH=SHA")
        repo = ROOT / name
        if not local_repo_present(name):
            raise SystemExit(
                f"bad --submodule {item!r}: no initialized checkout at {name}; "
                f"run `git submodule update --init {name}` first"
            )
        wanted[name] = run_git(
            "rev-parse", f"{sha}^{{commit}}", cwd=repo
        ).stdout.strip()
    return wanted


class Running(RuntimeError):
    def __init__(self, vm_id, command_id, command, start=None, timeout=None):
        self.vm_id, self.command_id, self.command = vm_id, command_id, command
        self.start, self.timeout = start, timeout  # dispatch instant + its budget
        super().__init__(f"RUNNING vm={vm_id} command={command_id}")


DEAD_EXEC_CODE = 124  # timeout convention: history recorded no verdict

# Argparse default for --timeout: a resumed run whose file predates the
# "timeout" field gets this budget, never the 540 s stream window.
DEFAULT_TIMEOUT = 1800

# Extra seconds past the command's own --timeout before a verdict-less exec
# is declared dead: covers the history write landing just after the command.
DEAD_EXEC_GRACE = 120


def _now_iso():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _entry_verdict(vm_id, command_id, entry, record):
    """Terminal (True, code, output) for an entry with both verdict fields."""
    try:
        code = int(entry["exitCode"])
    except (TypeError, ValueError):
        return None
    if record is not None:
        record.update(exitCode=code, finishedAt=entry["finishedAt"])
    return (
        True,
        code,
        (
            history_output(vm_id, command_id, strict=True)
            + history_output(vm_id, command_id, stderr=True, strict=True)
        ),
    )


def _boat_start_epoch(entry):
    """Boat's own start timestamp for a history entry (None when absent)."""
    try:
        return datetime.fromisoformat(
            str(entry.get("startedAt")).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError, AttributeError):
        return None


def _dead_exec_verdict(vm_id, command_id, record, reason):
    """Terminal failure for a dead exec: late verdict wins, else fail 124.

    Past its own timeout + grace a verdict-less exec is dead (its process is
    gone but history never recorded it), so fail with an exit status instead
    of hanging RUNNING forever. A stopped VM keeps its disk, so evidence is
    not lost; stopping also kills a hung exec.
    """
    if command_id:
        entries = history_entries(vm_id, limit=100, strict=True)
        entry = next((e for e in entries if e.get("commandId") == command_id), None)
        if entry is None and len(entries) >= 100:
            # Pushed out of the newest-100 window is not "never recorded": refuse to guess 124.
            raise RuntimeError(
                f"command {command_id} is older than the last 100 history entries; "
                "its verdict is unknown, check it manually"
            )
        if entry and entry.get("finishedAt") and entry.get("exitCode") is not None:
            verdict = _entry_verdict(vm_id, command_id, entry, record)
            if verdict is not None:
                return verdict
        partial = history_output(vm_id, command_id) + history_output(
            vm_id, command_id, stderr=True
        )
    else:
        partial = ""
    if record is not None:
        record.update(exitCode=DEAD_EXEC_CODE, finishedAt=_now_iso(), deadExec=True)
    msg = (
        f"dead exec: command {command_id or '<none>'} {reason}; "
        f"marked failed with exit {DEAD_EXEC_CODE}"
    )
    if partial:
        msg += f"\npartial output:\n{partial}"
    return True, DEAD_EXEC_CODE, msg


def dispatched_output(text):
    """True when exec output shows the VM ran something (not a bare CLI error)."""
    try:
        payload = json.loads(text or "")
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "stdout" in payload:
        return True
    for line in (text or "").splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict) and "type" in frame:
            return True
    return False


def vm_exec(vm_id, script, timeout=120, record=None):
    start = time.time()
    result = run_boat(
        "exec",
        "--json",
        "--stream",
        "--timeout",
        str(timeout),
        vm_id,
        "bash",
        "-lc",
        script,
        check=False,
    )
    rc, out = parse_exec_output(result.stdout, result.returncode)
    command_id = parse_command_id(result.stdout)
    if not command_id:
        # No command to poll: a saved run could never match history, so a
        # resume would loop exit 75 forever. Fail here instead.
        detail = (result.stdout or "") + (getattr(result, "stderr", "") or "")
        if result.returncode and not dispatched_output(result.stdout):
            raise RuntimeError(f"boat exec failed before dispatch:\n{detail}")
        raise RuntimeError(
            "boat exec returned no command id; dispatch status unknown: check "
            f"`boat history {vm_id} --mode commands --limit 5 --json` for a "
            "command started just now. If it is there, put its commandId into "
            "the run record and resume with --resume-run; otherwise rerun the "
            "build. Giving up instead of polling exit 75 with no command.\n"
            f"boat exec rc={result.returncode}, output tail:\n{detail[-2000:]}"
        )
    if record is not None:
        record.update(commandId=command_id, dispatchedAt=start)
    finished, code, stored = remote_status(vm_id, command_id, start, timeout, record)
    if not finished:
        raise Running(vm_id, command_id, script, start, timeout)
    return code, stored or out


def parse_exec_output(text, cli_rc):
    """Unwrap `boat exec` output: a single JSON envelope (short timeouts) or
    JSONL stream frames (long timeouts). Returns (rc, combined output)."""
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "stdout" in payload:
        out = (payload.get("stdout") or "") + (payload.get("stderr") or "")
        try:
            return int(payload.get("exitCode", cli_rc)), out
        except (TypeError, ValueError):
            return cli_rc, out
    chunks, rc, framed = [], cli_rc, False
    for line in text.splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if not isinstance(frame, dict) or "type" not in frame:
            continue
        framed = True
        if frame["type"] in ("stdout", "stderr"):
            chunks.append(frame.get("data") or "")
        elif frame["type"] == "exit":
            try:
                rc = int(frame.get("exitCode", rc))
            except (TypeError, ValueError):
                pass
    if framed:
        return rc, "".join(chunks)
    return cli_rc, text


def parse_command_id(text):
    """commandId of the VM-side command, if the exec output names it."""
    try:
        payload = json.loads(text or "")
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("commandId"):
        return payload["commandId"]
    for line in (text or "").splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict) and frame.get("commandId"):
            return frame["commandId"]
    return None


def history_entries(vm_id, limit=5, strict=False):
    """Recent exec entries from Boat history, newest first ([] on failure)."""
    result = run_boat(
        "history",
        vm_id,
        "--mode",
        "commands",
        "--limit",
        str(limit),
        "--json",
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
        if strict and (
            result.returncode or not isinstance(payload.get("commands"), list)
        ):
            raise RuntimeError("history unavailable; refusing VM stop")
        entries = payload.get("commands", [])
    except (ValueError, AttributeError):
        if strict:
            raise RuntimeError("history unavailable; refusing VM stop") from None
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def history_output(vm_id, command_id, stderr=False, strict=False):
    """Stored output of a finished command ("" when unavailable)."""
    args = ["history", vm_id, command_id, "--json"]
    if stderr:
        args.append("--stderr")
    result = run_boat(*args, check=False)
    try:
        payload = json.loads(result.stdout)
        if strict and (result.returncode or "data" not in payload):
            raise RuntimeError("history output unavailable; evidence not preserved")
        return payload.get("data", "") or ""
    except (ValueError, AttributeError):
        if strict:
            raise RuntimeError(
                "history output unavailable; evidence not preserved"
            ) from None
        return ""


def remote_status(vm_id, command_id, exec_start, timeout, record=None):
    """Only matching history with both verdict fields can complete a run.

    The budget is the command's own timeout plus grace — never the 540 s
    stream window: a command still inside its budget stays pending (the
    caller raises Running for a later resume). Past the budget the exec is
    dead (its process is gone but history never recorded it): fail with an
    exit status instead of hanging RUNNING forever.
    """
    budget = timeout + DEAD_EXEC_GRACE
    if not command_id:
        # Nothing was dispatched: no resume could ever match history.
        return _dead_exec_verdict(vm_id, command_id, record, "no command dispatched")
    poll_end = exec_start + min(timeout, 540)
    while True:
        entry = next(
            (
                e
                for e in history_entries(vm_id, limit=100)
                if e.get("commandId") == command_id
            ),
            None,
        )
        if entry and entry.get("finishedAt") and entry.get("exitCode") is not None:
            verdict = _entry_verdict(vm_id, command_id, entry, record)
            if verdict is not None:
                return verdict
        # Measure the deadline from Boat's own start timestamp when the
        # history record has one; fall back to the local dispatch instant.
        started = _boat_start_epoch(entry) if entry else None
        deadline = (started if started is not None else exec_start) + budget
        now = time.time()
        if now >= deadline:
            return _dead_exec_verdict(
                vm_id, command_id, record, "no verdict past the deadline"
            )
        if now >= poll_end:
            return False, None, ""
        time.sleep(min(10, max(0, poll_end - now)))


def remote_shas(vm_id, remote_root):
    script = (
        f"cd {shlex.quote(remote_root)} && git rev-parse HEAD && "
        "git submodule status | sed 's/^ *//'"
    )
    _, out = vm_exec(vm_id, script)
    lines = out.strip().splitlines()
    current = {"": lines[0].strip() if lines else ""}
    for line in lines[1:]:
        if line.startswith("-"):
            continue
        parts = line.lstrip(" +-").split()
        if len(parts) >= 2:
            current[parts[1]] = parts[0]
    return current


def remote_home(vm_id):
    rc, out = vm_exec(vm_id, "echo $HOME")
    if rc != 0 or not out.strip():
        raise RuntimeError(f"cannot determine remote home:\n{out}")
    return out.strip()


def remote_workspace(vm_id, home):
    probe = " ".join(
        f"[ -d {home}/{name}/.git ] && echo {name};" for name in REMOTE_REPOS
    )
    rc, out = vm_exec(vm_id, probe + " true")
    found = out.split()
    if rc != 0 or not found:
        raise RuntimeError(
            f"no workspace clone ({', '.join(REMOTE_REPOS)}) under {home}:\n{out}"
        )
    return f"{home}/{found[0]}"


def default_tip(local_repo):
    """Local commit of origin's default branch, or None when unknown."""
    head = run_git(
        "symbolic-ref", "refs/remotes/origin/HEAD", cwd=local_repo, check=False
    ).stdout.strip()
    candidates = (
        [head.split("refs/remotes/")[-1]] if head.startswith("refs/remotes/") else []
    )
    candidates += ["origin/main", "origin/master"]
    for ref in candidates:
        out = run_git(
            "rev-parse", "--verify", f"{ref}^{{commit}}", cwd=local_repo, check=False
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def worktree_patch(local_repo):
    """`git diff HEAD --binary` plus untracked files, or None when clean.

    Untracked files join the diff via temporary intent-to-add entries, then the
    index is reset for exactly those paths (pre-existing staged changes stay).
    A missing checkout has no local changes to ship (None): without this guard
    git would walk upward and diff the parent workspace instead.
    """
    if not Path(local_repo, ".git").exists():
        return None
    tracked = run_git("diff", "HEAD", "--binary", "--", cwd=local_repo, check=False)
    if tracked.returncode != 0:
        raise RuntimeError(f"git diff HEAD failed:\n{tracked.stdout}")
    out = run_git(
        "ls-files", "--others", "--exclude-standard", "-z", cwd=local_repo
    ).stdout
    untracked = [p for p in out.split("\0") if p]
    if not untracked:
        return tracked.stdout.encode() or None
    run_git("add", "-N", "--", *untracked, cwd=local_repo)
    try:
        full = run_git("diff", "HEAD", "--binary", "--", cwd=local_repo)
    finally:
        # Only our intent-to-add entries: staged content of other paths stays.
        run_git("reset", "-q", "--", *untracked, cwd=local_repo, check=False)
    return full.stdout.encode() or None


def bundle_exclusions(local_repo, want):
    """`^base` args for the transfer bundle: one merge-base with origin's
    default branch per parent line of want.

    A merge's bundle records a prerequisite per parent line (every excluded
    commit bordering the new history), so a single base is not enough. Empty
    when unknown (the caller then ships the full ref) or when a boundary
    cannot be computed.
    """
    out = run_git(
        "rev-list", "--parents", "--max-count=1", want, cwd=local_repo, check=False
    )
    if out.returncode != 0:
        return []
    parents = out.stdout.strip().split()[1:]
    tip = default_tip(local_repo)
    if not tip:
        return []
    exclusions = []
    for rev in parents if len(parents) > 1 else [want]:
        base = run_git("merge-base", rev, tip, cwd=local_repo, check=False)
        if base.returncode != 0 or not base.stdout.strip():
            return []
        for line in base.stdout.strip().splitlines():
            line = line.strip()
            if line and line != want and line not in exclusions:
                exclusions.append(line)
    return exclusions


def vm_has_all(vm_id, remote, commits):
    """True when every commit object exists in the VM repo (one exec)."""
    if not commits:
        return True
    check = " && ".join(
        f"git -C {shlex.quote(remote)} cat-file -e {shlex.quote(c)}" for c in commits
    )
    rc, _ = vm_exec(vm_id, check)
    return rc == 0


def vm_bundle_verify(vm_id, remote, guest):
    """(ok, output) of `git bundle verify` for the pushed bundle on the VM."""
    rc, out = vm_exec(
        vm_id, f"git -C {shlex.quote(remote)} bundle verify {shlex.quote(guest)}"
    )
    return rc == 0, out


def sender_is_shallow(local_repo):
    """True when the local clone hides history (shallow grafts or info/grafts).

    Bundle math on a grafted view omits graft-hidden parent links, so the
    packed history is one the VM cannot complete even though `bundle verify`
    (which only checks the recorded prerequisites) passes.
    """
    out = run_git("rev-parse", "--is-shallow-repository", cwd=local_repo, check=False)
    if out.returncode == 0 and out.stdout.strip() == "true":
        return True
    grafts = run_git(
        "rev-parse", "--git-path", "info/grafts", cwd=local_repo, check=False
    ).stdout.strip()
    return bool(grafts) and Path(grafts).is_file() and Path(grafts).stat().st_size > 0


def ensure_sender_complete(local_repo):
    """Deepen a grafted sender to full history before packing a bundle.

    Returns "complete" (nothing to do) or "unshallowed". A grafted sender
    must not pack: its bundles omit graft-hidden objects and the VM fetch
    dies with "Could not read" on the first hidden link.
    """
    if not sender_is_shallow(local_repo):
        return "complete"
    out = run_git("fetch", "origin", "--unshallow", cwd=local_repo, check=False)
    if out.returncode != 0:
        raise RuntimeError(
            f"cannot unshallow {local_repo} "
            f"(`git fetch origin --unshallow`):\n{out.stdout}"
        )
    return "unshallowed"


def unreadable_hint(detail, local_repo):
    """Point at the object a failed VM fetch could not read, if named."""
    match = re.search(r"Could not read ([0-9a-f]{40})", detail or "")
    if not match:
        return ""
    sha = match.group(1)
    known = run_git(
        "show", "-s", "--format=%h %s", sha, cwd=local_repo, check=False
    ).stdout.strip()
    return (
        f"\nVM cannot read {sha}"
        f" ({known or 'unknown to the sender; deepen it first'}): "
        f"the golden is stale or the sender packed graft-hidden history."
    )


def parse_local_jars(specs):
    """[(name, jar_path)] from DIR=PATH specs; fail fast on bad input."""
    parsed = []
    for item in specs:
        name, _, path = item.partition("=")
        jar = Path(path) if path else None
        if not name or jar is None or not str(path):
            raise SystemExit(f"bad --local-jar {item!r}, want DIR=PATH.jar")
        if jar.suffix.lower() != ".jar" or not jar.is_file():
            raise SystemExit(f"--local-jar {name}: no jar file at {jar}")
        parsed.append((name, jar))
    return parsed


GH_CREDENTIAL = "credential.https://github.com.helper=!gh auth git-credential"


def ensure_repo(vm_id, remote_root, home, rel, want, stage_dir):
    """Check out the exact commit on the VM, transferring objects if needed."""
    remote = f"{remote_root}/{rel}".rstrip("/")
    if rel:
        rc, out = vm_exec(
            vm_id,
            # Clone the default branch rather than `submodule update`, which needs the gitlink commit
            # on origin; the commit itself arrives below, by fetch or bundle. `submodule init` resolves
            # relative URLs into .git/config. Boat puts the GitHub token only into its own clones and
            # gh, not into git's global config.
            f"test -e {shlex.quote(remote + '/.git')} || {{ "
            f"key=$(git -C {shlex.quote(remote_root)} config -f .gitmodules --get-regexp '^submodule[.].*[.]path$' "
            f"| awk -v p={shlex.quote(rel)} '$2 == p {{ sub(/[.]path$/, \".url\", $1); print $1 }}') && "
            f"git -C {shlex.quote(remote_root)} submodule init -q -- {shlex.quote(rel)} && "
            f'url=$(git -C {shlex.quote(remote_root)} config --get "$key") && '
            # shortcut: one retry for an intermittent GitHub "multiple updates for ref" clone failure
            # seen on Boat; find the cause if it fails twice in a row.
            f'{{ git clone -q -c {shlex.quote(GH_CREDENTIAL)} "$url" {shlex.quote(remote)} || '
            f'git clone -q -c {shlex.quote(GH_CREDENTIAL)} "$url" {shlex.quote(remote)}; }}; }} && '
            # A fresh clone starts with hooks unwired: point it at the versioned
            # hooks when they exist and no explicit choice was made (best-effort).
            f"{{ test -d {shlex.quote(remote + '/.githooks')} && "
            f"git -C {shlex.quote(remote)} config --get core.hooksPath >/dev/null 2>&1 || "
            f"test ! -d {shlex.quote(remote + '/.githooks')} || "
            f"git -C {shlex.quote(remote)} config core.hooksPath .githooks || true; }}",
        )
        if rc != 0:
            raise RuntimeError(f"cannot initialize submodule {rel}:\n{out}")
    rc, out = vm_exec(vm_id, f"git -C {shlex.quote(remote)} rev-parse HEAD")
    current = out.strip() if rc == 0 else ""
    if current == want:
        return "already there"
    if rel:
        local_repo = ROOT / rel
    else:
        local_repo = ROOT
    # Pushed SHAs: fetch from origin, no local transfer at all.
    rc, out = vm_exec(
        vm_id,
        f"git -C {shlex.quote(remote)} -c {shlex.quote(GH_CREDENTIAL)} fetch -q origin {shlex.quote(want)} "
        f"&& git -C {shlex.quote(remote)} rev-parse --verify "
        f"{shlex.quote(want + '^{commit}')}",
    )
    if not (rc == 0 and out.strip() == want):
        # Local-only commits: ship the delta as a bundle (never via origin).
        if not local_repo_present(rel):
            raise RuntimeError(
                f"want {want} in {rel or 'workspace'} is not on the VM and the "
                f"local checkout of {rel} is not initialized; run "
                f"`git submodule update --init {rel}` first"
            )
        if (
            run_git(
                "cat-file", "-e", f"{want}^{{commit}}", cwd=local_repo, check=False
            ).returncode
            != 0
        ):
            raise RuntimeError(
                f"want {want} in {rel or 'workspace'} is not a local commit; "
                f"commit or stash it first (`git -C {local_repo} status --short`)"
            )
        # The sender must see complete history first: a grafted (shallow)
        # clone packs graft-hidden parent links the VM then cannot read, and
        # `bundle verify` still passes (it only checks the recorded
        # prerequisites), so the fetch dies with "Could not read".
        ensure_sender_complete(local_repo)
        # A merge's bundle records one prerequisite per parent line (every
        # excluded commit bordering the new history), so one checked base is
        # not enough: verify the pushed bundle on the VM with `git bundle
        # verify`, fetch it, and fall back to the full ref (no prerequisites)
        # when either step fails. The old `^VM-HEAD` failed the same way when
        # the VM sat at a commit the local clone never fetched.
        tag = f"boat-tmp-{want[:12]}"
        bundle = stage_dir / f"{(rel or 'workspace').replace('/', '_')}.bundle"
        guest = f"{home}/{stage_dir.name}/{bundle.name}"
        try:
            run_git("tag", "-f", tag, want, cwd=local_repo)
            exclusions = bundle_exclusions(local_repo, want)
            if exclusions and not vm_has_all(vm_id, remote, exclusions):
                exclusions = []  # a boundary the VM lacks: ship the full ref
            if exclusions:
                count = run_git(
                    "rev-list",
                    "--count",
                    want,
                    *[f"^{e}" for e in exclusions],
                    cwd=local_repo,
                    check=False,
                ).stdout.strip()
                if count == "0":
                    exclusions = None  # nothing new; objects must be on the VM

            def ship(excls):
                """Push, verify and fetch the bundle for excls; (ok, output)."""
                run_git(
                    "bundle",
                    "create",
                    str(bundle),
                    *([tag] + [f"^{e}" for e in excls]),
                    cwd=local_repo,
                )
                run_boat("push", vm_id, str(stage_dir))
                ok, detail = vm_bundle_verify(vm_id, remote, guest)
                if not ok:
                    return False, f"verify:\n{detail}"
                rc2, out2 = vm_exec(
                    vm_id,
                    f"git -C {shlex.quote(remote)} fetch {shlex.quote(guest)} "
                    f"+{shlex.quote(tag)}:{shlex.quote(tag)}",
                )
                if rc2 != 0:
                    return False, f"fetch:\n{out2}"
                return True, out2

            ok, detail = (True, "") if exclusions is None else ship(exclusions)
            if not ok and exclusions:
                # A boundary the pre-check missed (shallow/partial VM clone),
                # or a fetch-only read the verify pass cannot see: the full
                # ref has no prerequisites to miss.
                exclusions = []
                ok, detail = ship([])
            if not ok:
                raise RuntimeError(
                    f"bundle of {want} does not fetch "
                    f"in {rel or 'workspace'}:\n{detail}"
                    + unreadable_hint(detail, local_repo)
                )
        finally:
            run_git("tag", "-d", tag, cwd=local_repo, check=False)
            # Hygiene: a stopped fork can become a snapshot source; never leave
            # our tmp tags behind on the VM.
            vm_exec(vm_id, f"git -C {shlex.quote(remote)} tag -d {shlex.quote(tag)}")
    rc, out = vm_exec(
        vm_id, f"git -C {shlex.quote(remote)} checkout --detach {shlex.quote(want)}"
    )
    if rc != 0:
        raise RuntimeError(f"checkout of {want} in {rel or 'workspace'} failed:\n{out}")
    rc, head = vm_exec(vm_id, f"git -C {shlex.quote(remote)} rev-parse HEAD")
    if rc != 0 or want not in [line.strip() for line in head.strip().splitlines()]:
        raise RuntimeError(
            f"checkout of {want} in {rel or 'workspace'} left HEAD at "
            f"{head.strip() or '<no output>'} (checkout said):\n{out}"
        )
    return "checked out"


def write_env(vm_id, remote_root, task):
    bf_home = f"{remote_root}/.dev/{task}"
    session = f"boat-{vm_id}-{task}"
    # Export BF_DEV_HOME first so the managed JDK lands under the task dir;
    # resolve JH/QBIN now and bake literal values into the env file.
    script = (
        f"cd {shlex.quote(remote_root)}"
        f" && export BF_DEV_HOME={shlex.quote(bf_home)}"
        f" AI_AGENT_SESSION_ID={shlex.quote(session)}"
        " && python3 -I scripts/quality.py run java -version"
        " && JH=$(python3 -c \"import sys; sys.path.insert(0, 'scripts');"
        ' from toolchain import java_home; print(java_home())")'
        " && QBIN=$(python3 -c \"import sys; sys.path.insert(0, 'scripts');"
        " from toolchain import pinned_bins; print(':'.join(pinned_bins()))\")"
        " && PYBIN=$(python3 -c \"import sys; sys.path.insert(0, 'scripts');"
        " from toolchain import canonical_python;"
        ' print(canonical_python().parent)")'
        ' && { echo "export BF_DEV_HOME=$BF_DEV_HOME";'
        ' echo "export AI_AGENT_SESSION_ID=$AI_AGENT_SESSION_ID";'
        ' echo "export BLOCKFIELD_BOAT_VM=1";'
        ' echo "export JAVA_HOME=$JH";'
        ' echo "export PATH=$JH/bin:$PYBIN:$QBIN:/usr/local/sbin:/usr/local/bin'
        ':/usr/sbin:/usr/bin:/sbin:/bin"; }'
        f" > {shlex.quote(bf_home)}/boat-env.sh"
        f" && cat {shlex.quote(bf_home)}/boat-env.sh"
        ' && "$JH/bin/java" -version'
    )
    rc, out = vm_exec(vm_id, script, timeout=600)
    if rc != 0:
        raise RuntimeError(f"env provisioning failed:\n{out}")
    return f"{bf_home}/boat-env.sh"


def current_state(vm_id):
    result = run_boat("info", "--json", vm_id, check=False)
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        info = payload.get("sandbox", payload)
        return info.get("state", "?"), info.get("type")
    except (ValueError, KeyError, AttributeError):
        return "?", None


GOLDEN_PREFIX = "bf-golden-"


def list_vms():
    """All VMs from `boat list --all` ([] when unparseable)."""
    result = run_boat("list", "--all", "--json", check=False)
    text = result.stdout or ""

    def unwrap(data):
        if isinstance(data, dict):
            for key in ("sandboxes", "vms", "items"):
                if isinstance(data.get(key), list):
                    return data[key]
            return []
        return data if isinstance(data, list) else []

    try:
        return [v for v in unwrap(json.loads(text)) if isinstance(v, dict)]
    except ValueError:
        pass
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    return [v for v in unwrap(data) if isinstance(v, dict)]


def latest_golden_vm():
    """Newest snapshot-ready golden (forks excluded): task VMs start here."""
    goldens = [
        v
        for v in list_vms()
        if str(v.get("name", "")).startswith(GOLDEN_PREFIX)
        and not str(v.get("name", "")).endswith(" fork")
        and v.get("snapshotAvailable")
    ]
    if not goldens:
        raise RuntimeError("no golden VM with a snapshot; bake one: just boat-bake")
    goldens.sort(key=lambda v: v.get("snapshotCompletedAt") or v.get("createdAt") or "")
    return goldens[-1]["id"]


def find_vm_by_name(name, states=("stopped",)):
    """ID of the VM with this exact name in one of the states (None when absent)."""
    for vm in list_vms():
        if vm.get("name") == name and (not states or vm.get("state") in states):
            return vm.get("id")
    return None


def attach_vm(vm_id, vm_type):
    """Resume a stopped VM, or reuse a running one (refuses silent resize)."""
    state, cur_type = current_state(vm_id)
    if state != "stopped":
        if vm_type and vm_type != cur_type:
            # Size change on a running VM is refused; stop first.
            raise RuntimeError(
                "stop and preserve prior run evidence before resizing a running VM"
            )
        return vm_id, "running"  # already up; exec runs against it
    resume = ["resume", "--no-wait", vm_id]
    if vm_type:
        resume += ["--type", vm_type]
    run_boat(*resume)
    return vm_id, "resumed"


def fork_vm(source, vm_type, out_dir):
    """Fork source's latest snapshot and wait until ready."""
    fork_args = ["fork", source, "--json", "--ttl", "5400"]
    if vm_type:
        fork_args += ["--type", vm_type]
    result = run_boat(*fork_args)
    try:
        events = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        vm_id = events[-1].get("id") or events[-1].get("sandbox", {}).get("id")
    except (ValueError, KeyError):
        vm_id = None
    if not vm_id:
        raise RuntimeError(f"fork gave no id:\n{result.stdout}")
    print(f"boat_build provisioned vm={vm_id}", flush=True)
    (out_dir / "provision.json").write_text(json.dumps({"vm": vm_id}))
    state = wait_state(vm_id, want=("running", "idle"), timeout=240)
    if state not in ("running", "idle"):
        raise RuntimeError(
            f"VM {vm_id} not ready (state={state}); inspect before cleanup"
        )
    return vm_id


def wait_state(vm_id, want=("stopped",), timeout=240):
    deadline = time.time() + timeout
    state = "?"
    while time.time() < deadline:
        result = run_boat("info", "--json", vm_id, check=False)
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            state = payload.get("sandbox", payload)["state"]
        except (ValueError, KeyError, AttributeError):
            pass
        if state in want:
            return state
        time.sleep(10)
    return state


def ensure_worktree(vm_id, remote_root, home, wanted, stage_dir):
    """Ship `git diff HEAD` + untracked files of the touched repos as patches.

    Applied on the VM after checkout with `git apply --check` first, so a patch
    that cannot apply fails here with the file list instead of mid-command.
    Returns the number of repos patched.
    """
    patches = {}
    for rel in sorted(wanted):
        local_repo = ROOT / rel if rel else ROOT
        patch = worktree_patch(local_repo)
        if patch:
            # The patch is diffed against the local HEAD, but the VM checks
            # out wanted[rel]: applying one onto the other builds code that
            # matches neither tree. Fail fast instead of shipping it.
            head = run_git("rev-parse", "HEAD", cwd=local_repo, check=False)
            if head.returncode == 0 and head.stdout.strip() != wanted[rel]:
                raise RuntimeError(
                    f"worktree patch for {rel or 'workspace'} is diffed against "
                    f"local HEAD {head.stdout.strip()[:12]}, but the VM checks out "
                    f"{wanted[rel][:12]}; commit, stash or checkout first "
                    "(or drop it with --no-worktree)"
                )
            name = f"{(rel or 'workspace').replace('/', '_')}.patch"
            (stage_dir / name).write_bytes(patch)
            patches[rel] = name
    if not patches:
        return 0
    run_boat("push", vm_id, str(stage_dir))
    for rel, name in patches.items():
        remote = f"{remote_root}/{rel}".rstrip("/")
        guest = f"{home}/{stage_dir.name}/{name}"
        # A reused warm VM still carries the previous run's applied patch, so
        # apply --check would fail on the leftovers: reset to want first
        # (.dev/.cache hold task state and local caches, never patch output).
        rc, out = vm_exec(
            vm_id,
            f"git -C {shlex.quote(remote)} reset --hard {shlex.quote(wanted[rel])} "
            "&& git -C "
            f"{shlex.quote(remote)} clean -fd -e .dev -e .cache",
        )
        if rc != 0:
            raise RuntimeError(
                f"worktree reset to {wanted[rel][:12]} failed for {rel or 'workspace'}:\n{out}"
            )
        rc, out = vm_exec(
            vm_id,
            f"cat {shlex.quote(guest)} | git -C {shlex.quote(remote)} apply --check -",
        )
        if rc != 0:
            raise RuntimeError(
                f"worktree patch for {rel or 'workspace'} cannot apply on the VM "
                f"(rebase it onto {wanted[rel][:12]} or drop it with --no-worktree):\n{out}"
            )
        rc, out = vm_exec(
            vm_id,
            f"cat {shlex.quote(guest)} | git -C {shlex.quote(remote)} apply "
            f"--whitespace=fix - && git -C {shlex.quote(remote)} status --short",
        )
        if rc != 0:
            raise RuntimeError(
                f"worktree patch apply failed for {rel or 'workspace'}:\n{out}"
            )
    return len(patches)


def ensure_local_jars(vm_id, remote_root, home, specs, stage_dir, bf_home):
    """Install locally built fork jars where devstack's localmods/ finds them.

    Destination mirrors modmap: an entry's literal `jar` path, else the
    `build/libs/` dir scanned by newest_jar. A modmap stamp is written on the
    VM so `ensure_built` keeps the pushed jar instead of rebuilding it.
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import modmap

    mapping = modmap.load_mapping()
    staged = []
    for name, jar in specs:
        if name.endswith(".pw.toml"):
            dirname = modmap.owner(name, mapping)
            if not dirname:
                raise SystemExit(f"--local-jar {name}: no localmods/ entry owns it")
        else:
            dirname = name
        entry = mapping.get(dirname)
        if entry is None or dirname.startswith("_"):
            raise SystemExit(f"--local-jar {dirname}: unknown localmods/ entry")
        source, reason = modmap.classify(ROOT, dirname, entry)
        if source != modmap.LOCAL:
            raise SystemExit(
                f"--local-jar {dirname}: modmap says PIN ({reason}); the pushed jar "
                f"would be ignored — make a relevant change first "
                f"(`just build-mods --plan`)"
            )
        if "jar" in entry:
            dest_rel = f"localmods/{dirname}/{entry['jar']}"
        else:
            dest_rel = f"localmods/{dirname}/build/libs/{jar.name}"
        staged.append((dirname, jar, dest_rel))
    for dirname, jar, dest_rel in staged:
        staged_name = f"localjar-{dirname}-{jar.name}"
        shutil.copyfile(jar, stage_dir / staged_name)
    run_boat("push", vm_id, str(stage_dir))
    for dirname, jar, dest_rel in staged:
        guest = f"{home}/{stage_dir.name}/localjar-{dirname}-{jar.name}"
        dest = f"{remote_root}/{dest_rel}"
        rc, out = vm_exec(
            vm_id,
            f"mkdir -p {shlex.quote(str(Path(dest).parent))} && cp {shlex.quote(guest)} {shlex.quote(dest)}",
        )
        if rc != 0:
            raise RuntimeError(f"--local-jar {dirname}: install failed:\n{out}")
    for dirname, _, dest_rel in staged:
        script = (
            f"cd {shlex.quote(remote_root)} && python3 -I -c "
            f"\"import sys; sys.path.insert(0, 'scripts/dev'); import modmap; "
            f"m = modmap.load_mapping(); "
            f"modmap.write_stamp('{bf_home}', '{dirname}', "
            f"'localmods/{dirname}', modmap.local_jars('.', {{'{dirname}': m['{dirname}']}}))\""
        )
        rc, out = vm_exec(vm_id, script)
        if rc != 0:
            raise RuntimeError(
                f"--local-jar {dirname}: pushed, but the modmap stamp failed — "
                f"`just setup` on the VM would rebuild instead of using it:\n{out}"
            )
    return [d for d, _, _ in staged]


def vm_minutes(vm_id):
    result = run_boat("usage", "--json", vm_id, check=False)
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        seconds = float(data.get("seconds") or 0)
        mult = float(data.get("billingMultiplier") or 1)
        return seconds * mult / 60, data
    except (ValueError, KeyError, TypeError):
        return 0.0, {"raw": result.stdout[-500:]}


def save_evidence(vm_id, record, out_dir, patterns, warn_unmatched=False):
    """Copy before stop; an unmatched pattern only warns when warn_unmatched.

    A dead exec or a setup-phase resume may never have produced the files
    its patterns name: with warn_unmatched those patterns only log a
    warning, so the run still writes its manifest and reaches the stop.
    A failed copy still raises, keeping the VM available for recovery.
    """
    for index, pattern in enumerate(patterns):
        script = "python3 -c " + shlex.quote(
            "import glob,json; print(json.dumps(glob.glob("
            + repr(pattern)
            + ", recursive=True)))"
        )
        try:
            _, raw = vm_exec(vm_id, script)
            paths = json.loads(raw)
        except Running:
            raise  # a still-running lookup must keep the VM and its resume
        except (RuntimeError, ValueError) as exc:
            if not warn_unmatched:
                raise
            print(
                f"warning: evidence lookup failed for {pattern}: {exc}",
                file=sys.stderr,
            )
            continue
        if not paths:
            if warn_unmatched:
                print(
                    f"warning: evidence pattern matched nothing: {pattern}",
                    file=sys.stderr,
                )
                continue
            raise RuntimeError(f"evidence pattern matched nothing: {pattern}")
        for match_index, path in enumerate(paths):
            dest = out_dir / f"evidence-{index}" / str(match_index)
            dest.mkdir(parents=True, exist_ok=True)
            run_boat("scp", "-r", f"{vm_id}:{path}", str(dest))
    record["files"] = {
        str(p.relative_to(out_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in out_dir.rglob("*")
        if p.is_file() and p.name not in ("manifest.json", "resume.json")
    }
    (out_dir / "manifest.json").write_text(json.dumps(record, indent=2) + "\n")


def _legacy_verdict(vm_id, record):
    """Finished history entry for a resume file without dispatchedAt.

    No dispatch timestamp means no budget, so this never fails dead: a
    finished verdict completes the run, anything else stays pending.
    """
    command_id = record.get("commandId")
    if not command_id:
        return False, None, ""
    entry = next(
        (
            e
            for e in history_entries(vm_id, limit=100)
            if e.get("commandId") == command_id
        ),
        None,
    )
    if entry and entry.get("finishedAt") and entry.get("exitCode") is not None:
        verdict = _entry_verdict(vm_id, command_id, entry, record)
        if verdict is not None:
            return verdict
    return False, None, ""


def finish_run(saved, out_dir):
    """Complete a resumed run: never dispatches, never retries a dead exec.

    A dead dispatch found here ends 124 with deadExec set (the one retry
    applies only to the in-process path); rerun the build for a fresh run.
    """
    vm_id, record = saved["vm"], saved["record"]
    # Same budget as the dispatch: the command's own --timeout plus grace.
    # Pre-change resume files lack "timeout": fall back to the argparse
    # default, never the stream window.
    timeout = saved.get("timeout", DEFAULT_TIMEOUT)
    if record.get("dispatchedAt") is None:
        # Legacy resume file: no dispatch timestamp, so no budget and never
        # a dead verdict — but a finished verdict still completes the run.
        done, code, out = _legacy_verdict(vm_id, record)
    else:
        exec_start = record["dispatchedAt"]
        done, code, out = remote_status(
            vm_id, record.get("commandId"), exec_start, timeout, record
        )
    if not done:
        print(
            f"RUNNING vm={vm_id}; resume: just boat-build --resume-run {out_dir / 'resume.json'}"
        )
        return 75
    if record.get("deadExec"):
        print(
            "dead exec, not retried on resume; rerun the build",
            file=sys.stderr,
        )
    (out_dir / "build.log").write_text(out)
    save_evidence(
        vm_id,
        record,
        out_dir,
        saved["patterns"],
        warn_unmatched=bool(record.get("deadExec")) or saved.get("phase") == "setup",
    )
    if saved.get("out"):
        shutil.copytree(out_dir, saved["out"], dirs_exist_ok=True)
    if not saved["keep"]:
        run_boat("stop", vm_id)
        if wait_state(vm_id) != "stopped":
            raise RuntimeError(f"{vm_id} did not reach stopped")
    return code if saved.get("phase", "run") == "run" else (code or 3)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if args.resume_run:
        path = Path(args.resume_run).resolve()
        return finish_run(json.loads(path.read_text()), path.parent)
    run_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    out_dir = ROOT / ".dev" / args.task / run_name
    out_dir.mkdir(parents=True, exist_ok=True)
    # Outside the repo: `boat push` skips git-ignored files, and .dev is ignored.
    if args.stage_dir:
        stage_dir = Path(args.stage_dir)
        stage_dir.mkdir(parents=True, exist_ok=True)
        own_stage = False
    else:
        stage_dir = Path(tempfile.mkdtemp(prefix="boat-build-"))
        own_stage = True
    wanted = local_shas(args)

    # Fail fast before billing a VM: bad jar specs, stage inside the repo
    # (push skips ignored files there), or an in-progress rebase/merge that
    # would make the transfer ambiguous.
    jar_specs = parse_local_jars(args.local_jar)
    if not stage_dir.resolve().is_relative_to(Path(tempfile.gettempdir())) and (
        stage_dir.resolve() == ROOT or ROOT in stage_dir.resolve().parents
    ):
        raise SystemExit(f"--stage-dir must be outside the repo, got {stage_dir}")
    for rel in sorted(wanted):
        if not local_repo_present(rel):
            # Uninitialized submodule: no local merge state can exist there,
            # and its wanted SHA comes from the recorded gitlink (fetched from
            # origin on the VM). Checking it here would walk upward into the
            # parent workspace.
            continue
        local_repo = ROOT / rel if rel else ROOT
        flag = run_git(
            "rev-parse", "--verify", "MERGE_HEAD", cwd=local_repo, check=False
        )
        if flag.returncode == 0:
            raise SystemExit(
                f"{rel or 'workspace'}: finish the in-progress merge first; "
                f"boat_build cannot transfer a mid-merge state"
            )

    started = time.time()
    forked = False
    if args.fork_from:
        vm_id = fork_vm(args.fork_from, args.type, out_dir)
        forked = True
    elif args.vm:
        vm_id, _ = attach_vm(args.vm, args.type)
    else:
        # --name alone: resume the own stopped VM first, else fork newest golden.
        vm_id = find_vm_by_name(args.name)
        if vm_id is None:
            vm_id = fork_vm(latest_golden_vm(), args.type, out_dir)
            forked = True
        else:
            vm_id, _ = attach_vm(vm_id, args.type)

    rc = 3  # shortcut: setup failed before the command ran, <when> helper grows more steps
    # Never lose the id: with --keep a setup failure leaves the VM running,
    # and this line (before any fallible setup) tells the caller what to stop.
    print(f"boat_build vm={vm_id}: setup starts")
    log_path = out_dir / "build.log"
    pending = False
    copies_ok = True
    record = {
        "vm": vm_id,
        "command": args.command,
        "exitCode": None,
        "finishedAt": None,
    }
    saved = {
        "vm": vm_id,
        "record": record,
        "patterns": args.fetch + args.evidence,
        "keep": args.keep,
        "timeout": args.timeout,
        "phase": "setup",
        "out": str(Path(args.out).resolve()) if args.out else None,
    }
    try:
        if forked:
            # Inside try: a failed rename must still stop the new fork.
            run_boat("rename", vm_id, args.name)
        home = remote_home(vm_id)
        remote_root = remote_workspace(vm_id, home)
        current = remote_shas(vm_id, remote_root)
        for rel, want in sorted(wanted.items()):
            if current.get(rel) != want:
                ensure_repo(vm_id, remote_root, home, rel, want, stage_dir)
                if not rel:
                    current = remote_shas(vm_id, remote_root)
        if args.with_worktree:
            patched = ensure_worktree(vm_id, remote_root, home, wanted, stage_dir)
        else:
            patched = 0
        env_file = write_env(vm_id, remote_root, args.task)
        bf_home = f"{remote_root}/.dev/{args.task}"
        installed = []
        if jar_specs:
            installed = ensure_local_jars(
                vm_id, remote_root, home, jar_specs, stage_dir, bf_home
            )
        print(
            f"transfer: {len(wanted)} repos, {patched} worktree patches, "
            f"{len(installed)} local jars"
            + (f" ({', '.join(installed)})" if installed else "")
        )
        if len(args.command) == 1:
            quoted = args.command[0]  # one arg = shell snippet, run as-is
        else:
            quoted = " ".join(shlex.quote(c) for c in args.command)
        script = (
            f"source {shlex.quote(env_file)} && cd {shlex.quote(remote_root)} && "
            f"START=$SECONDS && {quoted}; RC=$?; "
            'echo "BOAT_BUILD rc=$RC elapsed=$((SECONDS-START))s"; exit $RC'
        )
        saved["phase"] = "run"
        (out_dir / "resume.json").write_text(json.dumps(saved, indent=2))
        try:
            rc, out = vm_exec(vm_id, script, timeout=args.timeout, record=record)
            if record.get("deadExec"):
                # One retry: the first dispatch died past its own deadline
                # with no verdict; a fresh dispatch may succeed.
                record.pop("deadExec", None)
                rc, out = vm_exec(vm_id, script, timeout=args.timeout, record=record)
        finally:
            (out_dir / "resume.json").write_text(json.dumps(saved, indent=2))
        log_path.write_text(out, encoding="utf-8")
        save_evidence(
            vm_id,
            record,
            out_dir,
            saved["patterns"],
            warn_unmatched=bool(record.get("deadExec"))
            or saved.get("phase") == "setup",
        )
        if args.out:
            try:
                shutil.copytree(out_dir, args.out, dirs_exist_ok=True)
            except (OSError, shutil.Error) as exc:
                copies_ok = False
                print(
                    f"--out copy to {args.out} failed ({exc}); VM {vm_id} left running"
                )
                raise
    except Running as exc:
        pending = True
        if record["finishedAt"] is None:
            record.update(commandId=exc.command_id, command=exc.command)
            if exc.start is not None:
                # Setup execs dispatch with no record: persist when it ran
                # and its budget so a resume reads history instead of 75.
                record["dispatchedAt"] = exc.start
        if exc.timeout is not None:
            saved["timeout"] = exc.timeout
        (out_dir / "resume.json").write_text(json.dumps(saved, indent=2))
        print(f"{exc}; resume: just boat-build --resume-run {out_dir / 'resume.json'}")
        return 75
    finally:
        # Only preserved, terminal runs with all requested copies may stop the VM.
        if (
            not args.keep
            and not pending
            and copies_ok
            and (out_dir / "manifest.json").exists()
        ):
            run_boat("stop", vm_id)
            state = wait_state(vm_id)
            if state != "stopped":
                raise RuntimeError(f"{vm_id} did not reach stopped")
        else:
            state = current_state(vm_id)[0]
        if own_stage:
            shutil.rmtree(stage_dir, ignore_errors=True)
        else:
            for left in stage_dir.glob("*.bundle"):
                left.unlink(missing_ok=True)
            for left in stage_dir.glob("*.patch"):
                left.unlink(missing_ok=True)
            for left in stage_dir.glob("localjar-*.jar"):
                left.unlink(missing_ok=True)
    minutes, _ = vm_minutes(vm_id)
    wall = (time.time() - started) / 60
    print(
        f"vm={vm_id} rc={rc} state={state} "
        f"vm_minutes={minutes:.1f} wall_minutes={wall:.1f} log={log_path}"
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
