"""Ship-wait: bounded, resumable, gate-aware waits (offline, stubbed gh).

Replays the 2026-11-01 incident: an agent copied pr_ship.py to /tmp and
blind-polled with `sleep 300` because a pr-ship wait outlived the 10 min
shell limit, while gated CI only starts after the review App approves.
The tooling must make the right path the easy one: bounded waits with an
exact resume command, no re-push on resume, bot-review gate before CI.
"""

import contextlib
import io
import os
import stat
import subprocess
import time
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


class TimeoutBound(unittest.TestCase):
    def test_budgets_match(self):
        self.assertEqual(await_mod.MAX_TIMEOUT, 540.0)
        self.assertEqual(pr_ship.MAX_TIMEOUT, await_mod.MAX_TIMEOUT)
        self.assertEqual(await_mod.STILL_PENDING, 75)
        self.assertEqual(pr_ship.STILL_PENDING, await_mod.STILL_PENDING)

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


class GateAwarePr(unittest.TestCase):
    SHA = "a" * 40

    def views(self, status):
        return [
            {"nameWithOwner": "Blockfield/fake"},
            {"headRefOid": self.SHA},
            {"headRefOid": self.SHA},
            status,
            {"headRefOid": self.SHA},
        ]

    def test_pending_gate_never_reports_no_ci(self):
        out = io.StringIO()
        with (
            patch.object(
                await_mod, "gh_json", side_effect=self.views({"statuses": []})
            ),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--pr", ".", "42", "--timeout", "0"])
        self.assertEqual(ctx.exception.code, 75)
        self.assertIn("bot-review", out.getvalue())
        self.assertNotIn("no CI run", out.getvalue())

    def test_pending_then_success_then_green(self):
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(
                await_mod, "bot_review_state", side_effect=["pending", "success"]
            ),
            patch.object(await_mod, "commit_runs", return_value=(0, 0, 1, 0)),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--pr", ".", "42", "--timeout", "60"])
        self.assertEqual(ctx.exception.code, 0)

    def test_failure_prints_path_line_findings(self):
        comments = [
            {
                "commit_id": self.SHA,
                "path": "a.py",
                "line": 10,
                "body": "**[major]** boom breaks\nmore text",
            },
            {
                "commit_id": self.SHA,
                "path": "b.py",
                "line": None,
                "original_line": 3,
                "body": "**[BLOCKER]** kaboom",
            },
            {
                "commit_id": self.SHA,
                "path": "c.py",
                "line": 5,
                "body": "**[minor]** meh",
            },
            {
                "commit_id": "0" * 40,
                "path": "d.py",
                "line": 1,
                "body": "**[major]** stale head",
            },
        ]
        status = {
            "statuses": [
                {"context": "bot-review", "state": "failure", "description": "x"}
            ]
        }
        with patch.object(await_mod, "gh_json", side_effect=self.views(status)):
            with patch.object(await_mod, "gh_api_list", return_value=comments):
                done, ok, verdict = await_mod.wait_pr(".", "42", 60.0, 15.0)
        self.assertTrue(done)
        self.assertFalse(ok)
        self.assertIn("bot-review failure", verdict)
        self.assertIn("a.py:10: **[major]** boom breaks", verdict)
        self.assertIn("b.py:3: **[BLOCKER]** kaboom", verdict)
        self.assertNotIn("minor", verdict)
        self.assertNotIn("d.py", verdict)

    def test_failure_exits_nonzero(self):
        # the verdict embeds finding text, so the exit code must not be
        # guessed from it: a finding mentioning GREEN still exits 1.
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(await_mod, "bot_review_state", return_value="failure"),
            patch.object(
                await_mod,
                "bot_findings",
                return_value=["a.py:1: **[major]** Do not report GREEN early"],
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--pr", ".", "42", "--timeout", "60"])
        self.assertEqual(ctx.exception.code, 1)

    def test_pending_runs_block_green(self):
        # one passed workflow plus one still running is not GREEN: the
        # wait continues and reports GREEN only once nothing is pending.
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(await_mod, "bot_review_state", return_value="success"),
            patch.object(
                await_mod, "commit_runs", side_effect=[(1, 0, 1, 0), (0, 0, 1, 0)]
            ),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            done, ok, verdict = await_mod.wait_pr(".", "42", time.time() + 60, 15.0)
        self.assertTrue(done and ok)
        self.assertIn("GREEN", verdict)

    def test_cancelled_waits_out_and_reports_distinctly(self):
        # a cancelled duplicate next to a running sibling is not FAILED:
        # the wait continues and reports GREEN once the sibling passes.
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(await_mod, "bot_review_state", return_value="success"),
            patch.object(
                await_mod, "commit_runs", side_effect=[(1, 0, 0, 1), (0, 0, 1, 0)]
            ),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            done, ok, verdict = await_mod.wait_pr(".", "42", time.time() + 60, 15.0)
        self.assertTrue(done and ok)
        self.assertIn("GREEN", verdict)
        # settled with only cancellations: neither GREEN nor FAILED.
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(await_mod, "bot_review_state", return_value="success"),
            patch.object(await_mod, "commit_runs", return_value=(0, 0, 1, 1)),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            done, ok, verdict = await_mod.wait_pr(".", "42", 60.0, 15.0)
        self.assertTrue(done)
        self.assertFalse(ok)
        self.assertIn("CANCELLED", verdict)
        self.assertNotIn("GREEN", verdict)

    def test_transient_gh_error_retries(self):
        # a rate-limited poll is retried inside the budget, never fatal.
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(
                await_mod,
                "bot_review_state",
                side_effect=[
                    SystemExit(
                        "gh api ... failed:\nAPI rate limit exceeded (HTTP 403)"
                    ),
                    "success",
                ],
            ),
            patch.object(await_mod, "commit_runs", return_value=(0, 0, 1, 0)),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            done, ok, verdict = await_mod.wait_pr(".", "42", time.time() + 60, 15.0)
        self.assertTrue(done and ok)
        self.assertIn("GREEN", verdict)

    def test_persistent_transient_error_is_still_pending(self):
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(await_mod, "pr_head_sha", return_value=self.SHA),
            patch.object(
                await_mod,
                "bot_review_state",
                side_effect=SystemExit("gh api ... failed:\nHTTP 503"),
            ),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--pr", ".", "42", "--timeout", "0"])
        self.assertEqual(ctx.exception.code, 75)

    def test_fatal_gh_error_still_raises(self):
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(
                await_mod,
                "pr_head_sha",
                side_effect=SystemExit("PR #42 is CONFLICTING: CI never starts."),
            ),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            await_mod.main(["--pr", ".", "42", "--timeout", "60"])
        self.assertIn("CONFLICTING", str(ctx.exception))

    def test_superseded_head_verdict_is_discarded(self):
        old, new = "a" * 40, "b" * 40
        with (
            patch.object(await_mod, "repo_slug", return_value="Blockfield/fake"),
            patch.object(
                await_mod, "pr_head_sha", side_effect=[old, old, new, new, new]
            ),
            patch.object(await_mod, "bot_review_state", return_value="success"),
            patch.object(await_mod, "commit_runs", return_value=(0, 0, 1, 0)),
            patch.object(await_mod.time, "sleep", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            done, ok, verdict = await_mod.wait_pr(".", "42", time.time() + 60, 15.0)
        self.assertTrue(done and ok)
        self.assertIn("GREEN", verdict)
        self.assertIn(new[:12], verdict)
        self.assertNotIn(old[:12], verdict)


class ShipResume(unittest.TestCase, FixtureRepos):
    """Timeout resume pushes nothing twice; the second run just waits."""

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

    def test_timeout_resume_pushes_once(self):
        with patch.dict(os.environ, {"FAKE_GH_BOT": "pending"}):
            code, out = self.ship("--timeout", "0")
        self.assertEqual(code, 75)
        self.assertIn("just pr-ship", out)
        pushed = git(self.origin, "rev-parse", "refs/heads/feature").stdout.strip()
        code, out = self.ship("--timeout", "60")
        self.assertEqual(code, 0)
        self.assertIn("no push needed", out)
        self.assertIn("GREEN", out)
        again = git(self.origin, "rev-parse", "refs/heads/feature").stdout.strip()
        self.assertEqual(pushed, again)


if __name__ == "__main__":
    unittest.main()
