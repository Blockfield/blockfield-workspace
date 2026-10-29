"""Pure-logic checks for scripts/dev/watch.py; no gradle, no jdb, no game."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "watch.py"
spec = importlib.util.spec_from_file_location("watch", SCRIPT)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


def make_jar(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


class ChangedClasses(unittest.TestCase):
    def test_only_differing_and_new_classes_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "old.jar", Path(tmp) / "new.jar"
            make_jar(old, {"a/B.class": b"1", "a/C.class": b"2"})
            make_jar(new, {"a/B.class": b"1", "a/C.class": b"3", "a/D.class": b"4"})
            changes = watch.changed_classes(old, new)
        self.assertEqual(sorted(d for d, _ in changes), ["a.C", "a.D"])

    def test_removed_classes_are_listed_and_require_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / "old.jar", Path(tmp) / "new.jar"
            make_jar(old, {"a/B.class": b"old"})
            make_jar(new, {})
            changes = watch.changed_classes(old, new)
            self.assertEqual(changes, [("a.B", "a/B.class")])
            self.assertTrue(watch.needs_restart(changes, old, new))


class RebuildCommand(unittest.TestCase):
    def test_gradle_build_uses_the_project_resource_limits(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(
                watch.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run,
        ):
            watch.remap_jar(Path(tmp), "server")
        self.assertIn("-Dorg.gradle.daemon.idletimeout=600000", run.call_args.args[0])
        self.assertNotIn("--no-daemon", run.call_args.args[0])
        self.assertIn("--max-workers=2", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 600)


class NeedsRestart(unittest.TestCase):
    def test_mixin_classes_go_through_restart(self):
        self.assertTrue(watch.needs_restart([("com.foo.MixinPlayer", "x")]))
        self.assertFalse(watch.needs_restart([("com.foo.PlayerLogic", "x")]))

    def test_new_classes_trigger_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.jar"
            make_jar(old, {"a/B.class": b"1"})
            self.assertTrue(
                watch.needs_restart([("a.NewClass", "a/NewClass.class")], old_jar=old)
            )
            self.assertFalse(watch.needs_restart([("a.B", "a/B.class")], old_jar=old))


class RedefineFailed(unittest.TestCase):
    def test_redefine_uses_relative_class_paths_in_a_directory_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="bf watch ") as tmp:
            root = Path(tmp)
            jar = root / "new.jar"
            make_jar(jar, {"a/B.class": b"new"})
            work = root / "work with spaces"
            work.mkdir()
            with (
                patch.object(watch.tempfile, "mkdtemp", return_value=str(work)),
                patch.object(watch, "jdb_path", return_value="jdb"),
                patch.object(
                    watch.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run,
            ):
                success, _ = watch.redefine(5005, [("a.B", "a/B.class")], jar)
                self.assertTrue(success)
                self.assertEqual(run.call_args.kwargs["cwd"], work)
                self.assertEqual(
                    run.call_args.kwargs["input"], "redefine a.B a/B.class\nquit\n"
                )

    def test_jdb_prompt_does_not_hide_redefine_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = Path(tmp) / "new.jar"
            make_jar(jar, {"a/B.class": b"new"})
            output = SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=(
                    "Initializing jdb ...\n> Error redefining a.B: schema change not implemented\n> "
                ),
            )
            with (
                patch.object(watch, "jdb_path", return_value="jdb"),
                patch.object(watch.subprocess, "run", return_value=output),
            ):
                success, message = watch.redefine(5005, [("a.B", "a/B.class")], jar)
            self.assertFalse(success)
            self.assertIn("schema change", message)

    def test_schema_change_counts_as_failure(self):
        self.assertTrue(
            watch.REDEFINE_FAILED.search("com.sun.jdi... schema change not implemented")
        )
        self.assertTrue(
            watch.REDEFINE_FAILED.search(
                "Operation not supported on target VM: unable to redefine"
            )
        )
        self.assertTrue(watch.REDEFINE_FAILED.search("Fatal error: connection refused"))
        self.assertFalse(watch.REDEFINE_FAILED.search(""))


class WatchLoop(unittest.TestCase):
    def test_locked_runtime_jar_requests_restart_after_redefine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "mods"
            runtime.mkdir()
            old = runtime / "blockfield-server-1.jar"
            built = root / old.name
            make_jar(old, {"a/B.class": b"old"})
            make_jar(built, {"a/B.class": b"new"})
            with (
                patch.object(watch, "newest_source_mtime", return_value=100),
                patch.object(watch, "side_dirs", return_value=[root]),
                patch.object(
                    watch, "deploy_built_jar", side_effect=PermissionError("locked jar")
                ),
            ):
                result = watch.watch_loop(
                    "server",
                    5005,
                    runtime,
                    root,
                    once=True,
                    poll=0,
                    rebuild=lambda *_: built,
                    hotswap=lambda *_: (True, ""),
                )
            self.assertEqual(result, "restart")
            self.assertEqual(watch.class_entries(old)["a/B.class"], b"old")

    def test_deploy_replaces_jar_without_changing_open_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "mods"
            target.mkdir()
            old = target / "blockfield-server-1.jar"
            old.write_bytes(b"old")
            built = root / old.name
            built.write_bytes(b"new")
            with old.open("rb") as loaded:
                if sys.platform == "win32":
                    with self.assertRaises(PermissionError):
                        watch.deploy_built_jar("server", built, target, root)
                else:
                    watch.deploy_built_jar("server", built, target, root)
                self.assertEqual(loaded.read(), b"old")
            if sys.platform == "win32":
                watch.deploy_built_jar("server", built, target, root)
            self.assertEqual(old.read_bytes(), b"new")

    def test_successive_rebuilds_at_same_path_both_hotswap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "mods"
            runtime.mkdir()
            old = runtime / "blockfield-server-1.jar"
            built = root / "build" / old.name
            built.parent.mkdir()
            make_jar(old, {"a/B.class": b"old"})
            builds, swaps = [], []

            def rebuild(*_):
                builds.append(len(builds))
                make_jar(built, {"a/B.class": str(builds[-1]).encode()})
                return built

            def hotswap(_, changes, *_args):
                swaps.append(changes)
                return True, ""

            with (
                patch.object(watch, "newest_source_mtime", side_effect=[100, 101, 102]),
                patch.object(watch, "side_dirs", return_value=[root]),
                self.assertRaises(StopIteration),
            ):
                watch.watch_loop(
                    "server",
                    5005,
                    runtime,
                    root,
                    poll=0,
                    rebuild=rebuild,
                    hotswap=hotswap,
                )
            self.assertEqual(len(builds), 2)
            self.assertEqual(len(swaps), 2)

    def test_redefine_path_returns_redefine(self):
        real = (
            watch.side_dirs,
            watch.newest_source_mtime,
            watch.deployed_jar,
            watch.changed_classes,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = tmp / "localmods" / "blockfield-mod" / "server" / "src"
            src.mkdir(parents=True)
            (src / "A.java").write_bytes(b"class A {}")
            old = tmp / "blockfield-server-1.jar"
            built = tmp / "build" / old.name
            built.parent.mkdir()
            make_jar(old, {"a/B.class": b"1"})
            make_jar(built, {"a/B.class": b"2"})
            watch.side_dirs = lambda side: [tmp / "localmods" / "blockfield-mod"]
            watch.newest_source_mtime = lambda side: 100.0
            watch.deployed_jar = lambda side, mods, game: old
            watch.changed_classes = lambda a, b: [("a.B", "a/B.class")]
            try:
                result = watch.watch_loop(
                    "server",
                    5005,
                    tmp,
                    tmp,
                    poll=0,
                    once=True,
                    rebuild=lambda d, s, j=None: built,
                    hotswap=lambda port, changes, jar, jh=None: (True, ""),
                )
            finally:
                (
                    watch.side_dirs,
                    watch.newest_source_mtime,
                    watch.deployed_jar,
                    watch.changed_classes,
                ) = real
        self.assertEqual(result, "redefine")

    def test_failed_redefine_returns_restart(self):
        real = (
            watch.side_dirs,
            watch.newest_source_mtime,
            watch.deployed_jar,
            watch.changed_classes,
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old, built = tmp / "old.jar", tmp / "new.jar"
            make_jar(old, {"a/B.class": b"1"})
            make_jar(built, {"a/B.class": b"2"})
            watch.side_dirs = lambda side: [tmp / "mod"]
            watch.newest_source_mtime = lambda side: 100.0
            watch.deployed_jar = lambda side, mods, game: old
            watch.changed_classes = lambda a, b: [("a.B", "a/B.class")]
            try:
                result = watch.watch_loop(
                    "server",
                    5005,
                    tmp,
                    tmp,
                    poll=0,
                    once=True,
                    rebuild=lambda d, s, j=None: built,
                    hotswap=lambda port, changes, jar, jh=None: (
                        False,
                        "schema change",
                    ),
                )
            finally:
                (
                    watch.side_dirs,
                    watch.newest_source_mtime,
                    watch.deployed_jar,
                    watch.changed_classes,
                ) = real
        self.assertEqual(result, "restart")


class BothWatchLoop(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mods = self.root / "server-mods"
        self.game = self.root / "game"
        self.mods.mkdir()
        (self.game / "mods").mkdir(parents=True)
        self.sources = {}
        self.built = {}
        for side in ("server", "client", "common"):
            source = self.root / side / "src" / "A.java"
            source.parent.mkdir(parents=True)
            source.write_text("class A {}")
            os.utime(source, (100, 100))
            self.sources[side] = source
        for side in ("server", "client"):
            runtime = self.mods if side == "server" else self.game / "mods"
            make_jar(runtime / f"blockfield-{side}-1.jar", {"a/B.class": b"old"})
            self.built[side] = self.root / f"blockfield-{side}-1.jar"
        self.events = []
        self.revision = 0
        self.receipt = self.root / "receipt.json"
        self.identities = {"server": [1, "birth-1"], "client": [2, "birth-2"]}
        patcher = patch.object(watch, "side_dirs", return_value=[self.root])
        patcher.start()
        self.addCleanup(patcher.stop)

    def rebuild(self, _, side, _java=None):
        self.events.append(("build", side))
        make_jar(self.built[side], {"a/B.class": str(self.revision).encode()})
        return self.built[side]

    def hotswap(self, port, changes, jar, _java=None):
        side = "server" if port == watch.SERVER_JDWP else "client"
        self.events.append(("swap", side))
        self.assertEqual(changes, [("a.B", "a/B.class")])
        self.assertEqual(jar, self.built[side])
        return True, ""

    def run_changes(self, changes, **kwargs):
        pending = iter(changes)

        def poll(_):
            changed = next(pending)
            self.revision += 1
            for side in changed:
                os.utime(self.sources[side], (100 + self.revision,) * 2)

        with patch.object(watch.time, "sleep", side_effect=poll):
            return watch.watch_loop(
                "both",
                None,
                self.mods,
                self.game,
                poll=0,
                rebuild=self.rebuild,
                hotswap=self.hotswap,
                **kwargs,
            )

    def run_once(self, **kwargs):
        return self.run_changes(
            [()],
            once=True,
            receipt_path=self.receipt,
            identity=lambda side: self.identities[side],
            **kwargs,
        )

    def test_receipt_skips_unchanged_sources_jars_and_processes(self):
        self.run_once()
        self.events.clear()
        guarded = []
        self.assertEqual(
            self.run_once(guard=guarded.append), {"server": "idle", "client": "idle"}
        )
        self.assertEqual(self.events, [])
        self.assertEqual(guarded, ["server", "client"])

    def test_receipt_selects_changed_side_and_common_without_mtime_changes(self):
        self.run_once()
        for source_side, selected in (
            ("server", ["server"]),
            ("client", ["client"]),
            ("common", ["server", "client"]),
        ):
            with self.subTest(source=source_side):
                source = self.sources[source_side]
                source.write_text("class A { void changed() {} }")
                os.utime(source, (100, 100))
                self.events.clear()
                self.run_once()
                self.assertEqual(
                    [side for event, side in self.events if event == "build"], selected
                )

    def test_receipt_detects_renaming_and_removing_last_java_source(self):
        self.run_once()
        source = self.sources["server"]
        renamed = source.with_name("Renamed.java")
        source.rename(renamed)
        self.events.clear()
        self.run_once()
        self.assertEqual(self.events, [("build", "server"), ("swap", "server")])
        renamed.unlink()
        self.events.clear()
        self.run_once()
        self.assertEqual(self.events, [("build", "server"), ("swap", "server")])

    def test_receipt_detects_replaced_runtime_jar(self):
        self.run_once()
        current = watch.deployed_jar("client", self.mods, self.game)
        make_jar(current, {"a/B.class": b"manual replacement"})
        self.events.clear()

        def restart(side, built):
            self.events.append(("restart", side))
            watch.deploy_built_jar(side, built, self.mods, self.game)

        result = self.run_once(restart=restart)
        self.assertEqual(result, {"server": "idle", "client": "restart"})
        self.assertEqual(self.events, [("build", "client"), ("restart", "client")])

    def test_exact_rebuilt_disk_replacement_requires_verified_restart(self):
        self.identities["client"] = {
            "process": [2, "birth-2"],
            "runtime_manifest_sha256": "c" * 64,
        }
        self.run_once()
        previous = self.receipt.read_bytes()
        current = watch.deployed_jar("client", self.mods, self.game)
        make_jar(self.built["client"], {"a/B.class": b"manual replacement"})
        current.write_bytes(self.built["client"].read_bytes())

        def rebuild(_, side, _java=None):
            self.events.append(("build", side))
            return self.built[side]

        self.events.clear()
        with (
            patch.object(self, "rebuild", side_effect=rebuild),
            self.assertRaisesRegex(watch.WatchError, "requires a restart"),
        ):
            self.run_once()
        self.assertEqual(self.receipt.read_bytes(), previous)

        def restart(side, built):
            self.events.append(("restart", side))
            watch.deploy_built_jar(side, built, self.mods, self.game)
            self.identities[side]["process"] = [99, "new birth"]

        self.events.clear()
        with patch.object(self, "rebuild", side_effect=rebuild):
            result = self.run_once(restart=restart)
        self.assertEqual(result, {"server": "idle", "client": "restart"})
        self.assertEqual(self.events, [("build", "client"), ("restart", "client")])
        saved = json.loads(self.receipt.read_text())["sides"]["client"]
        self.assertEqual(saved["jar"], watch.jar_fingerprint(current))
        self.assertEqual(saved["identity"], self.identities["client"])

    def test_receipt_detects_changed_process_identity(self):
        self.run_once()
        self.identities["server"] = [99, "another birth"]
        self.events.clear()
        self.run_once()
        self.assertEqual(self.events, [("build", "server"), ("swap", "server")])

    def test_changed_artifact_name_preserves_installed_basename_and_receipt(self):
        self.run_once()
        current = watch.deployed_jar("server", self.mods, self.game)
        self.built["server"] = self.root / "blockfield-server-next-version.jar"
        self.sources["server"].write_text("class A { void changed() {} }")
        self.events.clear()
        result = self.run_once()
        self.assertEqual(result, {"server": "redefine", "client": "idle"})
        self.assertEqual(self.events, [("build", "server"), ("swap", "server")])
        self.assertEqual(watch.deployed_jar("server", self.mods, self.game), current)
        self.assertEqual(current.read_bytes(), self.built["server"].read_bytes())
        self.assertFalse((self.mods / self.built["server"].name).exists())
        saved = json.loads(self.receipt.read_text())["sides"]["server"]
        self.assertEqual(saved["jar"], watch.jar_fingerprint(current))

    def test_corrupt_receipt_rebuilds_both_sides(self):
        self.run_once()
        for text in ("{broken", '{"schema":1,"sides":[]}'):
            with self.subTest(receipt=text):
                self.receipt.write_text(text)
                self.events.clear()
                self.run_once()
                self.assertEqual(
                    [side for event, side in self.events if event == "build"],
                    ["server", "client"],
                )

    def test_unsupported_resource_or_gradle_change_fails_before_build(self):
        self.run_once()
        previous = self.receipt.read_bytes()
        for path in (
            self.root / "client/src/main/resources/new.json",
            self.root / "gradle.properties",
        ):
            with self.subTest(input=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("changed")
                self.events.clear()
                with self.assertRaisesRegex(
                    watch.WatchError, "use build-mods and setup"
                ):
                    self.run_once()
                self.assertEqual(self.events, [])
                self.assertEqual(self.receipt.read_bytes(), previous)
                path.unlink()

    def test_classless_jar_change_restarts_and_records_installed_jar(self):
        self.run_once()
        self.sources["client"].write_text("class A { void changed() {} }")
        self.events.clear()
        current = watch.deployed_jar("client", self.mods, self.game)
        entries = watch.class_entries(current) | {"META-INF/MANIFEST.MF": b"changed"}

        def rebuild(_, side, _java=None):
            self.events.append(("build", side))
            make_jar(self.built[side], entries)
            return self.built[side]

        def restart(side, built):
            self.events.append(("restart", side))
            watch.deploy_built_jar(side, built, self.mods, self.game)

        with patch.object(self, "rebuild", side_effect=rebuild):
            result = self.run_once(restart=restart)
        self.assertEqual(result, {"server": "idle", "client": "restart"})
        self.assertEqual(self.events, [("build", "client"), ("restart", "client")])
        saved = json.loads(self.receipt.read_text())["sides"]["client"]
        self.assertEqual(saved["jar"], watch.jar_fingerprint(current))

    def test_failed_apply_does_not_advance_receipt(self):
        self.run_once()
        self.sources["server"].write_text("class A { void changed() {} }")
        previous = self.receipt.read_bytes()
        with (
            patch.object(self, "hotswap", return_value=(False, "schema change")),
            self.assertRaisesRegex(watch.WatchError, "restart failed"),
        ):
            self.run_once(
                restart=lambda *_: (_ for _ in ()).throw(OSError("restart failed"))
            )
        self.assertEqual(self.receipt.read_bytes(), previous)

    def test_common_changes_update_both_serially_using_each_deployed_baseline(self):
        with self.assertRaises(StopIteration):
            self.run_changes([("common",), ("common",)])
        self.assertEqual(
            self.events,
            [
                ("build", "server"),
                ("build", "client"),
                ("swap", "server"),
                ("swap", "client"),
            ]
            * 2,
        )
        for side in ("server", "client"):
            deployed = watch.deployed_jar(side, self.mods, self.game)
            self.assertEqual(watch.class_entries(deployed), {"a/B.class": b"2"})

    def test_side_only_changes_do_not_rebuild_the_other_side(self):
        with self.assertRaises(StopIteration):
            self.run_changes([("server",), ("client",)])
        self.assertEqual(
            self.events,
            [
                ("build", "server"),
                ("swap", "server"),
                ("build", "client"),
                ("swap", "client"),
            ],
        )

    def test_pending_server_restart_still_updates_client_common_change(self):
        original_rebuild = self.rebuild

        def rebuild(directory, side, java):
            built = original_rebuild(directory, side, java)
            if side == "server":
                make_jar(built, {"a/B.class": b"new", "a/New.class": b"new"})
            return built

        with patch.object(self, "rebuild", side_effect=rebuild):
            result = self.run_changes([("common",)])
        self.assertEqual(result, {"server": "restart", "client": "redefine"})
        self.assertEqual(
            self.events, [("build", "server"), ("build", "client"), ("swap", "client")]
        )

    def test_failed_second_build_leaves_both_runtime_jars_untouched(self):
        rebuild = self.rebuild

        def fail_server(directory, side, java):
            if side == "client":
                self.events.append(("build", side))
                return None
            return rebuild(directory, side, java)

        with (
            patch.object(self, "rebuild", side_effect=fail_server),
            self.assertRaisesRegex(watch.WatchError, "client build failed"),
        ):
            self.run_changes([("common",)], once=True)
        self.assertEqual(self.events, [("build", "server"), ("build", "client")])
        for side in ("server", "client"):
            jar = watch.deployed_jar(side, self.mods, self.game)
            self.assertEqual(watch.class_entries(jar), {"a/B.class": b"old"})

    def test_once_both_guards_each_operation_and_restarts_with_exact_built_jar(self):
        rebuild = self.rebuild
        deploy = watch.deploy_built_jar

        def new_class(directory, side, java):
            built = rebuild(directory, side, java)
            make_jar(built, {"a/New.class": side.encode()})
            return built

        def restart(side, built):
            self.events.append(("restart", side))
            self.assertEqual(built, self.built[side])
            deploy(side, built, self.mods, self.game)

        with patch.object(self, "rebuild", side_effect=new_class):
            result = self.run_changes(
                [()],
                once=True,
                guard=lambda side: self.events.append(("guard", side)),
                restart=restart,
            )
        self.assertEqual(result, {"server": "restart", "client": "restart"})
        self.assertEqual(
            self.events,
            [
                ("guard", "server"),
                ("build", "server"),
                ("guard", "client"),
                ("build", "client"),
                ("guard", "server"),
                ("guard", "client"),
                ("guard", "server"),
                ("restart", "server"),
                ("guard", "server"),
                ("guard", "client"),
                ("restart", "client"),
                ("guard", "client"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
