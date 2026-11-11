"""Argument handling and the stop-always path of boat_build (CLI mocked)."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import boat_build  # noqa: E402


class Args(unittest.TestCase):
    def test_requires_target(self):
        with self.assertRaises(SystemExit):
            boat_build.parse_args(["--", "true"])

    def test_fork_requires_name(self):
        with self.assertRaises(SystemExit):
            boat_build.parse_args(["--fork-from", "bx_x", "--", "true"])

    def test_requires_command(self):
        with self.assertRaises(SystemExit):
            boat_build.parse_args(["--vm", "bx_x"])

    def test_ok(self):
        args = boat_build.parse_args(
            ["--vm", "bx_x", "--timeout", "60", "--", "echo", "hi"]
        )
        self.assertEqual(
            (args.vm, args.timeout, args.command), ("bx_x", 60, ["echo", "hi"])
        )

    def test_name_alone_is_auto_target(self):
        args = boat_build.parse_args(["--name", "bf-task", "--", "true"])
        self.assertEqual((args.name, args.vm, args.fork_from), ("bf-task", None, None))


class GoldenSelect(unittest.TestCase):
    def vms(self):
        return json.dumps(
            [
                {
                    "id": "bx_old",
                    "name": "bf-golden-20261029",
                    "state": "stopped",
                    "snapshotAvailable": True,
                    "snapshotCompletedAt": "2026-10-29T18:57:27.615Z",
                },
                {
                    "id": "bx_new",
                    "name": "bf-golden-20261031",
                    "state": "stopped",
                    "snapshotAvailable": True,
                    "snapshotCompletedAt": "2026-10-31T21:20:36.819Z",
                },
                {
                    "id": "bx_fork",
                    "name": "bf-golden-20261031 fork",
                    "state": "stopped",
                    "snapshotAvailable": True,
                    "snapshotCompletedAt": "2026-11-03T22:18:45.188Z",
                },
                {
                    "id": "bx_mine",
                    "name": "bf-task",
                    "state": "stopped",
                    "snapshotAvailable": True,
                    "snapshotCompletedAt": "2026-11-04T00:00:00.000Z",
                },
            ]
        )

    def test_latest_golden_skips_forks(self):
        with patch.object(
            boat_build, "run_boat", return_value=FakeResult(0, self.vms())
        ):
            self.assertEqual(boat_build.latest_golden_vm(), "bx_new")

    def test_list_unwraps_sandboxes_envelope(self):
        envelope = json.dumps({"pageInfo": {"hasMore": False}, "sandboxes": []})
        with patch.object(boat_build, "run_boat", return_value=FakeResult(0, envelope)):
            self.assertEqual(boat_build.list_vms(), [])
        wrapped = json.dumps({"sandboxes": json.loads(self.vms())})
        with patch.object(boat_build, "run_boat", return_value=FakeResult(0, wrapped)):
            self.assertEqual(boat_build.latest_golden_vm(), "bx_new")

    def test_find_own_stopped_vm_by_name(self):
        with patch.object(
            boat_build, "run_boat", return_value=FakeResult(0, self.vms())
        ):
            self.assertEqual(boat_build.find_vm_by_name("bf-task"), "bx_mine")
            self.assertIsNone(boat_build.find_vm_by_name("bf-absent"))

    def test_no_golden_names_a_bake(self):
        with patch.object(boat_build, "run_boat", return_value=FakeResult(0, "[]")):
            with self.assertRaisesRegex(RuntimeError, "just boat-bake"):
                boat_build.latest_golden_vm()


class ExecEnvelope(unittest.TestCase):
    def test_parses_json_envelope(self):
        envelope = '{"exitCode": 3, "stdout": "41d5cb5\\n", "stderr": "warn\\n"}'
        self.assertEqual(
            boat_build.parse_exec_output(envelope, 0), (3, "41d5cb5\nwarn\n")
        )

    def test_plain_output_passes_through(self):
        # n1: a bare CLI rejection means nothing was dispatched: fail directly,
        # never save a command-less run that would resume-loop exit 75.
        with patch.object(
            boat_build,
            "run_boat",
            return_value=FakeResult(1, "auth rejected: login expired"),
        ):
            with self.assertRaisesRegex(RuntimeError, "before dispatch"):
                boat_build.vm_exec("bx_x", "true")

    def test_streamed_output_without_id_is_unknown_not_pending(self):
        # n1: frames prove the VM ran something, but with no command id no
        # resume can poll it: explicit recovery message, never exit 75.
        stream = '{"type": "stdout", "data": "hi\\n"}\n'
        with patch.object(boat_build, "run_boat", return_value=FakeResult(1, stream)):
            with self.assertRaisesRegex(RuntimeError, "unknown"):
                boat_build.vm_exec("bx_x", "true")

    def test_parses_jsonl_stream(self):
        stream = (
            '{"type": "attached", "processId": 1}\n'
            '{"type": "stdout", "data": "hi\\n"}\n'
            '{"type": "stderr", "data": "warn\\n"}\n'
            '{"type": "exit", "exitCode": 3}\n'
        )
        self.assertEqual(boat_build.parse_exec_output(stream, 0), (3, "hi\nwarn\n"))


class StopAlways(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = str(Path(self.tmp.name) / "logs")
        self.calls = []
        self.exec_script = None

        def fake_run_boat(*args, **kwargs):
            self.calls.append(args)
            if args[0] == "stop":
                manifests = list(
                    (boat_build.ROOT / ".dev" / "unit-x").glob("*/manifest.json")
                )
                self.assertTrue(manifests, "manifest must exist before stop")
            if args[0] == "usage":
                return FakeResult(0, '{"seconds": 120, "billingMultiplier": 1}')
            if args[0] == "history":
                return FakeResult(0, '{"commands": []}')
            if args[0] == "info":
                return FakeResult(0, '{"state": "stopped"}')
            return FakeResult(0, "{}")

        self.patches = [
            # Run manifests/logs land under ROOT/.dev/<task>: keep them out of the real checkout.
            patch.object(boat_build, "ROOT", Path(self.tmp.name)),
            patch.object(boat_build, "run_boat", side_effect=fake_run_boat),
            patch.object(boat_build, "local_shas", return_value={"": "abc"}),
            patch.object(boat_build, "remote_shas", return_value={"": "abc"}),
            patch.object(boat_build, "ensure_worktree", return_value=0),
            patch.object(boat_build, "write_env", return_value="/remote/boat-env.sh"),
            patch.object(boat_build, "vm_minutes", return_value=(2.0, {})),
        ]
        for target in self.patches:
            target.start()
            self.addCleanup(target.stop)

    def base(self, *extra):
        return [
            "--vm",
            "bx_mine",
            "--task",
            "unit-x",
            "--out",
            self.out,
            *extra,
            "--",
            "true",
        ]

    def stops(self):
        return [c for c in self.calls if c[:2] == ("stop", "bx_mine")]

    def test_stop_on_success(self):
        with patch.object(boat_build, "vm_exec", return_value=(0, "ok")):
            self.assertEqual(boat_build.main(self.base()), 0)
        self.assertTrue(self.stops())

    def test_stop_on_failure(self):
        with patch.object(boat_build, "vm_exec", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                boat_build.main(self.base())
        self.assertFalse(self.stops())

    def test_failed_rename_still_stops_fork(self):
        real = boat_build.run_boat.side_effect

        def fake(*args, **kwargs):
            if args[0] == "fork":
                self.calls.append(args)
                return FakeResult(0, '{"id": "bx_mine"}')
            if args[0] == "rename":
                raise RuntimeError("rename failed")
            return real(*args, **kwargs)

        with (
            patch.object(boat_build, "run_boat", side_effect=fake),
            patch.object(boat_build, "wait_state", return_value="running"),
        ):
            with self.assertRaises(RuntimeError):
                boat_build.main(
                    ["--fork-from", "bx_gold", "--name", "n", "--task", "unit-x"]
                    + ["--out", self.out, "--", "true"]
                )
        self.assertFalse(self.stops())

    def test_never_stops_other_vms(self):
        with patch.object(boat_build, "vm_exec", return_value=(0, "ok")):
            boat_build.main(self.base())
        stopped = {c[1] for c in self.calls if c[0] == "stop"}
        self.assertEqual(stopped, {"bx_mine"})

    def test_type_change_on_running_vm_is_refused(self):
        with patch.object(
            boat_build, "current_state", return_value=("idle", "default")
        ):
            with self.assertRaisesRegex(RuntimeError, "preserve prior run evidence"):
                boat_build.main(self.base("--type", "small"))
        self.assertFalse(self.stops())

    def test_pending_never_stops(self):
        with patch.object(
            boat_build,
            "vm_exec",
            side_effect=boat_build.Running("bx_mine", "cmd", "true"),
        ):
            self.assertEqual(boat_build.main(self.base()), 75)
        self.assertFalse(self.stops())

    def test_dead_exec_retries_the_run_once(self):
        # ws#22 closing criterion: a dead run-phase dispatch fails with an
        # exit status and the run is dispatched once more, not left running.
        runs = []

        def fake_vm_exec(vm_id, script, timeout=120, record=None):
            if script == "echo $HOME":
                return 0, "/home/boat\n"
            if script.startswith("[ -d "):
                return 0, "blockfield-workspace\n"
            runs.append(script)
            if len(runs) == 1:
                record.update(exitCode=124, finishedAt="now", deadExec=True)
                return 124, "dead exec: command cmd_1 no verdict past the deadline"
            return 0, "ok"

        with patch.object(boat_build, "vm_exec", side_effect=fake_vm_exec):
            self.assertEqual(boat_build.main(self.base()), 0)
        self.assertEqual(len(runs), 2)

    def test_copy_failure_never_stops(self):
        with (
            patch.object(boat_build, "vm_exec", return_value=(0, "ok")),
            patch.object(
                boat_build, "save_evidence", side_effect=RuntimeError("copy failed")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "copy failed"):
                boat_build.main(self.base())
        self.assertFalse(self.stops())

    def test_out_copy_failure_never_stops(self):
        # n2: the later --out copy failed after the manifest was preserved:
        # the VM must stay running and say so, not stop with the evidence
        # stranded in the run dir.
        buf = io.StringIO()
        with (
            patch.object(boat_build, "vm_exec", return_value=(0, "ok")),
            patch.object(
                boat_build.shutil,
                "copytree",
                side_effect=OSError(13, "Permission denied"),
            ),
            contextlib.redirect_stdout(buf),
        ):
            with self.assertRaises(OSError):
                boat_build.main(self.base())
        self.assertFalse(self.stops())
        self.assertIn("left running", buf.getvalue())

    def tearDown(self):
        pass


class Quoting(unittest.TestCase):
    def run_main(self, command):
        seen = []

        def fake_run_boat(*args, **kwargs):
            if args[0] == "history":
                return FakeResult(0, '{"commands": []}')
            if args[0] == "info":
                return FakeResult(0, '{"state": "stopped"}')
            if args[0] == "usage":
                return FakeResult(0, '{"seconds": 0, "billingMultiplier": 1}')
            return FakeResult(0, "{}")

        patches = [
            patch.object(boat_build, "run_boat", side_effect=fake_run_boat),
            patch.object(boat_build, "local_shas", return_value={"": "abc"}),
            patch.object(boat_build, "remote_shas", return_value={"": "abc"}),
            patch.object(boat_build, "ensure_worktree", return_value=0),
            patch.object(boat_build, "write_env", return_value="/remote/env"),
            patch.object(boat_build, "vm_minutes", return_value=(0.0, {})),
            patch.object(
                boat_build,
                "vm_exec",
                side_effect=lambda *a, **k: (seen.append(a[1]), (0, "ok"))[1],
            ),
        ]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in patches:
            target.start()
            self.addCleanup(target.stop)
        boat_build.main(
            ["--vm", "bx_mine", "--task", "unit-q", "--out", tmp.name, "--", *command]
        )
        return seen[-1]

    def test_single_arg_runs_as_shell(self):
        self.assertIn("&& true;", self.run_main(["echo hi && true"]))

    def test_many_args_are_quoted(self):
        self.assertIn("'hi there'", self.run_main(["echo", "hi there"]))


class TransferArgs(unittest.TestCase):
    def test_worktree_on_by_default(self):
        args = boat_build.parse_args(["--vm", "bx_x", "--", "true"])
        self.assertTrue(args.with_worktree)
        self.assertEqual(args.local_jar, [])

    def test_no_worktree_disables(self):
        args = boat_build.parse_args(["--vm", "bx_x", "--no-worktree", "--", "true"])
        self.assertFalse(args.with_worktree)

    def test_workspace_sha_resolves_to_full_commit(self):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        args = boat_build.parse_args(
            ["--vm", "bx_x", "--workspace-sha", head[:12], "--", "true"]
        )
        self.assertEqual(boat_build.local_shas(args)[""], head)

    def test_local_jar_repeatable(self):
        args = boat_build.parse_args(
            [
                "--vm",
                "bx_x",
                "--local-jar",
                "a=/tmp/a.jar",
                "--local-jar",
                "b=/tmp/b.jar",
                "--",
                "true",
            ]
        )
        self.assertEqual(args.local_jar, ["a=/tmp/a.jar", "b=/tmp/b.jar"])


def make_repo(path, origin=None):
    if origin is not None:
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(path)],
            check=True,
            capture_output=True,
        )
    else:
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=path,
            check=True,
            capture_output=True,
        )
    subprocess.run(["git", "config", "user.email", "t@localhost"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)


def commit_file(repo, name, content, message):
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=repo, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class BundleBase(unittest.TestCase):
    """The ^VM-HEAD bundle fails when the VM sits at an unknown commit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "origin")
        self.origin = root / "origin"
        commit_file(self.origin, "f.txt", "a\n", "init")
        make_repo(root / "local", origin=self.origin)
        self.local = root / "local"
        make_repo(root / "vm", origin=self.origin)
        self.vm = root / "vm"
        self.want = commit_file(self.local, "new.txt", "x\n", "unpushed")
        # VM at a commit the local clone never fetched.
        subprocess.run(
            ["git", "config", "user.email", "t@localhost"], cwd=self.vm, check=True
        )
        subprocess.run(["git", "config", "user.name", "t"], cwd=self.vm, check=True)
        (self.vm / "vm.txt").write_text("v\n", encoding="utf-8")
        subprocess.run(["git", "add", "vm.txt"], cwd=self.vm, check=True)
        subprocess.run(["git", "commit", "-qm", "vm-only"], cwd=self.vm, check=True)
        self.have = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.vm,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_old_have_exclusion_fails(self):
        out = subprocess.run(
            [
                "git",
                "bundle",
                "create",
                str(Path(self.tmp.name) / "old.bundle"),
                self.want,
                f"^{self.have}",
            ],
            cwd=self.local,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(out.returncode, 0, out.stderr)
        self.assertIn("bad object", out.stderr)

    def test_merge_base_bundle_fetches_on_vm(self):
        (base,) = boat_build.bundle_exclusions(self.local, self.want)
        self.assertTrue(base, "merge-base with origin/main must exist")
        subprocess.run(
            ["git", "tag", "-f", "boat-t", self.want],
            cwd=self.local,
            check=True,
            capture_output=True,
        )
        self.addCleanup(
            lambda: subprocess.run(
                ["git", "tag", "-d", "boat-t"], cwd=self.local, capture_output=True
            )
        )
        bundle = str(Path(self.tmp.name) / "new.bundle")
        subprocess.run(
            ["git", "bundle", "create", bundle, "boat-t", f"^{base}"],
            cwd=self.local,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", bundle, "boat-t:refs/tags/boat-t"],
            cwd=self.vm,
            check=True,
            capture_output=True,
        )
        got = subprocess.run(
            ["git", "rev-parse", "refs/tags/boat-t"],
            cwd=self.vm,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(got, self.want)

    def test_no_origin_means_full_ref(self):
        lone = Path(self.tmp.name) / "lone"
        make_repo(lone)
        want = commit_file(lone, "f.txt", "a\n", "init")
        self.assertIsNone(boat_build.default_tip(lone))
        self.assertEqual(boat_build.bundle_exclusions(lone, want), [])


class MergeBundlePrereqs(unittest.TestCase):
    """Hermetic repro: a merge's `want ^base` bundle records one prerequisite
    per parent line, so checking only the base ships a bundle the VM cannot
    fetch (p2-world-c5: workspace merge onto main with a PR second parent)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "origin")
        commit_file(root / "origin", "f.txt", "a\n", "init")  # A
        commit_file(root / "origin", "f.txt", "t\n", "tip")  # T = origin/main
        origin = root / "origin"
        subprocess.run(["git", "branch", "feat", "HEAD~1"], cwd=origin, check=True)
        subprocess.run(["git", "checkout", "-q", "feat"], cwd=origin, check=True)
        commit_file(origin, "g.txt", "f\n", "feat")  # F, second-parent line
        subprocess.run(["git", "checkout", "-q", "main"], cwd=origin, check=True)
        self.fork_point = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        make_repo(root / "local", origin=origin)
        self.local = root / "local"
        for cmd in (
            ["git", "merge", "-q", "--no-ff", "origin/feat", "-m", "merge"],
            ["git", "commit", "-q", "--allow-empty", "-m", "top"],
        ):
            subprocess.run(cmd, cwd=self.local, check=True, capture_output=True)
        self.want = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.local,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.base = subprocess.run(
            ["git", "merge-base", self.want, "origin/main"],
            cwd=self.local,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "tag", "-f", "bt", self.want],
            cwd=self.local,
            check=True,
            capture_output=True,
        )
        self.delta = str(root / "delta.bundle")
        subprocess.run(
            ["git", "bundle", "create", self.delta, "bt", f"^{self.base}"],
            cwd=self.local,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "bundle", "create", str(root / "full.bundle"), "bt"],
            cwd=self.local,
            check=True,
            capture_output=True,
        )
        self.full = str(root / "full.bundle")
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{origin}", "vmshallow"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        self.vm = root / "vmshallow"

    def test_delta_bundle_has_two_prerequisites(self):
        raw = Path(self.delta).read_bytes()
        # The v2 header is text lines up to the PACK payload; pack bytes
        # themselves can contain b"\n-" and must not be counted.
        header, _, _ = raw.partition(b"\nPACK")
        prereqs = [line for line in header.split(b"\n") if line.startswith(b"-")]
        self.assertEqual(len(prereqs), 2, prereqs)

    def test_delta_verify_fails_where_base_is_present(self):
        # The old single-base check passes (the VM has the base) ...
        self.assertEqual(
            subprocess.run(
                ["git", "cat-file", "-e", self.base], cwd=self.vm
            ).returncode,
            0,
        )
        # ... yet the bundle cannot be used there: the second parent-line
        # boundary is missing from the shallow clone.
        verify = subprocess.run(
            ["git", "bundle", "verify", self.delta],
            cwd=self.vm,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(verify.returncode, 0)
        # The base is present, but the second parent-line boundary is missing.
        self.assertIn(self.fork_point, (verify.stdout or "") + (verify.stderr or ""))

    def test_full_ref_verifies_and_fetches(self):
        self.assertEqual(
            subprocess.run(
                ["git", "bundle", "verify", self.full], cwd=self.vm
            ).returncode,
            0,
        )
        subprocess.run(
            ["git", "fetch", self.full, "bt:bt"],
            cwd=self.vm,
            check=True,
            capture_output=True,
        )
        got = subprocess.run(
            ["git", "rev-parse", "bt"],
            cwd=self.vm,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(got, self.want)

    def test_exclusions_cover_each_parent_line(self):
        exclusions = boat_build.bundle_exclusions(self.local, self.want)
        self.assertEqual(exclusions, [self.base])
        merge = subprocess.run(
            ["git", "rev-parse", f"{self.want}~1"],
            cwd=self.local,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        per_parent = boat_build.bundle_exclusions(self.local, merge)
        self.assertEqual(len(per_parent), 2)
        self.assertIn(self.base, per_parent)


class BranchSubmodules(unittest.TestCase):
    """`--branch` must transfer nested-path submodules (localmods/*).

    A top-level `ls-tree` never lists gitlinks below a tree, so the old
    code silently kept the golden's nested pins on the VM while the task
    changed them (pack-banner: the VM tested golden mod code).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "sub")
        self.sub = commit_file(root / "sub", "s.txt", "s\n", "sub")
        make_repo(root / "super")
        self.super = root / "super"
        for path in ("client", "localmods/m"):
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    "-q",
                    f"file://{root / 'sub'}",
                    path,
                ],
                cwd=self.super,
                check=True,
                capture_output=True,
            )
        # The server pin is git-ignored, not a submodule: every workspace
        # commit carries it, so fixtures must too.
        self.pin = "b" * 40
        (self.super / "pins").mkdir()
        (self.super / "pins" / "server.sha").write_text(self.pin + "\n")
        subprocess.run(["git", "add", "-A"], cwd=self.super, check=True)
        subprocess.run(["git", "commit", "-qm", "subs"], cwd=self.super, check=True)
        subprocess.run(["git", "branch", "task"], cwd=self.super, check=True)

    def test_branch_includes_nested_submodule_shas(self):
        args = boat_build.parse_args(["--vm", "bx_x", "--branch", "task", "--", "true"])
        with patch.object(boat_build, "ROOT", self.super):
            wanted = boat_build.local_shas(args)
        self.assertIn("", wanted)
        self.assertEqual(wanted["client"], self.sub)
        self.assertEqual(wanted["localmods/m"], self.sub)

    def test_submodule_override_resolves_in_named_repo(self):
        # The SHA belongs to the submodule's repo, not the workspace.
        args = boat_build.parse_args(
            [
                "--vm",
                "bx_x",
                "--branch",
                "task",
                "--submodule",
                f"localmods/m={self.sub}",
                "--",
                "true",
            ]
        )
        with patch.object(boat_build, "ROOT", self.super):
            wanted = boat_build.local_shas(args)
        self.assertEqual(wanted["localmods/m"], self.sub)

    def test_submodule_override_rejects_unknown_path(self):
        args = boat_build.parse_args(
            [
                "--vm",
                "bx_x",
                "--branch",
                "task",
                "--submodule",
                "nope/dead=abc123",
                "--",
                "true",
            ]
        )
        with patch.object(boat_build, "ROOT", self.super):
            with self.assertRaises(SystemExit):
                boat_build.local_shas(args)


class WorktreePatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "origin")
        commit_file(root / "origin", "f.txt", "a\n", "init")
        make_repo(root / "a", origin=root / "origin")
        make_repo(root / "b", origin=root / "origin")
        self.a, self.b = root / "a", root / "b"

    def test_clean_repo_has_no_patch(self):
        self.assertIsNone(boat_build.worktree_patch(self.a))

    def test_patch_carries_tracked_and_untracked(self):
        with open(self.a / "f.txt", "a", encoding="utf-8") as fh:
            fh.write("mod\n")
        (self.a / "new.txt").write_text("new\n", encoding="utf-8")
        (self.a / "sub").mkdir()
        (self.a / "sub" / "n.txt").write_text("n\n", encoding="utf-8")
        patch = boat_build.worktree_patch(self.a)
        self.assertTrue(patch)
        # Index restored: untracked files are untracked again.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.a,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn(" M f.txt", status)
        self.assertIn("?? new.txt", status)
        subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=self.b,
            input=patch,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "apply", "-"],
            cwd=self.b,
            input=patch,
            check=True,
            capture_output=True,
        )
        self.assertEqual((self.b / "new.txt").read_text(encoding="utf-8"), "new\n")
        self.assertIn("mod", (self.b / "f.txt").read_text(encoding="utf-8"))

    def test_staged_changes_survive_patch_collection(self):
        (self.a / "staged.txt").write_text("s\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged.txt"], cwd=self.a, check=True)
        boat_build.worktree_patch(self.a)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.a,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("staged.txt", staged)


class EnsureWorktree(unittest.TestCase):
    """Patches must match the tree the VM checks out, on a clean tree.

    Mirror of the edit-and-rerun loop on a reused warm VM: the previous
    run's applied patch lingered (skipped checkout), so apply --check died;
    and a patch diffed against local HEAD was shipped onto --branch/--submodule
    SHAs it was never made for.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "repo")
        self.base = commit_file(root / "repo", "f.txt", "a\n", "init")
        self.repo = root / "repo"
        self.stage = root / "stage"
        self.stage.mkdir()
        self.cmds = []

    def fake_vm(self):
        def vm_exec(vm_id, script, timeout=120):
            self.cmds.append(script)
            return 0, ""

        return vm_exec

    def test_head_mismatch_fails_fast(self):
        (self.repo / "f.txt").write_text("a\ndirty\n", encoding="utf-8")
        with (
            patch.object(boat_build, "ROOT", self.repo),
            patch.object(boat_build, "run_boat", return_value=None),
            patch.object(boat_build, "vm_exec", side_effect=self.fake_vm()),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                boat_build.ensure_worktree(
                    "bx_x", "/vm/root", "/vm", {"": "0" * 40}, self.stage
                )
        self.assertIn("diffed against", str(ctx.exception))
        self.assertEqual(self.cmds, [])

    def test_reset_runs_before_apply(self):
        (self.repo / "f.txt").write_text("a\ndirty\n", encoding="utf-8")
        with (
            patch.object(boat_build, "ROOT", self.repo),
            patch.object(boat_build, "run_boat", return_value=None),
            patch.object(boat_build, "vm_exec", side_effect=self.fake_vm()),
        ):
            n = boat_build.ensure_worktree(
                "bx_x", "/vm/root", "/vm", {"": self.base}, self.stage
            )
        self.assertEqual(n, 1)
        self.assertIn("reset --hard", self.cmds[0])
        self.assertIn("clean -fd", self.cmds[0])
        self.assertIn("apply --check", self.cmds[1])


class LocalJarArgs(unittest.TestCase):
    def test_bad_spec_rejected(self):
        with self.assertRaises(SystemExit):
            boat_build.parse_local_jars(["no-equals"])

    def test_missing_file_rejected(self):
        with self.assertRaises(SystemExit):
            boat_build.parse_local_jars(["somedir=/tmp/does-not-exist.jar"])

    def test_ok(self):
        with tempfile.NamedTemporaryFile(suffix=".jar", delete=False) as fh:
            fh.write(b"fake")
            path = fh.name
        self.addCleanup(Path(path).unlink, missing_ok=True)
        self.assertEqual(
            boat_build.parse_local_jars([f"d={path}"]), [("d", Path(path))]
        )


class EnsureRepoBundling(unittest.TestCase):
    """Bundle args depend on VM-side verification, not just the merge-base."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "origin")
        commit_file(root / "origin", "f.txt", "a\n", "init")
        (root / "ws").mkdir()
        make_repo(root / "ws" / "repo", origin=root / "origin")
        self.ws = root / "ws"
        self.local = self.ws / "repo"
        self.want = commit_file(self.local, "new.txt", "x\n", "unpushed")
        (self.base,) = boat_build.bundle_exclusions(self.local, self.want)
        self.assertTrue(self.base)
        self.stage = root / "stage"
        self.stage.mkdir()
        self.bundles = []
        self.vm_scripts = []

    def run_ensure(self, vm_script_results):
        real_run_git = boat_build.run_git

        def spy(*args, **kwargs):
            if args[:2] == ("bundle", "create"):
                self.bundles.append(args)
            return real_run_git(*args, **kwargs)

        scripts = list(vm_script_results)

        def fake_vm_exec(vm_id, script, timeout=120):
            self.vm_scripts.append(script)
            if script.startswith("test -e "):
                return 0, ""
            return scripts.pop(0)

        with (
            patch.object(boat_build, "ROOT", self.ws),
            patch.object(boat_build, "run_git", side_effect=spy),
            patch.object(boat_build, "vm_exec", side_effect=fake_vm_exec),
            patch.object(boat_build, "run_boat", return_value=FakeResult(0, "{}")) as _,
        ):
            boat_build.ensure_repo(
                "bx_x",
                "/srv/guest/blockfield-workspace",
                "/srv/guest",
                "repo",
                self.want,
                self.stage,
            )
        self.assertFalse(scripts, "all scripted VM replies consumed")

    def test_vm_missing_base_ships_full_ref(self):
        self.run_ensure(
            [
                (1, ""),  # rev-parse HEAD: unknown checkout
                (1, "not found"),  # fetch origin <want>: unpushed
                (1, ""),  # batched cat-file: VM lacks the boundary
                (0, "bundle is valid"),  # verify full ref: no prerequisites
                (0, ""),  # fetch bundle guest: full bundle applies cleanly
                (0, ""),  # delete tmp tag on the VM (finally)
                (0, ""),  # checkout --detach
                (0, self.want),  # rev-parse HEAD: at want  # checkout --detach: at want
            ]
        )
        self.assertEqual(len(self.bundles), 1)
        self.assertNotIn(f"^{self.base}", self.bundles[0])
        self.assertIn(self.want[:12], self.bundles[0][3])

    def test_vm_having_base_ships_verified_delta(self):
        self.run_ensure(
            [
                (1, ""),
                (1, "not found"),
                (0, ""),  # batched cat-file: boundary present
                (0, "bundle is valid"),  # verify delta: all prerequisites met
                (0, ""),
                (0, ""),  # delete tmp tag on the VM (finally)
                (0, ""),  # checkout --detach
                (0, self.want),  # rev-parse HEAD: at want
            ]
        )
        self.assertEqual(len(self.bundles), 1)
        self.assertIn(f"^{self.base}", self.bundles[0])
        fetch = next(s for s in self.vm_scripts if "fetch " in s and "bundle" in s)
        self.assertIn("+boat-tmp-", fetch)  # force-fetch over stale VM tags

    def test_fetch_failure_falls_back_to_full_ref(self):
        # verify passes but the fetch cannot read a graft-hidden object
        # (sender packed a grafted view, VM lacks the hidden link): retry
        # the full ref once, which the complete sender can pack wholly.
        missing = "e" * 40
        self.run_ensure(
            [
                (1, ""),
                (1, "not found"),
                (0, ""),  # batched cat-file: exclusions present
                (0, "bundle is valid"),  # verify delta: prerequisites met
                (1, f"error: Could not read {missing}"),  # fetch: hidden link
                (0, "bundle is valid"),  # verify full ref
                (0, ""),  # fetch full ref: complete
                (0, ""),  # delete tmp tag on the VM (finally)
                (0, ""),  # checkout --detach
                (0, self.want),  # rev-parse HEAD: at want
            ]
        )
        self.assertEqual(len(self.bundles), 2)
        self.assertIn(f"^{self.base}", self.bundles[0])
        self.assertNotIn(f"^{self.base}", self.bundles[1])

    def test_fetch_failure_raises_with_unreadable_sha(self):
        # Both attempts fail: the error names the object the VM cannot
        # read so the operator knows to refresh the golden.
        with self.assertRaises(RuntimeError) as ctx:
            self.run_ensure(
                [
                    (1, ""),
                    (1, "not found"),
                    (0, ""),
                    (0, "bundle is valid"),
                    (1, f"error: Could not read {self.base}"),
                    (0, "bundle is valid"),
                    (1, f"error: Could not read {self.base}"),
                    (0, ""),  # delete tmp tag on the VM (finally)
                ]
            )
        self.assertIn(self.base, str(ctx.exception))

    def test_checkout_failure_reports_git_stderr(self):
        # The old chained command could report an empty failure; the split
        # checkout surfaces git's own stderr (knife-sweep: empty output).
        with self.assertRaises(RuntimeError) as ctx:
            self.run_ensure(
                [
                    (0, "other"),  # rev-parse HEAD
                    (0, self.want),  # fetch origin <want>: pushed
                    (1, "error: Your local changes would be overwritten"),
                ]
            )
        self.assertIn("Your local changes", str(ctx.exception))

    def test_checkout_head_mismatch_reports_both_outputs(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_ensure(
                [
                    (0, "other"),
                    (0, self.want),
                    (0, "checked out"),  # checkout ok...
                    (0, "deadbee"),  # ...but HEAD is elsewhere
                ]
            )
        self.assertIn("deadbee", str(ctx.exception))

    def test_verify_failure_falls_back_to_full_ref(self):
        # The pre-check passes but `bundle verify` on the VM names a missing
        # second boundary (merge second parent): the old code fetched here and
        # died; the new code ships the full ref instead.
        self.run_ensure(
            [
                (1, ""),
                (1, "not found"),
                (0, ""),  # batched cat-file: exclusions present
                (1, "missing prerequisite"),  # verify delta: second boundary gone
                (0, "bundle is valid"),  # verify full ref
                (0, ""),
                (0, ""),  # delete tmp tag on the VM (finally)
                (0, ""),  # checkout --detach
                (0, self.want),  # rev-parse HEAD: at want
            ]
        )
        self.assertEqual(len(self.bundles), 2)
        self.assertIn(f"^{self.base}", self.bundles[0])
        self.assertNotIn(f"^{self.base}", self.bundles[1])
        self.assertEqual(
            self.bundles[1][1:3], ("create", str(self.stage / "repo.bundle"))
        )


class RemoteStatus(unittest.TestCase):
    """A dropped exec stream must never fail a still-running command.

    Mirror of the knife-sweep gate fork: the remote command kept running
    (no finishedAt) while the stream reported rc=1, and the helper stopped
    the VM mid-run. vm_exec now takes exitCode+finishedAt from Boat history.
    """

    def run_vm_exec(self, boat_replies, timeout=60):
        calls = list(boat_replies)

        def fake_run_boat(*args, **kwargs):
            return calls.pop(0)

        with (
            patch.object(boat_build, "run_boat", side_effect=fake_run_boat),
            patch("time.sleep"),
        ):
            return boat_build.vm_exec("bx_x", "true", timeout=timeout)

    def history(self, command_id, **extra):
        entry = {
            "commandId": command_id,
            "mode": "sync",
            "status": "exited",
            "startedAt": "2026-10-31T20:51:48.000Z",
            **extra,
        }
        return json.dumps({"commands": [entry]})

    def test_dropped_stream_waits_for_real_success(self):
        rc, _ = self.run_vm_exec(
            [
                FakeResult(1, '{"commandId": "cmd_x"}'),  # stream dropped
                FakeResult(0, self.history("cmd_x", status="running")),
                FakeResult(
                    0,
                    self.history(
                        "cmd_x", exitCode=0, finishedAt="2026-10-31T20:52:01.000Z"
                    ),
                ),
                FakeResult(0, json.dumps({"data": "gate ok"})),
                FakeResult(0, json.dumps({"data": ""})),
            ]
        )
        self.assertEqual(rc, 0)

    def test_running_past_deadline_is_dead(self):
        # ws#22: no verdict past the command's own timeout + grace fails
        # with 124 instead of hanging RUNNING forever.
        record = {}
        with (
            patch.object(
                boat_build, "history_entries", return_value=[{"commandId": "cmd_y"}]
            ),
            patch.object(boat_build, "history_output", return_value=""),
            patch("time.time", return_value=1000),
            patch("time.sleep") as nap,
        ):
            done, code, out = boat_build.remote_status("bx_x", "cmd_y", 0, 1, record)
        self.assertTrue(done)
        self.assertEqual(code, 124)
        self.assertIn("cmd_y", out)
        self.assertEqual(record["exitCode"], 124)
        self.assertTrue(record["finishedAt"])
        nap.assert_not_called()

    def test_missing_history_within_budget_is_pending(self):
        with (
            patch.object(boat_build, "history_entries", return_value=[]),
            patch("time.time", side_effect=[1000, 2000]),
            patch("time.sleep") as nap,
        ):
            self.assertEqual(
                boat_build.remote_status("bx_x", "cmd_x", 1000, 1800),
                (False, None, ""),
            )
        nap.assert_called_once_with(10)

    def test_history_failure_verdict(self):
        with (
            patch.object(
                boat_build,
                "history_entries",
                return_value=[{"commandId": "cmd", "exitCode": 1, "finishedAt": "now"}],
            ),
            patch.object(boat_build, "history_output", return_value="failed"),
        ):
            self.assertEqual(
                boat_build.remote_status("bx_x", "cmd", 0, -1),
                (True, 1, "failedfailed"),
            )

    def test_resume_preserves_before_stop_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            saved = {
                "vm": "bx_x",
                "record": {"commandId": "cmd", "command": "true"},
                "patterns": [],
                "keep": False,
                "out": str(Path(tmp) / "copy"),
            }

            def stop(*args):
                self.assertEqual(args, ("stop", "bx_x"))
                manifest = json.loads((out / "manifest.json").read_text())
                self.assertEqual(manifest["exitCode"], 0)
                self.assertEqual(manifest["finishedAt"], "now")
                self.assertIn("build.log", manifest["files"])
                self.assertEqual(
                    (Path(tmp) / "copy" / "build.log").read_text(), "loglog"
                )

            with (
                patch.object(
                    boat_build,
                    "history_entries",
                    return_value=[
                        {"commandId": "cmd", "exitCode": 0, "finishedAt": "now"}
                    ],
                ),
                patch.object(boat_build, "history_output", return_value="log"),
                patch.object(boat_build, "run_boat", side_effect=stop),
                patch.object(boat_build, "wait_state", return_value="stopped"),
                patch.object(boat_build, "vm_exec") as dispatch,
            ):
                self.assertEqual(boat_build.finish_run(saved, out), 0)
                dispatch.assert_not_called()
                saved["phase"] = "setup"
                self.assertEqual(boat_build.finish_run(saved, out), 3)

    def test_setup_dead_exec_fails_with_exit_status(self):
        # ws#22: a setup exec dead past its own timeout + grace fails 124
        # (the caller reruns the build) instead of hanging RUNNING forever.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "run"
            out.mkdir()
            saved = {
                "vm": "bx_x",
                "record": {
                    "commandId": "cmd_s",
                    "command": "setup",
                    "dispatchedAt": 0,
                    "exitCode": None,
                    "finishedAt": None,
                },
                "patterns": [],
                "keep": True,
                "timeout": 60,
                "phase": "setup",
                "out": None,
            }
            with (
                patch.object(
                    boat_build,
                    "history_entries",
                    return_value=[{"commandId": "cmd_s", "status": "running"}],
                ),
                patch.object(boat_build, "history_output", return_value=""),
                patch("time.time", return_value=10000),
            ):
                self.assertEqual(boat_build.finish_run(saved, out), 124)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["exitCode"], 124)
            self.assertTrue(manifest["deadExec"])

    def test_log_retrieval_failure_is_not_empty_success(self):
        with patch.object(
            boat_build, "run_boat", return_value=FakeResult(1, "unavailable")
        ):
            with self.assertRaisesRegex(RuntimeError, "output unavailable"):
                boat_build.history_output("bx_x", "cmd", strict=True)

    def test_manifest_hashes_log_and_copies_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "build.log").write_text("log")

            def copy(*args):
                (Path(args[-1]) / "report.txt").write_text("proof")

            with (
                patch.object(
                    boat_build,
                    "vm_exec",
                    return_value=(0, '["/a/report.txt", "/b/report.txt"]'),
                ),
                patch.object(boat_build, "run_boat", side_effect=copy),
            ):
                boat_build.save_evidence(
                    "bx_x",
                    {
                        "command": "true",
                        "vm": "bx_x",
                        "exitCode": 0,
                        "finishedAt": "now",
                    },
                    out,
                    ["/remote/*.txt"],
                )
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(len(manifest["files"]), 3)
            self.assertEqual(len(manifest["files"]["build.log"]), 64)


class DeadExecBoatStart(unittest.TestCase):
    """The dead-exec deadline runs from Boat's start timestamp (r4491569765)."""

    def test_deadline_measured_from_boat_start(self):
        # Fail-before: the dispatch lagged 150 s, so an exec_start-based
        # deadline reaps the command ~30 s before its own --timeout ends.
        from datetime import datetime, timezone

        dispatch = datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp()
        entry = {
            "commandId": "cmd_lag",
            "status": "running",
            "startedAt": "2026-11-05T00:02:30.000Z",  # boat started 150 s later
        }
        with (
            patch.object(boat_build, "history_entries", return_value=[entry]),
            patch.object(boat_build, "history_output", return_value=""),
            patch("time.time", return_value=dispatch + 1950),
            patch("time.sleep"),
        ):
            self.assertEqual(
                boat_build.remote_status("bx_x", "cmd_lag", dispatch, 1800),
                (False, None, ""),
            )


class DeadExecHistoryWindow(unittest.TestCase):
    def test_command_pushed_out_of_the_window_is_not_marked_dead(self):
        # Fail-before: 100 newer execs hid the command and it got exit 124.
        newer = [{"commandId": f"cmd_{i}"} for i in range(100)]
        record = {}
        with (
            patch.object(boat_build, "history_entries", return_value=newer),
            patch.object(boat_build, "history_output", return_value=""),
            self.assertRaises(RuntimeError),
        ):
            boat_build._dead_exec_verdict("bx_x", "cmd_old", record, "timed out")
        self.assertNotIn("exitCode", record)


class EvidenceKeepsRunning(unittest.TestCase):
    def test_running_lookup_is_not_swallowed_by_warn_unmatched(self):
        running = boat_build.Running("bx_x", "cmd_glob", "glob")
        with (
            patch.object(boat_build, "vm_exec", side_effect=running),
            tempfile.TemporaryDirectory() as out,
            self.assertRaises(boat_build.Running),
        ):
            boat_build.save_evidence(
                "bx_x", {}, Path(out), ["*.log"], warn_unmatched=True
            )


class GraftedSender(unittest.TestCase):
    """A grafted (shallow) sender packs parent links the VM cannot read.

    Mirror of the bx_taernc35 outage: the docs commit E hid behind the
    sender's shallow grafts (at X and D), so the bundle omitted it while the
    merge M above still links to it; `bundle verify` on the stale VM passes
    (no recorded prerequisites) but `git fetch` dies with "Could not read E".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "origin")
        self.origin = root / "origin"
        self.x = commit_file(self.origin, "f.txt", "a\n", "X")
        subprocess.run(["git", "branch", "base"], cwd=self.origin, check=True)
        subprocess.run(["git", "checkout", "-qb", "docs"], cwd=self.origin, check=True)
        self.e = commit_file(self.origin, "g.txt", "e\n", "E docs")
        self.d = commit_file(self.origin, "h.txt", "d\n", "D head")
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.origin, check=True)
        subprocess.run(
            ["git", "merge", "-q", "--no-ff", "docs", "-m", "M"],
            cwd=self.origin,
            check=True,
        )
        self.want = commit_file(self.origin, "i.txt", "w\n", "W")
        # Grafted sender with the production shape: full objects, but the
        # view stops at X and D, hiding E behind D's parent link (exactly
        # like the bx_taernc35 outage: grafts at dbf321f and d8c9bac hid
        # e07880f2, which the stale golden also lacked).
        subprocess.run(
            ["git", "clone", "-q", f"file://{self.origin}", "sender"],
            cwd=root,
            check=True,
        )
        self.sender = root / "sender"
        shallow = subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=self.sender,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        Path(shallow, "shallow").write_text(f"{self.x}\n{self.d}\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "clone",
                "-q",
                "--single-branch",
                "--branch",
                "base",
                f"file://{self.origin}",
                "vm",
            ],
            cwd=root,
            check=True,
        )
        self.vm = root / "vm"

    def revs(self, repo, ref):
        return subprocess.run(
            ["git", "rev-list", ref],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_fixture_hides_e_from_sender_but_not_from_merge(self):
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=self.sender,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "true",
        )
        # The merge links to E through D, but the grafted sender never
        # visits it: the lie every bundle built here is founded on.
        self.assertNotIn(self.e, self.revs(self.sender, self.want))
        self.assertNotEqual(
            subprocess.run(["git", "cat-file", "-e", self.e], cwd=self.vm).returncode,
            0,
        )

    def ship_full(self):
        subprocess.run(
            ["git", "tag", "-f", "bt", self.want],
            cwd=self.sender,
            check=True,
            capture_output=True,
        )
        bundle = str(Path(self.tmp.name) / "grafted.bundle")
        subprocess.run(
            ["git", "bundle", "create", bundle, "bt"],
            cwd=self.sender,
            check=True,
            capture_output=True,
        )
        return bundle

    def test_grafted_pack_verifies_but_does_not_fetch(self):
        bundle = self.ship_full()
        verify = subprocess.run(
            ["git", "bundle", "verify", bundle],
            cwd=self.vm,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        fetch = subprocess.run(
            ["git", "fetch", bundle, "bt:bt"],
            cwd=self.vm,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(fetch.returncode, 0)
        self.assertIn(f"Could not read {self.e}", fetch.stderr)

    def test_unshallow_sender_ships_fetchable_bundle(self):
        self.assertEqual(boat_build.ensure_sender_complete(self.sender), "unshallowed")
        self.assertEqual(boat_build.ensure_sender_complete(self.sender), "complete")
        self.assertIn(self.e, self.revs(self.sender, self.want))
        bundle = self.ship_full()
        subprocess.run(
            ["git", "fetch", bundle, "bt:bt"],
            cwd=self.vm,
            check=True,
            capture_output=True,
        )
        got = subprocess.run(
            ["git", "rev-parse", "bt"],
            cwd=self.vm,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(got, self.want)


class FakeResult:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


class RunBoatTimeout(unittest.TestCase):
    def test_transfers_get_the_stream_timeout(self):
        fake = type(sys)("toolchain")
        fake.child_env = lambda: {}
        fake.tool = lambda name: name
        seen = {}

        def run(command, **kwargs):
            seen[command[2]] = kwargs["timeout"]
            return subprocess.CompletedProcess(command, 0, "", "")

        with (
            patch.dict(sys.modules, {"toolchain": fake}),
            patch.object(boat_build.subprocess, "run", run),
        ):
            for verb in ("exec", "push", "scp", "status"):
                boat_build.run_boat(verb, "bx_x")
        self.assertEqual(seen, {"exec": 540, "push": 540, "scp": 540, "status": 60})


class RemoteWorkspace(unittest.TestCase):
    def test_prefers_first_existing_clone(self):
        with patch.object(
            boat_build, "vm_exec", return_value=(0, "blockfield-workspace\n")
        ):
            self.assertEqual(
                boat_build.remote_workspace("bx_x", "/h"), "/h/blockfield-workspace"
            )
        with patch.object(
            boat_build,
            "vm_exec",
            return_value=(0, "blockfield-workspace\nblockfield-workspace\n"),
        ):
            self.assertEqual(
                boat_build.remote_workspace("bx_x", "/h"),
                "/h/blockfield-workspace",
            )

    def test_missing_clone_fails(self):
        with patch.object(boat_build, "vm_exec", return_value=(0, "")):
            with self.assertRaises(RuntimeError):
                boat_build.remote_workspace("bx_x", "/h")


if __name__ == "__main__":
    unittest.main()


class UninitializedSubmodules(unittest.TestCase):
    """boat-build works with uninitialized submodules (ws-private#271).

    Git resolves a cwd without its own repo by walking upward, so every
    local access to a missing submodule checkout must be guarded: it must
    never diff, resolve, or pack the parent workspace by accident.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "origin")
        commit_file(root / "origin", "f.txt", "a\n", "init")
        make_repo(root / "super", origin=root / "origin")
        self.super = root / "super"
        # Fixture workspaces carry the server pin like the real checkout.
        (self.super / "pins").mkdir()
        (self.super / "pins" / "server.sha").write_text("b" * 40 + "\n")
        subprocess.run(["git", "add", "-A"], cwd=self.super, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "pin server"], cwd=self.super, check=True
        )

    def test_remote_uninitialized_submodule_is_initialized(self):
        origin = self.super.parent / "origin"
        subprocess.run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(origin),
                "client",
            ],
            cwd=self.super,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "add client"], cwd=self.super, check=True
        )
        guest = self.super.parent / "guest"
        subprocess.run(["git", "clone", "-q", str(self.super), str(guest)], check=True)
        want = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        def execute(_vm, script, timeout=120):
            result = subprocess.run(
                script,
                shell=True,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "GIT_ALLOW_PROTOCOL": "file"},
            )
            return result.returncode, result.stdout + result.stderr

        with patch.object(boat_build, "vm_exec", side_effect=execute):
            self.assertNotIn("client", boat_build.remote_shas("vm", str(guest)))
            boat_build.ensure_repo(
                "vm", str(guest), str(guest.parent), "client", want, guest.parent
            )
        self.assertTrue((guest / "client" / ".git").exists())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(guest / "client"), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            want,
        )

    def test_remote_init_ignores_unpushed_gitlink_with_relative_url(self):
        # The gitlink may name a local-only commit that reaches the VM later by bundle.
        origin = self.super.parent / "origin"
        git = ["git", "-c", "protocol.file.allow=always"]
        subprocess.run(
            [*git, "submodule", "add", str(origin), "client"],
            cwd=self.super,
            check=True,
            capture_output=True,
        )
        want = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=origin,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # The submodule clone has no identity of its own; CI has no global one.
        for key, value in (("user.email", "t@localhost"), ("user.name", "t")):
            subprocess.run(
                ["git", "config", key, value], cwd=self.super / "client", check=True
            )
        commit_file(self.super / "client", "f.txt", "local\n", "unpushed")
        # A relative URL resolves against the guest's own origin, so it must not reach `git clone` raw.
        subprocess.run(
            ["git", "config", "-f", ".gitmodules", "submodule.client.url", "../origin"],
            cwd=self.super,
            check=True,
        )
        subprocess.run(
            ["git", "add", "client", ".gitmodules"], cwd=self.super, check=True
        )
        subprocess.run(
            ["git", "commit", "-qm", "pin unpushed"], cwd=self.super, check=True
        )
        guest = self.super.parent / "guest"
        subprocess.run(["git", "clone", "-q", str(self.super), str(guest)], check=True)

        def execute(_vm, script, timeout=120):
            result = subprocess.run(
                script,
                shell=True,
                capture_output=True,
                text=True,
                env={**__import__("os").environ, "GIT_ALLOW_PROTOCOL": "file"},
            )
            return result.returncode, result.stdout + result.stderr

        with patch.object(boat_build, "vm_exec", side_effect=execute):
            # Fail-before: `submodule update --init` dies fetching the unpushed gitlink.
            boat_build.ensure_repo(
                "vm", str(guest), str(guest.parent), "client", want, guest.parent
            )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(guest / "client"), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            want,
        )

    def test_missing_checkout_has_no_patch(self):
        self.assertIsNone(boat_build.worktree_patch(self.super / "nope"))

    def test_empty_dir_has_no_patch(self):
        (self.super / "empty").mkdir()
        with open(self.super / "f.txt", "a", encoding="utf-8") as fh:
            fh.write("parent-dirty\n")
        # Fail-before: git walks upward and diffs the parent workspace.
        self.assertIsNone(boat_build.worktree_patch(self.super / "empty"))

    def test_submodule_override_rejects_uninitialized_dir(self):
        (self.super / "empty").mkdir()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.super,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        args = boat_build.parse_args(
            ["--vm", "bx_x", "--submodule", f"empty={head}", "--", "true"]
        )
        with patch.object(boat_build, "ROOT", self.super):
            # Fail-before: rev-parse walks upward and ships the parent SHA.
            with self.assertRaises(SystemExit) as ctx:
                boat_build.local_shas(args)
        self.assertIn("empty", str(ctx.exception))

    def test_bundle_without_checkout_names_submodule(self):
        def fake_vm_exec(vm_id, script, timeout=120):
            if "rev-parse HEAD" in script:
                return 1, ""
            return 1, "not found"  # fetch origin <want>: absent

        with (
            patch.object(boat_build, "ROOT", self.super),
            patch.object(boat_build, "vm_exec", side_effect=fake_vm_exec),
            patch.object(boat_build, "run_boat", return_value=FakeResult(0, "{}")),
        ):
            # Fail-before: FileNotFoundError from git in a missing cwd.
            with self.assertRaises(RuntimeError) as ctx:
                boat_build.ensure_repo(
                    "bx_x",
                    "/srv/guest/blockfield-workspace",
                    "/srv/guest",
                    "gone",
                    "a" * 40,
                    self.super,
                )
        self.assertIn("gone", str(ctx.exception))
        self.assertIn("cannot initialize submodule", str(ctx.exception))


class PinnedServer(unittest.TestCase):
    """The remote build checks out exactly the pinned server SHA (ws#88).

    The server is git-ignored, not a submodule, so ls-tree/submodule
    status never list it: the old code left the golden's stale server
    checkout on the VM while the task tested pinned server code.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        make_repo(root / "ws")
        self.ws = root / "ws"
        self.pin = "c" * 40
        (self.ws / "pins").mkdir()
        (self.ws / "pins" / "server.sha").write_text(self.pin + "\n")
        subprocess.run(["git", "add", "-A"], cwd=self.ws, check=True)
        subprocess.run(["git", "commit", "-qm", "pin server"], cwd=self.ws, check=True)

    def test_local_shas_includes_pin(self):
        args = boat_build.parse_args(["--vm", "bx_x", "--", "true"])
        with patch.object(boat_build, "ROOT", self.ws):
            self.assertEqual(boat_build.local_shas(args)["server"], self.pin)

    def test_branch_reads_branch_pin(self):
        new_pin = "d" * 40
        subprocess.run(["git", "checkout", "-qb", "srv"], cwd=self.ws, check=True)
        (self.ws / "pins" / "server.sha").write_text(new_pin + "\n")
        subprocess.run(["git", "commit", "-qam", "bump pin"], cwd=self.ws, check=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.ws, check=True)
        args = boat_build.parse_args(["--vm", "bx_x", "--branch", "srv", "--", "true"])
        with patch.object(boat_build, "ROOT", self.ws):
            self.assertEqual(boat_build.local_shas(args)["server"], new_pin)

    def test_missing_pin_fails_loudly(self):
        (self.ws / "pins" / "server.sha").unlink()
        subprocess.run(["git", "commit", "-qam", "drop pin"], cwd=self.ws, check=True)
        args = boat_build.parse_args(["--vm", "bx_x", "--", "true"])
        with patch.object(boat_build, "ROOT", self.ws):
            with self.assertRaises(SystemExit):
                boat_build.local_shas(args)

    def test_server_override_without_checkout_names_server_checkout(self):
        args = boat_build.parse_args(
            ["--vm", "bx_x", "--submodule", f"server={'e' * 40}", "--", "true"]
        )
        with patch.object(boat_build, "ROOT", self.ws):
            with self.assertRaises(SystemExit) as ctx:
                boat_build.local_shas(args)
        self.assertIn("server-checkout", str(ctx.exception))

    def run_server_ensure(self, replies):
        scripts = list(replies)

        def fake_vm_exec(vm_id, script, timeout=120):
            self.vm_scripts.append(script)
            return scripts.pop(0)

        self.vm_scripts = []
        stage = Path(self.tmp.name) / "stage"
        stage.mkdir(exist_ok=True)
        with (
            patch.object(boat_build, "ROOT", self.ws),
            patch.object(boat_build, "vm_exec", side_effect=fake_vm_exec),
            patch.object(boat_build, "run_boat", return_value=FakeResult(0, "{}")),
        ):
            return boat_build.ensure_repo(
                "bx_x",
                "/srv/guest/blockfield-workspace",
                "/srv/guest",
                "server",
                self.pin,
                stage,
            )

    def test_stale_server_checked_out_exactly(self):
        stale = "9" * 40
        self.assertEqual(
            self.run_server_ensure(
                [
                    (0, ""),  # server/.git exists: skip the clone
                    (0, stale),  # rev-parse HEAD: stale golden checkout
                    (0, self.pin),  # fetch origin <pin>: pushed
                    (0, "checked out"),  # checkout --detach
                    (0, self.pin),  # rev-parse HEAD: at the pin
                ]
            ),
            "checked out",
        )
        self.assertIn("blockfield-server", self.vm_scripts[0])
        checkout = next(s for s in self.vm_scripts if "checkout --detach" in s)
        self.assertIn(self.pin, checkout)

    def test_server_already_there_skips_transfer(self):
        self.assertEqual(
            self.run_server_ensure([(0, ""), (0, self.pin)]), "already there"
        )
        self.assertEqual(len(self.vm_scripts), 2)

    def test_server_head_mismatch_fails_loudly(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_server_ensure(
                [
                    (0, ""),
                    (0, "9" * 40),
                    (0, self.pin),
                    (0, "checked out"),
                    (0, "deadbee"),  # checkout ok... but HEAD is elsewhere
                ]
            )
        self.assertIn("server", str(ctx.exception))
        self.assertIn("deadbee", str(ctx.exception))

    def test_remote_shas_reports_server(self):
        guest = Path(self.tmp.name) / "guest"
        make_repo(guest)
        commit_file(guest, "f.txt", "a\n", "init")
        make_repo(guest / "server")
        want = commit_file(guest / "server", "s.txt", "s\n", "srv")
        guest_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=guest,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        def execute(_vm, script, timeout=120):
            result = subprocess.run(script, shell=True, capture_output=True, text=True)
            return result.returncode, result.stdout + result.stderr

        with patch.object(boat_build, "vm_exec", side_effect=execute):
            current = boat_build.remote_shas("vm", str(guest))
        self.assertEqual(current[""], guest_head)
        self.assertEqual(current["server"], want)
