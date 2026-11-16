import contextlib
import hashlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bump_mod_jar as bump  # noqa: E402
import mirror_mod_jar as mirror  # noqa: E402

MOD = "superbwarfare-fabric"
TAG = "bf27"
NAME = "superbwarfare-0.8.9.1-mc1.21.1-bf27.jar"
DATA = b"jar bf27"
SHA256 = hashlib.sha256(DATA).hexdigest()
SHA512 = hashlib.sha512(DATA).hexdigest()


def fake_download(repo, tag):
    assert (repo, tag) == (f"Blockfield/{MOD}", TAG), (repo, tag)
    return NAME, DATA, [("sha256", SHA256, "asset digest")]


def existing_for(size=len(DATA), digest=f"sha256:{SHA256}"):
    return {SHA512: (size, digest)}


class Mirror(unittest.TestCase):
    def test_missing_asset_is_uploaded_under_its_sha512(self):
        sent = []

        def send(file):
            sent.append(file)
            self.assertEqual(file.name, SHA512)
            self.assertEqual(file.read_bytes(), DATA)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(mirror.run(MOD, TAG, fake_download, {}, send), 0)
        self.assertEqual(len(sent), 1)

    def test_same_name_and_digest_is_skipped(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                mirror.run(MOD, TAG, fake_download, existing_for(), mock.Mock()), 0
            )

    def test_same_name_with_a_different_digest_is_an_error(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(mirror.MirrorError, "differs"):
                mirror.run(
                    MOD,
                    TAG,
                    fake_download,
                    existing_for(digest="sha256:0"),
                    mock.Mock(),
                )

    def test_same_name_with_a_different_size_is_an_error(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(mirror.MirrorError, "differs"):
                mirror.run(
                    MOD,
                    TAG,
                    fake_download,
                    {SHA512: (1, f"sha256:{SHA256}")},
                    mock.Mock(),
                )


class BumpRefusal(unittest.TestCase):
    def test_bump_refuses_pins_whose_jar_is_not_mirrored(self):
        jar = bump.Jar(TAG, NAME, SHA256, SHA512)
        with (
            mock.patch.object(mirror, "assets", return_value={}),
            self.assertRaisesRegex(bump.BumpError, f"just mirror-mod-jar {MOD} {TAG}"),
        ):
            mirror.require_mirrored(MOD, TAG, jar)
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch.object(mirror, "assets", return_value=existing_for()):
                mirror.require_mirrored(MOD, TAG, jar)

    def test_bump_main_writes_nothing_when_the_jar_is_not_mirrored(self):
        with (
            mock.patch.object(bump, "download", fake_download),
            mock.patch.object(mirror, "assets", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(mirror.main([MOD, TAG]), 1)
        self.assertIn("mirror-mod-jar", err.getvalue())


if __name__ == "__main__":
    unittest.main()
