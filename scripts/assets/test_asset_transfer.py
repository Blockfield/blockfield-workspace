import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
WORKSPACE_ROOT = HERE.parents[1]
FETCH = runpy.run_path(str(HERE / "fetch-world-seed.py"))["fetch"]
_PUBLISHER = runpy.run_path(str(HERE / "publish-assets.py"))
PUBLISH = _PUBLISHER["publish"]

sys.path.insert(0, str(WORKSPACE_ROOT / "scripts"))
from toolchain import tool  # noqa: E402


class AssetTransferTests(unittest.TestCase):
    def test_seed_is_verified_before_replacing_a_cached_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "distribution").mkdir()
            (root / "sources").mkdir()
            target = root / "sources/world-seed.zip"
            target.write_bytes(b"old")
            url = "s3://blockfield-assets/world-seed/world-seed-v10/world-seed.zip"
            (root / "distribution/assets.lock.json").write_text(
                json.dumps(
                    {
                        "url": "https://github.com/unused/provenance",
                        "r2-url": url,
                        "sha256": hashlib.sha256(b"new").hexdigest(),
                    }
                )
            )
            reader = Mock(return_value=b"bad")
            with patch.dict(
                FETCH.__globals__,
                runpy=Mock(run_path=Mock(return_value={"get_bytes": reader})),
            ):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    FETCH(root)
                self.assertEqual(target.read_bytes(), b"old")
                reader.return_value = b"new"
                self.assertEqual(FETCH(root), target)
                self.assertEqual(target.read_bytes(), b"new")
                FETCH(root)
                self.assertEqual(reader.call_count, 2)
                reader.assert_called_with(url)

    def test_publication_requires_immutable_upload_and_download_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "map.zip").write_bytes(b"resource")
            runner = Mock()
            with patch.dict(
                PUBLISH.__globals__,
                tool=lambda _: "rclone",
                subprocess=Mock(run=runner),
            ):
                self.assertEqual(
                    PUBLISH(root, "maps/fields/map-fields-test"),
                    "s3://blockfield-assets/maps/fields/map-fields-test",
                )
                upload, verify = [call.args[0] for call in runner.call_args_list]
                self.assertIn("--immutable", upload)
                self.assertIn("--download", verify)
                self.assertIn(
                    "r2_upload:blockfield-assets/maps/fields/map-fields-test", upload
                )
                self.assertIn(
                    "r2_read:blockfield-assets/maps/fields/map-fields-test", verify
                )
                runner.reset_mock()
                for prefix in (
                    "dokploy-backups/v1",
                    "music/../secrets",
                    "music/v1/extra/level",
                ):
                    with self.assertRaises(ValueError):
                        PUBLISH(root, prefix)
                (root / "map.zip").write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    PUBLISH(root, "maps/fields/map-fields-test")
                runner.assert_not_called()

    def test_dry_run_validates_without_writing_sums_or_calling_rclone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "world-seed.zip").write_bytes(b"seed")
            runner = Mock()
            with patch.dict(
                PUBLISH.__globals__,
                tool=Mock(side_effect=AssertionError("rclone should not run")),
                subprocess=Mock(run=runner),
            ):
                self.assertEqual(
                    PUBLISH(root, "world-seed/world-seed-v16", dry_run=True),
                    "s3://blockfield-assets/world-seed/world-seed-v16",
                )
                self.assertFalse((root / "SHA256SUMS").exists())
                runner.assert_not_called()

                (root / "SHA256SUMS").write_text(
                    f"{'0' * 64}  world-seed.zip\n", encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "checksum"):
                    PUBLISH(root, "world-seed/world-seed-v16", dry_run=True)

    def test_just_assets_dry_run_works_without_server_checkout(self):
        just = str(tool("just"))
        cache = WORKSPACE_ROOT / ".cache" / "quality"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            for relative in (
                "justfile",
                "scripts/toolchain.py",
                "scripts/toolchain_local.py",
                "scripts/tools.lock.json",
                "scripts/terminal.py",
                "scripts/dev_cache.py",
                "scripts/assets/publish-assets.py",
            ):
                dst = root / relative
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes((WORKSPACE_ROOT / relative).read_bytes())
            release_dir = Path(directory) / "release"
            release_dir.mkdir()
            (release_dir / "world-seed.zip").write_bytes(b"seed")
            env = {
                **os.environ,
                "BF_TOOL_CACHE": str(cache),
            }
            result = subprocess.run(
                [
                    just,
                    "assets",
                    "--dry-run",
                    "world-seed/world-seed-v16",
                    str(release_dir),
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                result.stdout.strip(),
                "s3://blockfield-assets/world-seed/world-seed-v16",
            )
            self.assertFalse((release_dir / "SHA256SUMS").exists())


class StrictSumsTests(unittest.TestCase):
    """server#213-P2: a hand-touched SHA256SUMS must fail at publish, not at
    deploy-time consumers that split on exactly two spaces."""

    PARSE = runpy.run_path(str(HERE / "publish-assets.py"))["parse_sums"]

    def test_canonical_two_space_form_parses(self):
        digest = hashlib.sha256(b"a").hexdigest()
        self.assertEqual(StrictSumsTests.PARSE(f"{digest}  a.zip\n"), {"a.zip": digest})

    def test_single_space_separator_is_refused(self):
        digest = hashlib.sha256(b"a").hexdigest()
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            StrictSumsTests.PARSE(f"{digest} a.zip\n")

    def test_binary_marker_is_refused(self):
        digest = hashlib.sha256(b"a").hexdigest()
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            StrictSumsTests.PARSE(f"{digest} *a.zip\n")

    def test_bad_hex_and_duplicates_are_refused(self):
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            StrictSumsTests.PARSE("x" * 64 + "  a.zip\n")
        digest = hashlib.sha256(b"a").hexdigest()
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            StrictSumsTests.PARSE(f"{digest}  a.zip\n{digest}  a.zip\n")


class ArtifactNameTests(unittest.TestCase):
    def test_whitespace_in_an_artifact_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foo  bar.zip").write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "whitespace"):
                _PUBLISHER["write_sums"](Path(tmp))
            self.assertFalse((Path(tmp) / "SHA256SUMS").exists())
