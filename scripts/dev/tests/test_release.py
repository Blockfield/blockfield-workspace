import contextlib
import hashlib
import json
import os
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
DIGEST = "sha256:" + "d" * 64


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
        # Neither tag run deploys on its own: the Game deploy is an explicit
        # gameplay dispatch and the client deploy a deploy-client dispatch.
        self.assertEqual(
            (kind, order), ("server-only", [("tag", "server"), ("deploy", "server")])
        )
        kind, order = release.plan({"client": True}, {"client": PROD}, PROD)
        self.assertEqual(
            (kind, order), ("client-only", [("tag", "client"), ("deploy", "client")])
        )

    def test_gameplay_change_tags_gameplay_first(self):
        kind, order = release.plan(
            {"server": True, "gameplay": True}, {"server": PROD}, PROD
        )
        self.assertEqual(
            (kind, order),
            (
                "server-only",
                [("tag-gameplay", "server"), ("tag", "server"), ("deploy", "server")],
            ),
        )
        compat = {"client": MOVED, "server": MOVED}
        kind, order = release.plan(
            {"client": True, "server": True, "gameplay": True}, compat, PROD
        )
        self.assertEqual(kind, "pair")
        self.assertEqual(
            order,
            [
                ("tag", "client"),
                ("candidate", "server"),
                ("tag-gameplay", "server"),
                ("tag", "server"),
                ("deploy-pair", "server"),
                ("deploy", "client"),
            ],
        )

    def test_server_only_dry_run_names_the_digest_dispatch(self):
        tags = {"server": "v2.79.4"}
        lines = release.tag_lines(
            [("tag", "server"), ("deploy", "server")], tags, "release/server-v2.79.4"
        )
        dispatch = [line for line in lines if "deploy.yml/dispatches" in line]
        self.assertEqual(len(dispatch), 1, lines)
        self.assertIn("blockfield-gameplay/actions/workflows/deploy.yml", dispatch[0])
        self.assertIn("inputs[digest]=", dispatch[0])
        self.assertNotIn("client_tag", dispatch[0])
        self.assertNotIn("inputs[tag]", dispatch[0])

    def test_independent_pair_tags_server_first(self):
        compat = {"client": PROD, "server": PROD}
        kind, order = release.plan({"client": True, "server": True}, compat, PROD)
        self.assertEqual(kind, "independent")
        self.assertEqual(
            order,
            [
                ("tag", "server"),
                ("deploy", "server"),
                ("tag", "client"),
                ("deploy", "client"),
            ],
        )

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


class ContractChecks(unittest.TestCase):
    def meta(self, compat, client="c" * 64):
        return {
            "compatibility": compat,
            "client": client,
            "client_version": "2.77.0",
        }

    def prod(self, compat, client="p" * 64):
        return {"compatibility": compat, "client": client, "build": "v2.76.0"}

    def test_same_contract_client_drift_warns_without_failing(self):
        # Same contract: server deploys and rolls back alone, no client_tag.
        res = release.contract_checks("server", self.meta(PROD), self.prod(PROD))
        self.assertTrue(res)
        self.assertEqual([level for level, _ in res if level == "FAIL"], [])
        self.assertTrue(
            any(
                level == "WARN" and "without client_tag" in text for level, text in res
            ),
            res,
        )

    def test_moved_contract_still_needs_client_tag(self):
        res = release.contract_checks("server", self.meta(MOVED), self.prod(PROD))
        self.assertEqual([level for level, _ in res if level == "FAIL"], [])
        self.assertTrue(
            any("client_tag" in text for _, text in res),
            res,
        )

    def test_unchanged_contract_is_ok(self):
        prod = self.prod(PROD)
        res = release.contract_checks(
            "server", self.meta(PROD, client=prod["client"]), prod
        )
        self.assertEqual([level for level, _ in res if level in ("FAIL", "WARN")], [])


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
        with mock.patch.object(release, "run", side_effect=[ok, ok, bad]) as run:
            held = release.held_issues()
        self.assertEqual(len(held), 203)
        self.assertIn(
            "blockfield-client#101 task 101 https://example.com/101", held[100]
        )
        self.assertIn("blockfield-gameplay: could not list", held[-1])
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
    def run_pin(self, pinned, dedicated_gameplay="v2.69.3"):
        root = self.root = Path(tempfile.mkdtemp(prefix="bf-release-pin-"))
        self.addCleanup(shutil.rmtree, root, True)
        for path in (
            "client",
            "server",
            "gameplay",
            "localmods/blockfield-mod",
            "pins",
        ):
            (root / path / ".git").mkdir(parents=True)
        shas = {
            "v2.69.1": "c" * 40,
            "v2.69.2": "d" * 40,
            "v2.69.3": "g" * 40,
            "v1.47.1": "e" * 40,
            "v1.47.2": "f" * 40,
        }

        def fake_git(repo, *args, **kw):
            if "ls-tree" in args:
                path = args[-1]
                if path in ("client", "localmods/blockfield-mod"):
                    sha = pinned.get(path, "0" * 40)
                    return f"160000 commit {sha}\t{path}" if sha else ""
                return ""
            if "show" in args:
                ref = args[-1]
                if "distribution/gameplay.lock.json" in ref:
                    return json.dumps({"tag": dedicated_gameplay})
                pin_path = ref.split(":", 1)[-1]
                content = pinned.get(pin_path)
                return mock.Mock(
                    returncode=0 if content else 128,
                    stdout=(content + "\n" if content else ""),
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
        said = self.run_pin(
            {
                "client": "a" * 40,
                "pins/dedicated.sha": "b" * 40,
                "pins/gameplay.sha": "g" * 40,
                "localmods/blockfield-mod": "a" * 40,
            }
        )
        self.assertTrue(
            any(
                "localmods/blockfield-mod" in line
                and "mod 1.47.2 overrides mod 1.47.1" in line
                for line in said
            ),
            said,
        )

    def test_dedicated_and_gameplay_pin_files_advance(self):
        said = self.run_pin(
            {
                "client": "c" * 40,
                "pins/dedicated.sha": "b" * 40,
                "pins/gameplay.sha": "b" * 40,
                "localmods/blockfield-mod": "e" * 40,
            }
        )
        self.assertEqual(
            (self.root / "pins/dedicated.sha").read_text(encoding="utf-8"),
            "d" * 40 + "\n",
        )
        self.assertEqual(
            (self.root / "pins/gameplay.sha").read_text(encoding="utf-8"),
            "g" * 40 + "\n",
        )
        self.assertTrue(any("dedicated 2.69.2" in line for line in said), said)

    def test_unpinned_dedicated_is_skipped(self):
        said = self.run_pin(
            {
                "client": "a" * 40,
                "pins/dedicated.sha": None,
                "pins/gameplay.sha": "g" * 40,
                "localmods/blockfield-mod": "e" * 40,
            }
        )
        self.assertIn("skip pins/dedicated.sha: the workspace does not pin it", said)


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

    def test_server_only_ships_game_with_digest_dispatch(self):
        calls = []
        tags = {"server": "v2.77.0"}
        patches = self.base_patches(
            calls, (tags, "server-only", [("tag", "server"), ("deploy", "server")], "")
        )
        self.assertTrue(
            self.run_patches(
                patches, lambda: release.cmd_ship(self.ship_args(only="server"))
            )
        )
        staged = [
            c
            for c in calls
            if c[0]
            in (
                "tag",
                "run",
                "published",
                "watched",
                "digest",
                "deploy",
                "probes",
                "pin",
            )
        ]
        # A plain dedicated tag, then one gameplay deploy dispatch with the
        # image digest (no client_tag: the contract did not move). No Workshop
        # staging (published) and no Workshop move (approve).
        self.assertEqual(
            staged,
            [
                ("tag", "server"),
                ("run", "server"),
                ("watched", "server"),
                ("digest", "v2.77.0"),
                ("deploy", "server", DIGEST, {"dry": False}),
                ("probes", "server"),
                ("pin",),
            ],
        )
        self.assertFalse(any(c[0] == "published" for c in calls))
        self.assertFalse(any(c[0] == "approve" for c in calls))
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

        def prepare_client(side, args, prod, manifest, repin):
            calls.append(("prepare", side))
            return tags[side], {"compatibility": MOVED}

        def prepare_server(args, prod, manifest, repin):
            calls.append(("prepare", "server"))
            self.assertIsNotNone(manifest)
            self.assertFalse(repin)
            return tags["server"], {"compatibility": MOVED}, None, False

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(release, "CACHE", Path(tmp)),
            mock.patch.object(
                release, "prod_state", return_value={"compatibility": PROD}
            ),
            mock.patch.object(release, "say_held"),
            mock.patch.object(release, "prepare_side", side_effect=prepare_client),
            mock.patch.object(release, "prepare_server", side_effect=prepare_server),
            mock.patch.object(release, "cmd_candidate", return_value="candidate-sha"),
        ):
            self.assertTrue(
                self.run_patches(
                    patches, lambda: release.cmd_ship(self.ship_args(server_merge=None))
                )
            )
        # next is healed before prepare cuts a release from it.
        self.assertEqual(
            calls[:5],
            [
                ("ff", "client"),
                ("ff", "server"),
                ("ff", "gameplay"),
                ("prepare", "client"),
                ("prepare", "server"),
            ],
        )
        self.assertLess(
            calls.index(("prepare", "server")), calls.index(("tag", "client"))
        )
        self.assertIn(("green", release.REPO["server"], "candidate-sha"), calls)

    def test_no_repo_waits_for_an_image_job(self):
        # The dedicated check workflow has a single `check` job: a required
        # `image` job could never turn green and ship would wait forever.
        head = "a" * 40
        data = {
            "headRefOid": head,
            "statusCheckRollup": [
                {"name": "check", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
        }
        for repo in release.REPO.values():
            with (
                self.subTest(repo=repo),
                mock.patch.object(release, "pr_number", return_value=7),
                mock.patch.object(release, "gh", return_value=json.dumps(data)),
            ):
                self.assertEqual(
                    release.wait_pr_green(repo, "release/x-1.0.0", 0, 0, head),
                    (7, head),
                )

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
        shas = {"server": "s" * 40, "client": "c" * 40, "gameplay": "g" * 40}
        runs = {
            "server": {"databaseId": 11, "url": "https://example.com/s"},
            "client": {"databaseId": 12, "url": "https://example.com/c"},
            "gameplay": {"databaseId": 13, "url": "https://example.com/g"},
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
                "image_digest",
                side_effect=lambda tag: calls.append(("digest", tag)) or DIGEST,
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
                    ("deploy", side, tag, kw)
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

    def test_unsynced_next_stops_ship_before_prepare(self):
        calls = []
        tags = {"server": "v2.76.3", "client": "v2.76.3"}
        patches = self.base_patches(calls, (tags, "pair", [], ""))
        prepare = mock.Mock(return_value=(tags, "pair", [], ""))
        patches = [
            p for p in patches if p.attribute not in ("ff_next_to_main", "prepare_all")
        ] + [
            mock.patch.object(release, "prepare_all", prepare),
            mock.patch.object(release, "ff_next_to_main", return_value=False),
            mock.patch.object(release, "has_next", return_value=True),
            mock.patch.object(release, "repo_dir", side_effect=lambda side: side),
        ]
        with self.assertRaises(SystemExit):
            self.run_patches(patches, lambda: release.cmd_ship(self.ship_args()))
        prepare.assert_not_called()

    def test_independent_ships_every_side_server_before_client(self):
        # Kind decides the flow, not len(tags): client + dedicated + gameplay is
        # still an independent release and every side ships.
        for with_gameplay in (False, True):
            with self.subTest(gameplay=with_gameplay):
                calls = []
                tags = {"server": "v2.76.3", "client": "v2.76.3"}
                if with_gameplay:
                    tags["gameplay"] = "v2.83.0"
                patches = self.base_patches(
                    calls,
                    (tags, "independent", [("tag", "server"), ("tag", "client")], ""),
                )
                self.assertTrue(
                    self.run_patches(
                        patches, lambda: release.cmd_ship(self.ship_args())
                    )
                )
                head = "a" * 40
                gameplay = (
                    [
                        ("green", release.REPO["gameplay"]),
                        ("merge", release.REPO["gameplay"], head),
                        ("ff", "gameplay"),
                        ("tag", "gameplay"),
                        ("run", "gameplay"),
                        ("watched", "gameplay"),
                    ]
                    if with_gameplay
                    else []
                )
                self.assertEqual(
                    calls,
                    [
                        ("ff", "client"),
                        ("ff", "server"),
                        ("ff", "gameplay"),
                        ("rc-gate",),
                        *gameplay,
                        ("green", release.REPO["server"]),
                        ("merge", release.REPO["server"], head),
                        ("ff", "server"),
                        ("tag", "server"),
                        ("run", "server"),
                        ("watched", "server"),
                        ("digest", "v2.76.3"),
                        ("deploy", "server", DIGEST, {"dry": False}),
                        ("green", release.REPO["client"]),
                        ("merge", release.REPO["client"], head),
                        ("ff", "client"),
                        ("tag", "client"),
                        ("run", "client"),
                        ("watched", "client"),
                        ("deploy", "client", "v2.76.3", {"dry": False}),
                        ("probes", "client"),
                        ("pin",),
                        ("green", release.WORKSPACE_REPO),
                        ("merge", release.WORKSPACE_REPO, head),
                        *[("ff", side) for side in tags],
                    ],
                )

    def test_rc_gate_covers_release_sides_not_gameplay(self):
        calls = []
        tags = {"server": "v2.76.3", "client": "v2.76.3", "gameplay": "v2.83.0"}
        patches = [
            p
            for p in self.base_patches(
                calls,
                (tags, "independent", [("tag", "server"), ("tag", "client")], ""),
            )
            if p.attribute != "require_rc_gate"
        ]
        gate = mock.Mock(return_value=True)
        patches.append(mock.patch.object(release, "require_rc_gate", gate))
        self.run_patches(patches, lambda: release.cmd_ship(self.ship_args()))
        self.assertEqual(
            gate.call_args.args[0], {"client": "v2.76.3", "server": "v2.76.3"}
        )

    def test_deferred_gameplay_is_tagged_then_prepare_reruns(self):
        calls = []
        deferred = ({"gameplay": "v2.83.0"}, "deferred", [], "")
        server = ({"server": "v2.83.1"}, "server-only", [("tag", "server")], "")
        # base_patches checks each run's tag against its prepared tags.
        both = ({**server[0], **deferred[0]}, *server[1:])
        patches = [
            p for p in self.base_patches(calls, both) if p.attribute != "prepare_all"
        ]
        prepare = mock.Mock(side_effect=[deferred, server])
        patches.append(mock.patch.object(release, "prepare_all", prepare))
        self.assertTrue(
            self.run_patches(patches, lambda: release.cmd_ship(self.ship_args()))
        )
        self.assertEqual(prepare.call_count, 2)
        tagged = [c[1] for c in calls if c[0] == "tag"]
        self.assertEqual(tagged, ["gameplay", "server"])
        self.assertLess(
            calls.index(("run", "gameplay")), calls.index(("tag", "server"))
        )
        self.assertEqual([c[0] for c in calls].count("deploy"), 1)

    def test_heal_covers_every_status_side(self):
        calls = []
        tags = {"server": "v2.77.0"}
        prepared = (tags, "server-only", [("tag", "server")], "")
        patches = [
            p
            for p in self.base_patches(calls, prepared)
            if p.attribute != "ff_next_to_main"
        ]
        healed = []
        patches.append(
            mock.patch.object(
                release,
                "ff_next_to_main",
                side_effect=lambda side, dry, **kw: healed.append(side) or True,
            )
        )
        self.run_patches(patches, lambda: release.cmd_ship(self.ship_args()))
        self.assertEqual(healed[:3], ["client", "server", "gameplay"])

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
                ("ff", "gameplay"),
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
                ("digest", tags["server"]),
                (
                    "deploy",
                    "server",
                    DIGEST,
                    {"client_tag": tags["client"], "dry": False},
                ),
                ("probes", "server"),
                ("deploy", "client", tags["client"], {"dry": False}),
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
        self.assertEqual(
            calls[:3], [("ff", "client"), ("ff", "server"), ("ff", "gameplay")]
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
            "release SUCCESS",
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
        self.assertEqual(order[-1], ("deploy", "server"))
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
        # Dedicated tag, then the tag-run release publishes the manifests (no
        # automatic Deploy) and one gameplay digest dispatch serves the game.
        self.assertEqual(text.count("deploy.yml/dispatches"), 1)
        self.assertIn("blockfield-gameplay/actions/workflows/deploy.yml", text)
        self.assertIn("inputs[digest]=", text)
        self.assertNotIn("client_tag", text)
        self.assertIn("dedicated v2.77.0 tag push run", text)
        self.assertIn("release SUCCESS publishes", text)
        self.assertIn("green workspace pin-PR", text)
        self.assertNotIn("move-approved-tag", text)

    def test_publication_accepts_expected_compatibility_refusal(self):
        # Dedicated tag runs publish without an automatic Deploy: a completed
        # release job is publication (the Game deploy is an explicit dispatch).
        jobs = [{"name": "release", "conclusion": "success"}]
        data = {
            "jobs": jobs,
            "status": "completed",
            "conclusion": "success",
            "url": "run-url",
        }
        with (
            mock.patch.object(
                release,
                "gh",
                side_effect=lambda *a: json.dumps(data),
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
        failed = {
            "jobs": [{"name": "release", "conclusion": "failure"}],
            "status": "completed",
            "conclusion": "failure",
            "url": "run-url",
        }
        with (
            mock.patch.object(
                release,
                "gh",
                return_value=json.dumps(failed),
            ),
            mock.patch.object(release, "verify_publication") as verify,
        ):
            with self.assertRaises(RuntimeError):
                release.wait_publication("server", "v2.77.0", 1, 1, 0)
            verify.assert_not_called()


class DispatchGrace(unittest.TestCase):
    """A just-dispatched run ID can 404 briefly; wait_run_success retries it."""

    RUN_ID = 123456789
    NOT_FOUND = RuntimeError(
        "gh run view 123456789 --repo R --json status,conclusion,url\n"
        "HTTP 404: Not Found (https://api.github.com/repos/R/actions/runs/123456789)"
    )

    def payload(self, status, conclusion):
        return json.dumps(
            {
                "status": status,
                "conclusion": conclusion,
                "url": "https://example.invalid/run",
            }
        )

    def test_404_then_success_is_pending_not_failure(self):
        with mock.patch.object(
            release,
            "gh",
            side_effect=[
                self.NOT_FOUND,
                self.NOT_FOUND,
                self.payload("in_progress", ""),
                self.payload("completed", "SUCCESS"),
            ],
        ) as gh:
            release.wait_run_success("server", self.RUN_ID, 120, 0, "R")
        self.assertEqual(gh.call_count, 4)

    def test_persistent_404_fails_with_pointer_inside_grace(self):
        now = [1000.0]
        with (
            mock.patch.object(release, "gh", side_effect=self.NOT_FOUND),
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch("time.sleep", lambda s: now.__setitem__(0, now[0] + s)),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "123456789.*still not visible.*do not retry"
            ):
                release.wait_run_success("server", self.RUN_ID, 1800, 5, "R")

    def test_late_404_after_long_visible_wait_gets_fresh_grace(self):
        now = [1000.0]
        replies = [self.payload("in_progress", "")] * 30 + [
            self.NOT_FOUND,
            self.payload("completed", "SUCCESS"),
        ]
        with (
            mock.patch.object(release, "gh", side_effect=replies) as gh,
            mock.patch("time.monotonic", lambda: now[0]),
            mock.patch("time.sleep", lambda s: now.__setitem__(0, now[0] + s)),
        ):
            release.wait_run_success("server", self.RUN_ID, 1800, 5, "R")
        self.assertEqual(gh.call_count, 32)

    def test_other_errors_raise_at_once(self):
        with mock.patch.object(
            release,
            "gh",
            side_effect=RuntimeError("gh run view 123456789\nHTTP 401: Unauthorized"),
        ) as gh:
            with self.assertRaisesRegex(RuntimeError, "Unauthorized"):
                release.wait_run_success("server", self.RUN_ID, 120, 0, "R")
        self.assertEqual(gh.call_count, 1)

    def test_failed_conclusion_still_raises(self):
        with mock.patch.object(
            release, "gh", return_value=self.payload("completed", "FAILURE")
        ):
            with self.assertRaisesRegex(RuntimeError, "FAILURE"):
                release.wait_run_success("server", self.RUN_ID, 120, 0, "R")


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
        for action, client_tag in (("deploy-pair", tags["client"]), ("deploy", None)):
            lines = release.tag_lines([(action, "server")], tags, "")
            line = next(line for line in lines if "deploy.yml/dispatches" in line)
            argv = shlex.split(line.split("   #", 1)[0])
            # The dry run prints the argv dispatch_deploy runs, digest placeholder aside.
            slot = next(
                i for i, a in enumerate(argv) if a.startswith("inputs[digest]=")
            )
            argv[slot] = f"inputs[digest]={DIGEST}"
            self.assertEqual(
                tuple(argv),
                release.deploy_command("server", DIGEST, client_tag=client_tag),
            )
        line = next(
            line
            for line in release.tag_lines([("deploy", "client")], tags, "")
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
            release.dispatch_deploy("client", tags["client"], 1, 0)
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
        text = texts["client/.github/workflows/client.yml"]
        self.assertLess(
            text.index("- name: Create prerelease"), text.index("- name: Release text")
        )
        self.assertLess(
            text.index("- name: Release text"),
            text.index("- name: Publish release notes"),
        )
        dedicated = texts["server/.github/workflows/release.yml"]
        for piece in (
            "Publish GitHub release assets",
            "--generate-notes",
            "dist/build.json",
            "dist/build-workshop.json",
            "dist/SHA256SUMS",
        ):
            self.assertIn(piece, dedicated)
        gameplay_release = texts["gameplay/.github/workflows/release.yml"]
        for piece in (
            "Publish GitHub release assets",
            "--generate-notes",
            "dist/pinned-mods.json",
            "dist/SHA256SUMS",
        ):
            self.assertIn(piece, gameplay_release)
        self.assertIn(
            "Validate target and remember previous pair",
            texts["gameplay/.github/workflows/deploy.yml"],
        )
        self.assertIn(
            "Server moves the client contract: rerun Deploy server with client_tag",
            texts["gameplay/scripts/prepare-server-deploy.py"],
        )
        # The dispatch argv names only inputs the deploy workflows declare.
        for side, workflow, extra in (
            ("server", "deploy.yml", {"client_tag": "v2.77.0"}),
            ("client", "deploy-client.yml", {}),
        ):
            cmd = release.deploy_command(side, DIGEST, **extra)
            self.assertIn(
                f"repos/{release.REPO['gameplay']}/actions/workflows/{workflow}/dispatches",
                cmd,
            )
            declared = texts[f"gameplay/.github/workflows/{workflow}"]
            for arg in cmd:
                if arg.startswith("inputs["):
                    name = arg[len("inputs[") : arg.index("]")]
                    self.assertIn(f"      {name}:\n", declared, (workflow, name))
        # Client tag run: check publishes, automatic Deploy stages.
        parent = texts["client/.github/workflows/client.yml"].split("jobs:\n", 1)[1]
        child = texts["client/.github/workflows/deploy.yml"]
        child_job = re.search(r"^  (\w+):$", child.split("jobs:\n", 1)[1], re.M)[1]
        step = next(
            block.splitlines()[0]
            for block in child.split("      - name: ")[1:]
            if "scripts/deploy-client.py" in block
        )
        jobs = [
            {"name": name, "conclusion": "success"}
            for name in re.findall(r"^  (\w+):$", parent, re.M)
        ]
        deploy = next(job for job in jobs if job["name"] == "deploy")
        deploy.update(
            name=f"deploy / {child_job}",
            conclusion="success",
            steps=[{"name": step, "conclusion": "success"}],
        )
        data = {
            "jobs": jobs,
            "status": "completed",
            "conclusion": "success",
            "url": "url",
        }
        with (
            mock.patch.object(
                release,
                "gh",
                return_value=json.dumps(data),
            ),
            mock.patch.object(release, "verify_publication", return_value="verified"),
        ):
            self.assertEqual(
                release.wait_publication("client", "v2.77.0", 12, 1, 0), "verified"
            )
        # Dedicated tag run: release publishes the manifests, no automatic Deploy.
        jobs = [{"name": "release", "conclusion": "success"}]
        data = {
            "jobs": jobs,
            "status": "completed",
            "conclusion": "success",
            "url": "url",
        }
        with (
            mock.patch.object(
                release,
                "gh",
                return_value=json.dumps(data),
            ),
            mock.patch.object(release, "verify_publication", return_value="verified"),
        ):
            self.assertEqual(
                release.wait_publication("server", "v2.77.0", 12, 1, 0), "verified"
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
        for side, names in (("client", ["check"]), ("server", ["release"])):
            jobs = [{"name": name, "conclusion": "success"} for name in names]
            if side == "client":
                jobs.append({"name": "deploy / deploy", "conclusion": "success"})
            data = {
                "status": "in_progress",
                "conclusion": "",
                "url": "running-tag",
                "jobs": jobs,
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
                if side == "client":
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

    def test_dedicated_release_needs_manifests_not_notes(self):
        data = {"draft": False, "tag_name": "v2.78.0", "assets": []}
        with mock.patch.object(release, "gh", return_value=json.dumps(data)):
            with self.assertRaisesRegex(RuntimeError, "build.json"):
                release.verify_publication("server", "v2.78.0")
        data["assets"] = [{"name": name} for name in ("release-notes.md",)]
        with mock.patch.object(release, "gh", return_value=json.dumps(data)):
            with self.assertRaisesRegex(RuntimeError, "build.json"):
                release.verify_publication("server", "v2.78.0")

    def test_dedicated_assets_and_manifest_must_match_checksums(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            meta = {"build": "v2.78.0", "compatibility": MOVED}
            build = json.dumps(meta).encode()
            ws = b"workshop"
            sums = {
                "build.json": hashlib.sha256(build).hexdigest(),
                "build-workshop.json": hashlib.sha256(ws).hexdigest(),
            }
            (root / "build.json").write_bytes(build)
            (root / "build-workshop.json").write_bytes(ws)
            (root / "SHA256SUMS").write_text(
                "".join(f"{sha}  {name}\n" for name, sha in sums.items())
            )
            assets = [
                {"name": name, "digest": f"sha256:{sha}"} for name, sha in sums.items()
            ]
            assets += [{"name": "SHA256SUMS"}]
            data = {"tag_name": "v2.78.0", "draft": False, "assets": assets}

            def gh(*args):
                if args[0] == "api":
                    return json.dumps(data)
                if args[:2] == ("release", "download"):
                    destination = Path(args[args.index("-D") + 1])
                    for name in ("build.json", "build-workshop.json", "SHA256SUMS"):
                        shutil.copyfile(root / name, destination / name)
                return ""

            with (
                mock.patch.object(release, "gh", side_effect=gh),
                mock.patch.object(release, "CACHE", root / "fresh" / "cache"),
            ):
                self.assertEqual(release.verify_publication("server", "v2.78.0"), meta)
                assets[0]["digest"] = "sha256:bad"
                with self.assertRaises(RuntimeError):
                    release.verify_publication("server", "v2.78.0")

    def test_running_automatic_deploy_keeps_polling_not_success(self):
        # Backlog 30 race: an unconcluded deploy on an incomplete run must not
        # report publication (2.82.1 slipped through the old early return).
        data = {
            "status": "in_progress",
            "conclusion": "",
            "url": "running-tag",
            "jobs": [
                {"name": "check", "conclusion": "success"},
                {"name": "deploy / deploy", "conclusion": ""},
            ],
        }
        with (
            mock.patch.object(release, "gh", return_value=json.dumps(data)),
            mock.patch.object(release, "verify_publication") as verify,
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                release.wait_publication("client", "v2.78.0", 1, 0, 0)
            verify.assert_not_called()

    def test_publication_accepts_only_successful_publish_jobs(self):
        for side, names in (("client", ["check"]), ("server", ["release"])):
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
                    else "Server moves the client contract: rerun Deploy server with client_tag"
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
            release.dispatch_deploy("server", DIGEST, 1, 0, client_tag="v2.77.0")
        self.assertEqual(
            dispatch.call_args.args,
            (
                False,
                "gh",
                "api",
                "--method",
                "POST",
                "repos/Blockfield/blockfield-gameplay/actions/workflows/deploy.yml/dispatches",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
                "-f",
                "ref=main",
                "-F",
                "return_run_details=true",
                "-f",
                f"inputs[digest]={DIGEST}",
                "-f",
                "inputs[client_tag]=v2.77.0",
            ),
        )
        self.assertEqual(dispatch.call_count, 1)
        success.assert_called_once_with("server", 12, 1, 0, release.REPO["gameplay"])

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
    def test_client_dispatch_targets_gameplay_deploy_client(self):
        self.assertEqual(
            release.deploy_command("client", "v2.77.0"),
            (
                "gh",
                "api",
                "--method",
                "POST",
                "repos/Blockfield/blockfield-gameplay/actions/workflows/deploy-client.yml/dispatches",
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
        success.assert_called_once_with("client", 12, 1, 0, release.REPO["gameplay"])

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
        # Both deploy workflows live in the gameplay repo.
        self.assertEqual(release.deploy_repo("client"), release.REPO["gameplay"])
        self.assertEqual(release.deploy_repo("server"), release.REPO["gameplay"])

    def test_wait_run_success_polls_given_repo(self):
        with mock.patch.object(
            release,
            "gh",
            return_value=json.dumps({"conclusion": "success", "url": "run-url"}),
        ) as gh:
            release.wait_run_success("client", 12, 1, 0, release.REPO["gameplay"])
        repo_flag = gh.call_args.args
        self.assertIn(release.REPO["gameplay"], repo_flag)
        self.assertNotIn(release.REPO["client"], repo_flag)

    def test_server_dispatch_targets_gameplay_deploy_with_digest(self):
        pair = release.deploy_command("server", DIGEST, client_tag="v2.77.0")
        self.assertIn(
            "repos/Blockfield/blockfield-gameplay/actions/workflows/deploy.yml/dispatches",
            pair,
        )
        self.assertIn(f"inputs[digest]={DIGEST}", pair)
        self.assertIn("inputs[client_tag]=v2.77.0", pair)
        plain = release.deploy_command("server", DIGEST)
        self.assertFalse([a for a in plain if "client_tag" in a or "inputs[tag]" in a])

    def test_image_digest_reads_the_ghcr_version_of_the_tag(self):
        versions = [
            {"name": "sha256:" + "1" * 64, "metadata": {"container": {"tags": []}}},
            {"name": DIGEST, "metadata": {"container": {"tags": ["v2.77.0"]}}},
        ]
        with mock.patch.object(release, "gh", return_value=json.dumps(versions)) as gh:
            self.assertEqual(release.image_digest("v2.77.0"), DIGEST)
        self.assertIn("blockfield-dedicated", " ".join(gh.call_args.args))
        with mock.patch.object(release, "gh", return_value=json.dumps(versions)):
            with self.assertRaisesRegex(RuntimeError, "v2.78.0"):
                release.image_digest("v2.78.0")

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

    def test_dedicated_leg_prefights_with_client_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []

            def git(repo, *args):
                events.append(args)
                if args[:2] == ("diff", "origin/main"):
                    return "distribution/gameplay.lock.json"
                return ""

            args = release.parser().parse_args(
                [
                    "prepare",
                    "--mod",
                    "v1.55.3",
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
                mock.patch.object(release, "published_repo", return_value="v2.76.4"),
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
                got, _ = release.prepare_dedicated(
                    args,
                    {"client_version": "2.77.0"},
                    "v2.76.5",
                    "file:///candidate",
                    False,
                )
            self.assertEqual(got, "v2.76.5")
            self.assertEqual(worktree.call_args.args[1], "origin/main")
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
            self.assertIn("fork pins commits since v2.76.4", message)

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
        # --server-merge now applies to the gameplay leg (code/pins live there).
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            events = []
            shas = iter(["a" * 40, "b" * 40])

            def git(repo, *args):
                events.append(args)
                if args[0] == "rev-parse":
                    return next(shas)
                if args[:2] == ("diff", "origin/main"):
                    return "mod-server/gradle.properties"
                return ""

            args = self.prepare_args("--server-merge", "pins/fork-a,pins/fork-b")
            patches = (
                mock.patch.object(release, "repo_dir", return_value=tree),
                mock.patch.object(release, "fetch_main"),
                mock.patch.object(
                    release, "remote_tags", return_value={"v2.76.4": "b" * 40}
                ),
                mock.patch.object(release, "published_repo", return_value="v2.76.4"),
                mock.patch.object(release, "git", side_effect=git),
                mock.patch.object(
                    release, "worktree", return_value=contextlib.nullcontext(tree)
                ),
                mock.patch.object(release, "run", return_value=""),
                mock.patch.object(release, "open_pr"),
            )
            with contextlib.ExitStack() as stack:
                mocks = [stack.enter_context(patch) for patch in patches]
                pr = mocks[-1]
                got = release.prepare_gameplay(args)
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
        tree = Path(tempfile.mkdtemp(prefix="bf-release-tree-"))
        self.addCleanup(shutil.rmtree, tree, True)
        (tree / "server" / "lobby").mkdir(parents=True)
        with (
            mock.patch.object(release, "CACHE", cache),
            mock.patch.object(release, "just", side_effect=self.fake_just(calls)),
            mock.patch.object(release, "fetch_world_seed", return_value=tree / "seed"),
            mock.patch.object(release, "repo_dir", return_value=tree),
        ):
            meta = release.build("server", tree, "v9.9.9")
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

    def fixture(self, with_next=True, next_extra=False, conflict=False):
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
            if conflict:
                (seed / "file.txt").write_text("three\n")
            git(seed, "add", "other.txt", "file.txt")
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

    def test_diverged_next_gets_main_merged_in(self):
        side = self.fixture(next_extra=True)
        before = self.remote(side, "refs/heads/next")
        main = self.remote(side, "refs/heads/main")
        identity = {k: "t" for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")}
        identity |= {k: "t@t" for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")}
        with (
            mock.patch.object(release, "repo_dir", return_value=side),
            mock.patch.dict(os.environ, identity),
        ):
            self.assertTrue(release.ff_next_to_main("server", False))
        after = self.remote(side, "refs/heads/next")
        subprocess.run(["git", "-C", str(side), "fetch", "-q", "origin"], check=True)
        for parent in (before, main):
            self.assertTrue(release.is_ancestor(side, parent, after))

    def test_conflicting_divergence_warns_and_moves_nothing(self):
        side = self.fixture(next_extra=True, conflict=True)
        before = self.remote(side, "refs/heads/next")
        said = []
        with (
            mock.patch.object(release, "repo_dir", return_value=side),
            mock.patch.object(release, "say", side_effect=said.append),
        ):
            self.assertFalse(release.ff_next_to_main("server", False))
        self.assertEqual(self.remote(side, "refs/heads/next"), before)
        self.assertIn("conflicts", "\n".join(said))

    def test_rejected_push_warns_instead_of_raising(self):
        side = self.fixture()
        said = []
        with (
            mock.patch.object(release, "repo_dir", return_value=side),
            mock.patch.object(
                release, "ship_mutate", side_effect=RuntimeError("rejected")
            ),
            mock.patch.object(release, "say", side_effect=said.append),
        ):
            self.assertFalse(release.ff_next_to_main("server", False))
        self.assertIn("could not move origin/next", "\n".join(said))

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
        self.assertIn("diverged", "\n".join(said))

    def test_shallow_clone_unshallows_before_ancestry(self):
        # A shallow submodule clone must still answer the next/main sync
        # check correctly: fetch_main unshallows before is_ancestor/merge-tree.
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
        git(base, "clone", "-q", f"file://{origin}", "seed")
        seed = base / "seed"
        (seed / "file.txt").write_text("one\n")
        git(seed, "add", "file.txt")
        git(seed, "commit", "-qm", "one")
        git(seed, "push", "-q", "origin", "HEAD:main")
        git(seed, "push", "-q", "origin", "HEAD:next")
        (seed / "file.txt").write_text("two\n")
        git(seed, "commit", "-qam", "two")
        git(seed, "push", "-q", "origin", "HEAD:main")
        git(
            base,
            "clone",
            "-q",
            "--depth",
            "1",
            "--no-single-branch",
            f"file://{origin}",
            "side",
        )
        side = base / "side"
        shallow = subprocess.run(
            ["git", "-C", str(side), "rev-parse", "--is-shallow-repository"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertEqual(shallow, "true")
        release.fetch_main(side)
        shallow = subprocess.run(
            ["git", "-C", str(side), "rev-parse", "--is-shallow-repository"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertEqual(shallow, "false")
        main = subprocess.run(
            ["git", "-C", str(side), "rev-parse", "origin/main"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        nxt = subprocess.run(
            ["git", "-C", str(side), "rev-parse", "origin/next"],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        self.assertTrue(release.is_ancestor(side, nxt, main))
        self.assertFalse(release.is_ancestor(side, main, nxt))

    def test_shallow_clone_unshallows_next_ahead_of_main(self):
        # next carrying commits main lacks must be complete as well, or the
        # sync check sees a truncated next and misreads the divergence.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)

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
        git(base, "clone", "-q", f"file://{base / 'origin.git'}", "seed")
        seed = base / "seed"
        (seed / "f").write_text("one\n")
        git(seed, "add", "f")
        git(seed, "commit", "-qm", "one")
        root = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "-q", "origin", "HEAD:main")
        (seed / "n").write_text("next\n")
        git(seed, "add", "n")
        git(seed, "commit", "-qm", "next-a")
        (seed / "n").write_text("next2\n")
        git(seed, "commit", "-qam", "next-b")
        git(seed, "push", "-q", "origin", "HEAD:next")
        git(seed, "checkout", "-q", root)
        (seed / "f").write_text("two\n")
        git(seed, "commit", "-qam", "two")
        git(seed, "push", "-q", "origin", "HEAD:main")
        git(
            base,
            "clone",
            "-q",
            "--depth",
            "1",
            "--no-single-branch",
            f"file://{base / 'origin.git'}",
            "side",
        )
        side = base / "side"
        release.fetch_main(side)
        self.assertEqual(git(side, "rev-parse", "--is-shallow-repository"), "false")
        self.assertEqual(git(side, "rev-list", "--count", "origin/next"), "3")
        self.assertEqual(git(side, "merge-base", "origin/main", "origin/next"), root)


class DeferredPrepare(unittest.TestCase):
    def test_deferred_when_gameplay_unpublished(self):
        args = release.parser().parse_args(["prepare", "--mod", "v1.57.0", "--dry-run"])
        with (
            mock.patch.object(
                release, "prod_state", return_value={"compatibility": PROD}
            ),
            mock.patch.object(release, "say_held"),
            mock.patch.object(release, "prepare_side", return_value=None),
            mock.patch.object(
                release, "prepare_gameplay", return_value=("v2.83.0", True)
            ),
            mock.patch.object(release, "release_published", return_value=False),
            mock.patch.object(
                release, "prepare_dedicated", side_effect=AssertionError("must not pin")
            ),
            mock.patch.object(release, "say"),
        ):
            tags, kind, order, branch = release.prepare_all(args)
        self.assertEqual(kind, "deferred")
        self.assertEqual(order, [])
        self.assertEqual(tags.get("gameplay"), "v2.83.0")

    def test_gameplay_leg_reuses_published_tag_without_new_pr(self):
        args = release.parser().parse_args(["prepare", "--mod", "v1.57.0", "--dry-run"])
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)

            def git(repo, *a):
                if a[0] == "diff":
                    return ""
                if a[0] == "log":
                    return ""
                return ""

            with (
                mock.patch.object(release, "repo_dir", return_value=tree),
                mock.patch.object(release, "fetch_main"),
                mock.patch.object(
                    release, "remote_tags", return_value={"v2.83.0": "a" * 40}
                ),
                mock.patch.object(release, "git", side_effect=git),
                mock.patch.object(
                    release, "worktree", return_value=contextlib.nullcontext(tree)
                ),
                mock.patch.object(release, "run", return_value=""),
                mock.patch.object(release, "open_pr") as pr,
            ):
                tag, opened = release.prepare_gameplay(args)
            self.assertEqual(tag, "v2.83.0")
            self.assertFalse(opened)
            pr.assert_not_called()


class DryRunLegs(unittest.TestCase):
    """Each leg of the new flow in dry-run mode: reads only, no network write."""

    @contextlib.contextmanager
    def no_writes(self):
        with (
            mock.patch.object(release, "run", side_effect=AssertionError("no run")),
            mock.patch.object(
                release, "ship_mutate", side_effect=AssertionError("no mutation")
            ),
            mock.patch.object(release, "say"),
        ):
            yield

    def test_status_reads_every_side_report_only(self):
        healed = []
        prod = {"build": "v1.0.0", "server_build": "v1.0.0", "compatibility": PROD}
        with (
            mock.patch.object(release, "prod_state", return_value=prod),
            mock.patch.object(release, "say_held"),
            mock.patch.object(
                release,
                "ff_next_to_main",
                side_effect=lambda side, dry, report_only=False: healed.append(
                    (side, report_only)
                ),
            ),
            mock.patch.object(release, "repo_dir", side_effect=lambda side: Path(side)),
            mock.patch.object(release, "fetch_main"),
            mock.patch.object(release, "integration_ref", return_value="origin/next"),
            mock.patch.object(
                release, "remote_tags", return_value={"v1.0.0": "a" * 40}
            ),
            mock.patch.object(release, "published", return_value="v1.0.0"),
            mock.patch.object(release, "published_repo", return_value="v1.0.0"),
            mock.patch.object(release, "git", return_value=""),
            mock.patch.object(release, "pins", return_value=[]),
            mock.patch.object(release, "show_pack", return_value="1.0.0"),
            mock.patch.object(release, "say") as say,
        ):
            self.assertTrue(release.cmd_status(SimpleNamespace()))
        self.assertEqual(
            healed, [("client", True), ("server", True), ("gameplay", True)]
        )
        say.assert_any_call("\nrelease kind: none")

    def test_preflight_builds_in_a_throwaway_tree(self):
        args = SimpleNamespace(side="server", ref="origin/main", tag="v2.83.0")
        with (
            mock.patch.object(release, "repo_dir", return_value=Path("server")),
            mock.patch.object(release, "fetch_main"),
            mock.patch.object(
                release, "worktree", return_value=contextlib.nullcontext(Path("t"))
            ),
            mock.patch.object(
                release, "preflight", return_value=([("ok", "built")], {})
            ) as pre,
            self.no_writes(),
        ):
            self.assertTrue(release.cmd_preflight(args))
        pre.assert_called_once_with("server", "v2.83.0", Path("t"))

    def test_candidate_dry_run_pushes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "pack.toml").write_text('version = "2.83.1"\n')
            with (
                mock.patch.object(release, "repo_dir", return_value=Path("server")),
                mock.patch.object(release, "git", return_value="c" * 40),
                mock.patch.object(
                    release, "worktree", return_value=contextlib.nullcontext(tree)
                ),
                mock.patch.object(release, "set_candidate"),
                mock.patch.object(
                    release, "preflight", return_value=([("ok", "built")], {})
                ),
                mock.patch.object(release, "mutate") as mutate,
                self.no_writes(),
            ):
                sha = release.cmd_candidate(
                    SimpleNamespace(
                        branch="release/dedicated-2.83.1",
                        tag="v2.83.0",
                        dry_run=True,
                        expected_tree=None,
                    )
                )
        self.assertEqual(sha, "c" * 40)
        self.assertTrue(mutate.call_args.args[0])
        self.assertEqual(mutate.call_args.args[1:3], ("git", "push"))

    def test_prepare_prints_the_ship_plan_with_the_digest_dispatch(self):
        tags = {"server": "v2.83.1"}
        order = [("tag", "server"), ("deploy", "server")]
        said = []
        with (
            mock.patch.object(
                release,
                "prepare_all",
                return_value=(tags, "server-only", order, "release/dedicated-2.83.1"),
            ),
            mock.patch.object(release, "say", side_effect=said.append),
        ):
            self.assertTrue(release.cmd_prepare(SimpleNamespace()))
        text = "\n".join(said)
        self.assertIn("blockfield-gameplay/actions/workflows/deploy.yml", text)
        self.assertNotIn("blockfield-server", text)

    def test_ship_dry_run_deferred_prints_gameplay_then_rerun(self):
        said = []
        with (
            mock.patch.object(release, "mod_published", return_value=True),
            mock.patch.object(
                release,
                "prepare_all",
                return_value=({"gameplay": "v2.83.0"}, "deferred", [], ""),
            ),
            mock.patch.object(release, "say", side_effect=said.append),
            mock.patch.object(
                release, "ship_tag_side", side_effect=AssertionError("no tag")
            ),
        ):
            self.assertTrue(
                release.cmd_ship(
                    SimpleNamespace(
                        mod="v1.54.0",
                        only=None,
                        dry_run=True,
                        confirmed_by_owner=False,
                        timeout=1,
                        interval=0,
                    )
                )
            )
        text = "\n".join(said)
        self.assertIn("gameplay v2.83.0", text)
        self.assertIn("dedicated pin PR", text)

    def test_gameplay_tag_is_listed_only_when_prepare_opened_it(self):
        args = release.parser().parse_args(["prepare", "--mod", "v1.57.0", "--dry-run"])
        for opened, expected in ((True, None), (False, None)):
            with (
                self.subTest(opened=opened),
                mock.patch.object(
                    release, "prepare_gameplay", return_value=("v2.83.0", opened)
                ),
                mock.patch.object(release, "release_published", return_value=True),
                mock.patch.object(
                    release,
                    "prepare_dedicated",
                    return_value=("v2.83.1", {"compatibility": PROD}),
                ),
                mock.patch.object(release, "say"),
            ):
                _, _, gameplay, _ = release.prepare_server(args, {})
                self.assertEqual(gameplay, "v2.83.0" if opened else expected)


if __name__ == "__main__":
    unittest.main()
