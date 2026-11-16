import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import bump_mod_jar as bump  # noqa: E402
import mirror_mod_jar as mirror  # noqa: E402
import release  # noqa: E402

MOD = "lr-tactical"
GH_AUTH_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)


@contextlib.contextmanager
def offline():
    """Fail on any real gh spawn or HTTP call; children inherit no gh auth.

    A test once reached the real `gh release upload` and left a junk asset in the
    public pack mirror; every gh/HTTP call here must go through a fake.
    """
    real = subprocess.Popen

    def popen(args, *rest, **kw):
        argv = list(args) if isinstance(args, (list, tuple)) else str(args).split()
        if argv and Path(str(argv[0])).stem == "gh":
            raise AssertionError(f"test spawned a real gh: {argv}")
        return real(args, *rest, **kw)

    def urlopen(*args, **kw):
        raise AssertionError(f"test opened a real URL: {args}")

    with (
        tempfile.TemporaryDirectory() as config,
        mock.patch.dict(os.environ, {"GH_CONFIG_DIR": config}),
        mock.patch.object(subprocess, "Popen", popen),
        mock.patch.object(urllib.request, "urlopen", urlopen),
    ):
        for name in GH_AUTH_ENV:
            os.environ.pop(name, None)
        yield


def setUpModule():
    unittest.enterModuleContext(offline())


class FakeMirror:
    """release.gh stand-in: serves the mirror release listing, logs every call."""

    def __init__(self, assets):
        self.assets = dict(assets)  # {name: (size, digest)}
        self.calls = []

    def uploads(self):
        return [call for call in self.calls if call[:2] == ("release", "upload")]

    def __call__(self, *args):
        self.calls.append(args)
        listing = f"repos/{mirror.MIRROR_REPO}/releases/tags/{mirror.MIRROR_TAG}"
        if args == ("api", listing):
            assets = [
                {"name": name, "size": size, "digest": digest}
                for name, (size, digest) in self.assets.items()
            ]
            return json.dumps({"assets": assets})
        if args[:3] == ("release", "upload", mirror.MIRROR_TAG) and args[4:] == (
            "--repo",
            mirror.MIRROR_REPO,
        ):
            data = Path(args[3]).read_bytes()
            digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
            self.assets[Path(args[3]).name] = (len(data), digest)
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


URL = "https://github.com/Blockfield/lr-tactical/releases/download"


def jar_bytes(tag):
    return f"jar {tag}".encode()


def jar(tag):
    data = jar_bytes(tag)
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
        edits, _ = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        holders = {(s, p) for s, paths in bump.MODS[MOD][1].items() for p in paths}
        self.assertEqual(set(edits), holders)
        for (side, path), text in edits.items():
            self.assertEqual(text, holder(path, JARS["bf7"]), path)
        self.assertEqual(bump.plan(MOD, "bf6", self.trees, fake_fetch)[0], {})

    def test_holders_lagging_on_different_releases_all_move(self):
        path = bump.MODS[MOD][1]["client"][0]
        self.write("client", path, holder(path, JARS["bf7"]))
        edits, _ = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        self.assertNotIn(("client", path), edits)
        self.assertEqual(len(edits), 7)

    def test_bare_name_of_an_older_release_is_left_alone(self):
        path = bump.MODS[MOD][1]["mod"][1]
        guard = f'assertFalse(snapshot.contains("{jar("bf5").name}"));\n'
        self.write("mod", path, holder(path, JARS["bf6"]) + guard)
        edits, _ = bump.plan(MOD, "bf7", self.trees, fake_fetch)
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

    def test_mod_only_bump_flags_old_hash_pinned_only_in_other_trees(self):
        # After a `--only mod` bump to bf6 (gameplay/client still pin bf5), a
        # later `--only mod` bump to bf7 must still flag a forgotten bf5 hash
        # in the mod tree even though bf5 is pinned only outside the scope.
        for side in ("gameplay", "client"):
            for path in bump.MODS[MOD][1][side]:
                self.write(side, path, holder(path, JARS["bf5"]))
        self.write("mod", "src/StalePins.java", f'"{JARS["bf5"].sha256}"\n')
        self.track()
        with self.assertRaisesRegex(
            bump.BumpError, "src/StalePins.java: holds an old hash"
        ):
            bump.plan(MOD, "bf7", self.trees, fake_fetch, "mod")

    def registry(self, entries):
        """Fixture block-registry.json in the gameplay tree, tracked by git."""
        path = self.trees["gameplay"] / bump.REGISTRY
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "pins": entries}))
        subprocess.run(["git", "-C", self.trees["gameplay"], "add", "-A"], check=True)

    def pin(self, j):
        return {
            "url": f"{URL}/{j.tag}/{j.name}",
            "algorithm": "sha512",
            "hash": j.sha512,
        }

    def test_registry_pinning_an_old_release_stops_the_write(self):
        self.registry({JARS["bf6"].name: self.pin(JARS["bf6"])})
        before = self.holder_bytes()
        with self.assertRaisesRegex(bump.BumpError, "just export-registry") as ctx:
            bump.plan(MOD, "bf7", self.trees, fake_fetch)
        self.assertIn("just vendor-block-registry", str(ctx.exception))
        self.assertNotIn("not a known holder", str(ctx.exception))
        self.assertEqual(self.holder_bytes(), before)

    def test_registry_pinning_the_new_release_lets_the_bump_through(self):
        self.registry({JARS["bf7"].name: self.pin(JARS["bf7"])})
        edits, _ = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        holders = {(s, p) for s, paths in bump.MODS[MOD][1].items() for p in paths}
        self.assertEqual(set(edits), holders)

    def test_already_behind_registry_stops_the_write(self):
        # Holders moved on (bf6) without a re-export, so the registry still
        # records bf5: bumping to bf7 must stop even though no bumped old
        # release is recorded.
        self.registry({JARS["bf5"].name: self.pin(JARS["bf5"])})
        before = self.holder_bytes()
        with self.assertRaisesRegex(bump.BumpError, "just export-registry") as ctx:
            bump.plan(MOD, "bf7", self.trees, fake_fetch)
        self.assertIn(JARS["bf5"].name, str(ctx.exception))
        self.assertEqual(self.holder_bytes(), before)

    def test_stale_registry_message_names_push_merge_and_vendor_sha(self):
        self.registry({JARS["bf6"].name: self.pin(JARS["bf6"])})
        with self.assertRaisesRegex(bump.BumpError, "just export-registry") as ctx:
            bump.plan(MOD, "bf7", self.trees, fake_fetch)
        err = str(ctx.exception)
        self.assertIn("push", err)
        self.assertIn("gameplay next", err)
        self.assertIn("just vendor-block-registry <merge-sha>", err)

    def test_registry_without_this_mod_is_not_a_block(self):
        self.registry(
            {"other-1.0.jar": {"url": "x", "algorithm": "sha512", "hash": "0" * 128}}
        )
        self.assertTrue(bump.plan(MOD, "bf7", self.trees, fake_fetch)[0])

    def test_missing_registry_is_not_a_block(self):
        self.assertTrue(bump.plan(MOD, "bf7", self.trees, fake_fetch)[0])

    def test_main_writes_nothing_when_the_registry_is_stale(self):
        self.registry({JARS["bf6"].name: self.pin(JARS["bf6"])})
        before = self.holder_bytes()
        code, err, _ = self.main("bf7", mirrored=["bf6", "bf7"])
        self.assertEqual(code, 1)
        self.assertIn("just export-registry", err)
        self.assertIn("just vendor-block-registry", err)
        self.assertEqual(self.holder_bytes(), before)

    def test_write_with_a_fresh_registry_names_the_re_export(self):
        self.registry({JARS["bf7"].name: self.pin(JARS["bf7"])})
        code, out, _ = self.run_main("bf7")
        self.assertEqual(code, 0)
        self.assertIn("just export-registry", out)

    def test_mod_only_bump_with_stale_registry_proceeds(self):
        self.registry({JARS["bf6"].name: self.pin(JARS["bf6"])})
        edits, _ = bump.plan(MOD, "bf7", self.trees, fake_fetch, "mod")
        self.assertEqual(
            set(edits), {("mod", path) for path in bump.MODS[MOD][1]["mod"]}
        )
        for (side, path), text in edits.items():
            self.assertEqual(text, holder(path, JARS["bf7"]), path)

    def test_mod_only_write_leaves_gameplay_untouched_and_prints_chain(self):
        self.registry({JARS["bf6"].name: self.pin(JARS["bf6"])})
        before = {
            (side, path): (self.trees[side] / path).read_bytes()
            for side, paths in bump.MODS[MOD][1].items()
            for path in paths
            if side != "mod"
        }
        code, out, _ = self.run_main("bf7", "--only", "mod")
        self.assertEqual(code, 0)
        for (side, path), text in before.items():
            self.assertEqual((self.trees[side] / path).read_bytes(), text)
        for path in bump.MODS[MOD][1]["mod"]:
            self.assertEqual(
                (self.trees["mod"] / path).read_bytes().decode(),
                holder(path, JARS["bf7"]),
                path,
            )
        self.assertIn("--only mod", out)
        self.assertIn("remaining release chain", out)

    def test_gameplay_only_bump_with_stale_registry_still_refuses(self):
        self.registry({JARS["bf6"].name: self.pin(JARS["bf6"])})
        before = self.holder_bytes()
        with self.assertRaisesRegex(bump.BumpError, "just export-registry"):
            bump.plan(MOD, "bf7", self.trees, fake_fetch, "gameplay")
        self.assertEqual(self.holder_bytes(), before)

    def test_archive_fallback_naming_the_old_jar_is_an_error(self):
        path = bump.MODS[MOD][1]["gameplay"][0]
        text = (
            holder(path, JARS["bf6"]) + f"archiveEntry: 'mods/{JARS['bf6'].name}'],\n"
        )
        self.write("gameplay", path, text)
        with self.assertRaisesRegex(bump.BumpError, "client archive ships"):
            bump.plan(MOD, "bf7", self.trees, fake_fetch)

    def test_truncated_hash_comment_moves_with_the_pin(self):
        path = bump.MODS[MOD][1]["mod"][0]
        comment = f"// oracle sha512 {JARS['bf6'].sha512[:8]}…\n"
        self.write("mod", path, holder(path, JARS["bf6"]) + comment)
        edits, _ = bump.plan(MOD, "bf7", self.trees, fake_fetch)
        self.assertIn(f"// oracle sha512 {JARS['bf7'].sha512[:8]}…", edits["mod", path])
        self.assertNotIn(JARS["bf6"].sha512[:8] + "…", edits["mod", path])

    def main(self, *args, mirrored=()):
        """bump.main with fake fetch and gh; (exit code, stderr, gh fake)."""
        fake = FakeMirror(
            {
                JARS[tag].sha512: (
                    len(jar_bytes(tag)),
                    f"sha256:{JARS[tag].sha256}",
                )
                for tag in mirrored
            }
        )
        flags = [
            arg for side, tree in self.trees.items() for arg in (f"--{side}", str(tree))
        ]
        with (
            mock.patch.object(bump, "fetch", fake_fetch),
            mock.patch.object(release, "gh", fake),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = bump.main([MOD, *args, *flags])
        self.assertEqual(fake.uploads(), [])
        return code, err.getvalue(), fake

    def holder_bytes(self):
        return {
            (side, path): (self.trees[side] / path).read_bytes()
            for side, paths in bump.MODS[MOD][1].items()
            for path in paths
        }

    def test_main_writes_holders_and_check_lists_them(self):
        path = bump.MODS[MOD][1]["gameplay"][0]
        self.assertEqual(self.main("bf7", "--dry-run")[0], 0)
        self.assertIn(JARS["bf6"].sha256, (self.trees["gameplay"] / path).read_text())
        code, _, fake = self.main("bf7", mirrored=["bf7"])
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)  # the mirror listing, nothing else
        self.assertEqual(
            (self.trees["gameplay"] / path).read_bytes().decode(),
            holder(path, JARS["bf7"]),
        )
        self.assertEqual(self.main("--check")[0], 0)

    def test_main_refuses_a_jar_missing_from_the_mirror(self):
        before = self.holder_bytes()
        code, err, fake = self.main("bf7", mirrored=["bf6"])
        self.assertEqual(code, 1)
        self.assertIn(f"just mirror-mod-jar {MOD} bf7", err)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.holder_bytes(), before)

    def run_main(self, *args, mirrored=("bf6", "bf7"), properties=None):
        """bump.main with fake fetch and gh; (exit code, stdout, stderr)."""
        fake = FakeMirror(
            {
                JARS[tag].sha512: (
                    len(jar_bytes(tag)),
                    f"sha256:{JARS[tag].sha256}",
                )
                for tag in mirrored
            }
        )
        if properties is not None:
            props = self.trees["gameplay"] / "mod-server/common.properties"
            props.parent.mkdir(parents=True, exist_ok=True)
            props.write_text(properties)
        flags = [
            arg for side, tree in self.trees.items() for arg in (f"--{side}", str(tree))
        ]
        with (
            mock.patch.object(bump, "fetch", fake_fetch),
            mock.patch.object(release, "gh", fake),
            contextlib.redirect_stdout(io.StringIO()) as out,
            contextlib.redirect_stderr(io.StringIO()) as err,
        ):
            code = bump.main([MOD, *args, *flags])
        self.assertEqual(fake.uploads(), [])
        return code, out.getvalue(), err.getvalue()

    def test_check_reports_the_common_jar_step(self):
        code, out, _ = self.run_main("--check", properties="commonVersion=1.66.0\n")
        self.assertEqual(code, 0)
        self.assertIn("mod-server/common.properties", out)
        self.assertIn("commonVersion=1.66.0", out)
        self.assertIn("vendor-block-registry", out)
        self.assertIn("client pack", out)

    def test_check_without_properties_still_reports_the_step(self):
        code, out, _ = self.run_main("--check")
        self.assertEqual(code, 0)
        self.assertIn("mod-server/common.properties", out)

    def test_write_prints_the_remaining_chain(self):
        code, out, _ = self.run_main("bf7")
        self.assertEqual(code, 0)
        self.assertIn("remaining release chain", out)
        self.assertIn("mod-server/common.properties", out)
        self.assertIn("vendor-block-registry", out)
        self.assertIn("client pack", out)


class Offline(unittest.TestCase):
    def test_real_gh_and_http_fail_loudly(self):
        with self.assertRaisesRegex(AssertionError, "real gh"):
            release.gh("auth", "status")
        with self.assertRaisesRegex(AssertionError, "real URL"):
            urllib.request.urlopen("https://github.com")
        self.assertFalse(set(GH_AUTH_ENV) & set(os.environ))


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


class RunpyIdentity(unittest.TestCase):
    def test_bump_run_via_runpy_shares_bumperror_with_mirror(self):
        scripts = Path(bump.__file__).resolve().parent
        code = (
            "import runpy, sys; "
            f"sys.path.insert(0, {str(scripts)!r}); "
            f"ns = runpy.run_path({str(Path(bump.__file__).resolve())!r},"
            " run_name='bump_runpy_probe'); "
            "import mirror_mod_jar as m; "
            "print(ns['BumpError'] is m.bump.BumpError)"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual(done.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
