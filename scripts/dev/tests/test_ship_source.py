"""Published history and explicit source identity regressions (offline Git repos)."""

import contextlib
import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_agent_friction as fixtures
from test_agent_friction import (
    FixtureRepos,
    agent_preflight,
    git,
    pr_ship,
)


class PublishedHistory(unittest.TestCase, FixtureRepos):
    setUp = fixtures.PrShip.setUp

    def feature(self, publish=True):
        git(self.repo, "checkout", "-qb", "feature")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-qm", "feature")
        if publish:
            git(self.repo, "push", "-q", "origin", "feature")
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def ship(self):
        with self.assertRaises(SystemExit) as ctx:
            pr_ship.main([str(self.repo), "feature"])
        return ctx.exception.code

    def test_published_merges_without_rewrite(self):
        old = self.feature()
        self.advance_origin()
        with patch.object(pr_ship, "run", wraps=pr_ship.run) as calls:
            self.assertEqual(self.ship(), 0)
        commands = [call.args[1:] for call in calls.call_args_list]
        self.assertTrue(any(c[:1] == ("merge",) for c in commands))
        self.assertFalse(any(c[:1] == ("rebase",) for c in commands))
        self.assertFalse(
            any("--force-with-lease" in c or "--force" in c for c in commands)
        )
        self.assertEqual(
            git(self.repo, "merge-base", "--is-ancestor", old, "HEAD").returncode, 0
        )

    def test_published_conflict_lists_files_and_does_not_push(self):
        self.feature()
        (self.repo / "file.txt").write_text("local\n")
        git(self.repo, "commit", "-qam", "local conflict")
        before = git(self.origin, "rev-parse", "feature").stdout
        self.advance_origin("remote")
        result = str(self.ship())
        self.assertIn('"status": "CONFLICT"', result)
        self.assertIn('"file.txt"', result)
        self.assertEqual(before, git(self.origin, "rev-parse", "feature").stdout)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")

    def test_diverged_remote_refused(self):
        self.feature()
        git(self.repo, "reset", "--hard", "origin/main")
        with patch.object(pr_ship, "run", wraps=pr_ship.run) as calls:
            self.assertIn("DIVERGED", str(self.ship()))
        self.assertFalse(
            any(c.args[1] in ("push", "rebase", "merge") for c in calls.call_args_list)
        )

    def test_unpublished_rebase_allowed(self):
        self.feature(False)
        self.advance_origin()
        with patch.object(
            pr_ship,
            "gh",
            return_value=type(
                "Result", (), {"returncode": 1, "stderr": "no pull requests found"}
            )(),
        ):
            with patch.object(pr_ship, "run", wraps=pr_ship.run) as calls:
                self.ship()
        self.assertTrue(any(c.args[1] == "rebase" for c in calls.call_args_list))


class PrBase(unittest.TestCase, FixtureRepos):
    """New PRs target the integration base; existing ones are retargeted (offline)."""

    def setUp(self):
        FixtureRepos.setUp(self)
        # unreleased work lives on origin/next; the task branch is cut from it.
        git(self.clone, "push", "-q", "origin", "HEAD:next")
        self.repo = self.worktree("unit")
        fixtures.configure(self.repo)
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "checkout", "-qb", "feature", "origin/next")
        (self.repo / "new.txt").write_text("new\n")
        git(self.repo, "add", "new.txt")
        git(self.repo, "commit", "-qm", "new")
        self.calls = []
        self.base = "main"
        self.have_pr = True

    def fake_gh(self, repo, *args):
        self.calls.append(args)
        if args[:2] == ("pr", "create"):
            self.base = args[args.index("--base") + 1]
            return SimpleNamespace(returncode=0, stdout="http://x/42\n", stderr="")
        if args[:2] == ("pr", "edit"):
            self.base = args[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ("pr", "view") and "statusCheckRollup" in args[-1]:
            sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "number": 42,
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "reviewDecision": "APPROVED",
                        "headRefOid": sha,
                        "baseRefName": self.base,
                        "url": "http://x/42",
                        "statusCheckRollup": [],
                    }
                ),
                stderr="",
            )
        if args[:2] == ("pr", "view"):
            if not self.have_pr:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="no pull requests found"
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"number": 42, "url": "http://x/42", "baseRefName": self.base}
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected gh call: {args}")

    def ship(self):
        out = io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            patch.object(pr_ship, "gh", side_effect=self.fake_gh),
            self.assertRaises(SystemExit) as ctx,
        ):
            pr_ship.main([str(self.repo), "feature"])
        return ctx.exception.code, out.getvalue()

    def test_create_targets_integration_base(self):
        self.have_pr = False
        code, out = self.ship()
        self.assertEqual(code, 0)
        creates = [c for c in self.calls if c[:2] == ("pr", "create")]
        self.assertEqual(len(creates), 1)
        self.assertIn("--base", creates[0])
        self.assertEqual(creates[0][creates[0].index("--base") + 1], "next")
        self.assertIn("base=next", out)

    def test_existing_pr_is_retargeted(self):
        code, out = self.ship()
        self.assertEqual(code, 0)
        edits = [c for c in self.calls if c[:2] == ("pr", "edit")]
        self.assertIn(("pr", "edit", "42", "--base", "next"), edits)
        self.assertIn("retargeted from main to next", out)
        self.assertIn("base=next", out)

    def test_existing_pr_on_base_is_left_alone(self):
        self.base = "next"
        code, _ = self.ship()
        self.assertEqual(code, 0)
        self.assertFalse([c for c in self.calls if c[:2] == ("pr", "edit")])


class SourceIdentity(unittest.TestCase):
    setUp = fixtures.PreflightPins.setUp

    def test_pin_behind_warns_without_checkout(self):
        os.chdir(self.wt)
        before = git(self.wt / "mod", "rev-parse", "HEAD").stdout
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agent_preflight.main(
                ["--mode", "upstream-main", "--source", "mod=origin/main"]
            )
        self.assertIn("WARN", output.getvalue())
        self.assertIn("behind=1", output.getvalue())
        self.assertIn("show origin/main:<path>", output.getvalue())
        self.assertEqual(before, git(self.wt / "mod", "rev-parse", "HEAD").stdout)

    def test_modes_label_sources_without_updating_pin(self):
        os.chdir(self.wt)
        for mode in ("pinned-candidate", "task-branch"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                agent_preflight.main(["--mode", mode, "--no-fetch"])
            self.assertIn(f"mode={mode}", output.getvalue())
            self.assertNotIn("WARN", output.getvalue())

    def test_master_default_branch_still_passes(self):
        git(self.sub_origin, "branch", "-m", "main", "master")
        sub = self.wt / "mod"
        git(sub, "fetch", "origin")
        git(
            sub,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/master",
        )
        git(sub, "update-ref", "-d", "refs/remotes/origin/main")
        os.chdir(self.wt)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agent_preflight.main([])
        self.assertIn("source=origin/master", output.getvalue())
        self.assertIn("show origin/master:<path>", output.getvalue())

    def test_bad_source_ref_refused(self):
        os.chdir(self.wt)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            agent_preflight.main(["--source", "mod=missing-ref"])
        self.assertIn("does not resolve", output.getvalue())
