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
        self.assertEqual((kind, order), ("server-only", [("tag", "server")]))

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
    def test_newer_server_mod_pin_warns_when_it_overrides_the_client_one(self):
        root = Path(tempfile.mkdtemp(prefix="bf-release-pin-"))
        self.addCleanup(shutil.rmtree, root, True)
        for path in ("client", "server", "localmods/blockfield-mod"):
            (root / path / ".git").mkdir(parents=True)
        shas = {
            "v2.69.1": "c" * 40,
            "v2.69.2": "d" * 40,
            "v1.47.1": "e" * 40,
            "v1.47.2": "f" * 40,
        }
        pinned = {"client": "a" * 40, "server": "b" * 40}

        def fake_git(repo, *args, **kw):
            if "ls-tree" in args:
                path = args[-1]
                sha = pinned.get(path, "0" * 40)
                return f"160000 commit {sha}\t{path}"
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
        self.assertTrue(
            any(
                "localmods/blockfield-mod" in line
                and "mod 1.47.2 overrides mod 1.47.1" in line
                for line in said
            ),
            said,
        )


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

    def test_single_side_still_requires_full_tag_run_success(self):
        for side in release.SIDES:
            calls = []
            tags = {side: "v2.77.0"}
            patches = self.base_patches(
                calls, (tags, f"{side}-only", [("tag", side)], "")
            )
            self.assertTrue(
                self.run_patches(
                    patches, lambda: release.cmd_ship(self.ship_args(only=side))
                )
            )
            self.assertIn(("watched", side), calls)
            self.assertNotIn(("published", side), calls)
            self.assertFalse(any(c[0] == "deploy" for c in calls))

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
                side_effect=lambda side, tag, dry, expected_sha=None: (
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
                "green",
                "merge",
                "tag",
                "green",
                "merge",
                "run",
                "gate",
                "tag",
                "watched",
                "run",
                "watched",
                "pin",
                "green",
                "merge",
            ],
        )
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
                ("green", release.REPO["client"]),
                ("merge", release.REPO["client"], "a" * 40),
                ("tag", "client"),
                ("run", "client"),
                ("published", "client"),
                ("candidate",),
                ("green", release.REPO["server"], "candidate-sha"),
                ("merge", release.REPO["server"], "candidate-sha"),
                ("tag", "server"),
                ("run", "server"),
                ("published", "server"),
                ("prerelease", tags["client"], True),
                ("deploy", "server", {"client_tag": tags["client"], "dry": False}),
                ("deploy", "client", {"dry": False}),
                ("verify",),
                ("pin",),
                ("green", release.WORKSPACE_REPO),
                ("merge", release.WORKSPACE_REPO, "a" * 40),
            ],
        )

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
        ):
            release.ship_tag_side(
                SimpleNamespace(dry_run=False),
                "client",
                "v2.77.0",
                1,
                0,
                publish_only=True,
            )
        self.assertEqual(merge.call_args.args[-2:], ("--match-head-commit", head))
        tag.assert_called_once_with("client", "v2.77.0", False, merged)

    def test_tag_rejects_unrelated_main(self):
        with (
            mock.patch.object(release, "repo_dir", return_value=Path("repo")),
            mock.patch.object(release, "git", return_value="c" * 40) as git,
            mock.patch.object(release, "preflight") as preflight,
        ):
            with self.assertRaisesRegex(RuntimeError, "differs from the merged commit"):
                release.push_tag("client", "v2.77.0", False, "b" * 40)
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
                    release.push_tag("server", "v2.77.0", False)
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
                return json.dumps(data) if args[0] == "api" else ""

            with (
                mock.patch.object(release, "gh", side_effect=gh),
                mock.patch.object(
                    release.tempfile,
                    "TemporaryDirectory",
                    return_value=contextlib.nullcontext(tmp),
                ),
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
        success.assert_called_once_with("server", 12, 1, 0)

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


if __name__ == "__main__":
    unittest.main()
