"""Offline preparation never treats missing evidence or server UI libraries as removable."""

import argparse
import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
import zlib

HERE = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit = load("mod_audit")
profiles = load("async_profile")


def jar(meta, entries=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"version": "1.0", **meta}))
        for name, content in (entries or {}).items():
            archive.writestr(name, content)
    return stream.getvalue()


class Audit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "localmods").mkdir()
        (self.root / "localmods/mods.json.example").write_text("{}")
        for side in ("client", "server"):
            (self.root / side / "mods").mkdir(parents=True)
            (self.root / side / "pack.toml").write_text('version = "2.0"\n')
        self.cache = self.root / "cache"
        self.cache.mkdir()

    def pin(self, data, name="game", cache=True, side="server"):
        path = self.root / side / "mods" / f"{name}.pw.toml"
        checksum = hashlib.sha512(data).hexdigest()
        path.write_text(
            f'filename = "{name}.jar"\n[download]\nhash-format = "sha512"\nhash = "{checksum}"\n'
        )
        if cache:
            (self.cache / f"{name}.jar").write_bytes(data)

    def report(self):
        return audit.audit(self.root, [self.cache])

    def test_embedded_alias_dependency_keeps_server_client_only_library(self):
        library = jar(
            {
                "id": "playeranimator",
                "provides": ["legacy_animation"],
                "entrypoints": {"client": ["AnimationUI"]},
                "mixins": ["client.json"],
            },
            {"client.json": json.dumps({"client": ["PoseMixin"]})},
        )
        game = jar(
            {
                "id": "game",
                "depends": {"legacy_animation": ">=1", "missing_library": "*"},
                "jars": [{"file": "META-INF/jars/library.jar"}],
            },
            {"META-INF/jars/library.jar": library},
        )
        self.pin(game)
        report = self.report()
        rows = {row["id"]: row for row in report["mods"]}
        self.assertEqual(report["pins"][0]["status"], "verified")
        self.assertEqual(rows["playeranimator"]["hard_required_by"], ["game"])
        self.assertEqual(rows["playeranimator"]["assessment"], "required-by-metadata")
        self.assertEqual(rows["playeranimator"]["mixins"][0]["common"], 0)
        self.assertEqual(rows["playeranimator"]["runtime_benefit"], "not measured")
        self.assertEqual(
            rows["game"]["dependencies_without_verified_provider"], ["missing_library"]
        )

    def test_hash_mismatch_is_not_inspected_or_marked_removable(self):
        self.pin(jar({"id": "game"}))
        (self.cache / "game.jar").write_bytes(b"not even a ZIP")
        report = self.report()
        self.assertEqual(report["pins"][0]["status"], "hash-mismatch")
        self.assertEqual(report["mods"], [])

    def test_missing_cache_stays_unverified(self):
        self.pin(jar({"id": "game"}), cache=False)
        self.assertEqual(self.report()["pins"][0]["status"], "unverified")

    def test_incomplete_consumer_evidence_suppresses_candidates_only_on_its_side(self):
        library = jar({"id": "library"})
        consumer = jar({"id": "consumer", "depends": {"library": "*"}})
        self.pin(library, "library")
        self.pin(consumer, "consumer", cache=False)
        self.pin(jar({"id": "client_feature"}), "client_feature", side="client")
        for cached in (None, b"wrong cached artifact"):
            with self.subTest(cached=cached is not None):
                if cached is not None:
                    (self.cache / "consumer.jar").write_bytes(cached)
                report = self.report()
                self.assertEqual(
                    [
                        (row["side"], row["id"])
                        for row in report["runtime_ab_candidates"]
                    ],
                    [("client", "client_feature")],
                )
                self.assertEqual(
                    report["candidate_evidence_by_side"]["server"],
                    {"complete": False, "unverified_pins": ["consumer.pw.toml"]},
                )
                self.assertTrue(
                    report["candidate_evidence_by_side"]["client"]["complete"]
                )
                self.assertTrue(
                    any("suppressed" in limit for limit in report["limits"])
                )
        (self.cache / "consumer.jar").write_bytes(consumer)
        report = self.report()
        library_row = next(row for row in report["mods"] if row["id"] == "library")
        self.assertEqual(library_row["hard_required_by"], ["consumer"])
        self.assertEqual(library_row["assessment"], "required-by-metadata")
        self.assertTrue(report["candidate_evidence_by_side"]["server"]["complete"])

    def test_cache_change_after_hash_check_is_rejected_before_inspection(self):
        data = jar({"id": "game"})
        self.pin(data)
        (self.cache / "game.jar").write_bytes(b"cache replaced during audit")
        with patch.object(
            audit, "digest", return_value=hashlib.sha512(data).hexdigest()
        ):
            report = self.report()
        self.assertEqual(report["pins"][0]["status"], "hash-mismatch")
        self.assertEqual(report["mods"], [])

    def test_client_environment_alias_is_not_a_server_provider(self):
        self.pin(
            jar({"id": "client_only", "environment": "client", "provides": ["alias"]}),
            "client_only",
        )
        self.pin(jar({"id": "game", "depends": {"alias": "*"}}))
        game = next(row for row in self.report()["mods"] if row["id"] == "game")
        self.assertEqual(game["dependencies_without_verified_provider"], ["alias"])
        client = next(
            row for row in self.report()["mods"] if row["id"] == "client_only"
        )
        self.assertFalse(client["active_on_side"])

    def test_candidate_has_binary_reference_and_cannot_be_recommended_unused(self):
        self.pin(jar({"id": "porting_lib_attributes"}), "attributes")
        self.pin(
            jar(
                {"id": "game"},
                {
                    "Game.class": b"io/github/fabricators_of_create/porting_lib/attributes/SwimSpeed"
                },
            )
        )
        candidate = next(
            row
            for row in self.report()["mods"]
            if row["id"] == "porting_lib_attributes"
        )
        self.assertEqual(candidate["assessment"], "reference-found")
        self.assertEqual(candidate["binary_references"][0]["mod"], "game")

    def test_palette_counts_actual_indices_not_unused_palette(self):
        states = {
            "palette": [{"Name": "minecraft:air"}, {"Name": "create:track"}],
            "data": [0] * 256,
        }
        states["data"][0] = 1
        counts = audit.palette_counts(states)
        self.assertEqual(counts, {"minecraft:air": 4095, "create:track": 1})
        self.assertEqual(
            audit.palette_counts({"palette": [{"Name": "yuushya:block"}]}),
            {"yuushya:block": 4096},
        )

    def test_world_seed_hash_mismatch_fails_before_parsing(self):
        path = self.root / "world.zip"
        path.write_bytes(b"not the pinned map")
        with self.assertRaisesRegex(ValueError, "does not match"):
            audit.world_usage(path, {"sha256": "0" * 64})

    def test_pinned_zip_counts_non_vanilla_placed_blocks(self):
        payload = (
            b"\x0a\x00\x00\x09\x00\x08sections\x0a\x00\x00\x00\x01"
            b"\x0a\x00\x0cblock_states\x09\x00\x07palette\x0a\x00\x00\x00\x01"
            b"\x08\x00\x04Name\x00\x0ccreate:track\x00\x00\x00\x00"
        )
        compressed = zlib.compress(payload)
        region = bytearray(8192)
        region[:4] = b"\x00\x00\x02\x01"
        region.extend((len(compressed) + 1).to_bytes(4, "big") + b"\x02" + compressed)
        path = self.root / "world.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("maps/test/region/r.0.0.mca", region)
        report = audit.world_usage(
            path, {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        )
        self.assertEqual(report["chunks"], 1)
        self.assertEqual(report["placed_blocks_by_namespace"], {"create": 4096})

    def test_entities_nbt_counts_display_content_without_placed_blocks(self):
        names = (b"yuushya:lamp", b"yuushya:" + b"a" * 40, b"0:" + b"a" * 46)
        self.assertEqual(len(names[1]), 0x30)
        payload = b"\x0a\x00\x00" + b"".join(
            b"\x08"
            + len(key).to_bytes(2, "big")
            + key
            + len(value).to_bytes(2, "big")
            + value
            for key, value in zip((b"Name", b"long", b"numeric"), names)
        )
        # Raw numeric/byte arrays are not namespaced string references.
        binary = b"phantom:not_a_string"
        payload += (
            b"\x07\x00\x06binary" + len(binary).to_bytes(4, "big") + binary + b"\x00"
        )
        for compression, compressed in (
            (1, gzip.compress(payload)),
            (2, zlib.compress(payload)),
            (3, payload),
        ):
            with self.subTest(compression=compression):
                region = bytearray(8192)
                region[:4] = b"\x00\x00\x02\x01"
                region.extend(
                    (len(compressed) + 1).to_bytes(4, "big")
                    + bytes([compression])
                    + compressed
                )
                path = self.root / "world.zip"
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("maps/test/entities/r.0.0.mca", region)
                report = audit.world_usage(
                    path, {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                )
                self.assertEqual(report["placed_blocks_by_namespace"], {})
                self.assertEqual(
                    report["nbt_references_by_namespace"], {"0": 1, "yuushya": 2}
                )

    def test_compat_namespace_is_owned_by_its_mod_and_config_is_a_consumer(self):
        self.pin(jar({"id": "create"}, {"data/create/recipe/a.json": "{}"}), "create")
        self.pin(
            jar({"id": "addon"}, {"data/create/recipe/b.json": "{}"}),
            "addon",
        )
        self.pin(jar({"id": "tweaks"}, {"assets/tweaks/icon.png": b""}), "tweaks")
        config = self.root / "server/server/lobby/config"
        config.mkdir(parents=True)
        (config / "tweaks.json").write_text("{}")
        report = self.report()
        rows = {row["id"]: row for row in report["mods"]}
        self.assertEqual(rows["addon"]["namespaces"], [])
        self.assertEqual(rows["create"]["namespaces"], ["create"])
        self.assertEqual(rows["tweaks"]["assessment"], "consumer-found")
        self.assertEqual(
            {row["id"] for row in report["runtime_ab_candidates"]}, {"addon", "create"}
        )


def case(variant, p95, shots=1000, online=8):
    return {
        "label": variant + str(p95),
        "variant": variant,
        "load": ["combat", "--guns"],
        "metrics": {
            "window_p50_median_ms": 4.0,
            "window_p95_median_ms": p95,
            "window_p99_median_ms": p95 + 2,
            "window_p99_max_ms": p95 + 5,
            "cpu_percent_one_core": 40.0,
            "gc_stw_ms": 90.0,
            "shots_success": shots,
            "online": online,
        },
    }


ASYNC_LOG = """Initializing Async Config...
Configuration saved.
Configuration loaded.
Initialized Pool with 2 threads
"""


class AsyncProfile(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads(profiles.PROFILE.read_text())
        self.prepared = profiles.prepare(self.profile)

    def own_case(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        server = home / "server"
        (server / "config").mkdir(parents=True)
        (server / "config/async.toml").write_text(self.prepared["config_toml"])
        log = server / "server.out"
        log.write_text(ASYNC_LOG)
        record = {"pid": 1234, "live": True, "owner": "own-session"}
        stack = SimpleNamespace(
            HOME=home,
            SERVER=server,
            LOG=log,
            PID_FILE=home / "server.pid",
            RCON_PORT=25576,
            RCON_PASSWORD="leaks",
            process_record=Mock(return_value=record),
            process_owner=Mock(return_value="own-session"),
            listener_owned=Mock(return_value=True),
        )
        samples = [
            {"wall_s": 0, "cpu_s": 1, "gc_stw_ms": 1, "gc_count": 1},
            {"wall_s": 100, "cpu_s": 20, "gc_stw_ms": 2, "gc_count": 2},
        ]
        output = json.dumps(
            {
                "scenario": "combat",
                "windows": [[5, 4, 8, 10]] * 6,
                "kills": 0,
                "online": 8,
            }
        )
        return stack, samples, output

    def test_case_uses_owned_rcon_port_for_effective_gate_and_stress_subprocess(self):
        stack, samples, output = self.own_case()
        rcon = Mock()
        rcon.cmd.side_effect = ["stats", "exclusions"]
        rcon.s.getsockname.return_value = ("127.0.0.1", 40123)
        with (
            patch.object(profiles, "stand", return_value=stack),
            patch.object(profiles, "effective", return_value={"variant": "B"}) as gate,
            patch.object(profiles, "sample", side_effect=samples),
            patch("scenarios.common.connect_rcon", return_value=rcon) as connect,
            patch.object(
                profiles.subprocess, "run", return_value=SimpleNamespace(stdout=output)
            ) as run,
        ):
            profiles.run_case(
                self.prepared, "B", "B1", stack.HOME / "out", ["combat", "--dm", "8"]
            )
        connect.assert_called_once_with(host="127.0.0.1", port=25576, password="leaks")
        stack.listener_owned.assert_any_call(1234, 25576)
        stack.listener_owned.assert_any_call(1234, 25576, peer_port=40123)
        rcon.s.close.assert_called_once()
        self.assertEqual(gate.call_args.args[-2:], ("stats", "exclusions"))
        command = run.call_args.args[0]
        from scenarios import stress

        parser = argparse.ArgumentParser()
        stress.register(parser.add_subparsers())
        args = parser.parse_args(command[3:])
        self.assertEqual(
            (args.rcon_host, args.rcon_port, args.rcon_password),
            ("127.0.0.1", 25576, "leaks"),
        )
        self.assertEqual(run.call_args.kwargs["env"]["BF_RCON_PORT"], "25576")
        self.assertEqual(Path(run.call_args.kwargs["env"]["BF_DEV_HOME"]), stack.HOME)

    def test_foreign_listener_fails_before_any_rcon_connection_or_load(self):
        for variant in "AB":
            with self.subTest(variant=variant):
                stack, _, _ = self.own_case()
                stack.listener_owned.return_value = False
                with (
                    patch.object(profiles, "stand", return_value=stack),
                    patch.object(profiles, "sample") as sample,
                    patch("scenarios.common.connect_rcon") as connect,
                    patch.object(profiles.subprocess, "run") as run,
                ):
                    with self.assertRaisesRegex(ValueError, "RCON listener"):
                        profiles.run_case(
                            self.prepared,
                            variant,
                            variant + "1",
                            stack.HOME / "out",
                            ["combat"],
                        )
                connect.assert_not_called()
                sample.assert_not_called()
                run.assert_not_called()
                self.assertFalse((stack.HOME / "out").exists())

    def test_rcon_connection_must_still_belong_to_owned_process_before_commands(self):
        stack, _, _ = self.own_case()
        stack.listener_owned.side_effect = [True, False]
        rcon = Mock()
        rcon.s.getsockname.return_value = ("127.0.0.1", 40123)
        with (
            patch.object(profiles, "stand", return_value=stack),
            patch("scenarios.common.connect_rcon", return_value=rcon),
            patch.object(profiles.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(ValueError, "RCON connection"):
                profiles.run_case(
                    self.prepared, "B", "B1", stack.HOME / "out", ["combat"]
                )
        rcon.cmd.assert_not_called()
        rcon.s.close.assert_called_once()
        run.assert_not_called()

    def test_stress_endpoint_overrides_and_argparse_abbreviations_are_rejected(self):
        for option in (
            "--rcon-host",
            "--rcon-port=25575",
            "--rcon-password",
            "--rcon-h",
            "--rcon-po=25575",
            "--rcon-pass",
        ):
            with self.subTest(option=option), patch.object(profiles, "stand") as stand:
                with self.assertRaisesRegex(ValueError, "RCON endpoint"):
                    profiles.run_case(
                        self.prepared,
                        "A",
                        "A1",
                        Path("unused"),
                        ["combat", option, "25575"],
                    )
                stand.assert_not_called()

    def test_native_toml_is_deterministic_and_restricted(self):
        expected = profiles.prepare(self.profile)
        shuffled = copy.deepcopy(self.profile)
        shuffled["config"] = dict(reversed(list(shuffled["config"].items())))
        shuffled["config"]["synchronizedEntities"].reverse()
        self.assertEqual(
            profiles.prepare(shuffled)["config_toml"], expected["config_toml"]
        )
        native = tomllib.loads(expected["config_toml"])
        self.assertFalse(native["enableAsyncSpawn"])
        self.assertFalse(native["enableAsyncRandomTicks"])
        self.assertEqual(native["maxThreads"], 2)
        self.assertTrue(profiles.PROTECTED <= set(native["synchronizedEntities"]))
        self.assertIsNone(expected["registry_resolved_exclusions"])
        self.assertFalse(expected["artifact_verified"])

    def test_scope_and_guard_changes_are_rejected(self):
        for target, key, value in (
            (self.profile, "scope", "production"),
            (self.profile["candidate"], "sha256", "0" * 64),
            (self.profile["candidate"], "source_commit", "main"),
            (self.profile["config"], "enableAsyncSpawn", True),
            (self.profile["config"], "maxThreads", True),
            (self.profile["config"], "synchronizedEntities", ["superbwarfare:drone"]),
        ):
            with self.subTest(key=key):
                previous = target[key]
                target[key] = value
                try:
                    with self.assertRaises(ValueError):
                        profiles.prepare(self.profile)
                finally:
                    target[key] = previous

    def test_complete_registry_resolves_only_existing_types_and_retains_wildcards(self):
        registry = {
            "complete": True,
            "minecraft": "1.21.1",
            "entity_types": [
                *profiles.VANILLA,
                "superbwarfare:drone",
                "superbwarfare:projectile",
                "minecraft:bat",
            ],
        }
        result = profiles.prepare(self.profile, registry)
        self.assertEqual(
            set(result["registry_resolved_exclusions"]),
            profiles.VANILLA | {"superbwarfare:drone", "superbwarfare:projectile"},
        )
        self.assertIn(
            "superbwarfare:*",
            tomllib.loads(result["config_toml"])["synchronizedEntities"],
        )

    def test_entity_census_cannot_masquerade_as_registry(self):
        for registry in (
            {"minecraft": "1.21.1", "entity_counts": {"tacz:bullet": 1}},
            {"complete": True, "minecraft": "1.21.1", "entity_types": ["tacz:bullet"]},
        ):
            with self.assertRaises(ValueError):
                profiles.prepare(self.profile, registry)

    def test_candidate_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "async.jar"
            path.write_bytes(b"not the reviewed artifact")
            with self.assertRaisesRegex(ValueError, "JAR hash"):
                profiles.prepare(self.profile, jar=path)

    def runtime(self, variant="B", log=ASYNC_LOG, jar_bytes=None, synced=None):
        prepared = self.prepared
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        mods = Path(temporary.name)
        if jar_bytes is not None:
            (mods / prepared["profile"]["candidate"]["filename"]).write_bytes(jar_bytes)
        entries = tomllib.loads(prepared["config_toml"])["synchronizedEntities"]
        listing = synced or "\u00a77Synchronized Entities: \n" + "".join(
            f"- {entry}\n" for entry in reversed(entries)
        )
        stats = "\u00a77Status: Enabled\nAsync Spawn: Disabled\nAsync Random Ticks: Disabled"
        # Async rewrites the TOML from a HashSet on load: order is not stable.
        rewritten = prepared["config_toml"].replace(
            ", ".join(f'"{e}"' for e in entries),
            ", ".join(f'"{e}"' for e in reversed(entries)),
        )
        return profiles.effective(
            prepared, variant, mods, rewritten, log, stats, listing
        )

    def test_effective_gate_needs_loaded_config_and_exact_exclusions(self):
        with patch.object(
            profiles, "CANDIDATE_HASH", hashlib.sha256(b"jar").hexdigest()
        ):
            self.assertEqual(self.runtime(jar_bytes=b"jar")["variant"], "B")
            for log in (
                ASYNC_LOG.replace(
                    "Configuration loaded.",
                    "Configuration not found. Creating defaults...",
                ),
                ASYNC_LOG.replace("2 threads", "16 threads"),
            ):
                with self.assertRaises(ValueError):
                    self.runtime(log=log, jar_bytes=b"jar")
            with self.assertRaisesRegex(ValueError, "synchronizedEntities"):
                self.runtime(jar_bytes=b"jar", synced="- minecraft:tnt\n")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.runtime(jar_bytes=b"other")
        with self.assertRaisesRegex(ValueError, "without Async"):
            self.runtime("A", log="", jar_bytes=b"jar")
        self.assertEqual(self.runtime("A", log="")["async_jar"], [])

    def test_compare_requires_equal_load_and_clear_separation(self):
        baseline = [case("A", p95) for p95 in (8.0, 8.5, 9.0)]
        noisy = profiles.compare(baseline + [case("B", p95) for p95 in (7.9, 8.6, 9.4)])
        self.assertEqual(noisy["verdict"], "no-demonstrated-gain")
        self.assertEqual(
            noisy["metrics"]["window_p95_median_ms"]["outcome"], "within-spread"
        )
        clear = profiles.compare(baseline + [case("B", p95) for p95 in (6.0, 6.5, 7.0)])
        self.assertEqual(clear["verdict"], "performance-gain-observed")
        with self.assertRaisesRegex(ValueError, "throughput"):
            profiles.compare(baseline + [case("B", 6.0, shots=800)] * 3)
        with self.assertRaisesRegex(ValueError, "three"):
            profiles.compare(baseline + [case("B", 6.0)])

    def test_gun_compare_requires_native_shots_and_online_evidence(self):
        for name in ("shots_success", "online"):
            for value in (None, 0, -1, True, "1000", 1000.0):
                with self.subTest(name=name, value=value):
                    cases = [case(variant, 8.0) for variant in "AAABBB"]
                    for row in cases:
                        row["metrics"][name] = value
                    with self.assertRaisesRegex(ValueError, name):
                        profiles.compare(cases)

    def test_non_gun_compare_does_not_require_shot_evidence(self):
        cases = [case(variant, 8.0, shots=None, online=None) for variant in "AAABBB"]
        for row in cases:
            row["load"] = ["combat"]
        self.assertEqual(profiles.compare(cases)["verdict"], "no-demonstrated-gain")

    def test_case_metrics_use_window_medians_and_process_deltas(self):
        windows = [[5, 4, 8, 10]] * 5 + [[60, 40, 80, 900]]
        before = {"wall_s": 0, "cpu_s": 10, "gc_stw_ms": 100, "gc_count": 3}
        after = {"wall_s": 100, "cpu_s": 50, "gc_stw_ms": 190, "gc_count": 9}
        row = {
            "windows": windows,
            "gunner": "SUCCESS=1200 COOL_DOWN=3",
            "kills": 4,
            "online": 8,
        }
        metrics = profiles.case_metrics(row, before, after)
        self.assertEqual(metrics["window_p99_median_ms"], 10)
        self.assertEqual(metrics["window_p99_max_ms"], 900)
        self.assertEqual(metrics["cpu_percent_one_core"], 40)
        self.assertEqual((metrics["gc_stw_ms"], metrics["shots_success"]), (90, 1200))
        with self.assertRaisesRegex(ValueError, "six"):
            profiles.case_metrics({**row, "windows": windows[:5]}, before, after)


if __name__ == "__main__":
    unittest.main()
