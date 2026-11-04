"""Tests for the public-export rewrite engine and fail-closed gates.

No private value appears in this file: probes are obviously synthetic
(example/acme-style names, zero-entropy IDs), and any check against the real
private inventory loads that inventory from the excluded private policy at
runtime instead of spelling it. Tests needing the private policy skip when it
is absent (public CI runs only the generic suite).
"""

import ast
import codecs
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
SCRIPT = HERE.parents[2] / "public_export.py"
CONFIG = HERE.parents[2] / "public_export_config.json"
PRIVATE = HERE.parents[2] / "public_export_private.json"
HAS_PRIVATE = PRIVATE.is_file()
TOOLING = (
    "public_export.py",
    "public_export_config.json",
    "dev/tests/test_public_export.py",
)
SHIPPED = [HERE.parents[2] / name for name in TOOLING]
spec = importlib.util.spec_from_file_location("public_export", SCRIPT)
public_export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(public_export)


def endpoint(char="a", upper=False, scheme=True):
    domain = ".R2.CLOUDFLARESTORAGE.COM" if upper else ".r2.cloudflarestorage.com"
    return ("https://" if scheme else "") + char * 32 + domain


def _fold_str(node, limit=65536):
    """Best-effort constant string folder for the shipped-source gate.

    Handles string literals, + concatenation, "s" * n repetition and joined
    f-string parts. Anything non-constant (names, calls, unbounded sizes)
    yields None: unfolded code stays covered by the raw-substring gate.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        out = "".join(parts)
        return out if len(out) <= limit else None
    if isinstance(node, ast.BinOp):
        if isinstance(node.op, ast.Add):
            left, right = _fold_str(node.left, limit), _fold_str(node.right, limit)
            if not isinstance(left, str) or not isinstance(right, str):
                return None
            out = left + right
            return out if len(out) <= limit else None
        if isinstance(node.op, ast.Mult):
            left, right = node.left, node.right
            if isinstance(left, ast.Constant) and isinstance(left.value, bool):
                return None
            if isinstance(right, ast.Constant) and isinstance(right.value, bool):
                return None
            left_s, right_s = _fold_str(left, limit), _fold_str(right, limit)
            left_n = left.value if isinstance(left, ast.Constant) else None
            right_n = right.value if isinstance(right, ast.Constant) else None
            if isinstance(left_s, str) and isinstance(right_n, int):
                num, part = right_n, left_s
            elif isinstance(right_s, str) and isinstance(left_n, int):
                num, part = left_n, right_s
            else:
                return None
            if num < 0 or len(part) * num > limit:
                return None
            return part * num
    return None


def _folded_strings(tree, limit=65536):
    """All constant string values obtainable by folding concatenations."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.BinOp, ast.JoinedStr)):
            value = _fold_str(node, limit)
            if isinstance(value, str):
                out.append(value)
    return out


def opaque_id():
    # Obviously synthetic: sequential runs, no private token inside.
    return "TestOnly000" + "00001XyZ"


def real_policy(testcase):
    if not HAS_PRIVATE:
        testcase.skipTest("private export policy absent")
    return public_export.load_policy(str(CONFIG), str(PRIVATE))


def make_repo(testcase, files, links=()):
    repo = Path(tempfile.mkdtemp(prefix="export-fixture-"))
    testcase.addCleanup(shutil.rmtree, repo, True)
    for rel, data in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    for link, dest in links:
        target = repo / link
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(dest)
    env = {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo)}
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True, env={**os.environ, **env}
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def write_policy_files(testcase, public=None, private=None):
    tmp = Path(tempfile.mkdtemp(prefix="export-policy-"))
    testcase.addCleanup(shutil.rmtree, tmp, True)
    pub = tmp / "public.json"
    pub.write_text(
        json.dumps(public if public is not None else json.loads(CONFIG.read_text()))
    )
    priv = tmp / "private.json"
    priv.write_text(
        json.dumps(
            private
            if private is not None
            else {
                "version": 1,
                "replacements": [],
                "drop_sections": [],
                "banned_tokens": [],
                "submodule_note_path": "SUBMODULES.md",
                "workflow_path": ".github/workflows/check.yml",
            }
        )
    )
    return str(pub), str(priv)


def export_fixture(testcase, files, links=(), private=None, public=None):
    repo = make_repo(testcase, files, links)
    pub, priv = write_policy_files(testcase, public, private)
    out = Path(tempfile.mkdtemp(prefix="export-out-"))
    testcase.addCleanup(shutil.rmtree, out, True)
    summary = public_export.export_tree("HEAD", out, pub, priv, repo=repo)
    return repo, summary, out


def assert_export_fails(testcase, files, rule_id, links=(), private=None, public=None):
    repo = make_repo(testcase, files, links)
    pub, priv = write_policy_files(testcase, public, private)
    out = Path(tempfile.mkdtemp(prefix="export-out-"))
    testcase.addCleanup(shutil.rmtree, out, True)
    with testcase.assertRaises(SystemExit) as ctx:
        public_export.export_tree("HEAD", out, pub, priv, repo=repo)
    testcase.assertIn(rule_id, str(ctx.exception))
    return repo


class EngineTest(unittest.TestCase):
    def test_apply_counts_and_replaces(self):
        rules = [
            {
                "id": "x",
                "paths": ["docs/**"],
                "regex": False,
                "pattern": "alpha",
                "replacement": "beta",
            }
        ]
        text, counts = public_export.apply_text("docs/n.md", "alpha alpha", rules)
        self.assertEqual((text, counts), ("beta beta", {"x": 2}))

    def test_path_globs(self):
        rules = [
            {
                "id": "x",
                "paths": ["docs/**"],
                "regex": False,
                "pattern": "a",
                "replacement": "b",
            }
        ]
        _, counts = public_export.apply_text("scripts/n.py", "a", rules)
        self.assertEqual(counts, {})

    def test_drop_section_markers_required(self):
        with self.assertRaises(SystemExit):
            public_export.apply_drop_sections(
                "no markers here",
                [{"paths": ["**"], "start": "AAA", "end": "BBB", "replacement": "C"}],
                "n.md",
            )
        text = public_export.apply_drop_sections(
            "keep\nAAA drop this\nBBB keep\n",
            [{"paths": ["**"], "start": "AAA", "end": "BBB", "replacement": "C"}],
            "n.md",
        )
        self.assertEqual(text, "keep\nC\nBBB keep\n")

    def test_drop_section_alone_changes_payload(self):
        policy = {
            "replacements": [],
            "forbidden": [],
            "drop_sections": [
                {
                    "paths": ["docs/note.md"],
                    "start": "SYNTH-START",
                    "end": "SYNTH-END",
                    "replacement": "cut here.",
                }
            ],
            "exclude_paths": [],
            "banned_tokens": [],
        }
        files = {
            "docs/note.md": (b"keep\nSYNTH-START secret-part\nSYNTH-END keep\n", 0o644)
        }
        payload, applied, dropped = public_export.build_payload(
            files, [], [], [], None, policy
        )
        self.assertEqual(
            payload["docs/note.md"][1], b"keep\ncut here.\nSYNTH-END keep\n"
        )

    def test_decode_text_tiers(self):
        self.assertEqual(public_export.decode_text(b"plain")[1], "utf-8")
        self.assertEqual(public_export.decode_text("ü".encode("utf-16")), (None, None))
        self.assertEqual(
            public_export.decode_text("caf\xe9".encode("latin-1"))[1], "latin-1"
        )
        self.assertEqual(
            public_export.decode_text("caf\xe9".encode("latin-1"))[0][3], "\xe9"
        )
        self.assertEqual(public_export.decode_text("x".encode("utf-32")), (None, None))
        self.assertEqual(public_export.decode_text(b"\x00bin")[0], None)


class PrivacyBypassTest(unittest.TestCase):
    def test_home_path(self):
        assert_export_fails(
            self, {"n.txt": "config in /ho" + "me/acme-qa/private.txt\n"}, "f-home-dir"
        )

    def test_email(self):
        assert_export_fails(
            self, {"n.txt": "contact qa.synth" + "@acme-example-test.com\n"}, "f-email"
        )

    def test_email_no_tld(self):
        assert_export_fails(
            self, {"n.txt": "contact ops" + "@acme-host" + " here\n"}, "f-email-no-tld"
        )
        repo, summary, out = export_fixture(self, {"n.txt": "a ci@example.invalid\n"})
        self.assertIn("ci@example.invalid", (out / "n.txt").read_text())

    def test_ssh_command(self):
        assert_export_fails(
            self, {"n.txt": "deploy via ss" + "h acme-bastion-01\n"}, "f-ssh"
        )

    def test_ssh_uppercase(self):
        assert_export_fails(
            self, {"n.txt": "run SS" + "H acme-home-01\n"}, "f-ssh-noun"
        )
        assert_export_fails(
            self, {"n.txt": "$ SS" + "H acme-bastion-01\n"}, "f-ssh-upper"
        )
        repo, summary, out = export_fixture(
            self, {"n.txt": "tunnel via SSH loopback ok\n"}
        )
        self.assertIn("SSH loopback", (out / "n.txt").read_text())

    def test_ssh_prose_noun(self):
        assert_export_fails(
            self, {"n.txt": "connect via SS" + "H acme-relay-02\n"}, "f-ssh-noun"
        )
        for noun in ("loopback", "tunnel", "server", "agent"):
            repo, summary, out = export_fixture(
                self, {"n.txt": "use SS" + "H " + noun + " here\n"}
            )
            self.assertIn(noun, (out / "n.txt").read_text())

    def test_ssh_config_host(self):
        assert_export_fails(
            self, {"cfg": "Host acme-ba" + "stion\n  HostName x\n"}, "f-ssh-host"
        )
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "the Host dispatcher routes it\n"}
        )
        self.assertIn("Host dispatcher", (out / "docs" / "n.md").read_text())

    def test_boat_doc_placeholders_pass(self):
        ssh_line = "just boat ss" + "h bx" + "_ID\n"
        home_line = "cd /ho" + "me/user/blockfield-workspace\n"
        exec_line = (
            "just boat exec --timeout 1800 bx" + "_ID bash -lc 'cd /ho"
            "me/user/blockfield-workspace; git status --short'\n"
        )
        repo, summary, out = export_fixture(
            self, {"docs/BOAT.md": ssh_line + home_line + exec_line}
        )
        text = (out / "docs" / "BOAT.md").read_text()
        self.assertIn("just boat ssh\n", text)
        self.assertIn("cd ~/blockfield-workspace\n", text)
        self.assertIn("cd ~/blockfield-workspace; git status --short", text)
        self.assertEqual(summary["applied"].get("boat-ssh-vm-id"), 1)
        self.assertEqual(summary["applied"].get("boat-home-workspace"), 2)

    def test_private_repo_name_becomes_public_name(self):
        # Split so this test file itself survives the export unchanged.
        line = (
            "git clone https://github.com/Blockfield/blockfield-workspace"
            + "-private.git\n"
        )
        repo, summary, out = export_fixture(self, {"README.md": line})
        text = (out / "README.md").read_text()
        self.assertEqual(
            text, "git clone https://github.com/Blockfield/blockfield-workspace.git\n"
        )
        self.assertEqual(summary["applied"].get("repo-public-name"), 1)

    def test_uppercase_endpoint_is_scrubbed(self):
        repo, summary, out = export_fixture(
            self, {"docs/n.md": endpoint(upper=True) + "\n"}
        )
        self.assertIn("<R2_ACCOUNT_ID>", (out / "docs" / "n.md").read_text())

    def test_endpoint_no_scheme_http_jurisdiction(self):
        for probe in (
            endpoint(scheme=False),
            endpoint().replace("https://", "http://"),
            endpoint().replace(".r2.", ".eu.r2."),
        ):
            repo, summary, out = export_fixture(self, {"docs/n.md": probe + "\n"})
            self.assertIn("<R2_ACCOUNT_ID>", (out / "docs" / "n.md").read_text())

    def test_bare_account_id(self):
        assert_export_fails(
            self, {"n.txt": "R2_ACCOUNT_I" + "D=" + "c" * 32 + "\n"}, "f-r2-bare-id"
        )

    def test_split_bare_account_id(self):
        split = "R2_ACCOUNT_I" + "D=" + "d" * 16 + "\n" + "d" * 16 + "\n"
        assert_export_fails(self, {"n.txt": split}, "f-r2-bare-id")

    def test_bare_account_id_json_and_yaml(self):
        key = "R2_ACCOUNT_" + "ID"
        value = "c" * 32
        cases = [
            '{"' + key + '": "' + value + '"}\n',
            key + ": " + value + "\n",
            "'" + key + "': '" + value + "'\n",
        ]
        for probe in cases:
            with self.subTest(probe=probe[:12]):
                assert_export_fails(self, {"docs/n.md": probe}, "f-r2-bare-id")

    def test_scripts_app_suffix_fails_closed(self):
        assert_export_fails(
            self,
            {"scripts/n.txt": "Dokploy `blockfield-proxy-" + "abcdef`\n"},
            "f-app-suffix",
        )

    def test_web_errors_suffixes_scrubbed(self):
        for name, stem in (
            ("blockfield-we" + "b-fedcba", "web"),
            ("blockfield-erro" + "rs-fedcba", "errors"),
        ):
            repo, summary, out = export_fixture(self, {"docs/n.md": f"app `{name}`\n"})
            self.assertIn(
                f"blockfield-{stem}-<SUFFIX>", (out / "docs" / "n.md").read_text()
            )

    def test_random_app_name(self):
        assert_export_fails(
            self,
            {"docs/n.md": "app `app-synthsvc-front" + "end-a1b2c3`\n"},
            "f-app-random",
        )

    def test_opaque_id_contexts(self):
        token = opaque_id()
        cases = [
            (f"see `{token}` here\n", "f-opaque-backtick"),
            (f'see "{token}" here\n', "f-opaque-quoted"),
            (f"key={token}\n", "f-opaque-assigned"),
            (f"url https://example.com/application/{token} ok\n", "f-opaque-bare"),
            (f"deploy {token} now\n", "f-opaque-bare"),
            (f'data {{"applicationId": "{token}"}}\n', "f-opaque-quoted"),
        ]
        for probe, rule in cases:
            with self.subTest(rule=rule):
                assert_export_fails(self, {"docs/n.md": probe}, rule)

    def test_opaque_bare_sentence_punctuation(self):
        token = "TestOnly000" + "00002XyZ"
        cases = [
            f"Deploy {token}.\n",
            f"For {token}: see notes\n",
        ]
        for probe in cases:
            with self.subTest(probe=probe[:10]):
                assert_export_fails(self, {"docs/n.md": probe}, "f-opaque-bare")

    def test_opaque_field_names_pass(self):
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "fields `clientPackSha256`, `serverPackSha256`\n"}
        )
        self.assertIn("clientPackSha256", (out / "docs" / "n.md").read_text())

    def test_opaque_allowlisted_tokens_pass(self):
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "pins `Deadmau5EarsLayer` and `Xvfb-linux-amd64` ok\n"}
        )
        text = (out / "docs" / "n.md").read_text()
        self.assertIn("Deadmau5EarsLayer", text)

    def test_opaque_allowed_then_forbidden_same_line(self):
        ident = "TestOnly" + "Abc123XyZ"
        cases = [
            ("see `Deadmau5EarsLayer` and `" + ident + "` now\n", "f-opaque-backtick"),
            ('see "Deadmau5EarsLayer" and "' + ident + '" now\n', "f-opaque-quoted"),
            ("deploy Deadmau5EarsLayer then " + ident + " now\n", "f-opaque-bare"),
            ("key=Deadmau5EarsLayer other=" + ident + "\n", "f-opaque-assigned"),
            (
                "url https://example.com/a/Deadmau5EarsLayer/b/" + ident + " ok\n",
                "f-opaque-bare",
            ),
        ]
        for probe, rule in cases:
            with self.subTest(rule=rule):
                assert_export_fails(self, {"docs/n.md": probe}, rule)

    def test_opaque_longer_token_containing_allowed(self):
        cases = [
            ("Xvfb-linux-amd64" + "x7Qa", "f-opaque-bare"),
            ("Qa7Xvfb-" + "linux-amd64", "f-opaque-bare"),
            ("TestOnly9" + "Subscribe" + "C2SPacket", "f-opaque-backtick"),
        ]
        for token, rule in cases:
            with self.subTest(token=token):
                assert_export_fails(self, {"docs/n.md": f"see `{token}` here\n"}, rule)

    def test_split_r2_endpoint_is_scrubbed(self):
        split = "https://" + "c" * 16 + "\n" + "c" * 16 + ".r2.cloudflarestorage.com"
        repo, summary, out = export_fixture(self, {"docs/n.md": split + "\n"})
        self.assertIn("<R2_ACCOUNT_ID>", (out / "docs" / "n.md").read_text())

    def test_submodule_url_variants(self):
        modules = (
            '[submodule "a"]\n\tpath = localmods/synth-priv\n'
            "\turl = https://github.com/Blockfield/synth-priv.git\n"
        )
        private = {
            "version": 1,
            "replacements": [],
            "drop_sections": [],
            "banned_tokens": [],
        }
        for url in (
            "https://github.com/Blockfield/synth-priv/blob/main/X.md",
            "http://github.com/Blockfield/synth-priv/blob/main/X.md",
            "github.com/Blockfield/synth-priv/blob/main/X.md",
            "https://www.github.com/Blockfield/synth-priv/blob/main/X.md",
            "HTTPS://GITHUB.COM/Blockfield/SYNTH-PRIV/blob/main/X.md",
        ):
            with self.subTest(url=url):
                repo, summary, out = export_fixture(
                    self,
                    {".gitmodules": modules, "docs/n.md": url + "\n"},
                    private=private,
                )
                text = (out / "docs" / "n.md").read_text()
                self.assertIn("<private-submodule>", text)
                self.assertNotIn("synth-priv/blob", text)
        repo, summary, out = export_fixture(
            self,
            {
                ".gitmodules": modules,
                "docs/n.md": "see https://github.com/Blockfield/other/blob/main/X.md\n",
            },
            private=private,
        )
        self.assertIn("Blockfield/other", (out / "docs" / "n.md").read_text())


class EncodingTest(unittest.TestCase):
    def test_utf16_fails_closed(self):
        # Fail closed: the pinned scanner reads bytes, so UTF-16 must never
        # pass as text. BOM-marked LE/BE and NUL-interleaved unmarked LE all
        # land in the binary hash allowlist, which rejects them.
        bodies = [
            ("see " + endpoint() + " here\n").encode("utf-16-le"),
            codecs.BOM_UTF16_LE + ("see " + endpoint() + " here\n").encode("utf-16-le"),
            codecs.BOM_UTF16_BE + ("see " + endpoint() + " here\n").encode("utf-16-be"),
        ]
        for data in bodies:
            with self.subTest(data=data[:4]):
                assert_export_fails(
                    self, {"docs/n.md": data}, "binary file not allowlisted by hash"
                )

    def test_utf16_secret_scanner_regression(self):
        # Real pinned scanner through the real entry point. The token below
        # is deterministic test data, never a credential. UTF-8 is the
        # positive control (gitleaks rule github-pat fires); UTF-16LE/BE
        # must fail closed as non-allowlisted binaries in both the export
        # and the public re-check.
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        token = "ghp_" + "".join(random.Random(148006).choices(alphabet, k=36))
        line = f'github_token = "{token}"\n'
        probes = {
            "utf-8": line.encode("utf-8"),
            "utf-16-le": codecs.BOM_UTF16_LE + line.encode("utf-16-le"),
            "utf-16-be": codecs.BOM_UTF16_BE + line.encode("utf-16-be"),
        }
        for name, data in probes.items():
            with self.subTest(probe=name):
                repo = make_repo(self, {"probe.txt": data})
                pub, priv = write_policy_files(self)
                out = Path(tempfile.mkdtemp(prefix="export-scan-"))
                self.addCleanup(shutil.rmtree, out, True)
                with self.assertRaises(SystemExit) as ctx:
                    public_export.main(
                        [
                            "--ref",
                            "HEAD",
                            "--out",
                            str(out),
                            "--config",
                            pub,
                            "--private",
                            priv,
                        ],
                        repo=repo,
                    )
                if name == "utf-8":
                    self.assertIn("gitleaks gate failed", str(ctx.exception))
                else:
                    self.assertIn("binary file not allowlisted", str(ctx.exception))
                    with self.assertRaises(SystemExit) as check_ctx:
                        public_export.check_tree(repo, pub)
                    self.assertIn(
                        "binary file not allowlisted", str(check_ctx.exception)
                    )

    def test_utf32_fails_closed(self):
        data = ("see " + endpoint() + " here\n").encode("utf-32")
        assert_export_fails(
            self, {"docs/n.md": data}, "binary file not allowlisted by hash"
        )

    def test_latin1_prose_is_gated(self):
        data = ("see " + endpoint() + " caf\xe9\n").encode("latin-1")
        repo, summary, out = export_fixture(self, {"docs/n.md": data})
        raw = (out / "docs" / "n.md").read_bytes().decode("latin-1")
        self.assertIn("<R2_ACCOUNT_ID>", raw)

    def test_nonallowlisted_binary_fails_closed(self):
        assert_export_fails(
            self,
            {"bin/blob.dat": b"\x00\x01\x02binary"},
            "binary file not allowlisted by hash",
        )

    def test_allowlisted_jar_verified(self):
        jar = HERE.parents[3] / "scripts/dev/testbot/gradle/wrapper/gradle-wrapper.jar"
        if not jar.is_file():
            self.skipTest("wrapper jar absent")
        import hashlib

        digest = hashlib.sha256(jar.read_bytes()).hexdigest()
        public = json.loads(CONFIG.read_text())
        public["allowed_binaries"] = {"lib/tool.jar": digest}
        good = jar.read_bytes()
        repo, summary, out = export_fixture(self, {"lib/tool.jar": good}, public=public)
        self.assertEqual((out / "lib" / "tool.jar").read_bytes(), good)
        assert_export_fails(
            self,
            {"lib/tool.jar": good + b"tampered"},
            "binary file not allowlisted by hash",
            public=public,
        )


class ScannerSuppressionTest(unittest.TestCase):
    """Tracked scanner-suppression inputs must not silence the pinned scan.

    Synthetic tokens only (deterministic github-style PATs, never
    credentials); the real pinned scanner runs through the real entry
    point in an isolated scratch repo.
    """

    ALLOWLIST_TOML = '[extend]\nuseDefault = true\n[allowlist]\npaths = [".*"]\n'

    @staticmethod
    def synthetic_token(seed):
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "ghp_" + "".join(random.Random(seed).choices(alphabet, k=36))

    def run_main(self, repo, out):
        pub, priv = write_policy_files(self)
        with self.assertRaises(SystemExit) as ctx:
            public_export.main(
                [
                    "--ref",
                    "HEAD",
                    "--out",
                    str(out),
                    "--config",
                    pub,
                    "--private",
                    priv,
                ],
                repo=repo,
            )
        return ctx

    def test_tracked_config_cannot_suppress(self):
        token = self.synthetic_token(148009)
        repo = make_repo(
            self,
            {
                "probe.txt": f'github_token = "{token}"\n',
                ".gitleaks.toml": self.ALLOWLIST_TOML,
            },
        )
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        ctx = self.run_main(repo, out)
        self.assertIn("gitleaks gate failed", str(ctx.exception))

    def test_inherited_config_env_cannot_suppress(self):
        token = self.synthetic_token(148009)
        repo = make_repo(self, {"probe.txt": f'github_token = "{token}"\n'})
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        scratch = Path(tempfile.mkdtemp(prefix="export-env-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        env_config = scratch / "allow.toml"
        env_config.write_text(self.ALLOWLIST_TOML)
        with mock.patch.dict(
            os.environ,
            {
                "GITLEAKS_CONFIG": str(env_config),
                "GITLEAKS_CONFIG_TOML": self.ALLOWLIST_TOML,
            },
        ):
            ctx = self.run_main(repo, out)
        self.assertIn("gitleaks gate failed", str(ctx.exception))

    def test_inline_allow_comment_cannot_suppress(self):
        token = self.synthetic_token(148009)
        repo = make_repo(
            self, {"probe.txt": f'github_token = "{token}" # gitleaks:allow\n'}
        )
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        ctx = self.run_main(repo, out)
        self.assertIn("gitleaks gate failed", str(ctx.exception))

    def test_tracked_ignore_file_cannot_suppress(self):
        token = self.synthetic_token(148009)
        out = Path(tempfile.mkdtemp(prefix="export-scan-"))
        self.addCleanup(shutil.rmtree, out, True)
        fingerprint = f"{out}/probe.txt:github-pat:1"
        repo = make_repo(
            self,
            {
                "probe.txt": f'github_token = "{token}"\n',
                ".gitleaksignore": fingerprint + "\n",
            },
        )
        ctx = self.run_main(repo, out)
        self.assertIn("scanner-suppression", str(ctx.exception))


class SymlinkTest(unittest.TestCase):
    def test_external_symlink_never_touched_and_dropped(self):
        outside = Path(tempfile.mkdtemp(prefix="export-outside-"))
        self.addCleanup(shutil.rmtree, outside, True)
        probe = outside / "secret.txt"
        before = "see " + endpoint() + "\n"
        probe.write_text(before)
        repo, summary, out = export_fixture(
            self, {"docs/n.md": "clean\n"}, links=[("docs/linked.txt", str(probe))]
        )
        self.assertEqual(probe.read_text(), before)
        self.assertFalse((out / "docs" / "linked.txt").exists())
        self.assertIn("docs/linked.txt", summary["symlinks_dropped"])

    def test_absolute_and_escaping_links_dropped(self):
        repo, summary, out = export_fixture(
            self,
            {"docs/n.md": "clean\n", "sub/x.txt": "clean\n"},
            links=[
                ("docs/abs.txt", "/etc/hostname"),
                ("sub/up.txt", "../../escape.txt"),
            ],
        )
        self.assertFalse((out / "docs" / "abs.txt").exists())
        self.assertFalse((out / "sub/up.txt").exists())
        self.assertEqual(
            sorted(summary["symlinks_dropped"]), ["docs/abs.txt", "sub/up.txt"]
        )

    def test_internal_link_to_scrubbed_file_kept(self):
        repo, summary, out = export_fixture(
            self, {"docs/real.txt": "clean\n"}, links=[("docs/short.txt", "real.txt")]
        )
        link = out / "docs" / "short.txt"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_text(), "clean\n")

    def test_excluded_symlink_paths_dropped(self):
        repo, summary, out = export_fixture(
            self,
            {"docs/n.md": "clean\n"},
            links=[
                ("scripts/public_export_private.json", "docs/n.md"),
                (".dev/tracked.txt", "docs/n.md"),
            ],
        )
        self.assertFalse((out / "scripts" / "public_export_private.json").exists())
        self.assertFalse((out / ".dev").exists())
        self.assertEqual(
            sorted(summary["symlinks_dropped"]),
            [".dev/tracked.txt", "scripts/public_export_private.json"],
        )


class WorkflowTest(unittest.TestCase):
    def test_generated_workflow_is_public_safe(self):
        workflow = public_export.render_workflow()
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("self-hosted", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("test_public_export", workflow)
        self.assertIn("--check .", workflow)
        self.assertIn("--redact", workflow)
        self.assertIn("--ignore-gitleaks-allow", workflow)
        self.assertIn("--gitleaks-ignore-path", workflow)
        self.assertIn("--config /tmp/gitleaks-trusted.toml", workflow)
        self.assertIn("[extend]", workflow)
        for pin in (
            public_export.WORKFLOW_PINS["checkout"],
            public_export.WORKFLOW_PINS["setup-python"],
        ):
            self.assertRegex(pin, r"\A[0-9a-f]{40}\Z")
            self.assertIn(pin, workflow)

    def test_generated_workflow_passes_actionlint(self):
        workflow = public_export.render_workflow()
        # The printf template must keep literal backslash-n so the run block
        # stays one indented YAML scalar (unescaped \n broke parsing before).
        self.assertIn("printf '[extend]\\nuseDefault = true\\n'", workflow)
        self.assertIn("unset GITLEAKS_CONFIG GITLEAKS_CONFIG_TOML", workflow)
        actionlint = shutil.which("actionlint")
        if actionlint is None and not HAS_PRIVATE:
            # Public CI has no pinned toolchain; the private CI keeps this mandatory.
            self.skipTest("actionlint not on PATH")
        self.assertIsNotNone(actionlint, "actionlint not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            wf = Path(tmp) / "public-export-check.yml"
            wf.write_text(workflow)
            result = subprocess.run(
                [actionlint, "-no-color", str(wf)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout)

    def test_source_workflows_and_dev_dropped(self):
        future = (
            "on: pull_request_target\njobs:\n  x:\n    runs-on: self-hosted\n"
            "    steps:\n      - run: echo ${{ secrets.TOKEN }}\n"
        )
        repo, summary, out = export_fixture(
            self,
            {
                ".dev/tracked.txt": "x\n",
                ".github/workflows/future.yml": future,
                ".github/workflows/legacy.yml": "on: push\njobs: {}\n",
            },
        )
        workflows = sorted(
            p.relative_to(out).as_posix()
            for p in (out / ".github" / "workflows").glob("*")
        )
        self.assertEqual(workflows, [".github/workflows/public-export-check.yml"])
        self.assertFalse((out / ".dev").exists())


class SubmoduleUrlTest(unittest.TestCase):
    def test_private_urls_rewritten_public_kept(self):
        modules = (
            '[submodule "a"]\n\tpath = localmods/synth-priv\n'
            "\turl = https://github.com/Blockfield/synth-priv.git\n"
            '[submodule "b"]\n\tpath = localmods/synth-pub\n'
            "\turl = https://github.com/Blockfield/synth-pub.git\n"
        )
        docs = (
            "see https://github.com/Blockfield/synth-priv/blob/main/docs/X.md\n"
            "and https://github.com/Blockfield/synth-pub/blob/main/docs/Y.md\n"
        )
        public = json.loads(CONFIG.read_text())
        public["public_submodule_paths"] = ["localmods/synth-pub"]
        repo, summary, out = export_fixture(
            self, {".gitmodules": modules, "docs/n.md": docs}, public=public
        )
        text = (out / "docs" / "n.md").read_text()
        self.assertNotIn("synth-priv/blob", text)
        self.assertIn("<private-submodule>", text)
        self.assertIn(
            "https://github.com/Blockfield/synth-pub/blob/main/docs/Y.md", text
        )
        self.assertFalse((out / ".gitmodules").exists())


class WorkspacePublicTreeTest(unittest.TestCase):
    """Workspace sterile tree (ws#13a): public repos stay linked, LICENSE fixed."""

    def test_public_submodule_allowlist_covers_client_and_mod(self):
        policy = real_policy(self)
        self.assertIn("client", policy["public_submodule_paths"])
        self.assertIn("localmods/blockfield-mod", policy["public_submodule_paths"])

    def test_client_and_mod_urls_survive_server_url_goes(self):
        policy = real_policy(self)
        modules = (
            '[submodule "client"]\n\tpath = client\n'
            "\turl = https://github.com/Blockfield/synth-client.git\n"
            '[submodule "mod"]\n\tpath = localmods/blockfield-mod\n'
            "\turl = https://github.com/Blockfield/synth-mod.git\n"
            '[submodule "server"]\n\tpath = server\n'
            "\turl = https://github.com/Blockfield/synth-server.git\n"
        )
        docs = (
            "see https://github.com/Blockfield/synth-client/blob/main/README.md\n"
            "see https://github.com/Blockfield/synth-mod/blob/main/CLAUDE.md\n"
            "see https://github.com/Blockfield/synth-server/blob/main/docs/DEPLOY.md\n"
        )
        files = {
            ".gitmodules": (modules.encode(), 0o644),
            "docs/n.md": (docs.encode(), 0o644),
        }
        payload, _, _ = public_export.build_payload(
            files,
            ["client", "localmods/blockfield-mod", "server"],
            [],
            [],
            modules.encode(),
            policy,
        )
        text = payload["docs/n.md"][1].decode()
        self.assertIn(
            "https://github.com/Blockfield/synth-client/blob/main/README.md", text
        )
        self.assertIn(
            "https://github.com/Blockfield/synth-mod/blob/main/CLAUDE.md", text
        )
        self.assertNotIn("synth-server", text)
        self.assertIn("<private-submodule>", text)
        self.assertFalse(
            any(r == ".gitmodules" for r in payload),
            ".gitmodules stays excluded from the public tree",
        )

    def test_public_license_replaces_source_text(self):
        policy = real_policy(self)
        files = {
            "LICENSE": (
                b"Copyright (c) 2026 Acme contributors. All rights reserved.\n",
                0o644,
            ),
            "docs/n.md": (b"clean\n", 0o644),
        }
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertEqual(
            payload["LICENSE"][1],
            b"Copyright (c) 2026 Blockfield. All rights reserved.\n",
        )

    def test_license_keys_flow_into_payload(self):
        # Synthetic policy: the license file lands byte-exact and passes the
        # public re-check, with no private policy involved.
        public = json.loads(CONFIG.read_text())
        public["license_path"] = "LICENSE"
        public["license_text"] = "Copyright (c) 2026 Acme. All rights reserved.\n"
        private = {
            "version": 1,
            "replacements": [],
            "drop_sections": [],
            "banned_tokens": [],
        }
        repo, summary, out = export_fixture(
            self,
            {"LICENSE": "old\n", "n.txt": "clean\n"},
            private=private,
            public=public,
        )
        self.assertEqual(
            (out / "LICENSE").read_text(),
            "Copyright (c) 2026 Acme. All rights reserved.\n",
        )
        result = public_export.check_tree(out)
        self.assertEqual(result["tree_files"], summary["tree_files"])

    def test_private_repo_urls_rewritten_and_gated(self):
        # Tokens load from the excluded private policy at runtime only.
        policy = real_policy(self)
        tokens = [
            entry["token"]
            for entry in policy["banned_tokens"]
            if isinstance(entry, dict) and entry.get("hint") == "private-repo"
        ]
        self.assertTrue(tokens, "private-repo tokens must exist")
        files = {
            "docs/n.md": (
                "".join(
                    f"see https://{token}/blob/main/docs/X.md\n" for token in tokens
                ).encode(),
                0o644,
            ),
            "scripts/tool.py": (
                "".join(f'URL = "https://{token}.git"\n' for token in tokens).encode(),
                0o644,
            ),
        }
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        for rel in ("docs/n.md", "scripts/tool.py"):
            text = payload[rel][1].decode()
            self.assertIn("<private-repo>", text)
            for token in tokens:
                self.assertNotIn(token, text)
                self.assertNotIn(token.upper(), text)
        for token in tokens:
            with self.subTest(path=token):
                with self.assertRaises(SystemExit):
                    public_export.build_payload(
                        {f"docs/{token}.md": (b"clean\n", 0o644)},
                        [],
                        [],
                        [],
                        None,
                        policy,
                    )


class CheckModeTest(unittest.TestCase):
    def test_check_passes_clean_tree(self):
        repo, summary, out = export_fixture(self, {"docs/n.md": "clean\n"})
        result = public_export.check_tree(out)
        self.assertEqual(result["tree_files"], summary["tree_files"])

    def test_check_fails_dirty_tree(self):
        repo, summary, out = export_fixture(self, {"docs/n.md": "clean\n"})
        (out / "docs" / "evil.md").write_text(
            "contact qa.synth" + "@acme-example-test.com\n"
        )
        with self.assertRaises(SystemExit):
            public_export.check_tree(out)

    def test_check_ignores_generated_files(self):
        repo, summary, out = export_fixture(self, {"docs/n.md": "clean\n"})
        (out / ".git").mkdir()
        (out / ".git" / "index").write_bytes(b"\x00\x01binary")
        cache = out / ".cache" / "quality"
        cache.mkdir(parents=True)
        (cache / "tool").write_bytes(b"\x00\x01binary")
        pycache = out / "scripts" / "__pycache__"
        pycache.mkdir(parents=True)
        (pycache / "x.pyc").write_bytes(b"\x00\x01binary")
        result = public_export.check_tree(out)
        self.assertEqual(result["tree_files"], summary["tree_files"])

    def test_check_reads_force_tracked_generated_files(self):
        repo = make_repo(self, {"docs/n.md": "clean\n", ".gitignore": ".cache/\n"})
        wf = repo / ".github" / "workflows" / "public-export-check.yml"
        wf.parent.mkdir(parents=True)
        wf.write_text(public_export.render_workflow())
        tracked = repo / ".cache" / "eval.txt"
        tracked.parent.mkdir(parents=True)
        tracked.write_text("config in /ho" + "me/acme-qa/private.txt here\n")
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@localhost",
                "add",
                "-f",
                ".cache/eval.txt",
                ".github/workflows/public-export-check.yml",
            ],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "tracked",
            ],
            cwd=repo,
            check=True,
        )
        with self.assertRaises(SystemExit) as ctx:
            public_export.check_tree(repo)
        self.assertIn("f-home-dir", str(ctx.exception))

    def test_gitleaks_gets_absolute_out(self):
        repo = make_repo(self, {"n.txt": "clean\n"})
        pub, priv = write_policy_files(
            self,
            json.loads(CONFIG.read_text()),
            {
                "version": 1,
                "replacements": [],
                "drop_sections": [],
                "banned_tokens": [],
            },
        )
        captured = {}
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and len(cmd) > 1 and cmd[1] == "detect":
                captured["source"] = cmd[cmd.index("--source") + 1]
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run(cmd, **kwargs)

        work = Path(tempfile.mkdtemp(prefix="export-cwd-"))
        self.addCleanup(shutil.rmtree, work, True)
        previous = os.getcwd()
        os.chdir(work)
        self.addCleanup(os.chdir, previous)
        with mock.patch.object(public_export.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(
                public_export, "ensure_gitleaks", return_value="/tmp/fake-gitleaks"
            ):
                public_export.main(
                    [
                        "--ref",
                        "HEAD",
                        "--out",
                        "rel-out",
                        "--config",
                        pub,
                        "--private",
                        priv,
                    ],
                    repo=repo,
                )
        self.assertTrue(Path(captured["source"]).is_absolute)

    def test_simulated_public_ci(self):
        policy = real_policy(self)
        files, gitlinks, symlinks, specials, gitmodules = (
            public_export.read_ref_snapshot("HEAD", public_export.ROOT)
        )
        root = HERE.parents[3]
        for name in TOOLING:
            data = (root / "scripts" / name).read_bytes()
            files[f"scripts/{name}"] = (data, 0o644)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        for chunk in status.split("\0"):
            if len(chunk) < 4:
                continue
            flag, rel = chunk[:2], chunk[3:]
            target = root / rel
            if rel == ".dev" or rel.startswith(".dev/"):
                continue
            if flag.strip() == "?" and not target.is_file():
                continue
            if target.is_file():
                files[rel] = (target.read_bytes(), files.get(rel, (b"", 0o644))[1])
        payload, applied, dropped = public_export.build_payload(
            files, gitlinks, symlinks, specials, gitmodules, policy
        )
        out = Path(tempfile.mkdtemp(prefix="export-sim-"))
        self.addCleanup(shutil.rmtree, out, True)
        public_export.write_output(out, payload)
        self.assertFalse((out / "scripts" / "public_export_private.json").exists())
        init = subprocess.run(["git", "init", "-q"], cwd=out)
        self.assertEqual(init.returncode, 0)
        subprocess.run(["git", "add", "-A"], cwd=out, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@localhost",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "sim",
            ],
            cwd=out,
            check=True,
        )
        # Actual CI order: unit tests first, then the re-check.
        tests = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "unittest",
                "discover",
                "-s",
                "scripts/dev/tests",
                "-p",
                "test_public_export*.py",
            ],
            cwd=out,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(tests.returncode, 0, tests.stdout[-2000:])
        result = public_export.check_tree(out)
        self.assertTrue(result["tree_files"] > 0)


class GitleaksCacheTest(unittest.TestCase):
    def test_cached_executable_is_reverified(self):
        binary = public_export.ensure_gitleaks()
        good = binary.read_bytes()
        try:
            binary.write_bytes(b"corrupt")
            restored = public_export.ensure_gitleaks()
            self.assertEqual(restored.read_bytes(), good)
        finally:
            binary.write_bytes(good)


@unittest.skipUnless(HAS_PRIVATE, "private export policy absent")
class PrivatePolicyTest(unittest.TestCase):
    def test_pr_snapshot_passes_full_gate(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        files, gitlinks, symlinks, specials, gitmodules = (
            public_export.read_ref_snapshot("HEAD", public_export.ROOT)
        )
        root = HERE.parents[3]
        for name in TOOLING:
            data = (root / "scripts" / name).read_bytes()
            files[f"scripts/{name}"] = (data, 0o644)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode("utf-8")
        for chunk in status.split("\0"):
            if len(chunk) < 4:
                continue
            flag, rel = chunk[:2], chunk[3:]
            target = root / rel
            if rel == ".dev" or rel.startswith(".dev/"):
                continue
            if flag.strip() == "?" and not target.is_file():
                continue
            if target.is_file():
                files[rel] = (target.read_bytes(), files.get(rel, (b"", 0o644))[1])
        payload, applied, dropped = public_export.build_payload(
            files, gitlinks, symlinks, specials, gitmodules, policy
        )
        public_export.verify_allowed_binaries(payload, policy["allowed_binaries"])
        workflows = sorted(r for r in payload if r.startswith(".github/workflows/"))
        self.assertEqual(workflows, [policy["workflow_path"]])
        self.assertFalse(any(r == ".dev" or r.startswith(".dev/") for r in payload))
        _, sub_gate = public_export.submodule_rules(
            gitmodules, policy.get("public_submodule_paths", [])
        )
        for rel, entry in payload.items():
            text = (
                entry[1]
                if entry[0] == "link"
                else public_export.decode_text(entry[1])[0]
            )
            if text is not None:
                self.assertEqual(public_export.scan_text(rel, text, sub_gate), [], rel)
        for name in TOOLING:
            self.assertEqual(
                payload[f"scripts/{name}"][1], (root / "scripts" / name).read_bytes()
            )

    def test_banned_token_coverage(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        for entry in policy["banned_tokens"]:
            token = entry["token"] if isinstance(entry, dict) else entry
            hint = entry.get("hint", "?") if isinstance(entry, dict) else "?"
            variants = list(dict.fromkeys([token, token.title()]))
            for variant in variants:
                with self.subTest(token=hint):
                    files = {"probe.md": (f"prefix {variant} suffix\n".encode(), 0o644)}
                    try:
                        payload, applied, _ = public_export.build_payload(
                            files, [], [], [], None, policy
                        )
                    except SystemExit:
                        continue  # fail-closed gate caught it: covered
                    changed = any(
                        e[1] != files[r][0]
                        for r, e in payload.items()
                        if e[0] == "file" and r in files
                    )
                    self.assertTrue(changed or applied, variant)

    def test_mixed_case_domain_gate(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        entry = next(
            e
            for e in policy["banned_tokens"]
            if isinstance(e, dict) and e.get("hint") == "domain"
        )
        variant = entry["token"].title()
        self.assertNotEqual(variant, entry["token"])
        self.assertEqual(variant.casefold(), entry["token"].casefold())
        cases = {
            "probe.md": f"Service URL: https://{variant}/docs\n",
            f"docs/{variant}.md": "clean\n",
        }
        for rel, content in cases.items():
            with self.subTest(path=rel):
                files = {rel: (content.encode(), 0o644)}
                with self.assertRaises(SystemExit):
                    public_export.build_payload(files, [], [], [], None, policy)

    def test_mixed_case_identity_third_party_gate(self):
        # Every identity/third-party name is identifying in any
        # capitalization: a mixed-case variant in an ordinary document or
        # filename must be rejected. Values load from the excluded private
        # policy at runtime only, never spelled in this file.
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        entries = [
            e
            for e in policy["banned_tokens"]
            if isinstance(e, dict) and e.get("hint") in ("identity", "third-party")
        ]
        self.assertTrue(entries)
        for entry in entries:
            token = entry["token"]
            variant = token.title()
            if variant in (token, token.upper()):
                variant = "".join(
                    c.upper() if i % 2 else c.lower() for i, c in enumerate(token)
                )
            self.assertEqual(variant.casefold(), token.casefold())
            cases = {
                "probe.md": f"Private owner/service: {variant}\n",
                f"docs/{variant}.md": "clean\n",
            }
            for rel, content in cases.items():
                with self.subTest(hint=entry.get("hint"), path=rel):
                    files = {rel: (content.encode(), 0o644)}
                    with self.assertRaises(SystemExit):
                        public_export.build_payload(files, [], [], [], None, policy)

    def test_date_stamped_backup_filename_rejected(self):
        # Short infrastructure node names stay identifying when glued to a
        # date stamp: substring matching applies in contents and filenames.
        # Values load from the excluded private policy at runtime only.
        policy = real_policy(self)
        node = next(
            entry["token"]
            for entry in policy["banned_tokens"]
            if isinstance(entry, dict)
            and entry.get("hint") == "node"
            and len(entry["token"]) == 7
        )
        cases = [
            {"probe.md": f"Backup file: {node}.log\n"},
            {"probe.md": f"Backup file: {node}20261029.log\n"},
            {f"docs/{node}20261029.log": "clean\n"},
        ]
        for files in cases:
            with self.subTest(files=sorted(files)):
                encoded = {
                    rel: (data.encode("utf-8"), 0o644) for rel, data in files.items()
                }
                with self.assertRaises(SystemExit):
                    public_export.build_payload(encoded, [], [], [], None, policy)

    def test_shipped_files_contain_no_private_literals(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        variants = set()
        for entry in policy["banned_tokens"]:
            token = entry["token"] if isinstance(entry, dict) else entry
            variants.add(token)
            variants.add(token.upper())
        texts = {
            p.relative_to(HERE.parents[3]).as_posix(): p.read_text(encoding="utf-8")
            for p in SHIPPED
        }
        for rel, text in texts.items():
            hits = sum(1 for v in variants if v and v in text)
            self.assertFalse(hits, f"private literals in shipped file: {rel}")
            findings = public_export.scan_text(
                rel, text, public_export.load_config(str(CONFIG))["forbidden"]
            )
            self.assertEqual(findings, [], rel)

    def test_folded_fragments_contain_no_private_literals(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        variants = set()
        for entry in policy["banned_tokens"]:
            token = entry["token"] if isinstance(entry, dict) else entry
            variants.add(token)
            variants.add(token.upper())
        for path in SHIPPED:
            if path.suffix != ".py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            joined = _folded_strings(tree)
            rel = path.relative_to(HERE.parents[3]).as_posix()
            hits = [v for v in variants if v and any(v in s for s in joined)]
            self.assertEqual(hits, [], rel)

    def test_folded_fragments_catch_split_token(self):
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        tokens = [
            entry["token"] if isinstance(entry, dict) else entry
            for entry in policy["banned_tokens"]
        ]
        tokens = [t for t in tokens if len(t) >= 8]
        self.assertTrue(tokens, "need a multi-char token for the fold probe")
        token = sorted(tokens, key=len)[-1]
        left, right = token[: len(token) // 2], token[len(token) // 2 :]
        source = "x = " + json.dumps(left) + " + " + json.dumps(right) + "\n"
        folded = _folded_strings(ast.parse(source))
        self.assertTrue(
            any(token in s for s in folded),
            "split private token must survive constant folding",
        )
        synthetic = _folded_strings(
            ast.parse('x = "acme-" + "widget-42"\ny = "qa" + "@" + "example.test"\n')
        )
        self.assertTrue(synthetic, "synthetic fixture must fold")
        self.assertFalse(
            any(token in s for s in synthetic for token in tokens),
            "synthetic fixture must not match the private inventory",
        )

    def test_missing_private_policy_fails_closed(self):
        repo = make_repo(self, {"n.txt": "clean\n"})
        out = Path(tempfile.mkdtemp(prefix="export-out-"))
        self.addCleanup(shutil.rmtree, out, True)
        with self.assertRaises(SystemExit) as ctx:
            public_export.export_tree(
                "HEAD", out, str(CONFIG), str(out / "nope.json"), repo=repo
            )
        self.assertIn("private export policy missing", str(ctx.exception))


CLIENT_CONFIG = HERE.parents[2] / "public_export_client.json"


class ClientOverlayTest(unittest.TestCase):
    """Client sterile-export overlay: deploy stays private, pack data passes."""

    def overlay_policy(self):
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        return public_export.load_policy(str(CONFIG), str(PRIVATE), str(CLIENT_CONFIG))

    def bare_policy(self):
        """Overlay policy without the workflow inventory (content-scan fixtures)."""
        policy = dict(self.overlay_policy())
        policy.pop("allowed_workflows", None)
        policy.pop("stub_files", None)
        policy.pop("license_path", None)
        policy.pop("license_text", None)
        return policy

    def test_overlay_file_is_public_safe(self):
        overlay = json.loads(CLIENT_CONFIG.read_text())
        self.assertEqual(overlay["repo"], "client")
        if HAS_PRIVATE:
            policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
            variants = set()
            for entry in policy["banned_tokens"]:
                token = entry["token"] if isinstance(entry, dict) else entry
                variants.add(token)
                variants.add(token.upper())
            text = CLIENT_CONFIG.read_text(encoding="utf-8")
            hits = [v for v in variants if v and v in text]
            self.assertEqual(hits, [], "private literals in client overlay")

    def test_resolve_target(self):
        repo, config, private, overlay = public_export.resolve_target("client")
        self.assertEqual(Path(overlay).name, "public_export_client.json")
        self.assertEqual(Path(private).name, "public_export_private.json")
        self.assertEqual(Path(repo).name, "client")
        if HAS_PRIVATE:
            self.assertTrue(Path(private).is_file())
            self.assertTrue(Path(repo).is_dir())
        with self.assertRaises(SystemExit):
            public_export.resolve_target("no-such-target")

    def test_overlay_excludes_deploy_cluster(self):
        policy = self.overlay_policy()
        for rel in (
            ".github/workflows/deploy.yml",
            "scripts/deploy-client.py",
            "scripts/deploy-webhooks.py",
            "scripts/workshop-deploy.py",
            "scripts/pack-swap.py",
            "scripts/check-deployed.py",
            "scripts/update-pack-gate.py",
            "tests/test_deploy.py",
            "tests/test_deploy_webhooks.py",
            "tests/test_workshop_deploy.py",
            "tests/test_pack_swap.py",
            "tests/test_live_pack_gate.py",
        ):
            self.assertIn(rel, policy["exclude_paths"], rel)

    def test_overlay_keeps_pack_workflows(self):
        policy = self.overlay_policy()
        self.assertEqual(
            sorted(policy["allowed_workflows"]),
            [
                ".github/workflows/client.yml",
                ".github/workflows/deploy.yml",
                ".github/workflows/pr-text.yml",
            ],
        )

    def test_deploy_stub_replaces_excluded_original(self):
        policy = self.overlay_policy()
        stub = policy["stub_files"][".github/workflows/deploy.yml"]
        self.assertIn("workflow_call", stub)
        self.assertIn("tag", stub)
        self.assertNotIn("blockfield-deploy", stub)
        # Split so this test file itself survives the export unchanged.
        private = (
            "runs-on: blockfield-deploy\n    steps:\n      - run: echo blockfield-proxy-"
            + "abcdef\n"
        )
        files = {
            ".github/workflows/deploy.yml": (private.encode(), 0o644),
            ".github/workflows/client.yml": (
                b"jobs:\n  deploy:\n    uses: ./.github/workflows/deploy.yml\n",
                0o644,
            ),
            ".github/workflows/pr-text.yml": (b"on: push\n", 0o644),
        }
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertEqual(payload[".github/workflows/deploy.yml"][1].decode(), stub)

    def test_deploy_stub_passes_actionlint(self):
        policy = self.overlay_policy()
        stub = policy["stub_files"][".github/workflows/deploy.yml"]
        actionlint = shutil.which("actionlint")
        if actionlint is None and not HAS_PRIVATE:
            self.skipTest("actionlint not on PATH")
        self.assertIsNotNone(actionlint, "actionlint not on PATH")
        with tempfile.TemporaryDirectory() as tmp:
            caller = (
                "on: push\njobs:\n  deploy:\n"
                "    uses: ./.github/workflows/deploy.yml\n"
                "    with:\n      tag: v1\n"
                "    secrets: inherit\n"
            )
            Path(tmp, "deploy.yml").write_text(stub)
            Path(tmp, "caller.yml").write_text(caller)
            result = subprocess.run(
                [actionlint, "-no-color", f"{tmp}/caller.yml", f"{tmp}/deploy.yml"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout)

    def test_deploy_stub_runs_pinned_runner(self):
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        policy = public_export.load_policy(
            str(CONFIG), str(PRIVATE), str(CLIENT_CONFIG)
        )
        stub = policy["stub_files"][".github/workflows/deploy.yml"]
        self.assertIn("runs-on: ubuntu-24.04", stub)
        self.assertNotIn("ubuntu-latest", stub)

    def test_overlay_stays_synced_with_base_forbidden(self):
        base = public_export.load_config(str(CONFIG))
        policy = self.overlay_policy()
        merged = {rule["id"]: rule for rule in policy["forbidden"]}
        for rule in base["forbidden"]:
            self.assertIn(rule["id"], merged)
            self.assertEqual(merged[rule["id"]]["pattern"], rule["pattern"])

    def test_pack_manifest_opaque_names_pass(self):
        policy = self.bare_policy()
        # Split so this test file itself survives the export unchanged.
        # Fixtures mirror real manifest shapes: the opaque run never spans a
        # whole quoted value, so only the assigned+bare gates (still exempt in
        # manifests) fire, not the re-armed quoted/backtick gates.
        stem = "SynthOnly" + "-ModAlpha99-v1"
        files = {
            "mods/a.pw.toml": (f'filename = "{stem}.jar"\n'.encode(), 0o644),
            "shaderpacks/b.pw.toml": (f'filename = "{stem}.zip"\n'.encode(), 0o644),
            "launcher.json": (
                f'{{"dl": {{"url": "https://example.invalid/p/{stem}/x"}}}}\n'.encode(),
                0o644,
            ),
        }
        key = "F3CopyIn" + "Inventory"
        files["config/kiwi-client.yaml"] = (f"{key}: true\n".encode(), 0o644)
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertEqual(len(payload), 4)

    def test_opaque_patches_are_scoped_per_gate(self):
        policy = self.bare_policy()
        by_id = {rule["id"]: rule for rule in policy["forbidden"]}
        # Probe (client a548bad, no patches): backtick/quoted never fire,
        # assigned fires only in pack manifests, bare only in manifests,
        # launcher.json and one config key. No gate stays blanket-exempt.
        self.assertEqual(by_id["f-opaque-backtick"].get("except_paths", []), [])
        self.assertEqual(by_id["f-opaque-quoted"].get("except_paths", []), [])
        self.assertEqual(
            by_id["f-opaque-assigned"].get("except_paths"),
            ["mods/*.pw.toml", "shaderpacks/*.pw.toml"],
        )
        bare = by_id["f-opaque-bare"]
        self.assertEqual(
            bare.get("except_paths"),
            ["mods/*.pw.toml", "shaderpacks/*.pw.toml", "launcher.json"],
        )
        self.assertIn("F3CopyIn" + "Inventory", bare.get("allow_exact", []))

    def test_scoped_opaque_gates_still_fire_elsewhere(self):
        policy = self.bare_policy()
        stem = "SynthOnly" + "ModAlpha99"
        ok = {
            rel: (f'filename = "{stem}.jar"\n'.encode(), 0o644)
            for rel in ["mods/a.pw.toml", "shaderpacks/b.pw.toml"]
        }
        payload, _, _ = public_export.build_payload(ok, [], [], [], None, policy)
        self.assertEqual(len(payload), 2)
        # Same token outside a pack manifest stays gated (assigned and bare).
        bad = {"config/other.toml": (f'filename = "{stem}.jar"\n'.encode(), 0o644)}
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-opaque-", str(ctx.exception))
        # Backtick/quoted gates are re-armed inside manifests too.
        for rule, text in (
            ("f-opaque-backtick", f"`{stem}`\n"),
            ("f-opaque-quoted", f'"{stem}"\n'),
        ):
            with self.assertRaises(SystemExit) as ctx:
                public_export.build_payload(
                    {"mods/a.pw.toml": (text.encode(), 0o644)},
                    [],
                    [],
                    [],
                    None,
                    policy,
                )
            self.assertIn(rule, str(ctx.exception))
        # launcher.json keeps only the bare exemption: assigned still fires.
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(
                {"launcher.json": (f'"dl": {stem}\n'.encode(), 0o644)},
                [],
                [],
                [],
                None,
                policy,
            )
        self.assertIn("f-opaque-assigned", str(ctx.exception))

    def test_patch_return_label_passes(self):
        policy = self.bare_policy()
        files = {
            "tools/packwiz-installer/installer.patch": (
                b"+                        return@" + b"createContext\n",
                0o644,
            )
        }
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertIn("tools/packwiz-installer/installer.patch", payload)

    def test_synthetic_hook_fixtures_pass(self):
        policy = self.bare_policy()
        # Fixture values come from the overlay itself so no raw literal that
        # matches the pre-push identifier grep ever appears in this file.
        suffix_rule = next(r for r in policy["forbidden"] if r["id"] == "f-app-suffix")
        allowed = list(suffix_rule.get("allow_exact", []))
        self.assertTrue(allowed, "overlay must pin the synthetic fixture values")
        files = {
            "tests/test_hooks.py": (
                "".join(f"endpoint {value} !\n" for value in allowed).encode()
                + b"GIT_AUTHOR_EMAIL=t@"
                + b"t\n",
                0o644,
            )
        }
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertIn("tests/test_hooks.py", payload)

    def test_new_hook_suffix_still_fails(self):
        policy = self.bare_policy()
        fresh = "blockfield-proxy-" + "zzz999"  # split: never raw in this file
        bad = {"tests/test_hooks.py": (f"endpoint {fresh} !\n".encode(), 0o644)}
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-app-suffix", str(ctx.exception))

    def test_base_suffix_gate_has_no_overlay_path_exemption(self):
        # Review #269: the workspace gate must exempt only the reviewed
        # fixture values on the overlay file, never the whole file.
        base = public_export.load_config(str(CONFIG))
        suffix = next(r for r in base["forbidden"] if r["id"] == "f-app-suffix")
        self.assertEqual(suffix.get("except_paths", []), [])
        overlay = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8"))
        pinned = overlay["rule_patches"]["f-app-suffix"]["allow_exact"]
        self.assertTrue(pinned, "overlay must pin the synthetic fixture values")
        self.assertEqual(sorted(suffix.get("allow_exact", [])), sorted(pinned))

    def test_base_email_gate_has_no_overlay_path_exemption(self):
        # Review #269: the workspace no-TLD-email gate must exempt only the
        # reviewed fixture values on the overlay file, never the whole file.
        base = public_export.load_config(str(CONFIG))
        rule = next(r for r in base["forbidden"] if r["id"] == "f-email-no-tld")
        self.assertEqual(rule.get("except_paths", []), [])
        overlay = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8"))
        pinned = overlay["rule_patches"]["f-email-no-tld"]["allow_exact"]
        self.assertTrue(pinned, "overlay must pin the reviewed email fixtures")
        self.assertEqual(sorted(rule.get("allow_exact", [])), sorted(pinned))

    def test_new_overlay_suffix_still_fails(self):
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        fresh = "blockfield-proxy-" + "zzz999"  # split: never raw in this file
        bad = {
            "scripts/public_export_client.json": (
                f"overlay pins {fresh}\n".encode(),
                0o644,
            )
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-app-suffix", str(ctx.exception))

    def test_new_overlay_address_still_fails(self):
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
        fresh = "ops@" + "internal"  # split: never raw in this file
        bad = {
            "scripts/public_export_client.json": (
                f"contact {fresh}\n".encode(),
                0o644,
            )
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-email-no-tld", str(ctx.exception))

    def test_installer_patch_exemption_is_value_scoped(self):
        # Review #269: the client overlay must exempt the reviewed patch
        # match, not the whole installer patch file.
        overlay = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8"))
        patch = overlay["rule_patches"]["f-email-no-tld"]
        self.assertEqual(patch.get("except_paths", []), [])
        self.assertIn("return@createContext", patch.get("allow_exact", []))
        policy = self.bare_policy()
        good = {
            "tools/packwiz-installer/installer.patch": (
                b"+                        return@createContext\n",
                0o644,
            )
        }
        payload, _, _ = public_export.build_payload(good, [], [], [], None, policy)
        self.assertIn("tools/packwiz-installer/installer.patch", payload)
        fresh = "ops@" + "internal"  # split: never raw in this file
        bad = {
            "tools/packwiz-installer/installer.patch": (
                f"contact {fresh}\n".encode(),
                0o644,
            )
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-email-no-tld", str(ctx.exception))

    def test_client_target_drops_path_token_exemptions(self):
        # Review #269: workspace-reviewed path exemptions for private
        # literals must not silently cover client files at the same paths.
        # Whole-file exempts and spelling exempts are dropped; only the
        # overlay-declared word paths carry over to the target export.
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        base = public_export.load_policy(str(CONFIG), str(PRIVATE))
        self.assertTrue(
            any(
                isinstance(e, dict) and e.get("except_paths")
                for e in base["banned_tokens"]
            ),
            "base policy must keep its reviewed path exemptions",
        )
        overlay = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8"))
        keep = overlay.get("banned_token_word_paths", [])
        self.assertTrue(keep, "overlay must declare its reviewed word paths")
        client = self.overlay_policy()
        for entry in client["banned_tokens"]:
            if not isinstance(entry, dict):
                continue
            self.assertEqual(entry.get("except_paths", []), [])
            self.assertEqual(entry.get("except_spellings", []), [])
            for word in entry.get("except_words", []):
                self.assertIn(word.get("path"), keep)
        kept = [
            (w.get("path"), w.get("word"))
            for e in client["banned_tokens"]
            if isinstance(e, dict)
            for w in e.get("except_words", [])
        ]
        self.assertTrue(kept, "declared word exemptions must survive")

    def test_client_export_rejects_workspace_exempted_literal(self):
        # Review #269: a private literal at a workspace-exempted path in the
        # client tree must fail the client export. Token loads from the
        # excluded private policy at runtime only, never spelled here.
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        base = public_export.load_policy(str(CONFIG), str(PRIVATE))
        token = next(
            e["token"]
            for e in base["banned_tokens"]
            if isinstance(e, dict) and e.get("hint") == "org-name"
        )
        policy = self.bare_policy()
        bad = {
            "docs/assets-manifest.json": (f"owner {token}\n".encode(), 0o644),
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("token:org-name", str(ctx.exception))

    def test_client_export_rejects_workspace_spelling(self):
        # Review #269: a workspace-reviewed spelling exemption must not cover
        # the same path in the client tree either. Spelling loads from the
        # excluded private policy at runtime only, never spelled here.
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        base = public_export.load_policy(str(CONFIG), str(PRIVATE))
        spelling = next(
            (
                w["spelling"]
                for e in base["banned_tokens"]
                if isinstance(e, dict) and e.get("hint") == "identity"
                for w in e.get("except_spellings", []) or []
                if w.get("path") == "docs/assets-manifest.json"
            ),
            None,
        )
        self.assertIsNotNone(spelling, "base policy must keep its spelling")
        policy = self.bare_policy()
        bad = {
            "docs/assets-manifest.json": (f"owner {spelling}\n".encode(), 0o644),
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("token:identity", str(ctx.exception))

    def test_kept_word_still_passes_client_export(self):
        # The overlay-declared review carries the shared config word over so
        # the real client file keeps passing the target export.
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        base = public_export.load_policy(str(CONFIG), str(PRIVATE))
        overlay = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8"))
        keep = overlay.get("banned_token_word_paths", [])
        pair = next(
            (
                (w["path"], w["word"])
                for e in base["banned_tokens"]
                if isinstance(e, dict)
                for w in e.get("except_words", []) or []
                if w.get("path") in keep
            ),
            None,
        )
        self.assertIsNotNone(pair, "overlay keep-list must match a base word")
        policy = self.bare_policy()
        rel, word = pair
        payload, _, _ = public_export.build_payload(
            {rel: (f"key {word}=4.0\n".encode(), 0o644)}, [], [], [], None, policy
        )
        self.assertIn(rel, payload)

    def test_overlay_carries_no_raw_identifier_literals(self):
        # The workspace pre-push hook greps raw added lines for the
        # infrastructure-identifier shape: fixture values in the overlay must
        # stay \u-escaped so pushing this file never trips that gate.
        text = CLIENT_CONFIG.read_text(encoding="utf-8")
        overlay = json.loads(text)
        values = overlay["rule_patches"]["f-app-suffix"]["allow_exact"]
        self.assertTrue(values, "f-app-suffix must pin exact fixture values")
        for value in values:
            self.assertNotIn(value, text)
            self.assertRegex(value, r"\Ablockfield-proxy-[a-z0-9]{6}\Z")

    def test_checkout_tag_passes_but_real_suffix_fails(self):
        policy = self.bare_policy()
        good = {
            ".github/workflows/client.yml": (
                b"- uses: actions/checkout@" + b"v4\n",
                0o644,
            )
        }
        payload, _, _ = public_export.build_payload(good, [], [], [], None, policy)
        self.assertIn(".github/workflows/client.yml", payload)
        bad = {
            "scripts/n.txt": (b"app `blockfield-proxy-" + b"abcdef`\n", 0o644),
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-app-suffix", str(ctx.exception))

    def test_unexpected_workflow_rejected(self):
        policy = self.overlay_policy()
        files = {
            ".github/workflows/client.yml": (b"on: push\n", 0o644),
            ".github/workflows/extra.yml": (b"on: push\n", 0o644),
        }
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(files, [], [], [], None, policy)
        self.assertIn("unexpected workflow inventory", str(ctx.exception))

    def test_nonallowlisted_binary_rejected(self):
        policy = self.bare_policy()
        payload = {"assets/icon.png": ("file", b"\x00\x01binary", 0o644)}
        with self.assertRaises(SystemExit) as ctx:
            public_export.verify_allowed_binaries(payload, policy["allowed_binaries"])
        self.assertIn("binary file not allowlisted", str(ctx.exception))

    def test_single_commit(self):
        make_repo(self, {"n.txt": "clean\n"})
        out = Path(tempfile.mkdtemp(prefix="export-commit-"))
        self.addCleanup(shutil.rmtree, out, True)
        target = out / "tree"
        (target).mkdir(parents=True, exist_ok=True)
        (target / "n.txt").write_text("clean\n")
        sha = public_export.commit_tree(
            target, "Sterile export test", "2026-10-31T12:00:00+00:00"
        )
        self.assertRegex(sha, r"\A[0-9a-f]{40}\Z")
        log = (
            subprocess.run(
                ["git", "-C", str(target), "rev-list", "--count", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        self.assertEqual(log, "1")

    def test_commit_pins_source_date(self):
        target = Path(tempfile.mkdtemp(prefix="export-commit-date-"))
        self.addCleanup(shutil.rmtree, target, True)
        (target / "n.txt").write_text("clean\n")
        date = "2026-10-31T12:00:00+00:00"
        public_export.commit_tree(target, "Sterile export test", date)
        # Git prints a zero UTC offset as Z (newer) or +00:00 (older, e.g.
        # the Boat golden image's git 2.43); the stored instant is exact
        # either way, and both spellings denote that same instant.
        want = ("2026-10-31T12:00:00Z", "2026-10-31T12:00:00+00:00")
        author = (
            subprocess.run(
                ["git", "-C", str(target), "show", "-s", "--format=%aI", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        committer = (
            subprocess.run(
                ["git", "-C", str(target), "show", "-s", "--format=%cI", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        self.assertIn(author, want)
        self.assertIn(committer, want)

    def test_commit_is_reproducible_for_source_date(self):
        date = "2026-10-31T12:00:00+00:00"
        shas = []
        for prefix in ("export-commit-repro-a-", "export-commit-repro-b-"):
            target = Path(tempfile.mkdtemp(prefix=prefix))
            self.addCleanup(shutil.rmtree, target, True)
            (target / "n.txt").write_text("clean\n")
            shas.append(public_export.commit_tree(target, "Sterile export", date))
        self.assertEqual(shas[0], shas[1])

    def test_commit_keeps_gitignored_payload_files(self):
        # A source commit may force-track files matching the exported tree's
        # .gitignore (or the user's global excludes): the commit must keep
        # every on-disk payload file, not just the unignored ones.
        target = Path(tempfile.mkdtemp(prefix="export-commit-ignore-"))
        self.addCleanup(shutil.rmtree, target, True)
        (target / ".gitignore").write_text("*.log\n")
        (target / "app.log").write_text("payload\n")
        (target / "keep.txt").write_text("payload\n")
        home = Path(tempfile.mkdtemp(prefix="export-commit-home-"))
        self.addCleanup(shutil.rmtree, home, True)
        (home / ".config" / "git").mkdir(parents=True)
        (home / ".config" / "git" / "ignore").write_text("keep.txt\n")
        env = {"GIT_CONFIG_GLOBAL": "/dev/null", "HOME": str(home)}
        with mock.patch.dict(os.environ, env):
            public_export.commit_tree(
                target, "Sterile export test", "2026-10-31T12:00:00+00:00"
            )
            tracked = (
                subprocess.run(
                    ["git", "-C", str(target), "ls-files"],
                    check=True,
                    stdout=subprocess.PIPE,
                    env={**os.environ, **env},
                )
                .stdout.decode()
                .split()
            )
        self.assertIn("app.log", tracked)
        self.assertIn("keep.txt", tracked)

    def test_commit_keeps_exact_bytes_despite_conversion(self):
        # Review #269: core.autocrlf/eol and attribute filters must not
        # rewrite payload bytes; the same source commits identically
        # on every machine.
        shas = []
        for prefix, hostile in (
            ("export-commit-crlf-plain-", False),
            ("export-commit-crlf-hostile-", True),
        ):
            target = Path(tempfile.mkdtemp(prefix=prefix))
            self.addCleanup(shutil.rmtree, target, True)
            (target / ".gitattributes").write_text("*.txt text eol=crlf\n")
            (target / "notes.txt").write_bytes(b"one\r\ntwo\r\n")
            env = (
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "core.autocrlf",
                    "GIT_CONFIG_VALUE_0": "true",
                }
                if hostile
                else {}
            )
            with mock.patch.dict(os.environ, env):
                shas.append(
                    public_export.commit_tree(
                        target,
                        "Sterile export test",
                        "2026-10-31T12:00:00+00:00",
                    )
                )
            stored = subprocess.run(
                ["git", "-C", str(target), "show", "HEAD:notes.txt"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertEqual(stored, b"one\r\ntwo\r\n")
        self.assertEqual(shas[0], shas[1])

    def test_commit_ignores_caller_git_dir(self):
        # Review #269: an export run from a hook (or any GIT_DIR-carrying
        # environment) must not read or clobber the caller's repository.
        victim = make_repo(self, {"v.txt": "victim\n"})
        before = (
            subprocess.run(
                ["git", "-C", str(victim), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        target = Path(tempfile.mkdtemp(prefix="export-commit-gitdir-"))
        self.addCleanup(shutil.rmtree, target, True)
        (target / "n.txt").write_text("clean\n")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(victim / ".git")}):
            sha = public_export.commit_tree(
                target, "Sterile export test", "2026-10-31T12:00:00+00:00"
            )
        after = (
            subprocess.run(
                ["git", "-C", str(victim), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        self.assertEqual(before, after)
        self.assertTrue((target / ".git").is_dir())
        self.assertRegex(sha, r"\A[0-9a-f]{40}\Z")

    def test_git_blob_ignores_caller_git_dir(self):
        # Review #269: manifest reads for the export must come from the
        # target repo even when the caller environment selects another one.
        src = make_repo(self, {".gitmodules": '[submodule "x"]\n', "n.txt": "clean\n"})
        other = make_repo(self, {"n.txt": "clean\n"})
        want = subprocess.run(
            ["git", "-C", str(src), "show", "HEAD:.gitmodules"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
            got = public_export.git_blob("HEAD", ".gitmodules", repo=src)
        self.assertEqual(got, want)

    def test_commit_ignores_caller_object_store(self):
        # Review #269: the export objects must land in the output repo even
        # when the caller environment points Git at an external object store.
        target = Path(tempfile.mkdtemp(prefix="export-commit-objstore-"))
        self.addCleanup(shutil.rmtree, target, True)
        (target / "n.txt").write_text("clean\n")
        store = Path(tempfile.mkdtemp(prefix="export-external-store-"))
        self.addCleanup(shutil.rmtree, store, True)
        with mock.patch.dict(os.environ, {"GIT_OBJECT_DIRECTORY": str(store)}):
            sha = public_export.commit_tree(
                target, "Sterile export test", "2026-10-31T12:00:00+00:00"
            )
        kind = (
            subprocess.run(
                ["git", "-C", str(target), "cat-file", "-t", sha],
                check=True,
                stdout=subprocess.PIPE,
            )
            .stdout.decode()
            .strip()
        )
        self.assertEqual(kind, "commit")

    def test_client_target_uses_own_submodule_allowlist(self):
        # Review #269: the client target must not inherit the workspace
        # public-submodule paths; a client-private URL at such a path must
        # be scrubbed from the export. The synthetic URL is split so no raw
        # literal resembling it ever appears in this file.
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        base = public_export.load_policy(str(CONFIG), str(PRIVATE))
        self.assertEqual(
            sorted(base["public_submodule_paths"]),
            [
                "client",
                "localmods/blockfield-mod",
                "localmods/create-fabric",
                "localmods/parcool-fabric",
                "localmods/simplebedrockmodel-fabric",
                "localmods/superbwarfare-fabric",
            ],
        )
        client = self.overlay_policy()
        self.assertEqual(client["public_submodule_paths"], [])
        policy = self.bare_policy()
        self.assertEqual(policy["public_submodule_paths"], [])
        stem = "https://example" + ".invalid/x-mod.git"
        modules = (
            '[submodule "localmods/create-fabric"]\n'
            "\tpath = localmods/create-fabric\n"
            f"\turl = {stem}\n"
        ).encode()
        files = {"localmods/create-fabric/note.txt": f"see {stem}\n".encode()}
        encoded = {r: (d, 0o644) for r, d in files.items()}
        payload, _, _ = public_export.build_payload(
            encoded, ["localmods/create-fabric"], [], [], modules, policy
        )
        text = payload["localmods/create-fabric/note.txt"][1].decode()
        self.assertIn("<private-submodule>", text)
        self.assertNotIn(stem, text)
        payload, _, _ = public_export.build_payload(
            encoded,
            ["localmods/create-fabric"],
            [],
            [],
            modules,
            base,
        )
        text = payload["localmods/create-fabric/note.txt"][1].decode()
        self.assertIn(stem, text)

    def test_commit_ignores_caller_template_dir(self):
        # Review #269: the export repo must not inherit the caller's Git
        # template directory (private hooks/files would land unscanned).
        target = Path(tempfile.mkdtemp(prefix="export-commit-template-"))
        self.addCleanup(shutil.rmtree, target, True)
        (target / "n.txt").write_text("clean\n")
        template = Path(tempfile.mkdtemp(prefix="export-foreign-template-"))
        self.addCleanup(shutil.rmtree, template, True)
        hooks = template / "hooks"
        hooks.mkdir()
        (hooks / "post-checkout").write_text("#!/bin/sh\necho foreign\n")
        with mock.patch.dict(os.environ, {"GIT_TEMPLATE_DIR": str(template)}):
            sha = public_export.commit_tree(
                target, "Sterile export test", "2026-10-31T12:00:00+00:00"
            )
        self.assertFalse((target / ".git" / "hooks" / "post-checkout").exists())
        self.assertRegex(sha, r"\A[0-9a-f]{40}\Z")

    def test_commit_pins_encoding(self):
        # Review #269: the caller's i18n.commitEncoding must not change the
        # export commit ID.
        shas = []
        for prefix, hostile in (
            ("export-commit-enc-plain-", False),
            ("export-commit-enc-latin-", True),
        ):
            target = Path(tempfile.mkdtemp(prefix=prefix))
            self.addCleanup(shutil.rmtree, target, True)
            (target / "n.txt").write_text("clean\n")
            env = (
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "i18n.commitEncoding",
                    "GIT_CONFIG_VALUE_0": "ISO-8859-1",
                }
                if hostile
                else {}
            )
            with mock.patch.dict(os.environ, env):
                shas.append(
                    public_export.commit_tree(
                        target,
                        "Sterile export test",
                        "2026-10-31T12:00:00+00:00",
                    )
                )
            raw = subprocess.run(
                ["git", "-C", str(target), "cat-file", "-p", shas[-1]],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            self.assertNotIn(b"\nencoding ", raw)
        self.assertEqual(shas[0], shas[1])

    def test_unreadable_tracked_gitmodules_fails_closed(self):
        # Review #269: a tracked export manifest that cannot be read must
        # fail the export, never silently skip the submodule gates.
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        repo = make_repo(
            self,
            {
                ".gitmodules": '[submodule "x"]\n\tpath = x\n',
                "n.txt": "clean\n",
            },
        )
        pub, priv = write_policy_files(self)
        out = Path(tempfile.mkdtemp(prefix="export-unreadable-"))
        self.addCleanup(shutil.rmtree, out, True)
        with mock.patch.object(public_export, "git_blob", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                public_export.export_tree("HEAD", out, pub, priv, repo=repo)
        self.assertIn(".gitmodules", str(ctx.exception))

    def test_commit_pins_object_format(self):
        # Review #269: the export commit must be SHA-1 even when the
        # machine prefers another object format (GIT_DEFAULT_HASH), or
        # the same source yields different commit IDs on different machines.
        shas = []
        for prefix, hostile in (
            ("export-commit-fmt-plain-", False),
            ("export-commit-fmt-sha256-", True),
        ):
            target = Path(tempfile.mkdtemp(prefix=prefix))
            self.addCleanup(shutil.rmtree, target, True)
            (target / "n.txt").write_text("clean\n")
            env = {"GIT_DEFAULT_HASH": "sha256"} if hostile else {}
            with mock.patch.dict(os.environ, env):
                shas.append(
                    public_export.commit_tree(
                        target,
                        "Sterile export test",
                        "2026-10-31T12:00:00+00:00",
                    )
                )
            fmt = (
                subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "--show-object-format"],
                    check=True,
                    stdout=subprocess.PIPE,
                )
                .stdout.decode()
                .strip()
            )
            self.assertEqual(fmt, "sha1")
        self.assertEqual(shas[0], shas[1])


if __name__ == "__main__":
    unittest.main()


MOD_CONFIG = HERE.parents[2] / "public_export_mod.json"

MOD_CANONICAL_LICENSE = b"Copyright (c) 2026 Blockfield. All rights reserved.\n"


class ModOverlayTest(unittest.TestCase):
    """Mod sterile-export overlay: server tree dropped, mod gates calibrated."""

    def overlay_policy(self):
        if not HAS_PRIVATE:
            self.skipTest("private export policy absent")
        return public_export.load_policy(str(CONFIG), str(PRIVATE), str(MOD_CONFIG))

    def bare_policy(self):
        """Overlay policy without the workflow inventory (content-scan fixtures)."""
        policy = dict(self.overlay_policy())
        policy.pop("allowed_workflows", None)
        return policy

    def test_resolve_mod_target(self):
        repo, config, private, overlay = public_export.resolve_target("mod")
        self.assertEqual(Path(overlay).name, "public_export_mod.json")
        self.assertEqual(Path(private).name, "public_export_private.json")
        self.assertEqual(Path(repo).name, "blockfield-mod")

    def test_overlay_file_is_public_safe(self):
        overlay = json.loads(MOD_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(overlay["repo"], "mod")
        if HAS_PRIVATE:
            policy = public_export.load_policy(str(CONFIG), str(PRIVATE))
            variants = set()
            for entry in policy["banned_tokens"]:
                token = entry["token"] if isinstance(entry, dict) else entry
                variants.add(token)
                variants.add(token.upper())
            text = MOD_CONFIG.read_text(encoding="utf-8")
            hits = [v for v in variants if v and v in text]
            self.assertEqual(hits, [], "private literals in mod overlay")

    def test_overlay_excludes_server_tree(self):
        policy = self.overlay_policy()
        # Every path in a3-dropped-server-files.txt lives under server/.
        self.assertIn("server/**", policy["exclude_paths"])
        self.assertIn("CREDITS.txt", policy["exclude_paths"])
        self.assertIn(".gitmodules", policy["exclude_paths"])
        self.assertIn(".dev/**", policy["exclude_paths"])

    def test_server_files_and_credits_absent(self):
        policy = self.bare_policy()
        files = {
            "server/build.gradle": (b"clean\n", 0o644),
            "server/src/main/java/com/lunares/blockfield/server/BlockFieldServer.java": (
                b"clean\n",
                0o644,
            ),
            "CREDITS.txt": (b"clean\n", 0o644),
            "client/src/main/java/Clean.java": (b"clean\n", 0o644),
        }
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertNotIn("server/build.gradle", payload)
        self.assertNotIn(
            "server/src/main/java/com/lunares/blockfield/server/BlockFieldServer.java",
            payload,
        )
        self.assertNotIn("CREDITS.txt", payload)
        self.assertFalse(
            [r for r in payload if r == "server" or r.startswith("server/")]
        )
        self.assertIn("client/src/main/java/Clean.java", payload)

    def test_overlay_keeps_mod_workflows(self):
        policy = self.overlay_policy()
        self.assertEqual(
            sorted(policy["allowed_workflows"]),
            [
                ".github/workflows/pr-text.yml",
                ".github/workflows/quality.yml",
                ".github/workflows/release.yml",
            ],
        )

    def test_mod_license_is_single_string(self):
        policy = self.bare_policy()
        files = {"LICENSE": (b"old license\n", 0o644)}
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertEqual(payload["LICENSE"][1], MOD_CANONICAL_LICENSE)

    def test_mod_single_commit(self):
        policy = self.overlay_policy()
        self.assertTrue(policy["single_commit"])
        self.assertIn("{sha}", policy["single_commit_message"])

    def test_known_packet_name_passes(self):
        # Split so this test file itself survives the export unchanged.
        packet = "Animation" + "WireC2SPacket"
        policy = self.bare_policy()
        files = {"common/src/Packets.java": (f"class {packet} {{}}\n".encode(), 0o644)}
        payload, _, _ = public_export.build_payload(files, [], [], [], None, policy)
        self.assertIn("common/src/Packets.java", payload)

    def test_fresh_opaque_token_still_fails(self):
        # Split so this test file itself survives the export unchanged.
        fresh = "TestToken9Ab" + "CdEfGhIjKlMn"
        policy = self.bare_policy()
        bad = {"common/src/Packets.java": (f"class {fresh} {{}}\n".encode(), 0o644)}
        with self.assertRaises(SystemExit) as ctx:
            public_export.build_payload(bad, [], [], [], None, policy)
        self.assertIn("f-opaque-bare", str(ctx.exception))

    def test_overlay_values_stay_escaped(self):
        # The workspace export scans this overlay raw: fixture values must
        # stay \u-escaped so pushing this file never trips that gate.
        text = MOD_CONFIG.read_text(encoding="utf-8")
        overlay = json.loads(text)
        values = []
        for patch in overlay.get("rule_patches", {}).values():
            values += patch.get("allow_exact", [])
        self.assertTrue(values, "mod overlay must pin reviewed fixture values")
        for value in values:
            self.assertNotIn(value, text)
