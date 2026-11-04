"""Hermetic before/after replays for the agent-friction tooling.

Each test replays one past failure pattern from the 2026-10-31/11-01
coordinator log against fixture repos (bare origin + clones + a fake `gh`
on PATH): the old behavior would have stalled, pushed, or waited; the new
helper refuses or waits correctly. No network, no Boat, no Gradle.
"""

import importlib.util
import contextlib
import io
import os
import stat
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

DEV = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEV / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent_preflight = load("agent_preflight")
await_mod = load("await")
pr_ship = load("pr_ship")
boat_build = load("boat_build")
devstack = load("devstack")


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{result.stdout}")
    return result


def configure(repo):
    git(repo, "config", "user.email", "ci@example.invalid")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "commit.gpgsign", "false")


class FixtureRepos:
    """Bare origin + clone + linked worktree, all offline (mixin)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin = base / "origin.git"
        git(base, "init", "--bare", "-q", "origin.git")
        seed = base / "seed"
        git(base, "clone", "-q", str(self.origin), str(seed))
        configure(seed)
        (seed / "file.txt").write_text("one\n")
        git(seed, "add", "file.txt")
        git(seed, "commit", "-qm", "one")
        git(seed, "push", "-q", "origin", "HEAD:main")
        git(seed, "push", "-q", "origin", "HEAD:refs/heads/other")
        self.clone = base / "clone"
        git(base, "clone", "-q", "--branch", "main", str(self.origin), str(self.clone))
        configure(self.clone)

    def advance_origin(self, text="two"):
        work = Path(self.tmp.name) / "writer"
        git(
            Path(self.tmp.name),
            "clone",
            "-q",
            "--branch",
            "main",
            str(self.origin),
            str(work),
        )
        configure(work)
        (work / "file.txt").write_text(text + "\n")
        git(work, "commit", "-qam", text)
        git(work, "push", "-q", "origin", "HEAD:main")

    def worktree(self, name="wt"):
        path = Path(self.tmp.name) / name
        git(self.clone, "worktree", "add", str(path))
        return path


class PreflightWorktree(unittest.TestCase, FixtureRepos):
    def setUp(self):
        FixtureRepos.setUp(self)

    def test_plain_task_clone_passes(self):
        agent_preflight.assert_worktree(self.clone)

    def test_linked_worktree_passes(self):
        agent_preflight.assert_worktree(self.worktree())


class PreflightStale(unittest.TestCase, FixtureRepos):
    """Pattern 2 replay (knife-live built stale mod 3672713a)."""

    def setUp(self):
        FixtureRepos.setUp(self)

    def test_behind_refuses(self):
        self.advance_origin()
        wt = self.worktree()
        git(wt, "fetch", "-q", "origin")
        line, bad = agent_preflight.check_repo(wt, "", fetch=False)
        self.assertTrue(bad, line)
        self.assertIn("BEHIND", line)

    def test_rebased_passes(self):
        self.advance_origin()
        wt = self.worktree()
        git(wt, "fetch", "-q", "origin")
        git(wt, "rebase", "-q", "origin/main")
        line, bad = agent_preflight.check_repo(wt, "", fetch=False)
        self.assertFalse(bad, line)
        self.assertIn("ok", line)

    def test_ahead_with_local_commits_passes(self):
        wt = self.worktree()
        (wt / "local.txt").write_text("unpushed\n")
        git(wt, "add", "local.txt")
        git(wt, "commit", "-qm", "local")
        line, bad = agent_preflight.check_repo(wt, "", fetch=False)
        self.assertFalse(bad, line)
        self.assertIn("ahead", line)

    def test_mid_merge_refuses(self):
        wt = self.worktree()
        gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
        (gitdir / "MERGE_HEAD").write_text("0" * 40)
        self.assertEqual(agent_preflight.mid_flight(wt, ""), ["merge"])

    def test_mid_rebase_apply_refuses(self):
        wt = self.worktree()
        gitdir = Path(git(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
        apply = gitdir / "rebase-apply"
        apply.mkdir(exist_ok=True)
        (apply / "head-name").write_text("refs/heads/main\n")
        self.assertEqual(agent_preflight.mid_flight(wt, ""), ["rebase"])

    def test_full_main_behind_is_exit_1(self):
        self.advance_origin()
        wt = self.worktree()
        git(wt, "fetch", "-q", "origin")
        with self.assertRaises(SystemExit) as ctx:
            old = os.getcwd()
            os.chdir(wt)
            try:
                agent_preflight.main(["--repo", "", "--no-fetch"])
            finally:
                os.chdir(old)
        self.assertEqual(ctx.exception.code, 1)


class PrShipCheckoutGate(unittest.TestCase, FixtureRepos):
    """pr-ship gate: task-owned checkouts pass, the main checkout is refused.

    Replays the post-278 block: agents shipping mod/site/proxy/CP PRs from a
    submodule inside their T3 worktree (or a cache clone) were refused as if
    they were the coordinator's main checkout, because for those layouts git
    dir == common dir too. The gate now follows the superproject chain and
    admits standalone Blockfield checkouts outside the invoking workspace's
    main checkout (no machine paths involved).
    """

    def setUp(self):
        FixtureRepos.setUp(self)
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.oldcwd = os.getcwd()
        self.addCleanup(os.chdir, self.oldcwd)

    def blockfield(self, repo, name="blockfield-mod"):
        git(
            repo,
            "remote",
            "set-url",
            "origin",
            f"https://github.com/Blockfield/{name}.git",
        )

    def fresh_clone(self, name):
        dest = Path(self.tmp.name) / name
        git(
            Path(self.tmp.name),
            "clone",
            "-q",
            "--branch",
            "main",
            str(self.origin),
            str(dest),
        )
        configure(dest)
        return dest

    def test_submodule_inside_worktree_passes(self):
        wt = self.worktree("unit")
        configure(wt)
        git(
            wt,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.origin),
            "mod",
        )
        sub = wt / "mod"
        # the old gate rejected exactly this layout:
        own = git(sub, "rev-parse", "--path-format=absolute", "--git-dir")
        common = git(sub, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.assertEqual(own.stdout.strip(), common.stdout.strip())
        agent_preflight.assert_worktree(sub)  # must not raise

    def test_cache_clone_passes(self):
        main = self.fresh_clone("main")
        self.blockfield(main, name="blockfield-workspace")
        cache = self.fresh_clone("cache")
        self.blockfield(cache)
        os.chdir(main)
        agent_preflight.assert_worktree(cache)  # must not raise

    def test_task_clone_from_own_cwd_passes(self):
        clone = self.fresh_clone("task")
        self.blockfield(clone)
        os.chdir(clone)
        agent_preflight.main(["--no-fetch"])

    def test_absolute_script_invocation(self):
        for clone in (self.fresh_clone("task"), self.worktree()):
            with self.subTest(clone=clone):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        str(DEV / "agent_preflight.py"),
                        "--no-fetch",
                    ],
                    cwd=clone,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_workspace_linked_worktree_passes(self):
        self.blockfield(self.clone, name="blockfield-workspace")
        wt = self.worktree()
        os.chdir(wt)
        agent_preflight.assert_worktree(wt)

    def test_main_checkout_refused(self):
        main = self.fresh_clone("main")
        os.chdir(main)
        for name in ("blockfield-workspace", "blockfield-workspace"):
            for host in ("github.com", "ssh.github.com"):
                for url in (
                    f"https://{host}/Blockfield/{name}.git",
                    f"git@{host}:Blockfield/{name}.git",
                    f"ssh://git@{host}:443/Blockfield/{name}.git",
                ):
                    with self.subTest(url=url):
                        git(main, "remote", "set-url", "origin", url)
                        with self.assertRaises(SystemExit) as ctx:
                            agent_preflight.assert_worktree(main)
                        self.assertIn("main checkout", str(ctx.exception))

    def test_task_clone_independent_of_invoking_checkout(self):
        main = self.fresh_clone("main")
        self.blockfield(main, name="blockfield-workspace")
        cache = self.fresh_clone("cache")
        self.blockfield(cache)
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        os.chdir(plain)  # not a git repo: invoking main is unknowable
        agent_preflight.assert_worktree(cache)

    def test_unresolvable_workspace_pin_ref_fails(self):
        ws = self.fresh_clone("ws")
        git(
            ws,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.origin),
            "mod",
        )
        git(ws, "commit", "-qm", "add mod")
        for ref in (
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/main",
            "refs/remotes/origin/master",
            "refs/remotes/origin/other",
        ):
            git(ws, "update-ref", "-d", ref, check=False)
        line, bad = agent_preflight.check_repo(ws, "mod", fetch=False)
        self.assertTrue(bad, line)
        self.assertIn("cannot read workspace pin ref", line)

    def test_foreign_origin_passes(self):
        other = self.fresh_clone("other")
        os.chdir(other)
        agent_preflight.assert_worktree(other)

    def test_pr_ship_from_cache_clone_pushes_and_reports_state(self):
        main = self.fresh_clone("main")
        self.blockfield(main, name="blockfield-workspace")
        repo = self.fresh_clone("repo")
        self.blockfield(repo)
        # hermetic transport: keep the Blockfield origin URL, but serve
        # fetch/push from the fixture bare repo (no network).
        git(
            repo,
            "config",
            f"url.{self.origin}.insteadOf",
            "https://github.com/Blockfield/blockfield-mod.git",
        )
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_REPO": str(repo),
        }
        git(repo, "checkout", "-qb", "feature")
        (repo / "new.txt").write_text("new\n")
        git(repo, "add", "new.txt")
        git(repo, "commit", "-qm", "new")
        os.chdir(main)
        out = io.StringIO()
        with patch.dict(os.environ, env):
            with (
                contextlib.redirect_stdout(out),
                self.assertRaises(SystemExit) as ctx,
            ):
                pr_ship.main([str(repo), "feature"])
        self.assertEqual(ctx.exception.code, 0)
        pushed = git(self.origin, "rev-parse", "refs/heads/feature")
        local = git(repo, "rev-parse", "HEAD")
        self.assertEqual(pushed.stdout.strip(), local.stdout.strip())
        self.assertIn("PR #42", out.getvalue())
        self.assertIn("http://x/42", out.getvalue())
        self.assertIn(local.stdout.strip()[:12], out.getvalue())
        self.assertIn("checks and reviews", out.getvalue())


class PreflightPins(unittest.TestCase):
    """Preflight compares pinned submodules to the origin/main gitlink.

    Replays the always-red preflight: every fresh worktree compared each
    submodule to its origin tip, so pinned submodules showed BEHIND and
    agents learned to ignore the gate. A submodule is now stale only when
    its checkout differs from the recorded gitlink; the repos the task
    works on (passed as --repo) must still track their origin tip.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.oldcwd = os.getcwd()
        self.addCleanup(os.chdir, self.oldcwd)
        base = Path(self.tmp.name)
        self.sub_origin = base / "sub.git"
        git(base, "init", "--bare", "-q", "sub.git")
        seed = base / "subseed"
        git(base, "clone", "-q", str(self.sub_origin), str(seed))
        configure(seed)
        (seed / "file.txt").write_text("one\n")
        git(seed, "add", "file.txt")
        git(seed, "commit", "-qm", "one")
        git(seed, "push", "-q", "origin", "HEAD:main")
        git(self.sub_origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.super_origin = base / "super.git"
        git(base, "init", "--bare", "-q", "super.git")
        superseed = base / "superseed"
        git(base, "clone", "-q", str(self.super_origin), str(superseed))
        configure(superseed)
        git(
            superseed,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(self.sub_origin),
            "mod",
        )
        git(superseed, "commit", "-qm", "pin mod")
        git(superseed, "push", "-q", "origin", "HEAD:main")
        git(self.super_origin, "symbolic-ref", "HEAD", "refs/heads/main")
        # advance the submodule origin past the recorded pin
        (seed / "file.txt").write_text("two\n")
        git(seed, "commit", "-qam", "two")
        git(seed, "push", "-q", "origin", "HEAD:main")
        self.super = base / "super"
        git(
            base,
            "clone",
            "-q",
            "--branch",
            "main",
            str(self.super_origin),
            str(self.super),
        )
        self.wt = base / "wt"
        git(self.super, "worktree", "add", str(self.wt))
        git(
            self.wt,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "-q",
            "--init",
            "mod",
        )

    def test_pinned_submodule_is_green(self):
        line, bad = agent_preflight.check_repo(self.wt, "mod", fetch=True)
        self.assertFalse(bad, line)
        self.assertIn("pin=", line)

    def test_moved_submodule_is_stale(self):
        sub = self.wt / "mod"
        git(sub, "fetch", "-q", "origin")
        git(sub, "checkout", "-q", "origin/main")
        line, bad = agent_preflight.check_repo(self.wt, "mod", fetch=True)
        self.assertTrue(bad, line)
        self.assertIn("STALE", line)

    def test_strict_task_repo_must_track_tip(self):
        line, bad = agent_preflight.check_repo(
            self.wt, "mod", fetch=True, strict_tip=True
        )
        self.assertTrue(bad, line)
        self.assertIn("BEHIND", line)

    def test_full_preflight_green_on_fresh_worktree(self):
        os.chdir(self.wt)
        agent_preflight.main([])  # must not raise

    def test_strict_repo_flag_refuses_stale_task_repo(self):
        os.chdir(self.wt)
        with self.assertRaises(SystemExit) as ctx:
            agent_preflight.main(["--repo", "mod"])
        self.assertEqual(ctx.exception.code, 1)


class AwaitPid(unittest.TestCase):
    def test_exited_pid_returns_zero(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        with self.assertRaises(SystemExit) as ctx:
            await_mod.main(["--pid", str(proc.pid), "--timeout", "5"])
        self.assertEqual(ctx.exception.code, 0)

    def test_running_pid_times_out(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.kill)
        with self.assertRaises(SystemExit) as ctx:
            await_mod.main(
                ["--pid", str(proc.pid), "--timeout", "1", "--interval", "1"]
            )
        self.assertEqual(ctx.exception.code, 75)
        proc.kill()


class AwaitBoat(unittest.TestCase):
    def test_finished_command_reports_rc(self):
        entry = {
            "commandId": "cmd1",
            "finishedAt": "2026-11-01T00:00:00Z",
            "exitCode": 3,
        }
        with (
            patch.object(await_mod.boat_build, "history_entries", return_value=[entry]),
            patch.object(await_mod.boat_build, "history_output", return_value="log\n"),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--boat", "bx_x", "cmd1", "--timeout", "10"])
        self.assertEqual(ctx.exception.code, 1)

    def test_unfinished_command_times_out_without_sleeping(self):
        entry = {"commandId": "cmd1", "startedAt": "2026-11-01T00:00:00Z"}
        with (
            patch.object(await_mod.boat_build, "history_entries", return_value=[entry]),
            patch.object(await_mod.time, "sleep", return_value=None),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--boat", "bx_x", "cmd1", "--timeout", "0"])
        self.assertEqual(ctx.exception.code, 75)


FAKE_GH = """#!/usr/bin/env python3
import json, os, subprocess, sys
repo = os.environ["FAKE_GH_REPO"]
args = sys.argv[1:]
if args[:2] == ["pr", "view"]:
    sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    if "statusCheckRollup" in args[-1]:
        # final state read: reports the per-check rollup, or fails transiently
        if os.environ.get("FAKE_GH_STATE_FAIL"):
            print("transient API failure", file=sys.stderr)
            sys.exit(1)
        state = os.environ.get("FAKE_GH_BOT", "SUCCESS")
        print(json.dumps({"number": 42, "mergeable": os.environ.get("FAKE_GH_MERGEABLE", "MERGEABLE"),
                          "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
                          "headRefOid": sha, "url": "http://x/42",
                          "statusCheckRollup": [{"__typename": "StatusContext", "context": "bot-review",
                                                 "state": state, "description": "fake"},
                                                {"__typename": "CheckRun", "name": "ci",
                                                 "status": "COMPLETED", "conclusion": "SUCCESS"}]}))
    elif os.environ.get("FAKE_GH_NO_PR"):
        print("no pull requests found for branch", file=sys.stderr)
        sys.exit(1)
    else:
        print(json.dumps({"number": 42, "mergeable": os.environ.get("FAKE_GH_MERGEABLE", "MERGEABLE"),
                          "mergeStateStatus": "CLEAN", "headRefOid": sha, "url": "http://x/42"}))
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "Blockfield/fake"}))
elif args[:1] == ["api"] and args[1].endswith("/status"):
    state = os.environ.get("FAKE_GH_BOT", "success")
    print(json.dumps({"state": state, "statuses": [{"context": "bot-review",
                      "state": state, "description": "fake"}]}))
elif args[:1] == ["api"] and args[1].endswith("/comments"):
    print(json.dumps([]))
elif args[:2] == ["run", "list"]:
    print(json.dumps([{"status": "completed", "conclusion": "success", "databaseId": 7}]))
elif args[:2] == ["pr", "create"]:
    print("http://x/42")
else:
    sys.exit(f"unexpected gh call: {args}")
"""


class AwaitGh(unittest.TestCase):
    def test_gh_json_passes_a_single_json_flag(self):
        seen = {}

        def fake_run(args, cwd=None):
            seen["args"] = list(args)

            class Result:
                returncode = 0
                stdout = '{"a": 1}'
                stderr = ""

            return Result()

        with patch.object(await_mod, "run", side_effect=fake_run):
            self.assertEqual(
                await_mod.gh_json(["pr", "view", "1", "--json", "number"], "."),
                {"a": 1},
            )
        self.assertEqual(seen["args"].count("--json"), 1)


class PrShip(unittest.TestCase, FixtureRepos):
    """Pattern 7 replay: conflicting/stale branch must not be pushed+waited on."""

    def setUp(self):
        FixtureRepos.setUp(self)
        # pr-ship operates on a linked worktree, like every agent unit.
        self.repo = self.worktree("unit")
        configure(self.repo)
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_REPO": str(self.repo),
        }
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_conflicting_rebase_is_refused_and_aborted(self):
        git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "file.txt").write_text("local\n")
        git(self.repo, "commit", "-qam", "local")
        self.advance_origin("remote")
        git(self.repo, "fetch", "-q", "origin")
        with self.assertRaises(SystemExit) as ctx:
            pr_ship.main([str(self.repo), "feature"])
        self.assertIn("CONFLICT", str(ctx.exception))
        # rebase was aborted: the branch still holds the local commit
        self.assertEqual(
            git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
            "feature",
        )
        self.assertIn("local", (self.repo / "file.txt").read_text())
        # and nothing was pushed
        remote = git(
            self.origin, "rev-parse", "--verify", "refs/heads/feature", check=False
        )
        self.assertNotEqual(remote.returncode, 0)

    def test_stale_branch_rebases_pushes_once_and_reports_state(self):
        git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-qm", "new")
        self.advance_origin("remote")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                pr_ship.main([str(self.repo), "feature"])
        self.assertEqual(ctx.exception.code, 0)
        pushed = git(self.origin, "rev-parse", "refs/heads/feature").stdout.strip()
        local = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(pushed, local)
        # single push: origin main is the remote change, feature contains both
        self.assertIn("remote", (self.repo / "file.txt").read_text())
        self.assertIn("PR #42", out.getvalue())
        self.assertIn(local[:12], out.getvalue())

    def test_conflicting_pr_is_never_waited_on(self):
        git(self.repo, "checkout", "-qb", "feat")
        self.env["FAKE_GH_MERGEABLE"] = "CONFLICTING"
        with patch.dict(os.environ, self.env):
            with self.assertRaises(SystemExit) as ctx:
                pr_ship.main([str(self.repo), "feat"])
        self.assertIn("CONFLICTING", str(ctx.exception))

    def test_state_report_includes_per_check_rollup(self):
        git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-qm", "new")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                pr_ship.main([str(self.repo), "feature"])
        self.assertEqual(ctx.exception.code, 0)
        # a failed bot-review status and a pending one share the same
        # aggregate merge state, so each check is printed individually.
        self.assertIn("bot-review:SUCCESS", out.getvalue())
        self.assertIn("ci:SUCCESS", out.getvalue())

    def test_create_then_failed_lookup_keeps_url_and_fails(self):
        git(self.repo, "checkout", "-qb", "fresh")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-qm", "new")
        env = {**self.env, "FAKE_GH_NO_PR": "1", "FAKE_GH_STATE_FAIL": "1"}
        with patch.dict(os.environ, env):
            with self.assertRaises(SystemExit) as ctx:
                pr_ship.main([str(self.repo), "fresh"])
        # identity unknown: non-zero so the caller notices, with the
        # creation URL kept so the PR can be recovered.
        self.assertNotEqual(ctx.exception.code, 0)
        self.assertIn("http://x/42", str(ctx.exception))


class BoatBuildKeep(unittest.TestCase):
    def test_keep_skips_stop(self):
        calls = []

        def fake_run_boat(*args, **kwargs):
            calls.append(args)
            if args[0] == "usage":
                return {"rc": 0, "out": '{"seconds": 120}'}
            if args[0] == "info":
                return {"rc": 0, "out": '{"state": "stopped"}'}
            return {"rc": 0, "out": "{}"}

        class FakeResult:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out
                self.stderr = ""

        def fake(*args, **kwargs):
            result = fake_run_boat(*args, **kwargs)
            return FakeResult(result["rc"], result["out"])

        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "logs")
            with (
                patch.object(boat_build, "run_boat", side_effect=fake),
                patch.object(boat_build, "local_shas", return_value={"": "abc"}),
                patch.object(boat_build, "remote_shas", return_value={"": "abc"}),
                patch.object(boat_build, "ensure_worktree", return_value=0),
                patch.object(boat_build, "write_env", return_value="/e.sh"),
                patch.object(boat_build, "vm_exec", return_value=(0, "ok")),
                patch.object(boat_build, "vm_minutes", return_value=(2.0, {})),
            ):
                self.assertTrue(
                    boat_build.parse_args(["--vm", "bx_m", "--", "true"]).keep is False
                )
                self.assertEqual(
                    boat_build.main(
                        [
                            "--vm",
                            "bx_m",
                            "--task",
                            "u",
                            "--out",
                            out,
                            "--keep",
                            "--",
                            "true",
                        ]
                    ),
                    0,
                )
        self.assertFalse([c for c in calls if c[:2] == ("stop", "bx_m")])


class GradleGuard(unittest.TestCase):
    def test_refuses_without_markers(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in (
                "BLOCKFIELD_BOAT_VM",
                "CI",
                "GITHUB_ACTIONS",
                "BF_ALLOW_LOCAL_GRADLE",
            )
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                devstack.require_boat_for_gradle("testbot build")
        self.assertIn("owner PC", str(ctx.exception))

    def test_allows_boat_vm(self):
        with patch.dict(os.environ, {"BLOCKFIELD_BOAT_VM": "1"}):
            devstack.require_boat_for_gradle("testbot build")

    def test_allows_ci(self):
        with patch.dict(os.environ, {"CI": "true"}, clear=False):
            env = {k: v for k, v in os.environ.items() if k != "BLOCKFIELD_BOAT_VM"}
            with patch.dict(os.environ, env, clear=True):
                devstack.require_boat_for_gradle("testbot build")


if __name__ == "__main__":
    unittest.main()
