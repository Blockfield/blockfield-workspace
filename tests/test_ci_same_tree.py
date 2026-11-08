"""Same-tree lookup for main-push CI skips (fake gh, no network)."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci-same-tree.sh"

# Fake `gh`: answers the API calls ci-same-tree.sh makes using COMMIT_TREE and
# ARTIFACT_NAMES from the environment, so no network is needed. PR_RUN_IDS /
# REVIEW_RUN_IDS override which run ids each event lookup returns (default: one
# pull_request run, no review runs).
FAKE_GH = """#!/bin/sh
args="$*"
case "$args" in
  *"/runs?status=success"*) printf 'abc123\\n' ;;
  *"/git/commits/"*) printf '%s\\n' "${COMMIT_TREE:-other}" ;;
  *"?event=pull_request_review"*) printf '%s' "${REVIEW_RUN_IDS:-}" ;;
  *"?event=pull_request"*) printf '%s' "${PR_RUN_IDS:-42\\n}" ;;
  *"/artifacts"*) printf '%s\\n' "${ARTIFACT_NAMES:-unrelated}" ;;
esac
"""


class SameTree(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        repo = self.tmp / "repo"
        repo.mkdir()
        self.run_cmd(["git", "init", "-q", str(repo)])
        (repo / "file.txt").write_text("content\n")
        self.run_cmd(["git", "-C", str(repo), "add", "file.txt"])
        self.run_cmd(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.email=t@localhost",
                "-c",
                "user.name=test",
                "commit",
                "-qm",
                "fixture",
            ]
        )
        self.repo = repo
        self.tree = self.run_cmd(
            ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"]
        ).strip()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        gh = bindir / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "Blockfield/blockfield-workspace",
        }

    def run_cmd(self, cmd):
        return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout

    def lookup(self, extra_env=None, *argv):
        env = dict(self.env)
        env.update(extra_env or {})
        proc = subprocess.run(
            ["sh", str(SCRIPT), "standards.yml", *argv],
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=env,
        )
        return proc.returncode, proc.stdout.strip()

    def test_head_tree_match_reports_tested(self):
        rc, out = self.lookup({"COMMIT_TREE": self.tree})
        self.assertEqual((rc, out), (0, "true"))

    def test_tested_tree_artifact_reports_tested(self):
        rc, out = self.lookup(
            {
                "COMMIT_TREE": "no-match",
                "ARTIFACT_NAMES": f"tested-tree-{self.tree}",
            }
        )
        self.assertEqual((rc, out), (0, "true"))

    def test_unknown_tree_reports_untested(self):
        rc, out = self.lookup({"COMMIT_TREE": "no-match"})
        self.assertEqual((rc, out), (0, "false"))

    def test_review_run_artifact_reports_tested(self):
        rc, out = self.lookup(
            {
                "COMMIT_TREE": "no-match",
                "PR_RUN_IDS": "",
                "REVIEW_RUN_IDS": "43\n",
                "ARTIFACT_NAMES": f"tested-tree-{self.tree}",
            }
        )
        self.assertEqual((rc, out), (0, "true"))

    def test_branch_filter_keeps_matching(self):
        rc, out = self.lookup({"COMMIT_TREE": self.tree}, "next")
        self.assertEqual((rc, out), (0, "true"))

    def test_missing_workflow_argument_fails_closed(self):
        proc = subprocess.run(
            ["sh", str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=self.repo,
            env=self.env,
        )
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
