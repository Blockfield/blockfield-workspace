import contextlib
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bump_mod_jar as bump  # noqa: E402

MOD = "lr-tactical"
URL = "https://github.com/Blockfield/lr-tactical/releases/download"


def jar(tag):
    data = f"jar {tag}".encode()
    return bump.Jar(
        tag,
        f"lrtactical-1.21.1-0.4.1-{tag}.jar",
        hashlib.sha256(data).hexdigest(),
        hashlib.sha512(data).hexdigest(),
    )


JARS = {j.tag: j for j in (jar("bf5"), jar("bf6"), jar("bf7"))}


def fake_fetch(repo, tag):
    assert repo == "Blockfield/lr-tactical", repo
    return JARS[tag]


def holder(path, j):
    """Fixture text in the shape of the real holder at path, pinning j."""
    if path.endswith("pw.toml"):
        return f'filename = "{j.name}"\n[download]\nurl = "{URL}/{j.tag}/{j.name}"\nhash = "{j.sha512}"\n'
    if path.endswith("oracle-mod-pins.gradle"):
        return f"'{j.name}': [url: '{URL}/{j.tag}/{j.name}', algorithm: 'sha512', hash: '{j.sha512}'],\n"
    if path.endswith("build.gradle"):
        return f"lrTactical: [url: '{URL}/{j.tag}/{j.name}',\r\n  sha256: '{j.sha256}'],\r\n"
    return f'    "{j.sha256}",\n'  # frozen test constant / AnimationManifest.PINS


class Bump(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.trees = {side: Path(tmp.name, side) for side in bump.TREES}
        for side, paths in bump.MODS[MOD][1].items():
            for path in paths:
                self.write(side, path, holder(path, JARS["bf6"]))
        self.write(
            "mod", "docs/history.md", "bf1 shipped as lrtactical-1.21.1-0.4.1-bf1.jar\n"
        )
        self.track()

    def write(self, side, path, text):
        file = self.trees[side] / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(text.encode())

    def track(self):
        for tree in self.trees.values():
            subprocess.run(["git", "init", "-q", tree], check=True)
            subprocess.run(["git", "-C", tree, "add", "-A"], check=True)

    def test_old_pin_in_every_holder_becomes_the_new_one(self):
        edits = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        holders = {(s, p) for s, paths in bump.MODS[MOD][1].items() for p in paths}
        self.assertEqual(set(edits), holders)
        for (side, path), text in edits.items():
            self.assertEqual(text, holder(path, JARS["bf7"]), path)
        self.assertEqual(bump.plan(MOD, "bf6", self.trees, fake_fetch), {})

    def test_holders_lagging_on_different_releases_all_move(self):
        path = bump.MODS[MOD][1]["client"][0]
        self.write("client", path, holder(path, JARS["bf7"]))
        edits = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        self.assertNotIn(("client", path), edits)
        self.assertEqual(len(edits), 7)

    def test_bare_name_of_an_older_release_is_left_alone(self):
        path = bump.MODS[MOD][1]["mod"][1]
        guard = f'assertFalse(snapshot.contains("{jar("bf5").name}"));\n'
        self.write("mod", path, holder(path, JARS["bf6"]) + guard)
        edits = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        self.assertEqual(edits["mod", path], holder(path, JARS["bf7"]) + guard)

    def test_missing_holder_is_an_error(self):
        path = bump.MODS[MOD][1]["gameplay"][2]
        (self.trees["gameplay"] / path).unlink()
        with self.assertRaisesRegex(bump.BumpError, "holder missing"):
            bump.plan(MOD, "bf7", self.trees, fake_fetch)

    def test_holder_that_pins_nothing_known_is_an_error(self):
        path = bump.MODS[MOD][1]["mod"][0]
        self.write("mod", path, '    "' + "0" * 64 + '",\n')
        with self.assertRaisesRegex(bump.BumpError, f"mod {path}: pins none"):
            bump.plan(MOD, "bf7", self.trees, fake_fetch)

    def test_unknown_file_with_the_old_hash_is_an_error(self):
        self.write("gameplay", "src/My Other.java", f'"{JARS["bf6"].sha256}"\n')
        self.track()
        with self.assertRaisesRegex(
            bump.BumpError, "src/My Other.java: holds an old hash"
        ):
            bump.plan(MOD, "bf7", self.trees, fake_fetch)

    def test_archive_fallback_naming_the_old_jar_is_an_error(self):
        path = bump.MODS[MOD][1]["gameplay"][0]
        text = (
            holder(path, JARS["bf6"]) + f"archiveEntry: 'mods/{JARS['bf6'].name}'],\n"
        )
        self.write("gameplay", path, text)
        with self.assertRaisesRegex(bump.BumpError, "client archive ships"):
            bump.plan(MOD, "bf7", self.trees, fake_fetch)

    def test_main_writes_holders_and_check_lists_them(self):
        patch = mock.patch.object(bump, "fetch", fake_fetch)
        patch.start()
        self.addCleanup(patch.stop)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        flags = [
            arg for side, tree in self.trees.items() for arg in (f"--{side}", str(tree))
        ]
        self.assertEqual(bump.main([MOD, "bf7", "--dry-run", *flags]), 0)
        path = bump.MODS[MOD][1]["gameplay"][0]
        self.assertIn(JARS["bf6"].sha256, (self.trees["gameplay"] / path).read_text())
        self.assertEqual(bump.main([MOD, "bf7", *flags]), 0)
        self.assertEqual(
            (self.trees["gameplay"] / path).read_bytes().decode(),
            holder(path, JARS["bf7"]),
        )
        self.assertEqual(bump.main([MOD, "--check", *flags]), 0)


class Verify(unittest.TestCase):
    def test_sums_file_lines_for_the_jar(self):
        text = "aa  other.jar\nbb  x.jar\ncc *x.jar\n"
        self.assertEqual(bump.sums_for(text, "x.jar"), ["bb", "cc"])

    def test_every_listed_hash_must_match(self):
        data = b"jar"
        good = hashlib.sha256(data).hexdigest()
        j = bump.verified("bf1", "x.jar", data, [("sha256", good, "digest")])
        self.assertEqual(j.sha512, hashlib.sha512(data).hexdigest())
        with self.assertRaisesRegex(bump.BumpError, "SHA512SUMS"):
            bump.verified(
                "bf1",
                "x.jar",
                data,
                [("sha256", good, "d"), ("sha512", "0", "SHA512SUMS")],
            )
        with self.assertRaisesRegex(bump.BumpError, "nothing|no digest"):
            bump.verified("bf1", "x.jar", data, [("sha256", "", "asset digest")])


if __name__ == "__main__":
    unittest.main()
