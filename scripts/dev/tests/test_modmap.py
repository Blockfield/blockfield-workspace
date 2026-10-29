"""Units for modmap: explicit localmods/ ownership, no prefix guessing."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("modmap", HERE.parent / "modmap.py")
modmap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modmap)
EXAMPLE = HERE.parent.parent.parent / "localmods" / "mods.json.example"
PACK_SPEC = importlib.util.spec_from_file_location(
    "pack_build_modmap", HERE.parent / "pack_build.py"
)
pack_build = importlib.util.module_from_spec(PACK_SPEC)
PACK_SPEC.loader.exec_module(pack_build)


class Ownership(unittest.TestCase):
    def test_renamed_fork_resolves_by_mapping_not_prefix(self):
        mapping = modmap.load_mapping(EXAMPLE)
        self.assertEqual(modmap.owner("parcool.pw.toml", mapping), "parcool-fabric")

    def test_mod_covers_both_sides(self):
        mapping = modmap.load_mapping(EXAMPLE)
        self.assertEqual(
            modmap.owner("blockfield-client.pw.toml", mapping), "blockfield-mod"
        )
        self.assertEqual(
            modmap.owner("blockfield-server.pw.toml", mapping), "blockfield-mod"
        )

    def test_dropped_fork_has_no_owner(self):
        mapping = modmap.load_mapping(EXAMPLE)
        self.assertNotIn("sbw-drone-warfare-fabric", mapping)
        self.assertNotIn(
            "sbw-drone-warfare-fabric", set(modmap.owners(mapping).values())
        )


class Resolve(unittest.TestCase):
    def test_unknown_metafile_has_no_owner(self):
        self.assertIsNone(modmap.owner("nope.pw.toml", {}))


class MappingDefaults(unittest.TestCase):
    def test_stale_local_map_adds_new_defaults_and_preserves_whole_node_overrides(self):
        defaults = modmap.load_mapping(EXAMPLE)
        overrides = {
            name: entry
            for name, entry in defaults.items()
            if name not in ("lr-tactical", "tacz-refabricated")
        }
        overrides["parcool-fabric"] = {"both": "custom-parcool.pw.toml"}
        overrides["create-fabric"] = {}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mods.json"
            original = json.dumps(overrides)
            path.write_text(original, encoding="utf-8")
            path.with_name("mods.json.example").write_text(
                json.dumps(defaults), encoding="utf-8"
            )
            mapping = modmap.load_mapping(path)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
        for name in ("lr-tactical", "tacz-refabricated"):
            self.assertEqual(mapping[name], defaults[name])
        self.assertEqual(mapping["parcool-fabric"], overrides["parcool-fabric"])
        self.assertEqual(mapping["create-fabric"], {})


class LocalJars(unittest.TestCase):
    def _tree(self, tmp):
        root = Path(tmp)
        (root / "localmods").mkdir(parents=True)
        libs = root / "localmods" / "blockfield-mod" / "server" / "build" / "libs"
        libs.mkdir(parents=True)
        (libs / "blockfield-server-1.0+mc.jar").write_bytes(b"PK-new")
        return root

    def test_metafile_maps_to_built_jar(self):
        import tempfile

        mapping = {"blockfield-mod": {"server": "blockfield-server.pw.toml"}}
        with tempfile.TemporaryDirectory() as tmp:
            jars = modmap.local_jars(self._tree(tmp), mapping)
        self.assertEqual(
            jars["blockfield-server.pw.toml"].name, "blockfield-server-1.0+mc.jar"
        )

    def test_unbuilt_dir_contributes_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "localmods").mkdir()
            self.assertEqual(
                modmap.local_jars(
                    root, {"blockfield-mod": {"server": "blockfield-server.pw.toml"}}
                ),
                {},
            )

    def test_sources_and_dev_jars_ignored(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._tree(tmp)
            libs = root / "localmods" / "blockfield-mod" / "server" / "build" / "libs"
            for name in (
                "blockfield-server-1.0-sources.jar",
                "blockfield-server-1.0-dev.jar",
            ):
                (libs / name).write_bytes(b"PK")
            mapping = {"blockfield-mod": {"server": "blockfield-server.pw.toml"}}
            self.assertEqual(
                modmap.local_jars(root, mapping)["blockfield-server.pw.toml"].name,
                "blockfield-server-1.0+mc.jar",
            )

    def test_oracle_classifier_jars_never_win_by_mtime(self):
        import tempfile

        # Stand-batch2 trap: `check` runs the oracleTest gate, which rebuilds
        # -oracle.jar/-oracle-named.jar after remapJar, so newest-by-mtime
        # installed the ORACLE test jar as the client mod. Twice.
        mapping = {
            "blockfield-mod": {
                "client": "blockfield-client.pw.toml",
                "server": "blockfield-server.pw.toml",
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for side in ("client", "server"):
                libs = root / "localmods" / "blockfield-mod" / side / "build" / "libs"
                libs.mkdir(parents=True)
                full = libs / f"blockfield-{side}-1.0+mc.jar"
                full.write_bytes(b"PK-full")
                os.utime(full, (1_000_000_000, 1_000_000_000))
                for classifier in ("-oracle.jar", "-oracle-named.jar", "-test.jar"):
                    decoy = libs / f"blockfield-{side}-1.0+mc{classifier}"
                    decoy.write_bytes(b"PK-decoy")
                    os.utime(decoy, (2_000_000_000, 2_000_000_000))
            jars = modmap.local_jars(root, mapping)
            self.assertEqual(
                jars["blockfield-client.pw.toml"].name,
                "blockfield-client-1.0+mc.jar",
            )
            self.assertEqual(
                jars["blockfield-server.pw.toml"].name,
                "blockfield-server-1.0+mc.jar",
            )


class RecipeJars(unittest.TestCase):
    def test_exact_pair_ignores_newer_old_upstream_and_gradle_jars_in_both_packs(self):
        cases = (
            (
                "lr-tactical",
                "lrtactical-fabric.pw.toml",
                "lrtactical-1.21.1-0.4.1-bf2.jar",
                "lrtactical-1.21.1-0.4.1-bf1.jar",
                "lrtactical-1.21.1-0.4.1.jar",
            ),
            (
                "tacz-refabricated",
                "tacz-refabricated.pw.toml",
                "TACZ-Refabricated-1.21.1-0.7.0-bf8.jar",
                "TACZ-Refabricated-1.21.1-0.7.0-bf7.jar",
                "TACZ-Refabricated-1.21.1-0.7.0-forge1.1.8-hotfix.jar",
            ),
        )
        defaults = modmap.load_mapping(EXAMPLE)
        mapping = {name: defaults[name] for name, *_ in cases}
        with tempfile.TemporaryDirectory(prefix="bf recipe jars ") as tmp:
            root = Path(tmp)
            candidates = {}
            for name, metafile, candidate_name, old_name, upstream_name in cases:
                self.assertEqual(mapping[name]["both"], metafile)
                self.assertEqual(mapping[name]["jar"], f"files/mods/{candidate_name}")
                directory = root / "localmods" / name
                candidate = directory / mapping[name]["jar"]
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(b"PK-candidate-" + name.encode())
                os.utime(candidate, (1_000_000_000, 1_000_000_000))
                candidates[metafile] = candidate
                for relative in (
                    f"files/mods/{old_name}",
                    f"files/mods/{upstream_name}",
                    "build/libs/newer.jar",
                ):
                    decoy = directory / relative
                    decoy.parent.mkdir(parents=True, exist_ok=True)
                    decoy.write_bytes(b"PK-decoy")
                    os.utime(decoy, (2_000_000_000, 2_000_000_000))
            jars = modmap.local_jars(root, mapping)
            self.assertEqual(jars, candidates)
            for side in ("client", "server"):
                with self.subTest(side=side):
                    original = Mock(return_value="published")
                    module = SimpleNamespace(resolve_mod=original)
                    library = SimpleNamespace(safe_path=lambda value: value)
                    pack_build.overlay(module, library, jars)
                    for _, metafile, _, published_name, _ in cases:
                        item = (f"mods/{metafile}", {"filename": published_name})
                        self.assertEqual(
                            module.resolve_mod(root / side, item, {}),
                            (
                                *item,
                                f"mods/{published_name}",
                                candidates[metafile].read_bytes(),
                            ),
                        )
                    original.assert_not_called()
                    other = ("mods/unrelated.pw.toml", {"filename": "unrelated.jar"})
                    self.assertEqual(
                        module.resolve_mod(root / side, other, {}), "published"
                    )
                    original.assert_called_once_with(root / side, other, {})

    def test_missing_exact_candidate_uses_published_resolver_despite_other_builds(self):
        mapping = {"recipe": {"both": "recipe.pw.toml", "jar": "files/mods/new.jar"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ("build/libs/old.jar", "files/mods/upstream.jar"):
                decoy = root / "localmods" / "recipe" / relative
                decoy.parent.mkdir(parents=True, exist_ok=True)
                decoy.write_bytes(b"PK-old")
            jars = modmap.local_jars(root, mapping)
            self.assertEqual(jars, {})
            original = Mock(return_value="published")
            module = SimpleNamespace(resolve_mod=original)
            pack_build.overlay(module, SimpleNamespace(), jars)
            item = ("mods/recipe.pw.toml", {"filename": "published.jar"})
            self.assertEqual(module.resolve_mod(root, item, {}), "published")
            original.assert_called_once_with(root, item, {})

    def test_nonliteral_absolute_and_parent_paths_are_rejected_on_either_os(self):
        invalid = (
            None,
            "",
            "files/*.jar",
            "files/?.jar",
            "files/[ab].jar",
            "/outside.jar",
            r"C:\outside.jar",
            "C:outside.jar",
            r"\outside.jar",
            r"\\host\share\outside.jar",
            "../outside.jar",
            "files/../../outside.jar",
            r"files\..\outside.jar",
            "files/not-a-jar.zip",
        )
        with tempfile.TemporaryDirectory() as tmp:
            for value in invalid:
                with (
                    self.subTest(path=value),
                    self.assertRaisesRegex(ValueError, "JAR path"),
                ):
                    modmap.local_jars(
                        Path(tmp), {"recipe": {"both": "recipe.pw.toml", "jar": value}}
                    )

    def test_exact_jar_symlink_cannot_escape_its_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.jar"
            outside.write_bytes(b"PK-outside")
            directory = root / "localmods" / "recipe"
            directory.mkdir(parents=True)
            try:
                (directory / "candidate.jar").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"Native symlinks unavailable: {exc}")
            with self.assertRaisesRegex(ValueError, "escapes recipe"):
                modmap.local_jars(
                    root,
                    {"recipe": {"both": "recipe.pw.toml", "jar": "candidate.jar"}},
                )


def git(cwd, *args):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def commit(cwd, name="file.txt", data=b"x"):
    path = Path(cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    git(cwd, "add", name)
    git(cwd, "-c", "user.email=t@localhost", "-c", "user.name=t", "commit", "-qm", name)


def fork_tree(tmp, tag="bf1", pin=None):
    """root/ with packs pinning one fork metafile at tag and a git checkout."""
    root = Path(tmp)
    for side in ("client", "server"):
        mods = root / side / "mods"
        mods.mkdir(parents=True)
        (mods / "fork.pw.toml").write_text(
            'filename = "fork.jar"\n[download]\n'
            f'url = "https://example.com/fork/releases/download/{pin or tag}/fork.jar"\n',
            encoding="utf-8",
        )
    directory = root / "localmods" / "fork"
    directory.mkdir(parents=True)
    git(directory, "init", "-qb", "main")
    commit(directory, ".gitignore", b"/build/\n")
    commit(directory, "src/main/Fork.java", b"v1")
    git(directory, "tag", tag)
    mapping = {"fork": {"both": "fork.pw.toml"}}
    return root, directory, mapping


class Classify(unittest.TestCase):
    def test_head_equal_to_pin_uses_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            self.assertEqual(
                modmap.classify(root, "fork", mapping["fork"])[0], modmap.PIN
            )

    def test_head_older_than_pin_uses_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            commit(directory, "src/main/New.java", b"v2")
            git(directory, "tag", "-f", "bf1")
            git(directory, "reset", "-q", "--hard", "HEAD~1")
            source, reason = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.PIN)
            self.assertIn("older than pin", reason)

    def test_irrelevant_only_diff_uses_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            for name in (
                "README.md",
                ".github/workflows/build.yml",
                "docs/guide.md",
                "src/test/java/ForkTest.java",
                ".editorconfig",
                "dev/quality.py",
            ):
                commit(directory, name, b"docs")
            source, reason = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.PIN)
            self.assertIn("jar-irrelevant", reason)

    def test_cache_jar_override_forces_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            commit(directory, "src/main/New.java", b"v2")
            mapping["fork"]["jar"] = ".cache/published-baseline-missing.jar"
            source, reason = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.PIN)
            self.assertIn("forced", reason)

    def test_test_package_inside_src_main_is_relevant(self):
        self.assertFalse(
            modmap.irrelevant_path("src/main/java/com/fork/test/Probe.java")
        )
        self.assertTrue(modmap.irrelevant_path("src/test/java/com/fork/Probe.java"))
        self.assertTrue(modmap.irrelevant_path("tests/fixture.json"))

    def test_relevant_diff_builds_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            commit(directory, "src/main/Fork.java", b"v2")
            commit(directory, "build.gradle", b"v2")
            source, reason = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.LOCAL)
            self.assertIn("relevant changes", reason)

    def test_dirty_checkout_builds_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            (directory / "src/main/Fork.java").write_bytes(b"dirty")
            source, _ = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.LOCAL)
            (directory / "src/main/Fork.java").write_bytes(b"v1")
            (directory / "new-source.java").write_bytes(b"untracked")
            source, _ = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.LOCAL)

    def test_missing_tag_builds_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp, pin="bf-missing")
            source, reason = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.LOCAL)
            self.assertIn("not in local clone", reason)

    def test_tag_sharing_a_branch_name_resolves_the_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            git(directory, "branch", "bf1")  # branch at the tag commit
            commit(directory, "src/main/Fork.java", b"v2")
            git(directory, "branch", "-f", "bf1", "HEAD")
            # HEAD is the branch tip; the tag still pins the older commit, whose
            # diff to HEAD is relevant. Resolving the branch would report a pin.
            source, _ = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.LOCAL)
            git(directory, "reset", "-q", "--hard", "refs/tags/bf1")
            source, _ = modmap.classify(root, "fork", mapping["fork"])
            self.assertEqual(source, modmap.PIN)

    def test_pin_decision_hides_built_jars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            libs = directory / "build" / "libs"
            libs.mkdir(parents=True)
            (libs / "fork-bf1.jar").write_bytes(b"PK-stale")
            self.assertEqual(modmap.local_jars(root, mapping), {})
            commit(directory, "src/main/Fork.java", b"v2")
            self.assertEqual(list(modmap.local_jars(root, mapping)), ["fork.pw.toml"])


class Stamps(unittest.TestCase):
    def _stamped(self, tmp):
        root, directory, mapping = fork_tree(tmp)
        home = Path(tmp) / "home"
        libs = directory / "build" / "libs"
        libs.mkdir(parents=True)
        jar = libs / "fork-bf1.jar"
        jar.write_bytes(b"PK")
        jars = modmap._scan_jars(root, "fork", mapping["fork"])
        modmap.write_stamp(home, "fork", directory, jars)
        return root, directory, mapping, home, jar, jars

    def test_fresh_stamp_hits_and_source_or_jar_changes_miss(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping, home, jar, jars = self._stamped(tmp)
            self.assertTrue(modmap.stamp_fresh(home, "fork", directory, jars))
            (directory / "src/main/Fork.java").write_bytes(b"edited")
            self.assertFalse(modmap.stamp_fresh(home, "fork", directory, jars))
            git(directory, "checkout", "-q", "--", "src/main/Fork.java")
            self.assertTrue(modmap.stamp_fresh(home, "fork", directory, jars))
            jar.write_bytes(b"PK-rebuilt")
            self.assertFalse(modmap.stamp_fresh(home, "fork", directory, jars))

    def test_untracked_sources_invalidate_the_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping, home, jar, jars = self._stamped(tmp)
            (directory / "Extra.java").write_bytes(b"v1")
            self.assertFalse(modmap.stamp_fresh(home, "fork", directory, jars))

    def test_missing_stamp_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            self.assertFalse(
                modmap.stamp_fresh(Path(tmp) / "home", "fork", directory, {})
            )


class EnsureBuilt(unittest.TestCase):
    def test_pin_mod_never_runs_the_builder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)

            def fail(command, cwd):
                raise AssertionError(f"must not build: {command}")

            report = modmap.ensure_built(root, mapping, Path(tmp) / "home", runner=fail)
            self.assertEqual([row[1] for row in report], [modmap.PIN])

    def test_local_mod_assembles_and_second_run_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            commit(directory, "src/main/Fork.java", b"v2")
            (root / ".dev" / "coord-20261009").mkdir(parents=True)
            (root / ".dev" / "coord-20261009" / "gradle-slot").write_bytes(b"")
            home = Path(tmp) / "home"
            calls = []

            def runner(command, cwd):
                calls.append(([str(c) for c in command], cwd))
                libs = directory / "build" / "libs"
                libs.mkdir(parents=True, exist_ok=True)
                (libs / "fork-bf2.jar").write_bytes(b"PK-fresh")

            report = modmap.ensure_built(root, mapping, home, runner=runner)
            self.assertEqual(len(calls), 1)
            command = calls[0][0]
            self.assertIn("assemble", command)
            self.assertNotIn("build", [c for c in command if c != "just"])
            self.assertTrue(command[0].endswith("gradle-slot"))
            self.assertIn("built", report[0][2])
            calls.clear()
            report = modmap.ensure_built(root, mapping, home, runner=runner)
            self.assertEqual(calls, [])
            self.assertEqual(report[0][2], "fresh")

    def test_build_failure_and_missing_artifact_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            commit(directory, "src/main/Fork.java", b"v2")
            (root / ".dev" / "coord-20261009").mkdir(parents=True)
            (root / ".dev" / "coord-20261009" / "gradle-slot").write_bytes(b"")
            home = Path(tmp) / "home"

            def fail(command, cwd):
                raise RuntimeError("boom")

            with self.assertRaisesRegex(modmap.BuildError, "boom"):
                modmap.ensure_built(root, mapping, home, runner=fail)

            def noop(command, cwd):
                pass

            with self.assertRaisesRegex(modmap.BuildError, "did not produce"):
                modmap.ensure_built(root, mapping, home, runner=noop)

    def test_missing_slot_builds_without_the_shared_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, directory, mapping = fork_tree(tmp)
            commit(directory, "src/main/Fork.java", b"v2")
            calls = []

            def runner(command, cwd):
                calls.append([str(c) for c in command])
                libs = directory / "build" / "libs"
                libs.mkdir(parents=True, exist_ok=True)
                (libs / "fork-bf2.jar").write_bytes(b"PK-fresh")

            modmap.ensure_built(root, mapping, Path(tmp) / "home", runner=runner)
            self.assertEqual(len(calls), 1)
            self.assertIn("assemble", calls[0])
            self.assertNotIn("gradle-slot", calls[0][0])


if __name__ == "__main__":
    unittest.main()
