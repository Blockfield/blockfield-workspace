import contextlib
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import release  # noqa: E402 - common helper lives alongside the dev directory.

PROD = "a" * 64
MOVED = "b" * 64


class Versions(unittest.TestCase):
    def test_pack_version_ignores_other_version_keys(self):
        text = 'name = "B"\nversion = "2.61.0"\n\n[versions]\nfabric = "0.19.3"\nminecraft = "1.21.1"\n'
        self.assertEqual(release.pack_version(text), "2.61.0")

    def test_semver_order_is_numeric(self):
        self.assertEqual(
            release.newest(["v2.9.0", "v2.10.0", "world-seed-v13", "v2.2.1"]), "v2.10.0"
        )
        self.assertIsNone(release.newest(["bf9", "v1.2"]))

    def test_next_version_skips_burnt_tags(self):
        self.assertEqual(release.next_version("v2.60.5", "2.60.4", "patch"), "2.60.6")
        self.assertEqual(release.next_version("v2.61.0", "2.62.0", "minor"), "2.63.0")
        self.assertEqual(release.next_version(None, "1.0.0", "patch"), "1.0.1")

    def test_tag_must_match_pack_and_be_new_and_greater(self):
        levels = lambda *args: [level for level, _ in release.check_tag(*args)]  # noqa: E731
        self.assertEqual(levels("v2.61.1", "2.61.1", {"v2.61.0"}), ["ok", "ok"])
        self.assertEqual(levels("v2.60.5", "2.60.4", {"v2.60.4"})[0], "FAIL")
        self.assertIn("FAIL", levels("v2.61.0", "2.61.0", {"v2.61.0"}))
        self.assertEqual(levels("v2.60.9", "2.60.9", {"v2.61.0"})[-1], "FAIL")
        self.assertEqual(levels("2.61.1", "2.61.1", set()), ["FAIL"])


class Plan(unittest.TestCase):
    def test_nothing_changed(self):
        self.assertEqual(release.plan({}, {}, PROD), ("none", []))

    def test_single_side_keeps_contract(self):
        kind, order = release.plan({"server": True}, {"server": PROD}, PROD)
        # Server-only is tag-only: the tag run deploys Game automatically,
        # so the dry-run text must not instruct a manual deploy dispatch.
        self.assertEqual((kind, order), ("server-only", [("tag", "server")]))
        kind, order = release.plan({"client": True}, {"client": PROD}, PROD)
        self.assertEqual((kind, order), ("client-only", [("tag", "client")]))

    def test_server_only_dry_run_has_no_manual_deploy(self):
        tags = {"server": "v2.79.4"}
        lines = release.tag_lines([("tag", "server")], tags, "release/server-v2.79.4")
        self.assertFalse(
            [line for line in lines if "dispatches" in line or "deploy.yml" in line],
            lines,
        )

    def test_independent_pair_tags_server_first(self):
        compat = {"client": PROD, "server": PROD}
        kind, order = release.plan({"client": True, "server": True}, compat, PROD)
        self.assertEqual(kind, "independent")
        self.assertEqual(order, [("tag", "server"), ("tag", "client")])

    def test_contract_change_on_both_sides_is_a_pair(self):
        compat = {"client": MOVED, "server": MOVED}
        kind, order = release.plan({"client": True, "server": True}, compat, PROD)
        self.assertEqual(kind, "pair")
        self.assertEqual(order[0], ("tag", "client"))
        self.assertEqual(order[-2:], [("deploy-pair", "server"), ("deploy", "client")])

    def test_contract_change_on_one_side_cannot_be_released(self):
        self.assertEqual(
            release.plan({"client": True}, {"client": MOVED}, PROD)[0], "mismatch"
        )
        compat = {"client": MOVED, "server": "c" * 64}
        self.assertEqual(
            release.plan({"client": True, "server": True}, compat, PROD)[0], "mismatch"
        )


class Safety(unittest.TestCase):
    def test_mutate_refuses_tags_and_deploys(self):
        for cmd in (
            ("git", "push", "origin", "v2.63.0"),
            ("git", "push", "--tags"),
            ("git", "push", "origin", "refs/tags/v1.0.0"),
            ("gh", "workflow", "run", "deploy.yml"),
            ("gh", "pr", "merge", "1"),
        ):
            with self.assertRaises(ValueError):
                release.mutate(True, *cmd)

    def test_dry_run_runs_nothing(self):
        self.assertEqual(
            release.mutate(
                True, "git", "push", "-u", "origin", "release/client-2.62.0"
            ),
            "(dry run)",
        )


class HeldIssuesTest(unittest.TestCase):
    def test_lists_all_pages_excludes_prs_and_reports_gh_failures(self):
        issues = [
            {"number": n, "title": f"task {n}", "html_url": f"https://example.com/{n}"}
            for n in range(1, 102)
        ]
        pr = {"number": 102, "pull_request": {}}
        ok = mock.Mock(
            returncode=0, stdout=json.dumps([issues[:100], [issues[100], pr]])
        )
        bad = mock.Mock(
            returncode=1, stdout="", stderr="ERROR: unknown flag: --slurp\n"
        )
        with mock.patch.object(release, "run", side_effect=[ok, bad]) as run:
            held = release.held_issues()
        self.assertEqual(len(held), 102)
        self.assertIn(
            "blockfield-client#101 task 101 https://example.com/101", held[-2]
        )
        self.assertIn("blockfield-server: could not list", held[-1])
        self.assertIn("unknown flag: --slurp", held[-1])
        for call in run.call_args_list:
            self.assertIn("--paginate", call.args)
            self.assertIn("--slurp", call.args)

    def test_empty_release_queue_prints_nothing(self):
        empty = mock.Mock(returncode=0, stdout="[[]]")
        with (
            mock.patch.object(release, "run", return_value=empty),
            mock.patch.object(release, "say") as say,
        ):
            release.say_held()
        say.assert_not_called()


class Ancestor(unittest.TestCase):
    def check(self, code, stderr=""):
        done = mock.Mock(returncode=code, stdout="", stderr=stderr)
        with mock.patch.object(release, "run", return_value=done):
            return release.is_ancestor("repo", "a" * 40, "b" * 40)

    def test_descendant_passes(self):
        self.assertTrue(self.check(0))

    def test_not_ancestor_skips(self):
        self.assertFalse(self.check(1))

    def test_git_failure_raises_instead_of_skipping(self):
        with self.assertRaises(RuntimeError):
            self.check(128, "fatal: bad object")


class PinOverrides(unittest.TestCase):
    def run_pin(self, pinned, server_file=""):
        root = self.root = Path(tempfile.mkdtemp(prefix="bf-release-pin-"))
        self.addCleanup(shutil.rmtree, root, True)
        for path in ("client", "server", "localmods/blockfield-mod", "pins"):
            (root / path / ".git").mkdir(parents=True)
        shas = {
            "v2.69.1": "c" * 40,
            "v2.69.2": "d" * 40,
            "v1.47.1": "e" * 40,
            "v1.47.2": "f" * 40,
        }

        def fake_git(repo, *args, **kw):
            if "ls-tree" in args:
                path = args[-1]
                sha = pinned.get(path, "0" * 40)
                return f"160000 commit {sha}\t{path}" if sha else ""
            if "show" in args:
                # git show fails outright when base has no pins/server.sha.
                return mock.Mock(
                    returncode=0 if server_file else 128, stdout=server_file
                )
            return ""

        import contextlib

        args = SimpleNamespace(
            client="v2.69.1", server="v2.69.2", base="main", dry_run=True
        )
        said = []
        with (
            mock.patch.object(release, "ROOT", root),
            mock.patch.object(
                release,
                "prod_state",
                return_value={"build": "v2.69.1", "server_build": "v2.69.2"},
            ),
            mock.patch.object(release, "git", side_effect=fake_git),
            mock.patch.object(release, "run", return_value=mock.Mock(returncode=0)),
            mock.patch.object(
                release, "tag_commit", side_effect=lambda repo, tag: shas[tag]
            ),
            mock.patch.object(
                release,
                "pins",
                side_effect=lambda repo, tag: (
                    [("mod", "Blockfield/blockfield-mod", "v1.47.1")]
                    if repo == root / "client"
                    else [("mod", "Blockfield/blockfield-mod", "v1.47.2")]
                ),
            ),
            mock.patch.object(
                release, "say", side_effect=lambda text: said.append(text)
            ),
            mock.patch.object(
                release, "worktree", return_value=contextlib.nullcontext(root)
            ),
            mock.patch.object(release, "mutate", return_value=""),
        ):
            self.assertTrue(release.cmd_pin(args))
        return said

    def test_newer_server_mod_pin_warns_when_it_overrides_the_client_one(self):
        said = self.run_pin({"client": "a" * 40, "server": "b" * 40})
        self.assertTrue(
            any(
                "localmods/blockfield-mod" in line
                and "mod 1.47.2 overrides mod 1.47.1" in line
                for line in said
            ),
            said,
        )

    def test_server_pin_file_advances(self):
        # The public workspace pins the private server in pins/server.sha;
        # pinning used to crash, then skip it, leaving the file stale.
        said = self.run_pin(
            {"client": "a" * 40, "server": None}, server_file="b" * 40 + "\n"
        )
        self.assertEqual(
            (self.root / "pins/server.sha").read_text(encoding="utf-8"), "d" * 40 + "\n"
        )
        self.assertTrue(any("server 2.69.2" in line for line in said), said)

    def test_unpinned_server_is_skipped(self):
        said = self.run_pin({"client": "a" * 40, "server": None})
        self.assertIn("skip server: the workspace does not pin it", said)


class Ship(unittest.TestCase):
    def ship_args(self, **kw):
        base = dict(
            mod="v1.54.0",
            decor=None,
            bump="patch",
            only=None,
            dry_run=False,
            confirmed_by_owner=True,
            timeout=60,
            interval=0,
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_refuses_without_owner_confirmation(self):
        with self.assertRaises(SystemExit):
            release.cmd_ship(self.ship_args(confirmed_by_owner=False))

    def test_client_only_still_requires_full_tag_run_success(self):
        calls = []
        tags = {"client": "v2.77.0"}
        patches = self.base_patches(
            calls, (tags, "client-only", [("tag", "client")], "")
        )
        self.assertTrue(
            self.run_patches(
                patches, lambda: release.cmd_ship(self.ship_args(only="client"))
            )
        )
        self.assertIn(("watched", "client"), calls)
        self.assertFalse(any(c[0] == "published" for c in calls))
        # Probes wait for /api/rooms to serve the tag, which only Deploy client does.
        self.assertEqual([c[1] for c in calls if c[0] == "deploy"], ["client"])
        kinds = [c[0] for c in calls]
        self.assertLess(kinds.index("watched"), kinds.index("deploy"))
        self.assertLess(kinds.index("deploy"), kinds.index("probes"))

    def test_server_only_ships_game_without_workshop(self):
        calls = []
        tags = {"server": "v2.77.0"}
        patches = self.base_patches(
            calls, (tags, "server-only", [("tag", "server")], "")
        )
        self.assertTrue(
            self.run_patches(
                patches, lambda: release.cmd_ship(self.ship_args(only="server"))
            )
        )
        staged = [
            c
            for c in calls
            if c[0] in ("tag", "run", "published", "watched", "deploy", "probes", "pin")
        ]
        # A plain tag, then the automatic tag-run deploys Game on its own:
        # no Workshop staging (published), no explicit Workshop move
        # (approve), no manual Game deploy.
        self.assertEqual(
            staged,
            [
                ("tag", "server"),
                ("run", "server"),
                ("watched", "server"),
                ("probes", "server"),
                ("pin",),
            ],
        )
        self.assertFalse(any(c[0] == "published" for c in calls))
        self.assertFalse(any(c[0] == "approve" for c in calls))
        self.assertFalse(any(c[0] == "deploy" for c in calls))
        self.assertIn(("ff", "server"), calls)

    def test_wait_until_returns_first_truthy_and_times_out(self):
        self.assertEqual(release.wait_until("x", 60, 0, lambda: "done"), "done")
        with self.assertRaises(RuntimeError):
            release.wait_until("x", 0.05, 0.01, lambda: None)

    def test_pr_checks_green_red_pending(self):
        def fake_gh(*args):
            if "headRefOid,statusCheckRollup" in args:
                return json.dumps({"statusCheckRollup": self.rollup})
            raise AssertionError(args)

        with mock.patch.object(release, "gh", side_effect=fake_gh):
            self.rollup = [
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "COMPLETED", "conclusion": "SKIPPED"},
            ]
            self.assertEqual(release.pr_checks("R", 1), "green")
            self.rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
            self.assertEqual(release.pr_checks("R", 1), "red")
            self.rollup = [
                {"status": "COMPLETED", "conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS", "conclusion": None},
            ]
            self.assertEqual(release.pr_checks("R", 1), "pending")
            self.rollup = []
            self.assertEqual(release.pr_checks("R", 1), "pending")

    def test_pr_checks_status_context_states(self):
        for state, expected in (
            ("SUCCESS", "green"),
            ("FAILURE", "red"),
            ("ERROR", "red"),
            ("PENDING", "pending"),
            ("EXPECTED", "pending"),
        ):
            rollup = [
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                },
                {
                    "__typename": "StatusContext",
                    "context": "bot-review",
                    "state": state,
                },
            ]
            with (
                self.subTest(state=state),
                mock.patch.object(
                    release,
                    "gh",
                    return_value=json.dumps({"statusCheckRollup": rollup}),
                ),
            ):
                self.assertEqual(release.pr_checks("R", 1), expected)
                self.assertEqual(
                    release.pr_checks("R", 1, required_jobs=("tests", "bot-review")),
                    expected,
                )
                if state == "SUCCESS":
                    self.assertEqual(
                        release.pr_checks("R", 1, required_jobs=("missing",)), "pending"
                    )

    def test_pr_checks_required_job_latest_skip_is_not_green(self):
        # Bot-gate shape after the job-level `if:` fix: a COMMENTED review
        # leaves a SKIPPED run, an APPROVED review a SUCCESS run, both on the
        # same head SHA. A skip is never green, and a later skip must not mask
        # an earlier green one.
        def fake_gh(*args):
            if "headRefOid,statusCheckRollup" in args:
                return json.dumps({"statusCheckRollup": self.rollup})
            raise AssertionError(args)

        with mock.patch.object(release, "gh", side_effect=fake_gh):
            self.rollup = [
                {
                    "__typename": "CheckRun",
                    "name": "check",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "startedAt": "2026-11-03T14:45:35Z",
                    "completedAt": "2026-11-03T14:46:00Z",
                },
                {
                    "__typename": "CheckRun",
                    "name": "check",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                    "startedAt": "2026-11-03T14:47:00Z",
                    "completedAt": "2026-11-03T14:47:05Z",
                },
            ]
            self.assertEqual(
                release.pr_checks("R", 1, required_jobs=("check",)), "green"
            )
            self.rollup = [
                {
                    "__typename": "CheckRun",
                    "name": "check",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                    "startedAt": "2026-11-03T14:47:00Z",
                    "completedAt": "2026-11-03T14:47:05Z",
                },
            ]
            self.assertEqual(
                release.pr_checks("R", 1, required_jobs=("check",)), "pending"
            )
            # No required jobs: an all-skipped comment-review run is not green.
            self.assertEqual(release.pr_checks("R", 1), "pending")
            # A successful commit status (e.g. bot-review) with no successful
            # CI check run is not green either: statuses never prove CI ran.
            self.rollup = [
                {
                    "__typename": "StatusContext",
                    "context": "bot-review",
                    "state": "SUCCESS",
                },
                {
                    "__typename": "CheckRun",
                    "name": "check",
                    "status": "COMPLETED",
                    "conclusion": "SKIPPED",
                    "startedAt": "2026-11-03T14:47:00Z",
                    "completedAt": "2026-11-03T14:47:05Z",
                },
            ]
            self.assertEqual(release.pr_checks("R", 1), "pending")

    def test_ship_polls_every_five_seconds_by_default(self):
        args = release.parser().parse_args(["ship", "--mod", "v1.55.3"])
        self.assertEqual(args.interval, 5)

    def test_prepare_both_sides_before_client_publication(self):
        calls = []
        tags = {side: "v2.78.0" for side in release.SIDES}
        patches = self.base_patches(calls, (tags, "pair", [], ""))[1:]

        def prepare(side, args, prod, manifest, repin):
            calls.append(("prepare", side))
            if side == "server":
                self.assertIsNotNone(manifest)
                self.assertFalse(repin)
            return tags[side], {"compatibility": MOVED}

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(release, "CACHE", Path(tmp)),
            mock.patch.object(
                release, "prod_state", return_value={"compatibility": PROD}
            ),
            mock.patch.object(release, "say_held"),
            mock.patch.object(release, "prepare_side", side_effect=prepare),
            mock.patch.object(release, "cmd_candidate", return_value="candidate-sha"),
        ):
            self.assertTrue(
                self.run_patches(
                    patches, lambda: release.cmd_ship(self.ship_args(server_merge=None))
                )
            )
        self.assertEqual(calls[:2], [("prepare", "client"), ("prepare", "server")])
        self.assertLess(
            calls.index(("prepare", "server")), calls.index(("tag", "client"))
        )
        self.assertIn(("green", release.REPO["server"], "candidate-sha"), calls)

    def test_server_image_must_succeed_on_final_head_before_merge(self):
        head = "a" * 40
        for conclusion in (
            "SUCCESS",
            "FAILURE",
            "SKIPPED",
            "NEUTRAL",
            None,
            "missing",
            "old-head",
        ):
            data = {
                "headRefOid": "b" * 40 if conclusion == "old-head" else head,
                "statusCheckRollup": [
                    {"name": "check", "status": "COMPLETED", "conclusion": "SUCCESS"},
                ],
            }
            if conclusion != "missing":
                data["statusCheckRollup"].append(
                    {
                        "name": "image",
                        "status": "IN_PROGRESS" if conclusion is None else "COMPLETED",
                        "conclusion": "SUCCESS"
                        if conclusion == "old-head"
                        else conclusion,
                    }
                )
            with (
                self.subTest(conclusion=conclusion),
                mock.patch.object(release, "pr_number", return_value=7),
                mock.patch.object(release, "gh", return_value=json.dumps(data)),
                mock.patch.object(release, "merge_pr") as merge,
            ):
                if conclusion == "SUCCESS":
                    self.assertEqual(
                        release.wait_pr_green(
                            release.REPO["server"], "release/server-2.78.0", 0, 0, head
                        ),
                        (7, head),
                    )
                else:
                    with self.assertRaises(RuntimeError):
                        release.ship_tag_side(
                            self.ship_args(timeout=0),
                            "server",
                            "v2.78.0",
                            0,
                            0,
                            expected_head=head,
                        )
                merge.assert_not_called()

    def test_mod_published(self):
        ok = mock.Mock(returncode=0, stdout="v1.54.0", stderr="")
        missing = mock.Mock(returncode=1, stdout="", stderr="release not found")
        broken = mock.Mock(returncode=1, stdout="", stderr="HTTP 500: boom")
        with mock.patch.object(release, "run", return_value=ok):
            self.assertTrue(release.mod_published("v1.54.0"))
        with mock.patch.object(release, "run", return_value=missing):
            self.assertFalse(release.mod_published("v9.9.9"))
        with mock.patch.object(release, "run", return_value=broken):
            with self.assertRaises(RuntimeError):
                release.mod_published("v1.54.0")

    def base_patches(self, calls, prepared):
        shas = {"server": "s" * 40, "client": "c" * 40}
        runs = {
            "server": {"databaseId": 11, "url": "https://example.com/s"},
            "client": {"databaseId": 12, "url": "https://example.com/c"},
        }
        return (
            mock.patch.object(release, "prepare_all", return_value=prepared),
            mock.patch.object(release, "say"),
            mock.patch.object(release, "mod_published", return_value=True),
            mock.patch.object(
                release,
                "wait_pr_green",
                side_effect=lambda repo, branch, t, i, head=None: (
                    calls.append(("green", repo, *([head] if head else [])))
                    or (7, head or "a" * 40)
                ),
            ),
            mock.patch.object(
                release,
                "merge_pr",
                side_effect=lambda repo, n, dry, head=None: (
                    calls.append(("merge", repo, *([head] if head else [])))
                    or "merged-sha"
                ),
            ),
            mock.patch.object(
                release,
                "push_tag",
                side_effect=lambda side, tag, dry, expected_sha=None, *a, **k: (
                    calls.append(("tag", side)) or shas[side]
                ),
            ),
            mock.patch.object(
                release,
                "wait_release_run",
                side_effect=lambda side, sha, t, i, tag: (
                    self.assertEqual(tag, prepared[0][side])
                    or calls.append(("run", side))
                    or runs[side]
                ),
            ),
            mock.patch.object(
                release,
                "wait_deploy_started",
                side_effect=lambda side, rid, t, i: calls.append(("gate", side)),
            ),
            mock.patch.object(
                release,
                "wait_run_success",
                side_effect=lambda side, rid, t, i: calls.append(("watched", side)),
            ),
            mock.patch.object(
                release,
                "cmd_pin",
                side_effect=lambda args: calls.append(("pin",)) or "release/pins-x",
            ),
            mock.patch.object(
                release,
                "wait_publication",
                side_effect=lambda side, tag, rid, t, i: (
                    calls.append(("published", side))
                    or {"client": "digest", "compatibility": MOVED}
                ),
            ),
            mock.patch.object(
                release,
                "dispatch_deploy",
                side_effect=lambda side, tag, t, i, **kw: calls.append(
                    ("deploy", side, kw)
                ),
            ),
            mock.patch.object(
                release,
                "check_client_release",
                side_effect=lambda tag, pre, **kw: calls.append(
                    ("prerelease", tag, pre)
                ),
            ),
            mock.patch.object(
                release,
                "verify_pair",
                side_effect=lambda *args: calls.append(("verify",)),
            ),
            mock.patch.object(
                release,
                "require_rc_gate",
                side_effect=lambda tags, *a, **k: calls.append(("rc-gate",)) or True,
            ),
            mock.patch.object(
                release,
                "post_deploy_probes",
                side_effect=lambda tags, *a, **k: (
                    calls.append(("probes", next(iter(tags)))) or True
                ),
            ),
            mock.patch.object(
                release,
                "ff_next_to_main",
                side_effect=lambda side, dry: calls.append(("ff", side)) or True,
            ),
        )

    def run_patches(self, patches, func):
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return func()

    def test_independent_gates_client_tag_on_server_deploy(self):
        calls = []
        tags = {"server": "v2.76.3", "client": "v2.76.3"}
        patches = self.base_patches(
            calls, (tags, "independent", [("tag", "server"), ("tag", "client")], "")
        )
        self.assertTrue(
            self.run_patches(patches, lambda: release.cmd_ship(self.ship_args()))
        )
        kinds = [c[0] for c in calls]
        self.assertEqual(
            kinds,
            [
                "ff",
                "ff",
                "rc-gate",
                "green",
                "merge",
                "ff",
                "tag",
                "green",
                "merge",
                "ff",
                "run",
                "gate",
                "tag",
                "watched",
                "run",
                "watched",
                "deploy",
                "probes",
                "pin",
                "green",
                "merge",
                "ff",
                "ff",
            ],
        )
        # The client tag run is a publish-only stub: serving needs the dispatch.
        self.assertEqual([c[1] for c in calls if c[0] == "deploy"], ["client"])
        tagged = [c[1] for c in calls if c[0] == "tag"]
        self.assertEqual(tagged, ["server", "client"])
        gate = kinds.index("gate")
        first_tag = kinds.index("tag")
        self.assertLess(first_tag, gate)
        self.assertLess(gate, kinds.index("tag", first_tag + 1))

    def test_pair_tags_client_first_then_candidate_then_server(self):
        calls = []
        tags = {"client": "v2.76.3", "server": "v2.76.3"}
        order = [
            ("tag", "client"),
            ("candidate", "server"),
            ("tag", "server"),
            ("deploy-pair", "server"),
            ("deploy", "client"),
        ]
        patches = self.base_patches(calls, (tags, "pair", order, ""))
        candidate = mock.patch.object(
            release,
            "cmd_candidate",
            side_effect=lambda args: (
                calls.append(("candidate",))
                or (self.assertEqual(args.tag, "v2.76.3") or "candidate-sha")
            ),
        )
        with candidate:
            self.assertTrue(
                self.run_patches(patches, lambda: release.cmd_ship(self.ship_args()))
            )
        self.assertEqual(
            calls,
            [
                ("ff", "client"),
                ("ff", "server"),
                ("rc-gate",),
                ("green", release.REPO["client"]),
                ("merge", release.REPO["client"], "a" * 40),
                ("ff", "client"),
                ("tag", "client"),
                ("run", "client"),
                ("published", "client"),
                ("candidate",),
                ("green", release.REPO["server"], "candidate-sha"),
                ("merge", release.REPO["server"], "candidate-sha"),
                ("ff", "server"),
                ("tag", "server"),
                ("run", "server"),
                ("published", "server"),
                ("prerelease", tags["client"], True),
                ("deploy", "server", {"client_tag": tags["client"], "dry": False}),
                ("probes", "server"),
                ("deploy", "client", {"dry": False}),
                ("probes", "client"),
                ("verify",),
                ("pin",),
                ("green", release.WORKSPACE_REPO),
                ("merge", release.WORKSPACE_REPO, "a" * 40),
                ("ff", "client"),
                ("ff", "server"),
            ],
        )

    def test_merge_then_failure_still_ffs_and_rerun_heals_first(self):
        calls = []
        tags = {"server": "v2.77.0"}
        prepared = (tags, "server-only", [("tag", "server")], "")
        # First run: the release PR merges, then tagging dies. The merge
        # already moved next, and nothing after it ran.
        with contextlib.ExitStack() as stack:
            for patch in self.base_patches(calls, prepared):
                stack.enter_context(patch)
            stack.enter_context(
                mock.patch.object(
                    release, "push_tag", side_effect=RuntimeError("tag failed")
                )
            )
            with self.assertRaisesRegex(RuntimeError, "tag failed"):
                release.cmd_ship(self.ship_args())
        merge_at = calls.index(("merge", release.REPO["server"], "a" * 40))
        self.assertIn(("ff", "server"), calls[merge_at + 1 :])
        self.assertNotIn(("pin",), calls)
        # Rerun: the start-of-ship resync heals next before anything else.
        calls.clear()
        self.assertTrue(
            self.run_patches(
                self.base_patches(calls, prepared),
                lambda: release.cmd_ship(self.ship_args()),
            )
        )
        self.assertEqual(calls[:2], [("ff", "client"), ("ff", "server")])

    def test_dry_run_prepares_but_mutates_nothing(self):
        prepared = ({"server": "v2.76.3"}, "server-only", [("tag", "server")], "")
        with (
            mock.patch.object(release, "prepare_all", return_value=prepared),
            mock.patch.object(release, "say"),
            mock.patch.object(release, "mod_published", return_value=True),
            mock.patch.object(
                release, "merge_pr", side_effect=AssertionError("must not merge")
            ),
            mock.patch.object(
                release, "push_tag", side_effect=AssertionError("must not tag")
            ),
            mock.patch.object(
                release, "cmd_pin", side_effect=AssertionError("must not pin")
            ),
        ):
            self.assertTrue(release.cmd_ship(self.ship_args(dry_run=True)))

    def test_red_checks_stop_before_any_tag(self):
        prepared = ({"server": "v2.76.3"}, "server-only", [("tag", "server")], "")
        with (
            mock.patch.object(release, "prepare_all", return_value=prepared),
            mock.patch.object(release, "say"),
            mock.patch.object(release, "mod_published", return_value=True),
            mock.patch.object(release, "ff_next_to_main", return_value=True),
            mock.patch.object(release, "require_rc_gate", return_value=True),
            mock.patch.object(
                release,
                "wait_pr_green",
                side_effect=RuntimeError("checks are red on R PR #1"),
            ),
            mock.patch.object(
                release, "push_tag", side_effect=AssertionError("must not tag")
            ),
        ):
            with self.assertRaises(RuntimeError):
                release.cmd_ship(self.ship_args())

    def test_pair_stops_at_each_failed_gate_without_retry(self):
        tags = {s: "v2.77.0" for s in release.SIDES}
        for name in (
            "wait_publication",
            "cmd_candidate",
            "dispatch_deploy",
            "check_client_release",
            "verify_pair",
        ):
            with self.subTest(gate=name):
                calls = []
                patches = self.base_patches(
                    calls, (tags, "pair", [], "release/server-2.77.0")
                )
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    stack.enter_context(
                        mock.patch.object(release, "cmd_candidate", return_value=True)
                    )
                    failed = stack.enter_context(
                        mock.patch.object(
                            release, name, side_effect=RuntimeError("gate failed")
                        )
                    )
                    with self.assertRaisesRegex(RuntimeError, "gate failed"):
                        release.cmd_ship(self.ship_args())
                    self.assertEqual(failed.call_count, 1)
                self.assertNotIn(("pin",), calls)
                if name in ("wait_publication", "cmd_candidate"):
                    self.assertNotIn(("tag", "server"), calls)
                if name == "dispatch_deploy":
                    self.assertNotIn(("verify",), calls)

    def test_pair_dry_run_prints_full_plan_without_confirmation_or_mutations(self):
        tags = {s: "v2.77.0" for s in release.SIDES}
        _, order = release.plan(
            dict.fromkeys(release.SIDES, True),
            dict.fromkeys(release.SIDES, MOVED),
            PROD,
        )
        said = []
        with (
            mock.patch.object(release, "mod_published", return_value=True),
            mock.patch.object(
                release,
                "prepare_all",
                return_value=(tags, "pair", order, "release/server-2.77.0"),
            ),
            mock.patch.object(release, "say", side_effect=said.append),
            mock.patch.object(
                release, "ship_mutate", side_effect=AssertionError("no mutations")
            ),
            mock.patch.object(
                release, "ship_tag_side", side_effect=AssertionError("no tag")
            ),
            mock.patch.object(
                release, "cmd_candidate", side_effect=AssertionError("no candidate")
            ),
            mock.patch.object(
                release, "dispatch_deploy", side_effect=AssertionError("no dispatch")
            ),
            mock.patch.object(release, "cmd_pin", side_effect=AssertionError("no pin")),
        ):
            self.assertTrue(
                release.cmd_ship(self.ship_args(dry_run=True, confirmed_by_owner=False))
            )
        text = "\n".join(said)
        for expected in (
            "--branch release/server-2.77.0",
            "check (includes publish)",
            "prepare + release",
            "inputs[client_tag]=v2.77.0",
            "new dispatch run to succeed",
            "verify public",
            "green workspace pin-PR",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("move-approved-tag", text)

    def test_server_only_dry_run_prints_game_only_plan(self):
        tags = {"server": "v2.77.0"}
        _, order = release.plan({"server": True}, {"server": PROD}, PROD)
        said = []
        with (
            mock.patch.object(release, "mod_published", return_value=True),
            mock.patch.object(
                release,
                "prepare_all",
                return_value=(tags, "server-only", order, "release/server-2.77.0"),
            ),
            mock.patch.object(release, "say", side_effect=said.append),
            mock.patch.object(
                release, "ship_mutate", side_effect=AssertionError("no mutations")
            ),
            mock.patch.object(
                release, "ship_tag_side", side_effect=AssertionError("no tag")
            ),
            mock.patch.object(
                release, "dispatch_deploy", side_effect=AssertionError("no dispatch")
            ),
            mock.patch.object(release, "cmd_pin", side_effect=AssertionError("no pin")),
        ):
            self.assertTrue(
                release.cmd_ship(self.ship_args(dry_run=True, confirmed_by_owner=False))
            )
        text = "\n".join(said)
        # Game ships alone: tag, then the automatic tag-run Deploy (no manual
        # dispatch: real ship never runs deploy.yml for server-only); the
        # Workshop is never staged, moved or mentioned.
        self.assertNotIn("deploy.yml/dispatches", text)
        self.assertIn("automatic Deploy ships", text)
        self.assertIn("no manual dispatch", text)
        self.assertIn("green workspace pin-PR", text)
        self.assertNotIn("workshop", text.lower())
        self.assertNotIn("move-approved-tag", text)

    def test_publication_accepts_expected_compatibility_refusal(self):
        # A server-only tag run whose automatic Deploy fails closed at the
        # client-gate step still publishes: Game ships without the Workshop.
        jobs = [
            {"name": "prepare", "conclusion": "success"},
            {"name": "release", "conclusion": "success"},
            {
                "name": "deploy / deploy",
                "conclusion": "failure",
                "steps": [
                    {
                        "name": "Validate target and remember previous pair",
                        "conclusion": "failure",
                    }
                ],
            },
        ]
        data = {
            "jobs": jobs,
            "status": "completed",
            "conclusion": "failure",
            "url": "run-url",
        }
        with (
            mock.patch.object(
                release,
                "gh",
                side_effect=lambda *a: (
                    "log Server gates on another client than the served one: "
                    "rerun Deploy server with client_tag"
                    if "--log-failed" in a
                    else json.dumps(data)
                ),
            ),
            mock.patch.object(
                release, "verify_publication", return_value="manifest"
            ) as verify,
        ):
            self.assertEqual(
                release.wait_publication("server", "v2.77.0", 1, 1, 0),
                "manifest",
            )
            verify.assert_called_once_with("server", "v2.77.0")
        with (
            mock.patch.object(
                release,
                "gh",
                side_effect=lambda *a: (
                    "log some other failure"
                    if "--log-failed" in a
                    else json.dumps(data)
                ),
            ),
            mock.patch.object(release, "verify_publication") as verify,
        ):
            with self.assertRaises(RuntimeError):
                release.wait_publication("server", "v2.77.0", 1, 1, 0)
            verify.assert_not_called()


class PairGates(unittest.TestCase):
    def test_client_release_visibility(self):
        tag = "v2.77.0"
        staged = {"tag_name": tag, "draft": False, "prerelease": True}
        promoted = staged | {"prerelease": False}
        with mock.patch.object(release, "gh", return_value=json.dumps(staged)):
            self.assertEqual(release.check_client_release(tag, True), staged)
            with self.assertRaisesRegex(RuntimeError, "prerelease=False"):
                release.check_client_release(tag, False, latest=True)
        with mock.patch.object(
            release, "gh", side_effect=[json.dumps(promoted), json.dumps(promoted)]
        ):
            self.assertEqual(
                release.check_client_release(tag, False, latest=True), promoted
            )
        for latest in (
            promoted | {"tag_name": "v2.76.1"},
            staged,
            promoted | {"draft": True},
        ):
            with mock.patch.object(
                release, "gh", side_effect=[json.dumps(promoted), json.dumps(latest)]
            ):
                with self.assertRaisesRegex(RuntimeError, "Latest"):
                    release.check_client_release(tag, False, latest=True)
        for invalid in (
            promoted,
            staged | {"draft": True},
            staged | {"tag_name": "other"},
            {},
        ):
            with mock.patch.object(release, "gh", return_value=json.dumps(invalid)):
                with self.assertRaises(RuntimeError):
                    release.check_client_release(tag, True)

    def test_client_refusal_and_stage_failure_are_fatal(self):
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", ""):
            data = {
                "status": "completed",
                "conclusion": "failure" if conclusion == "failure" else "success",
                "url": "url",
                "jobs": [
                    {"name": "check", "conclusion": "success"},
                    {
                        "name": "deploy / deploy",
                        "conclusion": conclusion,
                        "steps": [
                            {
                                "name": "Verify compatibility and deploy client",
                                "conclusion": conclusion,
                            }
                        ],
                    },
                ],
            }
            with (
                mock.patch.object(release, "gh", return_value=json.dumps(data)) as gh,
                mock.patch.object(release, "verify_publication") as verify,
            ):
                with self.assertRaisesRegex(RuntimeError, "must stage successfully"):
                    release.wait_publication("client", "v2.77.0", 12, 1, 0)
                self.assertEqual(gh.call_count, 1)
                verify.assert_not_called()

    def test_merge_rejects_changed_head(self):
        head = "a" * 40
        for data in (
            {
                "state": "MERGED",
                "headRefOid": "changed",
                "mergeCommit": {"oid": "b" * 40},
            },
            {"state": "OPEN", "headRefOid": head},
            {"state": "MERGED", "headRefOid": head, "mergeCommit": None},
        ):
            with (
                mock.patch.object(release, "ship_mutate") as merge,
                mock.patch.object(release, "gh", return_value=json.dumps(data)),
            ):
                with self.assertRaisesRegex(RuntimeError, "nothing was tagged"):
                    release.merge_pr("repo", 7, False, head)
                self.assertEqual(merge.call_count, 1)

    def test_green_merge_tag(self):
        head, merged = "a" * 40, "b" * 40
        pair = {"client": "v2.77.0", "server": "v2.77.1"}

        def gh(*args):
            if "headRefOid" in args:
                return json.dumps({"headRefOid": head})
            if "headRefOid,statusCheckRollup" in args:
                return json.dumps(
                    {
                        "headRefOid": head,
                        "statusCheckRollup": [
                            {"status": "COMPLETED", "conclusion": "SUCCESS"}
                        ],
                    }
                )
            if "headRefOid,mergeCommit,state" in args:
                return json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": head,
                        "mergeCommit": {"oid": merged},
                    }
                )
            raise AssertionError(args)

        with (
            mock.patch.object(release, "gh", side_effect=gh),
            mock.patch.object(release, "pr_number", return_value=7),
            mock.patch.object(release, "ship_mutate") as merge,
            mock.patch.object(release, "push_tag", return_value=merged) as tag,
            mock.patch.object(
                release, "wait_release_run", return_value={"databaseId": 12}
            ),
            mock.patch.object(release, "wait_publication"),
            mock.patch.object(release, "ff_next_to_main", return_value=True),
        ):
            release.ship_tag_side(
                SimpleNamespace(dry_run=False),
                "client",
                "v2.77.0",
                1,
                0,
                publish_only=True,
                release_tags=pair,
            )
        self.assertEqual(merge.call_args.args[-2:], ("--match-head-commit", head))
        tag.assert_called_once_with(
            "client", "v2.77.0", False, merged, None, None, pair
        )

    def test_next_ahead_of_main_is_left_alone(self):
        def ancestor(repo, older, newer):
            return (older, newer) == ("m" * 40, "n" * 40)

        with (
            mock.patch.object(release, "repo_dir", return_value=Path("repo")),
            mock.patch.object(release, "has_next", return_value=True),
            mock.patch.object(release, "fetch_main"),
            mock.patch.object(
                release,
                "git",
                side_effect=lambda repo, *a: (
                    "m" * 40 if a[-1] == "origin/main" else "n" * 40
                ),
            ),
            mock.patch.object(release, "is_ancestor", side_effect=ancestor),
            mock.patch.object(release, "ship_mutate") as push,
        ):
            self.assertTrue(release.ff_next_to_main("client", False))
        push.assert_not_called()

    def test_tag_rejects_unrelated_main(self):
        with (
            mock.patch.object(release, "repo_dir", return_value=Path("repo")),
            mock.patch.object(release, "git", return_value="c" * 40) as git,
            mock.patch.object(release, "preflight") as preflight,
        ):
            with self.assertRaisesRegex(RuntimeError, "differs from the merged commit"):
                release.push_tag(
                    "client", "v2.77.0", False, "b" * 40, gate_waiver="test"
                )
        preflight.assert_not_called()
        self.assertFalse(any(c.args[1] in ("tag", "push") for c in git.call_args_list))

    def test_dry_helpers(self):
        with (
            mock.patch.object(
                release, "run", side_effect=AssertionError("no dispatch")
            ),
            mock.patch.object(
                release, "wait_run_success", side_effect=AssertionError("no wait")
            ),
            mock.patch.object(
                release, "wait_release_run", side_effect=AssertionError("no run lookup")
            ),
            mock.patch.object(
                release, "wait_pr_green", side_effect=AssertionError("no PR wait")
            ),
        ):
            release.dispatch_deploy(
                "server", "v2.77.0", 1, 0, client_tag="v2.77.0", dry=True
            )
            release.ship_tag_side(
                SimpleNamespace(dry_run=True), "client", "v2.77.0", 1, 0
            )

    def test_plan_dispatch(self):
        tags = dict.fromkeys(release.SIDES, "v2.77.0")
        for action, side in (("deploy-pair", "server"), ("deploy", "client")):
            line = next(
                line
                for line in release.tag_lines([(action, side)], tags, "")
                if line.startswith("gh api ")
            ).split("   #", 1)[0]
            with (
                mock.patch.object(
                    release,
                    "ship_mutate",
                    return_value=json.dumps({"workflow_run_id": 12, "html_url": "url"}),
                ) as mutate,
                mock.patch.object(release, "wait_run_success"),
            ):
                release.dispatch_deploy(
                    side,
                    tags[side],
                    1,
                    0,
                    client_tag=tags["client"] if side == "server" else None,
                )
            self.assertEqual(tuple(shlex.split(line)), mutate.call_args.args[1:])

    def test_workflow_signals(self):
        # CI has no private submodules; verbatim excerpts keep the same signal test offline.
        snapshots = json.loads(
            (Path(__file__).parent / "fixtures/release-signals.json").read_text()
        )
        refs = snapshots.pop("_sources")
        texts = {}
        for name, pieces in snapshots.items():
            side, path = name.split("/", 1)
            repo = release.ROOT / side
            # The workspace still pins the previous release while the new client PR is pending.
            have_source = (repo / ".git").exists() and subprocess.run(
                ["git", "-C", str(repo), "cat-file", "-e", refs[side]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode == 0
            texts[name] = (
                subprocess.check_output(
                    ["git", "-C", str(repo), "show", f"{refs[side]}:{path}"],
                    text=True,
                    timeout=10,
                )
                if have_source
                else "\n".join(pieces)
            )
            for piece in pieces:
                self.assertIn(piece, texts[name], name)
        for side, workflow, create in (
            ("client", "client.yml", "Create prerelease"),
            ("server", "release.yml", "Create version GitHub Release"),
        ):
            text = texts[f"{side}/.github/workflows/{workflow}"]
            self.assertLess(
                text.index(f"- name: {create}"), text.index("- name: Release text")
            )
            self.assertLess(
                text.index("- name: Release text"),
                text.index("- name: Publish release notes"),
            )
        for side, workflow, script, prefix in (
            ("client", "client.yml", "deploy-client.py", None),
            (
                "server",
                "release.yml",
                "prepare-server-deploy.py",
                "Server gates on another",
            ),
        ):
            parent = texts[f"{side}/.github/workflows/{workflow}"].split("jobs:\n", 1)[
                1
            ]
            child = texts[f"{side}/.github/workflows/deploy.yml"]
            child_job = re.search(r"^  (\w+):$", child.split("jobs:\n", 1)[1], re.M)[1]
            step = next(
                block.splitlines()[0]
                for block in child.split("      - name: ")[1:]
                if f"scripts/{script}" in block
            )
            log = (
                re.search(
                    r'"(' + prefix + r'[^"\n]+)"', texts[f"{side}/scripts/{script}"]
                )[1]
                if prefix
                else ""
            )
            jobs = [
                {"name": name, "conclusion": "success"}
                for name in re.findall(r"^  (\w+):$", parent, re.M)
            ]
            deploy = next(job for job in jobs if job["name"] == "deploy")
            deploy.update(
                name=f"deploy / {child_job}",
                conclusion="success" if side == "client" else "failure",
                steps=[
                    {
                        "name": step,
                        "conclusion": "success" if side == "client" else "failure",
                    }
                ],
            )
            data = {
                "jobs": jobs,
                "status": "completed",
                "conclusion": "success" if side == "client" else "failure",
                "url": "url",
            }
            with (
                mock.patch.object(
                    release,
                    "gh",
                    side_effect=lambda *a: (
                        log if "--log-failed" in a else json.dumps(data)
                    ),
                ),
                mock.patch.object(
                    release, "verify_publication", return_value="verified"
                ),
            ):
                self.assertEqual(
                    release.wait_publication(side, "v2.77.0", 12, 1, 0), "verified"
                )

    def test_workflow_signals_offline(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(release, "ROOT", Path(tmp)),
        ):
            self.test_workflow_signals()

    def test_candidate_green_checks_must_belong_to_pushed_head(self):
        data = {
            "headRefOid": "old",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        }
        with mock.patch.object(release, "gh", side_effect=lambda *a: json.dumps(data)):
            self.assertEqual(release.pr_checks("repo", 1, "candidate"), "pending")
            data["headRefOid"] = "candidate"
            self.assertEqual(release.pr_checks("repo", 1, "candidate"), "green")
        with (
            mock.patch.object(release, "ship_mutate") as merge,
            mock.patch.object(
                release,
                "gh",
                return_value=json.dumps(
                    {
                        "state": "MERGED",
                        "headRefOid": "candidate",
                        "mergeCommit": {"oid": "a" * 40},
                    }
                ),
            ),
        ):
            release.merge_pr("repo", 1, False, "candidate")
        self.assertEqual(
            merge.call_args.args[-2:], ("--match-head-commit", "candidate")
        )

    def test_tag_run_is_selected_by_tag_event_and_sha(self):
        data = [
            {"headSha": "other", "databaseId": 1},
            {"headSha": "correct", "databaseId": 2},
        ]
        with mock.patch.object(release, "gh", return_value=json.dumps(data)) as gh:
            self.assertEqual(
                release.release_run("repo", "client.yml", "correct", "v2.77.0")[
                    "databaseId"
                ],
                2,
            )
        self.assertIn("v2.77.0", gh.call_args.args)
        self.assertIn("push", gh.call_args.args)

    def test_preflight_failure_or_main_change_prevents_tag(self):
        for results, shas in (
            ([("FAIL", "bad pins")], ["initial"]),
            ([], ["initial", "moved"]),
        ):
            with (
                mock.patch.object(release, "repo_dir", return_value=Path("repo")),
                mock.patch.object(
                    release,
                    "worktree",
                    return_value=contextlib.nullcontext(Path("tree")),
                ),
                mock.patch.object(release, "preflight", return_value=(results, None)),
                mock.patch.object(release, "fetch_main"),
                mock.patch.object(
                    release,
                    "git",
                    side_effect=lambda repo, *a: (
                        shas.pop(0) if a[0] == "rev-parse" else ""
                    ),
                ) as git,
            ):
                with self.assertRaises(RuntimeError):
                    release.push_tag("server", "v2.77.0", False, gate_waiver="test")
            self.assertFalse(
                any(c.args[1] in ("tag", "push") for c in git.call_args_list)
            )

    def test_publication_unblocks_before_full_run_but_keeps_stage_and_red_gates(self):
        for side, names in (("client", ["check"]), ("server", ["prepare", "release"])):
            data = {
                "status": "in_progress",
                "conclusion": "",
                "url": "running-tag",
                "jobs": [{"name": name, "conclusion": "success"} for name in names]
                + [
                    {
                        "name": "deploy / deploy",
                        "conclusion": "success" if side == "client" else "",
                    },
                ],
            }
            with (
                self.subTest(side=side),
                mock.patch.object(
                    release, "gh", side_effect=lambda *a: json.dumps(data)
                ),
                mock.patch.object(
                    release, "verify_publication", return_value="verified"
                ) as verify,
            ):
                self.assertEqual(
                    release.wait_publication(side, "v2.78.0", 1, 0, 0), "verified"
                )
                verify.assert_called_once_with(side, "v2.78.0")
                verify.reset_mock()
                data["jobs"][0]["conclusion"] = "failure"
                with self.assertRaisesRegex(RuntimeError, "publication job"):
                    release.wait_publication(side, "v2.78.0", 1, 0, 0)
                verify.assert_not_called()
                data["jobs"][0]["conclusion"] = "success"
                data["jobs"][-1].update(
                    conclusion="failure",
                    steps=[{"name": "Unrelated failure", "conclusion": "failure"}],
                )
                with self.assertRaises(RuntimeError):
                    release.wait_publication(side, "v2.78.0", 1, 0, 0)
                verify.assert_not_called()

    def test_resume_pair_refuses_changed_trees_and_server_tag(self):
        args = SimpleNamespace(
            resume_client_published=["v2.79.1", "v2.79.4", "a" * 40, "b" * 40]
        )
        for client_tree, server_tree, tagged in (
            ("a" * 40, "b" * 40, False),
            ("c" * 40, "b" * 40, False),
            ("a" * 40, "c" * 40, False),
            ("a" * 40, "b" * 40, True),
        ):

            def git(repo, *cmd):
                if cmd[0] == "ls-remote":
                    return "tagged" if tagged else ""
                if cmd[0] == "rev-parse":
                    return client_tree if repo == "client" else server_tree
                return ""

            with (
                mock.patch.object(release, "repo_dir", side_effect=lambda s: s),
                mock.patch.object(release, "git", side_effect=git),
            ):
                if client_tree == "a" * 40 and server_tree == "b" * 40 and not tagged:
                    self.assertEqual(
                        release.resume_pair(args)[0],
                        {"client": "v2.79.1", "server": "v2.79.4"},
                    )
                else:
                    with self.assertRaises(SystemExit):
                        release.resume_pair(args)

    def test_candidate_tree_guard_precedes_any_candidate_change(self):
        with (
            mock.patch.object(release, "repo_dir", return_value=Path("server")),
            mock.patch.object(release, "git", return_value="c" * 40),
            mock.patch.object(
                release, "worktree", return_value=contextlib.nullcontext(Path("server"))
            ),
            mock.patch.object(release, "set_candidate") as mutate,
        ):
            with self.assertRaisesRegex(SystemExit, "server tree changed"):
                release.cmd_candidate(
                    SimpleNamespace(
                        branch="release/server-2.79.4",
                        tag="v2.79.1",
                        dry_run=False,
                        expected_tree="b" * 40,
                    )
                )
            mutate.assert_not_called()

    def test_client_stage_evidence_binds_workflow_tag_and_no_promotion(self):
        data = {
            "event": "workflow_dispatch",
            "path": ".github/workflows/deploy.yml",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
        }
        jobs = {
            "jobs": [
                {
                    "name": "deploy",
                    "conclusion": "success",
                    "steps": [
                        {
                            "name": "Mark the deployed client as latest",
                            "conclusion": "skipped",
                        }
                    ],
                }
            ]
        }
        for log, event, promotion, allowed in (
            ("Client v2.79.1 staged invisibly;", "workflow_dispatch", "skipped", True),
            ("Client v2.79.0 staged invisibly;", "workflow_dispatch", "skipped", False),
            ("Client v2.79.1 staged invisibly;", "push", "skipped", False),
            ("Client v2.79.1 staged invisibly;", "workflow_dispatch", "success", False),
        ):
            data["event"] = event
            jobs["jobs"][0]["steps"][0]["conclusion"] = promotion
            with mock.patch.object(
                release, "gh", side_effect=[json.dumps(data), json.dumps(jobs), log]
            ):
                if allowed:
                    self.assertTrue(release.verify_client_stage("v2.79.1", 42))
                else:
                    with self.assertRaises(RuntimeError):
                        release.verify_client_stage("v2.79.1", 42)

    def test_external_stage_accepts_skipped_not_failed_automatic_deploy(self):
        data = {
            "status": "completed",
            "conclusion": "success",
            "url": "tag-run",
            "jobs": [
                {"name": "check", "conclusion": "success"},
                {"name": "deploy", "conclusion": "skipped"},
            ],
        }
        with (
            mock.patch.object(release, "gh", side_effect=lambda *a: json.dumps(data)),
            mock.patch.object(release, "verify_client_stage", return_value=True),
            mock.patch.object(release, "verify_publication", return_value="verified"),
        ):
            self.assertEqual(
                release.wait_publication(
                    "client", "v2.79.1", 1, 0, 0, client_stage_run=42
                ),
                "verified",
            )
            data["jobs"][-1]["conclusion"] = "failure"
            with self.assertRaises(RuntimeError):
                release.wait_publication(
                    "client", "v2.79.1", 1, 0, 0, client_stage_run=42
                )

    def test_publication_allows_only_ancillary_skips(self):
        for name, conclusion, allowed in (
            ("bot-gate", "skipped", True),
            ("release-text", "neutral", True),
            ("bot-gate", "failure", False),
            ("check", "skipped", False),
            ("check", "neutral", False),
        ):
            data = {
                "status": "completed",
                "conclusion": "success",
                "url": "tag-run",
                "jobs": [
                    {"name": "check", "conclusion": "success"},
                    {"name": "deploy / deploy", "conclusion": "success"},
                ],
            }
            if name == "check":
                data["jobs"][0]["conclusion"] = conclusion
            else:
                data["jobs"].append({"name": name, "conclusion": conclusion})
            with (
                self.subTest(name=name, conclusion=conclusion),
                mock.patch.object(release, "gh", return_value=json.dumps(data)),
                mock.patch.object(
                    release, "verify_publication", return_value="verified"
                ),
            ):
                if allowed:
                    self.assertEqual(
                        release.wait_publication("client", "v2.79.1", 1, 0, 0),
                        "verified",
                    )
                else:
                    with self.assertRaises(RuntimeError):
                        release.wait_publication("client", "v2.79.1", 1, 0, 0)

    def test_release_created_before_notes_is_not_publication(self):
        data = {"draft": False, "tag_name": "v2.78.0", "assets": []}
        with mock.patch.object(release, "gh", return_value=json.dumps(data)):
            with self.assertRaisesRegex(RuntimeError, "release-notes.md"):
                release.verify_publication("server", "v2.78.0")
        data["assets"].append({"name": "release-notes.md"})
        with mock.patch.object(release, "gh", return_value=json.dumps(data)):
            release.verify_publication("server", "v2.78.0")

    def test_publication_accepts_only_successful_publish_jobs(self):
        for side, names in (("client", ["check"]), ("server", ["prepare", "release"])):
            for conclusion in ("success", "failure", "skipped", "cancelled"):
                jobs = [{"name": name, "conclusion": conclusion} for name in names]
                step = (
                    "Verify compatibility and deploy client"
                    if side == "client"
                    else "Validate target and remember previous pair"
                )
                refusal = (
                    "Client is incompatible with the running server; coordinate shared-mod/protocol changes"
                    if side == "client"
                    else "Server gates on another client than the served one: rerun Deploy server with client_tag"
                )
                jobs.append(
                    {
                        "name": "deploy / deploy",
                        "conclusion": "success" if side == "client" else "failure",
                        "steps": [{"name": step, "conclusion": "failure"}],
                    }
                )
                data = {
                    "jobs": jobs,
                    "status": "completed",
                    "conclusion": "success" if side == "client" else "failure",
                    "url": "run-url",
                }
                with (
                    self.subTest(side=side, conclusion=conclusion),
                    mock.patch.object(
                        release,
                        "gh",
                        side_effect=lambda *a: (
                            refusal if "--log-failed" in a else json.dumps(data)
                        ),
                    ),
                    mock.patch.object(
                        release, "verify_publication", return_value="manifest"
                    ) as verify,
                ):
                    if conclusion == "success":
                        self.assertEqual(
                            release.wait_publication(side, "v2.77.0", 1, 1, 0),
                            "manifest",
                        )
                        verify.assert_called_once_with(side, "v2.77.0")
                    else:
                        with self.assertRaises(RuntimeError):
                            release.wait_publication(side, "v2.77.0", 1, 1, 0)
                        verify.assert_not_called()

    def test_unrelated_automatic_deploy_failure_stops_pair(self):
        for step, log in (
            ("Require a stable Game and Workshop pair", "Workshop not ready"),
            ("Verify compatibility and deploy client", "Pack swap failed"),
        ):
            data = {
                "status": "completed",
                "conclusion": "failure",
                "url": "failed-run",
                "jobs": [
                    {"name": "check", "conclusion": "success"},
                    {
                        "name": "deploy / deploy",
                        "conclusion": "failure",
                        "steps": [{"name": step, "conclusion": "failure"}],
                    },
                ],
            }
            with (
                mock.patch.object(
                    release,
                    "gh",
                    side_effect=lambda *a: (
                        log if "--log-failed" in a else json.dumps(data)
                    ),
                ),
                mock.patch.object(release, "verify_publication") as verify,
            ):
                with self.assertRaisesRegex(RuntimeError, "must stage successfully"):
                    release.wait_publication("client", "v2.77.0", 1, 1, 0)
                verify.assert_not_called()

    def test_missing_jobs_and_unfinished_auto_deploy_do_not_pass(self):
        for jobs, status in (
            ([], "completed"),
            ([{"name": "check", "conclusion": "success"}], "in_progress"),
        ):
            with mock.patch.object(
                release,
                "gh",
                return_value=json.dumps({"jobs": jobs, "status": status, "url": "url"}),
            ):
                with self.assertRaises(RuntimeError):
                    release.wait_publication("client", "v2.77.0", 1, 0, 0)

    def test_client_assets_and_manifest_must_match_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = {"build": "v2.77.0", "client": "digest", "compatibility": MOVED}
            manifest = json.dumps(meta).encode()
            names = [
                "build.json",
                "Blockfield-web.tar",
                "Blockfield-client.zip",
                "Blockfield-prism.zip",
                "Blockfield.mrpack",
            ]
            sums = {
                name: hashlib.sha256(
                    manifest if name == "build.json" else name.encode()
                ).hexdigest()
                for name in names
            }
            (root / "build.json").write_bytes(manifest)
            (root / "SHA256SUMS").write_text(
                "".join(f"{sha}  {name}\n" for name, sha in sums.items())
            )
            assets = [
                {"name": name, "digest": f"sha256:{sha}"} for name, sha in sums.items()
            ]
            assets += [{"name": "SHA256SUMS"}, {"name": "release-notes.md"}]
            data = {
                "tag_name": "v2.77.0",
                "draft": False,
                "prerelease": True,
                "assets": assets,
            }

            def gh(*args):
                if args[0] == "api":
                    return json.dumps(data)
                if args[:2] == ("release", "download"):
                    destination = Path(args[args.index("-D") + 1])
                    for name in ("build.json", "SHA256SUMS"):
                        shutil.copyfile(root / name, destination / name)
                return ""

            with (
                mock.patch.object(release, "gh", side_effect=gh),
                mock.patch.object(release, "CACHE", root / "fresh" / "cache"),
            ):
                self.assertEqual(release.verify_publication("client", "v2.77.0"), meta)
                for changed in ("digest", "manifest", "missing", "draft"):
                    with self.subTest(changed=changed):
                        if changed == "digest":
                            assets[1]["digest"] = "sha256:bad"
                        elif changed == "manifest":
                            assets[1]["digest"] = "sha256:" + sums[names[1]]
                            (root / "build.json").write_bytes(b"changed")
                        elif changed == "missing":
                            assets.pop()
                        else:
                            data["draft"] = True
                        with self.assertRaises(RuntimeError):
                            release.verify_publication("client", "v2.77.0")

    def test_dispatch_argv(self):
        with (
            mock.patch.object(
                release,
                "ship_mutate",
                return_value=json.dumps({"workflow_run_id": 12, "html_url": "run-url"}),
            ) as dispatch,
            mock.patch.object(
                release, "gh", side_effect=AssertionError("must not search runs")
            ),
            mock.patch.object(release, "wait_run_success") as success,
        ):
            release.dispatch_deploy("server", "v2.77.0", 1, 0, client_tag="v2.77.0")
        self.assertEqual(
            dispatch.call_args.args,
            (
                False,
                "gh",
                "api",
                "--method",
                "POST",
                "repos/Blockfield/blockfield-server/actions/workflows/deploy.yml/dispatches",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                "-f",
                "ref=main",
                "-F",
                "return_run_details=true",
                "-f",
                "inputs[tag]=v2.77.0",
                "-f",
                "inputs[client_tag]=v2.77.0",
            ),
        )
        self.assertEqual(dispatch.call_count, 1)
        success.assert_called_once_with("server", 12, 1, 0, release.REPO["server"])

    def test_dispatch_without_run_id_stops_without_retry(self):
        with (
            mock.patch.object(release, "ship_mutate", return_value="") as dispatch,
            mock.patch.object(release, "wait_run_success") as success,
        ):
            with self.assertRaisesRegex(RuntimeError, "do not retry"):
                release.dispatch_deploy("client", "v2.77.0", 1, 0)
        self.assertEqual(dispatch.call_count, 1)
        success.assert_not_called()

    def test_run_failure_stops_without_retry(self):
        with mock.patch.object(
            release,
            "gh",
            return_value=json.dumps({"conclusion": "failure", "url": "failed"}),
        ) as gh:
            with self.assertRaisesRegex(RuntimeError, "FAILURE"):
                release.wait_run_success("server", 1, 1, 0)
        self.assertEqual(gh.call_count, 1)

    def test_verify_pair_requires_all_public_fields(self):
        client = {"client": "digest", "compatibility": MOVED}
        tags = {s: "v2.77.0" for s in release.SIDES}
        good = {"build": tags["client"], "server_build": tags["server"], **client}
        for key in good:
            with mock.patch.object(
                release, "prod_state", return_value=good | {key: "old"}
            ):
                with self.assertRaises(RuntimeError):
                    release.verify_pair(tags, client, 0, 0)
        with (
            mock.patch.object(release, "prod_state", return_value=good),
            mock.patch.object(release, "check_client_release") as promoted,
        ):
            release.verify_pair(tags, client, 1, 0)
        promoted.assert_called_once_with(tags["client"], False, latest=True)


class ClientDeploy(unittest.TestCase):
    def test_client_dispatch_targets_server_workflow(self):
        self.assertEqual(
            release.deploy_command("client", "v2.77.0"),
            (
                "gh",
                "api",
                "--method",
                "POST",
                "repos/Blockfield/blockfield-server/actions/workflows/deploy-client.yml/dispatches",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                "-f",
                "ref=main",
                "-F",
                "return_run_details=true",
                "-f",
                "inputs[tag]=v2.77.0",
                "-f",
                "inputs[dry_run]=false",
            ),
        )
        with (
            mock.patch.object(
                release,
                "ship_mutate",
                return_value=json.dumps({"workflow_run_id": 12, "html_url": "run-url"}),
            ) as dispatch,
            mock.patch.object(release, "wait_run_success") as success,
        ):
            release.dispatch_deploy("client", "v2.77.0", 1, 0)
        self.assertEqual(
            dispatch.call_args.args[1:],
            release.deploy_command("client", "v2.77.0"),
        )
        success.assert_called_once_with("client", 12, 1, 0, release.REPO["server"])

    def test_deploy_waiter_uses_dispatch_repo_for_every_side(self):
        for side in ("client", "server"):
            with (
                mock.patch.object(
                    release,
                    "ship_mutate",
                    return_value=json.dumps(
                        {"workflow_run_id": 12, "html_url": "run-url"}
                    ),
                ),
                mock.patch.object(release, "wait_run_success") as success,
            ):
                release.dispatch_deploy(side, "v2.77.0", 1, 0)
            success.assert_called_once_with(side, 12, 1, 0, release.deploy_repo(side))
        self.assertEqual(release.deploy_repo("client"), release.REPO["server"])
        self.assertEqual(release.deploy_repo("server"), release.REPO["server"])

    def test_wait_run_success_polls_given_repo(self):
        with mock.patch.object(
            release,
            "gh",
            return_value=json.dumps({"conclusion": "success", "url": "run-url"}),
        ) as gh:
            release.wait_run_success("client", 12, 1, 0, release.REPO["server"])
        repo_flag = gh.call_args.args
        self.assertIn(release.REPO["server"], repo_flag)
        self.assertNotIn(release.REPO["client"], repo_flag)

    def test_server_dispatch_still_targets_server_deploy(self):
        self.assertIn(
            "repos/Blockfield/blockfield-server/actions/workflows/deploy.yml/dispatches",
            release.deploy_command("server", "v2.77.0", client_tag="v2.77.0"),
        )
        self.assertIn(
            "inputs[client_tag]=v2.77.0",
            release.deploy_command("server", "v2.77.0", client_tag="v2.77.0"),
        )

    def test_rollback_client_dispatches_older_tag(self):
        args = release.parser().parse_args(
            ["rollback-client", "v2.76.3", "--confirmed-by-owner"]
        )
        with mock.patch.object(release, "dispatch_deploy") as dispatch:
            self.assertTrue(release.cmd_rollback_client(args))
        dispatch.assert_called_once_with("client", "v2.76.3", 1800, 5, dry=False)

    def test_rollback_client_rejects_bad_tag(self):
        args = release.parser().parse_args(
            ["rollback-client", "nope", "--confirmed-by-owner"]
        )
        with (
            mock.patch.object(
                release, "dispatch_deploy", side_effect=AssertionError("no dispatch")
            ),
        ):
            with self.assertRaises(SystemExit):
                release.cmd_rollback_client(args)

    def test_rollback_client_dry_run_dispatches_nothing(self):
        args = release.parser().parse_args(["rollback-client", "v2.76.3", "--dry-run"])
        with mock.patch.object(release, "say"):
            with mock.patch.object(
                release, "run", side_effect=AssertionError("no dispatch")
            ):
                with mock.patch.object(release, "wait_run_success") as success:
                    self.assertTrue(release.cmd_rollback_client(args))
        success.assert_not_called()

    def test_rollback_client_refuses_without_owner_confirmation(self):
        args = release.parser().parse_args(["rollback-client", "v2.76.3"])
        with mock.patch.object(
            release, "dispatch_deploy", side_effect=AssertionError("no dispatch")
        ):
            with self.assertRaisesRegex(SystemExit, "--confirmed-by-owner"):
                release.cmd_rollback_client(args)

    def test_rollback_client_dry_run_needs_no_confirmation(self):
        args = release.parser().parse_args(["rollback-client", "v2.76.3", "--dry-run"])
        with mock.patch.object(release, "dispatch_deploy") as dispatch:
            self.assertTrue(release.cmd_rollback_client(args))
        dispatch.assert_called_once_with("client", "v2.76.3", 1800, 5, dry=True)


class PendingServerPins(unittest.TestCase):
    def test_notes_early(self):
        for legacy in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                tree = Path(tmp)
                (tree / "pack.toml").write_text('version = "2.76.1"\n')
                workflows = tree / ".github/workflows"
                workflows.mkdir(parents=True)
                (workflows / "client.yml").write_text(
                    "release-notes-tool.py check"
                    if legacy
                    else "Release text at tag time"
                )
                args = release.parser().parse_args(
                    ["prepare", "--mod", "v1.55.3", "--bump", "minor", "--dry-run"]
                )
                with (
                    mock.patch.object(release, "repo_dir", return_value=tree),
                    mock.patch.object(release, "fetch_main"),
                    mock.patch.object(
                        release, "remote_tags", return_value={"v2.76.1": "a" * 40}
                    ),
                    mock.patch.object(release, "show_pack", return_value="2.76.1"),
                    mock.patch.object(release, "published", return_value="v2.76.1"),
                    mock.patch.object(
                        release,
                        "git",
                        side_effect=lambda repo, *a: (
                            "mods/mod.pw.toml" if a[0] == "diff" else ""
                        ),
                    ),
                    mock.patch.object(
                        release, "worktree", return_value=contextlib.nullcontext(tree)
                    ),
                    mock.patch.object(
                        release, "pin_jar", return_value="mods/mod.pw.toml"
                    ),
                    mock.patch.object(release, "refresh_index"),
                    mock.patch.object(
                        release, "build", return_value={"compatibility": PROD}
                    ),
                    mock.patch.object(
                        release, "prod_state", return_value={"compatibility": PROD}
                    ),
                    mock.patch.object(release, "open_pr") as pr,
                ):
                    if legacy:
                        with self.assertRaisesRegex(SystemExit, "preflight failed"):
                            release.prepare_side("client", args, {}, None, False)
                        pr.assert_not_called()
                    else:
                        self.assertEqual(
                            release.prepare_side("client", args, {}, None, False)[0],
                            "v2.77.0",
                        )
                        pr.assert_called_once()

    def test_client_only_rejects_server_merge(self):
        args = release.parser().parse_args(
            [
                "prepare",
                "--mod",
                "v1.55.3",
                "--only",
                "client",
                "--server-merge",
                "pins/fork-bf-releases",
            ]
        )
        with mock.patch.object(
            release,
            "prod_state",
            side_effect=AssertionError("no work before rejection"),
        ):
            with self.assertRaisesRegex(ValueError, "--only client"):
                release.prepare_all(args)

    def test_pending_branch_is_merged_into_main_before_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []

            def git(repo, *args):
                events.append(args)
                if args[0] == "rev-parse":
                    return "a" * 40
                if args[:2] == ("diff", "origin/main"):
                    return "mods/create.pw.toml"
                return ""

            args = release.parser().parse_args(
                [
                    "prepare",
                    "--mod",
                    "v1.55.3",
                    "--server-merge",
                    "pins/fork-bf-releases",
                    "--dry-run",
                ]
            )
            with (
                mock.patch.object(release, "repo_dir", return_value=tree),
                mock.patch.object(release, "fetch_main"),
                mock.patch.object(
                    release, "remote_tags", return_value={"v2.76.4": "b" * 40}
                ),
                mock.patch.object(release, "show_pack", return_value="2.76.4"),
                mock.patch.object(release, "git", side_effect=git),
                mock.patch.object(
                    release, "worktree", return_value=contextlib.nullcontext(tree)
                ) as worktree,
                mock.patch.object(release, "run"),
                mock.patch.object(release, "set_version"),
                mock.patch.object(release, "refresh_index"),
                mock.patch.object(
                    release,
                    "preflight",
                    side_effect=lambda *a, **kw: (
                        events.append(("preflight",)) or [],
                        {"compatibility": MOVED},
                    ),
                ) as preflight,
                mock.patch.object(release, "open_pr") as pr,
            ):
                got = release.prepare_side(
                    "server", args, {}, "file:///candidate", False
                )
            self.assertEqual(got[0], "v2.76.5")
            self.assertEqual(worktree.call_args.args[1], "origin/main")
            merge = ("merge", "--no-ff", "--no-edit", "a" * 40)
            self.assertLess(events.index(merge), events.index(("preflight",)))
            self.assertEqual(preflight.call_args.args[3], "file:///candidate")
            self.assertTrue(pr.call_args.args[0])


class RcGateTest(unittest.TestCase):
    def record(self, releases, checks):
        return {
            "format": release.RC_RECORD_FORMAT,
            "battery": "just scenario rc",
            "releases": releases,
            "checks": checks,
        }

    def levels(self, tags, record):
        with mock.patch.object(release, "say"):
            return [level for level, _ in release.check_rc_record(tags, record)]

    def test_pass_pair_exact_versions(self):
        tags = {"client": "v2.80.0", "server": "v2.80.1"}
        checks = [
            {
                "name": "versions loaded match pins",
                "result": "PASS",
                "evidence": "1.57.0",
            },
            {"name": "first-shot after deploy", "result": "PASS", "evidence": "ok"},
        ]
        self.assertEqual(self.levels(tags, self.record(tags, checks)), ["ok", "ok"])

    def test_releases_must_match_exactly(self):
        tags = {"client": "v2.80.0", "server": "v2.80.1"}
        good = [{"name": "x", "result": "PASS", "evidence": ""}]
        for releases in (
            {"client": "v2.80.0"},
            {"client": "v2.80.0", "server": "v2.80.1", "extra": "v1.0.0"},
            {"client": "v2.80.0", "server": "v2.80.2"},
            None,
        ):
            self.assertEqual(
                self.levels(tags, self.record(releases, good)), ["FAIL"], releases
            )

    def test_fail_check_blocks(self):
        tags = {"server": "v2.80.1"}
        checks = [{"name": "hitreg smoke", "result": "FAIL", "evidence": "0/10"}]
        self.assertEqual(self.levels(tags, self.record(tags, checks)), ["FAIL"])

    def test_skip_second_client_only_on_server_only(self):
        skipped = [
            {
                "name": "long-range kill hamburg",
                "result": "SKIP",
                "evidence": "no client",
            },
            {
                "name": "tdm death/respawn at BASE",
                "result": "SKIP",
                "evidence": "no client",
            },
        ]
        self.assertEqual(
            self.levels(
                {"server": "v2.80.1"}, self.record({"server": "v2.80.1"}, skipped)
            ),
            ["ok", "ok"],
        )
        pair = {"client": "v2.80.0", "server": "v2.80.1"}
        self.assertIn("FAIL", self.levels(pair, self.record(pair, skipped)))
        other = [{"name": "first-shot after deploy", "result": "SKIP", "evidence": ""}]
        self.assertEqual(
            self.levels(
                {"server": "v2.80.1"}, self.record({"server": "v2.80.1"}, other)
            ),
            ["FAIL"],
        )

    def test_empty_or_missing_checks_fail(self):
        tags = {"server": "v2.80.1"}
        for checks in ([], None, "PASS"):
            self.assertEqual(
                self.levels(tags, self.record(tags, checks)), ["FAIL"], checks
            )

    def test_load_rejects_unreadable_and_non_object(self):
        with self.assertRaises(RuntimeError):
            release.load_rc_record("/nonexistent/rc.json")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rc.json"
            path.write_text("[1, 2]")
            with self.assertRaises(RuntimeError):
                release.load_rc_record(path)

    def test_require_refuses_without_record_or_waiver(self):
        with mock.patch.object(release, "say"):
            with self.assertRaises(SystemExit):
                release.require_rc_gate({"server": "v2.80.1"})
            with self.assertRaises(SystemExit):
                release.require_rc_gate(
                    {"server": "v2.80.1"}, rc_record="/nonexistent/rc.json"
                )

    def test_require_passes_with_record_and_waiver(self):
        tags = {"server": "v2.80.1"}
        record = self.record(
            tags,
            [{"name": "versions loaded match pins", "result": "PASS", "evidence": ""}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rc.json"
            path.write_text(json.dumps(record))
            with mock.patch.object(release, "say"):
                self.assertTrue(release.require_rc_gate(tags, path))
                self.assertTrue(
                    release.require_rc_gate(tags, gate_waiver="RC cancelled by owner")
                )

    def test_push_tag_refuses_without_gate(self):
        with mock.patch.object(release, "say"):
            with self.assertRaises(SystemExit):
                release.push_tag("server", "v2.80.1", False)
            with mock.patch.object(
                release, "repo_dir", side_effect=RuntimeError("past the gate")
            ):
                with self.assertRaisesRegex(RuntimeError, "past the gate"):
                    release.push_tag("server", "v2.80.1", False, gate_waiver="test")


class PostDeployProbesTest(unittest.TestCase):
    def run_probes(self, tags, prod, client=None, timeout=1, interval=0):
        said = []
        with (
            mock.patch.object(release, "prod_state", return_value=dict(prod)),
            mock.patch.object(release, "say", side_effect=said.append),
            mock.patch.object(
                release,
                "entry_probe",
                return_value=("REQUIRED", "manual: proxy gate Game backend open"),
            ),
        ):
            result = release.post_deploy_probes(tags, client, timeout, interval)
        return result, said

    def test_rooms_and_contract_pass_with_required_manuals(self):
        tags = {"client": "v2.80.0", "server": "v2.80.1"}
        prod = {
            "build": "v2.80.0",
            "server_build": "v2.80.1",
            "compatibility": MOVED,
            "client": "digest",
        }
        ok, said = self.run_probes(tags, prod, {"compatibility": MOVED})
        self.assertTrue(ok)
        text = "\n".join(said)
        self.assertIn("/api/rooms serves client v2.80.0, server v2.80.1", text)
        self.assertIn("build.json contract", text)
        for needle in ("server.properties", "proxy gate"):
            self.assertIn(needle, text)
        self.assertIn("REQUIRED", text)
        # The Workshop is never ship's business: no Workshop READY probe, and
        # the entry probe names the Game backend only.
        self.assertNotIn("Workshop READY", text)
        self.assertNotIn("both backends", text)
        self.assertIn("manual: proxy gate Game backend open", text)

    def test_client_only_skips_server_manuals(self):
        prod = {"build": "v2.80.0", "compatibility": MOVED}
        ok, said = self.run_probes({"client": "v2.80.0"}, prod)
        self.assertTrue(ok)
        text = "\n".join(said)
        self.assertIn("proxy gate", text)
        self.assertNotIn("server.properties", text)
        self.assertNotIn("Workshop READY", text)

    def test_contract_mismatch_fails(self):
        tags = {"server": "v2.80.1"}
        prod = {"build": "v2.80.0", "server_build": "v2.80.1", "compatibility": PROD}
        with (
            mock.patch.object(release, "prod_state", return_value=prod),
            mock.patch.object(release, "say"),
        ):
            with self.assertRaises(RuntimeError):
                release.post_deploy_probes(tags, {"compatibility": MOVED}, 1, 0)

    def test_waits_for_rooms_then_probes_and_retries_transient_errors(self):
        prod = {"build": "v2.80.0", "server_build": "v2.80.1", "compatibility": MOVED}
        states = [OSError("down"), dict(prod, server_build="v2.80.0"), prod]
        with (
            mock.patch.object(
                release, "prod_state", side_effect=list(states)
            ) as prod_state,
            mock.patch.object(release, "say"),
            mock.patch.object(
                release, "entry_probe", return_value=("ok", "synthetic entry alive")
            ),
        ):
            self.assertTrue(
                release.post_deploy_probes({"server": "v2.80.1"}, None, 60, 0)
            )
        self.assertEqual(prod_state.call_count, 3)

    def test_timeout_raises_without_retry(self):
        prod = {"build": "v2.80.0", "server_build": "v2.80.0", "compatibility": MOVED}
        with (
            mock.patch.object(release, "prod_state", return_value=prod),
            mock.patch.object(release, "say"),
        ):
            with self.assertRaises(RuntimeError):
                release.post_deploy_probes({"server": "v2.80.1"}, None, 0, 0)

    def test_automated_entry_probe_replaces_the_manual_gate(self):
        prod = {"build": "v2.80.0", "compatibility": MOVED}
        ok, said = self.run_probes({"client": "v2.80.0"}, prod)
        self.assertTrue(ok)
        self.assertIn("proxy gate", "\n".join(said))
        with (
            mock.patch.object(release, "prod_state", return_value=dict(prod)),
            mock.patch.object(release, "say", side_effect=lambda m: said.append(m)),
            mock.patch.object(
                release, "entry_probe", return_value=("FAIL", "synthetic entry dead")
            ),
        ):
            with self.assertRaises(RuntimeError):
                release.post_deploy_probes({"client": "v2.80.0"}, None, 1, 0)

    def test_entry_probe_without_checkout_stays_manual(self):
        with mock.patch.object(Path, "is_file", lambda self: False):
            level, text = release.entry_probe("v2.80.0")
        self.assertEqual(level, "REQUIRED")
        self.assertIn("proxy gate", text)

    def test_client_leg_never_expects_the_client_tag(self):
        # /api/rooms build is the server build: after a client deploy the
        # probe must expect the server tag, or skip the build comparison.
        prod = {"build": "v2.80.0", "server_build": "v2.80.1", "compatibility": MOVED}
        seen = []
        with (
            mock.patch.object(release, "prod_state", return_value=dict(prod)),
            mock.patch.object(release, "say"),
            mock.patch.object(
                release,
                "entry_probe",
                side_effect=lambda tag: seen.append(tag) or ("ok", "probe"),
            ),
        ):
            self.assertTrue(
                release.post_deploy_probes({"client": "v2.80.0"}, None, 1, 0)
            )
            self.assertTrue(
                release.post_deploy_probes({"server": "v2.80.1"}, None, 1, 0)
            )
        self.assertEqual(seen, [None, "v2.80.1"])

    def test_entry_probe_without_tag_skips_build_comparison(self):
        good = subprocess.CompletedProcess([], 0, "entry alive", "")
        new_help = subprocess.CompletedProcess(
            [], 0, "usage: entry_liveness.py [--game-only]", ""
        )
        with (
            mock.patch.object(Path, "is_file", lambda self: True),
            mock.patch.object(
                release.subprocess, "run", side_effect=[new_help, good]
            ) as run,
        ):
            self.assertEqual(release.entry_probe(None)[0], "ok")
            argv = run.call_args.args[0]
            self.assertIn("--game-only", argv)
            self.assertNotIn("--tag", argv)

    def test_entry_probe_maps_exit_codes(self):
        good = subprocess.CompletedProcess([], 0, "entry alive", "")
        bad = subprocess.CompletedProcess([], 1, "FAIL tcp_game", "")
        old_help = subprocess.CompletedProcess([], 0, "usage: entry_liveness.py", "")
        new_help = subprocess.CompletedProcess(
            [], 0, "usage: entry_liveness.py [--game-only]", ""
        )
        with (
            mock.patch.object(Path, "is_file", lambda self: True),
            mock.patch.object(
                release.subprocess, "run", side_effect=[old_help, good]
            ) as run,
        ):
            self.assertEqual(release.entry_probe("v2.80.0")[0], "ok")
            argv = run.call_args.args[0]
            self.assertIn("workshop.blockfield.pro", argv)
            self.assertIn("--workshop-host", argv)
            self.assertNotIn("--game-only", argv)
        with (
            mock.patch.object(Path, "is_file", lambda self: True),
            mock.patch.object(
                release.subprocess, "run", side_effect=[new_help, good]
            ) as run,
        ):
            self.assertEqual(release.entry_probe("v2.80.0")[0], "ok")
            self.assertIn("--game-only", run.call_args.args[0])
        with (
            mock.patch.object(Path, "is_file", lambda self: True),
            mock.patch.object(release.subprocess, "run", side_effect=[new_help, bad]),
        ):
            level, text = release.entry_probe("v2.80.0")
            self.assertEqual(level, "FAIL")
            self.assertIn("tcp_game", text)


class ForkPinOnly(unittest.TestCase):
    """prepare_side must release fork-pin-only content: unreleased commits
    vs the newest tag count even when mod/decor pins match origin/main."""

    def prepare_args(self, *extra):
        return release.parser().parse_args(
            ["prepare", "--mod", "v1.57.0", "--dry-run", *extra]
        )

    def side_patches(self, tree, git):
        return (
            mock.patch.object(release, "repo_dir", return_value=tree),
            mock.patch.object(release, "fetch_main"),
            mock.patch.object(
                release, "remote_tags", return_value={"v2.76.4": "b" * 40}
            ),
            mock.patch.object(release, "show_pack", return_value="2.76.4"),
            mock.patch.object(release, "git", side_effect=git),
            mock.patch.object(
                release, "worktree", return_value=contextlib.nullcontext(tree)
            ),
            mock.patch.object(release, "pin_jar", return_value="mods/mod.pw.toml"),
            mock.patch.object(release, "set_version"),
            mock.patch.object(release, "refresh_index"),
            mock.patch.object(
                release, "preflight", return_value=([], {"compatibility": PROD})
            ),
            mock.patch.object(release, "open_pr"),
        )

    def run_patches(self, patches, func):
        with contextlib.ExitStack() as stack:
            mocks = [stack.enter_context(patch) for patch in patches]
            return func(*mocks[-2:])

    def test_unreleased_commits_release_despite_unchanged_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []

            def git(repo, *args):
                events.append(args)
                if args[0] == "diff":
                    return ""
                if args[0] == "log":
                    self.assertEqual(args[1:3], ("--no-merges", "--format=%h"))
                    return "abc1234 Pin SBW bf26\n"
                return ""

            patches = self.side_patches(tree, git)
            got, preflight, pr = self.run_patches(
                patches,
                lambda preflight, pr: (
                    release.prepare_side(
                        "client", self.prepare_args(), {}, None, False
                    ),
                    preflight,
                    pr,
                ),
            )
            self.assertEqual(got[0], "v2.76.5")
            self.assertEqual(got[1], {"compatibility": PROD})
            fetch = ("fetch", "-q", "origin", "tag", "v2.76.4")
            log = next(args for args in events if args[0] == "log")
            self.assertIn(fetch, events)
            self.assertLess(events.index(fetch), events.index(log))
            message = pr.call_args.args[5]
            self.assertIn("fork pins since v2.76.4", message)

    def test_no_tags_still_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []

            def git(repo, *args):
                events.append(args)
                return ""

            patches = (
                mock.patch.object(release, "repo_dir", return_value=tree),
                mock.patch.object(release, "fetch_main"),
                mock.patch.object(release, "remote_tags", return_value={}),
                mock.patch.object(release, "show_pack", return_value="2.76.4"),
                mock.patch.object(release, "git", side_effect=git),
                mock.patch.object(
                    release, "worktree", return_value=contextlib.nullcontext(tree)
                ),
                mock.patch.object(release, "pin_jar", return_value="mods/mod.pw.toml"),
                mock.patch.object(release, "set_version"),
                mock.patch.object(release, "refresh_index"),
                mock.patch.object(release, "preflight"),
                mock.patch.object(release, "open_pr"),
            )
            with contextlib.ExitStack() as stack:
                mocks = [stack.enter_context(patch) for patch in patches]
                preflight, pr = mocks[-2:]
                self.assertIsNone(
                    release.prepare_side("client", self.prepare_args(), {}, None, False)
                )
            self.assertFalse([args for args in events if args[0] == "log"], events)
            preflight.assert_not_called()
            pr.assert_not_called()

    def test_no_unreleased_commits_still_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []

            def git(repo, *args):
                events.append(args)
                return ""

            patches = self.side_patches(tree, git)
            with contextlib.ExitStack() as stack:
                mocks = [stack.enter_context(patch) for patch in patches]
                preflight, pr = mocks[-2:]
                self.assertIsNone(
                    release.prepare_side("client", self.prepare_args(), {}, None, False)
                )
            self.assertTrue(any(args[0] == "log" for args in events), events)
            preflight.assert_not_called()
            pr.assert_not_called()

    def test_server_merge_merges_branches_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []
            shas = iter(["a" * 40, "b" * 40])

            def git(repo, *args):
                events.append(args)
                if args[0] == "rev-parse":
                    return next(shas)
                if args[:2] == ("diff", "origin/main"):
                    return "mods/sbw.pw.toml"
                return ""

            args = self.prepare_args("--server-merge", "pins/fork-a,pins/fork-b")
            patches = (
                mock.patch.object(release, "repo_dir", return_value=tree),
                mock.patch.object(release, "fetch_main"),
                mock.patch.object(
                    release, "remote_tags", return_value={"v2.76.4": "b" * 40}
                ),
                mock.patch.object(release, "show_pack", return_value="2.76.4"),
                mock.patch.object(release, "git", side_effect=git),
                mock.patch.object(
                    release, "worktree", return_value=contextlib.nullcontext(tree)
                ),
                mock.patch.object(release, "run", return_value=""),
                mock.patch.object(release, "set_version"),
                mock.patch.object(release, "refresh_index"),
                mock.patch.object(
                    release, "preflight", return_value=([], {"compatibility": PROD})
                ),
                mock.patch.object(release, "open_pr"),
            )
            with contextlib.ExitStack() as stack:
                mocks = [stack.enter_context(patch) for patch in patches]
                pr = mocks[-1]
                got = release.prepare_side("server", args, {}, None, False)
            self.assertEqual(got[0], "v2.76.5")
            ops = [
                args
                for args in events
                if args[0] in ("fetch", "merge") or args[:2] == ("diff", "origin/main")
            ]
            self.assertEqual(
                ops,
                [
                    ("fetch", "-q", "origin", "refs/heads/pins/fork-a"),
                    ("merge", "--no-ff", "--no-edit", "a" * 40),
                    ("fetch", "-q", "origin", "refs/heads/pins/fork-b"),
                    ("merge", "--no-ff", "--no-edit", "b" * 40),
                    ("diff", "origin/main", "--name-only"),
                ],
            )
            self.assertIn("merge pins/fork-a " + "a" * 7, pr.call_args.args[5])
            self.assertIn("merge pins/fork-b " + "b" * 7, pr.call_args.args[5])


class ServerBuildWithoutGradle(unittest.TestCase):
    """Server contract preflight runs on the owner PC without Gradle: the
    Game image jar is built from mod-server in CI, so the local build reads
    the contract with an explicit skip instead of the built jar."""

    def fake_just(self, calls):
        def run(tree, *args):
            calls.append(args)
            if args[0] == "build" and "--out" in args:
                out = Path(args[list(args).index("--out") + 1])
                out.mkdir(parents=True, exist_ok=True)
                (out / "build.json").write_text(
                    json.dumps(
                        {
                            "compatibility": PROD,
                            "client": "c" * 64,
                            "client_version": "v9.9.9",
                        }
                    )
                )

        return run

    def test_server_build_passes_the_explicit_jar_skip(self):
        cache = Path(tempfile.mkdtemp(prefix="bf-release-build-"))
        self.addCleanup(shutil.rmtree, cache, True)
        calls = []
        with (
            mock.patch.object(release, "CACHE", cache),
            mock.patch.object(release, "just", side_effect=self.fake_just(calls)),
        ):
            meta = release.build("server", Path("/tmp/tree"), "v9.9.9")
        self.assertEqual(meta["compatibility"], PROD)
        finals = [args for args in calls if args[0] == "build"]
        self.assertEqual(len(finals), 1)
        self.assertIn("--no-server-jar", finals[0])

    def test_client_build_does_not_pass_the_server_skip(self):
        cache = Path(tempfile.mkdtemp(prefix="bf-release-build-"))
        self.addCleanup(shutil.rmtree, cache, True)
        calls = []

        def run(tree, *args):
            calls.append(args)
            if args[0] == "build" and "--out" in args:
                out = Path(args[list(args).index("--out") + 1])
                (out / "web").mkdir(parents=True, exist_ok=True)
                (out / "web/build.json").write_text(json.dumps({"compatibility": PROD}))

        with (
            mock.patch.object(release, "CACHE", cache),
            mock.patch.object(release, "just", side_effect=run),
        ):
            release.build("client", Path("/tmp/tree"), "v9.9.9")
        finals = [args for args in calls if args[0] == "build"]
        self.assertEqual(len(finals), 1)
        self.assertNotIn("--no-server-jar", finals[0])


class PushTagGateTest(unittest.TestCase):
    def gated(self, *extra):
        seen = []

        def gate(tags, *a):
            seen.append(dict(tags))
            raise SystemExit("stop before git")

        with mock.patch.object(release, "require_rc_gate", side_effect=gate):
            with self.assertRaises(SystemExit):
                release.push_tag(
                    "server", "v2.79.6", False, "s" * 40, "rc.json", None, *extra
                )
        return seen

    def test_pair_record_is_checked_against_the_whole_release(self):
        # A pair RC record never equals one side; gating {side: tag} refused every pair tag.
        pair = {"server": "v2.79.6", "client": "v2.79.3"}
        self.assertEqual(self.gated(pair), [pair])

    def test_single_side_defaults_to_its_own_tag(self):
        self.assertEqual(self.gated(), [{"server": "v2.79.6"}])


class FfNextToMain(unittest.TestCase):
    """`release ship` fast-forwards origin/next to origin/main (bare fixture)."""

    def fixture(self, with_next=True, next_extra=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        origin = base / "origin.git"

        def git(repo, *args):
            return subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=t@invalid",
                    "-c",
                    "user.name=t",
                    "-c",
                    "commit.gpgsign=false",
                    *args,
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            ).stdout.strip()

        git(base, "init", "--bare", "-q", "origin.git")
        git(base, "clone", "-q", str(origin), "seed")
        seed = base / "seed"
        (seed / "file.txt").write_text("one\n")
        git(seed, "add", "file.txt")
        git(seed, "commit", "-qm", "one")
        git(seed, "push", "-q", "origin", "HEAD:main")
        if with_next:
            git(seed, "push", "-q", "origin", "HEAD:next")
        (seed / "file.txt").write_text("two\n")
        git(seed, "commit", "-qam", "two")
        git(seed, "push", "-q", "origin", "HEAD:main")
        if next_extra:
            git(seed, "fetch", "-q", "origin")
            git(seed, "checkout", "-q", "next")
            (seed / "other.txt").write_text("fork\n")
            git(seed, "add", "other.txt")
            git(seed, "commit", "-qm", "fork")
            git(seed, "push", "-q", "origin", "HEAD:next")
        git(base, "clone", "-q", "--branch", "main", str(origin), "side")
        return base / "side"

    def remote(self, side, ref):
        out = subprocess.run(
            ["git", "-C", str(side), "ls-remote", "origin", ref],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        return out.split()[0]

    def test_stale_next_moves_to_main(self):
        side = self.fixture()
        self.assertNotEqual(
            self.remote(side, "refs/heads/next"), self.remote(side, "refs/heads/main")
        )
        with mock.patch.object(release, "repo_dir", return_value=side):
            self.assertTrue(release.ff_next_to_main("server", False))
        self.assertEqual(
            self.remote(side, "refs/heads/next"), self.remote(side, "refs/heads/main")
        )

    def test_dry_run_moves_nothing(self):
        side = self.fixture()
        before = self.remote(side, "refs/heads/next")
        with mock.patch.object(release, "repo_dir", return_value=side):
            self.assertTrue(release.ff_next_to_main("server", True))
        self.assertEqual(self.remote(side, "refs/heads/next"), before)

    def test_synced_next_is_a_noop(self):
        side = self.fixture()
        with mock.patch.object(release, "repo_dir", return_value=side):
            self.assertTrue(release.ff_next_to_main("server", False))
            self.assertTrue(release.ff_next_to_main("server", False))

    def test_diverged_next_raises_and_moves_nothing(self):
        side = self.fixture(next_extra=True)
        before = self.remote(side, "refs/heads/next")
        with mock.patch.object(release, "repo_dir", return_value=side):
            with self.assertRaisesRegex(RuntimeError, "refusing to move next"):
                release.ff_next_to_main("server", False)
        self.assertEqual(self.remote(side, "refs/heads/next"), before)

    def test_missing_next_is_a_noop(self):
        side = self.fixture(with_next=False)
        with mock.patch.object(release, "repo_dir", return_value=side):
            self.assertFalse(release.ff_next_to_main("server", False))

    def test_report_only_names_exact_command_and_pushes_nothing(self):
        side = self.fixture()
        before = self.remote(side, "refs/heads/next")
        main = self.remote(side, "refs/heads/main")
        said = []
        with (
            mock.patch.object(release, "repo_dir", return_value=side),
            mock.patch.object(release, "say", side_effect=said.append),
        ):
            self.assertFalse(release.ff_next_to_main("server", False, report_only=True))
        self.assertEqual(self.remote(side, "refs/heads/next"), before)
        self.assertIn(f"push origin {main}:refs/heads/next", "\n".join(said))

    def test_report_only_synced_is_a_quiet_ok(self):
        side = self.fixture()
        with mock.patch.object(release, "repo_dir", return_value=side):
            self.assertTrue(release.ff_next_to_main("server", False))
        said = []
        with (
            mock.patch.object(release, "repo_dir", return_value=side),
            mock.patch.object(release, "say", side_effect=said.append),
        ):
            self.assertTrue(release.ff_next_to_main("server", False, report_only=True))
        self.assertIn("already at", "\n".join(said))

    def test_report_only_diverged_reports_without_raising(self):
        side = self.fixture(next_extra=True)
        before = self.remote(side, "refs/heads/next")
        said = []
        with (
            mock.patch.object(release, "repo_dir", return_value=side),
            mock.patch.object(release, "say", side_effect=said.append),
        ):
            self.assertFalse(release.ff_next_to_main("server", False, report_only=True))
        self.assertEqual(self.remote(side, "refs/heads/next"), before)
        self.assertIn("refusing to move next", "\n".join(said))


if __name__ == "__main__":
    unittest.main()
