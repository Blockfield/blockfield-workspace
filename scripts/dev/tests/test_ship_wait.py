"""Ship: push once and report, never wait (offline, stubbed gh).

Replays the 2026-11-01 incident: an agent copied pr_ship.py to /tmp and
blind-polled with `sleep 300` because a pr-ship wait outlived the 10 min
shell limit. The tooling now makes the right path the easy one: pr-ship
pushes once, prints the PR URL, head SHA and current check/review state,
and exits 0. No waiting, no resume commands. A second run on an unchanged
branch pushes nothing and just reports the state again.
"""

import contextlib
import io
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agent_friction import FixtureRepos, await_mod, configure, git, pr_ship

FAKE_GH = """#!/usr/bin/env python3
import json, os, subprocess, sys
repo = os.environ["FAKE_GH_REPO"]
args = sys.argv[1:]
if args[:2] == ["pr", "view"]:
    sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    print(json.dumps({"number": 42, "mergeable": os.environ.get("FAKE_GH_MERGEABLE", "MERGEABLE"),
                      "mergeStateStatus": "CLEAN", "reviewDecision": "APPROVED",
                      "headRefOid": sha, "url": "http://x/42"}))
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "Blockfield/fake"}))
elif args[:2] == ["run", "list"]:
    print(json.dumps([{"status": "completed", "conclusion": "success", "databaseId": 7}]))
elif args[:2] == ["run", "view"]:
    print(json.dumps({"status": "completed", "conclusion": "success", "url": "http://x/run"}))
elif args[:2] == ["pr", "create"]:
    print("http://x/42")
else:
    sys.exit(f"unexpected gh call: {args}")
"""


class TimeoutBound(unittest.TestCase):
    def test_budgets(self):
        self.assertEqual(await_mod.MAX_TIMEOUT, 540.0)
        self.assertEqual(await_mod.STILL_PENDING, 75)

    def test_timeout_is_capped(self):
        self.assertEqual(await_mod.parse_timeout("3600"), 540.0)
        self.assertEqual(await_mod.parse_timeout("60"), 60.0)
        self.assertEqual(await_mod.parse_timeout("0"), 0.0)

    def test_pid_timeout_exits_75_with_resume(self):
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.kill)
        self.addCleanup(proc.wait)
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--pid", str(proc.pid), "--timeout", "0"])
        self.assertEqual(ctx.exception.code, 75)
        self.assertIn("just await --pid", out.getvalue())
        proc.kill()
        proc.wait()


class PrModeRemoved(unittest.TestCase):
    def test_pr_flag_is_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            await_mod.main(["--pr", ".", "42"])
        self.assertIn("usage", str(ctx.exception))

    def test_pr_ship_has_no_wait_surface(self):
        self.assertFalse(hasattr(pr_ship, "MAX_TIMEOUT"))
        self.assertFalse(hasattr(pr_ship, "STILL_PENDING"))


class ShipReportsWithoutWaiting(unittest.TestCase, FixtureRepos):
    """Push once, report URL/head/state, exit 0; a second run pushes nothing."""

    def setUp(self):
        FixtureRepos.setUp(self)
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
        git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-qm", "new")

    def ship(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                pr_ship.main([str(self.repo), "feature", *args])
        return ctx.exception.code, out.getvalue()

    def test_push_reports_url_head_state_and_exits_zero(self):
        code, out = self.ship()
        self.assertEqual(code, 0)
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        pushed = git(self.origin, "rev-parse", "refs/heads/feature").stdout.strip()
        self.assertEqual(pushed, head)
        self.assertIn("PR #42", out)
        self.assertIn("http://x/42", out)
        self.assertIn(head[:12], out)
        self.assertIn("checks and reviews", out)
        self.assertIn("reviewDecision=APPROVED", out)

    def test_second_run_pushes_nothing(self):
        code, _ = self.ship()
        self.assertEqual(code, 0)
        pushed = git(self.origin, "rev-parse", "refs/heads/feature").stdout.strip()
        code, out = self.ship()
        self.assertEqual(code, 0)
        self.assertIn("no push needed", out)
        again = git(self.origin, "rev-parse", "refs/heads/feature").stdout.strip()
        self.assertEqual(pushed, again)

    def test_never_waits_or_polls(self):
        calls = []
        real_run = pr_ship.subprocess.run

        def record(*args, **kwargs):
            calls.append(args[0])
            return real_run(*args, **kwargs)

        with (
            patch.object(pr_ship.subprocess, "run", side_effect=record),
            patch("time.sleep", side_effect=AssertionError("poll")),
        ):
            code, _ = self.ship()
        self.assertEqual(code, 0)
        self.assertFalse(
            [c for c in calls if "await.py" in " ".join(str(a) for a in c)],
        )

    def test_timeout_flag_is_refused(self):
        with self.assertRaises(SystemExit) as ctx:
            pr_ship.main([str(self.repo), "feature", "--timeout", "60"])
        self.assertIn("usage", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
