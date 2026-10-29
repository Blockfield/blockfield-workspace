"""The workspace overlay is testable without checking out private submodules."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "pack_build", Path(__file__).resolve().parents[1] / "pack_build.py"
)
pack_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack_build)


class PackOverlay(unittest.TestCase):
    def test_server_build_reads_nested_lobby_and_preserves_explicit_source(self):
        with tempfile.TemporaryDirectory(prefix="bf server source ") as tmp:
            root = Path(tmp).resolve()
            lobby = root / "server/lobby/blockfield/maps"
            lobby.mkdir(parents=True)
            config = lobby / "firing_range.json"
            config.write_text('{"id":"firing_range"}', encoding="utf-8")
            for source in (None, root / "custom source"):
                module = SimpleNamespace(build=Mock())
                arguments = [
                    "server",
                    "--root",
                    str(root),
                    "--out",
                    str(root / "output"),
                    "--world-seed",
                    str(root / "seed.zip"),
                ]
                if source is not None:
                    arguments.extend(["--server-source", str(source)])
                with (
                    patch.object(
                        pack_build, "load_build", return_value=(module, module)
                    ),
                    patch.object(pack_build, "local_jars", return_value={}),
                    patch.object(pack_build, "overlay"),
                ):
                    pack_build.main(arguments)
                args = module.build.call_args.args[0]
                self.assertEqual(args.server_source, source or root / "server")
                if source is None:
                    self.assertEqual(
                        (
                            args.server_source
                            / "lobby/blockfield/maps/firing_range.json"
                        ).read_bytes(),
                        config.read_bytes(),
                    )

    def test_loader_selects_each_standalone_entrypoint(self):
        root = Path("repo with spaces")
        library = SimpleNamespace()
        for side, filename in (
            ("client", "build-client.py"),
            ("server", "build-distribution.py"),
        ):
            module = SimpleNamespace()
            spec = SimpleNamespace(loader=Mock())
            with (
                patch.object(
                    pack_build.importlib.util,
                    "spec_from_file_location",
                    return_value=spec,
                ) as locate,
                patch.object(
                    pack_build.importlib.util, "module_from_spec", return_value=module
                ),
                patch.dict(pack_build.sys.modules, packlib=library),
            ):
                self.assertEqual(
                    pack_build.load_build(root, side),
                    (module, library if side == "client" else module),
                )
                locate.assert_called_once_with(
                    "workspace_pack", root / "scripts" / filename
                )
                spec.loader.exec_module.assert_called_once_with(module)

    def test_exact_metafiles_override_and_unmatched_pins_use_the_original_resolver(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="bf local jars ") as tmp:
            root = Path(tmp)
            jar = root / "local.jar"
            jar.write_bytes(b"PK-local")
            item = ("mods/parcool.pw.toml", {"filename": "ParCool.jar", "download": {}})
            library = SimpleNamespace(safe_path=lambda value: value)
            for key in (item[0], "parcool.pw.toml"):
                original = Mock(return_value=b"pinned result")
                module = SimpleNamespace(resolve_mod=original)
                pack_build.overlay(module, library, {key: jar})
                self.assertEqual(
                    module.resolve_mod(root, item, {}),
                    (*item, "mods/ParCool.jar", b"PK-local"),
                )
                original.assert_not_called()
                jar.write_bytes(b"broken")
                with self.assertRaisesRegex(ValueError, "Not a ZIP"):
                    module.resolve_mod(root, item, {})
                jar.write_bytes(b"PK-local")
                other = ("mods/another.pw.toml", item[1])
                self.assertEqual(module.resolve_mod(root, other, {}), b"pinned result")
                original.assert_called_once_with(root, other, {})
                original.side_effect = ValueError("checksum mismatch")
                with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                    module.resolve_mod(root, other, {})

    def test_server_uses_dev_marker_with_or_without_overrides(self):
        contract = "c" * 64
        for jars in ({}, {"parcool.pw.toml": Path("local.jar")}):
            original = Mock()
            module = SimpleNamespace(resolve_mod=Mock(), check_client=original)
            published = {
                "client": "d" * 64,
                "compatibility": "e" * 64,
                "client_version": "v2.59.3",
            }
            pack_build.overlay(module, module, jars)
            module.check_client({}, contract, published)
            original.assert_called_once_with({}, contract, published)
            self.assertEqual(
                published,
                {
                    "client": contract,
                    "compatibility": contract,
                    "client_version": "v2.59.3",
                },
            )


if __name__ == "__main__":
    unittest.main()
