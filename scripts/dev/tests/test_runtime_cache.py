"""Runtime reuse validates the original pack and a narrowly scoped mod receipt."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "runtime_cache_devstack", Path(__file__).parents[1] / "devstack.py"
)
devstack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(devstack)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ServerRuntimeCache(unittest.TestCase):
    def setUp(self):
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.root = Path(
            self.context.enter_context(tempfile.TemporaryDirectory())
        ).resolve()
        self.home = self.root / "stack with spaces"
        self.server = self.home / "server"
        (self.server / "mods").mkdir(parents=True)
        (self.server / "config").mkdir()
        (self.server / "fabric-server-launch.jar").write_bytes(b"launcher")
        self.mod = self.server / "mods/blockfield-server-base.jar"
        self.other = self.server / "mods/other.jar"
        self.mod.write_bytes(b"original Blockfield mod")
        self.other.write_bytes(b"original external mod")
        self.manifest = self.server / "build.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "compatibility": "original common pair",
                    "server": "original server pack digest",
                    "mods": [
                        {"path": "mods/" + jar.name, "sha256": sha(jar)}
                        for jar in (self.mod, self.other)
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.original_manifest = self.manifest.read_bytes()
        self.receipt = self.home / "watch-receipt.json"
        self.context.enter_context(patch.object(devstack, "HOME", self.home))
        self.context.enter_context(patch.object(devstack, "SERVER", self.server))

    def save_receipt(self, jar=None):
        jar = self.mod if jar is None else jar
        saved = {
            "schema": 1,
            "sides": {
                "server": {
                    "source": "a" * 64,
                    "unsupported": "b" * 64,
                    "jar": {"path": str(jar.resolve()), "sha256": sha(jar)},
                    "identity": {
                        "process": [123, "birth", 456],
                        "runtime_manifest_sha256": sha(self.manifest),
                    },
                }
            },
        }
        self.receipt.write_text(json.dumps(saved), encoding="utf-8")
        return saved

    def test_original_runtime_needs_no_iteration_receipt(self):
        self.assertTrue(devstack.server_runtime_ready())

    def test_changed_blockfield_requires_a_receipt(self):
        self.mod.write_bytes(b"iteration")
        self.assertFalse(devstack.server_runtime_ready())

    def test_exact_blockfield_receipt_reuses_the_unchanged_base_manifest(self):
        self.mod.write_bytes(b"iteration")
        self.save_receipt()
        self.assertTrue(devstack.server_runtime_ready())
        self.assertEqual(self.manifest.read_bytes(), self.original_manifest)

    def test_receipt_does_not_allow_other_mod_changes(self):
        self.mod.write_bytes(b"iteration")
        self.save_receipt()
        self.other.write_bytes(b"changed external mod")
        self.assertFalse(devstack.server_runtime_ready())
        self.save_receipt(self.other)
        self.assertFalse(devstack.server_runtime_ready())

    def test_receipt_is_bound_to_the_complete_base_manifest(self):
        self.mod.write_bytes(b"iteration")
        self.save_receipt()
        manifest = json.loads(self.manifest.read_bytes())
        manifest["compatibility"] = "different common pair"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertFalse(devstack.server_runtime_ready())

    def test_receipt_is_bound_to_the_installed_jar_bytes(self):
        self.mod.write_bytes(b"iteration")
        self.save_receipt()
        self.mod.write_bytes(b"later unrecorded iteration")
        self.assertFalse(devstack.server_runtime_ready())

    def test_equal_jar_bytes_at_another_path_are_not_an_override(self):
        self.mod.write_bytes(b"iteration")
        for location in (self.root, self.server / "mods"):
            with self.subTest(location=location):
                other_copy = location / "blockfield-server-other.jar"
                other_copy.write_bytes(self.mod.read_bytes())
                self.save_receipt(other_copy)
                self.assertFalse(devstack.server_runtime_ready())

    def test_receipt_does_not_replace_a_missing_manifest_jar(self):
        replacement = self.mod.with_name("blockfield-server-re-named.jar")
        self.mod.rename(replacement)
        self.save_receipt(replacement)
        self.assertFalse(devstack.server_runtime_ready())

    def test_malformed_or_unbound_receipts_fail_closed(self):
        self.mod.write_bytes(b"iteration")
        for mutation in (
            lambda saved: saved.update(schema=2),
            lambda saved: saved["sides"].update(client=saved["sides"].pop("server")),
            lambda saved: saved["sides"]["server"].pop("identity"),
            lambda saved: saved["sides"]["server"].update(identity=None),
            lambda saved: saved["sides"]["server"].update(jar=[]),
        ):
            with self.subTest(mutation=mutation):
                saved = self.save_receipt()
                mutation(saved)
                self.receipt.write_text(json.dumps(saved), encoding="utf-8")
                self.assertFalse(devstack.server_runtime_ready())
        self.receipt.write_text("{unfinished", encoding="utf-8")
        self.assertFalse(devstack.server_runtime_ready())

    def test_blockfield_path_cannot_alias_another_mod(self):
        self.mod.unlink()
        try:
            self.mod.symlink_to(self.other)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        self.save_receipt()
        self.assertFalse(devstack.server_runtime_ready())

    def test_stack_home_symlink_does_not_invalidate_an_exact_mod_receipt(self):
        alias = self.root / "stack alias"
        try:
            alias.symlink_to(self.home, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        self.mod.write_bytes(b"iteration")
        self.save_receipt()
        with (
            patch.object(devstack, "HOME", alias),
            patch.object(devstack, "SERVER", alias / "server"),
        ):
            self.assertTrue(devstack.server_runtime_ready())

    def test_manifest_paths_keep_the_original_restrictions(self):
        self.mod.write_bytes(b"iteration")
        for path in (
            str(self.mod.resolve()),
            "C:\\mods\\blockfield-server-base.jar",
            "mods/../mods/blockfield-server-base.jar",
            "config/blockfield-server-base.jar",
        ):
            with self.subTest(path=path):
                manifest = json.loads(self.original_manifest)
                manifest["mods"][0]["path"] = path
                self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
                self.save_receipt()
                self.assertFalse(devstack.server_runtime_ready())

    def test_disabled_proxy_mods_still_need_their_original_checksum(self):
        proxy = self.server / "mods-off/FabricProxy-Lite.jar"
        proxy.parent.mkdir()
        proxy.write_bytes(b"proxy mod")
        manifest = json.loads(self.original_manifest)
        manifest["mods"].append({"path": "mods/" + proxy.name, "sha256": sha(proxy)})
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.mod.write_bytes(b"iteration")
        self.save_receipt()
        self.assertTrue(devstack.server_runtime_ready())
        proxy.write_bytes(b"changed proxy mod")
        self.assertFalse(devstack.server_runtime_ready())


if __name__ == "__main__":
    unittest.main()
